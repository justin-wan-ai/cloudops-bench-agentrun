# P5 Runner：DSL 真驱动执行

## 1. 架构

```
dsl_versions/vX.Y.dsl.txt   (params / plans / state / flow)
        │  dsl_parse.load_dsl()   —— 引用、guard、action、post 穷举、计划名全部加载期校验
        ▼
   Dsl{params, state, plans, flow[]}
        │  runner.run_case()  = 顺序遍历 flow
        ▼
   ┌─ extract_anchors ──────────────── anchors.py（GT allowlist）
   ├─ probes(plan: census|family) ──── plans.compile_steps（gate 为真才执行，签名去重）
   ├─ decide (×3) ──────────────────── decide.run_decide：五段式 + intent/refute_if
   │     space → options               （signals / candidates / fixed）
   │     input → payload               （逐字段，不含别的）
   │     pre   → 机械决策表            （命中即 0 token）
   │     ask   → 只有当 pre 全不命中
   │     post  → 答案映射（other/error/unavailable 必填）
   │     out   → $node.field 绑定
   ├─ build_plan ───────────────────── plans.build_plan(family, dsl.plans, steps_per_wave)
   ├─ loop(max_waves,min_waves,stop_when) body: probes → derive → pick → verify
   ├─ escalate(when) ───────────────── judge.escalate（DeepSeek，预算内）
   ├─ normalize_answer ─────────────── top_3_predictions
   └─ persist_trace ────────────────── 官方 trace schema + `agentrun` 决策块
        │
        └─ apply_state_wiring(dsl.state)：把 out 写进规则引擎读的 state 槽位
```

## 2. 必备能力对照

| 能力 | 实现 |
|---|---|
| `--dsl <file>` | 必须；不给则用默认 v0.1 并打印 |
| `--tasks-file` | `eval/tasks_5.json`（冻结清单） |
| `--only` / `--tag` | 子集运行 / rid 命名空间（`test:case#pick2`） |
| `--jev-req` | pass A：写受限请求 JSONL（`jev/req/final_live.jsonl`） |
| `--jev-in` + `--jev-mode answers` | pass B：答案库回放，0 模型调用 |
| `--eval-out` | 逐 case 成本/决策落盘 |
| `--set k=v` | 标量参数消融（P5 门禁用；计划/闸门/guard 改 DSL 文件） |
| `dsl_check` | `--dsl …` 打印 SOP 全文（含 intent/refute_if）；`--review` 做 SOP lint（死分支/悬空接线/重复 guard），`--json` 供 CI |
| trace | 每次 `state.observe()` 记录 tool/args/observation/purpose |

## 3. rid 设计

```
test:boutique/runtime/32#route
test:boutique/runtime/32#pick1        # 第 1 波
test:boutique/runtime/32#verify1
```

rid = `{tag}:{case_id}#{node}{wave}`；pick/verify 带 wave 保证「同一波同一问题」，改计划顺序会改变答案库
键（本项目在 5 个 case 上直接从 live trace 重建答案库，因此不存在迁移问题；全量跑时必须按 rid 迁移）。

## 4. P5 门禁证据

| 门禁 | 证据 | 结果 |
|---|---|---|
| 改 params → 行为变 | `--set max_waves=1` | CA 1.00 → **0.60**，steps 3.6 → 2.2 ✅ |
| 改 **gate** → 行为变 | `ablation/v06_alerts_always.dsl.txt`（`healthy_system`→`always`） | 调用 18→22、EE 0.833→**0.680** ✅ |
| 改 **min_waves** → 行为变 | `ablation/v06_min_waves2.dsl.txt`（=v0.2 生产行为） | 生产 MC 0.820→**0.753** ✅ |
| 删 loop 内节点 → 步骤消失 | `dsl_versions/ablation_noderive.dsl.txt`（删 `derive_candidates`） | CA/FA/JRA 1.00 → **0.00**，pick=None ✅ |
| 删**节点/计划** → 加载期报错 | `ablation/v06_noroute.dsl.txt` | `unknown node reference '$route.family'` ✅ |
| 删**签核/理由** → 加载期报错 | `ablation/v06_{no_signoff,no_intent}.dsl.txt` | `missing SOP front-matter ['approved_by']` / `decide.intent is required` ✅ |
| **死分支 / 重复 guard** → lint 拦截 | `ablation/v06_{deadbranch,dupguard}.dsl.txt` | `FAIL … dead branches` / `WARN … unreachable` ✅ |
| 可离线 replay | `--jev-mode answers --jev-in jev/ans/v1.json` | 5/5 决策与最终答案**逐字节一致**，0 token ✅ |
| runner 无 GT 读取 | `anchors.INPUT_ALLOWLIST` + `GoldLeak` 运行时断言；`rules._app()` 只认官方服务名 | ✅ |
| tag 生效 | rid 前缀 = `--tag` | ✅ |
| 死变量检查 | `--set cands_cap=1` | 本样本 0 变化 → 记为「对本样本不敏感」，不当作有效改动（见 ledger） |

## 5. 策略归属（v0.4 的分界线）

| 层 | 归属 | 例子 |
|---|---|---|
| 走哪些节点、什么顺序 | **DSL** `flow` | 删 `route` → `$route.family` 悬空 → 加载期报错 |
| 每个家族探哪些、受哪个 gate 约束 | **DSL** `plans` + `probes` | 改 `alerts` 的 gate → EE 0.833→0.680 |
| 问模型的原文 / 候选渲染字段 / payload 字段 | **DSL** `decide.ask` / `space.render` / `input` | 增删字段只改 DSL |
| 什么时候不问模型（机械规则）及其顺序 | **DSL** `decide.pre` | `top_over_second >= $params.dominance_ratio` |
| 模型答 other/insufficient/error 时怎么办 | **DSL** `decide.post`（穷举，缺键报错） | `contradicted → escalate: true` |
| 决策结果落到哪个 state 槽位 | **DSL** `state:` | `picked: $pick.picked` |
| SOP 归属/签核/边界 + 每个判定的目的与证伪条件 | **DSL** front-matter / `intent` / `refute_if` | 缺则加载失败 |
| 探针语义、gate/signal/guard 的判定实现、规则引擎 | 代码 `plans.py` / `decide.py` / `rules.py` | `probe_evidence` 怎么判 |

## 5.4 消融样例的版本说明

`dsl_versions/ablation/v06_*.dsl.txt` 是 **v0.6 时代**的消融文件，用来证明 P5 门禁（改参数/删节点必须改行为）。
其中的调用数与 EE 是当时测的，不与 v0.13 的分数直接可比；v0.13 的门禁证据见 `06_ledger.md` 的 P5 表与
`runs/coverage_754_v013.json`。

## 5.5 词表与语义（v0.13 终态）

| 类别 | 词表 |
|---|---|
| code 节点 | `extract_anchors` `build_plan` `derive_candidates` `normalize_answer` |
| jev | `route` `pick` `verify`（五段式 + `intent`/`refute_if`） |
| **gate（原子 + 2 个显式复合）** | `always` `healthy_system` `weak` `weak_strong` `weak_very_strong` `probe_evidence` `restart_evidence` `volume_evidence` `runtime_stall` `unscheduled_without_events` `node_scoped_symptom` `node_all_healthy` `node_reported_unreachable` `connectivity_refused` `connectivity_node_failure` `pending_claim` `service_target_found` `service_config_clean` `service_missing_for_pod` `no_candidate_yet` ＋ `probe_or_restart_or_weak` / `restart_or_weak_strong` |
| probe（26 个） | pods · pods_labels · alerts · deployments · events · resourcequota · namespaces · pvcs · pvs · storage_classes · describe_pvc · services · endpoints · describe_pod · describe_deployment · describe_service · app_yaml · error_logs · error_logs_entry · recent_logs · connectivity · deps · deps_entry · code_files · source_code · node_kubelet · node_containerd · node_kube_proxy · node_scheduler · cluster_config |
| escalate reason | `no_candidates` `contradicted` `family_unknown` `weak_after_waves` `symptom_only`（v0.14 试点，未采纳）… |
| **gate 列表语义** | **AND**（全部成立）；OR 只能用上面两个显式复合 gate 表达（ledger R18） |
| `scope.system` | 系统专属词表类规则（告警签名 / 入口服务）的作用域；由 runner 写入 `state.scope_system` |

探针自检：`python3 -m workspace.agentrun.coverage --smoke <system>/<category>/<case>` —— 逐个探针跑一次，
任何返回 `Error:` 的探针立即失败（R12 的教训：探针**参数**失效会静默污染全部下游规则）。

## 6. 已知边界

- 循环语义是**固定的**（probe → derive → pick → verify），DSL 决定链路/参数/计划/闸门，不是逐行解释器。
- gate 与规则同源（都读 observation），因此**新增规则可能需要同步调整 gate**（现在 gate 名字在 DSL 里可见）。
- `v0.1–v0.5` 的 DSL 文件**不再可加载**（旧语法 / 缺 SOP front-matter）；其行为在 `dsl_versions/ablation/v06_min_waves2.dsl.txt` 等变体里以 v0.6 语法复现。
- `model_calls` 统计口径在 v0.5 修正：每次真实模型往返都计数（含弃权），因此同行为下 v0.4 的 3 变成 7。
- v0.6 起 trace 的 `agentrun.sop` 记录 SOP id / owner / approved_by / reviewed_at / change_ticket：一条结论可追溯到签署它的 SOP 版本。
- judge agent 用文本 JSON 协议（非 function calling），预算 `escalate_budget=8`；超预算直接放弃并记录 `notes`。
