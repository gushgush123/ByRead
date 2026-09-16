"""
rsshub.py —— "搜索即订阅" 引擎（对用户不可见的实现细节）

分层解析（resolver）设计，逐级降级，任何一层挂掉都不影响其他层：

    L1  直链识别      https://example.com/feed.xml        → 直接校验并添加
    L2  主页链接   → 源  B站空间/知乎主页/微博主页/少数派  → 原生解析出参数，拼出可订阅地址
    L3  关键词     → 候选  输入"半佛仙人"                  → 搜索出多个平台候选供选择
    L4  全失败            → 自然语言引导（"可以粘贴主页链接"）

为什么不是原文档那种"统一 search_api + JSON 路径"的表格：
    实测各个平台的"搜索"根本不是一回事——B 站要 wbi 签名，微博/知乎要 Cookie 且公共实例直接 429，
    少数派这类则是固定地址根本没有搜索。硬塞进一张表，Phase 2 必然崩。
    所以这里改成：能原生做的原生做（B 站），固定地址的直接给（少数派/V2EX 等），
    做不了的诚实降级成"请粘贴主页链接"，而不是假装能搜。

RSSHub 的定位也随之变化：
    它可靠的是"路由拼接"（知道 uid 就能出 feed），不可靠的是"搜索"（公共实例普遍限流）。
    因此它只作为"已知参数 → 地址"的构造函数，并且实例地址可配置 + 自动探测。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional
from urllib.parse import urlparse

import requests

import net  # noqa: F401  统一网络初始化

import db

log = logging.getLogger("byread.resolver")

TIMEOUT = 8

# 公共实例候选（按探测顺序）。rsshub.app 是官方实例，但在部分网络环境下不可达，所以放在候选里一起探测。
INSTANCE_CANDIDATES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.liumingye.cn",
    "https://rsshub.woodland.cafe",
]

# --------------------------------------------------------------------------- #
# 预置源
# --------------------------------------------------------------------------- #
# kind="direct" → 自带标准订阅地址（或本地原生生成的 byread://），最可靠
# kind="route"  → 需要 RSSHub 实例拼路由
#
# 顺序即设置页"推荐源"的展示顺序。direct 的都是实测可用的，
# 能直连就直连、能原生就原生，尽量不让任何东西依赖公共实例。
PRESETS: list[dict] = [
    {
        "id": "sspai",
        "label": "少数派",
        "keys": ["少数派", "sspai"],
        "kind": "direct",
        "feed_url": "https://sspai.com/feed",
        "site_url": "https://sspai.com",
        "desc": "高效工作，品质生活",
    },
    {
        "id": "ruanyifeng",
        "label": "阮一峰的网络日志",
        "keys": ["阮一峰", "科技爱好者周刊", "ruanyifeng"],
        "kind": "direct",
        "feed_url": "https://www.ruanyifeng.com/blog/atom.xml",
        "site_url": "https://www.ruanyifeng.com/blog/",
        "desc": "每周分享科技与编程",
    },
    {
        "id": "zhihu_daily",
        "label": "知乎日报",
        "keys": ["知乎日报"],
        "kind": "direct",
        "feed_url": "byread://zhihu/daily",
        "site_url": "https://daily.zhihu.com",
        "desc": "每天三次，每次七分钟（原生接口，正文齐全）",
    },
    {
        "id": "bilibili_popular",
        "label": "B站热门",
        "keys": ["b站热门", "bilibili热门", "热门视频"],
        "kind": "direct",
        "feed_url": "byread://bilibili/popular",
        "site_url": "https://www.bilibili.com/v/popular/all",
        "desc": "B 站当前热门视频（原生接口）",
    },
    {
        "id": "ithome",
        "label": "IT之家",
        "keys": ["it之家", "ithome"],
        "kind": "direct",
        "feed_url": "https://www.ithome.com/rss/",
        "site_url": "https://www.ithome.com",
        "desc": "科技数码资讯",
    },
    {
        "id": "ifanr",
        "label": "爱范儿",
        "keys": ["爱范儿", "ifanr"],
        "kind": "direct",
        "feed_url": "https://www.ifanr.com/feed",
        "site_url": "https://www.ifanr.com",
        "desc": "科技与生活方式",
    },
    {
        "id": "geekpark",
        "label": "极客公园",
        "keys": ["极客公园", "geekpark"],
        "kind": "direct",
        "feed_url": "https://www.geekpark.net/rss",
        "site_url": "https://www.geekpark.net",
        "desc": "科技产品与商业观察",
    },
    {
        "id": "gcores",
        "label": "机核",
        "keys": ["机核", "gcores"],
        "kind": "direct",
        "feed_url": "byread://gcores/latest",
        "site_url": "https://www.gcores.com",
        "desc": "游戏、电台与亚文化（原生接口，RSS 已被 WAF 拦，故改走官方 API）",
    },
    {
        "id": "yystv",
        "label": "游研社",
        "keys": ["游研社", "yystv"],
        "kind": "direct",
        "feed_url": "https://www.yystv.cn/rss/feed",
        "site_url": "https://www.yystv.cn",
        "desc": "游戏文化与考据",
    },
    {
        "id": "juejin",
        "label": "掘金",
        "keys": ["掘金", "juejin"],
        "kind": "direct",
        "feed_url": "https://juejin.cn/rss",
        "site_url": "https://juejin.cn",
        "desc": "开发者技术社区",
    },
    {
        "id": "coolshell",
        "label": "酷壳",
        "keys": ["酷壳", "coolshell", "陈皓"],
        "kind": "direct",
        "feed_url": "https://coolshell.cn/feed",
        "site_url": "https://coolshell.cn",
        "desc": "技术随笔与深度长文",
    },
    {
        "id": "codingnow",
        "label": "云风的 Blog",
        "keys": ["云风", "codingnow"],
        "kind": "direct",
        "feed_url": "https://blog.codingnow.com/atom.xml",
        "site_url": "https://blog.codingnow.com",
        "desc": "游戏引擎与编程思考",
    },
    {
        "id": "leiphone",
        "label": "雷峰网",
        "keys": ["雷峰网", "雷锋网", "leiphone"],
        "kind": "direct",
        "feed_url": "https://www.leiphone.com/feed",
        "site_url": "https://www.leiphone.com",
        "desc": "AI 与前沿科技报道",
    },
    {
        "id": "tmtpost",
        "label": "钛媒体",
        "keys": ["钛媒体", "tmtpost"],
        "kind": "direct",
        "feed_url": "https://www.tmtpost.com/rss.xml",
        "site_url": "https://www.tmtpost.com",
        "desc": "商业与科技深度",
    },
    {
        "id": "solidot",
        "label": "Solidot 奇客",
        "keys": ["solidot", "奇客"],
        "kind": "direct",
        "feed_url": "https://www.solidot.org/index.rss",
        "site_url": "https://www.solidot.org",
        "desc": "科技新闻摘要",
    },
    {
        "id": "hackernews",
        "label": "Hacker News",
        "keys": ["hacker news", "hackernews", "hn", "黑客新闻"],
        "kind": "direct",
        "feed_url": "https://hnrss.org/frontpage",
        "site_url": "https://news.ycombinator.com",
        "desc": "硅谷技术圈头条",
    },
    {
        "id": "v2ex",
        "label": "V2EX 最新主题",
        "keys": ["v2ex"],
        "kind": "route",
        "route": "/v2ex/topics/latest",
        "site_url": "https://www.v2ex.com",
        "desc": "创意工作者的社区",
    },
    {
        "id": "douban_movie",
        "label": "豆瓣电影正在上映",
        "keys": ["豆瓣电影", "豆瓣"],
        "kind": "route",
        "route": "/douban/movie/playing",
        "site_url": "https://movie.douban.com",
        "desc": "当前院线影片",
    },
    {
        "id": "36kr",
        "label": "36氪快讯",
        "keys": ["36氪", "36kr"],
        "kind": "route",
        "route": "/36kr/newsflashes",
        "site_url": "https://36kr.com",
        "desc": "商业科技快讯",
    },
    {
        "id": "github_trending",
        "label": "GitHub 每日趋势",
        "keys": ["github", "github热门", "github趋势"],
        "kind": "direct",
        "feed_url": "byread://github/trending",
        "site_url": "https://github.com/trending",
        "desc": "最近一周新建、涨星最快的仓库（原生接口）",
    },
]

# 平台别名 → 内部平台标识
PLATFORM_ALIASES = {
    "b站": "bilibili", "bilibili": "bilibili", "哔哩哔哩": "bilibili", "bili": "bilibili",
    "微博": "weibo", "weibo": "weibo",
    "知乎": "zhihu", "zhihu": "zhihu",
    "公众号": "wechat", "微信公众号": "wechat", "微信": "wechat", "wechat": "wechat",
    "少数派": "sspai", "sspai": "sspai",
}

# 需要登录信息才能按名字搜索的平台 → 引导文案
NEEDS_URL_HINT = {
    "weibo": ("微博", "https://m.weibo.cn/u/博主的数字ID"),
    "zhihu": ("知乎", "https://www.zhihu.com/people/用户名"),
    "wechat": ("微信公众号", "公众号里任意一篇文章的链接"),
}


def _needs_cookie_hint(platform: str, cookie_ready: bool) -> str:
    """配置了登录信息就还能按名字搜，否则只能粘贴链接。"""
    name, example = NEEDS_URL_HINT[platform]
    if cookie_ready:
        return f"{name}没搜到这个人。可以直接粘贴他的主页链接，例如：{example}"
    return (f"{name}需要先粘贴主页链接才能订阅，例如：{example}"
            "（想直接输入名字搜，可以在设置页 → 登录信息 里配置一次）")


def _zhihu_candidates(keyword: str, limit: int = 4) -> list[dict]:
    """知乎按名字找人（需要登录信息，尽力而为）。登录失效会抛异常，由调用方转成提示。"""
    import cookies

    cookie = cookies.get_cookie("zhihu")
    if not cookie:
        return []
    try:
        import zhihu

        users = zhihu.search_people(keyword, cookie, limit=limit)
    except zhihu.ZhihuAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.info("知乎搜索失败：%s", exc)
        return []

    out = []
    for u in users:
        detail = u.get("headline") or "知乎用户"
        out.append(
            {
                "label": f"知乎 · {u['name']}",
                "detail": detail,
                "feed_url": f"byread://zhihu/people/{u['token']}",
                "title": u["name"],
                "site_url": f"https://www.zhihu.com/people/{u['token']}",
                "icon": u.get("avatar"),
                "platform": "知乎",
            }
        )
    return out


def _weibo_candidates(keyword: str, limit: int = 4) -> list[dict]:
    """微博按名字找人（需要登录信息，尽力而为）。登录失效会抛异常，由调用方转成提示。"""
    import cookies

    cookie = cookies.get_cookie("weibo")
    if not cookie:
        return []
    try:
        import weibo

        users = weibo.search_users(keyword, cookie, limit=limit)
    except weibo.WeiboAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.info("微博搜索失败：%s", exc)
        return []

    out = []
    for u in users:
        # 微博的 followers 是格式化字符串（"355.6万"），直接用
        detail = u.get("description") or "微博用户"
        followers = u.get("followers")
        if followers:
            detail = f"{followers} 粉丝 · {detail}"
        if u.get("verified"):
            detail = "✔ " + detail
        out.append(
            {
                "label": f"微博 · {u['name']}",
                "detail": detail,
                "feed_url": f"byread://weibo/user/{u['uid']}",
                "title": u["name"],
                "site_url": f"https://m.weibo.cn/u/{u['uid']}",
                "icon": u.get("avatar"),
                "platform": "微博",
            }
        )
    return out


class ResolverError(RuntimeError):
    """解析失败（对外只给人话）。"""


# --------------------------------------------------------------------------- #
# 实例管理
# --------------------------------------------------------------------------- #
def _instance_ok(base: str, timeout: int = TIMEOUT) -> bool:
    """探测实例是否可用（拿一个最轻的固定路由试）。"""
    if not base:
        return False
    try:
        r = requests.get(
            base.rstrip("/") + "/v2ex/topics/latest",
            timeout=timeout,
            headers={"User-Agent": "ByRead/0.1 (local RSS reader)"},
        )
        return r.status_code < 400
    except Exception:  # noqa: BLE001
        return False


def probe_instances(include_configured: bool = True, timeout: int = TIMEOUT) -> list[dict]:
    """逐个探测候选实例，返回可用性列表（供设置页"检测"按钮）。"""
    configured = (db.get_setting("rsshub_instance") or "").strip().rstrip("/")
    targets: list[str] = []
    if include_configured and configured:
        targets.append(configured)
    for url in INSTANCE_CANDIDATES:
        if url.rstrip("/") not in targets:
            targets.append(url.rstrip("/"))

    results = []
    for url in targets:
        started = time.time()
        ok = _instance_ok(url, timeout=timeout)
        results.append(
            {
                "url": url,
                "ok": ok,
                "ms": int((time.time() - started) * 1000),
                "configured": url == configured,
            }
        )
    return results


def resolve_instance(force: bool = False) -> Optional[str]:
    """
    取一个可用的实例地址。
    优先级：设置里手填的 → 上次自动探测成功的 → 按候选顺序现场探测。
    """
    configured = (db.get_setting("rsshub_instance") or "").strip().rstrip("/")
    if configured and not force:
        return configured

    cached = (db.get_setting("rsshub_instance_auto") or "").strip().rstrip("/")
    if cached and not force and _instance_ok(cached):
        return cached

    for url in INSTANCE_CANDIDATES:
        if _instance_ok(url):
            db.set_setting("rsshub_instance_auto", url)
            log.info("自动选用订阅服务实例：%s", url)
            return url
    return None


def build_route_url(route: str) -> str:
    """把路由拼成完整可订阅地址；没有可用实例时抛 ResolverError。"""
    base = resolve_instance()
    if not base:
        raise ResolverError(
            "暂时无法连接订阅服务，可以稍后再试，或直接粘贴该网站自己的订阅地址"
        )
    return base.rstrip("/") + route


# --------------------------------------------------------------------------- #
# 输入识别
# --------------------------------------------------------------------------- #
def is_url(text: str) -> bool:
    return bool(re.match(r"^https?://", (text or "").strip(), re.I))


def looks_like_feed_url(url: str) -> bool:
    """粗判是不是"像订阅地址"（.xml/.rss/atom/feed/feeds/ 等）。"""
    path = urlparse(url).path.lower()
    if re.search(r"\.(xml|rss|atom|json)$", path):
        return True
    if re.search(r"(feed|rss|atom)", path):
        return True
    if "feedburner" in (urlparse(url).netloc or "").lower():
        return True
    return False


def split_platform(text: str) -> tuple[Optional[str], str]:
    """
    "B站 半佛仙人" → ("bilibili", "半佛仙人")
    "半佛仙人"     → (None, "半佛仙人")
    """
    text = (text or "").strip()
    for alias in sorted(PLATFORM_ALIASES, key=len, reverse=True):
        if text.lower().startswith(alias):
            rest = text[len(alias):].lstrip(" :：·-—")
            if rest:
                return PLATFORM_ALIASES[alias], rest
    # 也支持"半佛仙人 B站"这种写法
    for alias in sorted(PLATFORM_ALIASES, key=len, reverse=True):
        if text.lower().endswith(alias):
            head = text[: -len(alias)].rstrip(" :：·-—")
            if head:
                return PLATFORM_ALIASES[alias], head
    return None, text


# --------------------------------------------------------------------------- #
# L1 / L2：链接 → 订阅源
# --------------------------------------------------------------------------- #
def resolve_url(url: str) -> Optional[dict]:
    """
    把一个链接解析成可订阅源。识别不了返回 None（上层会当作普通订阅地址去校验）。
    """
    url = (url or "").strip()
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    host = (parsed.netloc or "").lower().lstrip("www.")
    path = parsed.path or ""

    # B 站空间：space.bilibili.com/123456 或 bilibili.com/123456
    m = re.search(r"(?:space\.)?bilibili\.com/(\d+)", url)
    if m:
        mid = m.group(1)
        return _bilibili_candidate(int(mid))

    # 少数派
    if "sspai.com" in host:
        return {
            "label": "少数派",
            "detail": "高效工作，品质生活",
            "feed_url": "https://sspai.com/feed",
            "title": "少数派",
            "site_url": "https://sspai.com",
            "icon": None,
            "platform": "少数派",
        }

    # 知乎用户主页：zhihu.com/people/xxx → 原生抓取（需登录信息）
    m = re.search(r"zhihu\.com/people/([A-Za-z0-9_\-%]+)", url)
    if m:
        slug = m.group(1)
        return {
            "label": f"知乎 · {slug}",
            "detail": "知乎用户的动态与回答",
            "feed_url": f"byread://zhihu/people/{slug}",
            "title": f"知乎 · {slug}",
            "site_url": f"https://www.zhihu.com/people/{slug}",
            "icon": None,
            "platform": "知乎",
        }

    # 微博主页：weibo.com/u/1234567890、weibo.com/1234567890、m.weibo.cn/u/xxx
    m = (re.search(r"weibo\.(?:com|cn)/u/(\d+)", url)
         or re.search(r"weibo\.com/(\d{6,})", url)
         or re.search(r"m\.weibo\.cn/(?:profile|u)/(\d+)", url))
    if m:
        uid = m.group(1)
        return {
            "label": f"微博 · {uid}",
            "detail": "微博用户的微博",
            "feed_url": f"byread://weibo/user/{uid}",
            "title": f"微博 · {uid}",
            "site_url": f"https://m.weibo.cn/u/{uid}",
            "icon": None,
            "platform": "微博",
        }

    # B 站视频/专栏链接：不做猜测，交给上层当普通订阅地址去校验
    return None


def _bilibili_candidate(mid: int) -> dict:
    """构造 B 站候选（原生源）。名字延迟到添加时再取，避免多打一次接口。"""
    return {
        "label": f"B站 · 用户 {mid}",
        "detail": "B站 UP 主动态",
        "feed_url": f"byread://bilibili/dynamic/{mid}",
        "title": f"B站 · {mid}",
        "site_url": f"https://space.bilibili.com/{mid}",
        "icon": None,
        "platform": "B站",
    }


def _route_candidate(label: str, detail: str, route: str, title: str,
                     site_url: Optional[str], platform: str) -> dict:
    """需要实例拼路由的候选：feed_url 延迟到添加时解析，避免搜索阶段就卡在网络探测上。"""
    return {
        "label": f"{label} · {detail}",
        "detail": detail,
        "feed_url": None,
        "route": route,
        "title": title,
        "site_url": site_url,
        "icon": None,
        "platform": platform,
    }


def _preset_candidate(preset: dict) -> dict:
    if preset["kind"] == "direct":
        return {
            "label": preset["label"],
            "detail": preset.get("desc") or "推荐订阅源",
            "feed_url": preset["feed_url"],
            "title": preset["label"],
            "site_url": preset.get("site_url"),
            "icon": None,
            "platform": preset["label"],
        }
    return _route_candidate(
        label=preset["label"],
        detail=preset.get("desc") or "推荐订阅源",
        route=preset["route"],
        title=preset["label"],
        site_url=preset.get("site_url"),
        platform=preset["label"],
    )


# --------------------------------------------------------------------------- #
# L3：关键词 → 候选列表
# --------------------------------------------------------------------------- #
def _bilibili_candidates(keyword: str, limit: int = 4) -> tuple[list[dict], Optional[str]]:
    """B 站原生搜索。返回 (候选列表, 错误提示)。"""
    try:
        import bilibili

        users = bilibili.search_users(keyword, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("B 站搜索失败：%s", exc)
        return [], "B 站暂时搜不了，稍后再试"

    out = []
    for u in users:
        fans = u.get("fans")
        detail = f"{_fmt_fans(fans)}粉丝" if isinstance(fans, int) else "B站 UP 主"
        if u.get("sign"):
            detail += f" · {u['sign']}"
        out.append(
            {
                "label": f"B站 · {u['uname']}",
                "detail": detail,
                "feed_url": f"byread://bilibili/dynamic/{u['mid']}",
                "title": u["uname"],
                "site_url": f"https://space.bilibili.com/{u['mid']}",
                "icon": u.get("avatar"),
                "platform": "B站",
            }
        )
    return out, None


def _fmt_fans(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _preset_matches(platform: Optional[str], keyword: str) -> list[dict]:
    """关键词命中平台名时，直接给出该平台的固定源。"""
    text = (keyword or "").strip().lower()
    if not text:
        return []
    out = []
    for preset in PRESETS:
        if platform and platform not in (preset["id"], "sspai" if preset["id"] == "sspai" else preset["id"]):
            # 指定了具体平台时，只在匹配的预置源里找
            if platform != preset["id"]:
                continue
        hit = any(k.lower() in text or text in k.lower() for k in preset["keys"])
        if hit:
            out.append(_preset_candidate(preset))
    return out


def search(query: str) -> dict:
    """
    搜索即订阅的统一入口。返回：
    {"candidates": [...], "hint": str|None, "notes": [str]}
    """
    query = (query or "").strip()
    if not query:
        return {"candidates": [], "hint": "输入博主名、平台名或订阅地址", "notes": []}

    # L1：链接
    if is_url(query):
        candidate = resolve_url(query)
        if candidate:
            return {"candidates": [candidate], "hint": None, "notes": []}
        if looks_like_feed_url(query):
            return {
                "candidates": [
                    {
                        "label": "直接添加该订阅地址",
                        "detail": query,
                        "feed_url": query,
                        "title": urlparse(query).netloc or query,
                        "site_url": None,
                        "icon": None,
                        "platform": "订阅源",
                    }
                ],
                "hint": None,
                "notes": [],
            }
        return {
            "candidates": [],
            "hint": "这个链接我没法直接订阅，可以试试该网站的订阅地址（通常以 .xml 或 /feed 结尾）",
            "notes": [],
        }

    # 先做"整串精确命中预置源"的判断。
    # 必须放在平台拆分之前：否则"知乎日报"会被当成"知乎 + 日报"，
    # 于是走到"知乎需要粘贴主页链接"的死胡同（实测踩过这个坑）。
    normalized = query.lower()
    exact_presets = [
        p for p in PRESETS if normalized in [k.lower() for k in p["keys"]]
    ]
    if exact_presets:
        return {
            "candidates": [_preset_candidate(p) for p in exact_presets],
            "hint": None,
            "notes": [],
        }

    platform, keyword = split_platform(query)
    keyword = keyword.strip()
    candidates: list[dict] = []
    notes: list[str] = []

    # 固定平台 / 推荐源的命中结果排在前面：
    # 否则输入"少数派"时，B 站里同名的 UP 主会把这个网站挤到后面去。
    preset_hits = _preset_matches(platform, keyword or query)

    import cookies

    zhihu_ready = cookies.has_cookie("zhihu")
    weibo_ready = cookies.has_cookie("weibo")

    if platform == "zhihu":
        try:
            found = _zhihu_candidates(keyword)
        except Exception as exc:  # 登录失效这类问题要说清原因，不能笼统说"没搜到"
            return {"candidates": [], "hint": str(exc), "notes": notes}
        if not found:
            return {"candidates": [], "hint": _needs_cookie_hint("zhihu", zhihu_ready),
                    "notes": notes}
        candidates.extend(found)
    elif platform == "weibo":
        try:
            found = _weibo_candidates(keyword)
        except Exception as exc:
            return {"candidates": [], "hint": str(exc), "notes": notes}
        if not found:
            return {"candidates": [], "hint": _needs_cookie_hint("weibo", weibo_ready),
                    "notes": notes}
        candidates.extend(found)
    elif platform == "wechat":
        return {"candidates": [], "hint": _needs_cookie_hint("wechat", False),
                "notes": notes}

    if platform in (None, "bilibili"):
        candidates.extend(preset_hits)
        found, err = _bilibili_candidates(keyword)
        candidates.extend(found)
        if err:
            notes.append(err)
    elif platform not in ("zhihu", "weibo", "wechat"):
        candidates.extend(preset_hits)

    # 没指定平台时，配了登录信息的平台也一起搜 —— 这就是原文档里
    # "找到以下相关源，请选择"的多平台候选效果
    if platform is None:
        if zhihu_ready:
            try:
                candidates.extend(_zhihu_candidates(keyword, limit=3))
            except Exception as exc:  # noqa: BLE001
                notes.append(str(exc))
        if weibo_ready:
            try:
                candidates.extend(_weibo_candidates(keyword, limit=3))
            except Exception as exc:  # noqa: BLE001
                notes.append(str(exc))

    # 去重（同一个订阅地址只留一个）
    seen = set()
    unique = []
    for c in candidates:
        key = c.get("feed_url") or c.get("route") or c.get("label")
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    if not unique:
        hint = "没找到相关源。可以换个说法，或者直接粘贴该博主的主页链接 / 网站的订阅地址"
        if platform in NEEDS_URL_HINT:
            hint = _needs_cookie_hint(
                platform, zhihu_ready if platform == "zhihu" else weibo_ready
            )
        return {"candidates": [], "hint": hint, "notes": notes}

    return {"candidates": unique[:8], "hint": None, "notes": notes}


def presets_for_ui() -> list[dict]:
    """设置页"推荐订阅源"用。"""
    return [
        {
            "id": p["id"],
            "label": p["label"],
            "desc": p.get("desc"),
            "kind": p["kind"],
        }
        for p in PRESETS
    ]


def get_preset(preset_id: str) -> Optional[dict]:
    for p in PRESETS:
        if p["id"] == preset_id:
            return p
    return None


def candidate_to_feed(candidate: dict) -> dict:
    """
    把候选转成可入库的订阅信息：
    需要实例拼路由的候选在这里才真正解析 feed_url（失败则抛 ResolverError）。
    """
    feed_url = candidate.get("feed_url")
    if not feed_url and candidate.get("route"):
        feed_url = build_route_url(candidate["route"])
    if not feed_url:
        raise ResolverError("这个源暂时无法添加，请换一个试试")
    return {
        "feed_url": feed_url,
        "title": candidate.get("title") or candidate.get("label") or feed_url,
        "site_url": candidate.get("site_url"),
        "icon": candidate.get("icon"),
        "platform": candidate.get("platform"),
    }
