# P7 实验台账（06_ledger）

口径：n=5，官方 `evaluation.py`；**机制侧**=无模型调用，**生产**=真实 jev，**含答案**=答案库回放。
n=5 时 1 题 = 20pp，故按「净翻转 + 机制可解释 + 非退化」判定，不追 +3pp。

| # | 版本 | 类型 | 改动（单变量） | 机制 CA/MC/EE/Steps | 生产 CA/MC | 判定 | 原因 |
|---|---|---|---|---|---|---|---|
| 0 | — | 基线 | 原生 ReAct（deepseek-flash, max_iter=20） | 1.00 / 0.793 / 0.900 / 3.4 | — | 基线 | 17 次模型调用，134.8k in-token，58.1s |
| 1 | v0.1 | 机制 | 首个完整 DSL：census(alerts+pods+deployments) → route → 全量 family 计划 → 规则 → pick/verify | 1.00 / 0.737 / 0.513 / 5.8 | — | 不采纳 | 每个 case 有 3 次零贡献调用（`GetAlerts`×4、`deployments`×3、盲跟一步） |
| 2 | v0.2a | 机制 | census 改为 pods+alerts；`deployments` 移入 admission 计划；plan 步骤加 `when` 闸门；一波一探查 | 1.00 / **0.820** / **0.833** / 3.6 | — | 采纳 | MC +0.083、EE +0.32、零翻转；开销全部来自免费探查 |
| 3 | v0.2b | 机制 | `GetAlerts` 仅在 pod 全绿时拉取（其余 case 已有 pod 级故障） | 1.00 / 0.820 / 0.833 / 3.6 | — | 采纳 | 去掉 4 次零贡献调用，CA 不变 |
| 4 | v0.2c | 机制 | scheduling 的 `GetClusterConfiguration` 由「弱候选才跑」改为「必跑」 | 1.00 / 0.820 / 0.833 / 3.6 | — | 采纳 | Pending 是容量/约束问题，cluster 视图属可采纳证据（scheduling MC 0.67→1.00） |
| 5 | v0.2 | 生产 | jev(verify) 改为三分类；支配候选不做语义闸门；同一证据签名不重复判定 | — | 1.00 / 0.753 / steps 3.4, 3 model calls | 部分采纳 | 成本从 65 次模型调用降到 3；但 `min_waves=2` 早停损失过程分（MC 0.820→0.753） |
| 6 | **v0.3** | 生产 | `min_waves` 2 → 5：探查免费，结论才要钱；跑到计划穷尽 | 1.00 / 0.820 / 0.833 / 3.6 | **1.00 / 0.820**, 3 model calls, 884 token, 9.7s | 采纳（已被 v0.4 取代） | 生产口径 = 机制口径的过程分，模型调用只花在「一处歧义」 |
| 7 | v0.4 | 机制重构 | 把计划表 / 路由顺序 / gate 从 `plans.py` 搬进 DSL | 1.00 / 0.820 / 0.833 / 3.6 | 1.00 / 0.820 | 采纳（已被 v0.5 取代） | 策略可见，但 jev 内部逻辑仍在 Python |
| 8 | v0.5 | 语法重构 | 每个判定节点改成五段式 `ask+space / input / pre / post / out`；`$node.field` 显式数据流；`state:` 显式接线；新增 `dsl_check` | 1.00 / 0.820 / 0.833 / 3.6（0 翻转） | 1.00 / 0.820, **7** calls, 1593 token | 采纳（已被 v0.6 取代） | `pre`/`post` 就是原先 `jevnodes.py` 里的 if 链，逐行搬进 DSL（该文件已删除）；删节点/删计划/写错 guard 全部加载期报错 |
| 9 | v0.6 | 可审性重构 | DSL 自身作为 SOP：front-matter（`scope/owner/approved_by/reviewed_at/change_ticket/boundaries`）、每个 decide 的 `intent`/`refute_if`、按值班顺序重排文件、复合 gate 拆成原子 gate+OR 列表、`dsl_check --review` lint | 1.00 / 0.820 / 0.833 / 3.6（0 翻转） | 1.00 / 0.820, 7 calls, 1593 token | 采纳（已被 v0.7 取代） | 语义零变化（5/5 决策与步数一致）；此前「人读的东西」散在 Python/口头，现在缺签核或缺 `intent` 直接加载失败 |
| **10** | v0.7 (R1) | 机制 | scheduling 判定改为「scheduler 文本 + 节点清单 + PVC/PV/SC 对象」：①只在 `FailedScheduling` 消息里取证据 ②节点可行性求解（selector/affinity → taint → cordon → capacity）③PVC 分类走 pvc/pv/sc 表 ④修 `pvcs` 别名（此前静默报错，证据全盲） | evolve 27: JRA **0.667→0.963**（+8）<br>val 12: **0.667→0.917**（+3）<br>sealed 125: **0.968→0.992**（+3） | 同左（机制口径，0 model call） | 采纳 | val +25pp、sealed +3 题、两套 OOD 逐题不变；**全量 754 机制口径 correct 0.435→0.480（+34 题，good→bad = 0）**；代价：scheduling 每题 +0.4~0.8 次调用（EE val 0.819→0.715），换 PVC/节点证据完整性 |

## 参数/策略消融（P5 门禁，均在 v0.4 上）

| 变体 | CA | MC | EE | Steps | 工具调用 | 判定 |
|---|---|---|---|---|---|---|
| v0.6 | 1.000 | 0.820 | 0.833 | 3.6 | 18 | — |
| `alerts` gate→always（`ablation/v06_alerts_always`） | 1.000 | 0.820 | **0.680** | 4.4 | 22 | gate 是策略、有代价 ✅ |
| `min_waves` 5→2（`ablation/v06_min_waves2`，live） | 1.000 | 0.753 | 0.817 | 3.4 | 17 | 早停损失过程分 ✅（复现 v0.2 数字） |
| `max_waves=1` | 0.600 | 0.490 | 0.833 | 2.2 | 11 | 行为/分数都变 → 参数真的被使用 ✅ |
| 删 `derive_candidates`（DSL） | 0.000 | 0.870 | 0.780 | 4.4 | 22 | 节点存在性决定执行 ✅（MC 反高：无效调用变多） |
| `alerts` gate `healthy_system`→`always` | 1.000 | 0.820 | **0.680** | 4.4 | 22 | gate 是策略、有代价 ✅（MC 不变，EE 掉 0.153） |
| `min_waves` 5→2（= v0.2 生产行为） | 1.000 | 0.753 | 0.817 | 3.4 | 17 | 早停损失过程分 ✅（复现了 v0.2 的 0.753/0.4） |
| `cands_cap=1` | 1.000 | 0.820 | 0.833 | 3.6 | 18 | 本样本不敏感 → **死变量候选**，不当有效改动 |
| 删 `route` 节点 / 删 `approved_by` / 删 `pick.intent` | — | — | — | — | — | 均**加载期 `DslError`**（`unknown node reference` / `missing SOP front-matter` / `decide.intent is required`） ✅ |
| `post` 死分支 / 重复 guard | — | — | — | — | — | `dsl_check --review` 分别报 `FAIL` / `WARN`（退出码非 0） ✅ |

## 负结果登记（防止后人重试）

| # | 尝试 | 结果 | 结论 |
|---|---|---|---|
| R1 | 用 `jev(verify)` 的 `noul` 绝对概率做闸门（`conf_min=0.55`） | 5/5 假设被判 ~0.07–0.15，全部误升级；judge 8 步/例，5 case 150k token | **绝对概率不可用**：改三分类 + 「是否压过其他两类」的尺度无关判定；`insufficient` 不触发升级 |
| R2 | 对**支配候选**加语义闸门（单候选也问 verify） | 与参考手册 −71pp 一致：成本 +1 次调用/波，收益 0 | 支配即答案（模式 B）；闸门只用于多候选/冲突 |
| R3 | 早停（`min_waves=2`，验证通过即停） | 生产口径 MC 0.820→0.753、ECR 0.6→0.4 | 快照探查免费、结论昂贵 → 不应为省探查早停 |
| R4 | 用 fault_category（目录名）当路由特征 | — | 未采用：那是标注泄漏；baseline 侧确实拿到了它（同为输入面），本项目在 agentrun 侧**主动不使用**，作为对比的保守偏差记录在此 |
| R5 | 把策略表留在 `plans.py`，DSL 只声明节点名 | 删 `jev: route` 会静默退化成「永远用 runtime 计划」，DSL 读不出「弃权取谁」「runtime 探几步」 | **策略必须在 DSL**：v0.4 搬走表，v0.5 把 jev 的 if 链也搬走 |
| R6 | 用 `noul` 概率做闸门（v0.3 前） | 5/5 假设被判 ~0.1 → 全量误升级 | 见 R1；v0.4 起改为三分类 + `require: beats_alternatives` |
| **11** | v0.8 (R2) | 机制 | infrastructure 判定改为「节点组件矩阵」：①`CheckNodeServiceStatus` 逐组件（kubelet/containerd/kube-proxy/scheduler），按 `Status: ●/○` 判活 ②只对**节点级症状**付费（`node_scoped_symptom`）③scheduler 静默（Pending 无事件）→ 查 master ④`kubernetes.io/unreachable` taint 视为节点不可达而非容忍度问题 ⑤`CheckServiceConnectivity` 的逐节点结果 → kube-proxy ⑥census 加 `-o wide`（换 M1/M2 过程分） | infra evolve 12: JRA **0.000→1.000**<br>infra val 4: **0.000→0.750**<br>infra sealed 24: **0.083→0.917**<br>all-evolve 105: 0.619→**0.724** | 同左（0 model call） | 采纳 | 全量 evolve 105 题 bad→good=11、good→bad=**0**；**全量 754 correct 0.480→0.531（+38 题，good→bad=0）**；OOD 两套 JRA 逐题不变；R1 scheduling MC 0.861→0.898、EOC 0.824→0.898（wide census 白捡）；代价：infra 每题 +1.3 次调用、OOD EE 0.513→0.475 |
| **12** | v0.9 (R3) | 机制 | service 家族从「无候选」到规则化：①入口服务错误日志按 pattern 频次→ClusterIP/DNS/noun 定位目标 ②`GetAppYAML` 的 **Service 文档**判 selector/port/targetPort/protocol ③**Deployment 文档**判 `*_ADDR` 与真实 Service:端口 ④新增 `pods_labels` 探针（selector 需要 pod labels）⑤修症状正则（"Availability Disruption" 未命中） | service evolve 12: JRA **0.000→0.917**<br>service val 4: **0.000→0.750**<br>service sealed 38: **0.000→0.921**<br>all-evolve 105: 0.724→**0.829** | 同左（0 model call） | | 采纳 | 105 题 bad→good=11、good→bad=**0**；all-val 0.692→**0.795**（0 回归）；OOD 两套逐题不变；代价：service 每题 7.7 次调用（EE 0.355） |
| **13** | v0.10 (R4) | 机制 + 划分 v2 | ①alerts 分三个物理机制：`RESOURCE_SATURATION[cpu_cfs]`→pod_cpu_overload；Node 级 `TCP Retransmission` 计数（1 个节点=delay、≥2 个节点=packet_loss，取 retrans 最高者）；无节点信号→用 `GetServiceDependencies` 求「延迟叶子」→pod_network_delay ②分区 v2：三个网络类型从 OOD 移入 id（此前无任何规则触碰），OOD 承诺集改为 liveness_* + code_wrong_return ③`scope.system` 首次真正生效（告警词表类规则只在该系统生效） | perf evolve 10: JRA **0.000→0.900**<br>val 4: **0.500**<br>sealed 15: **0.800**<br>all-evolve 112: 0.829→**0.857** | 同左（0 model call） | | 采纳 | evolve 0→0.900（9/10）；sealed 12/15；**OOD 自信答错率 0.85→0.42**；代价：perf 每题 3.0 次调用（EE 0.633） |
| **14** | v0.11 (R5) | 机制 + 语义修正 | ①`gateway` 规则的受害者改为**网关服务自身**（此前取"看起来最慢的服务"）②db 规则在日志出现 `UnknownHost/no such host` 时不再成立（DNS 不是端口）③探针 port 比较只在**单容器** pod 上做（sidecar 有自己的探针）④删掉 `*_probe_incorrect_timing` 猜测（无启动基线）⑤service 计划加 `no_candidate_yet` 闸门：有候选就不再取证 ⑥**修正 gate 列表语义：AND（原本实现成 OR）** ⑦`runtime_stall` 补 `PodInitializing` | service evolve 0.917（steps 7.67→**6.42**）；infra sealed 0.917→**0.958**；**ood_cross JRA 0.000→0.381**；all-evolve EE 0.671→**0.716** | 同左（0 model call） | | 采纳 | OOD cross 自信答错 35→18、答对 0→17；成本全线下降；ood_unseen 少 3 题（见 R19） |
| **15** | v0.12 (R6) | 机制（五簇补齐） | ①ResourceQuota 对象本身点名被耗尽的资源（`services: 11/11`、`requests.storage: 9Gi/10Gi`）②"workload 存在但 Service 对象缺失" → 服务配额，而非 env 写错 ③镜像拉取：只有 `failed to authorize/anonymous token` 才是缺 secret，裸 `403` 是 tag 不存在 ④内存 limit ≤32Mi 是物理下限 → `container_memory_limit_too_low` ⑤istio `statusPort` 注解与 containerPort 撞车 → `service_sidecar_port_conflict` ⑥探针端口改为与**该 pod 全部容器端口**比对（侧车端口不再误判） | evolve 112: 0.857→**0.955**<br>val 42: 0.833→**0.857**<br>sealed 378: **0.963**<br>全量 754: 0.651→**0.716** | 同左（0 model call） | | 采纳 | 全量 bad→good **49**、good→bad **0**；id 内 **0.953**；成本 4.41 次/题 |
| **16** | **v0.13 (R7)** | 机制 + 划分 v3 | ①工具报错文本（`Error: ... Allowed: [...]`）不再当证据——它曾让 17 个零副本 case 命中 gateway 规则（allowed 列表里有 `ts-route-*`）②`READY 0/0` 的 deployment → `deployment_zero_replicas` ③service 计划在"仍无候选"时补取 deployments ④入口服务按系统取（boutique=frontend / trainticket=ts-ui-dashboard）⑤`max_waves` 8→10（服务计划已 10 步，之前尾部两步从未执行） | evolve 115: **0.957**<br>val 43: **0.861**<br>sealed 391: **0.964**<br>id 内 **0.954**<br>全量 754: 0.716→**0.714** | 同左（0 model call） | **★采纳（当前最好）** | 零副本 17 题全对；但移除"错误文本当证据"后 **OOD cross 0.381→0.155**（那 17 个 gateway 是**蒙对**的），这是主动选择的诚实性代价（R23） |
| 17 | v0.14 (R8) | 机制 | 让 performance 规则对 trainticket 生效（其告警词表相同：`LATENCY_DEGRADATION`/`RESOURCE_SATURATION`/`NETWORK_QUALITY_DEGRADED`），并将 trainticket/perf 移入 id（划分 v4） | trainticket perf evolve **0.692**（boutique 部分不变） | — | **回滚** | trainticket 的服务依赖图只有 1 层（`ts-ui-dashboard → ts-gateway-service`），延迟告警是全系统性的 → "延迟叶子"退化成"最大延迟"，3/4 被测 case 答成 `ts-preserve-service`（调用方）而非真正注入的服务。OOD 上自信答错从 0 变 4 → 违反熔断门 |
| 18 | v0.14 (R9) | 能力试点 | 新增 `symptom_only` escalate 谓词：应用级异常 + 无确定性解释 → 交给 judge 读代码 | pilot（7 个 code 类型各 1 题）**0/7** | judge 8 步 / ~30k token / 题，**全部未提交答案** | **不采纳** | judge 从不调用 `GetSourceCode`（只在 logs/yaml/alerts 里打转），预算耗尽直接放弃 → 代码路径需要**代码优先的专用 agent**（强制读源码 + 必须提交契约），不是加一个 predicate 能解决的 |
| R25 | 用现有 judge 处理 code 缺陷 | 7 题 0 对、0 提交、~30k token/题；judge 不读源码 | 需要专门的 code-diagnosis agent（强制 `ListCodeFiles→GetSourceCode`、必须提交、预算内多次比对），并单独定评测口径 |
| R24 | 把 performance 词表推广到 trainticket | 依赖图 1 层 + 全系统延迟告警 → "叶子"启发式失效，OOD 自信答错 0→4 | 词表相同 ≠ 机制可用；跨系统复用前必须先确认**证据结构**（图深度/告警粒度）也相同 |
| R23 | 让工具报错文本进入证据 | `Error: ... Allowed: ['ts-route-plan-service',...]` 里的 "route" 命中了 gateway 规则 → 17 个零副本 case 被判成网关路由错（其中 trainticket 的 17 个 service case 反而"蒙对"） | 证据收集一律过滤 `Error:` 前缀；OOD 分数因此下降（0.381→0.155），但那是把蒙对改回诚实 |
| R21 | 把裸 `403 Forbidden` 当作"缺 imagePullSecret" | docker.io 镜像 tag 不存在时也返回 403 → 2 个 `incorrect_image_reference` 被判成缺 secret（全量 good→bad） | 只有 `failed to authorize` / `anonymous token` / `unauthorized` 才算凭证问题 |
| R22 | 用"env 指向不存在的 Service"直接判 env 写错 | 配额耗尽时 Service 根本没被创建，host 是**合法服务名但无对象** → 应为配额问题；同时 service 家族在缺 Service 时会误判 | 区分"名字拼错（无 pod）"与"对象没建成（有 pod 无 Service）"；后者交配额规则，并用 `service_missing_for_pod` 闸门按需取 resourcequota |
| R20 | `gateway` 规则用 `_app()` 当受害者 | 取到"告警/异常 pod 对应的服务"而不是网关本身 → trainticket 17 题类型对、对象错 | 受害者必须来自证据实体（Service 表里名字含 gateway/ingress 的那个） |
| R19 | 探针 port 比较对多容器 pod 也做 | trainticket 侧车（:15020）与业务容器（:1568x）被误判成 `readiness_probe_incorrect_port`（5 题自信答错） | 只在单容器 pod 上比较；代价：OOD liveness 集少 3 题"蒙对"，两套 OOD 的 confident-wrong 总数不变 |
| R18 | gate 列表按 OR 实现（DSL 注释却写成"多 gate = OR"） | `[service_target_found, no_candidate_yet]` 永远为真 → 拿到候选后仍继续取证（service 每题多 1.2 次调用）；同时掩盖了 `runtime_stall` 漏 `PodInitializing` 的 bug（infra evolve 1.000→0.750） | 列表语义改为 **AND**（符合"闸门"直觉）；真正的 OR 用两个显式复合 gate `probe_or_restart_or_weak` / `restart_or_weak_strong` 表达 |
| R16 | 让 4 类 performance 规则在 **trainticket** 上照常生效 | 告警/入口服务词表是 boutique 形状 → trainticket 47 题 performance 全部"自信答错"（OOD cross JRA 0.119→0.036，confident-wrong 0.85） | 用 DSL `scope.system` 约束**系统专属词表**类规则；代价是 OOD 上 JRA 从"偶尔蒙对"变 0，但 confident-wrong 0.85→0.42（这才是 OOD 门要的指标） |
| R17 | `pod_network_delay` vs `node_network_delay` 靠延迟幅度阈值区分 | 快照里 `-o wide` 的 NODE 列**全空**，无法做共节点判断；节点延迟若没有 Node 级告警（performance/25）就不可判 | 改为「节点级证据优先、无证据退回 pod 级」，并把不可判写进 SOP `boundaries`（当前 10 题里错 1 题） |
| R15 | 症状正则只写 `unavailab` | "Service Availability Disruption."（benchmark 主导措辞）不命中 → 这类 case 掉进 residual→infrastructure | 改为 `unavailab|unreachab|availab|disruption|...`（同一个坑在 R1 也踩过一次） |
| R14 | `yaml.safe_load` 解析 `GetAppYAML` | 该工具返回 **多文档**（Deployment+Service+ServiceAccount），`safe_load` 抛错 → 返回 `{}` → selector/env 规则**全部静默失效** | 改 `yaml.safe_load_all`；新增 `parse_app_docs / yaml_doc_of_kind` |
| R13 | 让 service 规则无条件下所有 `appyaml*` 观测生效 | startup/runtime 家族也会取 app_yaml，但没有 services/labels 证据 → selector 规则空匹配，**11 个 startup case 从对变错** | service 规则必须 gate 在「service 路由 + labels 已取」上（`service_route = services and labels`） |
| R12 | census 同时给 `output_wide` 和 `show_labels` | kubectl 拒绝两者并用 → 探针返回 `Error: Only one of ...`，被当作观测存下 → pods 表空 → 所有信号消失 → 27/27 scheduling 全部改道 service（JRA 0.963→0.000） | 拆成两个探针；且 runner 现在把 `Error:` 观测记入 `notes/probe_error`；新增 `coverage --smoke` 跑一遍全部探针，任何 `Error:` 即失败 |
| R11 | 用 `Tolerations: node.kubernetes.io/not-ready` 判「节点不可达」 | 每个 pod 默认都带这条 toleration → `node_reported_unreachable` gate 对 27/27 scheduling case 全为真，白花一次 probe（`good→bad=0` 但 EE 0.802→0.641） | gate 只能读**scheduler 的 FailedScheduling 消息**，不能读 pod 的 tolerations |
| R9 | 用 `GetResources(resource_type="pvcs")` 取卷证据 | 该别名不存在，工具返回 `Error: Unknown resource type`，而 runner 不报错 → 整个 PVC 子族一直在**无证据**下判定（`pvc_selector_mismatch` 全靠 Deployment.Selector 的字面命中） | 别名必须取工具白名单里的值（`persistentvolumeclaims`）；`dsl_check` 已加"probe 名存在"校验，但**参数取值**仍可能静默失效 → 后续给 probe 加首调用 smoke 断言 |
| R10 | 想区分 `pvc_capacity_mismatch` 与 `pvc_access_mode_mismatch` | 该 benchmark 的工具面不暴露 PVC 的 requested size / accessModes（`-o yaml` 不支持，describe 只有观测到的 Capacity/Access Modes）→ 两型在证据上完全同形 | **判定为不可判**：两候选同分并列，机制口径按固定顺序取第一个（等价抛硬币）；`boundaries` 里写明，不再投入 |
| R8 | 把 SOP 导出成 Markdown 供人评审 | 会变成第二个事实来源，必然与 DSL 漂移（同 R5）；且评审无法 diff 到执行物 | **否决**：改为让 DSL 自身可审（front-matter + `intent`/`refute_if` + lint），不生成第二份文档 |
| R7 | v0.4 的 `model_calls` 口径：只统计 `status=ans` | 弃权那次真实往返被漏计（报 3，实际 7） | **v0.5 修正为每次真实往返都计数**；成本对比一律用 v0.5 口径 |
| R6 | 让 judge 在机制口径也参与 | 机制口径语义就是「无模型」，混入即失去基准意义 | 机制模式显式跳过 escalate 并记录 `mechanism_mode_has_no_judge` |

## 未关闭的问题（P8 输入）

1. **jev 校准**：route 在 3/5 case 弃权到 `other`（概率 0.8–0.96），靠机械回退才正确。下一步应改 criteria 措辞/给候选加机械理由文本，并在 ≥30 条样本上测校准（n=5 不能调）。
2. **EE 0.833 vs baseline 0.900**：多出的零贡献调用是 `DescribeResource`（区分 probe 类型）与 `pvcs`（排除卷绑定）。是否可合并进 `DescribeResource` 一次输出，需在更多 case 上验证。
3. **规则覆盖**：目前 20+ 条规则覆盖 5 个 family 的代表机制，未覆盖 57 类全部；未覆盖时依赖 escalate，需扩样本测 escalate 比例。

## 补测：自由 agent 在 evolve 115 题上的成绩（口径补齐）

| 项 | 自由 agent（ReAct，deepseek-flash，≤20 步） | 固定 SOP（v0.13，机制口径） |
|---|---|---|
| CA / FA / **JRA** | 0.939 / 0.896 / **0.896** | 0.965 / 0.957 / **0.957** |
| MC / EOC / ECR / EE | 0.809 / 0.707 / 0.522 / 0.721 | 0.875 / 0.758 / 0.583 / 0.689 |
| 工具调用 / 题 | 5.05 | 4.59 |
| 模型调用 / 题 | 5.46 | **0** |
| token / 题（in / out） | 45,501 / 3,975 | 0 / 0 |
| 墙钟 / 题 | 29.1 s | 0.1 s |

证据：`runs/score_baseline_evolve.json`（115 题，5.23M 输入 token，3,350 秒）与 `runs/score_r8c_evolve_all.json`。
**口径修正**：迭代起点报告过的 0.435 是 **AgentRun v0.6** 的全量扫描，不是自由 agent 的成绩（README 1.4 已改正）。
自由 agent 未在 754 题上跑，按单题均值外推需 34.3M 输入 token / 6.1 小时。

## 收尾（v0.13，六轮汇总）

| 指标 | 基线 | 最终 | 变化 |
|---|---|---|---|
| 全量 754 机制口径正确率 | 0.435 | **0.714** | +210 题 |
| id 池（549 题） | — | **0.954** | 每轮 val/sealed 独立验证 |
| sealed（391 题，只跑一次） | — | **0.964** | 从未用于调参 |
| 候选率（754） | 0.760 | 0.836 | R5 起"有候选即停"刻意收敛 |
| 成本 | 17 次 LLM / 134.8k token / 58s（5 题基线） | **0 次 LLM** / 4.14 步/题 / 0.3s | 主路径模型调用归零 |

否决/回滚：R8（trainticket perf，R24）、R9（code 交给 judge，R25）。
完整结项报告：`08_final_report.md`。
