#!/usr/bin/env python3
"""llm_center_view.py — 模型中心页面 (FreeLLM Router M5)
[2026-08-07] 独立模块, app.py 只需 import + register_llm_center(app)
数据源: llm_rank.json (排行榜), llm_usage.json (用量), llm_providers.json (配置)
"""
import json, sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
SCRIPTS = Path("/opt/data/scripts")


def _fmt(v, n=2):
    try:
        return format(float(v), "." + str(n) + "f")
    except Exception:
        return "-"


def _load_json(name, default):
    try:
        return json.loads((SCRIPTS / name).read_text())
    except Exception:
        return default


def render_llm_center(HTML_HEAD, FOOTER, render_template_string):
    rank = _load_json("llm_rank.json", {"rank": [], "date": "-"})
    usage = _load_json("llm_usage.json", {"periods": {}})
    provs = _load_json("llm_providers.json", {"providers": []}).get("providers", [])

    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")

    rank_rows = ""
    for r in rank.get("rank", []):
        badge = "🟢" if r.get("status") == "ok" else ("🟡" if r.get("status") == "no_key" else "🔴")
        rank_rows += (
            "<tr><td>#" + str(r["rank"]) + "</td><td>" + badge + " " + r["provider"] + "</td>"
            + "<td><code>" + r["model"] + "</code></td>"
            + "<td class=num>" + _fmt(r["total"]) + "</td>"
            + "<td class=num>" + _fmt(r["quality"]) + "</td>"
            + "<td class=num>" + _fmt(r["speed"]) + "</td>"
            + "<td class=num>" + _fmt(r["stability"]) + "</td>"
            + "<td class=num>" + str(r.get("latency") or "-") + "</td></tr>"
        )

    usage_cards = ""
    for label in ["24h", "7d", "30d"]:
        p = usage.get("periods", {}).get(label, {}).get("total", {})
        usage_cards += (
            '<div class=u-card><div class=u-title>' + label + '</div>'
            + '<div class=u-num>' + str(p.get("calls", 0)) + '</div><div class=u-sub>调用次数</div>'
            + '<div class=u-tok>' + _fmt(p.get("in_tok", 0) / 1e6, 1) + 'M in · '
            + _fmt(p.get("out_tok", 0) / 1e6, 1) + 'M out</div>'
            + '<div class=u-sub>节省 <b style="color:#3fb950">$' + _fmt(p.get("saved_usd", 0)) + '</b> · 实付 $'
            + _fmt(p.get("est_cost", 0), 4) + '</div></div>'
        )

    prov_rows = ""
    for p in provs:
        if p.get("enabled"):
            st = "✅ 启用"
        elif "待" in p.get("note", "") or "待" in p.get("quota", ""):
            st = "🔌 待key"
        else:
            st = "⏸ 禁用"
        prov_rows += (
            "<tr><td>" + p["name"] + "</td><td><code>" + p.get("primary_model", "") + "</code></td>"
            + "<td>" + st + "</td><td style=font-size:11px;color:#8b949e>" + p.get("quota", "")[:60] + "</td></tr>"
        )

    body = (
        '<div class=block><h2>🏆 免费模型排行榜 '
        '<span style="font-size:12px;color:#8b949e">测评日 ' + str(rank.get("date", "-")) + '</span></h2>'
        '<p style="color:#8b949e;font-size:13px">综合分 = 质量×0.5 + 速度×0.3 + 稳定性×0.2 · 每日 04:30 自动测评 · '
        '<a href=/freeapi>📚 免费 API 大全</a></p>'
        '<table class=tb><thead><tr><th>名次</th><th>Provider</th><th>模型</th><th>综合</th><th>质量</th>'
        '<th>速度</th><th>稳定</th><th>延迟</th></tr></thead><tbody>'
        + (rank_rows or '<tr><td colspan=8 style="color:#8b949e">暂无测评数据</td></tr>')
        + '</tbody></table></div>'
        '<div class=block><h2>💰 用量与节省</h2><div class=u-grid>' + usage_cards + '</div></div>'
        '<div class=block><h2>⚙️ Provider 配置</h2>'
        '<table class=tb><thead><tr><th>Provider</th><th>模型</th><th>状态</th><th>额度</th></tr></thead>'
        '<tbody>' + (prov_rows or '<tr><td colspan=4 style="color:#8b949e">无配置</td></tr>') + '</tbody></table></div>'
        '<style>.u-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:12px}'
        '.u-card{background:#111827;border:1px solid #30363d;border-radius:10px;padding:16px;text-align:center}'
        '.u-title{color:#8b949e;font-size:13px;margin-bottom:6px}.u-num{font-size:28px;font-weight:700;color:#58a6ff}'
        '.u-sub{color:#8b949e;font-size:12px;margin-top:4px}.u-tok{color:#e6edf3;font-size:13px;margin-top:6px}'
        '.tb{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}'
        '.tb th{text-align:left;padding:8px;border-bottom:1px solid #30363d;color:#8b949e;font-weight:600}'
        '.tb td{padding:8px;border-bottom:1px solid #21262d}.num{text-align:right;font-family:monospace}'
        '.block{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:20px;margin-bottom:16px}'
        '.block h2{margin:0 0 8px;font-size:17px}</style>'
    )
    return render_template_string(HTML_HEAD + body + FOOTER, now=now)


def register_llm_center(app):
    @app.route("/llm-center")
    def llm_center():
        from flask import render_template_string
        # 延迟获取 app 的 HTML_HEAD/FOOTER (app.py 内定义)
        return render_llm_center(app.llm_html_head, app.llm_footer, render_template_string)
