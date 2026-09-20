"""本地 AI 助手的确定性测试（零网络）

    python tests/ai_interpret.py                 # 桩测试：把所有模型调用替换成假回复
    python tests/ai_interpret.py --assert-offline # 同上，但先把 socket 封死（证明真的零网络）
    python tests/ai_interpret.py --live           # 额外跑 3 条真实模型调用（需要 Ollama 在跑）

为什么这么测：
    「AI 会猜错」是已知事实，所以这个功能能不能上，靠的不是"模型答对率"，
    而是**模型不听话时我们会不会崩**：
      · 它返回代码块 / 前后带废话 / 字段缺失 / platform 乱填 / confidence 是字符串；
      · Ollama 没在跑 / 超时 / 用户把功能关了；
      · 它猜出来的东西**必须**是 loose 候选（前端默认折叠），不能混进"确定候选"里。
    这些全部是纯函数和桩能覆盖的，不需要模型、不需要网络、毫秒级。
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import net  # noqa: F401

import ai
import rsshub

# --------------------------------------------------------------------------- #
# parse_reply 的用例表：只管"模型不听话时会不会崩"
# --------------------------------------------------------------------------- #
PARSE_CASES: list[tuple[str, str, dict]] = [
    # (说明, 模型原话, 期望的字段子集)
    ("干净的 JSON", '{"platform":"bilibili","keyword":"半佛仙人","query":"B站 半佛仙人",'
                    '"confidence":0.9,"reason":"知名UP主"}',
     {"ok": True, "platform": "bilibili", "keyword": "半佛仙人", "confidence": 0.9}),
    ("```json 围栏", '```json\n{"platform":"zhihu","keyword":"李永乐","query":"知乎 李永乐",'
                     '"confidence":0.8}\n```',
     {"ok": True, "platform": "zhihu", "keyword": "李永乐"}),
    ("前后有废话", '好的，我的判断是：{"platform":"weibo","keyword":"友琳","query":"微博 友琳",'
                   '"confidence":0.7} 希望有帮助',
     {"ok": True, "platform": "weibo", "keyword": "友琳"}),
    ("platform 不在白名单 → 当没猜出来", '{"platform":"weibo2","keyword":"友琳","confidence":0.7}',
     {"ok": True, "platform": "", "platform_raw": "weibo2", "keyword": "友琳"}),
    ("confidence 是字符串", '{"platform":"","keyword":"少数派","confidence":"0.85"}',
     {"ok": True, "confidence": 0.85}),
    ("confidence 是垃圾 → 归零", '{"platform":"","keyword":"少数派","confidence":"高"}',
     {"ok": True, "confidence": 0.0}),
    ("confidence 超范围 → 夹到 0~1", '{"platform":"","keyword":"少数派","confidence":9}',
     {"ok": True, "confidence": 1.0}),
    ("关键词和查询都空 → 不算读懂", '{"platform":"bilibili","keyword":"","query":"",'
                                  '"confidence":0.2,"reason":"不是订阅意图"}',
     {"ok": False, "error": "模型认为这句话里没有具体的订阅对象"}),
    ("完全不是 JSON", "我不知道你在说什么", {"ok": False, "error": "模型没给出可解析的 JSON"}),
    ("空回复", "", {"ok": False, "error": "模型没给出可解析的 JSON"}),
    ("返回数组而不是对象", '[{"platform":"bilibili"}]', {"ok": False}),
    ("名字超长 → 截断到 40 字", '{"platform":"bilibili","keyword":"' + "很长的名字" * 20 + '"}',
     {"ok": True, "keyword_len": 40}),
]


def _stub_chat(reply: str, store: list):
    def fake(messages, wait):  # noqa: ANN001
        store.append({"messages": messages, "wait": wait})
        return reply
    return fake


def run_offline() -> tuple[bool, list[str]]:
    lines: list[str] = []
    fails: list[str] = []

    lines.append("parse_reply()（纯函数：模型不听话时的容错）")
    for name, reply, expect in PARSE_CASES:
        got = ai.parse_reply(reply)
        ok = True
        detail = []
        for key, want in expect.items():
            if key == "keyword_len":
                actual = len(got["keyword"])
                ok = ok and actual == want
                detail.append(f"keyword 长度={actual}")
                continue
            actual = got.get(key)
            if actual != want:
                ok = False
                detail.append(f"{key}={actual!r}≠{want!r}")
        lines.append(f"  {'✅' if ok else '❌'} {name}"
                     + (f"　[{'; '.join(detail)}]" if detail and not ok else ""))
        if not ok:
            fails.append(f"parse_reply 用例失败：{name}（{'; '.join(detail)}）")

    # ---------------- interpret()：把模型换成桩 ----------------
    lines.append("")
    lines.append("interpret()（桩：超时 / 连不上 / 功能关闭 / 空输入）")
    real_chat, real_get = ai._chat, ai._get  # noqa: SLF001

    def with_settings(**over):
        ai._get = lambda key, default: over.get(key, default)  # noqa: SLF001

    try:
        # 1) 正常：桩返回一句好 JSON
        calls: list = []
        ai._chat = _stub_chat('{"platform":"bilibili","keyword":"半佛仙人",'
                              '"query":"B站 半佛仙人","confidence":0.9,"reason":"知名UP主"}', calls)  # noqa: SLF001
        with_settings(ai_enabled="true", ai_timeout_seconds="6")
        got = ai.interpret("我想追那个讲财经的B站up")
        ok = got["ok"] and got["platform"] == "bilibili" and got["keyword"] == "半佛仙人" \
            and got["query_suggest"] == "B站 半佛仙人" and len(calls) == 1
        lines.append(f"  {'✅' if ok else '❌'} 正常解析 → 平台={got['platform'] or '-'} "
                     f"名字={got['keyword']!r} 查询={got['query_suggest']!r} "
                     f"（调用桩 {len(calls)} 次）")
        if not ok:
            fails.append("interpret 正常路径不对")

        # 2) 系统提示词必须带上（提示词掉了是最容易犯的错）
        sys_msg = calls[0]["messages"][0]["content"] if calls else ""
        ok = calls and calls[0]["messages"][0]["role"] == "system" and "platform" in sys_msg
        lines.append(f"  {'✅' if ok else '❌'} 请求里带上了 system 提示词（{len(sys_msg)} 字）")
        if not ok:
            fails.append("interpret 没带 system 提示词")

        # 3) 超时
        def boom_timeout(messages, wait):  # noqa: ANN001
            import requests
            raise requests.Timeout("stub")
        ai._chat = boom_timeout  # noqa: SLF001
        got = ai.interpret("随便说点什么")
        err = got["error"] or ""
        ok = (not got["ok"]) and ("太慢" in err or "还没加载完" in err)
        lines.append(f"  {'✅' if ok else '❌'} 超时 → ok={got['ok']} error={err!r}")
        if not ok:
            fails.append("interpret 超时处理不对")

        # 4) 连不上
        def boom_conn(messages, wait):  # noqa: ANN001
            import requests
            raise requests.ConnectionError("stub")
        ai._chat = boom_conn  # noqa: SLF001
        got = ai.interpret("随便说点什么")
        ok = (not got["ok"]) and "连不上" in (got["error"] or "")
        lines.append(f"  {'✅' if ok else '❌'} 连不上 → ok={got['ok']} error={got['error']!r}")
        if not ok:
            fails.append("interpret 连不上处理不对")

        # 5) 异常也不许抛出去
        def boom_other(messages, wait):  # noqa: ANN001
            raise RuntimeError("stub")
        ai._chat = boom_other  # noqa: SLF001
        try:
            got = ai.interpret("随便说点什么")
            ok = not got["ok"] and got["error"]
        except Exception as exc:  # noqa: BLE001
            ok = False
            lines.append(f"      抛异常了：{exc!r}")
        lines.append(f"  {'✅' if ok else '❌'} 其它异常被吞掉、返回可读错误")
        if not ok:
            fails.append("interpret 把异常抛出来了")

        # 6) 功能关闭时不发请求
        calls2: list = []
        ai._chat = _stub_chat('{"platform":"bilibili","keyword":"x"}', calls2)  # noqa: SLF001
        with_settings(ai_enabled="false")
        got = ai.interpret("半佛仙人")
        ok = (not got["ok"]) and "已关闭" in (got["error"] or "") and not calls2
        lines.append(f"  {'✅' if ok else '❌'} AI 关闭 → 不调模型、给可读原因"
                     f"（调用桩 {len(calls2)} 次）")
        if not ok:
            fails.append("AI 关闭时仍然调了模型")

        # 7) 空输入
        with_settings(ai_enabled="true")
        got = ai.interpret("   ")
        ok = (not got["ok"]) and got["error"] == "没有输入内容"
        lines.append(f"  {'✅' if ok else '❌'} 空输入 → {got['error']!r}")
        if not ok:
            fails.append("interpret 空输入处理不对")

        # ---------------- suggest()：候选必须一律 loose ----------------
        lines.append("")
        lines.append("suggest()（AI 候选必须全部 loose，且只能来自平台搜索的真实返回）")
        fake_found = [
            {"label": "B站 · 硬核的半佛仙人", "detail": "771.3万粉丝",
             "feed_url": "byread://bilibili/dynamic/37663924", "title": "硬核的半佛仙人",
             "site_url": "https://space.bilibili.com/37663924", "icon": None, "platform": "B站"},
            {"label": "B站 · 半佛仙人", "detail": "11粉丝",
             "feed_url": "byread://bilibili/dynamic/24189353", "title": "半佛仙人",
             "site_url": "https://space.bilibili.com/24189353", "icon": None, "platform": "B站"},
        ]
        real_pc = rsshub.platform_candidates
        rsshub.platform_candidates = lambda platform, keyword, limit=4: [dict(c) for c in fake_found]
        try:
            ai._chat = _stub_chat('{"platform":"bilibili","keyword":"半佛仙人",'  # noqa: SLF001
                                  '"query":"B站 半佛仙人","confidence":0.9,"reason":"知名UP主"}', [])
            with_settings(ai_enabled="true", ai_timeout_seconds="6")
            got = ai.suggest("我想追那个讲财经的B站up", limit=3)
            all_loose = bool(got["candidates"]) and all(c.get("match") == "loose"
                                                        for c in got["candidates"])
            all_ai = all(c.get("source") == "ai" for c in got["candidates"])
            marked = all("AI 猜的" in (c.get("detail") or "") for c in got["candidates"])
            ok = got["ok"] and all_loose and all_ai and marked
            lines.append(f"  {'✅' if ok else '❌'} {len(got['candidates'])} 个候选全部 "
                         f"match=loose / source=ai / 带「AI 猜的」说明")
            if not ok:
                fails.append("suggest 的候选没有全部标 loose")
            ok = bool(got["rewritten"]) and bool(got["note"])
            lines.append(f"  {'✅' if ok else '❌'} 同时给出改写查询（{got['rewritten']!r}）"
                         f"与说明")
            if not ok:
                fails.append("suggest 没有给出改写查询/说明")

            # 猜到了"不能按名字搜"的平台 → 不给候选，只给查询词
            ai._chat = _stub_chat('{"platform":"sspai","keyword":"少数派",'  # noqa: SLF001
                                  '"query":"少数派","confidence":0.9}', [])
            got = ai.suggest("少数派")
            ok = got["ok"] and not got["candidates"] and got["note"]
            lines.append(f"  {'✅' if ok else '❌'} 平台不能按名字搜（少数派）→ 0 候选 + "
                         f"只给查询词（{got['rewritten']!r}）")
            if not ok:
                fails.append("suggest 对不可搜索平台处理不对")

            # AI 也没读懂 → 0 候选 + 说明
            ai._chat = _stub_chat('{"platform":"","keyword":"","query":"","confidence":0}', [])  # noqa: SLF001
            got = ai.suggest("明天会下雨吗")
            ok = (not got["ok"]) and not got["candidates"] and got["note"]
            lines.append(f"  {'✅' if ok else '❌'} AI 也没读懂 → 0 候选 + 说明（{got['note']!r}）")
            if not ok:
                fails.append("suggest 对'AI 也没读懂'的处理不对")
        finally:
            rsshub.platform_candidates = real_pc
    finally:
        ai._chat, ai._get = real_chat, real_get  # noqa: SLF001

    lines.append("")
    lines.append("feedback()（反馈只写本机文件；这里写到临时目录，绝不碰真实数据）")
    import json as _json
    import tempfile

    real_path = ai.feedback_path
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "ai_feedback.jsonl"
        ai.feedback_path = lambda: target
        try:
            res = ai.save_feedback({"verdict": "down", "query": "我想追那个讲财经的B站up",
                                    "correct": "半佛仙人", "platform": "bilibili",
                                    "keyword": "", "rewritten": "", "raw": "{...}"})
            ok = res.get("ok") and res.get("count") == 1 and target.exists()
            lines.append(f"  {'✅' if ok else '❌'} 写一条 → ok={res.get('ok')} "
                         f"count={res.get('count')}")
            if not ok:
                fails.append("反馈没写成功")
            rec = _json.loads(target.read_text(encoding="utf-8").strip().splitlines()[0])
            ok = (rec["verdict"] == "down" and rec["query"] == "我想追那个讲财经的B站up"
                  and rec["correct"] == "半佛仙人" and rec["platform"] == "bilibili"
                  and rec["model"] == ai.model() and rec.get("ts"))
            lines.append(f"  {'✅' if ok else '❌'} 落盘字段完整（含时间戳与模型名）")
            if not ok:
                fails.append("反馈字段不对")

            ai.save_feedback({"query": "半佛仙人"})                   # 没给 verdict、也没正确答案
            ai.save_feedback({"query": "半佛仙人", "correct": "别的"})  # 给了正确答案 → 当"理解错了"
            text = ai.feedback_text()
            verdicts = [_json.loads(x)["verdict"] for x in text.strip().splitlines()]
            ok = verdicts == ["down", "up", "down"] and ai.feedback_count() == 3
            lines.append(f"  {'✅' if ok else '❌'} verdict 推断 + 计数：{verdicts}"
                         f"（count={ai.feedback_count()}）")
            if not ok:
                fails.append("verdict 推断或计数不对")

            ai.feedback_path = lambda: Path(tmp) / "no" / "deep" / "x.jsonl"
            res = ai.save_feedback({"query": "x"})
            lines.append(f"  {'✅' if res.get('ok') else '❌'} 目录不存在时自动建目录 → "
                         f"ok={res.get('ok')}")
            if not res.get("ok"):
                fails.append("反馈写入不会自动建目录")
        finally:
            ai.feedback_path = real_path

    lines.append("")
    if fails:
        lines.append(f"❌ 未通过 {len(fails)} 项：")
        for f in fails:
            lines.append(f"   - {f}")
    else:
        lines.append("✅ 全部通过（模型不听话 / 超时 / 连不上 / 功能关闭，都不崩）")
    return (not fails), lines


def run_live() -> tuple[bool, list[str]]:
    """真实模型冒烟（只在 --live 时跑；Ollama 不在就跳过，不算失败）。"""
    lines = ["真实模型冒烟（--live）"]
    st = ai.status()
    lines.append(f"  状态：{st['detail']}")
    if not (st["available"] and st["model_present"]):
        lines.append("  ⏭ 跳过：模型不可用")
        return True, lines
    fails = []
    for q in ["半佛仙人", "我想追那个讲财经的B站up", "明天会下雨吗"]:
        got = ai.interpret(q)
        lines.append(f"  {q!r} → ok={got['ok']} 平台={got['platform'] or '-'} "
                     f"名字={got['keyword']!r} 查询={got['query_suggest']!r} "
                     f"置信={got['confidence']:.2f} {got['elapsed_ms']}ms")
        lines.append(f"      模型原话：{(got['raw'] or '').replace(chr(10), ' ')[:150]}")
        if not got["ok"] and not got["error"]:
            fails.append(f"{q} 失败却没给原因")
    return (not fails), lines


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="本地 AI 助手的确定性测试")
    parser.add_argument("--live", action="store_true", help="额外跑真实模型冒烟")
    parser.add_argument("--assert-offline", action="store_true",
                        help="先把 socket 封死再跑（用来证明这套测试零网络）")
    args = parser.parse_args(argv)

    if args.assert_offline:
        def blocked(*a, **k):
            raise RuntimeError("零网络检查：本次运行不允许建立任何连接")
        socket.socket.connect = blocked            # type: ignore[method-assign]
        socket.socket.connect_ex = blocked         # type: ignore[method-assign]
        socket.create_connection = blocked         # type: ignore[assignment]
        socket.getaddrinfo = blocked               # type: ignore[assignment]
        # 对照组：确认拦截有效（否则"跑通了"什么也证明不了）
        try:
            import requests
            requests.get("http://127.0.0.1:5000/api/counts", timeout=2)
            print("❌ 对照组失败：拦截没生效，本次证明无效")
            return 2
        except Exception as exc:  # noqa: BLE001
            print(f"✅ 对照组：拦截有效（{type(exc).__name__}）")

    started = time.time()
    ok1, lines1 = run_offline()
    print("\n".join(lines1))
    if args.live:
        print()
        ok2, lines2 = run_live()
        print("\n".join(lines2))
    else:
        ok2 = True
    print(f"\n耗时 {time.time() - started:.3f} 秒"
          + ("（--assert-offline：全程零网络）" if args.assert_offline else ""))
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
