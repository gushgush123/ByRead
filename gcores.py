"""
gcores.py —— 机核原生支持（不依赖 RSSHub）

背景：机核的 RSS（www.gcores.com/rss）会间歇性被 WAF 拦掉，返回一个 15KB 的反爬页面，
表现为"有时能抓到、有时一条都没有"。实测它的官方 JSON API 一直稳定，所以改成原生实现。

接口（JSON:API 格式）：
    /gapi/v1/articles?page[limit]=20&sort=-published-at   文章
    /gapi/v1/radios?page[limit]=20&sort=-published-at     电台
    正文放在 attributes.content 里，是一段 Draft.js 风格的 blocks JSON。
"""

from __future__ import annotations

import html
import json
import logging
from typing import Optional

import requests

log = logging.getLogger("byread.gcores")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 10
API = "https://www.gcores.com/gapi/v1"
IMAGE_CDN = "https://image.gcores.com/"
MAX_BLOCKS = 600          # 长篇小说级文章有 1000+ 段，截断避免数据库膨胀
MAX_CONTENT_CHARS = 80000

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "application/vnd.api+json"})


def _blocks_to_html(raw: Optional[str]) -> str:
    """把 Draft.js 的 blocks JSON 转成安全 HTML。"""
    if not raw:
        return ""
    try:
        blocks = json.loads(raw).get("blocks") or []
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(blocks, list):
        return ""

    parts: list[str] = []
    in_list = False
    for block in blocks[:MAX_BLOCKS]:
        if not isinstance(block, dict):
            continue
        text = str(block.get("text") or "").strip()
        btype = block.get("type") or "unstyled"
        if btype == "atomic":
            continue
        if not text:
            continue
        safe = html.escape(text)
        if btype == "unordered-list-item":
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append(f"<li>{safe}</li>")
            continue
        if in_list:
            parts.append("</ul>")
            in_list = False
        if btype == "header-one":
            parts.append(f"<h2>{safe}</h2>")
        elif btype == "header-two":
            parts.append(f"<h3>{safe}</h3>")
        elif btype in ("header-three", "header-four"):
            parts.append(f"<h4>{safe}</h4>")
        elif btype == "blockquote":
            parts.append(f"<blockquote><p>{safe}</p></blockquote>")
        elif btype == "code-block":
            parts.append(f"<pre><code>{safe}</code></pre>")
        else:
            parts.append(f"<p>{safe}</p>")
    if in_list:
        parts.append("</ul>")

    result = "".join(parts)
    return result[:MAX_CONTENT_CHARS]


def _image_url(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    name = str(name).strip()
    if name.startswith("http"):
        return name
    return IMAGE_CDN + name


def _fetch_kind(kind: str, count: int) -> list[dict]:
    try:
        resp = _session.get(
            f"{API}/{kind}",
            params={"page[limit]": count, "sort": "-published-at"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("data") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("机核 %s 抓取失败：%s", kind, exc)
        return []


def fetch_gcores(limit: int = 20) -> dict:
    """文章 + 电台合并，按发布时间倒序。"""
    per_kind = max(5, limit)
    raw_items: list[tuple[str, dict]] = []
    for kind in ("articles", "radios"):
        for item in _fetch_kind(kind, per_kind):
            raw_items.append((kind, item))

    if not raw_items:
        raise RuntimeError("机核接口暂时不可用")

    entries = []
    for kind, item in raw_items:
        attrs = item.get("attributes") or {}
        title = (attrs.get("title") or "").strip()
        item_id = item.get("id")
        if not title or not item_id:
            continue

        cover = _image_url(attrs.get("thumb") or attrs.get("cover"))
        desc = (attrs.get("desc") or attrs.get("excerpt") or "").strip()
        body = _blocks_to_html(attrs.get("content"))
        parts = []
        if cover:
            parts.append(f'<p><img src="{html.escape(cover)}" alt="" loading="lazy" '
                         f'referrerpolicy="no-referrer"></p>')
        if body:
            parts.append(body)

        meta = []
        if kind == "radios":
            meta.append("电台")
            if attrs.get("duration"):
                meta.append(f"{int(attrs['duration']) // 60} 分钟")
        if attrs.get("likes-count"):
            meta.append(f"赞 {attrs['likes-count']}")
        if attrs.get("comments-count"):
            meta.append(f"评论 {attrs['comments-count']}")
        if meta:
            parts.append(f'<p class="stat-line">{html.escape(" · ".join(meta))}</p>')

        label = "电台" if kind == "radios" else "文章"
        entries.append(
            {
                "guid": f"gcores:{kind}:{item_id}",
                "title": f"[{label}] {title}",
                "summary": desc or title,
                "content": "".join(parts) or None,
                "link": f"https://www.gcores.com/{kind}/{item_id}",
                "author": "机核",
                "published": attrs.get("published-at") or attrs.get("created-at"),
            }
        )

    # 机核的接口分两次取，合并后要重新排序（app 层还会按入库时间去重）
    entries.sort(key=lambda e: str(e.get("published") or ""), reverse=True)
    return {
        "title": "机核",
        "site_url": "https://www.gcores.com",
        "icon": None,
        "description": "游戏、电台与亚文化（原生接口）",
        "entries": entries[:limit],
    }
