"""
weibo.py —— 微博原生支持（需要你提供一次浏览器登录信息）

微博在没有 Cookie 时会直接返回 432 / 403（反爬），拿到登录信息后，
m.weibo.cn 的接口非常稳定，比走 RSSHub 更快更全。

Cookie 只存本地数据库，只发给 weibo.cn / weibo.com。
"""

from __future__ import annotations

import html as html_mod
import logging
import re
import time
from datetime import datetime
from typing import Optional
from urllib.parse import quote

import requests

import db
import net  # noqa: F401  统一网络初始化（让 Python 用系统证书库）
from errors import TemporaryFeedError

log = logging.getLogger("byread.weibo")

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
TIMEOUT = 10
API = "https://m.weibo.cn/api"
MAX_LONGTEXT_PER_REFRESH = 5   # 长微博补全文的次数上限

_session = requests.Session()

_last_call = 0.0


class WeiboAuthError(TemporaryFeedError):
    """登录信息问题 / 临时限流（对用户只暴露人话）。属于临时失败，不该把源永久暂停。"""


def _throttle() -> None:
    """
    两次请求之间至少间隔 0.8 秒。
    微博对脚本化的高频访问很敏感，实测连续请求会把会话直接登出
    （之后所有接口都返回 {"ok":-100,...signin}），所以这里主动限速。
    """
    global _last_call
    delta = time.time() - _last_call
    if delta < 0.8:
        time.sleep(0.8 - delta)
    _last_call = time.time()


def _check_auth(payload: dict) -> dict:
    """
    微博的登录失效**不会**返回 401/403，而是 HTTP 200 + {"ok":-100,"url":".../sso/signin"}，
    数据字段直接为空。不识别它就会报成"这个人没发微博"，把原因指向完全错误的方向。
    """
    if not isinstance(payload, dict):
        return {}
    if payload.get("ok") == -100:
        raise WeiboAuthError(
            "微博暂时限制了这次访问（多为请求频率触发），一般几分钟到十几分钟会自己恢复；"
            "如果一直不行，再在浏览器重新登录后重新复制一次"
        )
    ok = payload.get("ok")
    if ok not in (1, None):
        raise RuntimeError(f"微博返回错误：{payload.get('msg') or ok}")
    return payload


def _headers(cookie: Optional[str], uid: Optional[str] = None) -> dict:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "MWeibo-Pwa": "1",
        "Referer": f"https://m.weibo.cn/u/{uid}" if uid else "https://m.weibo.cn/",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _get(path: str, cookie: Optional[str], params: Optional[dict] = None,
         uid: Optional[str] = None) -> requests.Response:
    _throttle()
    return _session.get(
        API + path, headers=_headers(cookie, uid), params=params or {}, timeout=TIMEOUT
    )


# --------------------------------------------------------------------------- #
# 登录信息校验
# --------------------------------------------------------------------------- #
def test_cookie(cookie: str) -> dict:
    """m.weibo.cn/api/config 会返回当前登录状态。"""
    try:
        resp = _get("/config", cookie)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"连不上微博：{exc}"}

    if resp.status_code == 432:
        return {"ok": False, "message": "被微博反爬拦住了（432），登录信息可能不完整或已过期"}
    if resp.status_code >= 400:
        return {"ok": False, "message": f"微博返回 HTTP {resp.status_code}"}
    try:
        data = (resp.json() or {}).get("data") or {}
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "返回的不是预期内容，可能被风控拦截了"}

    if data.get("login"):
        uid = data.get("uid") or ""
        return {"ok": True, "message": f"已登录（uid: {uid}）" if uid else "已登录"}
    return {"ok": False, "message": "登录状态无效，请重新登录微博后再复制一次"}


# --------------------------------------------------------------------------- #
# 用户信息
# --------------------------------------------------------------------------- #
def _avatar_url(user: dict) -> Optional[str]:
    """
    头像地址。优先用 avatar_hd —— profile_image_url 带着会过期的签名参数
    （?KID=...&Expires=...&ssig=...），存进库里过两天就失效了。
    """
    for key in ("avatar_hd", "profile_image_url", "avatar_large"):
        url = user.get(key)
        if url:
            return str(url).split("?")[0]
    return None


def get_user(uid: str, cookie: str) -> dict:
    """取微博用户的昵称/头像/简介。"""
    try:
        resp = _get("/container/getIndex", cookie, {"type": "uid", "value": uid}, uid=uid)
        data = _check_auth(resp.json()).get("data") or {}
        info = data.get("userInfo") or {}
        if not info:
            raise RuntimeError("没有取到用户资料")
        return {
            "name": _strip(info.get("screen_name"), 60) or f"微博用户 {uid}",
            "avatar": _avatar_url(info),
            "description": _strip(info.get("description"), 80)
                           or _strip(info.get("verified_reason"), 80),
            "followers": info.get("followers_count_str") or info.get("followers_count"),
        }
    except WeiboAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("读取微博用户 %s 失败：%s", uid, exc)
        return {"name": f"微博用户 {uid}", "avatar": None, "description": "", "followers": None}


# --------------------------------------------------------------------------- #
# 按关键词找人（尽力而为）
# --------------------------------------------------------------------------- #
def search_users(keyword: str, cookie: str, limit: int = 5) -> list[dict]:
    """
    m.weibo.cn 的综合搜索接口，能不能用取决于账号的搜索权限与风控，
    失败就返回空，上层会退化成"请粘贴主页链接"。
    """
    try:
        containerid = f"100103type=3&q={keyword}"
        resp = _get(
            "/container/getIndex",
            cookie,
            {"containerid": containerid, "page_type": "searchall"},
        )
        if resp.status_code >= 400:
            log.info("微博搜索返回 HTTP %s", resp.status_code)
            return []
        cards = (_check_auth(resp.json()).get("data") or {}).get("cards") or []
    except WeiboAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.info("微博搜索失败：%s", exc)
        return []

    out: list[dict] = []
    seen: set[str] = set()

    def collect(user: dict) -> None:
        uid = user.get("id") or user.get("idstr")
        # 微博搜索结果同样带 <em> 高亮，必须剥掉再当标题用
        name = _strip(user.get("screen_name"), 60)
        if not uid or not name or str(uid) in seen:
            return
        seen.add(str(uid))
        # 注意：followers_count 在搜索结果里是**格式化字符串**（"355.6万"），不是整数，
        # 直接拿来用即可；description 可能为空，那时用认证说明顶上（信息量更大）
        out.append(
            {
                "uid": str(uid),
                "name": name,
                "avatar": _avatar_url(user),
                "description": _strip(user.get("description"), 60)
                               or _strip(user.get("verified_reason"), 60),
                "followers": user.get("followers_count_str") or user.get("followers_count"),
                "verified": bool(user.get("verified")),
            }
        )

    for card in cards:
        if not isinstance(card, dict):
            continue
        if isinstance(card.get("user"), dict):
            collect(card["user"])
        for group in card.get("card_group") or []:
            if isinstance(group, dict) and isinstance(group.get("user"), dict):
                collect(group["user"])
        if len(out) >= limit:
            break
    return out[:limit]


# --------------------------------------------------------------------------- #
# 某人的微博
# --------------------------------------------------------------------------- #
def _parse_created(text: Optional[str]):
    """微博的时间格式是 'Mon Sep 14 22:48:28 +0800 2026'。"""
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%a %b %d %H:%M:%S %z %Y")
    except Exception:  # noqa: BLE001
        return None


def _strip(raw_html: Optional[str], limit: int = 300) -> str:
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(raw_html))
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _fetch_longtext(status_id: str, cookie: str) -> Optional[str]:
    """长微博的正文要单独取一次。"""
    try:
        resp = _get("/statuses/show", cookie, {"id": status_id})
        data = (resp.json() or {}).get("data") or {}
        text = data.get("longTextContent") or data.get("text")
        if not text:
            return None
        from feed_parser import sanitize_html

        return sanitize_html(text, base_url="https://m.weibo.cn/")
    except Exception as exc:  # noqa: BLE001
        log.info("补微博长文失败 %s：%s", status_id, exc)
        return None


def _build_content(mblog: dict, cookie: str, allow_longtext: bool) -> Optional[str]:
    from feed_parser import sanitize_html

    text_html = mblog.get("longTextContent") or mblog.get("text") or ""
    if mblog.get("isLongText") and allow_longtext and not mblog.get("longTextContent"):
        full = _fetch_longtext(str(mblog.get("id") or mblog.get("mid") or ""), cookie)
        if full:
            text_html = full
            time.sleep(0.2)

    parts = []
    if text_html:
        cleaned = sanitize_html(text_html, base_url="https://m.weibo.cn/")
        if cleaned:
            parts.append(cleaned)

    for pic in mblog.get("pics") or []:
        url = pic.get("url") if isinstance(pic, dict) else None
        if url:
            parts.append(f'<p><img src="{html_mod.escape(url)}" alt="" loading="lazy" '
                         f'referrerpolicy="no-referrer"></p>')

    # 转发内容
    retweet = mblog.get("retweeted_status")
    if isinstance(retweet, dict):
        original_author = (retweet.get("user") or {}).get("screen_name") or "原作者"
        block = [f"<blockquote><p>转发自 @{html_mod.escape(str(original_author))}</p>"]
        rt_text = sanitize_html(retweet.get("text") or "", base_url="https://m.weibo.cn/")
        if rt_text:
            block.append(rt_text)
        for pic in retweet.get("pics") or []:
            url = pic.get("url") if isinstance(pic, dict) else None
            if url:
                block.append(f'<p><img src="{html_mod.escape(url)}" alt="" loading="lazy" '
                             f'referrerpolicy="no-referrer"></p>')
        block.append("</blockquote>")
        parts.append("".join(block))

    stats = []
    if mblog.get("reposts_count"):
        stats.append(f"转发 {mblog['reposts_count']}")
    if mblog.get("comments_count"):
        stats.append(f"评论 {mblog['comments_count']}")
    if mblog.get("attitudes_count"):
        stats.append(f"赞 {mblog['attitudes_count']}")
    if stats:
        parts.append(f'<p class="stat-line">{html_mod.escape(" · ".join(stats))}</p>')

    return "".join(parts) or None


def fetch_user_weibo(
    uid: str,
    limit: int = 20,
    cookie: Optional[str] = None,
    content_state: Optional[dict] = None,
) -> dict:
    """抓取某个微博用户的微博。content_state：{guid: 是否已有正文}。"""
    if not cookie:
        raise WeiboAuthError("微博需要先配置登录信息（设置页 → 登录信息）")

    user = get_user(uid, cookie)
    entries: list[dict] = []
    longtext_budget = MAX_LONGTEXT_PER_REFRESH
    state = content_state or {}

    try:
        resp = _get(
            "/container/getIndex",
            cookie,
            {"type": "uid", "value": uid, "containerid": f"107603{uid}"},
            uid=uid,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"读取微博失败：{exc}") from exc

    if resp.status_code in (403, 432):
        raise WeiboAuthError("微博登录信息已失效或被反爬拦截，请重新复制一次")
    if resp.status_code >= 400:
        raise RuntimeError(f"微博返回 HTTP {resp.status_code}")

    try:
        payload = _check_auth(resp.json())
        cards = (payload.get("data") or {}).get("cards") or []
    except WeiboAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"微博返回内容无法解析：{exc}") from exc

    for card in cards:
        if not isinstance(card, dict):
            continue
        mblogs = []
        if isinstance(card.get("mblog"), dict):
            mblogs.append(card["mblog"])
        for group in card.get("card_group") or []:
            if isinstance(group, dict) and isinstance(group.get("mblog"), dict):
                mblogs.append(group["mblog"])

        for mblog in mblogs:
            status_id = str(mblog.get("id") or mblog.get("mid") or "")
            if not status_id:
                continue
            guid = f"weibo:status:{status_id}"
            # 没有正文、或正文是旧版解析逻辑取的 → 值得再取一次长文
            old = state.get(guid) or {}
            allow_long = longtext_budget > 0 and (
                not old.get("len") or old.get("v", 0) < db.CONTENT_VERSION
            )
            content = _build_content(mblog, cookie, allow_long)
            if allow_long and mblog.get("isLongText") and content:
                longtext_budget -= 1

            entries.append(
                {
                    "guid": guid,
                    "title": _strip(mblog.get("text") or "", 60) or "(无正文)",
                    "summary": _strip(mblog.get("text") or "", 200),
                    "content": content,
                    "link": f"https://m.weibo.cn/detail/{status_id}",
                    "author": user["name"],
                    "published": _parse_created(mblog.get("created_at")),
                }
            )
            if len(entries) >= limit:
                break
        if len(entries) >= limit:
            break

    if not entries:
        raise RuntimeError("没有取到微博内容。可能是登录信息失效，或该账号没有公开微博")

    return {
        "title": user["name"],
        "site_url": f"https://m.weibo.cn/u/{uid}",
        "icon": user.get("avatar"),
        "description": user.get("description") or f"{user['name']} 的微博",
        "entries": entries[:limit],
    }
