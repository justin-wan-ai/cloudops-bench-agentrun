# P3 轨迹采集

## 1. 采样

| 维度 | 本次 |
|---|---|
| 数量 | 5 case（baseline 轨迹 5 条 + agentrun 轨迹 3 口径 × 5 条） |
| 分层 | seed=42 随机抽 5 个**不同 category**（runtime ×2, codedefect, admission, scheduling；trainticket 1 / boutique 4） |
| 成功定义 | Rank-1 与 gold 全等；过程可解释（trace 里有 tool/observation/决策） |
| 失败样本 | 保留（本次 baseline 5/5 正确，失败样本来自过程分：2/5 ECR=0） |

## 2. Trace schema（两侧统一）

两侧 trace 都是官方 agent trace 格式，故官方评测器可直接打分：

```jsonc
{
  "case_id": "boutique/runtime/32",
  "final_answer": "{\"key_evidence_summary\":...,\"top_3_predictions\":[...]}",
  "steps": [                       // ← 官方评测器读这个数组
    {"step_id": 0, "action_type": "tool", "action_name": "DescribeResource",
     "action_input": {...}, "observation": "Name: checkoutservice-... 3.4 KB", "error": null}
  ],
  "agentrun": {                    // ← 只在 agentrun 侧存在：决策面与成本
    "dsl_version": "0.6",
    "sop": {"sop": "boutique-runtime-rca", "owner": "sre-platform", "approved_by": ["oncall-primary","sre-lead"],
            "reviewed_at": "2026-10-23", "change_ticket": "OPS-1234"},   // ← 结论可追溯到签核过的 SOP
    "params": {...}, "nodes_executed": [...], "census": [...], "plan": [...],
    "state_wiring": {...}, "bindings": {"route": {...}, "pick": {...}},
    "family": "runtime", "route_probabilities": {...},
    "picked": {"root_cause": "readiness_probe_incorrect_protocol", "object": "app/checkoutservice", "rule": "probe_config"},
    "scope_system": "boutique",          // 系统专属词表类规则的作用域
    "verify_verdict": "verified", "escalated": false,
    "candidates": [...], "jev": [...], "nodes": [...],
    "tool_calls": 3, "model_calls": 0, "tokens_in": 0, "tokens_out": 0, "wall_s": 0.04
  }
}
```

## 3. 五次轨迹分析（P3.4 的五个问题）

对 baseline 轨迹逐条标注「探索 / 生产 / 判断 / 校验 / 报告」：

| case | 探索 | 生产 | 语义判断 | 校验 | 报告 |
|---|---|---|---|---|---|
| trainticket/runtime/61 | `GetResources(pods,-o wide)` | `GetErrorLogs` / `GetAppYAML` / `GetResources(services,name=tsdb-mysql-leader)` | 「哪条证据说明端口错」 | services 端口互证 | Submit |
| boutique/codedefect/60 | `GetAlerts`→`GetErrorLogs` | `ListCodeFiles` / `GetSourceCode` | 「哪段代码解释写放大」 | 读源码确认 30000 行 CSV | Submit |
| boutique/admission/32 | `GetResources(pods/deployments,-o wide)` | `DescribeResource(deployment/replicaset)` | 「为什么没起来」 | events 里的 quota 文本 | Submit |
| boutique/scheduling/109 | `GetResources(pods,-o wide)` | `DescribeResource(pod)` | 「selector 还是 affinity」 | `GetClusterConfiguration` 交叉 | Submit |
| boutique/runtime/32 | — | `DescribeResource(pod)` | 「readiness 用 http 而端口是 grpc」 | 无需（证据自洽） | Submit |

统计：**探索类调用占 17 次调用中的 5 次（29%）**，且每个 case 都重新发现「同 namespace 有哪些 pod/deployment」——这正是可沉淀部分。

## 4. 边际证据贡献（`agentrun/diag.py`）

对每次调用，用官方 milestone 匹配器跑前缀，统计它新增了几个 milestone 组：

| case | baseline | agentrun v0.6 机制侧 |
|---|---|---|
| trainticket/runtime/61 | 4 调用 / 4 有贡献（`GetResources:1 GetErrorLogs:1 GetAppYAML:1 GetResources:1`） | 4 / 3（`DescribeResource:0`） |
| boutique/codedefect/60 | 4 / 3（`GetErrorLogs:0`） | 4 / 3（`GetResources(pods):0`） |
| boutique/admission/32 | 4 / 2 | 3 / 2 |
| boutique/scheduling/109 | 3 / 3 | 4 / 3（`pvcs:0`） |
| boutique/runtime/32 | 2 / 2 | 3 / 3 |
| **合计** | 17 调用 / 14 贡献 / **0.824** | 18 / 14 / **0.778** |

结论：**两侧都赚到 14 个 milestone**；agentrun 多花的 1 次是「结构化探查的固定成本」（`DescribeResource` 用于区分 probe 类型 / `pvcs` 用于排除卷绑定），换来的是 0 token 与可回放。

## 5. 门禁

- [x] 每条轨迹可定位到具体 case 与 DSL 版本
- [x] 探索/生产/判断/校验/报告已标注
- [x] 探索占比已量化（29%）
- [x] 前 5 类失败模式已写入 `00_charter.md`
