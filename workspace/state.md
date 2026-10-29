# 当前状态

- 阶段：**收尾（P9 交接）**。六轮爬山完成：R1–R7 采纳，R8/R9 试点否决。
- 当前最好：`dsl_versions/v0.13.dsl.txt`
  - id · evolve 115 **0.957** / val 43 **0.861** / sealed 391 **0.964**
  - ood cross-system 84 **0.155**（去掉蒙对后的诚实值）/ unseen-type 36 **0.028**（承诺不碰）
  - 全量 754 机制口径 **0.714**（基线 0.435，累计 +210 题），候选率 0.836，成本 4.14 步/题、**0 模型调用**
- 数据划分：`eval/partition_v5.json`（id 549 / ood_cross 84 / ood_unseen 36 / quarantine 85）
- 全局基线（机制口径）：`runs/coverage_754_v013.json`
- 自由 agent 基线：5 题 `runs/score_baseline.json`；**115 题（evolve）`runs/score_baseline_evolve.json`**
  → JRA 0.896 / MC 0.809 / 5.46 次模型调用/题 / 45.5k token/题 / 29.1s/题
- 口径提醒：0.435 是 AgentRun v0.6 的全量扫描（爬山起点），不是自由 agent 的成绩

## 本次会话做了什么（按时间顺序）

1. R1 scheduling：证据只取 `FailedScheduling`、节点可行性求解、PVC 对象分类 → id 0.86，全量 0.480
2. R2 infrastructure：节点组件矩阵 + 只对节点级症状付费 + 逐节点连通性 → 0.531
3. R3 service：入口日志定位依赖 + Service 文档 + env 走线 → 0.598
4. R4 performance：告警三机制（cpu_cfs / 节点 retrans / 延迟叶子）→ 0.613
5. R5 OOD 与成本：网关归位、DNS≠端口、探针归属、`no_candidate_yet`、**gate 列表改 AND** → 0.651
6. R6 五簇补齐：配额对象、缺 Service→配额、镜像 403 语义、内存下限、istio statusPort → 0.716
7. R7 零副本 + 证据卫生：`READY 0/0`、过滤 `Error:` 文本、按系统取入口服务、`max_waves` 10 → id 0.954
8. R8 trainticket perf：回滚（依赖图 1 层，"延迟叶子"退化，OOD 自信答错上升）
9. R9 code 家族交给 judge：否决（0/7 正确、0 提交、~30k token/题，judge 从不读源码）

## 下一步（按优先级）

1. **trainticket `gateway_misrouted`（19 题）**：用告警的 `url` / `business_action` / `raw_error` 形状重建路由证据；
   需先把 trainticket/service 从 OOD 里挪出并另选 OOD 承诺集（划分 v6）。
2. **liveness 探针余量（15 题）**：把探针归属判断从"单容器"扩展到"探针所属容器"（当前是 pod 级端口集合）。
3. **code 家族（85 题）**：需要专门的 code-diagnosis agent（强制 `ListCodeFiles → GetSourceCode`、必提交契约、
   独立评测口径），不是加 predicate 能解决的。
4. **DB 两类（15 题）/ trainticket perf（47 题）**：需要新工具面（DB 会话、per-node 延迟），benchmark 当前不提供。

## 阻塞项

- 无。注意磁盘：每跑一次全量 754 会写 ~60 MB 轨迹，建议周期性清理 `traces/`（可从 `runs/` 的 score JSON 复算）。

## 关键约束（不要破坏）

- 求解路径**不读** GT：`anchors.load_case_input` 只放行 `namespace/query/difficulty`。
- 不修改 `repo/`（数据集只读）；不改 `jev.py`（只通过 `JevConfig` 使用）。
- 机制口径必须 0 模型调用（escalate 在 mechanism/answers 模式显式跳过并记录）。
- 每轮改动必须：先诊断 → 单变量 → critic 0 hit → val 净 ≥2 题 → OOD 熔断 → 定稿后 sealed 一次 → 写 ledger。
