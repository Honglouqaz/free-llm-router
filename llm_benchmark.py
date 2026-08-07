#!/usr/bin/env python3
"""
llm_benchmark.py — 免费 LLM Provider 质量测评器 (M1)
====================================================
[FreeLLM Router 2026-08-07] 每日自动对已接入+候选 provider 跑固定测试集,
产出质量/速度/稳定性三维评分, 入库 llm_quality.db, 输出 llm_rank.json 排行榜。

评分模型: 综合分 = 质量×0.5 + 速度×0.3 + 稳定性×0.2
测试集: 5 任务 (摘要/逻辑推理/JSON结构化/代码/延迟), 纯规则评分零 LLM 成本。

用法:
    python3 llm_benchmark.py                 # 测评 enabled 免费 provider
    python3 llm_benchmark.py --include-paid  # 含付费 provider (deepseek)
    python3 llm_benchmark.py --provider X    # 只测指定 provider
"""
import json, os, sys, sqlite3, time, urllib.request, urllib.error, datetime, re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE_DIR, "llm_providers.json")
DB = os.path.join(BASE_DIR, "llm_quality.db")
RANK_OUT = os.path.join(BASE_DIR, "llm_rank.json")
ENV_FILE = "/opt/data/.env"
UA = "hermes-cli/0.20.0"
CST = datetime.timezone(datetime.timedelta(hours=8))

# sensenova 特殊端点 (api.sensenova.cn/compatible-mode 是 OpenAI 兼容真实路径)
SENSENOVA_OVERRIDE = "https://token.sensenova.cn/v1"

# ── 环境变量加载 ──
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

ENV = load_env()

def call_llm(base_url, key, model, messages, max_tokens=200, temperature=0.3, timeout=30):
    """OpenAI 兼容调用, 返回 (text, latency_s) 或 (None, error)"""
    if key.startswith("sk-") is False and not key:
        return None, "no_key"
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({"model": model, "messages": messages,
                       "max_tokens": max_tokens, "temperature": temperature}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}",
        "User-Agent": UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
            text = d["choices"][0]["message"]["content"].strip()
            return text, time.time() - t0
    except urllib.error.HTTPError as e:
        return None, f"HTTP{e.code}"
    except Exception as e:
        return None, type(e).__name__

# ── 5 个测试任务 (纯规则评分) ──

T_SUMMARY_TEXT = (
    "北京时间8月7日凌晨，美联储主席鲍威尔在杰克逊霍尔会议上表示，如果通胀数据持续回落，"
    "美联储可能在未来几个月内开始降息。这一表态推动美股三大指数集体收涨，纳指涨1.8%。"
    "同时，比特币价格突破66000美元，创下近三个月新高，以太坊跟随上涨5.2%。"
    "分析师认为，降息预期改善市场流动性环境，风险资产短期获得支撑。"
)
T_SUMMARY_ENTITIES = ["美联储", "鲍威尔", "降息", "比特币", "66000"]

def task_summary(base_url, key, model):
    """摘要质量: 长度合理 + 关键实体保留"""
    text, lat = call_llm(base_url, key, model,
                         [{"role": "user", "content": f"用不超过100字总结下面这段新闻:\n{T_SUMMARY_TEXT}"}])
    if text is None:
        return 0.0, lat
    kept = sum(1 for e in T_SUMMARY_ENTITIES if e in text)
    score = 0.2 * min(len(text) / 80, 1.0) + 0.2 * (kept / len(T_SUMMARY_ENTITIES))
    return min(score, 1.0), lat

def task_logic(base_url, key, model):
    """逻辑推理: 标准题, 答案关键词匹配"""
    q = ("有三个人在餐厅吃饭，账单是30元。每人付了10元。老板说打折只要25元，"
         "让服务员退回5元。服务员偷拿了2元，退给每人1元。现在每人实际付9元，3×9=27元，"
         "加上服务员偷的2元=29元。问：还有1元去哪了？请直接回答：钱没有丢，这是一道误导题。")
    text, lat = call_llm(base_url, key, model,
                         [{"role": "user", "content": q}], max_tokens=150)
    if text is None:
        return 0.0, lat
    score = 1.0 if ("没有丢" in text or "误导" in text or "27" in text and "2" in text and "25" in text) else 0.0
    return score, lat

def task_json(base_url, key, model):
    """JSON 结构化: 必须输出合法 JSON"""
    text, lat = call_llm(base_url, key, model,
                         [{"role": "user", "content": "输出一个JSON对象，字段: name(字符串), age(数字), tags(字符串数组)。只输出JSON，不要其他内容。"}],
                         max_tokens=100)
    if text is None:
        return 0.0, lat
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return 0.0, lat
    try:
        d = json.loads(m.group(0))
        score = 0.0
        if isinstance(d.get("name"), str): score += 0.4
        if isinstance(d.get("age"), (int, float)): score += 0.3
        if isinstance(d.get("tags"), list): score += 0.3
        return score, lat
    except Exception:
        return 0.0, lat

def task_code(base_url, key, model):
    """代码: 生成斐波那契函数, 语法检查 + 正确性"""
    text, lat = call_llm(base_url, key, model,
                         [{"role": "user", "content": "用Python写一个函数 fib(n) 返回第n个斐波那契数。只输出代码。"}],
                         max_tokens=150)
    if text is None:
        return 0.0, lat
    code_m = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    code = code_m.group(1) if code_m else text
    code = code.strip().strip("`").strip()
    if "def fib" not in code:
        return 0.1, lat
    try:
        ns = {}
        compile(code, "<bench>", "exec")
        exec(code, ns)
        if ns["fib"](10) == 55:
            return 1.0, lat
        return 0.5, lat
    except Exception:
        return 0.3, lat

def task_latency(base_url, key, model):
    """延迟: 最小请求计时 (取 min, 排除冷启动)"""
    best = 999
    for _ in range(2):
        _, lat = call_llm(base_url, key, model,
                          [{"role": "user", "content": "hi"}], max_tokens=5, timeout=20)
        if isinstance(lat, (int, float)) and lat < best:
            best = lat
    if best >= 999:
        return None
    # 速度分: <2s=1.0, 2-5s=0.7, 5-10s=0.4, >10s=0.2
    if best < 2: return 1.0
    if best < 5: return 0.7
    if best < 10: return 0.4
    return 0.2

# ── 主流程 ──
def get_providers(include_paid=False):
    cfg = json.load(open(CONFIG))
    provs = []
    for p in cfg.get("providers", []):
        if not p.get("enabled"):
            continue
        if p["name"] == "deepseek" and not include_paid:
            continue
        key = ENV.get(p.get("api_key_env", ""), "")
        base = SENSENOVA_OVERRIDE if p["name"] == "sensenova" else p["base_url"]
        provs.append({**p, "key": key, "base_url_eff": base})
    return provs

def run_benchmark(provider):
    name, key, model, base = provider["name"], provider["key"], provider["primary_model"], provider["base_url_eff"]
    if not key:
        return {"provider": name, "model": model, "quality": 0.0, "speed": 0.0,
                "stability": 0.0, "total": 0.0, "latency": None, "status": "no_key",
                "detail": "API key 未配置"}
    tasks = [("summary", task_summary), ("logic", task_logic), ("json", task_json), ("code", task_code)]
    results = {}
    ok_count = 0
    for tname, tfn in tasks:
        score, lat = tfn(base, key, model)
        results[tname] = score
        if score > 0: ok_count += 1
    lat_score = task_latency(base, key, model)
    results["latency"] = lat_score
    quality = sum(results.get(k, 0) for k in ["summary", "logic", "json", "code"]) / 4
    speed = lat_score if lat_score is not None else 0.0
    stability = ok_count / len(tasks)
    total = round(quality * 0.5 + speed * 0.3 + stability * 0.2, 3)
    return {"provider": name, "model": model,
            "quality": round(quality, 3), "speed": round(speed, 3),
            "stability": round(stability, 3), "total": total,
            "latency": round(results.get("latency") or 0, 2) if results.get("latency") else None,
            "status": "ok" if quality > 0 else "fail",
            "detail": json.dumps({k: round(v, 2) if isinstance(v, float) else v for k, v in results.items()}, ensure_ascii=False)}

def save_db(rows, date_str):
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS quality_snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, provider TEXT, model TEXT,
        quality REAL, speed REAL, stability REAL, total REAL,
        latency REAL, status TEXT, detail TEXT,
        created_at TEXT)""")
    for r in rows:
        conn.execute("INSERT INTO quality_snapshots(date, provider, model, quality, speed, stability, total, latency, status, detail, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (date_str, r["provider"], r["model"], r["quality"], r["speed"], r["stability"], r["total"],
                      r.get("latency"), r["status"], r["detail"], datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    # 保留 30 天
    conn.execute("DELETE FROM quality_snapshots WHERE date < date('now', '-30 days')")
    conn.commit()
    conn.close()

def main():
    include_paid = "--include-paid" in sys.argv
    only = None
    if "--provider" in sys.argv:
        only = sys.argv[sys.argv.index("--provider") + 1]
    provs = get_providers(include_paid)
    if only:
        provs = [p for p in provs if p["name"] == only]
    date_str = datetime.datetime.now(CST).strftime("%Y-%m-%d")
    print(f"🧪 LLM 测评器 | {date_str} | {len(provs)} 个 provider")
    rows = []
    for p in provs:
        print(f"  ▶ {p['name']} ({p['primary_model']})...")
        r = run_benchmark(p)
        rows.append(r)
        lat_str = f" ({r['latency']}s)" if r.get('latency') else ""
        print(f"     {r['status']}: 质量{r['quality']:.2f} 速度{r['speed']:.2f} 稳定{r['stability']:.2f} 综合{r['total']:.2f}{lat_str}")
    save_db(rows, date_str)
    # 排行榜输出
    ranked = sorted(rows, key=lambda x: -x["total"])
    out = {"date": date_str, "rank": [
        {"rank": i + 1, "provider": r["provider"], "model": r["model"], "total": r["total"],
         "quality": r["quality"], "speed": r["speed"], "stability": r["stability"],
         "latency": r.get("latency"), "status": r["status"]} for i, r in enumerate(ranked)]}
    with open(RANK_OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n📊 排行榜 → {RANK_OUT}")
    for r in ranked:
        print(f"  #{r['rank'] if 'rank' in r else ranked.index(r)+1} {r['provider']} 综合{r['total']:.2f}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
