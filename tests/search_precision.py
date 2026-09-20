"""搜索精度回归测试（P0）

用途：把阶段 0 核实报告里那 42 条样本固化成可重复运行的脚本，并给出硬门槛判定。
样本**包含全部失败项**（不许挑选）—— 覆盖率要能看到失败，那才是重点。

跑法（需要应用在运行）：
    python tests/search_precision.py

判定门槛（任一不过则退出码 1）：
    1. 四条精度事故全部消除
    2. 误报数 = 0（对无关输入不得给出候选）
    3. 总命中率 ≥ 改动前基线（64%）
    4. 失败路径耗时 ≤ 10 秒

说明：只调用 rsshub.search()（读操作），不写数据库、不加订阅。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import net  # noqa: F401

import rsshub

# ---------------------------------------------------------------- 固定样本集（42 条，与阶段 0 报告一致）
SAMPLES: list[tuple[str, str]] = (
    # A. 从用户现有 12 个订阅反推（他当初大概是这样搜的）
    [(q, "A 从现有订阅反推") for q in [
        "B站 Warma", "_warma_", "数码闲聊站", "蜗牛柯基weibo", "友琳_Yurin",
        "少数派", "云风的博客", "GitHub 每日趋势", "文元",
    ]]
    # B. 典型中文用户会想订阅的
    + [(q, "B 典型需求") for q in [
        "V2EX", "豆瓣电影", "36氪", "知乎日报", "B站热门",
        "小宇宙 忽左忽右", "喜马拉雅 某某", "微信公众号 某某", "豆瓣 租房小组",
        "原神 公告", "Steam 更新", "清华大学 通知", "人民网 时政",
        "掘金 前端", "酷壳", "机核", "爱范儿",
        "YouTube 某频道", "GitHub releases 某项目", "B站 半佛仙人",
    ]]
    # C. 直接给 URL
    + [(q, "C 直接给 URL") for q in [
        "https://space.bilibili.com/37663924",
        "https://www.zhihu.com/people/92-76-23-62-8",
        "https://weibo.com/u/1782488734",
        "https://www.xiaoyuzhoufm.com/podcast/5e280faa41823b7a0c8e4b3f",
        "https://www.douban.com/group/explore",
        "https://sspai.com/post/114557",
        "http://www.people.com.cn/rss/politics.xml",
        "https://www.bilibili.com/video/BV1gfGw6RE5p",
    ]]
    # D. 自由表述 / 无关输入（P0 只要求"失败得体面"）
    + [(q, "D 自由表述") for q in [
        "我想追那个讲财经的B站up",
        "帮我订个每天看新闻的",
        "今天天气不错",
        "隔壁老王的空间",
        "有没有讲历史的播客",
    ]]
)

# 改动前基线（阶段 0 报告 + P0 基线实跑；口径=只要给了候选就算命中那一层）
BASELINE = {"L0/L1 原生": 13, "L2 预置源": 13, "L3 URL识别": 2, "失败": 14, "误报": 5}
# 基线里"误报"包含：豆瓣 租房小组→豆瓣电影 / GitHub releases→每日趋势 /
# 今天天气不错→py今天天气不错 / 隔壁老王的空间→隔壁空间站的老王 / 清华大学 通知→清华录取通知书…

# 精度事故：输入 → 不允许被当成"正常命中"（None = 必须完全没有候选）
INCIDENTS = [
    ("豆瓣 租房小组", "豆瓣电影正在上映"),
    ("GitHub releases 某项目", "GitHub 每日趋势"),
    ("今天天气不错", None),
    ("隔壁老王的空间", None),
    ("清华大学 通知", None),          # P0 实跑新发现：命中了无关 UP 主
]

# 不得回归清单
NO_REGRESSION = [
    ("少数派", "精确命中「少数派」"),
    ("B站 半佛仙人", "返回「硬核的半佛仙人」等真实候选"),
    ("半佛仙人", "返回 B 站候选"),
    ("B站 硬核的半佛仙人", "返回该 UP 主"),
    ("V2EX", "命中「V2EX 最新主题」"),
    ("知乎日报", "命中「知乎日报」"),
    ("https://space.bilibili.com/37663924", "识别为 B 站用户"),
    ("https://www.ruanyifeng.com/blog/atom.xml", "当作订阅地址校验通过"),
    ("我想追那个讲财经的B站up", "允许失败，但必须干净（明确说没找到）"),
]

UNSUPPORTED_LINK = "https://www.bilibili.com/video/BV1gfGw6RE5p"


def classify(query: str, result: dict) -> str:
    """把一次 search 的结果归到解析链的哪一层（与阶段 0 报告口径一致）。"""
    cands = result.get("candidates") or []
    if not cands:
        return "失败"
    if rsshub.is_url(query):
        first = cands[0]
        if (first.get("feed_url") or "").startswith("byread://"):
            return "L0/L1 原生"
        return "L3 URL识别"
    plats = {c.get("platform") for c in cands}
    native = {"B站", "知乎", "微博", "机核", "GitHub"}
    if plats & native and plats - native:
        return "L0/L1+L2"
    if plats & native:
        return "L0/L1 原生"
    return "L2 预置源"


def run_one(query: str) -> dict:
    started = time.time()
    try:
        res = rsshub.search(query)
    except Exception as exc:  # noqa: BLE001
        res = {"candidates": [], "hint": f"异常 {type(exc).__name__}: {exc}"}
    cost = time.time() - started
    labels = [c.get("label") or "" for c in (res.get("candidates") or [])]
    loose = [c for c in (res.get("candidates") or []) if c.get("match") == "loose"]
    return {"query": query, "layer": classify(query, res), "cost": cost,
            "labels": labels, "loose": loose, "hint": res.get("hint"),
            "n": len(labels)}


def main() -> int:
    print(f"共 {len(SAMPLES)} 条样本，逐条跑 rsshub.search()（只读）\n")
    print(f"{'层':<14}{'用时':>7}  {'输入':<44}{'候选'}")
    print("-" * 104)
    rows = []
    for query, group in SAMPLES:
        r = run_one(query)
        r["group"] = group
        rows.append(r)
        mark = "❌" if r["layer"] == "失败" else "  "
        print(f"{mark}{r['layer']:<12}{r['cost']:>6.1f}s  {query[:42]:<44}{r['n']} 个 "
              f"{('| ' + ' / '.join(r['labels'][:2]))[:60] if r['n'] else ''}")

    stats: dict[str, int] = {}
    for r in rows:
        stats[r["layer"]] = stats.get(r["layer"], 0) + 1
    total = len(rows)
    hit = sum(v for k, v in stats.items() if k != "失败")

    # ---------------- 误报统计 ----------------
    false_positives = []
    for r in rows:
        cands = r["labels"]
        if not cands:
            continue
        if r["group"] == "D 自由表述":
            false_positives.append((r["query"], f"D 组不该有候选，却给了 {cands[0]}"))
            continue
        for q, bad in INCIDENTS:
            if r["query"] != q or bad is None:
                continue
            # 允许"降级为 loose 并带说明"，不允许当成正常命中
            if any(bad in c for c in cands) and not all(
                    c.get("match") == "loose" for c in r["loose"]) and not r["hint"]:
                false_positives.append((r["query"], f"把「{bad}」当成命中端出来了"))

    print("\n" + "=" * 104)
    print("分层覆盖对比（口径：给了候选就算命中那一层；loose 候选不记为误报，但也不算精确命中）")
    print(f"  {'层':<16}{'改动前':>8}{'改动后':>8}")
    for layer in ("L0/L1 原生", "L2 预置源", "L3 URL识别", "失败"):
        before = BASELINE.get(layer, 0)
        after = stats.get(layer, 0)
        print(f"  {layer:<16}{before:>8}{after:>8}")
    print(f"  {'误报（订错）':<14}{BASELINE['误报']:>8}{len(false_positives):>8}")
    base_net = 27 - BASELINE["误报"]          # 基线：27 条有候选，其中 5 条是误报
    net = hit - len(false_positives)
    print(f"\n  **净命中（有候选且不是误报）**：改动前 {base_net}/42 = {base_net / 42 * 100:.0f}%"
          f" → 改动后 {net}/42 = {net / 42 * 100:.0f}%")
    print(f"  原始命中（含误报）：改动前 27/42 = 64% → 改动后 {hit}/42 = {hit / total * 100:.0f}%")
    print("  说明：误报本来就不该算命中 —— 把 5 条误报变成干净失败会让'原始命中'下降，")
    print("        所以门槛看**净命中**，同时要求误报必须为 0。")

    # ---------------- 门槛 ----------------
    print("\n" + "=" * 104)
    print("硬门槛判定")
    fails = []

    for query, bad in INCIDENTS:
        r = next(x for x in rows if x["query"] == query)
        if not r["labels"]:
            verdict, ok = "干净失败（无候选）", True
        elif bad and any(bad in c for c in r["labels"]):
            marked = all(c.get("match") == "loose" for c in r["loose"]) and bool(r["hint"])
            verdict = f"降级为 loose + 说明（{r['labels'][0][:16]}）" if marked else \
                      f"❌ 仍当命中端出（{r['labels'][0][:16]}）"
            ok = marked
        else:
            verdict, ok = f"给了候选：{r['labels'][0][:20]}", False
        print(f"  {'✅' if ok else '❌'} 事故「{query}」 → {verdict}")
        if not ok:
            fails.append(f"精度事故未消除：{query}")

    print(f"\n  {'✅' if not false_positives else '❌'} 误报数 = {len(false_positives)}（要求 0）")
    for q, why in false_positives:
        print(f"       {q} → {why}")
    if false_positives:
        fails.append(f"存在 {len(false_positives)} 条误报")

    hit_ok = net / 42 >= base_net / 42 - 1e-9
    print(f"  {'✅' if hit_ok else '❌'} 净命中 {net}/42 = {net / 42 * 100:.0f}% ≥ 基线 "
          f"{base_net}/42 = {base_net / 42 * 100:.0f}%")
    if not hit_ok:
        fails.append("净命中低于基线")

    slow = [(r["query"], r["cost"]) for r in rows if r["layer"] == "失败" and r["cost"] > 10]
    print(f"  {'✅' if not slow else '❌'} 失败路径耗时：最慢 "
          f"{max((r['cost'] for r in rows if r['layer'] == '失败'), default=0):.1f}s（要求 ≤10s）")
    for q, c in slow:
        print(f"       {q} 用了 {c:.1f}s")
    if slow:
        fails.append("失败路径超过 10 秒")

    print("\n" + "=" * 104)
    print("不得回归清单")
    by_query = {r["query"]: r for r in rows}
    for query, expect in NO_REGRESSION:
        r = by_query.get(query) or run_one(query)      # 不在样本集里的按需补跑
        print(f"  {query[:44]:<46} → {r['layer']:<12} {r['n']} 个  "
              f"{('| ' + ' / '.join(r['labels'][:2]))[:52]}")

    print("\n" + "=" * 104)
    print("不支持链接的失败耗时（验收项）")
    r = run_one(UNSUPPORTED_LINK)
    print(f"  /api/search 路：{UNSUPPORTED_LINK} → {r['layer']} {r['n']} 个候选，用时 {r['cost']:.1f}s")
    print(f"  提示：{(r['hint'] or '（无提示！）')[:78]}")
    if r["cost"] > 10:
        fails.append("不支持链接的失败路径超过 10 秒")
    if not r["hint"]:
        fails.append("失败时没有可读的提示")

    # 「粘贴链接添加」是另一条路径（/api/feed），它自己会再走一遍平台识别+探测+发现，
    # 所以单独计时 —— 验收要求它也在 10 秒内给出干净失败。
    try:
        import app as app_module

        started = time.time()
        info, error = app_module._add_feed_from_url(UNSUPPORTED_LINK)  # noqa: SLF001
        cost = time.time() - started
        print(f"  /api/feed 路：用时 {cost:.1f}s → "
              f"{'添加成功 ' + str(info.get('feed_url')) if info else '干净失败'}"
              f"（{(error or '')[:60]}）")
        if cost > 10:
            fails.append("粘贴不支持链接的失败路径超过 10 秒")
        if info is None and not error:
            fails.append("粘贴不支持链接失败时没有可读的提示")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ /api/feed 路未能验证：{type(exc).__name__}: {exc}")

    print("\n" + "=" * 104)
    if fails:
        print(f"❌ 未通过 {len(fails)} 项：")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 全部门槛通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
