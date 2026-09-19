"""订阅源编码自检（离线，不联网）。

为什么要有它：**截断读取**会让 feedparser 放弃 XML 声明、改用猜测的编码。
实测人民网的源（声明 UTF-8、标题写在 CDATA 里）截成 256KB 后，
feed.title 变成按 iso-8859-2 解出来的乱码（"时政频道" → "æ—¶æ”¿é¢‘é“…"），
于是"粘贴地址订阅"会把乱码当源名写进库，而且它看着不像占位符，之后再也不会自愈。
（这是 v0.1.15 加"只读开头 256KB 做探测"时引入的回归，v0.1.21 修掉。）

修法：截断的文档先按它自己声明的编码解码，再交给 feedparser（feed_parser._decode_by_declaration）。
这个测试就用合成数据把"截断 → 乱码"和"截断 + 按声明解码 → 正确"两件事钉住，
不依赖任何外部站点，随时可跑：

    python tests/feed_encoding.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedparser  # noqa: E402

import feed_parser  # noqa: E402

ok = bad = 0


def check(label, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1
        print(f"  [OK] {label}" + (f"  {extra}" if extra else ""))
    else:
        bad += 1
        print(f"  [!!] {label}" + (f"  {extra}" if extra else ""))


def build_rss(encoding: str, items: int = 40) -> bytes:
    """造一份"标题在 CDATA 里"的 RSS，条目够多，保证截断点落在文档中间。"""
    body = "".join(
        f"<item><title>第 {i} 条新闻的标题</title>"
        f"<link>https://example.com/{i}</link>"
        f"<description>这是第 {i} 条新闻的摘要内容，写长一点好让文件足够大。</description></item>"
        for i in range(items)
    )
    xml = (f'<?xml version="1.0" encoding="{encoding}" standalone="yes"?>'
           f"<rss version=\"2.0\"><channel>"
           f"<title><![CDATA[时政频道]]></title>"
           f"<description><![CDATA[时政新闻]]></description>"
           f"<link>http://politics.people.cn</link>{body}</channel></rss>")
    return xml.encode(encoding)


def main() -> int:
    print("== 1. 完整文档：无论哪种编码都该解析正确（基线）==")
    for encoding in ("utf-8", "gb2312"):
        raw = build_rss(encoding)
        p = feedparser.parse(raw)
        check(f"{encoding} 完整文档标题正确", p.feed.get("title") == "时政频道",
              f"{p.feed.get('title')!r}（encoding={p.get('encoding')}）")

    print("\n== 2. 截断文档：feedparser 会猜错编码（这就是当初那个 bug）==")
    raw = build_rss("utf-8")
    cut = raw[:len(raw) // 2]          # 从中间截断，CDATA 一定是断的
    p_trunc = feedparser.parse(cut)
    check("截断后直接解析 → 标题确实是坏的（说明这个坑真实存在）",
          p_trunc.feed.get("title") != "时政频道",
          f"得到 {p_trunc.feed.get('title')!r}（encoding={p_trunc.get('encoding')}）")

    print("\n== 3. 我们的修法：截断时先按声明编码解码 ==")
    fixed = feedparser.parse(feed_parser._decode_by_declaration(cut))
    check("截断 + 按声明解码 → 标题正确", fixed.feed.get("title") == "时政频道",
          f"{fixed.feed.get('title')!r}")
    check("条目也还在", len(fixed.entries) > 0, f"{len(fixed.entries)} 条")

    print("\n== 4. 非 UTF-8 的源同样处理 ==")
    gbk = build_rss("gb2312")
    cut_gbk = gbk[:len(gbk) // 2]
    fixed_gbk = feedparser.parse(feed_parser._decode_by_declaration(cut_gbk))
    check("GB2312 源截断后标题也正确", fixed_gbk.feed.get("title") == "时政频道",
          f"{fixed_gbk.feed.get('title')!r}")

    print("\n== 5. 边界情况不能崩 ==")
    check("没有声明编码 → 原样返回字节", feed_parser._decode_by_declaration(b"<rss></rss>") == b"<rss></rss>")
    check("空内容 → 原样返回", feed_parser._decode_by_declaration(b"") == b"")
    weird = b'<?xml version="1.0" encoding="not-a-real-codec"?><rss><title>x</title></rss>'
    check("声明了不存在的编码 → 交回 feedparser（不抛异常）",
          feed_parser._decode_by_declaration(weird) == weird)

    print(f"\n结果：通过 {ok} 项，失败 {bad} 项")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
