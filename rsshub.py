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
        # 「云风的博客」是自然说法，算精确命中（否则会被判成"只是提到云风"而降级）
        "keys": ["云风", "codingnow", "云风的博客", "云风的blog"],
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


def _zhihu_candidates(keyword: str, limit: int = 4,
                      budget: Optional[Budget] = None) -> list[dict]:
    """知乎按名字找人（需要登录信息，尽力而为）。登录失效会抛异常，由调用方转成提示。"""
    import cookies

    cookie = cookies.get_cookie("zhihu")
    if not cookie:
        return []
    try:
        import zhihu

        users = zhihu.search_people(keyword, cookie, limit=limit,
                                    timeout=(budget.request_timeout(10) if budget else 10))
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


def _weibo_candidates(keyword: str, limit: int = 4,
                      budget: Optional[Budget] = None) -> list[dict]:
    """微博按名字找人（需要登录信息，尽力而为）。登录失效会抛异常，由调用方转成提示。"""
    import cookies

    cookie = cookies.get_cookie("weibo")
    if not cookie:
        return []
    try:
        import weibo

        users = weibo.search_users(keyword, cookie, limit=limit,
                                   timeout=(budget.request_timeout(10) if budget else 10))
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
    """
    探测实例是否可用：拿一个最轻的固定路由试，**并且要求它真的返回了一份订阅内容**。

    只判断"HTTP < 400"是不够的：很多站点（SPA、带兜底路由的站）对任意路径都回 200，
    于是把 https://sspai.com 填进去也会被判成"可用"，最后拼出来的地址当然取不到东西。
    """
    if not base:
        return False
    try:
        r = requests.get(
            base.rstrip("/") + "/v2ex/topics/latest",
            timeout=timeout,
            headers={"User-Agent": "ByRead/0.1 (local RSS reader)"},
        )
        if r.status_code >= 400:
            return False
        ctype = (r.headers.get("Content-Type") or "").lower()
        head = (r.content or b"")[:2000].lower()
        return "xml" in ctype or b"<rss" in head or b"<feed" in head
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


# 明显不是"实例根地址"的形状：订阅地址、opml 之类
_FEEDISH_PATH_RE = re.compile(r"\.(xml|rss|atom|json|opml)$|/(rss|feed|feeds|atom)(/|$)",
                              re.IGNORECASE)


def validate_instance(url: str) -> tuple[bool, str]:
    """
    检查"手填的订阅服务实例"能不能用，返回 (是否可用, 给用户看的一句话)。

    为什么必须有这道校验：这个字段以前是**原样存下来、原样使用**的。实测有人把订阅地址
    （http://www.people.com.cn/rss/politics.xml）填进了这里 ——
    于是 V2EX / 豆瓣 / 36氪 这三个"需要拼路由"的预置源全部订阅失败，
    而错误信息只是一句"找不到"，完全看不出是设置填错了。
    所以：先按形状挡一道（订阅地址一眼能认出来），再真的拿一个最轻的路由探一下。
    """
    value = (url or "").strip().rstrip("/")
    if not value:
        return True, "留空 = 每次自动探测一个可用实例"

    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, "要填 http:// 或 https:// 开头的地址"
    if _FEEDISH_PATH_RE.search(parsed.path or ""):
        return False, ("这看起来是一个「订阅地址」（以 .xml / .rss / /feed 之类结尾）。"
                       "这里要填的是「订阅服务实例」的根地址，例如 https://rsshub.app；"
                       "想订阅这个地址，请回首页点「＋ 添加」再粘贴它。")
    if not _instance_ok(value):
        # 探测不通**不算填错**：公共实例本来就时好时坏。
        # 真正的保险在 resolve_instance()：用它之前会再确认一次，不通就自动换一个，
        # 所以这里只提醒，不拦着保存（拦了会让人以为"这个实例不能用"而白折腾）。
        return True, ("提醒：刚才没能从这个实例取到内容（公共实例经常时好时坏）。"
                      "用的时候会再确认一次，不通就自动换一个。")
    return True, "实例可用，已保存"


def resolve_instance(force: bool = False) -> Optional[str]:
    """
    取一个可用的实例地址。
    优先级：设置里手填的（**要先确认它真的能用**）→ 上次自动探测成功的 → 按候选顺序现场探测。

    注意手填值也要探一下：以前是无条件信任的，于是填错一个地址就会让所有"需要拼路由"的源
    静默失效（实测踩过）。宁可慢一点，也不要拿着一个坏地址去拼 URL。
    """
    configured = (db.get_setting("rsshub_instance") or "").strip().rstrip("/")
    if configured:
        if not force and _instance_ok(configured):
            return configured
        log.warning("手填的订阅服务实例不可用（%s），本次改用自动探测 —— "
                    "去设置页 →「高级」检查一下这个地址，或清空它", configured)

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
                     site_url: Optional[str], platform: str, match: str = "exact") -> dict:
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
        "match": match,
    }


def _preset_candidate(preset: dict, match: str = "exact") -> dict:
    """match：exact = 用户就是要它；loose = 用户只是"提到了"这个平台（见 _preset_tier）"""
    if preset["kind"] == "direct":
        return {
            "label": preset["label"],
            "detail": preset.get("desc") or "推荐订阅源",
            "feed_url": preset["feed_url"],
            "title": preset["label"],
            "site_url": preset.get("site_url"),
            "icon": None,
            "platform": preset["label"],
            "match": match,
        }
    return _route_candidate(
        label=preset["label"],
        detail=preset.get("desc") or "推荐订阅源",
        route=preset["route"],
        title=preset["label"],
        site_url=preset.get("site_url"),
        platform=preset["label"],
        match=match,
    )


# --------------------------------------------------------------------------- #
# L3：关键词 → 候选列表
# --------------------------------------------------------------------------- #
def _bilibili_candidates(keyword: str, limit: int = 4,
                         budget: Optional[Budget] = None) -> tuple[list[dict], Optional[str]]:
    """B 站原生搜索。返回 (候选列表, 错误提示)。timeout 取整条链预算的剩余量。"""
    try:
        import bilibili

        users = bilibili.search_users(
            keyword, limit=limit, timeout=(budget.request_timeout(10) if budget else 10)
        )
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


def platform_candidates(platform: str, keyword: str, limit: int = 4) -> list[dict]:
    """
    按"平台 + 名字"直接问平台搜索接口，返回候选 —— **不过相关度闸门**。

    给谁用：ai.py 的 AI 兜底路径。那里要搜的名字是模型猜出来的（可能是「财经」这种泛词），
    P0 的相关度闸门判的是"用户给的名字 vs 候选名"，会把这类结果全滤光，AI 就永远给不出东西。
    所以**安全性不在这道闸门，而在调用方**：
        · 候选只能来自平台搜索接口的真实返回（AI 编不出不存在的账号）；
        · AI 路径上的候选必须标 match="loose"，前端默认折叠、点开才可见。
    普通搜索路径请继续用 search()，**不要**用这个函数绕过闸门。
    """
    platform = (platform or "").strip().lower()
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    try:
        if platform == "bilibili":
            return _bilibili_candidates(keyword, limit=limit)[0]
        if platform == "zhihu":
            return _zhihu_candidates(keyword, limit=limit)
        if platform == "weibo":
            return _weibo_candidates(keyword, limit=limit)
    except Exception as exc:  # noqa: BLE001  搜索失败就当没猜出来，不往上抛
        log.info("platform_candidates 失败（%s / %s）：%s", platform, keyword, exc)
    return []


# --------------------------------------------------------------------------- #
# 解析链总时间预算（P0 精度修复之三）
#
# 背景：实测粘贴一个不支持的链接要 26～45 秒才失败。原因不是"某一次请求慢"，
# 而是**没有总预算**：页面抓 10s + 最多 10 个候选各 10s（每个候选还可能重定向），
# 而用户在前端只能看到转圈。
#
# 做法：整条链一个 deadline，每次网络调用取 min(剩余预算, 单次上限)；
# 预算耗尽立刻返回干净失败，不再去试别的路径。
# --------------------------------------------------------------------------- #
# 每次网络调用要预留的"不可控开销"（秒）：DNS 解析 / TLS 握手不被 requests 的 timeout 覆盖。
# 实测：传入 timeout=2.03s 的那次调用实际跑了 3.90s。预算必须留出这段才叫"硬上限"。
_REQUEST_OVERHEAD = 1.5


class Budget:
    """整条解析链的时间预算（秒）。用单调时钟，不受系统时间调整影响。"""

    def __init__(self, seconds: float):
        self.total = max(0.5, float(seconds))
        self.deadline = time.monotonic() + self.total

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def expired(self) -> bool:
        """留一点收尾余量：低于 0.2s 就当没时间了。"""
        return self.remaining() <= 0.2

    def slice(self, cap: float) -> float:
        """这一次调用能用多少秒：min(剩余, 单次上限)，至少给 0.5s 免得直接无效。"""
        return max(0.5, min(float(cap), self.remaining()))

    def request_timeout(self, cap: float = 10.0) -> tuple[float, float]:
        """
        给 requests 用的超时：(连接上限, 读取上限)，且两者之和 ≤ 本次可用时间 - 预留开销。

        requests 的标量 timeout 是"建立连接"和"读取"**各自**的上限 ——
        给一个数就等于允许最坏两倍，8s 的预算会跑出 9.9s（实测贴边冲破 10s 验收）。
        另外 DNS 解析 / TLS 握手根本不在 timeout 覆盖范围内（实测有单次调用
        超时 2.03s 却实跑 3.90s），所以每次调用还要留出 _REQUEST_OVERHEAD 的余量。
        拆成二元组 + 留余量后，单次调用的最坏耗时仍落在预算内。
        """
        s = max(0.5, self.slice(cap) - _REQUEST_OVERHEAD)
        connect = max(0.5, min(3.5, s / 2))
        return (connect, max(0.5, s - connect))


def _search_budget() -> Budget:
    return Budget(_setting_float("search_budget_seconds", 8.0))


# 预算耗尽时的统一说法：不要笼统说"网络错误"，要让用户知道"不是没这个东西，是没查完"
_TIMEOUT_HINT = ("这个地址查起来太慢了，我先停在这儿（没查完，不代表它不能订）。"
                 "可以直接给我订阅地址（.xml / /feed 结尾），或者过一会儿再试。")


# --------------------------------------------------------------------------- #
# 平台搜索结果的相关度闸门（P0 精度修复之二）
#
# 背景：B 站搜索接口对**任意字符串**都会返回一批 UP 主（模糊匹配），
# 而我们把 Top N 直接当候选端出去了 —— 于是「今天天气不错」也能给出
# 「B站 · py今天天气不错」、「隔壁老王的空间」给出「B站 · 隔壁空间站的老王」。
# 用户看到"已添加"，然后读着陌生人的内容，全程没有报错。这是最坏的一类体验。
#
# 两道关：
#   1. **这不是个名字，是句话** → 干脆不搜（搜索接口对句子只会瞎给）。
#      判据是可配置的句式词表（settings: search_sentence_markers）。
#   2. 名称相关度：查询词与候选名必须"像同一个东西"才留下，阈值可配置。
#      ⚠️ 光靠相关度是不够的：实测「今天天气不错」⊂「py今天天气不错」、
#      「半佛仙人」⊂「硬核的半佛仙人」在数学上完全同构（都是前缀包含、LCS 比都是 1.0），
#      任何阈值都无法把它们分开 —— 所以第 1 关（句式词）才是消除那类事故的关键。
# --------------------------------------------------------------------------- #
_BRACKET_RE = re.compile(r"[【\[（(][^】\]）)]*[】\]）)]")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\u2b00-\u2bff\u2190-\u21ff\u2700-\u27bf\u2122\u00ae\u00a9]+"
)

# 句式词：出现这些，说明用户在"说话/提问"，不是在给一个博主名
DEFAULT_SENTENCE_MARKERS = (
    "帮我", "我想", "我要", "有没有", "什么", "怎么", "为什么", "是不是", "能不能",
    "那个", "这个", "每天", "空间", "主页", "频道", "账号", "博主", "主播", "天气", "不错",
    "推荐个", "来一个", "订阅个",
)


def _setting_float(key: str, default: float) -> float:
    try:
        raw = db.get_setting(key)
        return float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        log.warning("设置 %s 不是数字，用默认值 %s", key, default)
        return default


def _sentence_markers() -> tuple[str, ...]:
    """句式词表：settings 里可覆盖（逗号分隔），默认见 DEFAULT_SENTENCE_MARKERS。"""
    raw = (db.get_setting("search_sentence_markers") or "").strip()
    if not raw:
        return DEFAULT_SENTENCE_MARKERS
    parts = [p.strip() for p in re.split(r"[,，\s]+", raw) if p.strip()]
    return tuple(parts) or DEFAULT_SENTENCE_MARKERS


def looks_like_sentence(text: str) -> bool:
    """粗判"这句话不是在说一个名字"（用于决定**不**去跑平台名字搜索）。"""
    if not text:
        return False
    t = _norm(text)
    return any(_norm(m) and _norm(m) in t for m in _sentence_markers())


def _norm_name(text: str) -> str:
    """候选名归一化：去括号内容（【】[]（）()）、去 emoji，再走通用归一化。"""
    t = _BRACKET_RE.sub("", text or "")
    t = _EMOJI_RE.sub("", t)
    return _norm(t)


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（编辑距离那套的 O(n*m) DP；名字都很短，够用）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def relevance(query: str, name: str) -> tuple[bool, float, str]:
    """
    判断"候选名"是不是用户要找的东西。返回 (是否保留, 得分, 原因)。

    保留条件（任一，阈值可配置）：
      · 互为子串（「半佛仙人」↔「硬核的半佛仙人」）
      · 最长公共子串 ÷ len(query) ≥ search_relevance_lcs（默认 0.6）
      · 字符集合 Jaccard ≥ search_relevance_jaccard（默认 0.5）
    """
    q, n = _norm_name(query), _norm_name(name)
    if not q or not n:
        return False, 0.0, "空"
    if q == n:
        return True, 1.0, "完全相同"
    if q in n or n in q:
        score = min(len(q), len(n)) / max(len(q), len(n))
        return True, score, "互相包含"
    lcs_min = _setting_float("search_relevance_lcs", 0.6)
    jac_min = _setting_float("search_relevance_jaccard", 0.5)
    lcs_ratio = _lcs_len(q, n) / len(q)
    if lcs_ratio >= lcs_min:
        return True, lcs_ratio, f"公共子串比 {lcs_ratio:.2f}"
    jac = _jaccard(q, n)
    if jac >= jac_min:
        return True, jac, f"字符相似度 {jac:.2f}"
    return False, max(lcs_ratio, jac), f"太不像（子串比 {lcs_ratio:.2f}/相似度 {jac:.2f}）"


def _gate_platform_candidates(keyword: str, found: list[dict],
                              platform_label: str) -> list[dict]:
    """对平台搜索返回的候选做相关度闸门；被滤掉的写进日志（含得分，便于以后调阈值）。"""
    kept, dropped = [], []
    for c in found:
        name = c.get("title") or c.get("label") or ""
        ok, score, why = relevance(keyword, name)
        if ok:
            kept.append(c)
        else:
            dropped.append((name, score, why))
    if dropped:
        log.info("相关度闸门（%s，查询=%r）：滤掉 %d 个 → %s", platform_label, keyword,
                 len(dropped), "；".join(f"{n}={s:.2f}({w})" for n, s, w in dropped[:6]))
    return kept


# --------------------------------------------------------------------------- #
# 查询归一化 & 预置源分档（P0 精度修复之一）
#
# 背景：原来判断"预置源命中"用的是纯子串匹配：
#     hit = any(k in text or text in k for k in preset["keys"])
# 于是输入「豆瓣 租房小组」会命中「豆瓣电影正在上映」，还排在候选第一位。
# 问题不在子串匹配本身，而在于**把"提到了这个平台"当成了"要订的就是这个源"**。
#
# 所以分成三档：
#   A 精确   —— 归一化后正好等于它的名字/别名：用户就是在要它（少数派 / V2EX / 知乎日报…）
#   B 提到   —— 别名只是查询的一部分（豆瓣 租房小组）：**不算命中**，
#               降级到候选列表末尾 + 打标记 + 给一句人话说明
#   C 未提及 —— 忽略
# --------------------------------------------------------------------------- #
_PUNCT_RE = re.compile(r"""[\s,，。、;；:：!！?？'"“”‘’()（）\[\]【】{}<>《》…—\-_·~`|/\\]+""")


def _norm(text: str) -> str:
    """归一化查询词：小写 + 去掉空白与常见中英文标点（用于"是不是同一个词"的判断）。"""
    return _PUNCT_RE.sub("", (text or "").lower())


def _preset_tier(query_norm: str, preset: dict) -> tuple[str, str, str, bool]:
    """
    判断预置源与查询的关系，返回 (档位, 涉及的名字, 剩下的部分, 是否"用户只打了半截")。

    剩下的部分用于文案：输入「豆瓣 租房小组」+ 命中别名「豆瓣」→ 剩下「租房小组」，
    于是可以说"没找到「租房小组」，你提到了「豆瓣」，我这儿只有「豆瓣电影正在上映」"。
    partial=True 是反向情况（「少数」→「少数派」），文案要说"我这儿有个「少数派」"。
    """
    if not query_norm:
        return "C", "", "", False
    names = [preset["label"], *(preset.get("keys") or [])]

    for name in names:                      # A：完全相等（含 label，例如「GitHub 每日趋势」）
        if _norm(name) and _norm(name) == query_norm:
            return "A", name, "", False

    best: Optional[tuple[str, str, str, int, bool]] = None
    for name in names:
        n = _norm(name)
        if not n:
            continue
        if n in query_norm and len(n) < len(query_norm):
            # 平台名被提到，但后面还跟着别的东西 → 只是"提到"
            score = len(n)
            if best is None or score > best[3]:
                best = ("B", name, query_norm.replace(n, "", 1), score, False)
        elif query_norm in n and len(query_norm) < len(n):
            # 反向：用户只打了半截（「少数」→「少数派」），也算提到
            score = -len(n)
            if best is None or score > best[3]:
                best = ("B", name, query_norm, score, True)
    if best:
        return best[0], best[1], best[2], best[4]
    return "C", "", "", False


def _preset_matches(platform: Optional[str], keyword: str) -> tuple[list[dict], list[dict]]:
    """
    关键词匹配预置源。返回 (精确命中, 仅提及)。

    指定了平台时只看该平台的预置源（"B站 热门"不会跑去找少数派）。
    """
    text = _norm(keyword)
    if not text:
        return [], []
    exact: list[dict] = []
    loose: list[dict] = []
    for preset in PRESETS:
        if platform and platform != preset["id"]:
            continue
        tier, name, rest, partial = _preset_tier(text, preset)
        if tier == "A":
            exact.append(_preset_candidate(preset))
        elif tier == "B":
            cand = _preset_candidate(preset, match="loose")
            cand["mentioned"] = name
            cand["unmatched"] = rest
            cand["partial_query"] = partial
            cand["detail"] = f"你提到的是「{name}」，这是「{preset['label']}」"
            loose.append(cand)
    return exact, loose


def discover_candidates(page_url: str,
                        budget: Optional[Budget] = None) -> list[dict]:
    """
    从一个普通网页里自动发现订阅地址，转成候选格式交给现有弹窗。
    只有**真实解析成功**的地址才会被返回（绝不允许把首页 HTML 当成 feed）。
    budget：整条解析链的时间预算，透传成绝对 deadline 给 feed_parser，
    让"打开页面 + 试 N 个常见路径"共用同一份预算，而不是各花各的 10 秒。
    """
    try:
        import feed_parser

        found = feed_parser.discover_feeds(
            page_url, deadline=(budget.deadline if budget else None))
    except Exception as exc:  # noqa: BLE001
        log.info("自动发现订阅地址失败：%s %s", page_url, exc)
        return []

    netloc = urlparse(page_url).netloc
    out = []
    for item in found:
        out.append({
            "label": item.get("title") or netloc,
            "detail": "在这个网页里发现的订阅地址",
            "feed_url": item["feed_url"],
            "title": item.get("title") or netloc,
            "site_url": item.get("site_url") or page_url,
            "icon": item.get("icon"),
            "platform": None,
        })
    return out


def search(query: str, budget: Optional[Budget] = None) -> dict:
    """
    搜索即订阅的统一入口。返回：
    {"candidates": [...], "hint": str|None, "notes": [str]}

    链接的优先级（从高到低）：
      1. 预置源 / 平台识别（少数派、B站空间、知乎主页…）
      2. 这本身就是一个订阅地址 → 直接校验添加
      3. 自动发现：打开这个网页，从里面找出订阅地址（每个都必须真实解析成功）

    budget：整条解析链的时间预算（settings: search_budget_seconds，默认 8s）。
    不传就按设置新建一个 —— 保证"无论走到哪一层，总耗时都有上限"，
    而不是某一层 10s、下一层再 10s、用户面前转几十秒。
    """
    query = (query or "").strip()
    if not query:
        return {"candidates": [], "hint": "输入博主名、平台名或订阅地址", "notes": []}

    if budget is None:
        budget = _search_budget()

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
        # 预算已经花光就别再开新请求了：直接干净失败，别让用户干等
        if budget.expired():
            log.info("解析预算已用尽（%.1fs），跳过自动发现：%s", budget.total, query)
            return {"candidates": [], "hint": _TIMEOUT_HINT, "notes": []}
        # L3：普通网页 → 自动发现订阅地址
        discovered = discover_candidates(query, budget=budget)
        if discovered:
            return {"candidates": discovered, "hint": None, "notes": []}
        if budget.expired():
            return {"candidates": [], "hint": _TIMEOUT_HINT, "notes": []}
        return {
            "candidates": [],
            "hint": "这个网页里没找到订阅地址。可以看看页面底部有没有 RSS / 订阅 链接，"
                    "通常以 .xml 或 /feed 结尾",
            "notes": [],
        }

    # 先做"整串精确命中预置源"的判断（A 档）。
    # 必须放在平台拆分之前：否则"知乎日报"会被当成"知乎 + 日报"，
    # 于是走到"知乎需要粘贴主页链接"的死胡同（实测踩过这个坑）。
    query_norm = _norm(query)
    exact_presets = [p for p in PRESETS if _preset_tier(query_norm, p)[0] == "A"]
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
    # preset_loose 只放在最后（用户"提到"了这个平台，但没说要订它）。
    preset_exact, preset_loose = _preset_matches(platform, keyword or query)

    import cookies

    zhihu_ready = cookies.has_cookie("zhihu")
    weibo_ready = cookies.has_cookie("weibo")

    # P0 精度闸门（一）：这看起来是"一句话"，不是"一个名字" → 不跑平台名字搜索。
    # 平台搜索接口对任意字符串都会瞎给一批人（实测「今天天气不错」→「py今天天气不错」），
    # 而用户看到候选就会点，点完就是"静默订错"。宁可干净失败 + 告诉他怎么给地址。
    sentence_like = looks_like_sentence(keyword or query)
    if sentence_like:
        log.info("判为自由表述，跳过平台名字搜索：%r", query)

    if platform == "zhihu":
        try:
            found = [] if sentence_like else _zhihu_candidates(keyword, budget=budget)
        except Exception as exc:  # 登录失效这类问题要说清原因，不能笼统说"没搜到"
            return {"candidates": [], "hint": str(exc), "notes": notes}
        if not found and not sentence_like:
            return {"candidates": [], "hint": _needs_cookie_hint("zhihu", zhihu_ready),
                    "notes": notes}
        candidates.extend(_gate_platform_candidates(keyword, found, "知乎"))
    elif platform == "weibo":
        try:
            found = [] if sentence_like else _weibo_candidates(keyword, budget=budget)
        except Exception as exc:
            return {"candidates": [], "hint": str(exc), "notes": notes}
        if not found and not sentence_like:
            return {"candidates": [], "hint": _needs_cookie_hint("weibo", weibo_ready),
                    "notes": notes}
        candidates.extend(_gate_platform_candidates(keyword, found, "微博"))
    elif platform == "wechat":
        return {"candidates": [], "hint": _needs_cookie_hint("wechat", False),
                "notes": notes}

    if platform in (None, "bilibili"):
        candidates.extend(preset_exact)
        if sentence_like:
            found, err = [], None
        else:
            found, err = _bilibili_candidates(keyword, budget=budget)
        candidates.extend(_gate_platform_candidates(keyword, found, "B站"))
        if err:
            notes.append(err)
    elif platform not in ("zhihu", "weibo", "wechat"):
        candidates.extend(preset_exact)

    # 没指定平台时，配了登录信息的平台也一起搜 —— 这就是原文档里
    # "找到以下相关源，请选择"的多平台候选效果
    if platform is None and not sentence_like:
        if zhihu_ready:
            try:
                candidates.extend(_gate_platform_candidates(
                    keyword, _zhihu_candidates(keyword, limit=3, budget=budget), "知乎"))
            except Exception as exc:  # noqa: BLE001
                notes.append(str(exc))
        if weibo_ready:
            try:
                candidates.extend(_gate_platform_candidates(
                    keyword, _weibo_candidates(keyword, limit=3, budget=budget), "微博"))
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

    # "只是提到"的预置源：排在精确候选**后面**，并带上人话说明。
    # 它们不算命中 —— 用户可能只是顺口提了这个平台（实测：输入「豆瓣 租房小组」
    # 原来会把「豆瓣电影正在上映」当命中端出去，用户订完才发现订错了）。
    loose_unique = []
    for c in preset_loose:
        key = c.get("feed_url") or c.get("route") or c.get("label")
        if key in seen:
            continue
        seen.add(key)
        loose_unique.append(c)

    if not unique and not loose_unique:
        if sentence_like:
            # 自由表述：P0 不解决它，但必须"失败得体面"——明确说没读懂，并给出下一步
            return {"candidates": [], "hint": (
                f"「{query}」这句我没读出要订什么。可以直接给「平台 名字」"
                f"（例如「B站 半佛仙人」），或者把 TA 的主页链接粘过来。"), "notes": notes}
        if budget.expired():
            # 没查完就别说"没找到"——那是两件事，说错了会让用户以为这个人不存在
            log.info("解析预算用尽（%.1fs），返回超时提示：%r", budget.total, query)
            return {"candidates": [], "hint": _TIMEOUT_HINT, "notes": notes}
        hint = f"没找到叫「{keyword}」的博主。" if keyword else "没找到相关源。"
        hint += "如果你知道 TA 的主页链接（例如 space.bilibili.com/12345），粘过来我就能订。"
        if platform in NEEDS_URL_HINT:
            hint = _needs_cookie_hint(
                platform, zhihu_ready if platform == "zhihu" else weibo_ready
            )
        return {"candidates": [], "hint": hint, "notes": notes}

    # 只有"提到"的候选时，**不能**说"已找到 N 个相关源"，要把话说清楚：
    #   没找到「租房小组」。你提到了「豆瓣」，我这儿只有「豆瓣电影正在上映」——要订这个吗？
    hint = None
    if loose_unique and not unique:
        first = loose_unique[0]
        mentioned = first.get("mentioned") or ""
        rest = first.get("unmatched") or ""
        if first.get("partial_query"):
            # 用户只打了半截（「少数」→「少数派」）
            hint = f"没找到「{rest}」。我这儿有个「{first['label']}」——要订这个吗？"
        elif _norm(mentioned) == _norm(first.get("title") or ""):
            # 提到的就是源名本身（「掘金 前端」+「掘金」），别重复说两遍
            hint = f"没找到「{rest}」。我这儿有「{first['label']}」——要订这个吗？"
        else:
            hint = (f"没找到「{rest}」。你提到了「{mentioned}」，"
                    f"我这儿只有「{first['label']}」——要订这个吗？")
        if len(loose_unique) > 1:
            hint += f"（另有 {len(loose_unique) - 1} 个相关源，都在下面）"

    return {"candidates": (unique + loose_unique)[:8], "hint": hint, "notes": notes}


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
