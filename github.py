"""
github.py —— GitHub 趋势原生支持（不依赖 RSSHub）

用官方搜索接口做"最近新建 + 星标最多"，效果等同于 Trending 页，
而且比抓 HTML 稳（HTML 一改版就废）。
注意：GitHub 搜索接口未认证时限流 10 次/分钟，我们一次刷新只请求一次，足够用。
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("byread.github")

UA = "ByRead/0.1 (local RSS reader)"
TIMEOUT = 10
API = "https://api.github.com/search/repositories"

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "application/vnd.github+json"})


def fetch_trending(limit: int = 20, days: int = 7) -> dict:
    """最近 days 天内新建、按星标排序的仓库。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        resp = _session.get(
            API,
            params={
                "q": f"created:>{since}",
                "sort": "stars",
                "order": "desc",
                "per_page": min(limit, 30),
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("items") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("GitHub 趋势抓取失败：%s", exc)
        raise RuntimeError(f"GitHub 接口访问失败：{exc}") from exc

    entries = []
    for repo in items[:limit]:
        full_name = repo.get("full_name")
        if not full_name:
            continue
        desc = (repo.get("description") or "").strip()
        owner = (repo.get("owner") or {}).get("login")
        stars = repo.get("stargazers_count")
        lang = repo.get("language")
        parts = []
        if desc:
            parts.append(f"<p>{html.escape(desc)}</p>")
        meta = [f"⭐ {stars}"] if stars else []
        if lang:
            meta.append(lang)
        if repo.get("forks_count"):
            meta.append(f"fork {repo['forks_count']}")
        if meta:
            parts.append(f'<p class="stat-line">{html.escape(" · ".join(meta))}</p>')
        parts.append(
            f'<p><a href="{html.escape(repo.get("html_url") or "")}">'
            f'{html.escape(repo.get("html_url") or "")}</a></p>'
        )

        entries.append(
            {
                "guid": f"github:trending:{full_name}",
                "title": f"{full_name}" + (f" — {desc}" if desc else ""),
                "summary": desc[:200],
                "content": "".join(parts),
                "link": repo.get("html_url"),
                "author": owner,
                "published": repo.get("created_at"),
            }
        )

    return {
        "title": "GitHub 每日趋势",
        "site_url": "https://github.com/trending",
        "icon": None,
        "description": "最近一周新建、涨星最快的仓库",
        "entries": entries,
    }
