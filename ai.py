"""
ai.py —— 本地 AI 助手：把"人话"翻译成"能搜的查询词"

这个模块在整条链里的位置（**很重要，改之前先读这段**）：

    用户说「我想追那个讲财经的B站up」
        ↓  rsshub.search() 正常路径（L1/L2/L3 + P0 两道闸门）
    确定性的候选 —— 有，就直接给用户，**根本不叫 AI**
        ↓ 只有"一个候选都没有"时才出手
    ai.interpret()：本地小模型把这句话解析成 {平台, 名字, 建议查询词}
        ↓  用解析出的"平台 + 名字"去问平台搜索接口
    AI 猜出来的候选 —— **一律 match="loose"**，前端默认折叠、点开才可见

为什么这样设计：
  * AI 会猜错。实测同一个模型：「少数派」被判成 jike、「帮我订个每天看新闻的」猜成
    「豆瓣 每日新闻」。所以它**不能**参与"用户第一眼看到的候选" —— 那是 P0 两道闸门
    （looks_like_sentence / relevance）的职责，那部分精度已经验收过，不能被 AI 稀释。
  * 但"猜错了也无所谓"的前提是**看得见、可关掉**：AI 候选全部标 loose、默认折叠、
    并且把"它理解成了什么"和"为什么这么猜"直接摊开给用户看。
  * 用户看到 AI 的改写结果后，可以点「用它再搜一次」——那一次走的是**正常链**，
    候选重新由两道闸门判定，就回到确定性路径上了。

红线（与本项目一致）：
  * 只和本机 Ollama 说话（默认 127.0.0.1:11434），不联网、不需要 API key，输入不出本机。
  * AI 有自己的超时预算（settings: ai_timeout_seconds，默认 6），**不占用**解析链的
    search_budget_seconds；模型没热时宁可干净失败 + 一句人话，也不拖住用户。
  * 所有对外调用 try/except，绝不把异常抛给调用方。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Optional

import requests

log = logging.getLogger("byread.ai")

# --------------------------------------------------------------------------- #
# 平台代号表：模型的取值白名单 + 展示名 + 能不能"按名字搜"
# --------------------------------------------------------------------------- #
# code -> (中文展示名, 能否按名字搜到人/源)
PLATFORMS: dict[str, tuple[str, bool]] = {
    "bilibili": ("B站", True),
    "zhihu": ("知乎", True),
    "weibo": ("微博", True),
    "sspai": ("少数派", False),
    "v2ex": ("V2EX", False),
    "github": ("GitHub", False),
    "36kr": ("36氪", False),
    "douban": ("豆瓣", False),
    "jike": ("即刻", False),
    "gcores": ("机核", False),
    "ifanr": ("爱范儿", False),
    "youtube": ("YouTube", False),
    "wechat": ("微信公众号", False),
    "xiaoyuzhou": ("小宇宙", False),
}

# 提示词：要求只输出 JSON，字段固定。用 Ollama 的 format="json" 兜住语法，
# 但**仍然**要按"模型可能不听话"来解析（见 parse_reply 的容错）。
_SYSTEM_PROMPT = (
    "你是「白读」RSS 阅读器的订阅意图解析器。用户会用中文口语说想订阅什么。\n"
    "只输出一个 JSON 对象，不要解释、不要 markdown 代码块。字段：\n"
    '  "platform": 平台代号，只能从这些里选 '
    '["bilibili","zhihu","weibo","sspai","v2ex","github","36kr","douban","jike",'
    '"gcores","ifanr","youtube","wechat","xiaoyuzhou",""]；'
    "拿不准或这句话根本不是在说要订谁，就用空字符串；\n"
    '  "keyword": 你要找的那个名字（UP主名 / 博主名 / 网站名 / 专栏名）'
    "—— 必须是**具体名字**；说不出来就空字符串，禁止把「财经」「新闻」这类泛词当名字；\n"
    '  "query": 建议直接拿去搜索的查询词（例如 "B站 半佛仙人"）；说不出来就空字符串；\n'
    '  "confidence": 0~1 的小数；\n'
    '  "reason": 20 字以内的理由。'
)

# 冷启动实测 11s（加载 2.5GB 模型），热了 2.7~4.2s。启动时预热一次，
# 之后 status() 就能告诉用户"已就绪"还是"还没热"。
_STATE: dict = {"warm_at": 0.0, "warm_ms": 0, "warm_ok": False, "detail": "还没预热",
                "warming": False}
_warm_lock = threading.Lock()

_SNIPPET_MAX = 40          # 模型给的 keyword/query 长度上限（防止它把整句塞进来）
_MAX_FIELD = 200           # 展示用 raw 的截断长度


# --------------------------------------------------------------------------- #
# 设置项读取（都带默认值，读不到就用默认，绝不让设置缺失把功能搞崩）
# --------------------------------------------------------------------------- #
def _get(key: str, default: str) -> str:
    try:
        import db

        val = db.get_setting(key)
        return default if val in (None, "") else str(val)
    except Exception as exc:  # noqa: BLE001
        log.warning("读设置 %s 失败，用默认值：%s", key, exc)
        return default


def enabled() -> bool:
    return _get("ai_enabled", "true").strip().lower() in ("1", "true", "yes", "on")


def base_url() -> str:
    return _get("ai_base_url", "http://127.0.0.1:11434").rstrip("/")


def model() -> str:
    return _get("ai_model", "qwen3:4b-instruct-2507-q4_K_M")


def timeout() -> float:
    try:
        return max(1.0, min(10.0, float(_get("ai_timeout_seconds", "6"))))
    except (TypeError, ValueError):
        return 6.0


def prewarm_enabled() -> bool:
    return _get("ai_prewarm", "true").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# 模型通信（唯一会发请求的两个函数；测试里替换 _chat 就能完全离线）
# --------------------------------------------------------------------------- #
def _list_models(probe_timeout: float) -> list[str]:
    resp = requests.get(f"{base_url()}/api/tags", timeout=probe_timeout)
    resp.raise_for_status()
    data = resp.json() or {}
    return [str(m.get("name") or "") for m in (data.get("models") or [])]


def _chat(messages: list[dict], wait: float) -> str:
    """向本地 Ollama 要一次 JSON 回复，返回原始文本（模型说的话，未解析）。"""
    resp = requests.post(
        f"{base_url()}/api/chat",
        timeout=wait,
        json={
            "model": model(),
            "messages": messages,
            "stream": False,
            "format": "json",                  # 让 Ollama 保证语法是 JSON
            "options": {"temperature": 0},     # 同一句话要可复现
        },
    )
    resp.raise_for_status()
    body = resp.json() or {}
    return ((body.get("message") or {}).get("content") or "").strip()


# --------------------------------------------------------------------------- #
# 解析模型回复（**纯函数**，零网络 —— 离线测试就是测它）
# --------------------------------------------------------------------------- #
def _extract_json(text: str) -> Optional[dict]:
    """从模型回复里抠出 JSON 对象：容忍 ```json 围栏、前后有废话、字段用了单引号。"""
    if not text:
        return None
    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _clean_str(value, limit: int = _SNIPPET_MAX) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    text = value.strip().strip('"').strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def parse_reply(text: str) -> dict:
    """
    把模型回复规范成 {ok, platform, platform_label, platform_raw, keyword, query,
    confidence, reason, error}。**纯函数**：不读设置、不发请求，方便离线用例表覆盖。

    容错范围（都实测过可能发生）：代码围栏、前后废话、字段缺失、confidence 是字符串、
    platform 不在白名单、回复为空、根本不是 JSON。
    """
    out = {"ok": False, "platform": "", "platform_label": "", "platform_raw": "",
           "keyword": "", "query": "", "confidence": 0.0, "reason": "",
           "error": None, "raw": (text or "")[:_MAX_FIELD]}
    obj = _extract_json(text or "")
    if obj is None:
        out["error"] = "模型没给出可解析的 JSON"
        return out

    code = _clean_str(obj.get("platform"), 32).lower()
    out["platform_raw"] = code
    if code in PLATFORMS:
        out["platform"] = code
        out["platform_label"] = PLATFORMS[code][0]
    else:
        out["platform"] = ""            # 不认识的平台当没猜出来，别拿它去搜
        out["platform_label"] = ""

    out["keyword"] = _clean_str(obj.get("keyword"))
    out["query"] = _clean_str(obj.get("query"), 60)
    out["reason"] = _clean_str(obj.get("reason"), 60)

    conf = obj.get("confidence", 0)
    try:
        out["confidence"] = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        out["confidence"] = 0.0

    if not out["keyword"] and not out["query"]:
        out["error"] = "模型认为这句话里没有具体的订阅对象"
        return out
    out["ok"] = True
    return out


# --------------------------------------------------------------------------- #
# 对外的三个函数：status / interpret / suggest
# --------------------------------------------------------------------------- #
def status(probe: bool = True, wait: Optional[float] = None) -> dict:
    """模型状态。probe=True 时真的去问一次 /api/tags（很快，实测 0.05s）。"""
    info = {
        "enabled": enabled(),
        "base_url": base_url(),
        "model": model(),
        "timeout_seconds": timeout(),
        "available": False,       # Ollama 服务在不在
        "model_present": False,   # 模型下没下
        "models": [],
        "warmed": bool(_STATE.get("warm_at")),
        "warming": bool(_STATE.get("warming")),
        "warm_ms": int(_STATE.get("warm_ms") or 0),
        "warm_ok": bool(_STATE.get("warm_ok")),
        "detail": str(_STATE.get("detail") or ""),
    }
    if not info["enabled"]:
        info["detail"] = "AI 功能已关闭（设置页 → AI 实验室 里可以打开）"
        return info
    if not probe:
        return info
    try:
        models = _list_models(wait or min(3.0, timeout()))
        info["models"] = models
        info["available"] = True
        info["model_present"] = model() in models
        if not info["model_present"]:
            info["detail"] = f"Ollama 在跑，但没有模型 {model()}（可 ollama pull 或改设置）"
        elif info["warmed"]:
            info["detail"] = "已就绪"
        else:
            info["detail"] = "Ollama 在跑，模型还没加载（首次使用会等 5~11 秒）"
    except requests.Timeout:
        info["detail"] = f"Ollama 没响应（{info['base_url']} 超时）"
    except Exception as exc:  # noqa: BLE001
        info["detail"] = f"连不上 Ollama（{info['base_url']}）：{type(exc).__name__}"
    return info


def warm_up() -> dict:
    """
    同步预热：让 Ollama 把模型加载进显存。只该在后台线程里调用 ——
    冷启动实测 11 秒，放在请求里会把用户晾在那儿。
    """
    if not enabled():
        return {"ok": False, "detail": "AI 功能已关闭"}
    started = time.time()
    try:
        _chat([{"role": "user", "content": "hi"}], wait=60)
        cost_ms = int((time.time() - started) * 1000)
        with _warm_lock:
            _STATE.update({"warm_at": time.time(), "warm_ms": cost_ms, "warm_ok": True,
                           "detail": "已就绪"})
        log.info("本地模型预热完成：%s（%d ms）", model(), cost_ms)
        return {"ok": True, "ms": cost_ms}
    except Exception as exc:  # noqa: BLE001
        with _warm_lock:
            _STATE.update({"warm_ok": False, "detail": f"预热失败：{type(exc).__name__}"})
        log.info("本地模型预热失败（不影响正常使用）：%s", exc)
        return {"ok": False, "detail": str(exc)}


def start_prewarm() -> bool:
    """
    起一个后台线程把模型加载起来（启动时叫一次；冷启动超时后也会再叫一次）。
    已经在预热就直接返回 False，免得用户连点起一堆线程。失败绝不上抛。
    """
    if not (enabled() and prewarm_enabled()):
        return False
    with _warm_lock:
        if _STATE.get("warming"):
            return False
        _STATE["warming"] = True

    def worker():
        try:
            warm_up()
        except Exception as exc:  # noqa: BLE001
            log.info("预热线程异常（忽略）：%s", exc)
        finally:
            with _warm_lock:
                _STATE["warming"] = False

    threading.Thread(target=worker, name="byread-ai-prewarm", daemon=True).start()
    return True


def interpret(query: str) -> dict:
    """
    把一句人话解析成订阅意图。**永远返回 dict，永远不抛异常。**

    返回 {ok, query, platform, platform_label, keyword, query_suggest, confidence,
          reason, raw, error, elapsed_ms, cold}
    """
    out = {"ok": False, "query": query, "platform": "", "platform_label": "",
           "keyword": "", "query_suggest": "", "confidence": 0.0, "reason": "",
           "raw": "", "error": None, "elapsed_ms": 0, "cold": not bool(_STATE.get("warm_at"))}
    query = (query or "").strip()
    if not query:
        out["error"] = "没有输入内容"
        return out
    if not enabled():
        out["error"] = "AI 功能已关闭（设置页 → AI 实验室 里可以打开）"
        return out

    started = time.time()
    try:
        content = _chat([{"role": "system", "content": _SYSTEM_PROMPT},
                         {"role": "user", "content": query[:200]}], wait=timeout())
    except requests.Timeout:
        # 冷启动实测 11 秒 > 默认 6 秒预算 —— 第一次点很可能就撞在这个超时上。
        # 这时候顺手在后台把它加载起来，并明确告诉用户"不是坏了，是在加载"。
        out["cold"] = not bool(_STATE.get("warm_at"))
        if out["cold"] and start_prewarm():
            out["error"] = (f"本地模型还没加载完（超过 {timeout():.0f} 秒没回话）。"
                            "已经在后台加载了，十几秒后再点一次就快了")
        else:
            out["error"] = f"本地模型太慢（超过 {timeout():.0f} 秒没回话），稍后再试一次"
    except requests.ConnectionError:
        out["error"] = f"连不上本地模型（{base_url()} 没在跑？）"
    except Exception as exc:  # noqa: BLE001
        log.info("AI 解析失败：%s", exc)
        out["error"] = f"本地模型调用失败：{type(exc).__name__}"
    else:
        parsed = parse_reply(content)
        out.update({"ok": parsed["ok"], "platform": parsed["platform"],
                    "platform_label": parsed["platform_label"],
                    "keyword": parsed["keyword"], "query_suggest": parsed["query"],
                    "confidence": parsed["confidence"], "reason": parsed["reason"],
                    "raw": parsed["raw"], "error": parsed["error"]})
        if out["ok"]:
            log.info("AI 解析 %r → 平台=%s 名字=%r 查询=%r（%.1f）",
                     query, out["platform"] or "-", out["keyword"],
                     out["query_suggest"], out["confidence"])
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    return out


def suggest(query: str, limit: int = 3) -> dict:
    """
    AI 兜底：解析 + 拿"平台 + 名字"去平台搜索，产出**一律标 loose 的候选**。

    注意这里**故意不跑 P0 的相关度闸门**：那道闸门判的是"用户给的名字 vs 候选名"，
    而 AI 这条路线的输入是"猜出来的泛词"（例如「财经」），闸门会把结果全滤光，
    AI 就永远给不出任何东西。安全性靠三道别的保险：
        1. 候选只能来自平台搜索接口的真实返回（AI 编不出不存在的账号）；
        2. 全部 match="loose" —— 前端默认折叠，用户点开才看得见；
        3. 候选的 detail 里写明"AI 猜的"+ 理由，用户可以一眼判断对不对。
    """
    out = {"ok": False, "query": query, "rewritten": "", "platform": "",
           "platform_label": "", "keyword": "", "reason": "", "raw": "",
           "candidates": [], "note": "", "error": None, "elapsed_ms": 0}
    started = time.time()
    parsed = interpret(query)
    out.update({"platform": parsed["platform"], "platform_label": parsed["platform_label"],
                "keyword": parsed["keyword"], "reason": parsed["reason"],
                "raw": parsed["raw"], "rewritten": parsed["query_suggest"],
                "error": parsed["error"]})
    if not parsed["ok"]:
        if parsed["error"]:
            out["note"] = parsed["error"]
        out["elapsed_ms"] = int((time.time() - started) * 1000)
        return out

    code = parsed["platform"]
    keyword = parsed["keyword"]
    searchable = code in PLATFORMS and PLATFORMS[code][1]
    if not (searchable and keyword):
        # 猜不出"能按名字搜"的平台/名字 —— 只把改写后的查询词给用户，让他自己点着再搜
        out["ok"] = True
        out["note"] = ("AI 没给出具体的名字，只把它理解成了一句查询："
                       f"「{parsed['query_suggest'] or keyword}」")
        out["elapsed_ms"] = int((time.time() - started) * 1000)
        return out

    try:
        import rsshub

        found = rsshub.platform_candidates(code, keyword, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.info("AI 候选搜索失败：%s", exc)
        out["error"] = f"平台搜索失败：{type(exc).__name__}"
        out["elapsed_ms"] = int((time.time() - started) * 1000)
        return out

    label = PLATFORMS[code][0]
    reason = parsed["reason"] or "看不出理由"
    cands = []
    for c in found:
        c = dict(c)
        c["match"] = "loose"                 # ← 本次改造的核心：AI 的东西一律不确定
        c["source"] = "ai"
        base_detail = c.get("detail") or ""
        c["detail"] = f"AI 猜的：{reason}（{label}）" + (f" · {base_detail}" if base_detail else "")
        cands.append(c)
    out["ok"] = True
    out["candidates"] = cands
    out["note"] = (f"AI 把这句话理解成「{label} · {keyword}」，"
                   f"下面是它猜的 {len(cands)} 个候选（不确定，默认收起）")
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    return out
