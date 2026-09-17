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
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger("byread.db")

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
DB_PATH = INSTANCE_DIR / "bai_read.db"

# 默认设置：首次启动写入 settings 表
DEFAULT_SETTINGS: dict[str, str] = {
    "theme": "light",                       # light / dark
    "view_mode": "card",                    # card / list
    "filter_keywords": "[]",                # JSON 数组，标题命中即隐藏
    "block_images": "false",                # 阅读页是否屏蔽图片（隐私模式）
    "rsshub_instance": "",                  # 留空 = 自动探测可用实例
    "auto_refresh_minutes": "30",           # 0 = 关闭自动刷新
    "page_size": "30",
    "sidebar_open": "true",                 # 侧边栏展开 / 收起
    # 监听地址（给将来的多端/托管留的口子）。环境变量 BYREAD_HOST / BYREAD_PORT 优先，
    # 默认仍然只监听本机 127.0.0.1:5000 —— 局域网也访问不到，这是有意的安全默认
    "bind_host": "127.0.0.1",
    "bind_port": "5000",
    # 访问口令：留空 = 不鉴权（默认，本地自用）。填了之后所有页面和接口都要口令，
    # 也可以直接用环境变量 BYREAD_TOKEN 覆盖（详见 app.py 的鉴权钩子）
    "access_token": "",
    # 版本号（见下方 sync_versions）。这里只是把当前值写进设置表，方便查看与将来比对
    "schema_version": "1",
    "content_version": "3",
    # 登录信息（敏感，接口一律脱敏返回，只存本地）
    "zhihu_cookie": "",
    "weibo_cookie": "",
}

# --------------------------------------------------------------------------- #
# 版本号与"重处理"入口
#
# 两个版本号各管一件事，改了什么就把对应的 +1：
#   SCHEMA_VERSION  结构 / 语义：加表、加列、改字段含义
#   CONTENT_VERSION 正文提取：换解析器、补图片、改清洗规则
#
# 光有版本号还不够，得让**已经入库的数据**跟上，所以配一个注册表：
#   CONTENT_REPROCESS[版本号] = 函数(conn) -> 影响行数
# 启动时 sync_versions() 会按 (库里记录的版本, 当前版本] 依次执行这些函数。
# 没挂函数的版本走**默认动作**：把老文章的 content_v 清零，下次刷新按新逻辑重取 ——
# 这正是 CONTENT_VERSION 一直以来的约定（见 get_content_state / zhihu.py / weibo.py），
# 现在把它从"各处心照不宣的写法"变成了一处机制。
#
# 例：以后正文提取换了解析器
#     1) CONTENT_VERSION += 1
#     2) 需要精确控制就挂一个函数，只把受影响的文章标旧（见下面 _rescue_zhihu_truncated）：
#        CONTENT_REPROCESS[新版本号] = lambda conn: conn.execute(
#            "UPDATE articles SET content_v = 0 WHERE <条件>").rowcount
#    不挂也行，默认动作是"全部标旧"，代价只是多跑一轮正文请求。
#
# 用版本号而不是"看正文长度 / 有没有图"来判断，是因为后者既会漏（有正文但内容过时）
# 又会反复触发（正文本来就没图的内容每次刷新都白跑一次请求）。
# --------------------------------------------------------------------------- #
SCHEMA_VERSION = 1
CONTENT_VERSION = 3

SCHEMA_REPROCESS: dict[int, Callable[[sqlite3.Connection], int]] = {}


def _rescue_zhihu_truncated(conn) -> int:
    """
    v3 一次性修复：早期版本把知乎接口的**截断正文**当成完整正文存了下来。

    实测：66 条知乎回答里有 12 条是半截（库里 1346 字 / 接口 6346 字，配图一张不剩），
    而且因为"有正文 + 版本号是当前值"，刷新时永远不会再取一次。
    这里把知乎源的正文标记为"待重取"（content_v = 0），
    下次刷新（每次有配额，几轮跑完）就会按现在的逻辑重新取一遍并覆盖。

    只动知乎源：别的平台没有证据，不要牵连。
    以后哪个平台也发现这类问题，照抄这个函数挂到 CONTENT_REPROCESS 上即可。
    """
    cur = conn.execute(
        "UPDATE articles SET content_v = 0 "
        "WHERE COALESCE(content, '') <> '' AND feed_id IN "
        "  (SELECT id FROM feeds WHERE feed_url LIKE 'byread://zhihu/%')"
    )
    return cur.rowcount


CONTENT_REPROCESS: dict[int, Callable[[sqlite3.Connection], int]] = {
    3: _rescue_zhihu_truncated,
}


def mark_content_stale(feed_id: Optional[int] = None) -> int:
    """
    手动把（某个源的）文章正文标记为"待重取"，下次刷新会重新去取。返回影响行数。

    用途：怀疑某个平台给的是残文时（例如又发现一个新平台会截断正文），
    先标旧再刷新，不用清库也不用重新订阅：
        python -c "import db; print(db.mark_content_stale(feed_id=27))"
    不传 feed_id 就是全部文章。
    """
    try:
        with get_conn() as conn:
            if feed_id:
                cur = conn.execute("UPDATE articles SET content_v = 0 WHERE feed_id = ?", (feed_id,))
            else:
                cur = conn.execute("UPDATE articles SET content_v = 0")
            rows = cur.rowcount
        log.info("已把 %s 篇文章的正文标记为待重取（feed=%s）", rows, feed_id or "全部")
        return rows
    except Exception as exc:  # noqa: BLE001
        log.error("标记正文待重取失败：%s", exc)
        return 0

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
    -- 播客音频（来自条目里的 <enclosure type="audio/...">）。
    -- 单独一列，**不塞进 content**：正文清洗会把 <audio>/<video>/<source> 整段丢掉，
    -- 那是防注入的红线，不能为了放播放器就放开。
    audio_url      TEXT,
    audio_duration INTEGER,          -- 秒；源里没有 itunes:duration 时为 NULL
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

-- 收藏夹：名字 + 颜色由用户自定义（装的是**文章**）
CREATE TABLE IF NOT EXISTS folders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    color       TEXT DEFAULT '#007AFF',
    sort_order  INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- 频道：用户自建，把**订阅源**分组（和"收藏夹装文章"是两个不同维度）
CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    color       TEXT DEFAULT '#007AFF',
    sort_order  INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- 频道 ↔ 订阅源 多对多（同一个源可以同时属于"科技"和"每日必读"）
CREATE TABLE IF NOT EXISTS channel_feeds (
    channel_id  INTEGER NOT NULL,
    feed_id     INTEGER NOT NULL,
    PRIMARY KEY (channel_id, feed_id),
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE,
    FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE
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
CREATE INDEX IF NOT EXISTS idx_channel_feeds_feed   ON channel_feeds(feed_id);
"""

# 老库升级用：列不存在时才补
MIGRATIONS: list[tuple[str, str, str]] = [
    ("articles", "is_deleted", "is_deleted INTEGER DEFAULT 0"),
    ("articles", "content_v", "content_v INTEGER DEFAULT 0"),
    ("user_actions", "folder_id", "folder_id INTEGER"),
    ("articles", "audio_url", "audio_url TEXT"),
    ("articles", "audio_duration", "audio_duration INTEGER"),
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
            sync_versions()          # 4. 版本推进 / 重处理（没有变化时什么都不做）
            log.info("数据库就绪：%s", DB_PATH)
        except Exception as exc:  # noqa: BLE001
            log.error("数据库初始化失败：%s", exc)


# --------------------------------------------------------------------------- #
# 版本推进（统一的重处理入口）
# --------------------------------------------------------------------------- #
def _stored_version(conn, key: str) -> Optional[int]:
    """读设置表里记录的版本号；没记录或不是数字都返回 None。"""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    raw = row["value"] if isinstance(row, sqlite3.Row) else row[0]
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning("设置 %s 不是数字（%r），按未记录处理", key, raw)
        return None


def _write_version(conn, key: str, value: int) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _mark_content_stale(conn, current: int) -> int:
    """
    默认的重处理动作：把"用旧版逻辑取的正文"标记为待重取。

    content_v 清零后，本地源在下次刷新时会重新请求这几篇的正文
    （判断逻辑在 zhihu.py / weibo.py 里：old_ver < db.CONTENT_VERSION 就算过时）。
    """
    cur = conn.execute(
        "UPDATE articles SET content_v = 0 WHERE COALESCE(content_v, 0) < ?", (current,)
    )
    return cur.rowcount


def sync_versions() -> dict:
    """
    统一的"版本推进"入口，init_db() 里调用一次。返回做了什么（供日志查看）。

    规则：
      - 库里没记录过版本 → **只写版本号，不重处理**。否则每个老库升级后
        都会莫名其妙地全量重取一遍正文（实测很容易被当成"刷新坏了"）。
      - 记录过、且落在一个需要重处理的版本区间 → 依次执行注册表里的函数；
        没挂函数的版本走默认动作（见 _mark_content_stale）。
    整个过程不会抛异常：版本推进失败也不该让程序起不来。
    """
    summary = {"schema": (None, SCHEMA_VERSION), "content": (None, CONTENT_VERSION),
               "steps": []}
    try:
        with get_conn() as conn:
            for key, current, registry, kind in (
                ("schema_version", SCHEMA_VERSION, SCHEMA_REPROCESS, "schema"),
                ("content_version", CONTENT_VERSION, CONTENT_REPROCESS, "content"),
            ):
                old = _stored_version(conn, key)
                if old is None:
                    _write_version(conn, key, current)
                    continue
                summary[kind] = (old, current)
                if old >= current:
                    continue
                for version in range(old + 1, current + 1):
                    step = registry.get(version)
                    if step is not None:
                        summary["steps"].append((kind, version,
                                                 getattr(step, "__name__", "step"),
                                                 step(conn)))
                    elif kind == "content":
                        summary["steps"].append((kind, version, "标记老正文待重取",
                                                 _mark_content_stale(conn, current)))
                    # 结构版本没挂函数 = 结构变更已经由 _ensure_columns / SCHEMA 处理完了
                _write_version(conn, key, current)
        if summary["steps"]:
            log.info("版本推进：schema %s → %s，content %s → %s，执行 %s",
                     summary["schema"][0], summary["schema"][1],
                     summary["content"][0], summary["content"][1], summary["steps"])
    except Exception as exc:  # noqa: BLE001
        log.error("版本推进失败（不影响正常使用）：%s", exc)
    return summary


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
            # 计数必须排除"软删除"的文章，否则设置页和来源筛选条上的篇数会比实际多
            # （实测：某源显示 16 篇，实际只有 13 篇，差的 3 篇是用户删掉的）
            "  (SELECT COUNT(*) FROM articles a "
            "     WHERE a.feed_id = f.id AND COALESCE(a.is_deleted, 0) = 0) AS article_count, "
            "  (SELECT COUNT(*) FROM articles a "
            "     LEFT JOIN user_actions ua ON ua.article_id = a.id "
            "     WHERE a.feed_id = f.id AND COALESCE(a.is_deleted, 0) = 0 "
            "       AND COALESCE(ua.is_read, 0) = 0) AS unread_count "
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
                "(feed_id, guid, title, summary, content, link, author, published, content_v, "
                " audio_url, audio_duration) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    feed_id,
                    item.get("guid") or make_guid(feed_id, item.get("link"), item.get("title")),
                    (item.get("title") or "")[:500],
                    item.get("summary"),
                    content,
                    item.get("link"),
                    item.get("author"),
                    to_iso(item.get("published")),
                    # 入库时正文就是用当前版本的解析逻辑生成的；
                    # 但若抓取方标了"这份正文是被源截断的"（content_incomplete），
                    # 就写 0 = "不可信，下次刷新重取"（见 feed_parser.looks_truncated 的说明）
                    0 if item.get("content_incomplete") else (CONTENT_VERSION if content else 0),
                    item.get("audio_url"),
                    item.get("audio_duration"),
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
      4. 播客音频是后加的字段：早先入库的播客文章没有 audio_url，
         靠这条在下次刷新时补上，不需要重新订阅或清库。
    """
    try:
        guid = item.get("guid") or make_guid(feed_id, item.get("link"), item.get("title"))
        new_content = item.get("content") or ""
        new_title = (item.get("title") or "")[:500]
        new_summary = item.get("summary") or ""
        new_link = (item.get("link") or "").strip()
        new_audio = (item.get("audio_url") or "").strip()
        new_duration = item.get("audio_duration")

        with get_conn() as conn:
            row = _row(conn.execute(
                "SELECT content, title, summary, link, audio_url, COALESCE(content_v, 0) AS v "
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
            old_audio = row["audio_url"] or ""
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
            # 音频一旦抓到就不用再写（同一个 href 不会变）；换地址了就整份覆盖，
            # 包括把时长一起改掉 —— 否则会出现"新地址配旧时长"
            set_audio = bool(new_audio) and new_audio != old_audio

            if not (set_content or set_title or set_summary or set_link or set_audio):
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
                # 记下这份正文是用哪个版本的逻辑取的；
                # 抓取方标了"被源截断"就写 0，让下次刷新再取一次（宁可多取，不要残缺）
                sets.append("content_v = ?")
                params.append(0 if item.get("content_incomplete") else CONTENT_VERSION)
            if set_title:
                sets.append("title = ?")
                params.append(new_title)
            if set_summary:
                sets.append("summary = ?")
                params.append(new_summary)
            if set_link:
                sets.append("link = ?")
                params.append(new_link)
            if set_audio:
                sets.append("audio_url = ?")
                params.append(new_audio)
                sets.append("audio_duration = ?")
                params.append(int(new_duration) if new_duration else None)
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
    channel: Optional[int] = None,
) -> tuple[str, list]:
    """
    构造 WHERE 子句。
    keywords 命中标题的文章会被隐藏（在 SQL 层过滤，保证分页计数正确）。
    query   是用户主动搜索的词，跨 **来源名 / 标题 / 摘要 / 正文** 匹配。
    folder  取 "none"（未分类）或收藏夹 id。
    channel 频道 id：只看该频道里那些订阅源的文章。
    """
    where: list[str] = ["COALESCE(a.is_deleted, 0) = 0"]   # 软删除的文章永远不出现在任何列表里
    params: list = []

    if feed_id:
        where.append("a.feed_id = ?")
        params.append(feed_id)

    if channel:
        # 频道 = 一组订阅源。用子查询做过滤，抓取后新文章自动就在频道里，
        # 不需要任何"同步到频道"的额外逻辑
        where.append("a.feed_id IN (SELECT feed_id FROM channel_feeds WHERE channel_id = ?)")
        params.append(channel)

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
        # 除了文章自身的内容，**也匹配订阅源的名字** —— 这样输入"友琳"就能列出
        # 友琳这个源的全部文章（否则文章正文里没有这几个字，就一条都搜不到）
        where.append(
            "(COALESCE(a.title, '') LIKE ? OR COALESCE(a.summary, '') LIKE ? "
            " OR COALESCE(a.content, '') LIKE ? OR COALESCE(f.title, '') LIKE ?)"
        )
        params.extend([like, like, like, like])

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
    channel: Optional[int] = None,
) -> dict:
    """
    按发布时间倒序返回文章列表。
    cursor 形如 "published|id"，做 keyset 分页，避免 offset 在刷新时漂移。
    搜索时把"标题命中"的排在前面（相关性优先），组内仍按时间倒序。
    """
    try:
        where, params = _build_filters(view, feed_id, keywords, query, folder, channel)
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
            # 相关度排序：文章标题命中 → 来源名命中 → 其余（摘要/正文命中）。
            # "来源名命中"整组排在一起，所以搜某个源的名字时，那个源的文章会按时间连着列出来。
            order = ("CASE WHEN COALESCE(a.title, '') LIKE ? THEN 0 "
                     "WHEN COALESCE(f.title, '') LIKE ? THEN 1 ELSE 2 END, " + order)
            params_for_order = [f"%{query.strip()}%", f"%{query.strip()}%"]

        limit = max(1, min(int(limit or 30), 100))
        sql = (
            "SELECT a.id, a.feed_id, a.title, a.summary, a.link, a.author, a.published, "
            "  a.fetched_at, f.title AS feed_title, f.icon AS feed_icon, "
            "  COALESCE(ua.is_read, 0) AS is_read, COALESCE(ua.is_starred, 0) AS is_starred, "
            "  ua.folder_id AS folder_id, fl.name AS folder_name, fl.color AS folder_color, "
            "  (a.content IS NOT NULL AND a.content <> '') AS has_content, "
            # 只给"有没有音频"这个布尔值：卡片上只要一个小喇叭标记，
            # 音频地址本身（可能上百 KB 的签名 URL 也用不上）留给阅读页接口
            "  (a.audio_url IS NOT NULL AND a.audio_url <> '') AS has_audio, "
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
                   folder: Optional[str] = None,
                   channel: Optional[int] = None) -> int:
    try:
        where, params = _build_filters(view, feed_id, keywords, query, folder, channel)
        # 必须和 get_articles 用同样的 JOIN：搜索条件里可能引用 feeds 表（按来源名搜索），
        # 少了这个 JOIN 会直接 SQL 报错，而异常被吞掉后表现为"计数 0、列表却有内容"
        sql = (
            "SELECT COUNT(*) AS n FROM articles a "
            "JOIN feeds f ON f.id = a.feed_id "
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


# --------------------------------------------------------------------------- #
# 频道：把**订阅源**分组（和收藏夹装文章是两个维度）
# --------------------------------------------------------------------------- #
def get_channels() -> list[dict]:
    """频道列表，带"包含几个源 / 共多少篇 / 多少未读"三个统计。"""
    try:
        sql = (
            "SELECT c.*, "
            "  (SELECT COUNT(*) FROM channel_feeds cf WHERE cf.channel_id = c.id) AS feed_count, "
            "  (SELECT COUNT(*) FROM articles a "
            "     JOIN channel_feeds cf ON cf.feed_id = a.feed_id "
            "     WHERE cf.channel_id = c.id AND COALESCE(a.is_deleted, 0) = 0) AS article_count, "
            "  (SELECT COUNT(*) FROM articles a "
            "     JOIN channel_feeds cf ON cf.feed_id = a.feed_id "
            "     LEFT JOIN user_actions ua ON ua.article_id = a.id "
            "     WHERE cf.channel_id = c.id AND COALESCE(a.is_deleted, 0) = 0 "
            "       AND COALESCE(ua.is_read, 0) = 0) AS unread_count "
            "FROM channels c ORDER BY c.sort_order, c.id"
        )
        with get_conn() as conn:
            rows = _rows(conn.execute(sql))
            # 把"包含哪些订阅源"一起带上：编辑频道时要用来预勾选，
            # 不带的话打开编辑弹窗会显示成"一个源都没选"
            mapping: dict[int, list[int]] = {}
            for link in conn.execute("SELECT channel_id, feed_id FROM channel_feeds"):
                mapping.setdefault(int(link["channel_id"]), []).append(int(link["feed_id"]))
        for row in rows:
            row["feed_ids"] = mapping.get(int(row["id"]), [])
        return rows
    except Exception as exc:  # noqa: BLE001
        log.error("读取频道失败：%s", exc)
        return []


def get_channel(channel_id: int) -> Optional[dict]:
    try:
        with get_conn() as conn:
            row = _row(conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)))
        if row:
            row["feed_ids"] = get_channel_feed_ids(channel_id)
        return row
    except Exception as exc:  # noqa: BLE001
        log.error("读取频道 %s 失败：%s", channel_id, exc)
        return None


def get_channel_feed_ids(channel_id: int) -> list[int]:
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT feed_id FROM channel_feeds WHERE channel_id = ?", (channel_id,)
            ).fetchall()
        return [int(r["feed_id"]) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.error("读取频道订阅源失败：%s", exc)
        return []


def create_channel(name: str, color: str = "#007AFF",
                   feed_ids: Optional[Iterable[int]] = None) -> Optional[int]:
    name = (name or "").strip()
    if not name:
        return None
    try:
        with get_conn() as conn:
            row = _row(conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 AS nxt FROM channels"))
            cur = conn.execute(
                "INSERT INTO channels(name, color, sort_order) VALUES (?, ?, ?)",
                (name[:40], (color or "#007AFF")[:20], int(row["nxt"]) if row else 1),
            )
            channel_id = cur.lastrowid
        if feed_ids:
            set_channel_feeds(channel_id, feed_ids)
        return channel_id
    except Exception as exc:  # noqa: BLE001
        log.error("创建频道失败：%s", exc)
        return None


def update_channel(channel_id: int, name: Optional[str] = None,
                   color: Optional[str] = None) -> bool:
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE channels SET name = COALESCE(?, name), color = COALESCE(?, color) "
                "WHERE id = ?",
                ((name or "").strip()[:40] or None, (color or "")[:20] or None, channel_id),
            )
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.error("更新频道 %s 失败：%s", channel_id, exc)
        return False


def delete_channel(channel_id: int) -> bool:
    """删除频道。**订阅源和文章都不受影响**，只是解除分组关系。"""
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM channel_feeds WHERE channel_id = ?", (channel_id,))
            cur = conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
            return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        log.error("删除频道 %s 失败：%s", channel_id, exc)
        return False


def set_channel_feeds(channel_id: int, feed_ids: Iterable[int]) -> int:
    """整组替换频道里的订阅源。返回实际关联的数量。"""
    clean: list[int] = []
    for value in feed_ids or []:
        try:
            clean.append(int(value))
        except (TypeError, ValueError):
            continue
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM channel_feeds WHERE channel_id = ?", (channel_id,))
            for feed_id in dict.fromkeys(clean):       # 去重且保持顺序
                conn.execute(
                    "INSERT OR IGNORE INTO channel_feeds(channel_id, feed_id) VALUES (?, ?)",
                    (channel_id, feed_id),
                )
        return len(set(clean))
    except Exception as exc:  # noqa: BLE001
        log.error("设置频道订阅源失败：%s", exc)
        return 0


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


# --------------------------------------------------------------------------- #
# 残文修复（配合 feed_parser.CONTENT_REFETCHERS）
#
# "有正文、但版本号落后于当前版本" = 这份正文要么是旧逻辑取的，要么抓取方标过
# "被源截断"。刷新只能拿到最近 N 条，所以这些老文章需要单独一条通路去补 —— 就是这两个函数。
# --------------------------------------------------------------------------- #
def get_stale_content_articles(feed_id: int, limit: int = 5) -> list[dict]:
    """取该源下"有正文但正文不可信"的文章（给残文修复用）。"""
    try:
        with get_conn() as conn:
            return _rows(conn.execute(
                "SELECT id, title, link, content FROM articles "
                "WHERE feed_id = ? AND COALESCE(content, '') <> '' "
                "  AND COALESCE(content_v, 0) < ? AND COALESCE(is_deleted, 0) = 0 "
                "ORDER BY published DESC, id DESC LIMIT ?",
                (feed_id, CONTENT_VERSION, max(1, int(limit))),
            ))
    except Exception as exc:  # noqa: BLE001
        log.error("读取待修复正文失败 feed=%s：%s", feed_id, exc)
        return []


def save_repaired_content(article_id: int, content: str, complete: bool = True) -> bool:
    """
    把"重取回来的正文"写回去。complete=False 时保持 content_v=0 —— 下次还会再试。
    只在这份正文确实更完整时覆盖（避免用一份更差的把好内容冲掉）。
    """
    try:
        new_content = content or ""
        if not new_content:
            return False
        with get_conn() as conn:
            row = _row(conn.execute(
                "SELECT content, COALESCE(content_v, 0) AS v FROM articles WHERE id = ?",
                (article_id,)))
            if not row:
                return False
            old = row["content"] or ""
            better = (not old
                      or ("<img" not in old and "<img" in new_content)
                      or len(new_content) > len(old) + 200)
            if not better:
                # 内容没变好：只把版本号推进到当前值，表示"这份已经确认过了"，别再反复取
                if complete and int(row["v"] or 0) < CONTENT_VERSION:
                    conn.execute("UPDATE articles SET content_v = ? WHERE id = ?",
                                 (CONTENT_VERSION, article_id))
                return False
            conn.execute(
                "UPDATE articles SET content = ?, content_v = ? WHERE id = ?",
                (new_content, CONTENT_VERSION if complete else 0, article_id),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("写回修复后的正文失败 %s：%s", article_id, exc)
        return False


def mark_content_checked(article_id: int) -> bool:
    """
    把正文标记为"已确认过"（版本号推进到当前值，不动正文内容）。

    用在"抓取方明确说这条没有可取的内容"时（例如回答已被作者删除）——
    否则修复流程每轮刷新都会再去试一次，白占配额。
    """
    try:
        with get_conn() as conn:
            conn.execute("UPDATE articles SET content_v = ? WHERE id = ?",
                         (CONTENT_VERSION, article_id))
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("标记正文已确认失败 %s：%s", article_id, exc)
        return False


# --------------------------------------------------------------------------- #
# 数据导出（自用保险）
#
# **只导出数据表，绝不导出 settings** —— 登录信息（zhihu_cookie / weibo_cookie）
# 只存在 settings 里。这和 .gitignore 排除 instance/ 是同一条红线：
# 导出文件是要被复制、备份、甚至发给别人的，绝不能夹带 Cookie。
#
# 这里用**显式白名单**，而不是"遍历 sqlite_master 里所有表"：
# 后者意味着以后新增任何一张表都会自动被导出，早晚有一天会把 settings 捎出去。
# --------------------------------------------------------------------------- #
EXPORT_TABLES = (
    "feeds",           # 订阅源
    "articles",        # 文章（含软删除的，备份要完整）
    "user_actions",    # 已读 / 星标 / 收藏夹归属
    "folders",         # 收藏夹
    "channels",        # 频道
    "channel_feeds",   # 频道 ↔ 订阅源（少了它，频道恢复出来是空的）
)

# 手滑把 settings 加进白名单的话，让程序在启动时就炸掉，
# 而不是安静地把登录信息导出去（这种事必须"响"着失败）
if "settings" in EXPORT_TABLES:  # pragma: no cover
    raise RuntimeError("settings 表里有登录信息，绝不能进导出白名单")


def export_tables(tables: Optional[Iterable[str]] = None) -> dict[str, list[dict]]:
    """
    把指定的数据表原样导出成 JSON 可序列化的字典：{表名: [行, ...]}。

    表名只可能来自上面的白名单（不是用户输入），所以这里拼 SQL 没有注入问题；
    任何一张表失败只影响它自己，不影响其它表，也不会抛异常。
    """
    wanted = list(tables if tables is not None else EXPORT_TABLES)
    if "settings" in wanted:
        log.error("拒绝导出：settings 表里有登录信息")
        return {}
    out: dict[str, list[dict]] = {}
    try:
        with get_conn() as conn:
            for table in wanted:
                try:
                    out[table] = _rows(conn.execute(f"SELECT * FROM {table}"))
                except Exception as exc:  # noqa: BLE001
                    log.error("导出表 %s 失败：%s", table, exc)
                    out[table] = []
    except Exception as exc:  # noqa: BLE001
        log.error("导出数据失败：%s", exc)
    return out


def get_articles_for_export() -> list[dict]:
    """
    取出用于「每篇一个 Markdown」的文章（不含软删除的），带上来源名与收藏夹名。
    正文用 a.content（正文提取的结果），没有正文的走 summary。
    """
    try:
        with get_conn() as conn:
            return _rows(conn.execute(
                "SELECT a.id, a.title, a.author, a.published, a.link, a.content, a.summary, "
                "  a.audio_url, f.title AS feed_title, f.site_url AS feed_site_url, "
                "  COALESCE(ua.is_starred, 0) AS is_starred, fl.name AS folder_name "
                "FROM articles a JOIN feeds f ON f.id = a.feed_id "
                "LEFT JOIN user_actions ua ON ua.article_id = a.id "
                "LEFT JOIN folders fl ON fl.id = ua.folder_id "
                "WHERE COALESCE(a.is_deleted, 0) = 0 "
                "ORDER BY a.published DESC, a.id DESC"
            ))
    except Exception as exc:  # noqa: BLE001
        log.error("读取导出用文章失败：%s", exc)
        return []
