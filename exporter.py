"""
exporter.py —— 白读 · ByRead 数据导出（自用保险）

两个用途：
    1. 把清洗过的正文 HTML 转成 Markdown（`html_to_markdown`）
    2. 每篇文章一个 .md，打包成 zip（`build_markdown_zip`）

设计要点：
    1. **这里不碰数据库、不碰网络、不碰 Cookie**。输入是 db 层取好的行字典，输出是字符串/字节。
       导出内容里只会有订阅源、文章、收藏夹、频道这些数据，登录信息在 settings 表里，
       压根不会经过这个模块（导出白名单见 db.EXPORT_TABLES）。
    2. HTML→Markdown 是给**人看的存档**，不追求完美还原：段落、标题、列表、引用、代码块、
       链接、图片、表格这些常见结构转对就够了，认不出来的标签一律"去壳留字"。
       转不出来（解析炸了）时返回空串，调用方会退回纯摘要文本 —— 绝不抛异常打断导出。
    3. 文件名要能在 Windows 上落地：去掉 \\ / : * ? " < > | 和控制字符，首尾的点与空格也去掉
       （Windows 不允许文件名以点结尾），超长截断；重名自动加 -2、-3。
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from typing import Optional

from lxml import html as lxml_html

log = logging.getLogger("byread.export")

# Windows 文件名禁用字符
_BAD_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')
# 控制字符（含换行/制表）换成空格，比换成下划线可读
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_MULTI_BLANK = re.compile(r"\n{3,}")
# <br> 的占位符：正文里出现的换行要留着，源码排版用的换行要压掉，靠它区分
_BR_MARKER = "\x00br\x00"

# 当成"块级"处理的标签
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_CONTAINERS = {"div", "section", "article", "main", "body", "html", "center",
               "figure", "figcaption", "header", "footer", "aside", "nav", "form"}
_INLINE_PASSTHROUGH = {"span", "u", "mark", "small", "sub", "sup", "kbd", "samp",
                       "font", "abbr", "cite", "q", "time", "ins", "label"}


# --------------------------------------------------------------------------- #
# 文件名
# --------------------------------------------------------------------------- #
def safe_filename(name: str, fallback: str = "article", max_len: int = 60) -> str:
    """把标题变成能安全落地的文件名（不含扩展名）。"""
    cleaned = _CONTROL_CHARS.sub(" ", name or "")
    cleaned = _BAD_FILENAME_CHARS.sub("_", cleaned.strip())
    cleaned = _collapse(cleaned).strip(" .")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    return cleaned or fallback


def _collapse(text: str) -> str:
    """压掉多余空白，变成一行（标题、列表项、表格单元格用）。"""
    return _WHITESPACE.sub(" ", (text or "").replace(_BR_MARKER, " ")).strip()


def _collapse_keep_breaks(text: str) -> str:
    """
    压掉多余空白，但**保留 <br> 造成的换行**。

    正文里的 <br> 是有意义的（歌词、地址、微博的分行），不能压成空格。
    但 HTML 源码为了排版也会有换行（<p> 里长句子换行写很常见），那不是语义换行，
    必须一起压掉 —— 所以 <br> 在渲染时先变成占位符，压平所有换行之后，
    再把占位符换成 Markdown 的硬换行（行尾两个空格 + 换行）。
    """
    if not text:
        return ""
    segments = [_collapse(seg) for seg in text.split(_BR_MARKER)]
    return _MULTI_BLANK.sub("\n\n", "  \n".join(segments)).strip()


# --------------------------------------------------------------------------- #
# HTML → Markdown
# --------------------------------------------------------------------------- #
def _inline_html(el) -> str:
    """元素内部的行内内容（含子元素及其 tail）。"""
    out = [el.text or ""]
    for child in el:
        if isinstance(child.tag, str):
            out.append(_inline_node(child))
        out.append(child.tail or "")
    return "".join(out)


def _inline_node(el) -> str:
    """单个行内元素 → Markdown（不含它的 tail）。"""
    tag = el.tag.lower()
    if tag in ("strong", "b"):
        inner = _inline_html(el).strip()
        return f"**{inner}**" if inner else ""
    if tag in ("em", "i"):
        inner = _inline_html(el).strip()
        return f"*{inner}*" if inner else ""
    if tag in ("del", "s", "strike"):
        inner = _inline_html(el).strip()
        return f"~~{inner}~~" if inner else ""
    if tag == "code":
        inner = _inline_html(el).replace("`", "'")
        return f"`{inner}`" if inner.strip() else ""
    if tag == "a":
        href = (el.get("href") or "").strip()
        label = _inline_html(el).strip() or href
        return f"[{label}]({href})" if href else label
    if tag == "img":
        src = (el.get("src") or "").strip()
        if not src:
            return ""
        alt = (el.get("alt") or "").strip()
        return f"![{alt}]({src})"
    if tag == "br":
        return _BR_MARKER      # 占位符，最后统一换成 Markdown 的硬换行
    if tag == "hr":
        return "\n\n---\n\n"
    if tag == "input" or tag == "col" or tag == "colgroup":
        return ""
    # 认不出来的（span/u/mark/font…）：去壳留字
    return _inline_html(el)


def _render_table(table) -> str:
    """表格 → Markdown 管道表（第一行当表头）。"""
    rows: list[list[str]] = []
    for tr in table.iter("tr"):
        cells = []
        for cell in tr:
            if isinstance(cell.tag, str) and cell.tag.lower() in ("td", "th"):
                text = _inline_html(cell).strip()
                cells.append(_collapse(text).replace("|", "\\|"))
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |",
             "| " + " | ".join("---" for _ in range(width)) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _render_list(el) -> str:
    """ul / ol → Markdown 列表（嵌套列表缩进两格）。"""
    ordered = el.tag.lower() == "ol"
    lines: list[str] = []
    index = 0
    for li in el:
        if not (isinstance(li.tag, str) and li.tag.lower() == "li"):
            continue
        index += 1
        marker = f"{index}. " if ordered else "- "
        inline_parts = [li.text or ""]
        nested: list[str] = []
        for child in li:
            if isinstance(child.tag, str) and child.tag.lower() in ("ul", "ol"):
                nested.append(_render_block(child))
            else:
                inline_parts.append(_inline_node(child) if isinstance(child.tag, str) else "")
            inline_parts.append(child.tail or "")
        body = _collapse("".join(inline_parts)).strip()
        lines.append(marker + body)
        for sub in nested:
            lines.extend("  " + line if line.strip() else "" for line in sub.splitlines())
    return "\n".join(lines)


def _render_block(el) -> str:
    """块级元素 → Markdown 段落。"""
    tag = el.tag.lower() if isinstance(el.tag, str) else ""
    if tag in _HEADINGS:
        text = _collapse(_inline_html(el)).strip()
        return f"{'#' * int(tag[1])} {text}" if text else ""
    if tag == "p":
        return _collapse_keep_breaks(_inline_html(el))
    if tag == "hr":
        return "---"
    if tag == "pre":
        code = el.text_content().strip("\n")
        return f"```\n{code}\n```" if code.strip() else ""
    if tag in ("ul", "ol"):
        return _render_list(el)
    if tag == "blockquote":
        inner = _render_children(el)
        return "\n".join(("> " + line).rstrip() if line.strip() else ">" for line in inner.splitlines())
    if tag == "table":
        return _render_table(el)
    if tag == "dl":
        lines = []
        for child in el:
            if not isinstance(child.tag, str):
                continue
            text = _collapse(_inline_html(child)).strip()
            if child.tag.lower() == "dt":
                lines.append(f"**{text}**")
            elif text:
                lines.append(f": {text}")
        return "\n".join(lines)
    if tag == "li":
        return "- " + _collapse(_inline_html(el)).strip()
    if tag in _CONTAINERS:
        return _render_children(el)
    if tag in _INLINE_PASSTHROUGH:
        return _collapse(_inline_html(el)).strip()
    # 其他（img、a、strong…）：当行内处理
    return _inline_node(el).strip()


def _render_children(el) -> str:
    """把一个容器里的所有块级子元素渲染成 Markdown（块之间空一行）。"""
    blocks: list[str] = []
    if el.text and el.text.strip():
        blocks.append(_collapse(el.text).strip())
    for child in el:
        if isinstance(child.tag, str):
            rendered = _render_block(child)
            if rendered.strip():
                blocks.append(rendered)
        if child.tail and child.tail.strip():
            blocks.append(_collapse(child.tail).strip())
    return "\n\n".join(blocks)


def html_to_markdown(html_text: Optional[str]) -> str:
    """
    清洗过的正文 HTML → Markdown。只为"存档可读"服务，认不出的结构一律去壳留字。
    解析失败返回空串（调用方退回纯文本摘要），不抛异常。
    """
    if not html_text:
        return ""
    try:
        parser = lxml_html.HTMLParser(encoding="utf-8", recover=True)
        root = lxml_html.fromstring(f"<div>{html_text}</div>", parser=parser)
        markdown = _render_children(root)
    except Exception as exc:  # noqa: BLE001
        log.info("HTML 转 Markdown 失败（退回纯文本）：%s", exc)
        return ""
    return _MULTI_BLANK.sub("\n\n", markdown).strip()


# --------------------------------------------------------------------------- #
# 一篇文章 → Markdown
# --------------------------------------------------------------------------- #
def article_to_markdown(article: dict) -> str:
    """
    文章 → Markdown。头部是元信息（标题 / 来源 / 作者 / 时间 / 原文链接 / 收藏夹 / 音频），
    下面是正文；没有正文就退回摘要纯文本。字段全部来自 db，不含任何登录信息。
    """
    title = (article.get("title") or "(无标题)").strip()
    lines = [f"# {title}", ""]

    meta = [("来源", article.get("feed_title"))]
    if article.get("author"):
        meta.append(("作者", article.get("author")))
    meta.append(("时间", article.get("published")))
    if article.get("link"):
        meta.append(("原文", article.get("link")))
    if article.get("folder_name"):
        meta.append(("收藏夹", article.get("folder_name")))
    if article.get("audio_url"):
        meta.append(("音频", article.get("audio_url")))
    for label, value in meta:
        if value:
            lines.append(f"- **{label}**：{value}")

    lines += ["", "---", ""]

    body = html_to_markdown(article.get("content")) if article.get("content") else ""
    if not body.strip():
        body = (article.get("summary") or "").strip()
    lines.append(body or "_（这篇文章没有抓到正文，只有上面的信息）_")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 打包 zip
# --------------------------------------------------------------------------- #
def build_markdown_zip(articles: list[dict], folder: str = "byread-articles") -> bytes:
    """
    每篇文章一个 .md，打包成 zip 返回字节。

    文件名形如 `2026-09-16_少数派_标题.md`；同一来源同一天同名文章会自动加 -2、-3。
    单篇转换失败不会中断整个导出（日志有记录，正文位置会写明原因）。
    """
    buffer = io.BytesIO()
    used: dict[str, int] = {}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for article in articles:
            try:
                markdown = article_to_markdown(article)
            except Exception as exc:  # noqa: BLE001
                log.error("导出文章 %s 失败：%s", article.get("id"), exc)
                markdown = (f"# {article.get('title') or '(无标题)'}\n\n"
                            f"_（这篇导出失败：{exc}）_\n")

            published = str(article.get("published") or "")
            day = published[:10] if len(published) >= 10 else "无日期"
            stem = "_".join(part for part in (
                day,
                safe_filename(article.get("feed_title") or "", "未知来源", 20),
                safe_filename(article.get("title") or "", "无标题", 60),
            ) if part)

            # 重名（同一天同一来源同一标题）加序号，避免互相覆盖
            key = stem.lower()
            used[key] = used.get(key, 0) + 1
            if used[key] > 1:
                stem = f"{stem}-{used[key]}"

            zf.writestr(f"{folder}/{stem}.md", markdown)

        # 一篇文章都没有时也要给一个能打开的包，而不是 0 字节的坏文件
        if not articles:
            zf.writestr(f"{folder}/（没有文章）.md",
                        "# 没有可导出的文章\n\n数据库里还没有文章，或者文章都被删除了。\n")
    return buffer.getvalue()
