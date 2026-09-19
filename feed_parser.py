"""
feed_parser.py —— 抓取、解析、正文提取、HTML 清洗

职责：
    1. fetch_feed(url)          → 统一抓取入口（含本地生成的源，如 byread://bilibili/dynamic/xxx）
    2. extract_article_content  → 抓页面 + 提取正文（readability → trafilatura 兜底）
    3. sanitize_html            → 白名单清洗（服务端第一道防线，前端再用 DOMPurify 兜底）

安全红线落地方式：
    原需求文档写"绝对不使用 innerHTML 渲染 RSS 内容"。但 readability/feed 返回的就是 HTML，
    用 textContent 会把正文压成一坨没有段落的纯文本，阅读体验直接废掉。
    所以这里的正解是：服务端白名单清洗 + 只允许安全标签属性 + 前端 DOMPurify 二次清洗，
    而不是把 HTML 当纯文本渲染。
"""

from __future__ import annotations

import html as html_mod
import importlib
import logging
import re
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from lxml import etree
from lxml import html as lxml_html

import net  # noqa: F401  统一网络初始化（让 Python 用系统证书库，而不是只认 certifi）

log = logging.getLogger("byread.feed")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT = 10
LOCAL_SCHEME = "byread://"

# "这个地址是不是订阅源"只需要读开头就能判定：条目都排在文件前部。
# 实测第一个 <item> 结束的位置——Syntax 12KB / Changelog 11KB / Julia Evans 12KB /
# 云风 6KB / 少数派 1KB（整份文件却有 9.8MB）。取 256KB 留了 20 倍富余，
# 万一某源的条目排在很后面，下面的逻辑会整份重试一次再判定。
# 为什么要这么做：整份下载经常顶到 10 秒超时红线，超时后又退回"自动发现"，
# 把同一份大文件又下两次，粘贴地址订阅要等半分钟才返回。
PROBE_MAX_BYTES = 256 * 1024

_requests = requests.Session()
_requests.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})

# --------------------------------------------------------------------------- #
# HTML 白名单
# --------------------------------------------------------------------------- #
ALLOWED_TAGS = {
    "p", "br", "hr", "div", "span", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "blockquote", "pre", "code", "kbd", "samp",
    "strong", "b", "em", "i", "u", "s", "del", "ins", "mark", "small", "sub", "sup",
    "a", "img", "figure", "figcaption", "picture",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
}
ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
    "col": {"span"},
    "colgroup": {"span"},
}
DROP_ENTIRELY = {
    "script", "style", "iframe", "frame", "frameset", "object", "embed", "applet",
    "form", "input", "textarea", "select", "option", "button", "label", "fieldset",
    "link", "meta", "base", "noscript", "template", "svg", "math", "canvas",
    "video", "audio", "source", "track", "map", "area", "dialog",
}
_VOID_TAGS = {"br", "hr", "img", "col"}


def _is_safe_url(url: str) -> bool:
    """只允许 http/https/mailto；挡掉 javascript:、data: 等。"""
    if not url:
        return False
    low = url.strip().lower()
    if low.startswith(("http://", "https://", "mailto:", "#", "/")):
        return True
    return False


_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
# 懒加载图片的真实地址常放在这些属性里，按优先级取（data-original 一般是最高清的原图）
_LAZY_ATTRS = ("data-original", "data-actualsrc", "data-src", "data-lazy-src",
               "data-echo", "data-original-src")


def _promote_lazy_images(html_text: str) -> str:
    """
    把懒加载图片的真实地址搬到 src 上。

    这类站点的 <img> 长这样（知乎、微博、大量新闻站都是）：
        <img src="data:image/svg+xml;utf8,..."   ← 占位图
             data-actualsrc="https://…/xxx_720w.jpg"
             data-original="https://…/xxx_r.jpg">  ← 原图

    src 是占位图（data: URI），会被当成"不安全地址"清理掉，整篇文章就一张图都不剩
    （实测：知乎一条回答 68 张图全丢）。所以先搬家再清洗。
    """
    def fix(match) -> str:
        tag = match.group(0)
        real = None
        for attr in _LAZY_ATTRS:
            m = re.search(rf"""\s{attr}\s*=\s*["']([^"']+)["']""", tag, re.IGNORECASE)
            if m and m.group(1).strip():
                real = m.group(1).strip()
                break
        if not real:
            return tag
        if re.search(r"""\ssrc\s*=\s*["'][^"']*["']""", tag, re.IGNORECASE):
            # 已经有 src：直接替换掉那一个（不能留两个 src，浏览器只认第一个）
            tag = re.sub(r"""\ssrc\s*=\s*["'][^"']*["']""", f' src="{real}"',
                         tag, count=1, flags=re.IGNORECASE)
        else:
            tag = tag[:-1] + f' src="{real}">'
        return tag

    try:
        return _IMG_TAG_RE.sub(fix, html_text)
    except Exception as exc:  # noqa: BLE001
        log.info("懒加载图片地址提升失败：%s", exc)
        return html_text


def sanitize_html(raw_html: Optional[str], base_url: Optional[str] = None,
                  block_images: bool = False) -> str:
    """
    白名单清洗 HTML 片段。任何异常都返回空串，绝不把脏 HTML 漏出去。
    """
    if not raw_html:
        return ""
    raw_html = _promote_lazy_images(raw_html)
    try:
        # 统一包一层容器再解析，避免多根节点问题
        parser = lxml_html.HTMLParser(encoding="utf-8", recover=True)
        root = lxml_html.fromstring(f"<div>{raw_html}</div>", parser=parser)
    except Exception:
        try:
            root = lxml_html.fromstring(f"<div>{raw_html}</div>")
        except Exception as exc:  # noqa: BLE001
            log.warning("HTML 解析失败：%s", exc)
            return ""
    if root is None:
        return ""

    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            # 注释 / 处理指令，直接删掉（保留尾随文本）
            _drop_keep_tail(el)
            continue

        tag = el.tag.lower()
        if tag in DROP_ENTIRELY:
            _drop_keep_tail(el)
            continue

        if tag not in ALLOWED_TAGS:
            # 未知标签：去壳留内容（比整段删除更保守，避免丢正文）
            try:
                el.drop_tag()
            except Exception:  # noqa: BLE001
                _drop_keep_tail(el)
            continue

        # 属性白名单
        # 注意：隐藏样式必须在剥离属性之前读取，否则 style 先被删掉，隐藏元素就漏出去了
        raw_style = (el.get("style") or "").lower().replace(" ", "")
        if "display:none" in raw_style or "visibility:hidden" in raw_style or "opacity:0" in raw_style:
            _drop_keep_tail(el)
            continue

        allowed = ALLOWED_ATTRS.get(tag, set())
        for attr in list(el.attrib.keys()):
            name = attr.lower()
            if name.startswith("on") or name not in allowed:
                del el.attrib[attr]
                continue
            value = el.attrib.get(attr) or ""
            if attr in ("href", "src"):
                if not _is_safe_url(value):
                    del el.attrib[attr]
                    continue
                # 相对地址补全为绝对地址
                if base_url and not value.startswith(("http://", "https://", "mailto:", "#")):
                    el.attrib[attr] = urljoin(base_url, value)

        if tag == "img":
            if block_images or not el.get("src"):
                # 屏蔽图片 / 没有可用地址（原本是 data:、 javascript: 或 1x1 追踪像素）
                _drop_keep_tail(el)
                continue
            if el.get("width") == "1" and el.get("height") == "1":
                _drop_keep_tail(el)
                continue
            el.set("loading", "lazy")
            el.set("referrerpolicy", "no-referrer")
        elif tag == "a":
            el.set("target", "_blank")
            el.set("rel", "noopener noreferrer")

    # 去空壳（清完属性后可能只剩空 div/span/p）
    for el in reversed(list(root.iter())):
        if not isinstance(el.tag, str) or el.tag.lower() in _VOID_TAGS:
            continue
        if el.tag.lower() in ("img", "br", "hr"):
            continue
        if len(el) == 0 and not (el.text or "").strip():
            _drop_keep_tail(el)

    try:
        inner = "".join(
            lxml_html.tostring(child, encoding="unicode", method="html")
            for child in root
        )
        if not inner.strip() and (root.text or "").strip():
            inner = f"<p>{html_mod.escape(root.text.strip())}</p>"
        return inner.strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("HTML 序列化失败：%s", exc)
        return ""


def _drop_keep_tail(el) -> None:
    """删除元素但保留其尾部文本，避免粘连丢字。"""
    try:
        parent = el.getparent()
        if parent is None:
            return
        if el.tail:
            previous = el.getprevious()
            if previous is not None:
                previous.tail = (previous.tail or "") + el.tail
            else:
                parent.text = (parent.text or "") + el.tail
        parent.remove(el)
    except Exception:  # noqa: BLE001
        pass


def html_to_text(raw_html: Optional[str], limit: int = 300) -> str:
    """HTML → 纯文本摘要（列表页用，天然安全）。"""
    if not raw_html:
        return ""
    try:
        text = lxml_html.fromstring(f"<div>{raw_html}</div>").text_content()
    except Exception:  # noqa: BLE001
        text = re.sub(r"<[^>]+>", " ", str(raw_html))
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


# --------------------------------------------------------------------------- #
# 正文完整性（"这份正文是不是被源截断了"）
#
# 为什么需要它：入库的正文有可能是**被截断的半截**（源只给了摘要级片段，
# 或者当时走的是另一条取正文的路），而库里只记了"正文版本号"，
# 于是系统以为它是完整的，**永远不会再取一次** —— 实测就是这么丢了 12 篇知乎回答的配图
# （库里 1346 字 vs 接口 6346 字，图一张不剩）。见 db.CONTENT_REPROCESS。
#
# 通用做法分三步，任何平台都能用：
#   1. 判定：平台模块发现"内容不完整"时，给条目加一个字段 item["content_incomplete"] = True；
#      自己判断不了就用这里的 looks_truncated()，或者用平台接口自己的标记
#      （知乎的 content_need_truncated、微博的 isLongText 之类）。
#   2. 表达：db 层看到这个字段就把 content_v 写成 0 而不是当前版本号 ——
#      也就是复用已有的"这正文不可信，下次刷新重取"语义，不用新增字段。
#   3. 重取：刷新时本来就会重取 content_v < CONTENT_VERSION 的条目；
#      取回来若明显更完整，update_article_if_incomplete 会覆盖旧内容。
# 误判的代价也很小：至多让这一篇下次刷新多取一次，取回来没变化就把版本号推进到当前值。
# --------------------------------------------------------------------------- #
TRUNCATION_MARKERS = (
    "展开阅读全文", "阅读全文", "展开全文", "查看全文", "查看全部", "点击展开",
    "read more", "continue reading", "show more", "see more",
)


def looks_truncated(html_text: Optional[str]) -> bool:
    """
    粗判"这段正文像是被截断的"。

    只看**结尾那一小段**（不然正文中间提到"阅读全文"就会误判），
    并且要求正文已经有一定长度（太短的内容本来就会因为"太短"被别的机制处理）。
    """
    if not html_text:
        return False
    text = html_to_text(html_text, 200000).strip()
    if len(text) < 40:
        return False
    tail = text[-30:].lower()
    return any(marker in tail for marker in TRUNCATION_MARKERS)


# --------------------------------------------------------------------------- #
# 本地生成的源（byread://）
#
# 平台名 → 处理函数 的**注册表**。以前这里是一条 if/elif 长链，加一个平台就要
# 在链路中间插一段；现在加平台 = 写一个函数 + 一个 @local_handler("平台名")，
# fetch_local_feed 只负责解析地址和查表。
#
# 处理函数签名统一为 (parts, limit, content_state, feed_url) -> dict，
# 其中 parts = ["平台", "类型", "参数"...]（已经去掉空段）。
# 类型不认识时**要抛 ValueError**（以前是整条链走完落到最后那行 raise，
# 现在由各自的处理函数负责，报错信息保持一致）。
# --------------------------------------------------------------------------- #
LocalFeedHandler = Callable[[list, int, Optional[dict], str], dict]

LOCAL_FEED_HANDLERS: dict[str, LocalFeedHandler] = {}


def local_handler(platform: str):
    """把一个函数登记成某个 byread:// 平台的处理函数。"""
    def register(fn: LocalFeedHandler) -> LocalFeedHandler:
        LOCAL_FEED_HANDLERS[platform] = fn
        return fn

    return register


def _unknown_local_feed(feed_url: str):
    """统一的报错（和以前那条长链走完落到最后一行时的信息保持一致）。"""
    raise ValueError(f"未知的本地源类型：{feed_url}")


@local_handler("bilibili")
def _local_bilibili(parts, limit, content_state, feed_url):
    kind = parts[1] if len(parts) > 1 else ""
    import bilibili      # 局部导入，避免循环依赖

    if kind == "dynamic" and len(parts) >= 3:
        return bilibili.fetch_user_dynamics(int(parts[2]), limit=limit)
    if kind == "popular":
        return bilibili.fetch_popular(limit=limit)
    _unknown_local_feed(feed_url)


@local_handler("zhihu")
def _local_zhihu(parts, limit, content_state, feed_url):
    kind = parts[1] if len(parts) > 1 else ""
    if kind == "daily":
        import zhihu
        # 知乎日报每篇正文要单独请求一次，所以条数不宜太多
        return zhihu.fetch_daily(limit=min(limit, 12), days=2, content_state=content_state)
    if kind == "people" and len(parts) >= 3:
        import cookies
        import zhihu
        return zhihu.fetch_user_content(
            parts[2], limit=limit, cookie=cookies.get_cookie("zhihu"),
            content_state=content_state,
        )
    _unknown_local_feed(feed_url)


@local_handler("weibo")
def _local_weibo(parts, limit, content_state, feed_url):
    kind = parts[1] if len(parts) > 1 else ""
    if kind == "user" and len(parts) >= 3:
        import cookies
        import weibo
        return weibo.fetch_user_weibo(
            parts[2], limit=limit, cookie=cookies.get_cookie("weibo"),
            content_state=content_state,
        )
    _unknown_local_feed(feed_url)


@local_handler("github")
def _local_github(parts, limit, content_state, feed_url):
    kind = parts[1] if len(parts) > 1 else ""
    if kind == "trending":
        import github
        return github.fetch_trending(limit=limit)
    _unknown_local_feed(feed_url)


@local_handler("gcores")
def _local_gcores(parts, limit, content_state, feed_url):
    # 机核只有 latest 一种（RSS 被 WAF 拦，改走官方 JSON API）
    if len(parts) >= 2:
        import gcores
        return gcores.fetch_gcores(limit=limit)
    _unknown_local_feed(feed_url)


def is_local_feed(feed_url: str) -> bool:
    return bool(feed_url) and feed_url.startswith(LOCAL_SCHEME)


# --------------------------------------------------------------------------- #
# 残文修复：平台名 → "按一条链接重取这一条正文"的函数
#
# 为什么单独有这么一条通路：刷新只能拿到**最近 N 条**，老文章早就滚出窗口了。
# 早期版本把知乎接口的截断正文当完整正文存下来（实测 12 篇少了全部配图），
# 光把它们标成"待重取"没用 —— 它们再也不会出现在抓取窗口里，没人去取。
# 所以每个平台可以登记一个"给一条已入库的链接，把这一条的正文重新取回来"的函数：
#     @content_refetcher("zhihu")
#     def refetch_content(link) -> tuple[Optional[str], bool]   # (正文, 是否完整)
# 刷新时会顺带把该源"有正文但标记为待重取"的老文章补一遍（有配额，几轮跑完）。
# --------------------------------------------------------------------------- #
ContentRefetcher = Callable[[str], tuple]


CONTENT_REFETCHERS: dict[str, ContentRefetcher] = {}


def content_refetcher(platform: str):
    """把一个函数登记成某个平台的"按链接重取正文"实现。"""
    def register(fn: ContentRefetcher) -> ContentRefetcher:
        CONTENT_REFETCHERS[platform] = fn
        return fn

    return register


def platform_of(feed_url: str) -> str:
    """从 byread://<平台>/... 里取出平台名；不是本地源就返回空串。"""
    if not is_local_feed(feed_url):
        return ""
    parts = [p for p in feed_url[len(LOCAL_SCHEME):].strip("/").split("/") if p]
    return parts[0] if parts else ""


def get_content_refetcher(platform: str) -> Optional[ContentRefetcher]:
    """
    取某个平台的"重取正文"函数；没有就返回 None。

    注意：平台的注册是在**模块被导入时**发生的，而各平台模块一直是懒加载的
    （避免循环依赖、也避免没用到的平台白加载）。所以查表前先把同名模块导进来 ——
    平台名与模块名一致（bilibili / zhihu / weibo / gcores / github），这条约定本来就在用。
    """
    if not platform:
        return None
    if platform not in CONTENT_REFETCHERS:
        try:
            importlib.import_module(platform)
        except Exception as exc:  # noqa: BLE001
            log.info("加载平台模块 %s 失败（该平台无法修复正文）：%s", platform, exc)
    return CONTENT_REFETCHERS.get(platform)


def fetch_local_feed(feed_url: str, limit: int = 20,
                     content_state: Optional[dict] = None) -> dict:
    """
    处理 byread:// 开头的本地源。目前支持：
        byread://bilibili/dynamic/{mid}   B 站某 UP 主的动态
        byread://bilibili/popular         B 站热门视频
        byread://zhihu/daily              知乎日报（公开接口）
        byread://zhihu/people/{token}     某人的知乎回答/文章/想法（需登录信息）
        byread://weibo/user/{uid}         某人的微博（需登录信息）
        byread://github/trending          GitHub 趋势
        byread://gcores/latest            机核

    平台分派走 LOCAL_FEED_HANDLERS 注册表（见上）；加平台不用改这个函数。
    content_state：{guid: 是否已有正文}，用来跳过重复请求、并逐次补齐缺失的正文。
    """
    path = feed_url[len(LOCAL_SCHEME):].strip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        raise ValueError(f"未知的本地源类型：{feed_url}")

    handler = LOCAL_FEED_HANDLERS.get(parts[0])
    if handler is None:
        raise ValueError(f"未知的本地源类型：{feed_url}")
    return handler(parts, limit, content_state, feed_url)


def local_feed_url(platform: str, kind: str, param) -> str:
    return f"{LOCAL_SCHEME}{platform}/{kind}/{param}"


# --------------------------------------------------------------------------- #
# 播客音频（enclosure）
#
# 音频地址**单独存一列**，绝不塞进正文：清洗白名单里 audio/video/source 属于
# DROP_ENTIRELY（整段删除），那是防注入的红线，不能为了渲染播放器就放开。
# 阅读页拿 articles.audio_url 自己造 <audio> 元素，不经过富文本。
# --------------------------------------------------------------------------- #
AUDIO_EXTENSIONS = (
    ".mp3", ".m4a", ".m4b", ".mp4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac",
)
# 超过这个时长的一律当解析错误丢掉（播客再长也到不了 24 小时）
_MAX_AUDIO_SECONDS = 24 * 3600


def parse_duration(value) -> Optional[int]:
    """
    itunes:duration → 秒。源里的写法很杂：3637（int 或 str）、"1:02:03"、"02:03"。
    认不出来就返回 None（宁可不显示时长，也不显示一个错的）。
    """
    try:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            seconds = int(value)
        else:
            text = str(value).strip()
            if not text:
                return None
            if text.isdigit():
                seconds = int(text)
            else:
                parts = text.split(":")
                if len(parts) not in (2, 3):
                    return None
                seconds = 0
                for part in parts:          # "1:02:03" 从高位往低位累加
                    seconds = seconds * 60 + int(part)
        return seconds if 0 < seconds <= _MAX_AUDIO_SECONDS else None
    except (TypeError, ValueError):
        log.debug("时长解析失败：%r", value)
        return None


def _enclosure_candidates(entry) -> list[dict]:
    """
    汇总所有可能是附件的地方。

    feedparser 把 <enclosure> 同时放进了 entry.enclosures 和 entry.links（rel=enclosure），
    而 entry.enclosures 是派生出来的键 —— 用 .get() 取得到，但不在 entry.keys() 里
    （实测：用 keys() 判断会以为这个源没有音频，其实有）。两边都读一遍再按地址去重，
    顺便兼容 Podcasting 2.0 里常见的 media:content。
    """
    found: list[dict] = []
    for source in (entry.get("enclosures"), entry.get("links"), entry.get("media_content")):
        for item in source or []:
            if isinstance(item, dict):
                found.append(item)
    return found


def pick_audio(entry) -> tuple[Optional[str], Optional[int]]:
    """
    取第一个音频附件，返回 (地址, 时长秒数)；没有音频返回 (None, None)。

    只认 audio/*：视频播客（Syntax、很多访谈类）会把 video/mp4 放在 audio 前面，
    不判断类型的话阅读页会挂上一个放不出声的播放器。完全没写 type 的按扩展名兜底。
    """
    seen: set[str] = set()
    for enc in _enclosure_candidates(entry):
        href = str(enc.get("href") or enc.get("url") or "").strip()
        if not href or href in seen:
            continue
        seen.add(href)
        if not href.lower().startswith(("http://", "https://")):
            continue
        enc_type = str(enc.get("type") or "").strip().lower()
        if enc_type.startswith("audio/"):
            return href, parse_duration(entry.get("itunes_duration"))
        if not enc_type and href.split("?")[0].lower().endswith(AUDIO_EXTENSIONS):
            # 大多数播客源都写了 type；这里只是不让"漏写 type"的源丢掉音频
            return href, parse_duration(entry.get("itunes_duration"))
    return None, None


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #
def _read_capped(resp, max_bytes: int) -> tuple[bytes, bool]:
    """只读响应开头的 max_bytes 字节。返回 (内容, 是否被截断)。"""
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(64 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                return b"".join(chunks)[:max_bytes], True
    except Exception as exc:  # noqa: BLE001
        # 读到一半断了：不知道后面还有没有，一律当截断处理（由上层决定要不要整份重来）
        log.info("读取响应中断，按截断处理：%s", exc)
        return b"".join(chunks), True
    return b"".join(chunks), False


_CHARSET_DECL_RE = re.compile(rb"""encoding\s*=\s*["']([\w.:-]+)["']""", re.IGNORECASE)


def _decode_by_declaration(raw: bytes):
    """
    截断读取时，先按文档自己声明的编码解成字符串再交给 feedparser。

    为什么必须这么做：feedparser 在**残缺**文档上会放弃 XML 声明、改用猜测的编码。
    实测人民网的源（声明 UTF-8、标题写在 CDATA 里）截成 256KB 后，
    feed.title 变成按 iso-8859-2 解出来的乱码（"时政频道" → "æ—¶æ”¿é¢‘é“…"），
    于是"粘贴地址订阅"会把乱码当源名写进库 —— 而且它看着不像占位符，之后再也不会自愈。
    完整文档没有这个问题，所以只在这条（截断）路径上动手，其余情况一律交给 feedparser。
    """
    if not raw:
        return raw
    match = _CHARSET_DECL_RE.search(raw[:2048])
    if not match:
        return raw
    encoding = match.group(1).decode("ascii", "ignore").strip()
    try:
        return raw.decode(encoding, "replace")
    except (LookupError, UnicodeError) as exc:
        log.info("按声明编码 %s 解码失败，交回 feedparser 判断：%s", encoding, exc)
        return raw


def fetch_feed(feed_url: str, timeout: int = TIMEOUT, limit: int = 20,
               content_state: Optional[dict] = None,
               max_bytes: Optional[int] = None) -> dict:
    """
    统一抓取入口。返回：
    {title, site_url, description, icon,
     entries: [{guid,title,summary,content,link,author,published,audio_url,audio_duration}]}
    抓取或解析失败会抛异常，由上层负责重试与错误计数。
    content_state：库里 {guid: 是否已有正文}，本地源用它跳过/补齐正文请求。
    max_bytes：只读开头这么多字节再解析（只用于"这是不是个源"的探测，正式抓取必须传 None）。
    """
    if is_local_feed(feed_url):
        result = fetch_local_feed(feed_url, limit=limit, content_state=content_state)
        if not result.get("entries"):
            log.info("本地源暂无内容：%s", feed_url)
        return result

    truncated = False
    resp = _requests.get(feed_url, timeout=timeout, allow_redirects=True,
                         stream=bool(max_bytes))
    try:
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        if max_bytes:
            raw, truncated = _read_capped(resp, max_bytes)
        else:
            raw = resp.content
    finally:
        resp.close()
    if not raw:
        raise RuntimeError("返回内容为空")
    if truncated:
        # 截断的文档要自己按声明编码解码，否则 feedparser 会瞎猜编码（标题会变乱码）
        raw = _decode_by_declaration(raw)

    parsed = feedparser.parse(raw)
    entries = parsed.get("entries") or []
    if not entries and parsed.get("bozo"):
        if truncated:
            # 只读了开头、一条条目都没解出来：也可能是条目排在文件很后面（少见）。
            # 宁可慢一次，也不能把本来可用的源判成不可用
            log.info("截断探测没解析出条目，整份重试：%s", feed_url)
            full = _requests.get(feed_url, timeout=timeout, allow_redirects=True)
            if full.status_code >= 400:
                raise RuntimeError(f"HTTP {full.status_code}")
            raw = full.content          # 整份就是完整的，不用再按声明解码
            parsed = feedparser.parse(raw)
            entries = parsed.get("entries") or []
        if not entries and parsed.get("bozo"):
            # 不是有效的 feed
            raise RuntimeError("无法解析为订阅源内容")

    feed_meta = parsed.get("feed") or {}
    title = (feed_meta.get("title") or "").strip() or urlparse(feed_url).netloc
    site_url = (feed_meta.get("link") or "").strip() or None
    description = html_to_text(feed_meta.get("subtitle") or "", 200) or None
    icon = None
    image = feed_meta.get("image") or {}
    if isinstance(image, dict):
        icon = image.get("href") or image.get("url")
    if not icon:
        for link in feed_meta.get("links") or []:
            if isinstance(link, dict) and link.get("rel") == "icon":
                icon = link.get("href")
                break

    items = []
    for entry in entries[:limit]:
        try:
            item = _entry_to_item(entry, feed_url)
            if item:
                items.append(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("解析条目失败：%s", exc)

    return {
        "title": title,
        "site_url": site_url,
        "description": description,
        "icon": icon,
        "entries": items,
    }


def _entry_to_item(entry, feed_url: str) -> Optional[dict]:
    """feedparser 条目 → 统一结构。"""
    title = html_to_text(entry.get("title") or "", 500)
    link = (entry.get("link") or "").strip() or None
    author = (entry.get("author") or "").strip() or None

    # 兜底：有些源不给 <link>，但 <id>/<guid> 本身就是原文地址（Atom 很常见）。
    # 用户要求"每条都带原文链接"，所以这里补一道。
    if not link:
        candidate = str(entry.get("id") or entry.get("guid") or "").strip()
        if candidate.lower().startswith(("http://", "https://")):
            link = candidate

    published = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
        or entry.get("created_parsed")
    )

    # 正文：优先 content，其次 summary
    raw_content = ""
    content_list = entry.get("content") or []
    if content_list and isinstance(content_list, list):
        raw_content = (content_list[0] or {}).get("value") or ""
    raw_summary = entry.get("summary") or entry.get("description") or ""

    summary_text = html_to_text(raw_summary or raw_content, 300)
    full_html = ""
    if raw_content:
        full_html = sanitize_html(raw_content, base_url=link or feed_url)
    if full_html and len(html_to_text(full_html, 5000)) < 120 and raw_summary:
        # content 太短，补充 summary
        extra = sanitize_html(raw_summary, base_url=link or feed_url)
        full_html = (full_html + extra).strip()

    guid = (
        entry.get("id")
        or entry.get("guid")
        or link
        or f"{title}|{entry.get('published') or ''}"
    )

    if not title and not link:
        return None

    # 播客：音频地址单独给一个字段（正文里不会有 <audio>，那类标签在清洗时被整段删掉）
    audio_url, audio_duration = pick_audio(entry)

    return {
        "guid": str(guid)[:500],
        "title": title or "(无标题)",
        "summary": summary_text,
        "content": full_html or None,
        "link": link,
        "author": author,
        "published": published,
        "audio_url": audio_url,
        "audio_duration": audio_duration,
    }


# --------------------------------------------------------------------------- #
# 正文提取
# --------------------------------------------------------------------------- #
def _browser_headers(url: str) -> dict:
    """
    按域名组装请求头。知乎/微博会拦截无登录态的抓取（实测：知乎专栏页不带 Cookie 直接 403），
    所以这两个域名自动带上用户配置的登录信息 —— 只发给该域名本身。
    """
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        import cookies

        host = (urlparse(url).netloc or "").lower()
        platform = None
        if "zhihu" in host:
            platform = "zhihu"
            headers["Referer"] = "https://www.zhihu.com/"
        elif "weibo" in host:
            platform = "weibo"
            headers["Referer"] = "https://m.weibo.cn/"
        if platform:
            cookie = cookies.get_cookie(platform)
            if cookie:
                headers["Cookie"] = cookie
    except Exception as exc:  # noqa: BLE001
        log.info("组装请求头时跳过登录信息：%s", exc)
    return headers


# 正文提取的门槛：清洗后的纯文本短于这个长度，就当"这篇没提到正文"
MIN_CONTENT_CHARS = 80


def _fetch_page(url: str, timeout: int) -> Optional[tuple[str, str]]:
    """
    抓原文页面，返回 (HTML, 最终地址)；失败返回 None（不抛异常）。

    **只有这一层联网。** 提取一律是对"已经抓好的 HTML"做纯处理 ——
    这样 timeout / 代理 / UA / Cookie 全是我们说了算，不会因为换/加了提取库就绕开它们
    （trafilatura 自带 fetch_url，本文件一律不用它）。
    """
    try:
        resp = _requests.get(url, timeout=timeout, allow_redirects=True,
                             headers=_browser_headers(url))
        if resp.status_code >= 400:
            log.info("正文提取 HTTP %s：%s", resp.status_code, url)
            return None
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype:
            return None
        if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text, resp.url
    except Exception as exc:  # noqa: BLE001
        log.info("正文提取请求失败 %s：%s", url, exc)
        return None


def _clean_and_check(raw_html: Optional[str], base_url: str,
                     block_images: bool) -> Optional[str]:
    """
    提取结果**统一从这里出去**：清洗 → 太短算失败。返回 None 表示"这篇没提到"。

    两个引擎都走这一条路，好处是清洗规则只有一份，
    不会出现"某个引擎的结果绕过了白名单"这种事。
    """
    if not raw_html:
        return None
    cleaned = sanitize_html(raw_html, base_url=base_url, block_images=block_images)
    if len(html_to_text(cleaned, 5000)) < MIN_CONTENT_CHARS:
        return None
    return cleaned


def _looks_like_article_list(cleaned: str) -> bool:
    """
    识别"链接汤"：段落很多、但每段都极短。

    这是实测踩到的坑：B 站/知乎这类内容靠 JS 渲染的页面，HTML 里根本没有正文，
    trafilatura 会把手边的"相关视频 / 推荐阅读"那一列标题当成正文提出来。
    人工对比过两边：
        推荐位列表：20~41 段，最长的一段 18~73 字
        真正文（含短回答）：单段 136~143 字，或 76 段里最长 47 字
    所以判据取"段落 ≥10 且最长的一段 <100 字"——
    它只在 trafilatura 这条兜底路径上生效，readability 的结果一个都不拦，
    避免"本来能看的正文反而被这条规则挡掉"。
    """
    try:
        doc = lxml_html.fromstring(f"<div>{cleaned}</div>")
        texts = [" ".join((el.text_content() or "").split()) for el in doc.xpath(".//p")]
        texts = [t for t in texts if t]
        if len(texts) < 10:
            return False
        return max(len(t) for t in texts) < 100
    except Exception as exc:  # noqa: BLE001
        log.info("判断是否链接汤失败（按正文处理）：%s", exc)
        return False


def _extract_by_trafilatura(page_html: str, base_url: str,
                            block_images: bool) -> Optional[str]:
    """
    二级兜底：trafilatura。**只调它的提取函数**，HTML 由我们抓好传进去 ——
    绝不用它自带的 fetch_url（那会绕过我们的 timeout / 代理 / UA / Cookie）。
    输出要 HTML（不是 txt / markdown），才能和 readability 共用同一个清洗出口。
    """
    try:
        import trafilatura

        html = trafilatura.extract(
            page_html,
            url=base_url,
            output_format="html",
            include_images=not block_images,   # 它默认 False，不显式打开会一张图都不剩
            include_links=True,
            include_formatting=True,
            include_tables=True,
            favor_recall=True,                 # 它只在"readability 没提到"时上场，宁可多提
        ) or None
    except Exception as exc:  # noqa: BLE001
        log.info("trafilatura 提取失败 %s：%s", base_url, exc)
        return None

    if html and _looks_like_article_list(html):
        # 提到了推荐位列表 —— 这比"回退到摘要"更糟，宁可当没提到
        log.info("trafilatura 只提到一串列表（疑似推荐位），按失败处理：%s", base_url)
        return None
    return html


def extract_from_html(page_html: str, base_url: str,
                      block_images: bool = False) -> tuple[Optional[str], str]:
    """
    纯提取（不联网）。返回 (清洗后的正文, 用了哪个引擎)；都没提到返回 (None, "none")。

    顺序：readability（快而准，绝大多数站够用）
          → 太短或报错 → trafilatura（对知乎、少数派这类结构复杂的页面更稳）
          → 还是不行就 None，由前端回退显示摘要。

    readability 达标时**不会再跑 trafilatura** —— 所以这个改造只会把原来失败的补上，
    不可能让原来成功的变差。
    """
    try:
        from readability import Document

        summary = Document(page_html).summary(html_partial=True)
    except Exception as exc:  # noqa: BLE001
        log.info("readability 提取失败 %s：%s", base_url, exc)
        summary = None

    cleaned = _clean_and_check(summary, base_url, block_images)
    if cleaned:
        return cleaned, "readability"

    cleaned = _clean_and_check(_extract_by_trafilatura(page_html, base_url, block_images),
                               base_url, block_images)
    if cleaned:
        return cleaned, "trafilatura"
    return None, "none"


def extract_article_content(url: str, timeout: int = TIMEOUT,
                            block_images: bool = False) -> Optional[str]:
    """
    打开原文链接并提取正文；失败返回 None（前端回退显示摘要），**不抛异常**。
    知乎/微博这类站点需要登录态，会带上对应平台的 Cookie。
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    page = _fetch_page(url, timeout)
    if not page:
        return None
    page_html, final_url = page
    content, engine = extract_from_html(page_html, final_url, block_images)
    if not content:
        return None
    # 记下是哪个引擎提的、提了多少字：以后"这篇怎么只有一点点"能直接从日志看出来
    # （这里用一个大 limit 量全文长度，别被 html_to_text 的默认截断骗了）
    log.info("正文提取成功（%s，%d 字）：%s", engine,
             len(html_to_text(content, 1000000)), final_url)
    return content


def probe_feed_url(url: str) -> dict:
    """
    校验一个地址是否可用作订阅源。返回 {ok, title, site_url, icon, entry_count, error}
    （供"粘贴链接直接添加"用，避免添加一个抓不到的源）

    只读开头一小段（PROBE_MAX_BYTES = 256KB）来判定，避免大源（播客源整份接近 10MB）
    把订阅接口拖成几十秒。
    """
    try:
        data = fetch_feed(url, limit=1, max_bytes=PROBE_MAX_BYTES)
        return {
            "ok": True,
            "title": data.get("title") or url,
            "site_url": data.get("site_url"),
            "icon": data.get("icon"),
            "description": data.get("description"),
            "entry_count": len(data.get("entries") or []),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "title": None, "error": str(exc)}


# --------------------------------------------------------------------------- #
# 从普通网页里"自动发现"订阅地址
# --------------------------------------------------------------------------- #
# <link rel="alternate"> 里算订阅源的 type
_FEED_LINK_TYPES = ("application/rss+xml", "application/atom+xml", "application/xml",
                    "text/xml", "application/rdf+xml", "application/feed+json")
# 找不到 link 标签时，依次试这些常见路径
COMMON_FEED_PATHS = ("/feed", "/rss", "/atom.xml", "/feed.xml", "/index.xml", "/rss.xml")
_FEEDISH_PATH_RE = re.compile(r"(^|/)(feed|rss|atom)(\.(xml|rss|json))?$", re.IGNORECASE)


def _looks_like_feed_path(path: str) -> bool:
    p = (path or "").rstrip("/").lower()
    if not p or p.endswith(".html"):
        return False
    return bool(_FEEDISH_PATH_RE.search(p)) or p.endswith((".xml", ".rss"))


def extract_feed_links(page_url: str, page_html: str) -> list[str]:
    """
    从网页 HTML 里挖出候选订阅地址，按可信度排序：
      1. <link rel="alternate" type="application/rss+xml|atom+xml|...">（最标准）
      2. 页面里指向 feed/rss/atom 的 <a>（有些站点只在页脚放个链接）
    相对地址一律用 urljoin 补全。
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(href: str) -> None:
        if not href:
            return
        try:
            absolute = urljoin(page_url, href.strip())
        except Exception:  # noqa: BLE001
            return
        if not absolute.lower().startswith(("http://", "https://")):
            return
        if absolute in seen:
            return
        seen.add(absolute)
        out.append(absolute)

    try:
        doc = lxml_html.fromstring(page_html)
    except Exception as exc:  # noqa: BLE001
        log.info("解析页面找订阅地址失败：%s", exc)
        return []

    for el in doc.xpath("//link[@href]"):
        rel = (el.get("rel") or "").lower()
        typ = (el.get("type") or "").lower()
        href = el.get("href") or ""
        if "feed" in rel:
            add(href)
            continue
        if "alternate" not in rel:
            continue
        if any(t in typ for t in _FEED_LINK_TYPES) or _looks_like_feed_path(urlparse(href).path):
            add(href)

    for el in doc.xpath("//a[@href]"):
        href = el.get("href") or ""
        if any(t in href.lower() for t in ("feed", "rss", "atom")) and \
                _looks_like_feed_path(urlparse(href).path):
            add(href)

    return out


def candidate_feed_urls(page_url: str) -> list[str]:
    """
    在**不发请求**的前提下，列出这个页面所有可能的订阅地址：
    link 标签 / 页面内链接 + 常见路径。
    常见路径会同时拼在"域名根"和"页面所在目录"下面 ——
    例如 https://www.ruanyifeng.com/blog/ 的源其实在 /blog/atom.xml，只试根目录会漏掉。
    """
    parsed = urlparse(page_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    directory = page_url if page_url.endswith("/") else page_url.rsplit("/", 1)[0] + "/"

    urls: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        if u not in seen:
            seen.add(u)
            urls.append(u)

    for base in (origin, directory):
        for path in COMMON_FEED_PATHS:
            add(base.rstrip("/") + path)
    return urls


def discover_feeds(page_url: str, timeout: int = TIMEOUT, need: int = 3,
                   max_checks: int = 10) -> list[dict]:
    """
    从一个普通网页里自动找出可用的订阅地址。

    流程（按需求文档）：
      1. GET 该页（带浏览器 UA，timeout ≤10s）
      2. 先看 <link rel="alternate">，再试常见路径（/feed /rss /atom.xml ...）
      3. **每个候选都必须真实解析成功**才算数（复用 probe_feed_url）——
         绝不允许把首页 HTML 当成 feed 存进库
    最多检查 max_checks 个候选，凑够 need 个能用的就提前返回。
    """
    if not page_url or not page_url.lower().startswith(("http://", "https://")):
        return []

    candidates: list[str] = []
    try:
        resp = _requests.get(page_url, timeout=timeout, allow_redirects=True,
                             headers={"User-Agent": UA,
                                      "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        final_url = resp.url
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if resp.status_code < 400 and "html" in ctype:
            if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
                resp.encoding = resp.apparent_encoding or "utf-8"
            candidates = extract_feed_links(final_url, resp.text)
            page_url = final_url
        elif resp.status_code < 400:
            # 这个地址本身可能就是一个 feed
            candidates = [final_url]
    except Exception as exc:  # noqa: BLE001
        log.info("打开页面失败（继续试常见路径）：%s %s", page_url, exc)

    for url in candidate_feed_urls(page_url):
        if url not in candidates:
            candidates.append(url)

    results: list[dict] = []
    for url in candidates[:max_checks]:
        probe = probe_feed_url(url)
        if not probe.get("ok"):
            continue
        if not probe.get("entry_count"):
            # 能解析但一条都读不出来：当作不可用，避免订到一个空壳
            continue
        results.append({
            "feed_url": url,
            "title": probe.get("title") or urlparse(url).netloc,
            "site_url": probe.get("site_url") or page_url,
            "icon": probe.get("icon"),
            "description": probe.get("description"),
            "entry_count": probe.get("entry_count"),
        })
        if len(results) >= need:
            break
    log.info("自动发现：检查 %s 个候选，可用 %s 个（%s）",
             min(len(candidates), max_checks), len(results), page_url)
    return results
