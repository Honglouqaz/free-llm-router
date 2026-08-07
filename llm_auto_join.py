#!/usr/bin/env python3
"""
llm_auto_join.py — 新 Provider 自动入库 (M3)
=============================================
[FreeLLM Router 2026-08-07] 扫描 llm_providers.json 中 enabled=false 但 key 已配置
的 provider (如 zhipu/modelscope/siliconflow), 自动测评, 综合分 ≥ AUTO_JOIN_THRESHOLD
(0.85) 则自动启用并写入配置。

流程: 检测新 key → 测评 (复用 llm_benchmark) → 达标自动启用 → 报告
未达标: 保持禁用, 分数记录在 llm_quality.db (可查)

用法:
  python3 llm_auto_join.py            # 扫描+测评+自动启用
  python3 llm_auto_join.py --dry      # 只测评不写配置
"""
import json, os, sys, sqlite3, datetime, importlib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE_DIR, "llm_providers.json")
ENV_FILE = "/opt/data/.env"
AUTO_JOIN_THRESHOLD = 0.85

sys.path.insert(0, BASE_DIR)
import llm_benchmark as bench

def load_env():
    env = {}
    try:
        for line in open(ENV_FILE, errors="ignore"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return env

def main():
    dry = "--dry" in sys.argv
    env = load_env()
    cfg = json.load(open(CONFIG))
    now = datetime.datetime.now(bench.CST).strftime("%Y-%m-%d %H:%M")
    print(f"🔌 LLM 自动入库 | {now} | {'DRY(不写入)' if dry else '写入模式'}")

    candidates = []
    for p in cfg.get("providers", []):
        if p.get("enabled"):
            continue
        key = env.get(p.get("api_key_env", ""), "")
        if key:
            candidates.append({**p, "key": key})
    if not candidates:
        print("  ℹ️ 无待配置 provider (key 未就位)。注册免费 API 后把 key 加到 .env 即可自动接入。")
        return 0

    print(f"  发现 {len(candidates)} 个待测 provider")
    joined = []
    for c in candidates:
        name = c["name"]
        base = bench.SENSENOVA_OVERRIDE if name == "sensenova" else c["base_url"]
        provider = {"name": name, "key": c["key"], "primary_model": c["primary_model"], "base_url_eff": base}
        print(f"  ▶ 测评 {name} ({c['primary_model']})...")
        r = bench.run_benchmark(provider)
        print(f"     综合分 {r['total']:.3f} (门槛 {AUTO_JOIN_THRESHOLD}) → {'✅ 达标' if r['total'] >= AUTO_JOIN_THRESHOLD else '❌ 未达标'}")
        # 记录测评
        bench.save_db([r], datetime.datetime.now(bench.CST).strftime("%Y-%m-%d"))
        if r["total"] >= AUTO_JOIN_THRESHOLD:
            joined.append((name, r["total"]))
            if not dry:
                for p in cfg["providers"]:
                    if p["name"] == name:
                        p["enabled"] = True
                        p["note"] = (p.get("note", "") + f" | [auto-join {now}] 测评{r['total']:.2f}达标自动启用").strip()

    if joined:
        if not dry:
            json.dump(cfg, open(CONFIG, "w"), ensure_ascii=False, indent=1)
            print(f"\n🎉 自动启用 {len(joined)} 个: {', '.join(f'{n}({s:.2f})' for n, s in joined)}")
            print("   → 已写入 llm_providers.json, 网关/切换器下轮自动使用")
        else:
            print(f"\n🎉 DRY: 将启用 {len(joined)} 个: {', '.join(f'{n}({s:.2f})' for n, s in joined)}")
    else:
        print("\n  ℹ️ 本轮无达标 provider")
    return 0

if __name__ == "__main__":
    sys.exit(main())
