# P2.5 数据划分策略（扩大数据集，754 case）

依据 `coding-agent-rsi-data-partitioning-guide.md` 选择策略。本文只回答三件事：
**我们属于哪个 RSI 场景、按什么粒度切、每一份数据允许用来做什么。**

## 1. 场景判定：B（代码级 Harness 自演化），带 A 的成分

我们迭代的客体是「SOP 的工作流 + 机械规则 + 控制流」：

| 证据 | 归属 |
|---|---|
| 改 `plans`/`gate`/`pre`/`post` 会改变工具调用序列与判定 | 场景 B（Harness/控制流） |
| 判定文字 `ask`、`intent`、`refute_if` 也是被迭代对象 | 场景 A（提示词反思）的成分 |
| 不训练模型权重 | 排除 C |

因此采用 **B 的「演化集 + 跨域 OOD 基准」**，并借用 A 的「极端倒挂 + 噪声门控」。

## 2. 泄漏单元：故障类型（topic），不是 case

一题的 `root_cause` 就是它的"主题"。54% 的题型在我们的 5 题种子集里没出现过，而同一题型的不同 case
共享同一套取证路径与判定文本——**按 case 随机切分会让规则记住个别 case 的字面量**。

但 SRE SOP 的改进天然是"每个题型一条路径"，若强制题型完全不重叠，就只能测迁移、测不出拟合。
所以采用**两层划分**：

| 层 | 规则 | 用来回答 |
|---|---|---|
| 层 1（题型内） | 同一题型内 case 分 `evolve / val / sealed` | 这条 SOP 在它负责的题型上是否真的变好 |
| 层 2（题型间） | 整题型划入 `ood_*` / `quarantine`，**永不参与调参** | 机制是否可迁移、是否会自信地答错 |

层 1 的前提是**禁止 case 级字面量**（见 §5 正则检查器），否则层 1 就退化成背题。

## 3. 本项目的划分结果（`eval/partition_v1.json`，seed=42）

| suit | 含义 | 题数 |
|---|---|---|
| `id` | 可参与爬山（题型内再分 evolve/val/sealed） | **514** |
| `ood_cross_system` | 跨系统 OOD：trainticket 的 `service` + `performance`（Java/Spring/Nacos，与本 SOP 的取证路径不同） | **84** |

> **划分 v2（R4 前）**：`pod_network_delay` / `node_network_delay` / `node_network_packet_loss` 从 OOD 移入 `id`——
> 移动发生在任何 performance 规则存在之前（可核查 git 历史），OOD 承诺集改为 `liveness_probe_*` + `code_wrong_return`。
| `ood_unseen_type` | 题型级 OOD：`liveness_probe_*`、`code_wrong_return`（v2 承诺不碰；三个网络类型在 R4 前移入 id） | **36** |
| `quarantine` | 已知做不了、暂不评分：`code_*`（除 file_writes）、`db_readonly`、`db_connection_exhaustion`、`deployment_zero_replicas` | **102** |

`id` 内部：`evolve=105 / val=39 / sealed=370`（题型内自适应：n≥5 → 3 演 1 验；n=4 → 2/1/1；n=3 → 1/1/1，**每种题型至少留 1 题 sealed**）。

每轮只激活一个 scope（= `fault_category`），把样量压进指南要求的 20–100：

| 轮次 | scope | evolve | val | sealed（同题型） | 现状（机制口径 cand/ok） |
|---|---|---|---|---|---|
| R1 | `scheduling` | **27** | 12 | 125 | cand 1.00 / ok 0.86 → 目标：把 pvc_*、cordon 拉齐 |
| R2 | `infrastructure` | 12 | 4 | 24 | cand 0.45 / ok **0.00** → 目标：节点组件取证 |
| R3 | `service` | 15 | 5 | 52 | cand **0.00** / ok 0.00 → 目标：新建 service 家族规则 |
| R4 | `performance` | 9 | 3 | 21 | cand 1.00 / ok 0.08 → 目标：饱和度 vs 代码缺陷区分 |

任务文件已生成：`eval/tasks_{evolve,val,sealed}_scheduling.json`、`eval/tasks_ood_{cross_system,unseen_type}.json`、`eval/tasks_quarantine.json`。

## 4. 全量现状基线（本次扫描，`runs/coverage_754.json`）

机制口径（0 模型调用）在 754 题上：**候选率 0.76、rank-1 正确率 0.435**。
按题型看三档，直接决定后续轮次顺序：

| 档 | 题型数 | 题数 | 例 |
|---|---|---|---|
| 已支持（cand≥0.9 且 ok≥0.8） | 20 | 320 | scheduling 家族、quota、镜像、mysql、readiness |
| 有候选但错（可修） | 28 | 318 | `pod_cpu_overload` ok 0.00、`kubelet_unavailable` 0.00、`missing_image_pull_secret` 0.39、`service_*` 无候选 |
| 无候选（缺能力） | 10 | 116 | `service_*` 4 类、`liveness_*`、`container_memory_limit_too_low` |

> 注意：该扫描读了 `process-label` 做分组统计（诊断用途），**sed 已冻结**；R1 的 sealed 集此后只看分数、不看逐题答案。

## 4.5 R1 结果（scheduling，v0.6 → v0.7）

| 集合 | n | v0.6 JRA | v0.7 JRA | 说明 |
|---|---|---|---|---|
| evolve | 27 | 0.667 | **0.963** | +8 题：cordon×3、taint×3、anti-affinity×1、pvc_storage_class×1 |
| val | 12 | 0.667 | **0.917** | +3 题（+25pp）；1 题=8.3pp，故这是真信号 |
| sealed | 125 | 0.968 | **0.992** | +3 题，CA 0.968→1.000 |
| ood_cross_system | 84 | 0.119 | 0.119 | 逐题不变（无回归） |
| ood_unseen_type | 54 | 0.093 | 0.093 | 逐题不变（无回归） |
| **全量 754** | 754 | 0.435 | **0.651**（R1–R5） | v0.7 +34、v0.8 +38、v0.9 +51、v0.10 +21/−10、v0.11 +32/−3；candidate 0.760→0.821（R5 起的『有候选即停』刻意压低候选率，是成本取舍） |

未修：`pvc_capacity_mismatch` vs `pvc_access_mode_mismatch`（工具面不可判，见 ledger R10）；
已知代价：scheduling 每题 +0.6~1.0 次工具调用（EE val 0.819→0.733）。

## 4.6 R2 结果（infrastructure，v0.7 → v0.8）

| 集合 | n | v0.7 JRA | v0.8 JRA | 说明 |
|---|---|---|---|---|
| evolve | 12 | 0.000 | **1.000** | 12/12：containerd/kubelet/kube-proxy/scheduler 全覆盖 |
| val | 4 | 0.000 | **0.750** | 3/4 |
| sealed | 24 | 0.083 | **0.917** | 2/24 → 22/24 |
| all-evolve（全题型） | 105 | 0.619 | **0.724** | +11 题，good→bad = **0** |
| all-val | 39 | 0.615 | 0.692 | +3 题 |
| ood_cross_system | 84 | 0.119 | 0.119 | 逐题不变 |
| ood_unseen_type | 54 | 0.111 | 0.111 | 逐题不变 |

副产品：census 加 `-o wide` 让 R1 的 MC 0.861→0.898、EOC 0.824→0.898（白捡的过程分）。
代价：infra 每题 +1.3 次调用；OOD 上 EE 0.513→0.475（多出的 `connectivity`/`cluster_config` 在 OOD 无收益）。

## 4.7 R3 结果（service，v0.8 → v0.9）

| 集合 | n | v0.8 JRA | v0.9 JRA | 说明 |
|---|---|---|---|---|
| evolve | 12 | 0.000 | **0.917** | selector/port/targetPort/protocol/env 五类机制全部落地（11/12） |
| val | 4 | 0.000 | **0.750** | 3/4 |
| sealed | 38 | 0.000 | **0.921** | 35/38 |
| all-evolve | 105 | 0.724 | **0.829** | +11，good→bad = **0** |
| all-val | 39 | 0.692 | **0.795** | +4，good→bad = **0** |
| ood_cross_system | 84 | 0.119 | 0.119 | 逐题不变 |
| ood_unseen_type | 54 | 0.111 | 0.111 | 逐题不变 |

代价：service 每题 7.67 次调用（EE 0.355，是全项目最低）。R3 过程中踩了 4 个坑（多文档 YAML、互斥 flag、
跨家族污染、症状正则漏匹配），全部记入 ledger R12–R15，并新增 `coverage --smoke` 探针自检门。

## 4.8 R4 结果（performance，v0.9 → v0.10）

| 集合 | n | v0.9 JRA | v0.10 JRA | 说明 |
|---|---|---|---|---|
| evolve | 10 | 0.000 | **0.900** | 9/10；cpu 节流、节点 delay/loss、pod 延迟叶子全部落地 |
| val | 4 | 0.000 | **0.500** | 2/4 |
| sealed | 15 | 0.000 | **0.800** | 12/15 |
| all-evolve | 112 | 0.829 | **0.857** | +3，无回退 |
| all-val | 42 | 0.795 | 0.786 | 划分 v2 换样本，口径不可直接比较（−0 题级回退） |
| ood_cross_system | 84 | 0.119 | 0.000 | 见下：OOD 门看的是 confident-wrong |
| ood_cross 自信答错率 | 84 | 0.85 | **0.42** | answered 81→35，误答减少 |
| ood_unseen_type | 36 | 0.111 | 0.111 | 逐题不变 |

未修：`performance/25`（节点延迟但快照无 Node 级告警）会退回 pod 级；OOD 上仍有 35 题自信答错（来自
service/infra 机制在 trainticket 上的不适配），是 R5 的输入。

## 4.9 R5 结果（OOD 治理 + 成本，v0.10 → v0.11）

| 集合 | n | v0.10 | v0.11 | 说明 |
|---|---|---|---|---|
| service evolve / sealed | 12 / 38 | 0.917 / 0.921 | 0.917 / 0.921 | 同分，**步骤 7.67→6.42 / 7.82→6.47** |
| infra evolve / sealed | 12 / 24 | 1.000 / 0.917 | 1.000 / **0.958** | `PodInitializing` 补进 `runtime_stall` |
| ood_cross_system JRA | 84 | 0.000 | **0.381** | 网关受害者改对（17 题） |
| ood_cross 自信答错 | 84 | 35 | **18** | 目标 ≤10，还差 8（见下） |
| ood_unseen_type | 36 | 0.111 | 0.028 | sidecar 收紧的代价（3 题），confident-wrong 持平 7 |
| all-evolve / all-val | 112 / 42 | 0.857 / 0.786 | 0.857 / 0.786 | EE 0.671→**0.716**，steps 4.54→4.24 |

R5 未达成项：OOD cross 仍有 18 题自信答错（trainticket service 的 `service_dns_resolution_failure` / `sidecar_port_conflict`
被我们的通用 service 规则误判）。R6 需要按 `scope` 收敛或为 trainticket 建专属词表。

## 4.10 R6 结果（五簇补齐，v0.11 → v0.12）

| 集合 | n | v0.11 | v0.12 |
|---|---|---|---|
| all-evolve | 112 | 0.857 | **0.955** |
| all-val | 42 | 0.786 | **0.857** |
| all-sealed | 378 | — | **0.963** |
| id 内（全量） | 532 | 0.86 | **0.953** |
| 全量 754 | 754 | 0.651 | **0.716**（+49 / −0） |
| ood_cross / ood_unseen | 84 / 36 | 0.381 / 0.028 | 0.381 / 0.028 |

修掉的簇：sidecar 端口冲突 18、service 配额 11、imagePullSecret 11、内存 limit 9、探针归属（部分）。
剩余错误已高度集中：trainticket perf 47、code 家族 85、quarantine（replicas/DB）32、liveness 15。

## 4.11 R7 / R8 / R9 结果

| 轮次 | 内容 | 结果 | 判定 |
|---|---|---|---|
| **R7**（v0.13） | 零副本 deployment、证据卫生（过滤 `Error:` 文本）、按系统取入口服务、`max_waves` 10 | id 内 **0.954**；all-evolve 0.957 / val 0.861 / sealed 0.964；全量 0.714 | **采纳** |
| R8（v0.14） | performance 词表推广到 trainticket | trainticket perf 3/4 错（依赖图只有 1 层，"延迟叶子"退化） | **回滚**（R24） |
| R9 | 用 judge 读代码处理 code 缺陷 | 7 题 0 对、0 提交、~30k token/题 | **不采纳**（R25） |

R7 的取舍：移除"工具报错文本当证据"后，OOD cross 从 0.381 掉到 0.155 —— 那 17 个 `gateway_misrouted` 本来就
是错误文本里的 `ts-route-*` 蒙对的。OOD 只做熔断，不允许拿它换分，所以接受这个下降。

## 5.5 划分 v5（当前权威，`eval/partition_v5.json`）

| suit | n | 说明 |
|---|---|---|
| `id` | 549 | evolve 115 / val 43 / sealed 391 |
| `ood_cross_system` | 84 | trainticket service + performance（R8 回滚后仍在此） |
| `ood_unseen_type` | 36 | liveness_pointer_* + code_wrong_return（承诺不碰） |
| `quarantine` | 85 | code_*（除 file_writes）、db_readonly、db_connection_exhaustion |

历次划分：v1（初版 5 题时代）→ v2（网络三型入 id）→ v3（零副本入 id）→ v4（trainticket perf 入 id，**已回滚**）→ **v5 = 当前**。

## 5. 四道门（缺一不可）

| 门 | 实现 | 判定 |
|---|---|---|
| **正则检查器**（禁止 case 级字面量） | `python3 -m workspace.agentrun.critic`：扫描 DSL/`rules.py`/`plans.py` 里的 pod 名、IP、时间戳、quota/secret 名、case id、`case_id ==` | 0 hit 才可提交；当前 0 hit |
| **噪声门控** | evolve 27 题 → 1 题 = 3.7pp；净提升 **≥2 题** 且 val 不退化才接受；同时看翻转（好→坏 vs 坏→好） | 单题提升不算收益 |
| **OOD 熔断** | 每个候选版本跑 `tasks_ood_cross_system` + `tasks_ood_unseen_type`；**允许** OOD 分数不涨，**不允许**"自信答错"上升（wrong-but-confident rate）或误升级率暴涨 | 违反即回滚 |
| **Sealed 终局** | `tasks_sealed_<scope>` 只在版本定稿后跑一次；不用于选择 | 发布验收 |

## 6. 一轮的标准动作

```bash
# 0) 现状（机制口径，0 token）
python3 -m workspace.agentrun.runner --dsl dsl_versions/v0.6.dsl.txt --jev-mode mechanism \
        --tasks-file eval/tasks_evolve_scheduling.json --save-root traces/r1_evolve
python3 -m workspace.agentrun.score  --root traces/r1_evolve --layout agentrun --out runs/r1_evolve.json

# 1) 改 SOP（DSL 的 plans/pre/post），critic 必须 0 hit
python3 -m workspace.agentrun.critic --dsl dsl_versions/v0.7.dsl.txt

# 2) val 判定（噪声门控：净 ≥2 题）
python3 -m workspace.agentrun.runner --dsl dsl_versions/v0.7.dsl.txt --jev-mode mechanism \
        --tasks-file eval/tasks_val_scheduling.json --save-root traces/r1_val

# 3) OOD 熔断 + 生产口径抽样（live，确认 jev 不抖）
python3 -m workspace.agentrun.runner --dsl dsl_versions/v0.7.dsl.txt --jev-mode live \
        --tasks-file eval/tasks_ood_cross_system.json --save-root traces/r1_ood

# 4) 定稿后 sealed 跑一次
python3 -m workspace.agentrun.runner --dsl dsl_versions/v0.7.dsl.txt --jev-mode mechanism \
        --tasks-file eval/tasks_sealed_scheduling.json --save-root traces/r1_sealed
```

## 7. 与旧 5 题集的关系

`eval/tasks_5.json` 的 5 题**全部落在 `evolve`**（它们是 v0.1–v0.6 的蒸馏来源，属于"已见"），
因此新的 `sealed` / `ood` 数字与之前报告的 CA 1.00 **不可直接比较**——之前的 1.00 是训练集分数。
这一条必须写进 README 与 ledger，避免把拟合当泛化。
