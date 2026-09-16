"""
feed_parser.py —— 抓取、解析、正文提取、HTML 清洗

职责：
    1. fetch_feed(url)          → 统一抓取入口（含本地生成的源，如 byread://bilibili/dynamic/xxx）
    2. extract_article_content  → 用 readability 提取正文
    3. sanitize_html            → 白名单清洗（服务端第一道防线，前端再用 DOMPurify 兜底）

安全红线落地方式：
    原需求文档写"绝对不使用 innerHTML 渲染 RSS 内容"。但 readability/feed 返回的就是 HTML，
    用 textContent 会把正文压成一坨没有段落的纯文本，阅读体验直接废掉。
    所以这里的正解是：服务端白名单清洗 + 只允许安全标签属性 + 前端 DOMPurify 二次清洗，
    而不是把 HTML 当纯文本渲染。
"""

from __future__ import annotations

import html as html_mod
import logging
import re
from typing import Optional
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
# 本地生成的源（byread://）
# --------------------------------------------------------------------------- #
def is_local_feed(feed_url: str) -> bool:
    return bool(feed_url) and feed_url.startswith(LOCAL_SCHEME)


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
    content_state：{guid: 是否已有正文}，用来跳过重复请求、并逐次补齐缺失的正文。
    """
    path = feed_url[len(LOCAL_SCHEME):].strip("/")
    parts = [p for p in path.split("/") if p]

    if len(parts) >= 3 and parts[0] == "bilibili" and parts[1] == "dynamic":
        import bilibili  # 局部导入，避免循环依赖

        return bilibili.fetch_user_dynamics(int(parts[2]), limit=limit)

    if len(parts) >= 2 and parts[0] == "bilibili" and parts[1] == "popular":
        import bilibili

        return bilibili.fetch_popular(limit=limit)

    if len(parts) >= 2 and parts[0] == "zhihu" and parts[1] == "daily":
        import zhihu

        # 知乎日报每篇正文要单独请求一次，所以条数不宜太多
        return zhihu.fetch_daily(limit=min(limit, 12), days=2, content_state=content_state)

    if len(parts) >= 3 and parts[0] == "zhihu" and parts[1] == "people":
        import cookies
        import zhihu

        return zhihu.fetch_user_content(
            parts[2], limit=limit, cookie=cookies.get_cookie("zhihu"),
            content_state=content_state,
        )

    if len(parts) >= 3 and parts[0] == "weibo" and parts[1] == "user":
        import cookies
        import weibo

        return weibo.fetch_user_weibo(
            parts[2], limit=limit, cookie=cookies.get_cookie("weibo"),
            content_state=content_state,
        )

    if len(parts) >= 2 and parts[0] == "github" and parts[1] == "trending":
        import github

        return github.fetch_trending(limit=limit)

    if len(parts) >= 2 and parts[0] == "gcores":
        import gcores

        return gcores.fetch_gcores(limit=limit)

    raise ValueError(f"未知的本地源类型：{feed_url}")


def local_feed_url(platform: str, kind: str, param) -> str:
    return f"{LOCAL_SCHEME}{platform}/{kind}/{param}"


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #
def fetch_feed(feed_url: str, timeout: int = TIMEOUT, limit: int = 20,
               content_state: Optional[dict] = None) -> dict:
    """
    统一抓取入口。返回：
    {title, site_url, description, icon, entries: [{guid,title,summary,content,link,author,published}]}
    抓取或解析失败会抛异常，由上层负责重试与错误计数。
    content_state：库里 {guid: 是否已有正文}，本地源用它跳过/补齐正文请求。
    """
    if is_local_feed(feed_url):
        result = fetch_local_feed(feed_url, limit=limit, content_state=content_state)
        if not result.get("entries"):
            log.info("本地源暂无内容：%s", feed_url)
        return result

    resp = _requests.get(feed_url, timeout=timeout, allow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    if not resp.content:
        raise RuntimeError("返回内容为空")

    parsed = feedparser.parse(resp.content)
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

    return {
        "guid": str(guid)[:500],
        "title": title or "(无标题)",
        "summary": summary_text,
        "content": full_html or None,
        "link": link,
        "author": author,
        "published": published,
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


def extract_article_content(url: str, timeout: int = TIMEOUT,
                            block_images: bool = False) -> Optional[str]:
    """
    打开原文链接，用 readability 提取正文；失败返回 None（前端回退显示摘要）。
    知乎/微博这类站点需要登录态，会带上对应平台的 Cookie。
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
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
        page_html = resp.text
    except Exception as exc:  # noqa: BLE001
        log.info("正文提取请求失败 %s：%s", url, exc)
        return None

    try:
        from readability import Document

        doc = Document(page_html)
        summary = doc.summary(html_partial=True)
    except Exception as exc:  # noqa: BLE001
        log.info("readability 提取失败 %s：%s", url, exc)
        return None

    cleaned = sanitize_html(summary, base_url=resp.url, block_images=block_images)
    if len(html_to_text(cleaned, 5000)) < 80:
        # 提取到的内容太短，视为失败，让前端回退到摘要
        return None
    return cleaned


def probe_feed_url(url: str) -> dict:
    """
    校验一个地址是否可用作订阅源。返回 {ok, title, site_url, icon, entry_count, error}
    （供"粘贴链接直接添加"用，避免添加一个抓不到的源）
    """
    try:
        data = fetch_feed(url, limit=1)
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
