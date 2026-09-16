"""
db.py —— 白读 · ByRead 数据层

设计要点（与原需求文档的差异，均为修正项）：
1. articles 的唯一约束是 (feed_id, guid) 而不是全局 guid。
   原文档的 `guid TEXT UNIQUE` 会导致跨源同 guid 误吞文章；
   更严重的是很多源不提供 guid（空串），全局唯一会让第一篇之后的文章全部静默消失。
2. published 一律存 UTC ISO8601（形如 2026-09-07T10:30:00Z），字符串排序即时间排序。
   published 缺失时回退为抓取时间，保证列表永远有稳定的排序键（不会出现 NULL 排序问题）。
3. 所有对外函数都自带异常处理，任何数据库异常都不会冒泡成 500。
4. 每次调用新建连接（线程安全），WAL 模式，开启外键级联。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("byread.db")

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
DB_PATH = INSTANCE_DIR / "bai_read.db"

# 默认设置：首次启动写入 settings 表
# 正文提取逻辑的版本号。**改动解析方式（比如新支持了某种图片写法、换了解析器）就 +1**，
# 已入库的老文章会在下次刷新时自动按新逻辑重取一遍正文。
# 用版本号而不是"看正文长度/有没有图"来判断，是因为后者既会漏（有正文但内容过时）
# 又会反复触发（正文本来就没图的内容每次刷新都白跑一次请求）。
CONTENT_VERSION = 2

DEFAULT_SETTINGS: dict[str, str] = {
    "theme": "light",                       # light / dark
    "view_mode": "card",                    # card / list
    "filter_keywords": "[]",                # JSON 数组，标题命中即隐藏
    "block_images": "false",                # 阅读页是否屏蔽图片（隐私模式）
    "rsshub_instance": "",                  # 留空 = 自动探测可用实例
    "auto_refresh_minutes": "30",           # 0 = 关闭自动刷新
    "page_size": "30",
    "sidebar_open": "true",                 # 侧边栏展开 / 收起
    # 登录信息（敏感，接口一律脱敏返回，只存本地）
    "zhihu_cookie": "",
    "weibo_cookie": "",
}

_init_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def utc_now_iso() -> str:
    """当前 UTC 时间，ISO8601（秒级，Z 结尾）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso(value: Any) -> str:
    """把 datetime / struct_time / 时间戳 统一转成 UTC ISO8601 字符串。"""
    try:
        if value is None:
            return utc_now_iso()
        if isinstance(value, str):
            text = value.strip()
            # 有些接口把时间戳放在字符串里（B 站的 pub_ts 就是），要认出来
            if re.fullmatch(r"\d{9,13}(\.\d+)?", text):
                return datetime.fromtimestamp(float(text), timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            # 各种 ISO8601（含 +08:00 这种带时区的）统一归一化成 UTC，
            # 否则字符串排序会错乱：'...T22:48:28+08:00' 排在 '...T14:48:28Z' 后面，
            # 但它其实是同一时刻 —— 列表顺序就乱了
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                return text
        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        # time.struct_time（feedparser 的 published_parsed，本身已是 UTC）
        if hasattr(value, "tm_year"):
            import calendar

            return datetime.fromtimestamp(calendar.timegm(value), timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("时间转换失败 %r: %s", value, exc)
    return utc_now_iso()


def make_guid(*parts: Any) -> str:
    """guid 缺失时的兜底标识：对关键字段做 sha1。"""
    raw = "|".join("" if p is None else str(p) for p in parts)
    return "sha1:" + hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()


@contextmanager
def get_conn():
    """每次调用一个连接；出错自动回滚，退出自动关闭。"""
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _row(cur) -> Optional[dict]:
    r = cur.fetchone()
    return dict(r) if r else None


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_url      TEXT UNIQUE NOT NULL,
    site_url      TEXT,
    title         TEXT NOT NULL,
    description   TEXT,
    icon          TEXT,
    platform      TEXT,
    is_active     INTEGER DEFAULT 1,
    error_count   INTEGER DEFAULT 0,
    last_error    TEXT,
    last_fetched  TEXT,
    created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS articles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id     INTEGER NOT NULL,
    guid        TEXT NOT NULL,
    title       TEXT,
    summary     TEXT,
    content     TEXT,
    link        TEXT,
    author      TEXT,
    published   TEXT NOT NULL,
    fetched_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    content_tried INTEGER DEFAULT 0,
    content_v   INTEGER DEFAULT 0,
    FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE,
    UNIQUE (feed_id, guid)
);

CREATE TABLE IF NOT EXISTS user_actions (
    article_id  INTEGER PRIMARY KEY,
    is_read     INTEGER DEFAULT 0,
    is_starred  INTEGER DEFAULT 0,
    folder_id   INTEGER,
    read_at     TEXT,
    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
    FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE SET NULL
);

-- 收藏夹：名字 + 颜色由用户自定义
CREATE TABLE IF NOT EXISTS folders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    color       TEXT DEFAULT '#007AFF',
    sort_order  INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# 索引单独放一份，必须在 _ensure_columns() **之后**执行：
# 老库升级时新列还不存在，先建索引会直接报 "no such column" 并让整个建库流程中断
# （实测踩过：整个界面会变成 0 篇，因为所有查询都在报错，而异常被 try/except 吞掉了）
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_feeds_active        ON feeds(is_active);
CREATE INDEX IF NOT EXISTS idx_articles_feed_id    ON articles(feed_id);
CREATE INDEX IF NOT EXISTS idx_articles_published  ON articles(published DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_articles_link       ON articles(link);
CREATE INDEX IF NOT EXISTS idx_user_actions_read   ON user_actions(is_read);
CREATE INDEX IF NOT EXISTS idx_user_actions_star   ON user_actions(is_starred);
CREATE INDEX IF NOT EXISTS idx_user_actions_folder ON user_actions(folder_id);
"""

# 老库升级用：列不存在时才补
MIGRATIONS: list[tuple[str, str, str]] = [
    ("articles", "is_deleted", "is_deleted INTEGER DEFAULT 0"),
    ("articles", "content_v", "content_v INTEGER DEFAULT 0"),
    ("user_actions", "folder_id", "folder_id INTEGER"),
]

# 收藏夹可选配色（挑的都是深浅色模式下都能看清的）
FOLDER_COLORS = [
    "#007AFF", "#34C759", "#FF9500", "#FF3B30", "#AF52DE",
    "#5856D6", "#00C7BE", "#FF2D55", "#A2845E", "#8E8E93",
]


def _ensure_columns(conn) -> None:
    """给已存在的库补列（SQLite 没有 ADD COLUMN IF NOT EXISTS）。"""
    for table, column, ddl in MIGRATIONS:
        try:
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                log.info("数据库迁移：%s 增加列 %s", table, column)
        except Exception as exc:  # noqa: BLE001
            log.error("迁移 %s.%s 失败：%s", table, column, exc)


def init_db() -> None:
    """建库建表 + 写入默认设置 + 轻量数据迁移。可重复调用。"""
    with _init_lock:
        try:
            INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
            with get_conn() as conn:
                conn.executescript(SCHEMA)      # 1. 建表
                _ensure_columns(conn)           # 2. 给老库补列
                conn.executescript(INDEXES)     # 3. 建索引（可能依赖新列，必须在补列之后）
                for k, v in DEFAULT_SETTINGS.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v)
                    )
                # 迁移：微博头像地址带着会过期的签名（?KID=...&Expires=...&ssig=...），
                # 存下来过一两天就失效。去掉查询参数即可长期有效。
                conn.execute(
                    "UPDATE feeds SET icon = substr(icon, 1, instr(icon, '?') - 1) "
                    "WHERE icon LIKE '%sinaimg%?%'"
                )
            log.info("数据库就绪：%s", DB_PATH)
        except Exception as exc:  # noqa: BLE001
            log.error("数据库初始化失败：%s", exc)


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    try:
        with get_conn() as conn:
            row = _row(conn.execute("SELECT value FROM settings WHERE key = ?", (key,)))
        if row is None:
            return DEFAULT_SETTINGS.get(key, default)
        return row["value"]
    except Exception as exc:  # noqa: BLE001
        log.error("读取设置 %s 失败：%s", key, exc)
        return DEFAULT_SETTINGS.get(key, default)


def get_settings() -> dict[str, str]:
    result = dict(DEFAULT_SETTINGS)
    try:
        with get_conn() as conn:
            for row in _rows(conn.execute("SELECT key, value FROM settings")):
                result[row["key"]] = row["value"]
    except Exception as exc:  # noqa: BLE001
        log.error("读取全部设置失败：%s", exc)
    return result


def set_setting(key: str, value: Any) -> bool:
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, "" if value is None else str(value)),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("写入设置 %s 失败：%s", key, exc)
        return False


def get_filter_keywords() -> list[str]:
    """关键词过滤列表。"""
    try:
        raw = get_setting("filter_keywords") or "[]"
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception as exc:  # noqa: BLE001
        log.warning("关键词解析失败：%s", exc)
    return []


# --------------------------------------------------------------------------- #
# feeds
# --------------------------------------------------------------------------- #
def add_feed(
    feed_url: str,
    title: str,
    site_url: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    platform: Optional[str] = None,
) -> tuple[Optional[int], bool]:
    """添加订阅。返回 (feed_id, 是否新建)。url 已存在时返回已存在的 id。"""
    try:
        with get_conn() as conn:
            existing = _row(
                conn.execute("SELECT id FROM feeds WHERE feed_url = ?", (feed_url,))
            )
            if existing:
                return existing["id"], False
            cur = conn.execute(
                "INSERT INTO feeds(feed_url, site_url, title, description, icon, platform) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (feed_url, site_url, title or feed_url, description, icon, platform),
            )
            return cur.lastrowid, True
    except Exception as exc:  # noqa: BLE001
        log.error("添加订阅失败 %s：%s", feed_url, exc)
        return None, False


def get_feeds(active_only: bool = False) -> list[dict]:
    try:
        sql = (
            "SELECT f.*, "
            "  (SELECT COUNT(*) FROM articles a WHERE a.feed_id = f.id) AS article_count, "
            "  (SELECT COUNT(*) FROM articles a "
            "     LEFT JOIN user_actions ua ON ua.article_id = a.id "
            "     WHERE a.feed_id = f.id AND COALESCE(ua.is_read, 0) = 0) AS unread_count "
            "FROM feeds f"
        )
        if active_only:
            sql += " WHERE f.is_active = 1"
        sql += " ORDER BY f.is_active DESC, f.title COLLATE NOCASE"
        with get_conn() as conn:
            return _rows(conn.execute(sql))
    except Exception as exc:  # noqa: BLE001
        log.error("读取订阅列表失败：%s", exc)
        return []


def get_feed(feed_id: int) -> Optional[dict]:
    try:
        with get_conn() as conn:
            return _row(conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)))
    except Exception as exc:  # noqa: BLE001
        log.error("读取订阅 %s 失败：%s", feed_id, exc)
        return None


def delete_feed(feed_id: int) -> bool:
    """删除订阅（articles / user_actions 由外键级联删除）。"""
    try:
        with get_conn() as conn:
            cur = conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.error("删除订阅 %s 失败：%s", feed_id, exc)
        return False


def update_feed_meta(
    feed_id: int,
    title: Optional[str] = None,
    site_url: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
) -> None:
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE feeds SET title = COALESCE(?, title), "
                "  site_url = COALESCE(?, site_url), "
                "  description = COALESCE(?, description), "
                "  icon = COALESCE(?, icon) WHERE id = ?",
                (title, site_url, description, icon, feed_id),
            )
    except Exception as exc:  # noqa: BLE001
        log.error("更新订阅信息 %s 失败：%s", feed_id, exc)


def mark_feed_success(feed_id: int, article_count: int = 0) -> None:
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE feeds SET error_count = 0, is_active = 1, last_error = NULL, "
                "last_fetched = ? WHERE id = ?",
                (utc_now_iso(), feed_id),
            )
    except Exception as exc:  # noqa: BLE001
        log.error("更新订阅成功状态 %s 失败：%s", feed_id, exc)


def mark_feed_failure(feed_id: int, error: str, count_toward_pause: bool = True,
                      max_errors: int = 3) -> dict:
    """
    记录一次抓取失败。

    count_toward_pause=False 用于**临时性失败**（限流、软封、登录态过期）：
    既不计入"连续失败"，也不会把源暂停 —— 否则微博那种十几分钟就恢复的限流，
    会在自动刷新下攒够 3 次把源永久暂停，之后再也等不到自愈。
    """
    try:
        with get_conn() as conn:
            row = _row(conn.execute("SELECT error_count FROM feeds WHERE id = ?", (feed_id,)))
            count = row["error_count"] if row else 0
            if count_toward_pause:
                count += 1
                is_active = 0 if count >= max_errors else 1
            else:
                is_active = 1        # 不是源的错，保持可用（也顺带让被误暂停的源自愈）
            conn.execute(
                "UPDATE feeds SET error_count = ?, is_active = ?, last_error = ?, "
                "last_fetched = ? WHERE id = ?",
                (count, is_active, (error or "")[:500], utc_now_iso(), feed_id),
            )
            return {"error_count": count, "is_active": is_active, "temporary": not count_toward_pause}
    except Exception as exc:  # noqa: BLE001
        log.error("更新订阅失败状态 %s 失败：%s", feed_id, exc)
        return {"error_count": 0, "is_active": 1}


def set_feed_active(feed_id: int, active: bool) -> bool:
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE feeds SET is_active = ?, error_count = CASE WHEN ? THEN 0 ELSE error_count END "
                "WHERE id = ?",
                (1 if active else 0, 1 if active else 0, feed_id),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("切换订阅状态 %s 失败：%s", feed_id, exc)
        return False


# --------------------------------------------------------------------------- #
# articles
# --------------------------------------------------------------------------- #
def insert_article(feed_id: int, item: dict) -> bool:
    """
    插入一篇文章。返回 True 表示确实是新文章（用于"已更新 N 篇"的统计）。
    依赖 UNIQUE(feed_id, guid) + INSERT OR IGNORE 实现去重。
    """
    try:
        with get_conn() as conn:
            content = item.get("content")
            cur = conn.execute(
                "INSERT OR IGNORE INTO articles"
                "(feed_id, guid, title, summary, content, link, author, published, content_v) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    feed_id,
                    item.get("guid") or make_guid(feed_id, item.get("link"), item.get("title")),
                    (item.get("title") or "")[:500],
                    item.get("summary"),
                    content,
                    item.get("link"),
                    item.get("author"),
                    to_iso(item.get("published")),
                    # 入库时正文就是用当前版本的解析逻辑生成的
                    CONTENT_VERSION if content else 0,
                ),
            )
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.error("插入文章失败 feed=%s %s：%s", feed_id, item.get("title"), exc)
        return False


def update_article_if_incomplete(feed_id: int, item: dict) -> bool:
    """
    给已存在的文章"补课"。返回 True 表示确实改动了。

    什么时候补：
      1. 之前没正文、现在抓到了 —— 本地源的正文是一次一批补的（每次刷新有配额），
         补到的那批文章其实已经在库，`INSERT OR IGNORE` 会直接忽略，正文永远进不去。
      2. **新抓到的东西明显更完整**：现在有配图而库里没有，或正文长出一大截。
         没有这条的话，抓取逻辑的改进永远追不到已经入库的文章上
         （实测：B站图文补上图片后，老文章依然是没图的旧内容）。
      3. 标题是脏的：带 HTML 标签（搜索结果里的 `<em>` 高亮）或结构化数据的字符串残留。
    """
    try:
        guid = item.get("guid") or make_guid(feed_id, item.get("link"), item.get("title"))
        new_content = item.get("content") or ""
        new_title = (item.get("title") or "")[:500]
        new_summary = item.get("summary") or ""
        new_link = (item.get("link") or "").strip()

        with get_conn() as conn:
            row = _row(conn.execute(
                "SELECT content, title, summary, link, COALESCE(content_v, 0) AS v "
                "FROM articles "
                "WHERE feed_id = ? AND guid = ? AND COALESCE(is_deleted, 0) = 0",
                (feed_id, guid),
            ))
            if not row:
                return False

            old_content = row["content"] or ""
            old_title = row["title"] or ""
            old_summary = row["summary"] or ""
            old_link = row["link"] or ""
            old_v = int(row["v"] or 0)

            has_new_img = "<img" in new_content
            # 三个条件都必须先满足"值真的变了"，否则会出现每次刷新都重复写入同一份内容
            # （实测：标题里含 "<" 的文章，因为脏标题判定只看旧值，导致每轮都算一次"补齐"）
            set_content = bool(new_content) and new_content != old_content and (
                not old_content
                or ("<img" not in old_content and has_new_img)
                or len(new_content) > len(old_content) + 500
            )
            set_title = bool(new_title) and new_title != old_title and (
                not old_title or "<" in old_title or "{" in old_title
            )
            set_summary = (bool(new_summary) and new_summary != old_summary
                           and not old_summary.strip())
            # 链接也可能被"改对"：例如知乎想法原本指向想法页，取到被分享的回答后
            # 应该指向回答页（正文就是从那里来的）
            set_link = bool(new_link) and new_link != old_link

            if not (set_content or set_title or set_summary or set_link):
                # 内容确实没变化。但如果正文是用旧版逻辑取的，要把版本号推进到当前版本 ——
                # 否则"内容过时"这个判断永远成立，每次刷新都会重复请求同一份内容。
                # 这里只改一个整数列，代价极小，而且不计入"补齐 N 篇"的提示。
                if new_content and old_v < CONTENT_VERSION:
                    conn.execute(
                        "UPDATE articles SET content_v = ? WHERE feed_id = ? AND guid = ?",
                        (CONTENT_VERSION, feed_id, guid),
                    )
                return False

            # 只更新真正要改的列，避免把几十 KB 的正文白白重写一遍
            sets, params = [], []
            if set_content:
                sets.append("content = ?")
                params.append(new_content)
                sets.append("content_v = ?")       # 记下这份正文是用哪个版本的逻辑取的
                params.append(CONTENT_VERSION)
            if set_title:
                sets.append("title = ?")
                params.append(new_title)
            if set_summary:
                sets.append("summary = ?")
                params.append(new_summary)
            if set_link:
                sets.append("link = ?")
                params.append(new_link)
            params.extend([feed_id, guid])

            conn.execute(
                f"UPDATE articles SET {', '.join(sets)} WHERE feed_id = ? AND guid = ?",
                params,
            )
            return True
    except Exception as exc:  # noqa: BLE001
        log.error("补齐文章失败 feed=%s %s：%s", feed_id, item.get("title"), exc)
        return False


def _build_filters(
    view: str = "all",
    feed_id: Optional[int] = None,
    keywords: Optional[Iterable[str]] = None,
    query: Optional[str] = None,
    folder: Optional[str] = None,
) -> tuple[str, list]:
    """
    构造 WHERE 子句。
    keywords 命中标题的文章会被隐藏（在 SQL 层过滤，保证分页计数正确）。
    query   是用户主动搜索的词，跨 标题/摘要/正文 匹配。
    folder  取 "none"（未分类）或收藏夹 id。
    """
    where: list[str] = ["COALESCE(a.is_deleted, 0) = 0"]   # 软删除的文章永远不出现在任何列表里
    params: list = []

    if feed_id:
        where.append("a.feed_id = ?")
        params.append(feed_id)

    if view == "unread":
        where.append("COALESCE(ua.is_read, 0) = 0")
    elif view == "starred":
        where.append("COALESCE(ua.is_starred, 0) = 1")

    if folder == "none":
        where.append("COALESCE(ua.is_starred, 0) = 1 AND ua.folder_id IS NULL")
    elif folder:
        try:
            where.append("ua.folder_id = ?")
            params.append(int(folder))
        except (TypeError, ValueError):
            log.warning("非法收藏夹参数：%r", folder)

    for kw in keywords or []:
        kw = (kw or "").strip()
        if kw:
            where.append("COALESCE(a.title, '') NOT LIKE ?")
            params.append(f"%{kw}%")

    if query:
        like = f"%{query.strip()}%"
        where.append(
            "(COALESCE(a.title, '') LIKE ? OR COALESCE(a.summary, '') LIKE ? "
            " OR COALESCE(a.content, '') LIKE ?)"
        )
        params.extend([like, like, like])

    return (" WHERE " + " AND ".join(where)) if where else "", params


_IMG_SRC_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
# 表情、图标、占位图不能当缩略图 —— 微博正文经常以表情图开头，
# 不滤掉的话卡片上就是一张笑脸（实测踩过）
_IMG_BLOCK_HOSTS = ("face.t.sinajs.cn",)
_IMG_BLOCK_KEYWORDS = (
    "/expression/", "emoticon", "emoji", "/icon", "icon_", "_icon", "logo",
    "placeholder", "default", "spacer", "blank.", "pixel.", "avatar",
    "/face/", "timeline_card", "h5.sinaimg.cn/upload/",
)
_IMG_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")


def first_image(html_text: Optional[str]) -> Optional[str]:
    """
    从正文里挑出第一张**可用作缩略图**的图（列表页的网格卡片用）。
    跳过表情、图标、占位图；纯摘要源没有正文，返回 None，
    前端会退化成"来源首字 + 专属色"的占位块。
    """
    if not html_text:
        return None
    for match in _IMG_SRC_RE.finditer(html_text):
        url = (match.group(1) or "").strip()
        if url.startswith("//"):
            url = "https:" + url
        if not url.lower().startswith(("http://", "https://")):
            continue
        low = url.lower()
        host = low.split("/")[2] if low.count("/") >= 2 else ""
        if host in _IMG_BLOCK_HOSTS:
            continue
        if any(keyword in low for keyword in _IMG_BLOCK_KEYWORDS):
            continue
        if not low.split("?")[0].endswith(_IMG_EXTENSIONS):
            continue
        return url
    return None


def get_articles(
    view: str = "all",
    feed_id: Optional[int] = None,
    keywords: Optional[Iterable[str]] = None,
    limit: int = 30,
    cursor: Optional[str] = None,
    query: Optional[str] = None,
    folder: Optional[str] = None,
) -> dict:
    """
    按发布时间倒序返回文章列表。
    cursor 形如 "published|id"，做 keyset 分页，避免 offset 在刷新时漂移。
    搜索时把"标题命中"的排在前面（相关性优先），组内仍按时间倒序。
    """
    try:
        where, params = _build_filters(view, feed_id, keywords, query, folder)
        if cursor:
            try:
                c_pub, c_id = cursor.rsplit("|", 1)
                where += (" AND " if where else " WHERE ") + (
                    "(a.published < ? OR (a.published = ? AND a.id < ?))"
                )
                params.extend([c_pub, c_pub, int(c_id)])
            except Exception:  # noqa: BLE001
                log.warning("非法 cursor：%r", cursor)

        order = "a.published DESC, a.id DESC"
        if query:
            # 标题命中的排前面
            order = ("CASE WHEN COALESCE(a.title, '') LIKE ? THEN 0 ELSE 1 END, " + order)
            params_for_order = [f"%{query.strip()}%"]

        limit = max(1, min(int(limit or 30), 100))
        sql = (
            "SELECT a.id, a.feed_id, a.title, a.summary, a.link, a.author, a.published, "
            "  a.fetched_at, f.title AS feed_title, f.icon AS feed_icon, "
            "  COALESCE(ua.is_read, 0) AS is_read, COALESCE(ua.is_starred, 0) AS is_starred, "
            "  ua.folder_id AS folder_id, fl.name AS folder_name, fl.color AS folder_color, "
            "  (a.content IS NOT NULL AND a.content <> '') AS has_content, "
            # 只截前一段来找缩略图，避免把整篇正文（可能几十 KB）都读出来
            "  substr(a.content, 1, 8000) AS content_head "
            "FROM articles a "
            "JOIN feeds f ON f.id = a.feed_id "
            "LEFT JOIN user_actions ua ON ua.article_id = a.id "
            "LEFT JOIN folders fl ON fl.id = ua.folder_id"
            f"{where} ORDER BY {order} LIMIT ?"
        )
        args = list(params)
        if query:
            # ORDER BY 里的 ? 出现在 WHERE 参数之后、LIMIT 之前
            args.extend(params_for_order)
        args.append(limit + 1)

        with get_conn() as conn:
            rows = _rows(conn.execute(sql, args))

        for row in rows:
            row["image"] = first_image(row.pop("content_head", None))

        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = f"{rows[-1]['published']}|{rows[-1]['id']}" if rows and has_more else None
        return {"articles": rows, "next_cursor": next_cursor, "has_more": has_more}
    except Exception as exc:  # noqa: BLE001
        log.error("读取文章列表失败：%s", exc)
        return {"articles": [], "next_cursor": None, "has_more": False}


def count_articles(view: str = "all", feed_id: Optional[int] = None,
                   keywords: Optional[Iterable[str]] = None,
                   query: Optional[str] = None,
                   folder: Optional[str] = None) -> int:
    try:
        where, params = _build_filters(view, feed_id, keywords, query, folder)
        sql = (
            "SELECT COUNT(*) AS n FROM articles a "
            "LEFT JOIN user_actions ua ON ua.article_id = a.id" + where
        )
        with get_conn() as conn:
            row = _row(conn.execute(sql, params))
        return int(row["n"]) if row else 0
    except Exception as exc:  # noqa: BLE001
        log.error("统计文章数失败：%s", exc)
        return 0


def get_counts(keywords: Optional[Iterable[str]] = None) -> dict:
    """工具栏三个角标 + 总订阅数。"""
    return {
        "all": count_articles("all", keywords=keywords),
        "unread": count_articles("unread", keywords=keywords),
        "starred": count_articles("starred", keywords=keywords),
        "feeds": len(get_feeds()),
    }


def get_existing_guids(feed_id: int) -> set[str]:
    """取某个订阅源已经入库的 guid 集合（本地源用来跳过重复的正文请求）。"""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT guid FROM articles WHERE feed_id = ?", (feed_id,)
            ).fetchall()
        return {r["guid"] for r in rows}
    except Exception as exc:  # noqa: BLE001
        log.error("读取已有 guid 失败 feed=%s：%s", feed_id, exc)
        return set()


def get_content_state(feed_id: int) -> dict[str, dict]:
    """
    取某个订阅源已有文章的正文状态：{guid: {"len": 正文长度, "img": 是否含图}}。

    本地源用它决定"这篇还要不要再去请求一次正文"：
      - 没正文（len=0）→ 要取
      - 有正文但没图、而这类内容本该有图（例如知乎想法分享了带图回答）→ 还要取
    光看"有没有正文"是不够的：想法卡片自带文字却没有图，
    只看布尔值就会永远不去补图（实测就是这个问题）。
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT guid, length(COALESCE(content, '')) AS len, "
                "  (COALESCE(content, '') LIKE '%<img%') AS img, "
                "  COALESCE(link, '') AS link, COALESCE(content_v, 0) AS v "
                "FROM articles WHERE feed_id = ?",
                (feed_id,),
            ).fetchall()
        return {r["guid"]: {"len": int(r["len"] or 0), "img": bool(r["img"]),
                            "link": r["link"], "v": int(r["v"] or 0)}
                for r in rows}
    except Exception as exc:  # noqa: BLE001
        log.error("读取正文状态失败 feed=%s：%s", feed_id, exc)
        return {}


def delete_article(article_id: int) -> bool:
    """
    删除文章（**软删除**）。

    为什么不真删：文章去重靠 guid，源里那篇还在，下次刷新会原样再插进来 ——
    用户会发现"删了又回来"。软删除让这条记录继续占着 guid，但不再出现在任何列表/计数里。
    """
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE articles SET is_deleted = 1 WHERE id = ?", (article_id,)
            )
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.error("删除文章 %s 失败：%s", article_id, exc)
        return False


def restore_article(article_id: int) -> bool:
    """撤销删除。"""
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE articles SET is_deleted = 0 WHERE id = ?", (article_id,)
            )
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.error("恢复文章 %s 失败：%s", article_id, exc)
        return False


def _id_list(ids) -> tuple[str, list]:
    """把 id 列表变成占位符，顺手挡掉非法值。"""
    clean: list[int] = []
    for value in ids or []:
        try:
            clean.append(int(value))
        except (TypeError, ValueError):
            continue
    if not clean:
        return "", []
    return ",".join("?" * len(clean)), clean


# --------------------------------------------------------------------------- #
# 批量操作（多选）
# --------------------------------------------------------------------------- #
def batch_mark_read(ids, is_read: bool = True) -> int:
    try:
        ph, clean = _id_list(ids)
        if not ph:
            return 0
        with get_conn() as conn:
            # SELECT 带 WHERE 才能让 SQLite 正确识别后面的 ON CONFLICT
            cur = conn.execute(
                "INSERT INTO user_actions(article_id, is_read, read_at) "
                "SELECT id, ?, ? FROM articles "
                "WHERE id IN (" + ph + ") AND COALESCE(is_deleted, 0) = 0 "
                "ON CONFLICT(article_id) DO UPDATE SET is_read = excluded.is_read, "
                "  read_at = excluded.read_at",
                (1 if is_read else 0, utc_now_iso() if is_read else None, *clean),
            )
            return cur.rowcount
    except Exception as exc:  # noqa: BLE001
        log.error("批量标记已读失败：%s", exc)
        return 0


def batch_star(ids, starred: bool = True) -> int:
    try:
        ph, clean = _id_list(ids)
        if not ph:
            return 0
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO user_actions(article_id, is_starred) "
                "SELECT id, ? FROM articles "
                "WHERE id IN (" + ph + ") AND COALESCE(is_deleted, 0) = 0 "
                "ON CONFLICT(article_id) DO UPDATE SET is_starred = excluded.is_starred",
                (1 if starred else 0, *clean),
            )
            return cur.rowcount
    except Exception as exc:  # noqa: BLE001
        log.error("批量星标失败：%s", exc)
        return 0


def batch_set_folder(ids, folder_id: Optional[int]) -> int:
    """批量放进/移出收藏夹。放进收藏夹会自动加星标。"""
    try:
        ph, clean = _id_list(ids)
        if not ph:
            return 0
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO user_actions(article_id, folder_id, is_starred) "
                "SELECT id, ?, ? FROM articles "
                "WHERE id IN (" + ph + ") AND COALESCE(is_deleted, 0) = 0 "
                "ON CONFLICT(article_id) DO UPDATE SET folder_id = excluded.folder_id, "
                "  is_starred = MAX(COALESCE(user_actions.is_starred, 0), excluded.is_starred)",
                (folder_id, 1 if folder_id else 0, *clean),
            )
            return cur.rowcount
    except Exception as exc:  # noqa: BLE001
        log.error("批量移动收藏夹失败：%s", exc)
        return 0


def batch_delete(ids) -> int:
    try:
        ph, clean = _id_list(ids)
        if not ph:
            return 0
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE articles SET is_deleted = 1 WHERE id IN (" + ph + ")", clean
            )
            return cur.rowcount
    except Exception as exc:  # noqa: BLE001
        log.error("批量删除失败：%s", exc)
        return 0


def batch_restore(ids) -> int:
    try:
        ph, clean = _id_list(ids)
        if not ph:
            return 0
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE articles SET is_deleted = 0 WHERE id IN (" + ph + ")", clean
            )
            return cur.rowcount
    except Exception as exc:  # noqa: BLE001
        log.error("批量恢复失败：%s", exc)
        return 0


# --------------------------------------------------------------------------- #
# 收藏夹
# --------------------------------------------------------------------------- #
def get_folders(with_counts: bool = True) -> list[dict]:
    """收藏夹列表。counts 指"该收藏夹里已收藏的文章数"。"""
    try:
        sql = (
            "SELECT fl.id, fl.name, fl.color, fl.sort_order, fl.created_at, "
            "  (SELECT COUNT(*) FROM user_actions ua JOIN articles a ON a.id = ua.article_id "
            "     WHERE ua.folder_id = fl.id AND COALESCE(ua.is_starred, 0) = 1 "
            "       AND COALESCE(a.is_deleted, 0) = 0) AS count "
            "FROM folders fl ORDER BY fl.sort_order, fl.id"
        ) if with_counts else "SELECT * FROM folders ORDER BY sort_order, id"
        with get_conn() as conn:
            return _rows(conn.execute(sql))
    except Exception as exc:  # noqa: BLE001
        log.error("读取收藏夹失败：%s", exc)
        return []


def get_folder(folder_id: int) -> Optional[dict]:
    try:
        with get_conn() as conn:
            return _row(conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)))
    except Exception as exc:  # noqa: BLE001
        log.error("读取收藏夹 %s 失败：%s", folder_id, exc)
        return None


def create_folder(name: str, color: str = "#007AFF") -> Optional[int]:
    name = (name or "").strip()
    if not name:
        return None
    try:
        with get_conn() as conn:
            row = _row(conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS nxt FROM folders"))
            cur = conn.execute(
                "INSERT INTO folders(name, color, sort_order) VALUES (?, ?, ?)",
                (name[:40], (color or "#007AFF")[:20], int(row["nxt"]) if row else 1),
            )
            return cur.lastrowid
    except Exception as exc:  # noqa: BLE001
        log.error("创建收藏夹失败：%s", exc)
        return None


def update_folder(folder_id: int, name: Optional[str] = None,
                  color: Optional[str] = None) -> bool:
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE folders SET name = COALESCE(?, name), color = COALESCE(?, color) "
                "WHERE id = ?",
                ((name or "").strip()[:40] or None, (color or "")[:20] or None, folder_id),
            )
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.error("更新收藏夹 %s 失败：%s", folder_id, exc)
        return False


def delete_folder(folder_id: int) -> bool:
    """
    删除收藏夹。里面的文章不会被删，只是回到"未分类"（靠外键 ON DELETE SET NULL）。
    """
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE user_actions SET folder_id = NULL WHERE folder_id = ?", (folder_id,)
            )
            cur = conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.error("删除收藏夹 %s 失败：%s", folder_id, exc)
        return False


def set_article_folder(article_id: int, folder_id: Optional[int]) -> bool:
    """
    把文章放进收藏夹 / 移出收藏夹。
    放进收藏夹会自动加星标（否则它在收藏夹视图里看不见，用户会困惑）。
    """
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO user_actions(article_id, folder_id, is_starred) VALUES (?, ?, ?) "
                "ON CONFLICT(article_id) DO UPDATE SET folder_id = excluded.folder_id, "
                "  is_starred = MAX(COALESCE(user_actions.is_starred, 0), excluded.is_starred)",
                (article_id, folder_id, 1 if folder_id else 0),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("设置文章收藏夹 %s 失败：%s", article_id, exc)
        return False


def get_article(article_id: int) -> Optional[dict]:
    try:
        sql = (
            "SELECT a.*, f.title AS feed_title, f.site_url AS feed_site_url, f.icon AS feed_icon, "
            "  COALESCE(ua.is_read, 0) AS is_read, COALESCE(ua.is_starred, 0) AS is_starred, "
            "  ua.folder_id AS folder_id, fl.name AS folder_name, fl.color AS folder_color "
            "FROM articles a JOIN feeds f ON f.id = a.feed_id "
            "LEFT JOIN user_actions ua ON ua.article_id = a.id "
            "LEFT JOIN folders fl ON fl.id = ua.folder_id "
            "WHERE a.id = ? AND COALESCE(a.is_deleted, 0) = 0"
        )
        with get_conn() as conn:
            return _row(conn.execute(sql, (article_id,)))
    except Exception as exc:  # noqa: BLE001
        log.error("读取文章 %s 失败：%s", article_id, exc)
        return None


def save_article_content(article_id: int, content: Optional[str], tried: bool = True) -> bool:
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE articles SET content = ?, content_tried = ? WHERE id = ?",
                (content, 1 if tried else 0, article_id),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("保存正文 %s 失败：%s", article_id, exc)
        return False


def mark_read(article_id: int, is_read: bool = True) -> bool:
    """标记已读/未读。user_actions 用 UPSERT，无需预先建行。"""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO user_actions(article_id, is_read, read_at) VALUES (?, ?, ?) "
                "ON CONFLICT(article_id) DO UPDATE SET is_read = excluded.is_read, "
                "  read_at = excluded.read_at",
                (article_id, 1 if is_read else 0, utc_now_iso() if is_read else None),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("标记已读 %s 失败：%s", article_id, exc)
        return False


def mark_all_read() -> int:
    """全部已读。返回受影响的文章数。"""
    try:
        with get_conn() as conn:
            # 注意：SQLite 里 INSERT ... SELECT 后面的 ON CONFLICT 有解析歧义，
            # SELECT 必须带一个 WHERE（哪怕只是 WHERE true），否则会报 near "DO": syntax error
            cur = conn.execute(
                "INSERT INTO user_actions(article_id, is_read, read_at) "
                "SELECT id, 1, ? FROM articles WHERE true "
                "ON CONFLICT(article_id) DO UPDATE SET is_read = 1, read_at = excluded.read_at",
                (utc_now_iso(),),
            )
            return cur.rowcount
    except Exception as exc:  # noqa: BLE001
        log.error("全部已读失败：%s", exc)
        return 0


def toggle_star(article_id: int) -> Optional[bool]:
    """切换星标，返回新的星标状态；失败返回 None。"""
    try:
        with get_conn() as conn:
            row = _row(
                conn.execute(
                    "SELECT COALESCE(is_starred, 0) AS s FROM user_actions WHERE article_id = ?",
                    (article_id,),
                )
            )
            new_state = 0 if (row and row["s"]) else 1
            conn.execute(
                "INSERT INTO user_actions(article_id, is_starred) VALUES (?, ?) "
                "ON CONFLICT(article_id) DO UPDATE SET is_starred = excluded.is_starred",
                (article_id, new_state),
            )
            return bool(new_state)
    except Exception as exc:  # noqa: BLE001
        log.error("切换星标 %s 失败：%s", article_id, exc)
        return None


def clear_articles() -> int:
    """清空所有文章（保留订阅源）。"""
    try:
        with get_conn() as conn:
            cur = conn.execute("DELETE FROM articles")
            return cur.rowcount
    except Exception as exc:  # noqa: BLE001
        log.error("清空文章失败：%s", exc)
        return 0


def get_starred_articles() -> list[dict]:
    """导出 / 批量操作用：取全部星标文章。"""
    try:
        with get_conn() as conn:
            return _rows(
                conn.execute(
                    "SELECT a.*, f.title AS feed_title FROM articles a "
                    "JOIN feeds f ON f.id = a.feed_id "
                    "JOIN user_actions ua ON ua.article_id = a.id "
                    "WHERE ua.is_starred = 1 AND COALESCE(a.is_deleted, 0) = 0 "
                    "ORDER BY a.published DESC"
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.error("读取星标文章失败：%s", exc)
        return []


def get_deleted_count() -> int:
    """被软删除的文章数（设置页展示用）。"""
    try:
        with get_conn() as conn:
            row = _row(conn.execute(
                "SELECT COUNT(*) AS n FROM articles WHERE COALESCE(is_deleted, 0) = 1"
            ))
        return int(row["n"]) if row else 0
    except Exception as exc:  # noqa: BLE001
        log.error("统计已删除文章失败：%s", exc)
        return 0
