"""
app.py —— 白读 · ByRead 主应用

运行：python app.py  然后访问 http://127.0.0.1:5000

架构要点：
    1. 抓取一律走后台线程，"刷新"接口立即返回，前端轮询进度
       —— 否则 10 个源 × 3 次重试会把 Flask 单线程彻底堵死（原文档没写这一点，但 9.2 的进度条隐含了这个要求）。
    2. 正文提取是异步的：阅读页先出骨架和摘要，正文由前端单独请求，提取失败就优雅回退。
    3. 所有网络请求都有超时（≤10 秒），所有数据库操作都有异常处理。
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, render_template, request

import db
import cookies
import exporter
import feed_parser
import net  # noqa: F401  统一网络初始化（让 Python 用系统证书库）
import rsshub
from errors import TemporaryFeedError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("byread")

app = Flask(__name__)
# 接口返回中文而不是 \uXXXX 转义。注意：Flask 2.3 起 config["JSON_AS_ASCII"] 已经失效
# （这里原来那一行就是死配置），要用 app.json.ensure_ascii；前端两种都能解析，
# 但 curl 看接口、看日志时人眼友好得多。
app.json.ensure_ascii = False
app.config["TEMPLATES_AUTO_RELOAD"] = True

# 首次启动时自动添加的示例源（都是实测可直接抓取的，不依赖任何第三方服务）
SEED_FEEDS = [
    ("https://www.ruanyifeng.com/blog/atom.xml", "阮一峰的网络日志"),
    ("https://sspai.com/feed", "少数派"),
    ("https://hnrss.org/frontpage", "Hacker News"),
]

MAX_RETRIES = 3
RETRY_BACKOFF = 2  # 秒，指数退避


# --------------------------------------------------------------------------- #
# 后台刷新状态机
# --------------------------------------------------------------------------- #
REFRESH: dict = {
    "running": False,
    "mode": "",            # all / single
    "total": 0,
    "done": 0,
    "current": "",
    "new_count": 0,
    "filled_count": 0,     # 本次补齐正文/修正标题的篇数
    "results": [],
    "started_at": None,
    "finished_at": None,
    "last_run": None,      # 上次成功跑完的时间戳，供自动刷新判断
}
_refresh_lock = threading.Lock()


def _update_refresh(**kwargs) -> None:
    with _refresh_lock:
        REFRESH.update(kwargs)


def _title_is_placeholder(title: Optional[str], feed_url: str) -> bool:
    """
    判断订阅源标题是不是占位符（还没拿到真名）。
    包含 HTML 标签的也算：搜索结果里的名字带 <em> 高亮（例如 "<em>文元</em>"），
    这类脏标题要在下次成功抓取时被真名替换掉。
    """
    if not title:
        return True
    if title == feed_url:
        return True
    if "<" in title or ">" in title:
        return True
    tail = feed_url.rstrip("/").rsplit("/", 1)[-1]
    for prefix in ("B站 · ", "知乎 · ", "微博 · "):
        if title == f"{prefix}{tail}":
            return True
    # 标题就是订阅地址的域名："搜索即订阅"里"直接添加该订阅地址"那个候选就是这么填的
    # （它为了秒回不去抓源，标题只能先用域名兜着）。这不是真名，抓取成功后用源里的真名替换。
    host = (urlparse(feed_url).netloc or "").lower()
    bare = host[4:] if host.startswith("www.") else host
    if bare and title.strip().lower() in (host, bare):
        return True
    return bool(re.match(r"^(B站 · )?(用户|UP)?\s*\d+$", title.strip()))


def _cookie_requirement(feed_url: str) -> Optional[str]:
    """有些源在拿到登录信息之前根本抓不到，提前说清楚，而不是加进去一直失败。"""
    if feed_url.startswith("byread://zhihu/people/") and not cookies.has_cookie("zhihu"):
        return "知乎需要先在「设置页 → 登录信息」配置一次，然后才能订阅"
    if feed_url.startswith("byread://weibo/") and not cookies.has_cookie("weibo"):
        return "微博需要先在「设置页 → 登录信息」配置一次，然后才能订阅"
    return None


def _refresh_one(feed: dict) -> dict:
    """
    抓取单个源，带重试与错误计数。返回结果字典（不抛异常）。
    """
    feed_id = feed["id"]
    title = feed.get("title") or feed["feed_url"]
    last_error = ""
    temporary_error = False
    content_state = db.get_content_state(feed_id)
    for attempt in range(MAX_RETRIES):
        try:
            data = feed_parser.fetch_feed(
                feed["feed_url"], timeout=10, limit=30, content_state=content_state
            )
            new_count = 0
            filled_count = 0
            for item in data.get("entries") or []:
                if db.insert_article(feed_id, item):
                    new_count += 1
                elif db.update_article_if_incomplete(feed_id, item):
                    filled_count += 1

            # 本地源的标题永远以抓取结果为准（用户没有重命名功能，不存在覆盖用户意图的问题），
            # 这样搜索带来的脏标题（例如 "<em>文元</em>"）会在下次成功抓取时自愈
            new_title = data.get("title")
            if new_title and (feed_parser.is_local_feed(feed["feed_url"])
                              or _title_is_placeholder(feed.get("title"), feed["feed_url"])):
                db.update_feed_meta(feed_id, title=new_title)
            # 本地生成的源（B站）头像/主页地址随抓取结果补全
            if data.get("icon") and data["icon"] != feed.get("icon"):
                db.update_feed_meta(feed_id, icon=data["icon"])
            if data.get("site_url") and data["site_url"] != feed.get("site_url"):
                db.update_feed_meta(feed_id, site_url=data["site_url"])

            db.mark_feed_success(feed_id)
            return {
                "feed_id": feed_id,
                "feed": title,
                "status": "success",
                "new": new_count,
                "filled": filled_count,
                "total": len(data.get("entries") or []),
                "attempts": attempt + 1,
            }
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            # 临时性失败（限流/软封/登录态过期）不值得重试三次，一次就够 ——
            # 再试只会加重对方的限流
            if isinstance(exc, TemporaryFeedError):
                temporary_error = True
                break
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF ** attempt)  # 1s, 2s

    state = db.mark_feed_failure(feed_id, last_error,
                                 count_toward_pause=not temporary_error)
    # 失败必须留下痕迹：否则日志里一片安静，只剩一个"新增 0 篇"，无从排查
    if temporary_error:
        log.warning("抓取暂时失败：%s → %s（临时性失败，不计入暂停，下次刷新会再试）",
                    title, last_error)
    else:
        log.warning("抓取失败：%s（已重试 %s 次）→ %s%s", title, MAX_RETRIES, last_error,
                    "，已自动暂停该源" if state.get("is_active") == 0 else "")
    return {
        "feed_id": feed_id,
        "feed": title,
        "status": "failed",
        "error": last_error,
        "attempts": MAX_RETRIES,
        "paused": state.get("is_active") == 0,
        "temporary": temporary_error,
    }


def _refresh_worker(feed_ids: Optional[list[int]] = None) -> None:
    """后台刷新线程主体。运行标志在 start_refresh 里就已经置位（避免轮询看到上一轮的状态）。"""
    feeds = db.get_feeds(active_only=False)
    if feed_ids is not None:
        wanted = set(feed_ids)
        feeds = [f for f in feeds if f["id"] in wanted]

    _update_refresh(total=len(feeds))
    total_new = 0
    total_filled = 0
    try:
        for index, feed in enumerate(feeds, start=1):
            _update_refresh(current=feed.get("title") or feed["feed_url"], done=index - 1)
            result = _refresh_one(feed)
            total_new += result.get("new", 0)
            total_filled += result.get("filled", 0)
            with _refresh_lock:
                REFRESH["results"].append(result)
                REFRESH["new_count"] = total_new
                REFRESH["filled_count"] = total_filled
                REFRESH["done"] = index
    except Exception as exc:  # noqa: BLE001
        log.exception("刷新线程异常：%s", exc)
    finally:
        _update_refresh(
            running=False, current="", finished_at=db.utc_now_iso(),
            last_run=time.time(), new_count=total_new, filled_count=total_filled,
        )


def start_refresh(feed_ids: Optional[list[int]] = None) -> dict:
    """
    启动后台刷新；已有任务在跑时直接返回当前进度。

    关键：在这里（请求线程内）就把 running 置为 True，而不是等子线程起来再置。
    否则前端在 POST 之后立刻轮询，可能读到"上一轮已结束"的状态，
    误以为这次抓取已经完成 —— 这是实测中真实出现过的竞态。
    """
    with _refresh_lock:
        if REFRESH["running"]:
            return {"started": False, "running": True}
        REFRESH.update(
            {
                "running": True,
                "mode": "single" if feed_ids else "all",
                "total": 0,
                "done": 0,
                "current": "",
                "new_count": 0,
                "filled_count": 0,
                "results": [],
                "started_at": db.utc_now_iso(),
                "finished_at": None,
            }
        )
    try:
        thread = threading.Thread(
            target=_refresh_worker, args=(feed_ids,), name="byread-refresh", daemon=True
        )
        thread.start()
    except Exception as exc:  # noqa: BLE001
        log.error("刷新线程启动失败：%s", exc)
        _update_refresh(running=False, finished_at=db.utc_now_iso())
        return {"started": False, "running": False, "error": str(exc)}
    return {"started": True, "running": True}


def _scheduler_loop() -> None:
    """按设置的时间间隔自动刷新（0 = 关闭）。"""
    while True:
        try:
            time.sleep(60)
            minutes = int(db.get_setting("auto_refresh_minutes") or 0)
            if minutes <= 0:
                continue
            with _refresh_lock:
                running = REFRESH["running"]
                last = REFRESH["last_run"]
            if running:
                continue
            if last is None or (time.time() - last) >= minutes * 60:
                if db.get_feeds(active_only=True):
                    log.info("自动刷新开始（间隔 %s 分钟）", minutes)
                    start_refresh()
        except Exception as exc:  # noqa: BLE001
            log.warning("自动刷新调度异常：%s", exc)


# --------------------------------------------------------------------------- #
# 首次启动种子数据
# --------------------------------------------------------------------------- #
def bootstrap() -> None:
    db.init_db()
    if db.get_setting("seeded") == "1":
        return
    log.info("首次启动：添加示例订阅源")
    for url, title in SEED_FEEDS:
        db.add_feed(url, title)
    db.set_setting("seeded", "1")
    start_refresh()


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
@app.context_processor
def inject_globals():
    """所有模板都能拿到主题、视图模式、侧边栏状态（服务端渲染首屏，避免闪烁）。"""
    return {
        "theme": db.get_setting("theme") or "light",
        "view_mode": db.get_setting("view_mode") or "card",
        "sidebar_open": (db.get_setting("sidebar_open") or "true").lower() == "true",
    }


@app.route("/")
def page_index():
    return render_template("index.html")


@app.route("/reader/<int:article_id>")
def page_reader(article_id: int):
    article = db.get_article(article_id)
    if not article:
        return render_template("reader.html", article=None), 404
    return render_template("reader.html", article=article)


@app.route("/settings")
def page_settings():
    return render_template("settings.html", db_path=str(db.DB_PATH))


# --------------------------------------------------------------------------- #
# 文章 API
# --------------------------------------------------------------------------- #
VALID_VIEWS = {"all", "unread", "starred"}


def _current_view() -> str:
    view = (request.args.get("view") or "all").lower()
    return view if view in VALID_VIEWS else "all"


def _counts() -> dict:
    """
    给前端的角标数字。**必须带上关键词过滤**，和列表接口保持同一套口径。

    不带过滤的后果实测过：用户配了过滤词「直播」（藏起 3 篇）时，
    任何一次操作（标记已读 / 收藏 / 删除 / 删订阅）返回的角标都会从 216 跳回 219，
    和列表里的总数对不上，看起来像"凭空多出来 3 篇"。
    """
    return db.get_counts(keywords=db.get_filter_keywords())


@app.get("/api/articles")
def api_articles():
    view = _current_view()
    feed_id = request.args.get("feed_id", type=int)
    channel = request.args.get("channel", type=int)
    cursor = request.args.get("cursor")
    query = (request.args.get("q") or "").strip() or None
    folder = (request.args.get("folder") or "").strip() or None
    limit = request.args.get("limit", type=int) or int(db.get_setting("page_size") or 30)
    keywords = db.get_filter_keywords()

    data = db.get_articles(view=view, feed_id=feed_id, keywords=keywords,
                           limit=limit, cursor=cursor, query=query, folder=folder,
                           channel=channel)
    total = db.count_articles(view=view, feed_id=feed_id, keywords=keywords,
                              query=query, folder=folder, channel=channel)
    # 有多少篇是被"关键词过滤"藏起来的 —— 前端拿它做个提示，
    # 免得用户看到"来源写着 16 篇、点进去只有 13 篇"时一头雾水
    hidden = 0
    if keywords:
        hidden = max(0, db.count_articles(view=view, feed_id=feed_id, keywords=None,
                                         query=query, folder=folder,
                                         channel=channel) - total)
    return jsonify(
        {
            **data,
            "view": view,
            "query": query,
            "folder": folder,
            "channel": channel,
            "counts": db.get_counts(keywords=keywords),
            "total": total,
            "hidden_by_filter": hidden,
            "filter_keywords": keywords,
        }
    )


@app.get("/api/counts")
def api_counts():
    return jsonify(_counts())


@app.get("/api/article/<int:article_id>")
def api_article(article_id: int):
    article = db.get_article(article_id)
    if not article:
        return jsonify({"error": "文章不存在"}), 404
    return jsonify(
        {
            "id": article["id"],
            "title": article["title"],
            "link": article["link"],
            "author": article["author"],
            "published": article["published"],
            "feed_title": article["feed_title"],
            "feed_site_url": article["feed_site_url"],
            "summary": article["summary"],
            "content": article["content"],
            "content_tried": bool(article.get("content_tried")),
            "is_read": bool(article["is_read"]),
            "is_starred": bool(article["is_starred"]),
            # 收藏夹信息（阅读页的「📁」按钮要用）
            "folder_id": article.get("folder_id"),
            "folder_name": article.get("folder_name"),
            "folder_color": article.get("folder_color"),
            # 播客音频（阅读页的播放器要用）。地址单独一列，不在 content 里 —— 
            # 正文清洗会把 <audio> 整段丢掉，那是防注入的红线
            "audio_url": article.get("audio_url"),
            "audio_duration": article.get("audio_duration"),
        }
    )


@app.post("/api/article/<int:article_id>/content")
def api_article_content(article_id: int):
    """
    按需提取正文。前端先渲染摘要，再调这个接口补正文；
    提取失败返回 ok=False，前端保留摘要 + 原文链接（不会出现空白页）。
    """
    article = db.get_article(article_id)
    if not article:
        return jsonify({"error": "文章不存在"}), 404

    payload = request.get_json(silent=True) or {}
    force = bool(payload.get("force"))

    if article.get("content") and not force:
        return jsonify({"ok": True, "content": article["content"], "cached": True})

    if article.get("content_tried") and not force:
        return jsonify({"ok": False, "content": None, "cached": False,
                        "reason": "之前提取失败过"})

    if not article.get("link"):
        db.save_article_content(article_id, None, tried=True)
        return jsonify({"ok": False, "content": None, "reason": "该文章没有原文链接"})

    block_images = (db.get_setting("block_images") or "false").lower() == "true"
    content = feed_parser.extract_article_content(
        article["link"], timeout=10, block_images=block_images
    )
    db.save_article_content(article_id, content, tried=True)
    return jsonify(
        {
            "ok": bool(content),
            "content": content,
            "cached": False,
            "reason": None if content else "正文提取失败（有些网站会拦截自动抓取）",
        }
    )


@app.post("/api/article/<int:article_id>/read")
def api_mark_read(article_id: int):
    payload = request.get_json(silent=True) or {}
    is_read = payload.get("is_read", True)
    ok = db.mark_read(article_id, bool(is_read))
    return jsonify({"ok": ok, "is_read": bool(is_read)})


@app.post("/api/article/<int:article_id>/star")
def api_toggle_star(article_id: int):
    state = db.toggle_star(article_id)
    if state is None:
        return jsonify({"ok": False, "error": "操作失败"}), 500
    return jsonify({"ok": True, "is_starred": state})


@app.delete("/api/article/<int:article_id>")
def api_delete_article(article_id: int):
    """删除一篇文章（软删除，源里再出现也不会重新冒出来）。"""
    if not db.get_article(article_id):
        return jsonify({"ok": False, "error": "文章不存在"}), 404
    ok = db.delete_article(article_id)
    return jsonify({"ok": ok, "counts": _counts()})


@app.post("/api/article/<int:article_id>/restore")
def api_restore_article(article_id: int):
    ok = db.restore_article(article_id)
    return jsonify({"ok": ok, "counts": _counts()})


@app.post("/api/article/<int:article_id>/folder")
def api_set_article_folder(article_id: int):
    payload = request.get_json(silent=True) or {}
    raw = payload.get("folder_id")
    folder_id = None
    if raw not in (None, "", "none", 0, "0"):
        try:
            folder_id = int(raw)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "收藏夹参数不对"}), 400
        if not db.get_folder(folder_id):
            return jsonify({"ok": False, "error": "收藏夹不存在"}), 404
    ok = db.set_article_folder(article_id, folder_id)
    folder = db.get_folder(folder_id) if folder_id else None
    return jsonify({
        "ok": ok,
        "folder_id": folder_id,
        "folder_name": folder["name"] if folder else None,
        "folder_color": folder["color"] if folder else None,
        "is_starred": True if folder_id else None,
    })


# --------------------------------------------------------------------------- #
# 收藏夹
# --------------------------------------------------------------------------- #
@app.post("/api/articles/batch")
def api_batch_articles():
    """
    多选后的批量操作。
    action: read / unread / star / unstar / folder / delete / restore
    """
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "")
    ids = payload.get("ids")

    if action not in {"read", "unread", "star", "unstar", "folder", "delete", "restore"}:
        return jsonify({"ok": False, "error": "不支持的批量操作"}), 400
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "还没有选中任何文章"}), 400
    if len(ids) > 1000:
        return jsonify({"ok": False, "error": "一次最多操作 1000 篇"}), 400

    folder_id = None
    if action == "folder":
        raw = payload.get("folder_id")
        if raw not in (None, "", "none", 0, "0"):
            try:
                folder_id = int(raw)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "收藏夹参数不对"}), 400
            if not db.get_folder(folder_id):
                return jsonify({"ok": False, "error": "收藏夹不存在"}), 404

    if action == "read":
        affected = db.batch_mark_read(ids, True)
    elif action == "unread":
        affected = db.batch_mark_read(ids, False)
    elif action == "star":
        affected = db.batch_star(ids, True)
    elif action == "unstar":
        affected = db.batch_star(ids, False)
    elif action == "folder":
        affected = db.batch_set_folder(ids, folder_id)
    elif action == "delete":
        affected = db.batch_delete(ids)
    else:
        affected = db.batch_restore(ids)

    return jsonify({
        "ok": True,
        "action": action,
        "affected": affected,
        "ids": ids,
        "counts": _counts(),
        "folders": db.get_folders(),
    })


# --------------------------------------------------------------------------- #
# 频道（把订阅源分组）
# --------------------------------------------------------------------------- #
@app.get("/api/channels")
def api_channels():
    return jsonify({"channels": db.get_channels(), "colors": db.FOLDER_COLORS})


@app.post("/api/channels")
def api_create_channel():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "给频道起个名字"}), 400
    color = (payload.get("color") or db.FOLDER_COLORS[0]).strip()
    feed_ids = payload.get("feed_ids") or []
    channel_id = db.create_channel(name, color, feed_ids)
    if not channel_id:
        return jsonify({"ok": False, "error": "创建失败"}), 500
    log.info("新建频道「%s」，包含 %s 个订阅源", name, len(feed_ids))
    return jsonify({"ok": True, "channel": db.get_channel(channel_id),
                    "channels": db.get_channels()})


@app.post("/api/channels/<int:channel_id>")
def api_update_channel(channel_id: int):
    payload = request.get_json(silent=True) or {}
    if not db.get_channel(channel_id):
        return jsonify({"ok": False, "error": "频道不存在"}), 404
    db.update_channel(channel_id, name=payload.get("name"), color=payload.get("color"))
    if "feed_ids" in payload:
        count = db.set_channel_feeds(channel_id, payload.get("feed_ids") or [])
        log.info("频道 %s 更新为 %s 个订阅源", channel_id, count)
    return jsonify({"ok": True, "channel": db.get_channel(channel_id),
                    "channels": db.get_channels()})


@app.delete("/api/channels/<int:channel_id>")
def api_delete_channel(channel_id: int):
    if not db.get_channel(channel_id):
        return jsonify({"ok": False, "error": "频道不存在"}), 404
    ok = db.delete_channel(channel_id)
    return jsonify({"ok": ok, "channels": db.get_channels(), "counts": _counts()})


@app.get("/api/folders")
def api_folders():
    return jsonify({"folders": db.get_folders(), "colors": db.FOLDER_COLORS})


@app.post("/api/folders")
def api_create_folder():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "给收藏夹起个名字"}), 400
    color = (payload.get("color") or db.FOLDER_COLORS[0]).strip()
    folder_id = db.create_folder(name, color)
    if not folder_id:
        return jsonify({"ok": False, "error": "创建失败"}), 500
    return jsonify({"ok": True, "folder": db.get_folder(folder_id),
                    "folders": db.get_folders()})


@app.post("/api/folders/<int:folder_id>")
def api_update_folder(folder_id: int):
    payload = request.get_json(silent=True) or {}
    if not db.get_folder(folder_id):
        return jsonify({"ok": False, "error": "收藏夹不存在"}), 404
    ok = db.update_folder(folder_id, name=payload.get("name"), color=payload.get("color"))
    return jsonify({"ok": ok, "folder": db.get_folder(folder_id),
                    "folders": db.get_folders()})


@app.delete("/api/folders/<int:folder_id>")
def api_delete_folder(folder_id: int):
    if not db.get_folder(folder_id):
        return jsonify({"ok": False, "error": "收藏夹不存在"}), 404
    ok = db.delete_folder(folder_id)
    return jsonify({"ok": ok, "folders": db.get_folders(), "counts": _counts()})


@app.post("/api/read/all")
def api_read_all():
    count = db.mark_all_read()
    return jsonify({"ok": True, "count": count, "counts": _counts()})


# --------------------------------------------------------------------------- #
# 订阅源 API
# --------------------------------------------------------------------------- #
@app.get("/api/feeds")
def api_feeds():
    return jsonify({"feeds": db.get_feeds()})


@app.get("/api/presets")
def api_presets():
    return jsonify({"presets": rsshub.presets_for_ui()})


@app.post("/api/feeds/refresh")
def api_refresh_all():
    result = start_refresh()
    return jsonify(result)


@app.post("/api/feed/<int:feed_id>/refresh")
def api_refresh_one(feed_id: int):
    if not db.get_feed(feed_id):
        return jsonify({"error": "订阅不存在"}), 404
    return jsonify(start_refresh([feed_id]))


@app.get("/api/refresh/status")
def api_refresh_status():
    with _refresh_lock:
        state = dict(REFRESH)
    state["feeds"] = db.get_feeds()
    return jsonify(state)


@app.delete("/api/feed/<int:feed_id>")
def api_delete_feed(feed_id: int):
    ok = db.delete_feed(feed_id)
    if not ok:
        return jsonify({"ok": False, "error": "订阅不存在"}), 404
    return jsonify({"ok": True, "counts": _counts()})


@app.post("/api/feed/<int:feed_id>/toggle")
def api_toggle_feed(feed_id: int):
    payload = request.get_json(silent=True) or {}
    active = bool(payload.get("is_active", True))
    ok = db.set_feed_active(feed_id, active)
    return jsonify({"ok": ok, "is_active": active})


# --------------------------------------------------------------------------- #
# 搜索即订阅 / 添加订阅
# --------------------------------------------------------------------------- #
@app.post("/api/search")
def api_search():
    payload = request.get_json(silent=True) or {}
    query = (payload.get("q") or payload.get("query") or "").strip()
    if not query:
        return jsonify({"candidates": [], "hint": "输入博主名、平台名或订阅地址"})
    try:
        result = rsshub.search(query)
    except Exception as exc:  # noqa: BLE001
        log.exception("搜索失败：%s", exc)
        return jsonify({"candidates": [], "hint": "搜索出错了，稍后再试或直接粘贴订阅地址"})
    # 标记已经订阅过的候选，前端好提示
    existing = {f["feed_url"] for f in db.get_feeds()}
    for c in result.get("candidates", []):
        c["subscribed"] = bool(c.get("feed_url") and c["feed_url"] in existing)
    return jsonify(result)


def _add_feed_from_url(url: str) -> tuple[Optional[dict], Optional[str]]:
    """
    粘贴链接添加。优先级：
      1. 平台主页识别（B站空间 / 知乎 / 微博 / 少数派…）
      2. 这个地址本身就是订阅地址 → 直接校验
      3. 普通网页 → 自动发现里面的订阅地址（每个候选都必须真实解析成功）
    """
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None, "只能添加 http 或 https 开头的地址"

    candidate = rsshub.resolve_url(url)
    if candidate:
        try:
            info = rsshub.candidate_to_feed(candidate)
        except rsshub.ResolverError as exc:
            return None, str(exc)
        info["description"] = candidate.get("detail")
        return info, None

    probe = feed_parser.probe_feed_url(url)
    if probe["ok"] and probe.get("entry_count"):
        return (
            {
                "feed_url": url,
                "title": probe.get("title") or url,
                "site_url": probe.get("site_url"),
                "icon": probe.get("icon"),
                "description": probe.get("description"),
                "platform": None,
            },
            None,
        )

    # 走到这里说明它是个普通网页（或抓不到内容）—— 试着从页面里找订阅地址
    found = feed_parser.discover_feeds(url)
    if found:
        best = found[0]
        log.info("从 %s 自动发现订阅地址：%s", url, best["feed_url"])
        return (
            {
                "feed_url": best["feed_url"],
                "title": best.get("title") or url,
                "site_url": best.get("site_url") or url,
                "icon": best.get("icon"),
                "description": best.get("description"),
                "platform": None,
            },
            None,
        )
    return None, "这个网页里没找到订阅地址。看看页面底部有没有 RSS / 订阅 链接"


@app.post("/api/feed")
def api_add_feed():
    """
    添加订阅。接受三种请求体：
      {"url": "..."}                        粘贴链接 / 平台主页
      {"feed_url": ..., "title": ...}       搜索候选（已解析）
      {"preset": "sspai"}                   预置推荐源
    """
    payload = request.get_json(silent=True) or {}

    if payload.get("preset"):
        preset = rsshub.get_preset(str(payload["preset"]))
        if not preset:
            return jsonify({"ok": False, "error": "没有这个推荐源"}), 400
        candidate = rsshub._preset_candidate(preset)  # noqa: SLF001
        try:
            info = rsshub.candidate_to_feed(candidate)
        except rsshub.ResolverError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503
        info["description"] = preset.get("desc")
    elif payload.get("url"):
        info, error = _add_feed_from_url(str(payload["url"]))
        if error:
            return jsonify({"ok": False, "error": error}), 400
    elif payload.get("feed_url") or payload.get("route"):
        try:
            info = rsshub.candidate_to_feed(payload)
        except rsshub.ResolverError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503
    else:
        return jsonify({"ok": False, "error": "没有提供订阅地址"}), 400

    assert info is not None

    # 需要登录信息的源，提前说清楚，别让用户加进去之后一直抓取失败
    requirement = _cookie_requirement(info["feed_url"])
    if requirement:
        return jsonify({"ok": False, "error": requirement}), 400

    feed_id, created = db.add_feed(
        info["feed_url"],
        info.get("title") or info["feed_url"],
        site_url=info.get("site_url"),
        description=info.get("description"),
        icon=info.get("icon"),
        platform=info.get("platform"),
    )
    if not feed_id:
        return jsonify({"ok": False, "error": "添加失败，请稍后再试"}), 500

    # 新增的源立刻抓一次，用户马上就能看到文章
    if created:
        start_refresh([feed_id])

    return jsonify(
        {
            "ok": True,
            "created": created,
            "feed_id": feed_id,
            "title": info.get("title"),
            "message": "已添加" if created else "这个源已经在订阅列表里了",
        }
    )


# --------------------------------------------------------------------------- #
# 设置 / 实例 / OPML / 数据管理
# --------------------------------------------------------------------------- #
EDITABLE_SETTINGS = {
    "theme", "view_mode", "filter_keywords", "block_images",
    "rsshub_instance", "auto_refresh_minutes", "page_size", "sidebar_open",
}


def _public_settings() -> dict:
    """
    给前端的设置。登录信息一律脱敏：只告诉"有没有配置、多少字符、结尾几位"，
    绝不把 Cookie 原样回传（否则它会出现在浏览器的网络面板和各种截图里）。
    """
    settings = db.get_settings()
    for key in cookies.PLATFORM_KEYS.values():
        settings.pop(key, None)
    settings["cookies"] = {
        platform: cookies.mask(db.get_setting(key))
        for platform, key in cookies.PLATFORM_KEYS.items()
    }
    return settings


@app.get("/api/settings")
def api_get_settings():
    return jsonify({"settings": _public_settings(), "db_path": str(db.DB_PATH)})


@app.post("/api/settings")
def api_set_settings():
    payload = request.get_json(silent=True) or {}
    updated = {}
    messages = {}
    for key, value in payload.items():
        # 登录信息走专门的解析（支持粘贴 cURL）/ 脱敏逻辑
        if key in cookies.PLATFORM_KEYS.values():
            platform = next(p for p, k in cookies.PLATFORM_KEYS.items() if k == key)
            ok, message = cookies.set_cookie(platform, str(value or ""))
            messages[platform] = {"ok": ok, "message": message}
            continue
        if key not in EDITABLE_SETTINGS:
            continue
        if key == "filter_keywords" and isinstance(value, list):
            import json

            value = json.dumps(value, ensure_ascii=False)
        db.set_setting(key, value)
        updated[key] = value
    return jsonify(
        {
            "ok": True,
            "updated": updated,
            "messages": messages,
            "settings": _public_settings(),
        }
    )


@app.post("/api/cookies/test")
def api_test_cookie():
    payload = request.get_json(silent=True) or {}
    platform = str(payload.get("platform") or "")
    if platform not in cookies.PLATFORM_KEYS:
        return jsonify({"ok": False, "message": "不支持的平台"}), 400
    result = cookies.test_cookie(platform)
    log.info("测试 %s 登录信息：%s", platform, result.get("message"))
    return jsonify(result)


@app.post("/api/cookies/clear")
def api_clear_cookie():
    payload = request.get_json(silent=True) or {}
    platform = str(payload.get("platform") or "")
    if platform not in cookies.PLATFORM_KEYS:
        return jsonify({"ok": False, "message": "不支持的平台"}), 400
    cookies.set_cookie(platform, "")
    return jsonify({"ok": True, "message": "已清除", "settings": _public_settings()})


@app.post("/api/instances/probe")
def api_probe_instances():
    return jsonify({"instances": rsshub.probe_instances()})


@app.get("/api/instances")
def api_get_instance():
    return jsonify(
        {
            "configured": db.get_setting("rsshub_instance") or "",
            "auto": db.get_setting("rsshub_instance_auto") or "",
            "candidates": rsshub.INSTANCE_CANDIDATES,
        }
    )


def _opml_escape(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@app.get("/api/opml/export")
def api_opml_export():
    feeds = db.get_feeds()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        "    <title>白读 · ByRead 订阅列表</title>",
        f"    <dateCreated>{db.utc_now_iso()}</dateCreated>",
        "  </head>",
        "  <body>",
    ]
    for f in feeds:
        lines.append(
            f'    <outline type="rss" text="{_opml_escape(f.get("title"))}" '
            f'title="{_opml_escape(f.get("title"))}" '
            f'xmlUrl="{_opml_escape(f.get("feed_url"))}" '
            f'htmlUrl="{_opml_escape(f.get("site_url") or "")}"/>'
        )
    lines += ["  </body>", "</opml>"]
    xml = "\n".join(lines)
    return Response(
        xml,
        mimetype="text/xml",
        headers={
            "Content-Disposition": 'attachment; filename="byread-subscriptions.opml"'
        },
    )


@app.post("/api/opml/import")
def api_opml_import():
    raw = None
    if "file" in request.files:
        raw = request.files["file"].read()
    else:
        payload = request.get_json(silent=True) or {}
        if payload.get("content"):
            raw = str(payload["content"]).encode("utf-8")
    if not raw:
        return jsonify({"ok": False, "error": "没有收到文件内容"}), 400

    try:
        root = ET.fromstring(raw)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"OPML 解析失败：{exc}"}), 400

    added, skipped = 0, 0
    new_ids = []
    for outline in root.iter("outline"):
        xml_url = (outline.get("xmlUrl") or "").strip()
        if not xml_url.lower().startswith(("http://", "https://")):
            continue
        title = (outline.get("title") or outline.get("text") or xml_url).strip()
        feed_id, created = db.add_feed(
            xml_url, title, site_url=(outline.get("htmlUrl") or None)
        )
        if created and feed_id:
            added += 1
            new_ids.append(feed_id)
        else:
            skipped += 1

    if new_ids:
        start_refresh(new_ids[:30])  # 限制一次导入的抓取量，避免长时间占用
    return jsonify({"ok": True, "added": added, "skipped": skipped})


@app.delete("/api/articles/clear")
def api_clear_articles():
    count = db.clear_articles()
    return jsonify({"ok": True, "count": count, "counts": _counts()})


# --------------------------------------------------------------------------- #
# 数据导出（自用保险）
#
# 红线：**导出里绝不能有登录信息**。做法有两层 ——
#   1. db.EXPORT_TABLES 是显式白名单，settings 表（Cookie 就在里面）压根不在其中；
#   2. 出口处再实检一次：拿 settings 里两个 Cookie 的真实值在产物里搜一遍，命中就拒绝导出。
# 第 2 层是"不信任自己"的保险 —— 万一以后有人往白名单里加了带 Cookie 的表，这里会拦住。
# --------------------------------------------------------------------------- #
# 只有够长的值才拿来判定：Cookie 里还夹着 ariaDefaultTheme=default 这种通用词（7 字符），
# 拿它搜产物必然误报 —— 实测正文里一张图叫 `..._default.png` 就撞上了。
# 真正的登录凭证（z_c0 / SUB / SUBP / WBPSESS / SCF / _xsrf / _zap …）都在 24 字符以上。
_MIN_SECRET_LEN = 16


def _leaked_secret(text: str) -> Optional[str]:
    """
    产物里是否出现了登录信息。返回命中的说明，没命中返回 None。

    先看整条 Cookie 串，再按 `;` 拆成 `name=value` 逐个 value 比对（只比长度 ≥16 的），
    这样即使 Cookie 被换了顺序、加了空格也能查出来。
    """
    for key in ("zhihu_cookie", "weibo_cookie"):
        raw = (db.get_setting(key) or "").strip()
        if not raw:
            continue
        if raw in text:
            return f"{key} 的完整内容"
        for piece in re.split(r"[;\s]+", raw):
            if "=" not in piece:
                continue
            name, value = piece.split("=", 1)
            value = value.strip()
            if len(value) >= _MIN_SECRET_LEN and value in text:
                return f"{key} 里 {name.strip()} 的值"
    return None


def _export_blocked(what: str, leaked: str):
    """检测到登录信息就不给下载（500 + 日志留痕），绝不"先给出去再说"。"""
    log.error("【红线】导出被拦截：%s 里检测到 %s —— 导出白名单可能被改坏了", what, leaked)
    return jsonify({
        "ok": False,
        "error": "导出被安全拦截：产物里检测到登录信息（已记录日志），已中止下载",
    }), 500


@app.get("/api/export/json")
def api_export_json():
    """
    导出全部数据：订阅源 / 文章 / 已读星标 / 收藏夹 / 频道（含频道与订阅源的对应关系）。
    **不含 settings**，所以不含任何登录信息。文件可以直接留着当备份。
    """
    payload = {
        "format": "byread-backup",
        "version": 1,
        "app": "白读 · ByRead",
        "exported_at": db.utc_now_iso(),
        "note": "只包含数据表；登录信息（Cookie）不在其中，这是有意的设计。",
        "tables": db.export_tables(),
    }
    body = json.dumps(payload, ensure_ascii=False, indent=1)
    leaked = _leaked_secret(body)
    if leaked:
        return _export_blocked("JSON 导出", leaked)
    stats = {name: len(rows) for name, rows in payload["tables"].items()}
    log.info("导出 JSON：%s（%d 字节）", stats, len(body.encode("utf-8")))
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="byread-data-{time.strftime("%Y%m%d-%H%M")}.json"'},
    )


@app.get("/api/export/markdown")
def api_export_markdown():
    """
    导出全部文章为 Markdown 压缩包：每篇文章一个 .md
    （标题 / 来源 / 作者 / 时间 / 原文链接 / 收藏夹 / 正文）。
    软删除的文章不导出（那是用户自己删掉的）。
    """
    articles = db.get_articles_for_export()
    blob = exporter.build_markdown_zip(articles)

    # 压缩包里是 deflate 数据，不能只搜整包字节 —— 得逐篇解开来看
    leaked = None
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for name in zf.namelist():
                text = zf.read(name).decode("utf-8", "ignore")
                leaked = _leaked_secret(text)
                if leaked:
                    break
    except Exception as exc:  # noqa: BLE001
        log.error("导出 zip 自检失败：%s", exc)
    if leaked:
        return _export_blocked("Markdown 导出", leaked)

    log.info("导出 Markdown：%d 篇，%d 字节", len(articles), len(blob))
    return Response(
        blob,
        mimetype="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="byread-articles-{time.strftime("%Y%m%d-%H%M")}.zip"'},
    )


# --------------------------------------------------------------------------- #
# 图片代理
# --------------------------------------------------------------------------- #
# 为什么需要它：
#   微博图床（sinaimg.cn）和 B 站图床（hdslb.com）都有**防盗链**：带站点自己的 Referer
#   请求返回 200，而浏览器从 127.0.0.1 请求会被判 403。实测结果：
#       i2.hdslb.com   localhost=403  站点自身=200
#       tvax1.sinaimg.cn localhost=403 站点自身=200
#       picx.zhimg.com  localhost=200（知乎不拦）
#   结果是头像和文章配图在本地阅读器里全都显示不出来（浏览器里就是一个碎图标）。
#
#   所以本地做一层代理：服务端带着正确的 Referer 取图，再回给浏览器。
#   只允许已知图床域名（同时也就挡住了把接口当内网扫描器用的可能），限制大小并做内存缓存。
IMAGE_PROXY_HOSTS = ("hdslb.com", "sinaimg.cn", "zhimg.com", "gcores.com")
IMAGE_REFERERS = {
    "hdslb.com": "https://www.bilibili.com/",
    "sinaimg.cn": "https://m.weibo.cn/",
    "zhimg.com": "https://www.zhihu.com/",
    "gcores.com": "https://www.gcores.com/",
}
IMAGE_TTL = 6 * 3600
IMAGE_CACHE_MAX = 300
IMAGE_MAX_BYTES = 3 * 1024 * 1024

_image_cache: dict[str, tuple[bytes, str, float]] = {}
_image_lock = threading.Lock()


def _image_host_allowed(host: str) -> bool:
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) for h in IMAGE_PROXY_HOSTS)


@app.get("/api/image")
def api_image_proxy():
    url = (request.args.get("u") or "").strip()
    parsed = urlparse(url)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or not _image_host_allowed(parsed.hostname or "")):
        return Response(status=403)

    now = time.time()
    with _image_lock:
        hit = _image_cache.get(url)
        if hit and hit[2] > now:
            return Response(hit[0], mimetype=hit[1],
                            headers={"Cache-Control": "public, max-age=86400"})

    host = (parsed.hostname or "").lower()
    referer = next((v for k, v in IMAGE_REFERERS.items() if host.endswith(k)), None)
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer

    try:
        resp = requests.get(url, timeout=8, headers=headers, stream=True)
        if resp.status_code >= 400:
            log.info("代理取图失败 HTTP %s：%s", resp.status_code, host)
            return Response(status=404)
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            return Response(status=404)
        data = resp.raw.read(IMAGE_MAX_BYTES + 1, decode_content=True)
        if not data or len(data) > IMAGE_MAX_BYTES:
            return Response(status=413)
    except Exception as exc:  # noqa: BLE001
        log.info("代理取图异常 %s：%s", host, exc)
        return Response(status=404)

    with _image_lock:
        if len(_image_cache) > IMAGE_CACHE_MAX:
            _image_cache.clear()
        _image_cache[url] = (data, ctype, now + IMAGE_TTL)
    return Response(data, mimetype=ctype,
                    headers={"Cache-Control": "public, max-age=86400"})


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    bootstrap()
    threading.Thread(target=_scheduler_loop, name="byread-scheduler", daemon=True).start()
    log.info("白读已启动 → http://127.0.0.1:5000")
    # 关闭 reloader：避免调度线程被重复启动
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
