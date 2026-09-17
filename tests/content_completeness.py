"""正文完整性机制的自检（纯 Python，不需要 Node）。

覆盖这套"残文"机制的三层，任何一层坏了都会红：

  1. 判定：feed_parser.looks_truncated() 能认出"正文被截断"，且不误伤正常正文
  2. 表达：抓取方标了 item["content_incomplete"] 时，入库后 content_v 必须是 0
     （= "正文不可信，下次刷新重取"）；没标的必须是当前版本号
  3. 修复：db.save_repaired_content() 只在"确实更完整"时才覆盖，
     拿到"这条没有可取的内容"时要把版本号推进到当前值，避免每轮白试

以及"重取通路"的注册表能被懒加载的平台模块正确登记（platform_of / get_content_refetcher）。

用法：
    python tests/content_completeness.py          # 全在数据库副本上跑，真库不动
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import net  # noqa: F401

import db
import feed_parser

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
    print("== 1. 判定：looks_truncated() ==")
    long_body = "这是一段完整的正文内容，讲了很多事情。" * 5
    cases = [
        ("完整正文", f"<p>{long_body}</p>", False),
        ("结尾带「展开阅读全文」", f"<p>{long_body}展开阅读全文</p>", True),
        ("结尾带「阅读全文」", f"<p>{long_body}阅读全文</p>", True),
        ("英文 read more", f"<p>{'Some content here. ' * 8}Read more</p>", True),
        ("正文中间提到「阅读全文」（不该误判）",
         f"<p>{'正文内容。' * 3}点阅读全文可以看更多，但这里已经是全部了。{'继续正文。' * 8}</p>", False),
        ("太短的内容不判", "<p>短</p>", False),
        ("空", "", False),
    ]
    for label, html, want in cases:
        got = feed_parser.looks_truncated(html)
        check(label, got == want, f"期望 {want}，实际 {got}")

    print("\n== 2. 重取通路的注册表（懒加载也要能查到）==")
    check("platform_of 能解析平台名",
          feed_parser.platform_of("byread://zhihu/people/abc") == "zhihu"
          and feed_parser.platform_of("https://a.com/feed") == "",
          feed_parser.platform_of("byread://zhihu/people/abc"))
    check("未知平台返回 None", feed_parser.get_content_refetcher("no_such_platform") is None)
    refetcher = feed_parser.get_content_refetcher("zhihu")
    check("知乎的重取函数能查到（模块懒加载完成后自动登记）",
          refetcher is not None, getattr(refetcher, "__name__", None))

    print("\n== 3. 表达与修复（在数据库副本上跑）==")
    real = Path(db.DB_PATH)
    tmp = real.parent / "_completeness_test.db"
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(real) + suffix)
        if src.exists():
            shutil.copy2(src, str(tmp) + suffix)
    db.DB_PATH = tmp

    def q(sql, *args):
        conn = sqlite3.connect(tmp)
        try:
            return conn.execute(sql, args).fetchone()[0]
        finally:
            conn.close()

    try:
        feed_id = q("SELECT id FROM feeds LIMIT 1")
        base = {"title": "完整性测试", "summary": "", "link": "https://example.com/a",
                "content": "<p>" + "正文内容。" * 30 + "</p>"}

        db.insert_article(feed_id, dict(base, guid="t:incomplete", content_incomplete=True))
        check("标了 content_incomplete → content_v=0（下次刷新会重取）",
              q("SELECT content_v FROM articles WHERE guid='t:incomplete'") == 0)

        db.insert_article(feed_id, dict(base, guid="t:complete", content_incomplete=False))
        check("没标 → content_v=当前版本",
              q("SELECT content_v FROM articles WHERE guid='t:complete'") == db.CONTENT_VERSION,
              f"CONTENT_VERSION={db.CONTENT_VERSION}")

        # 修复时不能用更差的内容覆盖好内容
        art_id = q("SELECT id FROM articles WHERE guid='t:complete'")
        good = q("SELECT content FROM articles WHERE id=?", art_id)
        worse = "<p>短得多的一段。</p>"
        changed = db.save_repaired_content(art_id, worse, complete=True)
        check("更差的内容不会被写回", changed is False
              and q("SELECT content FROM articles WHERE id=?", art_id) == good)
        check("但会被标记为已确认（不再反复重取）",
              q("SELECT content_v FROM articles WHERE id=?", art_id) == db.CONTENT_VERSION)

        # 更好的内容应当写回
        better = "<p>" + "更完整的正文。" * 200 + '<img src="https://picx.zhimg.com/x.jpg"></p>'
        changed = db.save_repaired_content(art_id, better, complete=True)
        check("更完整的内容会写回", changed is True
              and q("SELECT content FROM articles WHERE id=?", art_id) == better)

        # 不完整的内容写回后仍然保持"待重取"
        art2 = q("SELECT id FROM articles WHERE guid='t:incomplete'")
        db.save_repaired_content(art2, better, complete=False)
        check("标为不完整的正文写回后 content_v 仍是 0",
              q("SELECT content_v FROM articles WHERE id=?", art2) == 0)

        # mark_content_checked：确认"这条没有可取的内容"
        db.mark_content_checked(art2)
        check("mark_content_checked 把版本号推进到当前值",
              q("SELECT content_v FROM articles WHERE id=?", art2) == db.CONTENT_VERSION)

        # get_stale_content_articles 只挑"有正文且版本落后"的
        stale = db.get_stale_content_articles(feed_id, limit=50)
        ids = {a["id"] for a in stale}
        check("待修列表不含已确认的文章", art_id not in ids and art2 not in ids,
              f"待修 {len(stale)} 篇")
    finally:
        conn = sqlite3.connect(tmp)
        conn.execute("DELETE FROM articles WHERE guid LIKE 't:%'")
        conn.commit()
        conn.close()
        db.DB_PATH = real
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(tmp) + suffix)
            if p.exists():
                p.unlink()
        print(f"\n（副本已删除，真库未被动：{real}）")

    print(f"\n结果：通过 {ok} 项，失败 {bad} 项")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
