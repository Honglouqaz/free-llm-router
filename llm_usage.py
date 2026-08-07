#!/usr/bin/env python3
"""
llm_usage.py — LLM 用量统计与成本报告 (M4)
============================================
[FreeLLM Router 2026-08-07] 从 Hermes state.db (session_model_usage) 统计
各 provider/model 的调用量、token 消耗、估算成本与免费节省。

输出: llm_usage.json (供 DataHub 模型中心页) + 终端报告

用法:
  python3 llm_usage.py            # 最近 24h + 7d + 30d 汇总
"""
import json, os, sys, sqlite3, datetime

STATE_DB = "/opt/data/state.db"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_usage.json")
CST = datetime.timezone(datetime.timedelta(hours=8))

# 付费基准价 (美元/M token, 用于计算节省; 以 deepseek 官方价近似)
PAID_REF = {"input": 0.56, "output": 1.68, "cache_read": 0.028}  # deepseek-v4-flash 近似价 $/M

def query(db, hours):
    conn = sqlite3.connect(db, timeout=10)
    cutoff = datetime.datetime.now().timestamp() - hours * 3600
    rows = conn.execute("""
        SELECT billing_provider, model,
               SUM(api_call_count) calls,
               SUM(input_tokens) in_tok, SUM(output_tokens) out_tok,
               SUM(cache_read_tokens) cache_r, SUM(cache_write_tokens) cache_w,
               SUM(actual_cost_usd) actual_cost
        FROM session_model_usage
        WHERE last_seen >= ?
        GROUP BY billing_provider, model
        ORDER BY calls DESC""", (cutoff,)).fetchall()
    conn.close()
    return rows

def fmt_metric(v):
    if v is None: return 0
    if v >= 1e9: return f"{v/1e9:.1f}B"
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return str(int(v))

def build():
    result = {"generated_at": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M"), "periods": {}}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        rows = query(STATE_DB, hours)
        prov_stats = {}
        total = {"calls": 0, "in_tok": 0, "out_tok": 0, "cache_r": 0, "est_cost": 0.0, "saved": 0.0}
        for prov, model, calls, in_tok, out_tok, cache_r, cache_w, actual_cost in rows:
            calls = calls or 0; in_tok = in_tok or 0; out_tok = out_tok or 0
            cache_r = cache_r or 0
            # 估算成本 (免费通道成本 0)
            est = float(actual_cost or 0.0)
            # 节省 = 若用付费基准要花的钱 - 实际
            would_pay = (in_tok * PAID_REF["input"] + out_tok * PAID_REF["output"] + cache_r * PAID_REF["cache_read"]) / 1e6
            saved = would_pay - est
            key = prov or "unknown"
            p = prov_stats.setdefault(key, {"calls": 0, "in_tok": 0, "out_tok": 0, "cache_r": 0, "est_cost": 0.0, "models": {}})
            p["calls"] += calls; p["in_tok"] += in_tok; p["out_tok"] += out_tok; p["cache_r"] += cache_r; p["est_cost"] += est
            p["models"][model or "?"] = {"calls": calls, "in_tok": in_tok, "out_tok": out_tok, "cache_r": cache_r}
            total["calls"] += calls; total["in_tok"] += in_tok; total["out_tok"] += out_tok
            total["cache_r"] += cache_r; total["est_cost"] += est; total["saved"] += saved
        result["periods"][label] = {
            "providers": prov_stats,
            "total": {"calls": total["calls"], "in_tok": total["in_tok"], "out_tok": total["out_tok"],
                      "cache_r": total["cache_r"], "est_cost": round(total["est_cost"], 4),
                      "saved_usd": round(total["saved"], 4)},
        }
    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return result

def main():
    data = build()
    print(f"📊 LLM 用量统计 | {data['generated_at']}")
    for label in ["24h", "7d", "30d"]:
        t = data["periods"][label]["total"]
        print(f"\n[{label}] 调用{t['calls']}次 | in={fmt_metric(t['in_tok'])} out={fmt_metric(t['out_tok'])} cache={fmt_metric(t['cache_r'])} | 实际${t['est_cost']:.4f} | 节省${t['saved_usd']:.2f}")
        for prov, p in sorted(data["periods"][label]["providers"].items(), key=lambda x: -x[1]["calls"]):
            print(f"  {prov}: {p['calls']}次 {fmt_metric(p['in_tok'])}in {fmt_metric(p['out_tok'])}out")
    print(f"\n→ {OUT}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
