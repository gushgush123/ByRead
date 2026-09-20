"""搜索闸门的确定性用例表（第一层门槛）—— 零网络、毫秒级、必须全过

为什么要有这一层：
    第二层（端到端跑 42 条真实样本）依赖 B 站搜索接口是否返回数据，而那个接口会
    **间歇性返回空**（实测「半佛仙人」连续两次 5 条 / 1 条，「_warma_」4 条 / 0 条）。
    净命中率因此在 55%~60% 之间抖 —— 把门槛押在它上面，等于让第三方决定我们红还是绿。
    所以门槛只押在**不依赖上游**的东西上：两个闸门函数本身。

    这一层只调用纯函数：
        rsshub.looks_like_sentence(query)      句式闸门
        rsshub.relevance(query, candidate)     相关度闸门（阈值来自设置项）
    不发任何网络请求，因此可以（也应该）每改一次代码就跑一遍：
        python tests/search_gates.py          # 直接跑
        python tests/search_precision.py --quick   # 同一个东西的快捷入口

用例表分三档：
    must_gate / must_pass   必须成立的期望 —— 不成立就是**失败**（退出码 1）
    known_gap               实测已知的缺陷 —— 只打印、不判失败
                            （把已知缺陷写成"正确期望"= 测试在保护缺陷，不是发现缺陷；
                              放进 known_gap 的效果是每次运行都看得见，修好了再往上挪）
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import net  # noqa: F401  统一网络初始化（只做本地初始化，不发请求）

import rsshub

# --------------------------------------------------------------------------- #
# 句式闸门用例表
# --------------------------------------------------------------------------- #
SENTENCE_CASES: dict[str, list] = {
    # 必须判为"这是一句话/在提问"，从而**不去**跑平台名字搜索
    "must_gate": [
        "今天天气不错",
        "隔壁老王的空间",
        "有没有讲历史的播客",
        "帮我订个每天看新闻的",
        "谁能告诉我该订什么",
        "帮我订阅半佛仙人",          # 真实意图是订阅「半佛仙人」，但不该拿整句去搜
        "我想订阅半佛仙人",
    ],
    # 必须放行（是名字或"平台 + 名字"，不能闸）
    "must_pass": [
        "B站 半佛仙人",
        "半佛仙人",
        "B站 罗翔",
        "李永乐",
        "隔壁老张",
        "少数派",
        "V2EX",
        "半佛仙人的B站",
        "搜一下半佛仙人",
    ],
    # 已知不足（实测）：句子但不含任何标记词 → 闸门拦不住，只能靠相关度闸门兜；
    # 含领域词（主页/空间/频道/账号）的**真名字** → 被误闸。
    "known_gap": [
        ("明天会下雨吗", "句子但不含任何标记词 → 第二道闸门顶不住，实测给出 4 个候选"),
        ("B站 半佛仙人的主页", "含领域词「主页」被误闸"),
        ("B站 半佛仙人 的空间", "含领域词「空间」被误闸"),
        ("QQ空间", "含领域词「空间」被误闸"),
        ("YouTube 某频道", "含领域词「频道」被误闸"),
        ("B站 某账号", "含领域词「账号」被误闸"),
    ],
}

# --------------------------------------------------------------------------- #
# 相关度闸门用例表
#
# (查询, 候选名) 全部是**实测观察到**的真实配对（B 站搜索接口的实际返回），
# 冻结成表后判定就完全不依赖上游。括号里的数字是实测得分。
# --------------------------------------------------------------------------- #
RELEVANCE_CASES: dict[str, list] = {
    # 必须保留（是同一个人 / 明显相关）
    "must_keep": [
        ("半佛仙人", "硬核的半佛仙人"),          # 互相包含
        ("半佛仙人", "半佛仙人"),                # 完全相同
        ("罗翔", "罗翔说刑法"),
        ("半佛仙人的主页", "硬核的半佛仙人"),     # 字符相似度 0.56
        ("订阅半佛仙人", "硬核的半佛仙人"),       # 公共子串比 0.67
        ("数码闲聊站", "大中家电数码闲聊站"),
        ("文元", "中医皮肤科王文元"),
    ],
    # 必须滤掉
    "must_drop": [
        ("清华大学 通知", "清华录取通知书的履行"),
    ],
    # 已知不足（实测）：这两条**必须**靠句式闸门拦，相关度拦不住 ——
    # 「半佛仙人」⊂「硬核的半佛仙人」与「Steam 更新」⊂「浪子秀白steam游戏更新」
    # 在数学上同构，任何"能放行前者"的阈值都会放行后者。
    "known_gap": [
        ("Steam 更新", "浪子秀白steam游戏更新", "公共子串比 0.71 / 相似度 0.54，两道阈值都放行"),
        ("隔壁老王的空间", "隔壁空间站的老王", "相似度 0.88 —— 只能靠句式闸门拦，相关度拦不住"),
    ],
}


def _counts() -> tuple[int, int, int, int, int, int]:
    return (len(SENTENCE_CASES["must_gate"]), len(SENTENCE_CASES["must_pass"]),
            len(SENTENCE_CASES["known_gap"]), len(RELEVANCE_CASES["must_keep"]),
            len(RELEVANCE_CASES["must_drop"]), len(RELEVANCE_CASES["known_gap"]))


def run() -> tuple[bool, list[str]]:
    """
    跑一遍确定性门槛。返回 (是否全过, 报告行)。
    不发网络请求；只用 rsshub 里的两个纯函数。
    """
    lines: list[str] = []
    fails: list[str] = []

    sg, sp, sk, rk, rd, rg = _counts()
    lines.append("第一层：确定性门槛（纯函数，零网络）")
    lines.append("=" * 96)
    lines.append(f"句式闸门 looks_like_sentence()：{sg + sp + sk} 条"
                 f"（must_gate {sg} / must_pass {sp} / known_gap {sk}）")
    lines.append(f"相关度闸门 relevance()：{rk + rd + rg} 条"
                 f"（must_keep {rk} / must_drop {rd} / known_gap {rg}）")
    lines.append("")

    for name in SENTENCE_CASES["must_gate"]:
        got = bool(rsshub.looks_like_sentence(name))
        ok = got
        lines.append(f"  {'✅' if ok else '❌'} [应闸]   {name!r:28} → looks_like_sentence={got}")
        if not ok:
            fails.append(f"句式闸门漏放：{name!r} 应判为句子，实际 False")

    for name in SENTENCE_CASES["must_pass"]:
        got = bool(rsshub.looks_like_sentence(name))
        ok = not got
        lines.append(f"  {'✅' if ok else '❌'} [应放行] {name!r:28} → looks_like_sentence={got}")
        if not ok:
            fails.append(f"句式闸门误闸：{name!r} 应放行，实际 True")

    lines.append("")
    lines.append("  相关度闸门：")
    for q, cand in RELEVANCE_CASES["must_keep"]:
        keep, score, why = rsshub.relevance(q, cand)
        lines.append(f"  {'✅' if keep else '❌'} [应保留] {q!r:16} × {cand!r:22}"
                     f" → keep={keep} {score:.2f}（{why}）")
        if not keep:
            fails.append(f"相关度闸门误滤：{q!r} × {cand!r} 应保留，实际被滤（{why}）")

    for q, cand in RELEVANCE_CASES["must_drop"]:
        keep, score, why = rsshub.relevance(q, cand)
        lines.append(f"  {'✅' if not keep else '❌'} [应滤掉] {q!r:16} × {cand!r:22}"
                     f" → keep={keep} {score:.2f}（{why}）")
        if keep:
            fails.append(f"相关度闸门漏放：{q!r} × {cand!r} 应被滤掉，实际保留")

    lines.append("")
    lines.append("  已知缺口 known_gap（只打印，不判失败）：")
    for name, note in SENTENCE_CASES["known_gap"]:
        got = bool(rsshub.looks_like_sentence(name))
        lines.append(f"    ⚠ 句式 {name!r:22} → looks_like_sentence={got}"
                     f"（{note}）")
        if (name in ("明天会下雨吗",) and got) or \
           (name not in ("明天会下雨吗",) and not got):
            lines.append("       ↑ 该缺口看起来已经修好，请把它挪进 must_gate / must_pass")
    for q, cand, note in RELEVANCE_CASES["known_gap"]:
        keep, score, why = rsshub.relevance(q, cand)
        lines.append(f"    ⚠ 相关度 {q!r:14} × {cand!r:20} → keep={keep} "
                     f"{score:.2f}（{why}；{note}）")
        if not keep:
            lines.append("       ↑ 该缺口看起来已经修好，请把它挪进 must_drop")

    lines.append("")
    if fails:
        lines.append(f"❌ 第一层未过 {len(fails)} 条：")
        for f in fails:
            lines.append(f"   - {f}")
    else:
        lines.append("✅ 第一层全过（must_gate / must_pass / must_keep / must_drop 全部符合期望）")
    return (not fails), lines


def main() -> int:
    started = time.time()
    ok, lines = run()
    print("\n".join(lines))
    print(f"\n耗时 {time.time() - started:.3f} 秒（零网络）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
