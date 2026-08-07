#!/usr/bin/env python3
"""
llm_router.py — 免费模型智能路由 (M2)
=====================================
[FreeLLM Router 2026-08-07] 任务分级 × 测评评分 × 健康状态 → 动态推荐链。

任务分级:
  T0 交易决策    质量优先, 允许付费兜底 (deepseek 最后)
  T1 主对话      综合分优先, 免费为主, 付费最后
  T2 定时任务    稳定性优先, 必须免费 (zen-3 → sensenova)
  T3 批量/总结   速度优先, 必须免费

数据源:
  llm_rank.json     — 测评排行榜 (当日, 无则历史均值)
  llm_providers.json — 配置 (enabled/key/model)
  free_llm_gateway   — 健康状态 (8099/healthz, 不可达则跳过健康调整)

输出: llm_chain.json — 各任务级推荐 fallback 链
      {t0: [{provider, model, score, reason}...], t1: ..., t2: ..., t3: ...}

用法:
  python3 llm_router.py          # 生成 llm_chain.json
  python3 llm_router.py --show   # 打印当前推荐链
"""
import json, os, sys, sqlite3, urllib.request, datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RANK = os.path.join(BASE_DIR, "llm_rank.json")
CONFIG = os.path.join(BASE_DIR, "llm_providers.json")
DB = os.path.join(BASE_DIR, "llm_quality.db")
CHAIN_OUT = os.path.join(BASE_DIR, "llm_chain.json")
GATEWAY_HEALTH = "http://127.0.0.1:8099/healthz"
CST = datetime.timezone(datetime.timedelta(hours=8))

# 排除付费 provider 的任务级 (T1/T2/T3 免费约束)
PAID = {"deepseek"}
FREE_ONLY = {"T2", "T3"}

def load_scores():
    """测评分: 当日 rank 优先, 无则查历史均值 (最近7天)"""
    scores = {}
    try:
        rank = json.load(open(RANK))
        for r in rank.get("rank", []):
            scores[r["provider"]] = {
                "total": r["total"], "quality": r["quality"],
                "speed": r["speed"], "stability": r["stability"],
                "date": rank.get("date", ""), "status": r.get("status", "ok")}
    except Exception:
        pass
    # 历史均值补充 (当日无分或 status=fail)
    try:
        conn = sqlite3.connect(DB, timeout=5)
        conn.row_factory = sqlite3.Row
        for row in conn.execute("""
            SELECT provider, AVG(total) total, AVG(quality) quality,
                   AVG(speed) speed, AVG(stability) stability
            FROM quality_snapshots
            WHERE date >= date('now','-7 days') AND status='ok'
            GROUP BY provider"""):
            p = row["provider"]
            if p not in scores or scores[p]["status"] != "ok":
                scores[p] = {"total": round(row["total"], 3), "quality": round(row["quality"], 3),
                             "speed": round(row["speed"], 3), "stability": round(row["stability"], 3),
                             "date": "历史均值", "status": "ok"}
    except Exception:
        pass
    return scores

def load_health():
    """网关健康: {provider: healthy/cooldown}"""
    try:
        with urllib.request.urlopen(GATEWAY_HEALTH, timeout=3) as r:
            d = json.loads(r.read())
            return d.get("providers", {})
    except Exception:
        return {}

def load_enabled():
    cfg = json.load(open(CONFIG))
    return {p["name"]: p for p in cfg.get("providers", []) if p.get("enabled")}

def rank_providers(scores, enabled, health, key_fn, free_only, tier):
    """按 key_fn 打分排序, 附加健康/冷却调整"""
    items = []
    for name, p in enabled.items():
        if free_only and name in PAID:
            continue
        s = scores.get(name, {"total": 0.5, "quality": 0.5, "speed": 0.5, "stability": 0.5, "date": "无测评", "status": "unknown"})
        base = key_fn(s)
        # 健康调整: 冷却中降 0.2, 未知不调
        h = health.get(name, {})
        if isinstance(h, dict):
            if h.get("status") == "cooldown" or h.get("cooldown_until"):
                base -= 0.2
        items.append({"provider": name, "model": p.get("primary_model", ""),
                      "score": round(base, 3), "date": s["date"], "status": s["status"]})
    items.sort(key=lambda x: -x["score"])
    return items

def build_chain():
    scores = load_scores()
    health = load_health()
    enabled = load_enabled()
    now = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M")

    chain = {"generated_at": now, "note": "FreeLLM Router 推荐链, 供 time_model_switch / cron_model_manager 参考", "tiers": {}}

    # T0 交易决策: 质量优先, 允许付费兜底
    chain["tiers"]["T0_交易决策"] = rank_providers(
        scores, enabled, health, lambda s: s["quality"] * 0.6 + s["stability"] * 0.4, free_only=False, tier="T0")

    # T1 主对话: 综合分优先, 免费为主
    chain["tiers"]["T1_主对话"] = rank_providers(
        scores, enabled, health, lambda s: s["total"], free_only=False, tier="T1")

    # T2 定时任务: 稳定性优先, 必须免费
    chain["tiers"]["T2_定时任务"] = rank_providers(
        scores, enabled, health, lambda s: s["stability"] * 0.5 + s["speed"] * 0.3 + s["quality"] * 0.2, free_only=True, tier="T2")

    # T3 批量/总结: 速度优先, 必须免费
    chain["tiers"]["T3_批量"] = rank_providers(
        scores, enabled, health, lambda s: s["speed"] * 0.6 + s["stability"] * 0.4, free_only=True, tier="T3")

    json.dump(chain, open(CHAIN_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return chain

def main():
    if "--show" in sys.argv:
        try:
            chain = json.load(open(CHAIN_OUT))
        except Exception:
            chain = build_chain()
    else:
        chain = build_chain()
    print(f"🧭 LLM Router | {chain['generated_at']}")
    for tier, items in chain["tiers"].items():
        print(f"\n  [{tier}]")
        for i, it in enumerate(items[:5]):
            print(f"    {i+1}. {it['provider']} ({it['model']}) 分{it['score']} [{it['date']}]")
    print(f"\n→ {CHAIN_OUT}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
