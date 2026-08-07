# FreeLLM Router — 免费大模型智能调度中心

应对 LLM API 涨价潮：**免费额度管理 + 质量测评闭环 + 任务分级路由**。

在多家 LLM 厂商免费额度（opencode zen / 商汤 / 智谱 / 魔搭 / 硅基流动 / OpenRouter 免费层等）之间自动发现、测评、择优、路由，把付费调用降到最低，同时按任务类型保证质量与速度。

```
freeapi 采集(每日) ──→ llm_benchmark 测评 ──→ llm_quality.db
                                                  ↓
                      llm_rank.json 排行榜 ←─────┘
                              ↓
                      llm_router 推荐链 ──→ llm_chain.json
                              ↓
              time_model_switch 动态 fallback 链 (Hermes 接入)
```

## 核心模块

| 模块 | 文件 | 作用 |
|------|------|------|
| 聚合网关 | `free_llm_gateway.py` | OpenAI 兼容网关 (127.0.0.1:8099)，配置顺序路由 + 健康探测 + 失败冷却 failover |
| 质量测评器 | `llm_benchmark.py` | 5 任务测试集（摘要/逻辑推理/JSON 结构化/代码/延迟），**纯规则评分零 LLM 成本** |
| 智能路由 | `llm_router.py` | 任务分级 × 测评评分 × 健康状态 → 动态推荐链 |
| 自动入库 | `llm_auto_join.py` | 检测新配置的 API key → 自动测评 → 综合分 ≥ 0.85 自动启用 |
| 用量统计 | `llm_usage.py` | 从调用记录聚合 token 用量 / 成本 / 节省金额 |
| 模型中心页 | `examples/llm_center_view.py` | DataHub 页面：排行榜 + 用量 + provider 状态 |

## 评分模型

```
综合分 = 质量 × 0.5 + 速度 × 0.3 + 稳定性 × 0.2
```

- **质量**：4 个任务的平均得分（摘要实体保留 / 逻辑答案 / JSON 可解析 / 代码可运行）
- **速度**：最小请求延迟分档（<2s=1.0, 2-5s=0.7, 5-10s=0.4, >10s=0.2）
- **稳定性**：任务成功率

## 任务分级路由

| 级别 | 场景 | 策略 | 付费兜底 |
|------|------|------|---------|
| T0 | 交易决策 | 质量优先 | ✅ 允许 |
| T1 | 主对话 | 综合分优先 | 最后 |
| T2 | 定时任务 | 稳定性优先 | ❌ 禁止 |
| T3 | 批量/总结 | 速度优先 | ❌ 禁止 |

## 快速开始

```bash
# 1. 配置 provider（环境变量放 key，绝不入库）
cp llm_providers.example.json llm_providers.json
# .env: export ZEN_API_KEY_1=xxx  SENSENOVA_API_KEY=xxx  ...

# 2. 启动网关
python3 free_llm_gateway.py
curl http://127.0.0.1:8099/healthz   # 健康检查

# 3. 测评（每日自动跑, 也可手动）
python3 llm_benchmark.py              # 全量测评 → llm_quality.db + llm_rank.json
python3 llm_benchmark.py --provider X # 单测某个

# 4. 生成推荐链
python3 llm_router.py --show

# 5. 用量统计
python3 llm_usage.py
```

## Cron 建议

```cron
30 4 * * *  python3 llm_benchmark.py    # 每日测评（避开主对话高峰）
0  5 * * *  python3 llm_usage.py        # 用量统计
10 5 * * *  python3 llm_auto_join.py    # 新 key 自动接入
```

## 接入 Hermes / 其他客户端

- `examples/time_model_switch.py` 演示如何读取 `llm_chain.json` 动态生成 fallback 链（无推荐链时回退硬编码，安全兜底）
- `examples/llm_center_view.py` 演示模型中心页渲染（排行榜 + 用量卡片 + provider 状态）

## 安全设计

- **API key 一律从环境变量读取**，代码仓库零密钥
- 测评仅消耗免费额度（每 provider 每天 ~6 次调用）
- 自动入库有门槛（综合分 ≥ 0.85）且仅对非聚合站生效
- 网关失败冷却（30s 指数增长至 5min），4xx 参数错误直接透传不误判

## License

MIT
