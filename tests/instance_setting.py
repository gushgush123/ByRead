"""「订阅服务实例」设置的校验与自动回退自检。

背景（真实踩过）：这个字段以前是**原样存下来、原样使用**的。有人把订阅地址
（http://www.people.com.cn/rss/politics.xml）填进了这里 ——
于是 V2EX / 豆瓣 / 36氪 这三个"需要拼路由"的预置源全部订阅失败，
而错误信息只是一句"找不到"，完全看不出是设置填错了。

现在有两道保险，这个测试把它们钉住：
  1. 保存时：明显填错（订阅地址 / 不是 http(s)）→ 直接拒绝并说明原因
  2. 使用时：手填值也要先探一下是否真能用；不能用就改用自动探测（记一条 warning）

用法（需要应用正在运行时才会顺带测保存接口那一段）：
    python tests/instance_setting.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import net  # noqa: F401

import db
import rsshub

ok = bad = 0


def check(label, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1
        print(f"  [OK] {label}" + (f"  {extra}" if extra else ""))
    else:
        bad += 1
        print(f"  [!!] {label}" + (f"  {extra}" if extra else ""))


def main() -> int:
    print("== 1. 形状校验（不需要联网）==")
    for label, value, want in [
        ("留空 = 自动探测", "", True),
        ("订阅地址 .xml（就是踩过的那个坑）",
         "http://www.people.com.cn/rss/politics.xml", False),
        ("订阅地址 /feed", "https://example.com/feed", False),
        ("订阅地址 .rss", "https://example.com/a.rss", False),
        ("不是网址", "rsshub.app", False),
        ("空协议", "ftp://example.com", False),
    ]:
        got, message = rsshub.validate_instance(value)
        check(label, got == want, f"{'可用' if got else '拒绝'}：{message[:44]}")

    print("\n== 2. 运行期保险：存了坏地址也不能让拼路由的源失效 ==")
    original = db.get_setting("rsshub_instance")
    try:
        db.set_setting("rsshub_instance", "https://this-instance-does-not-exist-xyz.invalid")
        got = rsshub.resolve_instance()
        check("不会拿这个坏地址去拼 URL", got != "https://this-instance-does-not-exist-xyz.invalid",
              f"返回 {got!r}")
        check("要么换成可用实例、要么诚实返回 None（公共实例可能都不可用）",
              got is None or got.startswith("http"))
    finally:
        db.set_setting("rsshub_instance", original or "")
        print(f"  （已恢复原值：{original!r}）")

    print("\n== 3. 保存接口（应用在运行时才有意义）==")
    base = "http://127.0.0.1:5000"
    try:
        import requests

        requests.get(base + "/api/counts", timeout=5)
    except Exception:
        print("  跳过：应用没在运行（先 python app.py 再跑本测试）")
        print(f"\n结果：通过 {ok} 项，失败 {bad} 项")
        return 0 if bad == 0 else 1

    import requests

    before = db.get_setting("rsshub_instance") or ""
    try:
        r = requests.post(base + "/api/settings",
                          json={"rsshub_instance": "http://www.people.com.cn/rss/politics.xml"},
                          timeout=30)
        check("填订阅地址 → 拒绝保存（400）", r.status_code == 400, f"HTTP {r.status_code}")
        check("拒绝原因说得清楚", "实例" in (r.json().get("error") or ""),
              (r.json().get("error") or "")[:50])
        check("拒绝后库里的值没被改坏",
              (db.get_setting("rsshub_instance") or "") == before)

        r = requests.post(base + "/api/settings", json={"rsshub_instance": ""}, timeout=30)
        check("清空（= 自动探测）能被接受", r.status_code == 200, f"HTTP {r.status_code}")
    finally:
        requests.post(base + "/api/settings", json={"rsshub_instance": before}, timeout=30)
        print(f"  （已恢复原值：{before!r}）")

    print(f"\n结果：通过 {ok} 项，失败 {bad} 项")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
