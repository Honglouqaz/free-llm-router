#!/usr/bin/env python3
"""
free_llm_gateway.py — 自研免费 LLM 聚合网关 (freellm 替代, 2026-08-06)
OpenAI 兼容 /v1/chat/completions + /v1/models
路由: 配置顺序 + 健康度 + 记忆冷却 → 选最优 provider, 失败/限流自动 failover

用法:
  python3 free_llm_gateway.py            # 启动 (Flask, 127.0.0.1:8099)
  python3 free_llm_gateway.py --ping     # 探测所有 provider 健康度
  python3 free_llm_gateway.py --test     # 端到端测试 (chat)
"""
import json, os, sys, time, threading, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

try:
    from flask import Flask, request, jsonify, Response, stream_with_context
except ImportError:
    print("❌ 需要 Flask: /opt/data/.venv/bin/pip install flask")
    sys.exit(1)

PORT = int(os.environ.get("GATEWAY_PORT", "8099"))
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_providers.json")
BEIJING = timezone(timedelta(hours=8))

app = Flask(__name__)

# ── 状态 ──
provider_state = {}   # name -> {"healthy": bool, "fail_count": int, "cooldown_until": float, "last_ping": float}


def load_providers():
    with open(CONFIG) as f:
        return json.load(f)["providers"]


def get_key(env_name):
    v = os.environ.get(env_name, "")
    if v:
        return v.strip().strip('"').strip("'")
    for line in open("/opt/data/.env", errors="ignore"):
        line = line.strip()
        if line.startswith(env_name + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _call(provider, payload, timeout=30):
    """转发 chat 请求, 返回 (ok, body_bytes_or_None)"""
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {get_key(provider['api_key_env'])}",
        "User-Agent": "HermesRadar/2.0",
    })
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return True, r.read()
    except urllib.error.HTTPError as e:
        code = e.code
        # 401/403/429 = 认证/限流 → 记失败; 4xx 其他 = 参数问题 → 不算 provider 故障
        if code in (401, 403, 429):
            return False, None
        return True, e.read()  # 透传 4xx 错误体
    except Exception:
        return False, None


def provider_available(p, now):
    st = provider_state.get(p["name"], {})
    cooldown = st.get("cooldown_until", 0)
    return now > cooldown


def route_and_call(messages, model, stream, temperature, max_tokens):
    """按策略尝试所有 provider, 返回第一个成功的响应"""
    providers = load_providers()
    now = time.time()
    last_err = None
    for p in providers:
        if not p.get("enabled", True):
            continue
        if not provider_available(p, now):
            continue
        # 构造转发 payload (model 用 provider 主模型)
        payload = {
            "model": p.get("primary_model", model),
            "messages": messages,
            "stream": stream,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        ok, body = _call(p, payload)
        if ok and body:
            _mark_success(p["name"])
            return 200, body, p["name"]
        elif not ok:
            _mark_fail(p["name"], f"限流/认证失败")
            last_err = f"{p['name']}: 不可用"
            continue
        else:
            return 400, body, p["name"]  # 参数类错误, 直接返回
    return 503, json.dumps({"error": {"message": f"所有 provider 不可用: {last_err}"}}).encode(), None


def _mark_success(name):
    provider_state[name] = {"healthy": True, "fail_count": 0, "cooldown_until": 0, "last_ping": time.time()}


def _mark_fail(name, reason):
    st = provider_state.get(name, {})
    st["fail_count"] = st.get("fail_count", 0) + 1
    st["cooldown_until"] = time.time() + min(300, 30 * st["fail_count"])  # 30s 起步, 封顶 5min
    st["healthy"] = False
    st["last_ping"] = time.time()
    st["last_reason"] = reason
    provider_state[name] = st


@app.route("/v1/models", methods=["GET"])
def models():
    out = []
    for p in load_providers():
        if p.get("enabled", True):
            out.append({"id": p.get("primary_model", "auto"), "object": "model", "owned_by": p["name"]})
    return jsonify({"object": "list", "data": out})


@app.route("/v1/chat/completions", methods=["POST"])
def chat():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": {"message": "invalid JSON"}}), 400
    messages = data.get("messages", [])
    model = data.get("model", "auto")
    stream = bool(data.get("stream", False))
    temperature = data.get("temperature", 0.7)
    max_tokens = data.get("max_tokens", 2048)

    code, body, used = route_and_call(messages, model, stream, temperature, max_tokens)
    if code != 200:
        return Response(body, status=code, mimetype="application/json")
    # 非流式: 直接返回 JSON; 流式: 透传 SSE
    if stream:
        return Response(body, mimetype="text/event-stream")
    return Response(body, mimetype="application/json")


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"ok": True, "providers": {
        name: {"healthy": st.get("healthy", False), "cooldown": round(max(0, st.get("cooldown_until", 0) - time.time())),
               "fail_count": st.get("fail_count", 0)}
        for name, st in provider_state.items()
    }})


def ping_all():
    """后台探测所有 provider (轻量 chat)"""
    providers = load_providers()
    for p in providers:
        if not p.get("enabled", True):
            continue
        payload = {"model": p.get("primary_model", "x"), "messages": [{"role": "user", "content": "ping"}],
                   "max_tokens": 1}
        ok, _ = _call(p, payload, timeout=12)
        if ok:
            _mark_success(p["name"])
        else:
            _mark_fail(p["name"], "ping 失败")
    print(f"[gateway] ping 完成: {[(p['name'], provider_state.get(p['name'],{}).get('healthy')) for p in providers]}", flush=True)


def _start_pinger():
    def loop():
        while True:
            try:
                ping_all()
            except Exception as e:
                print(f"[gateway] ping 异常: {e}", flush=True)
            time.sleep(180)  # 3 分钟一轮
    t = threading.Thread(target=loop, daemon=True)
    t.start()


if __name__ == "__main__":
    if "--ping" in sys.argv:
        ping_all()
        sys.exit(0)
    if "--test" in sys.argv:
        # 端到端测试
        body = json.dumps({"model": "auto", "messages": [{"role": "user", "content": "回复OK两个字"}], "max_tokens": 20}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=60)
            d = json.loads(r.read())
            print(f"✅ 网关测试通过: {d['choices'][0]['message']['content'][:50]}")
            sys.exit(0)
        except Exception as e:
            print(f"❌ 网关测试失败: {e}")
            sys.exit(1)
    _start_pinger()
    print(f"[gateway] 免费 LLM 聚合网关启动 @ 127.0.0.1:{PORT} (providers={len(load_providers())})", flush=True)
    app.run(host="127.0.0.1", port=PORT, threaded=True)
