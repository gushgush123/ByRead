"""
zhihu.py —— 知乎原生支持

分两部分：
  1) 知乎日报：完全公开的官方接口，无需登录（一直可用）
  2) 某人的产出（回答 / 文章 / 想法）：需要你提供一次浏览器登录信息（Cookie）

关于"某人的动态"这个接口的重要说明：
    知乎的 /api/v4/members/{token}/activities（动态时间线）**现在对任何账号都返回空数组**
    —— HTTP 200、data 为 []，即使登录信息完全有效。实测已废（这是知乎的接口变更）。
    所以这里改用三个仍在正常工作的接口合并：
        /members/{token}/answers   回答（正文需再取一次，可拿到）
        /members/{token}/articles  文章（excerpt 可用；正文接口 403，由阅读页按需提取）
        /members/{token}/pins      想法（自带有 content）
    好处是比原来的"动态"更干净：投票、关注之类的内容不会再混进来。

关于 Cookie：
    没有登录态时知乎不会报错，而是静默返回空，看起来像"这个人没发过东西"。
    Cookie 只存在本地数据库，只发给 zhihu.com。
"""

from __future__ import annotations

import html as html_mod
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import requests

log = logging.getLogger("byread.zhihu")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 10
DAILY_API = "https://news-at.zhihu.com/api/4"
API = "https://www.zhihu.com/api/v4"
MAX_FULLTEXT_PER_REFRESH = 8   # 每次刷新最多补几篇正文，避免请求过多

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "application/json"})


class ZhihuAuthError(RuntimeError):
    """需要登录信息（对用户只暴露人话）。"""


def _flatten_text(value, depth: int = 0) -> str:
    """
    把结构化内容（知乎的想法是 list[dict]，每项形如 {"content": "<a ...>文本</a>"}）
    里的文本挖出来。注意优先取 content/text 这类正文字段，避免把 dict 直接 str() 出来
    —— 那会把 {'content': '...'} 这种原文泄漏到标题里（实测踩过这个坑）。
    """
    if depth > 6 or value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("content", "text", "title", "excerpt_title", "excerpt", "summary", "name"):
            if key in value:
                text = _flatten_text(value[key], depth + 1)
                if text.strip():
                    return text
        return " ".join(_flatten_text(v, depth + 1) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten_text(v, depth + 1) for v in value)
    return ""


def _strip(raw, limit: int) -> str:
    """去掉 HTML 标签与实体，用于标题/简介。搜索结果里的名字带 <em> 高亮，必须剥掉。"""
    if raw is None or raw == "":
        return ""
    text = raw if isinstance(raw, str) else _flatten_text(raw)
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _structured_html(raw) -> str:
    """
    把结构化内容转成 HTML（供后续白名单清洗）。
    字符串原样返回；列表则逐个节点提取：图片节点出 <img>，文本节点包成 <p>。
    节点里的文本本身就是 HTML 片段（含 <a> 等），所以不转义，交给清洗环节处理。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, (list, tuple)):
        return str(raw)

    parts: list[str] = []
    for node in raw:
        if isinstance(node, str):
            parts.append(f"<p>{node}</p>")
            continue
        if not isinstance(node, dict):
            continue
        url = node.get("url") or node.get("original_url") or node.get("src")
        ntype = str(node.get("type") or "")
        is_image = bool(url) and ("image" in ntype or "img" in ntype
                                 or node.get("original_url") is not None)
        if is_image:
            parts.append(f'<p><img src="{html_mod.escape(str(url))}" alt="" '
                         f'loading="lazy" referrerpolicy="no-referrer"></p>')
            continue
        text = node.get("content")
        if isinstance(text, str) and text.strip():
            parts.append(f"<p>{text}</p>")
    return "".join(parts)


def _headers(cookie: Optional[str]) -> dict:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.zhihu.com/",
        "x-requested-with": "fetch",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _get(url: str, cookie: Optional[str], params: Optional[dict] = None) -> requests.Response:
    return _session.get(url, headers=_headers(cookie), params=params or {}, timeout=TIMEOUT)


# --------------------------------------------------------------------------- #
# 登录信息校验 / 用户信息
# --------------------------------------------------------------------------- #
def test_cookie(cookie: str) -> dict:
    """用 /api/v4/me 看"我是谁"，能拿到昵称就说明登录信息有效。"""
    try:
        resp = _get(f"{API}/me", cookie)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"连不上知乎：{exc}"}

    if resp.status_code in (401, 403):
        return {"ok": False, "message": "登录信息无效或已过期（重新登录后再复制一次）"}
    if resp.status_code >= 400:
        return {"ok": False, "message": f"知乎返回 HTTP {resp.status_code}"}
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "返回的不是预期内容，可能被风控拦截了"}

    name = _strip(data.get("name"), 60)
    if not name:
        return {"ok": False, "message": "没有取到登录账号信息，登录信息可能不完整"}
    return {"ok": True, "message": f"已登录：{name}"}


def get_user(token: str, cookie: str) -> dict:
    """取知乎用户公开信息。"""
    try:
        resp = _get(
            f"{API}/members/{token}",
            cookie,
            {"include": "name,headline,avatar_url,url_token,follower_count"},
        )
        data = resp.json()
        return {
            "name": _strip(data.get("name"), 60) or token,
            "headline": _strip(data.get("headline"), 80),
            "avatar": data.get("avatar_url"),
            "follower_count": data.get("follower_count"),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("读取知乎用户 %s 失败：%s", token, exc)
        return {"name": token, "headline": "", "avatar": None, "follower_count": None}


# --------------------------------------------------------------------------- #
# 按关键词找人（需要登录信息，尽力而为）
# --------------------------------------------------------------------------- #
def search_people(keyword: str, cookie: str, limit: int = 5) -> list[dict]:
    """
    知乎的搜索接口对签名很敏感，能不能用取决于账号与风控状态，
    所以这里"尽力而为"：失败就返回空，上层会退化成"请粘贴主页链接"。
    """
    try:
        resp = _get(
            f"{API}/search_v3",
            cookie,
            {
                "t": "general",
                "q": keyword,
                "correction": 1,
                "offset": 0,
                "limit": 20,
                "show_all_topics": 0,
                "search_source": "Normal",
            },
        )
        if resp.status_code >= 400:
            log.info("知乎搜索返回 HTTP %s", resp.status_code)
            return []
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.info("知乎搜索失败：%s", exc)
        return []

    out = []
    for item in data.get("data") or []:
        obj = item.get("object") if isinstance(item, dict) else None
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "people":
            continue
        token = obj.get("url_token") or obj.get("id")
        # 名字和简介带 <em> 高亮标签（例如 "<em>文元</em>"），必须剥掉，
        # 否则会被当成订阅源标题存下来（实测踩过这个坑）
        name = _strip(obj.get("name"), 60)
        if not token or not name:
            continue
        out.append(
            {
                "token": token,
                "name": name,
                "headline": _strip(obj.get("headline"), 60),
                "avatar": obj.get("avatar_url"),
            }
        )
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# 某人的产出：回答 / 文章 / 想法
# --------------------------------------------------------------------------- #
def _answer_to_entry(item: dict, user_name: str) -> Optional[dict]:
    aid = item.get("id")
    if not aid:
        return None
    question = item.get("question") or {}
    qtitle = _strip(question.get("title"), 120)
    qid = question.get("id")
    link = (f"https://www.zhihu.com/question/{qid}/answer/{aid}"
            if qid else f"https://www.zhihu.com/answer/{aid}")
    return {
        "guid": f"zhihu:answer:{aid}",
        "title": (f"[回答] {qtitle}" if qtitle else "[回答]")[:200],
        "summary": "",          # 正文取回来之后再补
        "content": None,        # 回答列表接口不给正文，需要单独取
        "link": link,
        "author": user_name,
        "published": item.get("created_time") or item.get("updated_time"),
        "_need_answer_content": aid,
    }


def _article_to_entry(item: dict, user_name: str) -> Optional[dict]:
    aid = item.get("id")
    if not aid:
        return None
    url = str(item.get("url") or "")
    if url.startswith("http"):
        link = url.replace("http://", "https://", 1)
    else:
        link = f"https://zhuanlan.zhihu.com/p/{aid}"
    return {
        "guid": f"zhihu:article:{aid}",
        "title": f"[文章] {_strip(item.get('title'), 120)}"[:200],
        "summary": _strip(item.get("excerpt"), 200),
        # 专栏正文接口返回 403，交给阅读页按需提取（带登录信息可成功，实测 495 字符）
        "content": None,
        "link": link,
        "author": user_name,
        "published": item.get("created") or item.get("updated"),
    }


def _pin_to_entry(item: dict, user_name: str) -> Optional[dict]:
    pid = item.get("id")
    if not pid:
        return None
    # 想法的 content 是结构化列表（图片节点 + 文本节点），不能直接当字符串用
    body_html = _structured_html(item.get("content"))
    url = str(item.get("url") or "")
    if url.startswith("/"):
        link = "https://www.zhihu.com" + url
    elif url.startswith("http"):
        link = url.replace("http://", "https://", 1)
    else:
        link = f"https://www.zhihu.com/pin/{pid}"
    title_text = _strip(item.get("excerpt_title"), 80) or _strip(body_html, 60)
    return {
        "guid": f"zhihu:pin:{pid}",
        "title": (f"[想法] {title_text}" if title_text else "[想法]")[:200],
        "summary": _strip(body_html, 200),
        "content": body_html or None,
        "link": link,
        "author": user_name,
        "published": item.get("created") or item.get("updated"),
    }


# 三个仍在正常工作的接口（activities 已失效，见文件头说明）
CONTENT_SOURCES: list[tuple[str, str, dict, Callable]] = [
    ("answers", "/answers", {"limit": 20, "offset": 0, "sort_by": "created"}, _answer_to_entry),
    ("articles", "/articles", {"limit": 20, "offset": 0}, _article_to_entry),
    ("pins", "/pins", {"limit": 20, "offset": 0}, _pin_to_entry),
]


def _fetch_answer_content(answer_id, cookie: str) -> Optional[str]:
    """单条回答的正文：/api/v4/answers/{id}?include=content（实测可用）。"""
    try:
        resp = _get(f"{API}/answers/{answer_id}", cookie, {"include": "content"})
        if resp.status_code >= 400:
            return None
        content = resp.json().get("content")
        if not content:
            return None
        from feed_parser import sanitize_html

        cleaned = sanitize_html(content, base_url="https://www.zhihu.com/")
        if not cleaned or len(_strip(cleaned, 5000)) < 40:
            return None
        return cleaned
    except Exception as exc:  # noqa: BLE001
        log.info("补知乎回答正文失败 %s：%s", answer_id, exc)
        return None


def fetch_user_content(
    token: str,
    limit: int = 20,
    cookie: Optional[str] = None,
    content_state: Optional[dict] = None,
) -> dict:
    """
    抓取某个知乎用户的回答 / 文章 / 想法，合并按时间倒序。
    content_state：{guid: 是否已有正文}，用来跳过重复请求并逐次补齐缺失的正文。
    """
    if not cookie:
        raise ZhihuAuthError("知乎需要先配置登录信息（设置页 → 登录信息）")

    user = get_user(token, cookie)
    entries: list[dict] = []
    errors: list[str] = []

    for name, path, params, mapper in CONTENT_SOURCES:
        try:
            resp = _get(f"{API}/members/{token}{path}", cookie, params)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            continue

        if resp.status_code in (401, 403):
            raise ZhihuAuthError("知乎登录信息已失效，请重新复制一次")
        if resp.status_code >= 400:
            errors.append(f"{name}: HTTP {resp.status_code}")
            continue
        try:
            items = (resp.json() or {}).get("data") or []
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: 返回内容无法解析（{exc}）")
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                entry = mapper(item, user["name"])
            except Exception as exc:  # noqa: BLE001
                log.info("解析知乎条目失败：%s", exc)
                continue
            if entry:
                entries.append(entry)
        time.sleep(0.2)

    if not entries:
        detail = f"（{'；'.join(errors)}）" if errors else ""
        raise RuntimeError(
            f"没有取到任何内容{detail}。该账号可能没有公开的回答/文章/想法"
        )

    # 没有时间戳的排到最后
    entries.sort(key=lambda e: int(e.get("published") or 0), reverse=True)
    entries = entries[:limit]

    state = content_state or {}
    budget = MAX_FULLTEXT_PER_REFRESH
    from feed_parser import sanitize_html

    for entry in entries:
        answer_id = entry.pop("_need_answer_content", None)

        if entry.get("content"):
            entry["content"] = sanitize_html(entry["content"], base_url="https://www.zhihu.com/")
            if not entry.get("summary"):
                entry["summary"] = _strip(entry["content"], 200)
            continue

        if not answer_id or budget <= 0:
            continue
        # 库里已经有正文了就不重复取；已入库但没正文的继续补（逐次补齐）
        if state.get(entry["guid"]):
            continue

        content = _fetch_answer_content(answer_id, cookie)
        if content:
            entry["content"] = content
            if not entry.get("summary"):
                entry["summary"] = _strip(content, 200)
            budget -= 1
        time.sleep(0.2)

    return {
        "title": user["name"],
        "site_url": f"https://www.zhihu.com/people/{token}",
        "icon": user.get("avatar"),
        "description": user.get("headline") or f"{user['name']} 的知乎内容",
        "entries": entries,
    }


# --------------------------------------------------------------------------- #
# 知乎日报（公开接口，无需登录）
# --------------------------------------------------------------------------- #
def _get_json(path: str) -> dict:
    resp = _session.get(DAILY_API + path, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _clean_body(raw_html: str) -> str:
    """知乎日报的正文里图片放在 data-original / data-actualsrc 上，先搬过来再清洗。"""
    if not raw_html:
        return ""
    text = re.sub(
        r'data-(?:original|actualsrc)="([^"]+)"',
        lambda m: f'src="{m.group(1)}"',
        raw_html,
    )
    text = re.sub(r'<div class="img-place-holder">.*?</div>', "", text, flags=re.S)
    from feed_parser import sanitize_html  # 局部导入，避免循环依赖

    return sanitize_html(text, base_url="https://daily.zhihu.com/")


def fetch_story(story_id: int) -> dict:
    try:
        data = _get_json(f"/news/{story_id}")
        return {
            "content": _clean_body(data.get("body") or ""),
            "image": data.get("image"),
            "title": data.get("title"),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("读取知乎日报正文 %s 失败：%s", story_id, exc)
        return {"content": "", "image": None, "title": None}


def fetch_daily(limit: int = 10, days: int = 2,
                content_state: Optional[dict] = None) -> dict:
    """
    抓取知乎日报。days 表示往前翻几天（首次订阅时能一次性拿到一批文章）。
    content_state 里已有正文的文章不再重复请求正文。
    """
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    collected: list[dict] = []
    seen: set[int] = set()

    for day_index in range(max(1, days)):
        try:
            if day_index == 0:
                data = _get_json("/news/latest")
            else:
                d = datetime.strptime(date_str, "%Y%m%d") - timedelta(days=day_index)
                data = _get_json(f"/news/before/{d.strftime('%Y%m%d')}")
        except Exception as exc:  # noqa: BLE001
            log.warning("读取知乎日报第 %s 天失败：%s", day_index, exc)
            continue

        batch_date = str(data.get("date") or date_str)
        stories = list(data.get("top_stories") or []) + list(data.get("stories") or [])
        for story in stories:
            sid = story.get("id")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            collected.append({"story": story, "date": batch_date})
        time.sleep(0.3)

    state = content_state or {}
    entries = []
    for index, item in enumerate(collected[:limit]):
        story = item["story"]
        sid = story["id"]
        # 接口返回的顺序就是"最新在前"，编码成递减时间戳以保证列表顺序一致
        try:
            base = datetime.strptime(item["date"], "%Y%m%d").replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            base = datetime.now(timezone.utc)
        published = (base - timedelta(seconds=index)).strftime("%Y-%m-%dT%H:%M:%SZ")

        guid = f"zhihu:daily:{sid}"
        content = ""
        cover = None
        if not state.get(guid):
            detail = fetch_story(sid)
            content = detail.get("content") or ""
            cover = detail.get("image")
        images = story.get("images") or []
        cover = cover or (images[0] if images else None)
        if cover and "<img" not in content:
            content = (f'<p><img src="{html_mod.escape(cover)}" alt="" loading="lazy" '
                       f'referrerpolicy="no-referrer"></p>') + content

        hint = _strip(story.get("hint"), 60)
        entries.append(
            {
                "guid": guid,
                "title": _strip(story.get("title"), 120),
                "summary": hint,
                "content": content or None,
                "link": story.get("url") or f"https://daily.zhihu.com/story/{sid}",
                "author": hint.split("·")[0].strip() if "·" in hint else None,
                "published": published,
            }
        )

    return {
        "title": "知乎日报",
        "site_url": "https://daily.zhihu.com",
        "icon": None,
        "description": "每天三次，每次七分钟",
        "entries": entries,
    }
