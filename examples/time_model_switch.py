#!/usr/bin/env python3
"""
主模型峰谷时段切换器 v4.0 (2026-08-05)
修复 v3 孤儿文件 bug: 旧版改 ~/.hermes/config.yaml(不生效), 本版改真实配置 /opt/data/config.yaml

策略 (北京时间):
  高峰时段 09:00-11:59 / 14:00-17:59 → 主模型 opencode-zen-2 (Key_3, 免费)
  低谷时段 其余 → 主模型 deepseek 官方 (deepseek-v4-flash 付费, 谷时便宜)
定时任务(LLM cron)统一用 opencode-zen-2 免费 (2026-08-06 起)

幂等: 已在目标模式则只输出不重启; 跨时段才改配置并重启 gateway。
"""
import re, subprocess, sys, json
from datetime import datetime, timezone, timedelta

CONFIG = "/opt/data/config.yaml"
BEIJING = timezone(timedelta(hours=8))

# 高峰 (opencode 主) fallback 链: opencode-zen-3 → sensenova → openrouter (全免费, 无 deepseek 付费兜底)
# 注意: 字符串必须以 \n 结尾, 否则 re.sub 替换 fallback 段时会与下一顶级键粘连
# (曾导致 config.yaml 损坏: "provider: deepseekbrowser:")
PEAK_FALLBACK = """fallback_providers:
  - model: deepseek-v4-flash-free
    provider: custom:opencode-zen-3
  - model: deepseek-v4-flash
    provider: custom:sensenova
  - model: openrouter/free
    provider: openrouter
"""

# 低谷 (deepseek 主) fallback 链: opencode-zen-2 → opencode-zen-3 → sensenova → openrouter
OFFPEAK_FALLBACK = """fallback_providers:
  - model: deepseek-v4-flash-free
    provider: custom:opencode-zen-2
  - model: deepseek-v4-flash-free
    provider: custom:opencode-zen-3
  - model: deepseek-v4-flash
    provider: custom:sensenova
  - model: openrouter/free
    provider: openrouter
"""

# ── [FreeLLM Router 2026-08-07] 动态 fallback 链 ──
# 读 llm_chain.json (测评路由推荐), 存在则用推荐顺序, 否则用上面硬编码
PROVIDER_MAP = {
    "opencode-zen-2": ("custom:opencode-zen-2", "deepseek-v4-flash-free"),
    "opencode-zen-3": ("custom:opencode-zen-3", "deepseek-v4-flash-free"),
    "sensenova": ("custom:sensenova", "deepseek-v4-flash"),
    "openrouter": ("openrouter", "openrouter/free"),
    "deepseek": ("deepseek", "deepseek-v4-flash"),
}

def _load_dynamic_fallback(exclude_primary=None, max_items=3, include_paid=False):
    """从 llm_chain.json 生成 fallback_providers YAML (失败返回 None 用硬编码)"""
    try:
        chain = json.load(open("/opt/data/scripts/llm_chain.json"))
        items = chain["tiers"]["T1_主对话"]
    except Exception:
        return None
    lines = ["fallback_providers:"]
    added = 0
    for it in items:
        p = it["provider"]
        if exclude_primary and p == exclude_primary:
            continue
        if p in PROVIDER_MAP:
            hermes_p, model = PROVIDER_MAP[p]
            if not include_paid and hermes_p in ("deepseek", "custom:deepseek-fallback"):
                continue
            lines.append(f"  - model: {model}")
            lines.append(f"    provider: {hermes_p}")
            added += 1
            if added >= max_items:
                break
    # 免费兜底 openrouter 始终保留
    if not any("openrouter" in l for l in lines):
        lines.append("  - model: openrouter/free")
        lines.append("    provider: openrouter")
    return "\n".join(lines) + "\n"

def now_cst():
    return datetime.now(BEIJING)

def target_mode(now):
    """CST 9-11 或 14-17 点 = peak, 其余 offpeak"""
    h = now.hour
    return "peak" if (9 <= h <= 11) or (14 <= h <= 17) else "offpeak"

def read_cfg():
    with open(CONFIG) as f:
        return f.read()

def current_mode(cfg):
    """基于 model 段精确判断, 避免子串误判:
    - 必须匹配完整行 (deepseek-v4-flash-free ≠ deepseek-v4-flash)
    - fallback 链里的 provider: deepseek 不能误触发 offpeak
    """
    model_block = cfg.split("fallback_providers:")[0]  # 只看 model 段
    if re.search(r'default:\s*deepseek-v4-flash-free\s*$', model_block, flags=re.M) \
            and re.search(r'^  provider:\s*custom:opencode-zen-2\s*$', model_block, flags=re.M):
        return "peak"
    if re.search(r'default:\s*deepseek-v4-flash\s*$', model_block, flags=re.M) \
            and re.search(r'^  provider:\s*deepseek\s*$', model_block, flags=re.M):
        return "offpeak"
    return "unknown"

# 完整 model 块 (切换时整块替换, 避免 base_url/api_key 残留导致打到错误端点)
PEAK_MODEL = """model:
  default: deepseek-v4-flash-free
  provider: custom:opencode-zen-2
  base_url: https://opencode.ai/zen/v1/
  api_key: ${OPENCODE_ZEN_API_KEY_3}
  api_mode: chat_completions
"""

OFFPEAK_MODEL = """model:
  default: deepseek-v4-flash
  provider: deepseek
  base_url: https://api.deepseek.com/v1
  api_key: ${DEEPSEEK_API_KEY}
  api_mode: chat_completions
"""

def switch_to(cfg, mode):
    """返回切换后的配置文本 (整块替换 model 段, 保证 base_url/api_key 同步)"""
    model_block = PEAK_MODEL if mode == "peak" else OFFPEAK_MODEL
    # 匹配 ^model: 开头的完整块 (到下一个顶级键或文件尾)
    cfg = re.sub(r'^model:\n(?:  .*\n)*', model_block, cfg, count=1, flags=re.M)
    if mode == "peak":
        dyn = _load_dynamic_fallback(exclude_primary="opencode-zen-2", max_items=3)
        fb_block = dyn if dyn else PEAK_FALLBACK
        cfg = re.sub(r'^fallback_providers:.*?(?=^\S+:$)', fb_block, cfg, count=1, flags=re.S | re.M)
    else:
        dyn = _load_dynamic_fallback(exclude_primary=None, max_items=4)
        fb_block = dyn if dyn else OFFPEAK_FALLBACK
        cfg = re.sub(r'^fallback_providers:.*?(?=^\S+:$)', fb_block, cfg, count=1, flags=re.S | re.M)
    return cfg

def main():
    dry = "--dry" in sys.argv
    now = now_cst()
    want = target_mode(now)
    cfg = read_cfg()
    cur = current_mode(cfg)

    print(f"模型切换器 v4.0 · {now.strftime('%m-%d %H:%M')} CST · 目标={want} · 当前={cur}")

    if cur == want:
        print(f"  [IDLE] 已在 {want} 模式, 无需切换")
        return 0

    new_cfg = switch_to(cfg, want)
    # 校验: 切换后模式必须正确
    if current_mode(new_cfg) != want:
        print(f"  [FAIL] 切换校验失败: {current_mode(new_cfg)} != {want}")
        return 1

    if dry:
        print("  [DRY] 模拟切换 OK, 未写文件")
        return 0

    with open(CONFIG, "w") as f:
        f.write(new_cfg)
    print(f"  [SWITCH] ✅ → {want} 模式, 重启 gateway 生效")

    subprocess.run(["pkill", "-f", "hermes gateway run --replace"], capture_output=True, timeout=5)
    subprocess.Popen(["/opt/hermes/.venv/bin/hermes", "gateway", "run", "--replace"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("  [GATEWAY] 已重启")
    return 0

if __name__ == "__main__":
    sys.exit(main())
