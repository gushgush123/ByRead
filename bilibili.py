"""
bilibili.py —— B 站原生支持（不依赖 RSSHub）

为什么单独做这个模块：
    实测发现 RSSHub 公共实例对 B 站/微博/知乎这类"需要 Cookie"的命名空间普遍 429 限流，
    而 B 站官方的搜索接口和空间动态接口只要带上 wbi 签名 + 一个 buvid3 Cookie 就能稳定返回。
    所以 B 站这条链路完全原生实现：搜索博主 → 拿 mid → 直接生成文章条目。
    对用户而言，体验就是"输入博主名 → 选中 → 开始读"，中间没有任何技术术语。

实现要点：
    1. wbi 签名：从 nav 接口取 img_key/sub_key，按固定置换表混合成 mixin key，
       再对参数做 md5(query + mixin_key) 得到 w_rid。
    2. Cookie：先访问一次 www.bilibili.com 拿到 buvid3（没有它接口会返回 412）。
    3. 密钥与 Cookie 带 TTL 缓存，失败时自动重建一次。
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import threading
import time
import urllib.parse
from typing import Any, Optional

import requests

import net  # noqa: F401  统一网络初始化

log = logging.getLogger("byread.bilibili")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT = 10

# wbi 混合密钥置换表（B 站前端固定值）
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

# 动态类型的展示文案
_TYPE_LABEL = {
    "DYNAMIC_TYPE_AV": "视频",
    "DYNAMIC_TYPE_DRAW": "图文",
    "DYNAMIC_TYPE_WORD": "动态",
    "DYNAMIC_TYPE_ARTICLE": "专栏",
    "DYNAMIC_TYPE_FORWARD": "转发",
    "DYNAMIC_TYPE_LIVE_RCMD": "直播",
    "DYNAMIC_TYPE_PGC": "番剧",
}


class BilibiliError(RuntimeError):
    """B 站接口异常（对外只暴露人话文案）。"""


class _Session:
    """带 wbi 密钥与 Cookie 缓存的会话（惰性初始化 + TTL）。"""

    KEY_TTL = 30 * 60  # 密钥 30 分钟过期

    def __init__(self) -> None:
        self._s = requests.Session()
        self._s.headers.update(
            {
                "User-Agent": UA,
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        self._lock = threading.Lock()
        self._key: Optional[str] = None
        self._key_at = 0.0
        self._last_call = 0.0

    # -- 内部工具 ---------------------------------------------------------
    def _throttle(self) -> None:
        """两次请求之间至少间隔 0.4 秒，避免触发风控。"""
        delta = time.time() - self._last_call
        if delta < 0.4:
            time.sleep(0.4 - delta)
        self._last_call = time.time()

    def _bootstrap_cookie(self) -> None:
        if "buvid3" in self._s.cookies:
            return
        try:
            self._s.get("https://www.bilibili.com/", timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            log.warning("获取 B 站 Cookie 失败：%s", exc)

    def _mixin_key(self, raw: str) -> str:
        return "".join(raw[i] for i in _MIXIN_TAB)[:32]

    def _ensure_key(self, force: bool = False) -> str:
        with self._lock:
            fresh = self._key and (time.time() - self._key_at) < self.KEY_TTL
            if fresh and not force:
                return self._key  # type: ignore[return-value]
            self._bootstrap_cookie()
            self._throttle()
            try:
                r = self._s.get(
                    "https://api.bilibili.com/x/web-interface/nav", timeout=TIMEOUT
                )
                data = (r.json() or {}).get("data") or {}
                wbi = data.get("wbi_img") or {}
                img = wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
                sub = wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0]
                self._key = self._mixin_key(img + sub)
                self._key_at = time.time()
                return self._key
            except Exception as exc:  # noqa: BLE001
                raise BilibiliError("无法连接 B 站，请稍后再试") from exc

    def _sign(self, params: dict) -> str:
        key = self._ensure_key()
        params = dict(params, wts=int(time.time()))
        clean = {
            k: re.sub(r"[!'()*]", "", str(v)) for k, v in sorted(params.items())
        }
        query = urllib.parse.urlencode(clean)
        params["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
        return urllib.parse.urlencode(sorted(params.items()))

    def get_json(self, url: str, params: Optional[dict] = None,
                 signed: bool = False, timeout: Optional[float] = None) -> dict:
        """发起请求并解析 JSON，失败重试一次（含重建密钥）。"""
        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                self._throttle()
                if signed:
                    qs = self._sign(params or {})
                    full = f"{url}?{qs}"
                else:
                    full = url
                r = self._s.get(full, params=None if signed else (params or {}),
                                timeout=(timeout or TIMEOUT))
                if r.status_code == 412:
                    # 412 = 风控，重建 Cookie/密钥后重试
                    self._s.cookies.clear()
                    self._key = None
                    raise BilibiliError("被 B 站风控拦截")
                r.raise_for_status()
                data = r.json()
                code = data.get("code")
                if code not in (0, None):
                    raise BilibiliError(f"B 站接口返回错误：{data.get('message') or code}")
                return data
            except BilibiliError as exc:
                last_err = exc
                if attempt == 0:
                    self._bootstrap_cookie()
                    self._key = None
                    continue
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt == 0:
                    time.sleep(1.0)
                    continue
        raise BilibiliError(f"B 站接口访问失败：{last_err}")


_session = _Session()


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def search_users(keyword: str, limit: int = 6, timeout: Optional[float] = None) -> list[dict]:
    """
    按关键词搜索 B 站 UP 主。
    返回 [{mid, uname, fans, sign, avatar, platform}]
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    try:
        data = _session.get_json(
            "https://api.bilibili.com/x/web-interface/wbi/search/type",
            {"search_type": "bili_user", "keyword": keyword, "page": 1},
            signed=True,
            timeout=timeout,
        )
    except BilibiliError as exc:
        log.warning("B 站搜索失败：%s", exc)
        raise

    results = []
    for item in ((data.get("data") or {}).get("result") or [])[:limit]:
        uname = re.sub(r"<[^>]+>", "", str(item.get("uname") or "")).strip()
        mid = item.get("mid")
        if not mid or not uname:
            continue
        results.append(
            {
                "mid": int(mid),
                "uname": uname,
                "fans": item.get("fans"),
                "sign": (item.get("usign") or "")[:60],
                "avatar": _normalize_url(item.get("upic")),
                "platform": "B站",
            }
        )
    return results


def get_user_info(mid: int) -> dict:
    """
    取 UP 主基本信息（用于订阅源的标题/图标）。
    注意：/x/space/wbi/acc/info 现在需要 dm_img 系列风控参数，基本必然失败，
    所以改用 web-interface/card（无需签名，稳定得多）。
    """
    try:
        data = _session.get_json(
            "https://api.bilibili.com/x/web-interface/card", {"mid": int(mid)}
        )
        card = (data.get("data") or {}).get("card") or {}
        return {
            "name": card.get("name") or f"B站用户 {mid}",
            "face": _normalize_url(card.get("face")),
            "sign": card.get("sign") or "",
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("读取 B 站用户信息 %s 失败：%s", mid, exc)
        return {"name": f"B站用户 {mid}", "face": None, "sign": ""}


def fetch_user_dynamics(mid: int, limit: int = 20) -> dict:
    """
    抓取 UP 主的空间动态，转换成统一的文章条目结构。

    重要：这个接口会随机返回空列表（同一账号连续请求可能 13/0/13/0 条），
    属于它的反爬行为，不是"该博主没有动态"。所以这里要重试到拿到数据为止。

    返回 {"title": 博主名, "site_url": 空间地址, "icon": 头像, "entries": [...]}
    """
    mid = int(mid)
    items: list = []
    for attempt in range(3):
        try:
            data = _session.get_json(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                {
                    "host_mid": mid,
                    "offset": "",  # 显式传空串，否则同账号二次请求容易返回空
                    "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
                    "timezone_offset": -480,
                },
                signed=True,
            )
        except BilibiliError:
            if attempt == 2:
                raise
            time.sleep(1.0)
            continue
        items = ((data.get("data") or {}).get("items")) or []
        if items:
            break
        time.sleep(0.8 * (attempt + 1))

    # 优先用动态里自带的作者信息，拿不到再单独查一次
    name, face = None, None
    for item in items:
        author = ((item.get("modules") or {}).get("module_author")) or {}
        if author.get("name"):
            name = author["name"]
            face = _normalize_url(author.get("face"))
            break
    if not name:
        info = get_user_info(mid)
        name, face = info["name"], info.get("face")

    entries = []
    for item in items[:limit]:
        try:
            entry = _item_to_entry(item, mid)
            if entry:
                entries.append(entry)
        except Exception as exc:  # noqa: BLE001
            log.warning("解析 B 站动态失败：%s", exc)

    if not items:
        log.info("B 站用户 %s 本次没有取到动态（可能确实没有发布）", mid)

    return {
        "title": name,
        "site_url": f"https://space.bilibili.com/{mid}",
        "icon": face,
        "description": f"{name} 的 B 站动态",
        "entries": entries,
    }


def fetch_popular(limit: int = 20) -> dict:
    """
    B 站热门视频（官方接口，无需签名、无需 Cookie）。
    对用户就是"B站热门"这一个源，跟其他订阅没有区别。
    """
    data = _session.get_json(
        "https://api.bilibili.com/x/web-interface/popular", {"ps": min(limit, 50), "pn": 1}
    )
    items = ((data.get("data") or {}).get("list")) or []

    entries = []
    for item in items[:limit]:
        bvid = item.get("bvid")
        if not bvid:
            continue
        cover = _normalize_url(item.get("pic"))
        desc = (item.get("desc") or "").strip()
        owner = (item.get("owner") or {}).get("name")
        stat = item.get("stat") or {}
        parts = []
        if cover:
            parts.append(f'<p><img src="{html.escape(cover)}" alt="" loading="lazy" '
                         f'referrerpolicy="no-referrer"></p>')
        if desc:
            parts.append(_text_to_html(desc))
        meta = []
        if owner:
            meta.append(f"UP：{owner}")
        if item.get("duration"):
            meta.append(f"时长 {item['duration'] // 60}:{item['duration'] % 60:02d}")
        if stat.get("view"):
            meta.append(f"播放 {stat['view']}")
        if stat.get("like"):
            meta.append(f"点赞 {stat['like']}")
        if meta:
            parts.append(f'<p class="stat-line">{html.escape(" · ".join(meta))}</p>')

        entries.append(
            {
                "guid": f"bilibili:popular:{bvid}",
                "title": f"[热门] {(item.get('title') or '').strip()}",
                "summary": (f"{owner} · {desc}" if owner else desc)[:200],
                "content": "".join(parts) or None,
                "link": f"https://www.bilibili.com/video/{bvid}",
                "author": owner,
                "published": item.get("pubdate"),
            }
        )

    return {
        "title": "B站热门",
        "site_url": "https://www.bilibili.com/v/popular/all",
        "icon": None,
        "description": "B 站当前热门视频",
        "entries": entries,
    }


# --------------------------------------------------------------------------- #
# 内部解析
# --------------------------------------------------------------------------- #
def _normalize_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    url = str(url).strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def _first_line(text: str, max_len: int = 60) -> str:
    text = (text or "").strip().replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "…"


def _stat_text(item: dict) -> str:
    stat = ((item.get("modules") or {}).get("module_stat")) or {}
    parts = []
    for key, label in (("like", "赞"), ("comment", "评论"), ("forward", "转发")):
        node = stat.get(key) or {}
        if isinstance(node, dict) and node.get("count"):
            parts.append(f"{label} {node['count']}")
    return " · ".join(parts)


def _text_to_html(text: str) -> str:
    """纯文本 → 安全段落 HTML（先转义，再按换行分段）。"""
    text = (text or "").strip()
    if not text:
        return ""
    paragraphs = [p.strip() for p in re.split(r"\n{1,}", text) if p.strip()]
    return "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)


def _img_tag(url: str) -> str:
    return (f'<p><img src="{html.escape(url)}" alt="" loading="lazy" '
            f'referrerpolicy="no-referrer"></p>')


def _collect_images(major: dict) -> list[str]:
    """
    从一条动态的 major 里尽量把图片挖出来。

    关键点（实测）：新接口把"图文动态"和"纯文字动态"统一成了 MAJOR_TYPE_OPUS，
    图片放在 major.opus.pics[] 里（字段 url/width/height/live_url）；
    旧的 major.draw.items[].src 已经不用了，但老动态还会出现，所以两个都认。
    视频在 archive.cover，专栏在 article.covers，直播在 live_rcmd 里。
    """
    images: list[str] = []
    seen: set[str] = set()

    def add(url):
        norm = _normalize_url(url)
        if norm and norm not in seen:
            seen.add(norm)
            images.append(norm)

    opus = major.get("opus") or {}
    for pic in opus.get("pics") or []:
        if isinstance(pic, dict) and pic.get("url"):
            add(pic["url"])

    draw = major.get("draw") or {}
    for item in draw.get("items") or []:
        if isinstance(item, dict) and item.get("src"):
            add(item["src"])

    archive = major.get("archive") or {}
    if archive.get("cover"):
        add(archive["cover"])

    article = major.get("article") or {}
    for cover in article.get("covers") or []:
        add(cover)

    # 直播封面藏在 live_rcmd.content 的 JSON 字符串里
    live = _parse_live(major.get("live_rcmd"))
    if live and live.get("cover"):
        add(live["cover"])

    common = major.get("common") or {}
    if isinstance(common, dict):
        if common.get("cover"):
            add(common["cover"])
        for cover in common.get("covers") or []:
            add(cover)

    return images


def _parse_live(live_rcmd) -> Optional[dict]:
    """直播动态：真正的标题/封面在一段 JSON 字符串里。"""
    if not isinstance(live_rcmd, dict):
        return None
    content = live_rcmd.get("content")
    if not isinstance(content, str):
        return None
    try:
        info = (json.loads(content) or {}).get("live_play_info") or {}
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(info, dict):
        return None
    status = info.get("live_status")
    return {
        "title": (info.get("title") or "").strip(),
        "cover": info.get("cover"),
        "area": info.get("area_name") or "",
        "room_id": info.get("room_id"),
        "live": status == 1,
        "watched": ((info.get("watched_show") or {}).get("text_large") or ""),
    }


def _entry_from_major(major: dict, dyn_id: str, desc: str, fallback_link: str):
    """按 major 的类型整理出 (标题, 链接, 文字 HTML, 图片列表)。"""
    title, link = "", fallback_link
    text_html = ""

    opus = major.get("opus") or {}
    draw = major.get("draw") or {}
    archive = major.get("archive") or {}
    article = major.get("article") or {}
    live = _parse_live(major.get("live_rcmd"))

    if opus:
        title = (opus.get("title") or "").strip()
        summary_text = ((opus.get("summary") or {}).get("text") or "").strip()
        link = (_normalize_url(opus.get("jump_url"))
                or f"https://www.bilibili.com/opus/{dyn_id}")
        # 有的动态标题在 opus.title，有的只有正文
        text_html = _text_to_html(summary_text or desc)
        if not title:
            title = _first_line(summary_text or desc)
    elif archive:
        title = (archive.get("title") or "").strip()
        link = _normalize_url(archive.get("jump_url")) or fallback_link
        adesc = (archive.get("desc") or "").strip()
        text_html = _text_to_html(adesc)
        if not title:
            title = _first_line(adesc) or "B站视频"
    elif article:
        title = (article.get("title") or "").strip() or "B站专栏"
        link = f"https://www.bilibili.com/read/cv{article.get('id')}"
        text_html = _text_to_html((article.get("desc") or "").strip())
    elif draw:
        title = (draw.get("title") or "").strip()
        text_html = _text_to_html(desc)
        if not title:
            title = _first_line(desc)
    elif live:
        title = live["title"] or "直播"
        if live.get("room_id"):
            link = f"https://live.bilibili.com/{live['room_id']}"
        bits = []
        if live.get("live"):
            bits.append("正在直播")
        if live.get("area"):
            bits.append(live["area"])
        if live.get("watched"):
            bits.append(live["watched"])
        text_html = f"<p>{html.escape(' · '.join(bits))}</p>" if bits else ""
    else:
        # 纯文字 / 其他
        text_html = _text_to_html(desc)
        if not title:
            title = _first_line(desc)

    return title, link, text_html, _collect_images(major)


def _item_to_entry(item: dict, mid: int) -> Optional[dict]:
    """把一条 B 站动态映射成统一文章条目。"""
    dyn_id = str(item.get("id_str") or "")
    if not dyn_id:
        return None

    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    dynamic = modules.get("module_dynamic") or {}
    major = dynamic.get("major") or {}
    desc = ((dynamic.get("desc") or {}).get("text") or "").strip()
    topic = (dynamic.get("topic") or {}).get("name")
    pub_ts = author.get("pub_ts")
    # 官方接口把时间戳放在字符串里，这里统一成 int（db 层还会再兜一次底）
    try:
        pub_ts = int(str(pub_ts)) if pub_ts is not None else None
    except (TypeError, ValueError):
        pub_ts = None
    dtype = item.get("type") or "DYNAMIC_TYPE_WORD"

    # 转发动态：正文取转发语 + 原动态
    orig = item.get("orig") if dtype == "DYNAMIC_TYPE_FORWARD" else None

    fallback_link = f"https://t.bilibili.com/{dyn_id}"
    title, link, text_html, images = _entry_from_major(major, dyn_id, desc, fallback_link)

    body_parts: list[str] = []
    if text_html:
        body_parts.append(text_html)
    for url in images[:9]:
        body_parts.append(_img_tag(url))

    # 转发的原动态（含原动态的图）
    if orig:
        orig_modules = orig.get("modules") or {}
        orig_dyn = orig_modules.get("module_dynamic") or {}
        orig_author = (orig_modules.get("module_author") or {}).get("name")
        orig_major = orig_dyn.get("major") or {}
        orig_desc = ((orig_dyn.get("desc") or {}).get("text") or "").strip()
        o_title, o_link, o_text, o_images = _entry_from_major(
            orig_major, str(orig.get("id_str") or dyn_id), orig_desc, fallback_link
        )
        block = [f"<blockquote><p>转发自 @{html.escape(str(orig_author or '原作者'))}</p>"]
        if o_text:
            block.append(o_text)
        elif orig_desc:
            block.append(_text_to_html(orig_desc[:800]))
        for url in o_images[:6]:
            block.append(_img_tag(url))
        if o_link:
            block.append(f'<p><a href="{html.escape(o_link)}">{html.escape(o_link)}</a></p>')
        block.append("</blockquote>")
        body_parts.append("".join(block))
        if not title:
            title = o_title or _first_line(orig_desc or desc) or "转发动态"
        if link == fallback_link and o_link:
            link = o_link
    elif desc and not text_html:
        body_parts.insert(0, _text_to_html(desc))

    if not title:
        title = _first_line(desc) or f"{_TYPE_LABEL.get(dtype, '动态')}"

    # 标题前缀标注类型，列表里更容易扫读
    label = _TYPE_LABEL.get(dtype)
    if label and label not in title:
        title = f"[{label}] {title}"

    content = "".join(p for p in body_parts if p)
    stats = _stat_text(item)
    if stats:
        content += f'<p class="stat-line">{html.escape(stats)}</p>'

    summary = _first_line(desc or re.sub(r"<[^>]+>", "", content), 200)
    if topic:
        summary = f"#{topic} {summary}".strip()

    return {
        "guid": f"bilibili:dynamic:{dyn_id}",
        "title": title[:200],
        "summary": summary,
        "content": content or None,
        "link": link,
        "author": author.get("name"),
        "published": pub_ts,
    }
