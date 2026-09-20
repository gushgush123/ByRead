"""搜索精度验收脚本（P0）—— 两层结构

    python tests/search_precision.py --quick     # 第一层：确定性门槛，零网络，<0.1 秒
    python tests/search_precision.py             # 第一层 + 第二层（端到端观察，约 60 秒）
    python tests/search_precision.py --runs 3    # 端到端跑三次，报区间

为什么要分两层（这是本次改造的核心）：

    第二层依赖 B 站搜索接口是否返回数据，而它会**间歇性返回空** —— 实测同一份代码：
        「半佛仙人」连续两次调用 → 5 条 / 1 条
        「_warma_」 连续两次调用 → 4 条 / 0 条
        「Warma」   连续三次调用 → 4 / 4 / 4 条
    后果：净命中率在 55%~60% 之间抖（基线 52% 只隔 1~3 条样本）。
    一个会因为第三方接口随机性而红/绿的验收门槛，不能当门用 —— 它会让后续所有改动
    的验收数字都失去意义。所以：

        第一层（门槛）：只测两个闸门函数本身（纯函数，不依赖上游）→ 必须全过
        第二层（观察）：42 条真实样本跑一遍，覆盖/耗时/候选/可疑候选 → 只记录

    例外：第二层里少数**确定性**的检查仍然判定 —— 判定函数不碰网络的那几条
    （句式闸门短路掉的 D 组、预置源分档的 loose、失败耗时上限、失败必须有可读提示）。
    判定/记录在输出里逐条标明。

只读：不写库、不加订阅；判定函数不发请求。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import net  # noqa: F401

import rsshub

sys.path.insert(0, str(Path(__file__).resolve().parent))

import search_gates  # noqa: E402  第一层：确定性门槛
import ai_interpret  # noqa: E402  第一层：本地 AI 助手的确定性测试（同样零网络）

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
    # D. 自由表述 / 无关输入（只要求"失败得体面"）
    + [(q, "D 自由表述") for q in [
        "我想追那个讲财经的B站up",
        "帮我订个每天看新闻的",
        "今天天气不错",
        "隔壁老王的空间",
        "有没有讲历史的播客",
    ]]
)

# 改动前基线（阶段 0 报告）。**只用于报告里展示对比，不参与判定** ——
# 它是上游返回数据时的统计量，会随 B 站接口抖动。
BASELINE = {"L0/L1 原生": 13, "L2 预置源": 13, "L3 URL识别": 2, "失败": 14, "误报": 5}

# 事故清单（数据化）：每条自带期望类型。
#   no_candidate      候选数必须为 0          —— 只给"不依赖上游"的用
#   loose_only        候选必须全部 match=loose 且有 hint
#   no_bad_candidate  候选里不得出现指定字符串 —— 上游依赖型用这条（收窄断言）
INCIDENTS = [
    {"query": "豆瓣 租房小组", "expect": "loose_only",
     "bad": "豆瓣电影正在上映", "upstream": False,
     "note": "预置源分档：只能作为 loose 候选端出，且必须带说明"},
    {"query": "GitHub releases 某项目", "expect": "loose_only",
     "bad": "GitHub 每日趋势", "upstream": False,
     "note": "同上"},
    {"query": "今天天气不错", "expect": "no_candidate", "bad": None, "upstream": False,
     "note": "句式闸门短路，不发任何请求 → 结果确定"},
    {"query": "隔壁老王的空间", "expect": "no_candidate", "bad": None, "upstream": False,
     "note": "同上"},
    {"query": "清华大学 通知", "expect": "no_bad_candidate", "bad": "清华录取通知书的履行",
     "upstream": True,
     "note": "这条**依赖上游**（B站若真返回一个叫「清华大学」的账号，relevance 会放行，"
             "而那未必是误报）→ 断言收窄为「不得出现这个具体候选」"},
]

# 不得回归清单（展示用，不做判定）
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

# 只有"平台按名字搜出来"的候选才在可疑观察器的作用域内
PLATFORM_SEARCHES = {"B站", "知乎", "微博"}

LAYERS = ("L0/L1 原生", "L2 预置源", "L3 URL识别", "仅 loose 候选", "失败")
# 「仅 loose 候选」= 只有 loose（不确定）候选，没有一个是两道闸门判过的。
# 这一桶**不算命中**：AI 猜的、以及"只是提到了平台"的候选都在这里，
# 把它们算进命中会让"净命中"这个观察值被 AI 的猜测污染。

# 判定/记录口径（P0 之后的 AI 改动带来的语义变化，写在这里免得以后被误读）：
#   「候选数」= **确定候选**（match != "loose"）。loose 候选一律单独计数并打印出来，
#   因为前端默认折叠它们、用户不点开就看不见 —— 这正是 AI 候选被允许存在的前提。
VISIBLE = lambda c: c.get("match") != "loose"          # noqa: E731  确定候选（用户直接可见）


def classify(query: str, result: dict) -> str:
    """把一次 search 的结果归到解析链的哪一层（与阶段 0 报告口径一致）。"""
    cands = result.get("candidates") or []
    if not cands:
        return "失败"
    if all(c.get("match") == "loose" for c in cands):
        return "仅 loose 候选"          # 前端默认折叠：用户第一眼看不到任何东西
    if rsshub.is_url(query):
        first = cands[0]
        if (first.get("feed_url") or "").startswith("byread://"):
            return "L0/L1 原生"
        return "L3 URL识别"
    plats = {c.get("platform") for c in cands if c.get("match") != "loose"}
    native = {"B站", "知乎", "微博", "机核", "GitHub"}
    if plats & native and plats - native:
        return "L0/L1+L2"
    if plats & native:
        return "L0/L1 原生"
    return "L2 预置源"


def split_keyword(query: str) -> str:
    """取出真正拿去平台搜索的那个关键词（「B站 半佛仙人」→「半佛仙人」）。"""
    if rsshub.is_url(query):
        return query
    _, keyword = rsshub.split_platform(query)
    return (keyword or query).strip() or query


def suspicious_candidate(keyword: str, cand: dict) -> str | None:
    """
    通用"可疑候选"判据（**观察**，不判失败）：

        候选名与查询词【不是互为连续子串】，且长度差 > max(2, 0.4 × len(查询词))

    作用域（三条都是我加的限定，写在这里以免被当成漏报）：
      · 只看平台按名字搜出来的候选（B站/知乎/微博）—— 预置源/URL 候选是按规则产生的，
        名字当然和查询词不一样；
      · 排除 URL 查询 —— 粘贴链接解析出来的候选（「B站 · 37663924」）本来就不是按名字搜的；
      · 排除已标记 loose 的候选 —— 它们本来就带「⚠ 不确定」和一句说明，再警告一次没有信息量。

    复核（实测）：命中「Steam 更新 × 浪子秀白steam游戏更新」（非子串、长度差 6 > 2.8）；
    「数码闲聊站 × 大中家电数码闲聊站」「文元 × 中医皮肤科王文元」「半佛仙人 × 硬核的半佛仙人」
    都互为子串 → 不报；「隔壁老王的空间 × 隔壁空间站的老王」长度差只有 1 → 不报
    （那条由句式闸门负责）。

    已知噪声（实测）：真实跑起来还会报出「友琳_Yurin × Yurin录播号_友琳日记」——
    查询的两半（友琳 / yurin）在候选名里被「录播号」隔开，与 Steam 那条结构相同，
    规则分不开（人看着像是同一个人的录播号）。**观察器不是过滤器**，有噪声是接受的；
    它的价值是把这类候选变成"每次运行都看得见"。
    """
    if cand.get("platform") not in PLATFORM_SEARCHES:
        return None
    if rsshub.is_url(keyword):
        return None
    if cand.get("match") == "loose":
        return None
    name = cand.get("title") or cand.get("label") or ""
    q, n = rsshub._norm(keyword), rsshub._norm(name)  # noqa: SLF001
    if not q or not n:
        return None
    if q in n or n in q:
        return None
    diff = abs(len(n) - len(q))
    limit = max(2, 0.4 * len(q))
    if diff > limit:
        return (f"不是互为子串，长度差 {diff} > 上限 {limit:.1f}"
                f"（查询 {q!r} / 候选 {n!r}）")
    return None


def run_one(query: str) -> dict:
    started = time.time()
    try:
        res = rsshub.search(query)
    except Exception as exc:  # noqa: BLE001
        res = {"candidates": [], "hint": f"异常 {type(exc).__name__}: {exc}"}
    cost = time.time() - started
    cands = res.get("candidates") or []
    labels = [c.get("label") or "" for c in cands]
    loose = [c for c in cands if c.get("match") == "loose"]
    visible = [c for c in cands if VISIBLE(c)]
    keyword = split_keyword(query)
    suspicious = []
    for c in cands:
        why = suspicious_candidate(keyword, c)
        if why:
            suspicious.append((c.get("title") or c.get("label"), why))
    return {"query": query, "layer": classify(query, res), "cost": cost,
            "labels": labels, "loose": loose, "visible": visible, "hint": res.get("hint"),
            "n": len(visible), "keyword": keyword, "suspicious": suspicious}


def one_pass(detail: bool) -> list[dict]:
    rows = []
    if detail:
        print(f"{'层':<14}{'用时':>7}  {'输入':<44}{'确定候选'}")
        print("-" * 104)
    for query, group in SAMPLES:
        r = run_one(query)
        r["group"] = group
        rows.append(r)
        if detail:
            mark = "❌" if r["layer"] in ("失败", "仅 loose 候选") else "  "
            extra = f"（另有 loose {len(r['loose'])} 个）" if r["loose"] else ""
            print(f"{mark}{r['layer']:<12}{r['cost']:>6.1f}s  {query[:42]:<44}{r['n']} 个 "
                  f"{('| ' + ' / '.join(r['labels'][:2]))[:52] if r['n'] else ''}{extra}")
    return rows


def layer_stats(rows: list[dict]) -> tuple[dict[str, int], int]:
    stats: dict[str, int] = {}
    for r in rows:
        stats[r["layer"]] = stats.get(r["layer"], 0) + 1
    # 「失败」和「仅 loose 候选」都不算命中 —— 后者是 AI 猜的 / 只是提到平台的，
    # 前端默认折叠，用户第一眼看不到，算成命中会污染这个观察值。
    hit = sum(v for k, v in stats.items() if k not in ("失败", "仅 loose 候选"))
    return stats, hit


def check_incidents(rows: list[dict]) -> tuple[list[str], list[str]]:
    """返回 (失败项, 打印行)。判定 —— 但逐条标明是否依赖上游。"""
    fails, lines = [], []
    by_query = {r["query"]: r for r in rows}
    for inc in INCIDENTS:
        r = by_query.get(inc["query"])
        if r is None:
            fails.append(f"事故样本缺失：{inc['query']}")
            continue
        tag = "上游依赖" if inc["upstream"] else "确定性"
        labels, visible, loose, hint = r["labels"], r["visible"], r["loose"], r["hint"]
        if inc["expect"] == "no_candidate":
            # 「无候选」= 没有**确定候选**（用户直接可见的那种）。
            # AI 的 loose 候选（前端默认折叠、点开才可见）不计入 —— 见文件头的口径说明。
            ok = not visible
            verdict = ("干净失败（无确定候选）" if ok
                       else f"给了确定候选（{visible[0].get('label', '')[:24]}）")
            if loose:
                verdict += f"，另有 loose {len(loose)} 个（折叠，不计入）"
        elif inc["expect"] == "loose_only":
            marked = bool(labels) and all(c.get("match") == "loose" for c in loose) \
                and len(loose) == len(labels) and bool(hint)
            ok = marked
            verdict = (f"降级为 loose + 说明（{labels[0][:20]}）" if marked
                       else f"❌ 仍当命中端出（{labels[0][:20] if labels else '无候选'}）")
        else:  # no_bad_candidate
            bad = inc["bad"]
            found = [x for x in labels if bad in x]
            ok = not found
            verdict = (f"未出现「{bad}」" + (f"（其余候选 {len(labels)} 个）" if labels else "")
                       if ok else f"❌ 出现了「{bad}」")
        lines.append(f"  {'✅' if ok else '❌'} 事故「{inc['query']}」[{tag}] → {verdict}")
        if not ok:
            fails.append(f"精度事故未消除：{inc['query']}（{inc['expect']}）")
    return fails, lines


def check_d_group(rows: list[dict]) -> tuple[list[str], list[str]]:
    """
    D 组（自由表述）必须**没有确定候选** —— 句式闸门短路，不发请求，结果确定。

    注意口径：这里判的是「用户直接可见的候选」= 非 loose 的那批。
    接上本地 AI 之后，D 组这类句子**可能**多出几个 AI 猜的 loose 候选
    （前端默认折叠、点开才可见，并且写明"AI 猜的"）—— 那是设计好的行为，不是误报；
    数量会单独打印出来，免得"0"被误读成"AI 什么也没给"。
    """
    fails, lines = [], []
    bad, loose_rows = [], []
    for r in rows:
        if r["group"] != "D 自由表述":
            continue
        if r["visible"]:
            bad.append((r["query"], r["visible"][0].get("label", "")))
        if r["loose"]:
            loose_rows.append((r["query"], len(r["loose"])))
    for q, label in bad:
        lines.append(f"       {q} → 却给了确定候选「{label}」")
    for q, n in loose_rows:
        lines.append(f"       （折叠不计入）{q} → AI/预置源给的 loose 候选 {n} 个")
    lines.append(f"  {'✅' if not bad else '❌'} 已知事故 + D 组 确定候选误报 = {len(bad)}"
                 f"（要求 0；口径说明见下）")
    if bad:
        fails.append(f"已知事故清单 + D 组里有 {len(bad)} 条确定的误报")
    return fails, lines


def observe_suspicious(rows: list[dict]) -> list[tuple[str, str, str]]:
    """通用可疑候选观察（不判失败）：跨所有样本收集，按 (查询, 候选名) 去重。"""
    seen, out = set(), []
    for r in rows:
        for name, why in r["suspicious"]:
            key = (r["query"], name)
            if key in seen:
                continue
            seen.add(key)
            out.append((r["query"], name, why))
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--quick" in argv or "-q" in argv:
        print("第一层：确定性门槛（--quick，零网络）\n")
        rc = search_gates.main()
        if rc:
            return rc
        print("\n" + "=" * 104)
        return ai_interpret.main([])

    runs = 1
    if "--runs" in argv:
        i = argv.index("--runs")
        try:
            runs = max(1, int(argv[i + 1]))
        except (IndexError, ValueError):
            print("--runs 后面要跟一个正整数，例如 --runs 3")
            return 2

    started_all = time.time()
    print("第一层：确定性门槛（零网络）")
    print("=" * 104)
    ok_quick, quick_lines = search_gates.run()
    for ln in quick_lines:
        print(ln)
    print()

    print("=" * 104)
    print(f"第二层：端到端观察（{len(SAMPLES)} 条固定样本 × {runs} 次；只读，不写库、不加订阅）")
    print("  口径：走的是 rsshub.search()，**不含** app.py 的 AI 兜底 ——")
    print("        这一层量的是「确定候选」的精度；AI 路径的验收在 tests/ai_interpret.py。")
    print("=" * 104)

    passes: list[list[dict]] = []
    for i in range(runs):
        if runs > 1:
            print(f"\n---- 第 {i + 1} / {runs} 次 ----")
        rows = one_pass(detail=(runs == 1))
        passes.append(rows)
        stats, hit = layer_stats(rows)
        if runs > 1:
            loose_n = sum(len(r["loose"]) for r in rows)
            print("  " + "  ".join(f"{k}={stats.get(k, 0)}" for k in LAYERS)
                  + f"  确定命中={hit}/{len(rows)} = {hit / len(rows) * 100:.0f}%"
                  + f"  loose 候选 {loose_n} 个（折叠，不计入命中）")

    # ---------------- 观察值（不判门槛）----------------
    print("\n" + "=" * 104)
    print("观察值（依赖上游是否返回数据 → **只记录，不判门槛**）")
    per_run = []
    for rows in passes:
        stats, hit = layer_stats(rows)
        per_run.append((stats, hit))
    header = f"  {'指标':<14}" + "".join(f"{('第%d次' % (i + 1)):>10}" for i in range(runs)) \
             + f"{'区间':>16}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for key in ("确定命中", "loose 候选", *LAYERS):
        if key == "确定命中":
            vals = [h for _, h in per_run]
            cells = [f"{v}/{len(SAMPLES)}" for v in vals]
            rng = f"{min(vals)}~{max(vals)}（{min(vals) / len(SAMPLES) * 100:.0f}%~"
            rng += f"{max(vals) / len(SAMPLES) * 100:.0f}%）"
        elif key == "loose 候选":
            vals = [sum(len(r["loose"]) for r in rows) for rows in passes]
            cells = [str(v) for v in vals]
            rng = f"{min(vals)}~{max(vals)}"
        else:
            vals = [stats.get(key, 0) for stats, _ in per_run]
            cells = [str(v) for v in vals]
            rng = f"{min(vals)}~{max(vals)}"
        print(f"  {key:<14}" + "".join(f"{c:>10}" for c in cells) + f"{rng:>16}")
    base_net = 27 - BASELINE["误报"]
    print(f"\n  改动前基线（阶段 0 报告，同样受上游抖动影响）：净命中 {base_net}/42 = "
          f"{base_net / 42 * 100:.0f}%，各层 " +
          "  ".join(f"{k}={BASELINE.get(k, 0)}" for k in LAYERS))
    print("  口径：这里统计的是「确定命中」= 有非 loose 候选（用户一眼能看见的那种），")
    print("        比改动前那份「净命中」更严 —— loose 候选（AI 猜的 / 只是提到平台）"
          "单列一行，不计入。")
    print("        它仍然只是观察值：上游 B 站接口会间歇性返回空（实测「半佛仙人」5 条/1 条、"
          "「_warma_」4 条/0 条），所以它不当门槛。")

    # ---------------- 可疑候选观察器 ----------------
    print("\n" + "=" * 104)
    print("通用「可疑候选」观察器（不判失败，只让人看见）")
    print("  判据：候选名与查询词不是互为连续子串，且长度差 > max(2, 0.4×len(查询词))")
    print("  作用域：平台按名字搜出的候选（B站/知乎/微博），排除 URL 查询与已标记 loose 的")
    all_susp = []
    for rows in passes:
        for item in observe_suspicious(rows):
            if item not in all_susp:
                all_susp.append(item)
    print(f"  抓到 {len(all_susp)} 条：")
    for q, name, why in all_susp:
        print(f"    ⚠ {q!r} → {name!r}：{why}")
    if not all_susp:
        print("    （本次一条都没有 —— 注意「Steam 更新」这类是**间歇性**的，"
              "多跑几次才看得到频率）")

    # ---------------- 判定项 ----------------
    print("\n" + "=" * 104)
    print("判定项（只用不依赖上游的检查）")
    fails: list[str] = []
    if not ok_quick:
        fails.append("第一层确定性门槛未过")

    print("\n  【判定·确定性】五条精度事故（逐条标明是否依赖上游）：")
    inc_fails, inc_lines = [], []
    for i, rows in enumerate(passes):
        f, lines = check_incidents(rows)
        inc_fails += [f"第 {i + 1} 次：{x}" for x in f]
        if i == 0:
            inc_lines = lines
    for ln in inc_lines:
        print(ln)
    fails += inc_fails

    print("\n  【判定·确定性】自由表述组 + 已知事故清单的误报数：")
    d_fails, d_lines = [], []
    for i, rows in enumerate(passes):
        f, lines = check_d_group(rows)
        d_fails += [f"第 {i + 1} 次：{x}" for x in f]
        if i == 0:
            d_lines = lines
    for ln in d_lines:
        print(ln)
    print("        ⚠ 覆盖面说明：这里的 0 指「已知事故清单（5 条）+ D 组（5 条）」为 0，")
    print("          不是通用意义上一例误报都没有 —— 全样本的通用扫描见上面的「可疑候选观察器」。")
    fails += d_fails

    print("\n  【判定·确定性】失败路径必须给出可读提示：")
    missing = [(r["query"], i + 1) for i, rows in enumerate(passes)
               for r in rows if r["layer"] == "失败" and not r["hint"]]
    for q, i in missing:
        print(f"       ❌ 第 {i} 次：{q} 失败但没有提示")
    print(f"  {'✅' if not missing else '❌'} 失败样本共 "
          f"{sum(1 for rows in passes for r in rows if r['layer'] == '失败')} 条"
          f"（{runs} 次合计），缺提示 {len(missing)} 条")
    if missing:
        fails.append("失败时没有可读提示")

    print("\n  【判定·弱上游依赖】失败路径耗时 ≤ 10 秒（预算是我们自己的机制，留了余量）：")
    fails_rows = [r for rows in passes for r in rows if r["layer"] == "失败"]
    worst = max((r["cost"] for r in fails_rows), default=0.0)
    worst_non_url = max((r["cost"] for r in fails_rows if not r["query"].startswith("http")),
                        default=0.0)
    slow = [(r["query"], r["cost"], i + 1) for i, rows in enumerate(passes)
            for r in rows if r["layer"] == "失败" and r["cost"] > 10]
    print(f"       失败样本最慢 {worst:.1f}s（其中非 URL 样本最慢 {worst_non_url:.1f}s）；"
          f"超过 10s 的 {len(slow)} 条")
    for q, c, i in slow:
        print(f"       ❌ 第 {i} 次：{q} 用了 {c:.1f}s")
    if slow:
        fails.append("失败路径超过 10 秒")

    print("\n  【判定·弱上游依赖】不支持链接的两条路径（/api/search 与 /api/feed）：")
    r = run_one(UNSUPPORTED_LINK)
    print(f"       /api/search：{r['layer']} {r['n']} 个候选，用时 {r['cost']:.1f}s")
    print(f"       提示：{(r['hint'] or '（无提示！）')[:70]}")
    if r["cost"] > 10:
        fails.append("不支持链接（/api/search）超过 10 秒")
    if not r["hint"]:
        fails.append("不支持链接失败时没有提示")
    try:
        import app as app_module

        t0 = time.time()
        info, error = app_module._add_feed_from_url(UNSUPPORTED_LINK)  # noqa: SLF001
        cost = time.time() - t0
        print(f"       /api/feed  ：用时 {cost:.1f}s → "
              f"{'添加成功' if info else '干净失败'}（{(error or '')[:56]}）")
        if cost > 10:
            fails.append("不支持链接（/api/feed）超过 10 秒")
        if info is None and not error:
            fails.append("不支持链接（/api/feed）失败时没有提示")
    except Exception as exc:  # noqa: BLE001
        print(f"       ⚠ /api/feed 路未能验证：{type(exc).__name__}: {exc}")

    # ---------------- 不得回归清单 ----------------
    print("\n" + "=" * 104)
    print("不得回归清单（展示用，不做判定）")
    by_query = {r["query"]: r for r in passes[0]}
    for query, expect in NO_REGRESSION:
        r = by_query.get(query) or run_one(query)
        print(f"  {query[:44]:<46} → {r['layer']:<12} {r['n']} 个  "
              f"{('| ' + ' / '.join(r['labels'][:2]))[:52]}")

    print("\n" + "=" * 104)
    print(f"总耗时 {time.time() - started_all:.1f} 秒")
    if fails:
        print(f"\n❌ 未通过 {len(fails)} 项：")
        for f in fails:
            print("   -", f)
        return 1
    print("\n✅ 判定项全部通过（观察值见上，不作为门槛）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
