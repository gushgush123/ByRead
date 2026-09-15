"""
cookies.py —— 登录信息（Cookie）的收取、存储与校验

设计原则：
    1. Cookie 只存在本地 SQLite（instance/bai_read.db），只发给它所属的平台，不上传任何地方。
    2. 永远不回传给前端：设置接口读取时一律脱敏（只告诉你"已配置、多少字符、结尾几位"）。
    3. 永远不写日志。
    4. 用户不需要手工拼 Cookie 字符串 —— 支持直接粘贴浏览器"复制为 cURL"的整段内容，
       我们从里面把 Cookie 头抠出来（HttpOnly 的 Cookie 只能这样拿到，
       document.cookie 是拿不到 z_c0 / SUB 这类关键字段的）。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import db

log = logging.getLogger("byread.cookies")

# 平台 → 设置键
PLATFORM_KEYS = {
    "zhihu": "zhihu_cookie",
    "weibo": "weibo_cookie",
}

PLATFORM_LABELS = {
    "zhihu": "知乎",
    "weibo": "微博",
}


def get_cookie(platform: str) -> Optional[str]:
    """取某平台的 Cookie；没配置返回 None。"""
    key = PLATFORM_KEYS.get(platform)
    if not key:
        return None
    value = (db.get_setting(key) or "").strip()
    return value or None


def has_cookie(platform: str) -> bool:
    return bool(get_cookie(platform))


def set_cookie(platform: str, raw_text: str) -> tuple[bool, str]:
    """
    保存 Cookie。raw_text 可以是：
      - 浏览器「复制为 cURL」的整段命令（推荐，能拿到 HttpOnly 的 Cookie）
      - 完整的 Cookie 请求头
      - 纯 Cookie 字符串
    返回 (是否成功, 提示文案)。
    """
    key = PLATFORM_KEYS.get(platform)
    if not key:
        return False, "不支持的平台"

    raw_text = (raw_text or "").strip()
    if not raw_text:
        # 空内容 = 清除
        db.set_setting(key, "")
        return True, "已清除"

    cookie = extract_cookie(raw_text)
    if not cookie:
        return False, "没认出里面的登录信息，请用浏览器「复制为 cURL」整段粘贴，或直接粘贴 Cookie 字符串"
    if len(cookie) < 20 or "=" not in cookie:
        return False, "这段内容看起来不像登录信息，请重新复制"

    db.set_setting(key, cookie)
    log.info("已保存 %s 的登录信息（%s 字符）", PLATFORM_LABELS.get(platform, platform), len(cookie))
    return True, f"已保存（{len(cookie)} 字符）"


# --------------------------------------------------------------------------- #
# 从各种格式里抠出 Cookie
# --------------------------------------------------------------------------- #
_CURL_COOKIE_HEADER = re.compile(
    r"""(?:-H|--header)\s+(?P<q>['"])\s*cookie\s*:\s*(?P<v>.*?)(?P=q)""",
    re.IGNORECASE | re.DOTALL,
)
_CURL_COOKIE_FLAG = re.compile(
    r"""(?:-b|--cookie)\s+(?P<q>['"])(?P<v>.*?)(?P=q)""",
    re.IGNORECASE | re.DOTALL,
)
_LEADING_COOKIE_LABEL = re.compile(r"^\s*cookie\s*:\s*", re.IGNORECASE)


def extract_cookie(raw_text: str) -> Optional[str]:
    """
    尽力从用户粘贴的内容里提取 Cookie 字符串。
    识别顺序：curl 的 -H 'cookie: ...' → curl 的 -b '...' → 带 cookie: 前缀的裸串 → 裸串
    """
    if not raw_text:
        return None

    text = raw_text.strip()
    # Windows 的「复制为 cURL (cmd)」用 ^ 换行、"" 转义
    text = text.replace("^\n", " ").replace("^\r\n", " ")
    text = text.replace('\\"', '"')

    match = _CURL_COOKIE_HEADER.search(text)
    if not match:
        match = _CURL_COOKIE_FLAG.search(text)
    if match:
        return _normalize(match.group("v"))

    # 只粘贴了 Cookie 请求头，或纯 Cookie 字符串
    single_line = _LEADING_COOKIE_LABEL.sub("", text.strip())
    if "\n" not in single_line.strip() and "=" in single_line:
        return _normalize(single_line)
    return None


def _normalize(value: str) -> Optional[str]:
    """规整成单行 Cookie 字符串，丢掉明显不是 Cookie 的部分。"""
    value = (value or "").strip()
    if not value:
        return None
    value = re.sub(r"\s*\n\s*", " ", value)
    value = re.sub(r"\s{2,}", " ", value)
    # 去掉 curl 里可能连带粘进来的尾巴（例如 ' -H ' 之类）
    value = value.split("' ")[0].split('" ')[0].strip()
    if not value or "=" not in value:
        return None
    return value


def mask(cookie: Optional[str]) -> dict:
    """给前端看的脱敏信息。"""
    if not cookie:
        return {"configured": False}
    tail = cookie[-4:] if len(cookie) >= 4 else ""
    return {"configured": True, "length": len(cookie), "tail": tail}


# --------------------------------------------------------------------------- #
# 测试登录信息是否有效
# --------------------------------------------------------------------------- #
def test_cookie(platform: str) -> dict:
    """
    用平台自己的"我是谁"接口验证登录态：
      知乎 /api/v4/me   微博 m.weibo.cn/api/config
    返回 {ok, message}
    """
    cookie = get_cookie(platform)
    if not cookie:
        return {"ok": False, "message": "还没有配置登录信息"}

    if platform == "zhihu":
        import zhihu

        return zhihu.test_cookie(cookie)
    if platform == "weibo":
        import weibo

        return weibo.test_cookie(cookie)
    return {"ok": False, "message": "不支持的平台"}
