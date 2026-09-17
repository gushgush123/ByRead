"""图片域名自检：确认"需要 Referer 才给图"的图床都在代理白名单里。

为什么要有这个测试（真实踩过的坑）：
    cdnfile.sspai.com 只要求"有 Referer"——带任意 Referer 都 200，完全不带就 403。
    而阅读页给所有图片加了 referrerpolicy="no-referrer"，于是少数派的配图全挂（整页白框）。
    这类问题不会报错、不会进日志，只会"图片不显示"，很难自己发现；
    而图床域名又随订阅源变化，所以做成一个可以随时重跑的检查。

做法：
    1. 扫描库里所有文章的正文，收集出现过的图片域名；
    2. 每个域名取一张真实的图，分别用"不带 Referer"和"带该站点 Referer"请求一次；
    3. 只要出现"不带 403、带 200"，就说明这个域名必须走本地代理 ——
       不在 app.IMAGE_PROXY_HOSTS 里就判定失败（退出码 1）。

用法（需要先跑过应用、库里有文章）：
    python tests/image_hosts.py            # 只检查，报告问题
    python tests/image_hosts.py --all      # 把每个域名的实测结果都打出来
"""
from __future__ import annotations

import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import net  # noqa: F401  统一网络初始化（和主程序一致）


def collect_hosts(limit_per_host: int = 1) -> dict[str, str]:
    """从库里收集 {域名: 一个真实图片地址}。"""
    import db

    hosts: dict[str, str] = {}
    counts: dict[str, int] = defaultdict(int)
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT content FROM articles WHERE COALESCE(content, '') LIKE '%<img%'"
    ).fetchall()
    conn.close()
    for row in rows:
        for url in re.findall(r'<img[^>]+src="([^"]+)"', row["content"] or "", re.I):
            if not url.lower().startswith(("http://", "https://")):
                continue
            host = (urlparse(url).hostname or "").lower()
            if not host:
                continue
            counts[host] += 1
            if counts[host] <= limit_per_host:
                hosts.setdefault(host, url)
    return hosts


def fetch_status(url: str, referer: str | None) -> str:
    import requests

    import feed_parser

    headers = {"User-Agent": feed_parser.UA}
    if referer:
        headers["Referer"] = referer
    try:
        resp = requests.get(url, timeout=10, headers=headers, stream=True)
        resp.raw.read(1024, decode_content=True)
        resp.close()
        return str(resp.status_code)
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__


def main() -> int:
    import app

    show_all = "--all" in sys.argv
    hosts = collect_hosts()
    if not hosts:
        print("库里还没有带图片的文章，跳过（等抓到文章后再跑）。")
        return 0

    whitelist = tuple(h.lower() for h in app.IMAGE_PROXY_HOSTS)

    def proxied(host: str) -> bool:
        return any(host == h or host.endswith("." + h) for h in whitelist)

    print(f"库里共 {len(hosts)} 个图片域名，白名单 {len(whitelist)} 条：{', '.join(whitelist)}\n")
    print(f"{'域名':<36}{'不带Referer':<14}{'带站点Referer':<15}{'走代理':<8}判定")
    print("-" * 92)

    problems: list[str] = []
    checked = 0
    for host, url in sorted(hosts.items()):
        site = "https://" + ".".join(host.split(".")[-2:])
        no_ref = fetch_status(url, None)
        with_ref = fetch_status(url, site + "/")
        checked += 1
        is_proxied = proxied(host)
        if no_ref != "200" and with_ref == "200":
            verdict = "OK（已走代理）" if is_proxied else "!! 需要 Referer 但没走代理 —— 图片会挂"
            if not is_proxied:
                problems.append(f"{host}（参考 Referer：{site}/）")
        elif no_ref == "200":
            verdict = "不需要代理" + ("（仍走代理，无害）" if is_proxied else "")
        else:
            verdict = f"两种都取不到（{no_ref}/{with_ref}），跳过"
        if show_all or "!!" in verdict:
            print(f"{host:<36}{no_ref:<14}{with_ref:<15}{'是' if is_proxied else '否':<8}{verdict}")

    print("-" * 92)
    if problems:
        print(f"\n发现 {len(problems)} 个域名需要 Referer 却不在 IMAGE_PROXY_HOSTS 里：")
        for p in problems:
            print("   -", p)
        print("\n修法：把它加进 app.py 的 IMAGE_PROXY_HOSTS 与 IMAGE_REFERERS "
              "（前端名单由 base.html 的 meta 标签自动同步，不用改前端）。")
        return 1

    print(f"\n检查了 {checked} 个域名：需要 Referer 的都已经走本地代理，没有问题。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
