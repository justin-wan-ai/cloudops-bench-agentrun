# Cloud-OpsBench × AgentRun

本仓库把一个自由形式的诊断 agent 改造成固定的诊断流程，并用数据驱动的迭代方法改进这个流程。

- 数据集：Cloud-OpsBench。754 个故障案例。数据不随本仓库分发。
- 任务：给定一个故障快照，找出根因（哪个对象坏了，坏在哪）。
- 结果：全量 754 题的根因准确率从 **0.435** 提高到 **0.714**。主路径不再调用大模型。

> 写作规则：本文按 ISO 24495-1 与 ASD-STE100 的简明原则书写。一句话一个意思。术语先用先定义。
> 同一事物只用一个词。要求用“必须”。步骤用列表。

---

## 1. 背景：为什么要改造

**问题**：值班工程师收到告警后，要自己决定查什么、看哪里。每个案例都要重新探索。这一步慢，而且不稳定。

**自由 agent 的做法**：每次都给模型 12 个诊断工具，让它自己想下一步。结果是：每个案例都重复同样的探索动作。同一条告警，两次运行的路径不稳定。成本高：每步一次完整模型调用。流程也无法回放。

**本项目的做法**：先把探索做一次，再把结论写死成流程。之后 95% 的案例走流程，剩下 5% 交回 agent。

改造后有三个部件。下文逐一解释。

| 部件 | 做什么 | 谁来做 | 成本 |
|---|---|---|---|
| 固定 SOP | 决定查什么、按什么顺序查、在什么条件下停 | 确定性代码 + 少量判定节点 | 微秒级，0 token |
| jev 判定 | 只回答类型化的选择题 | 专用判定模型（一次调用） | 毫秒级 |
| escalate 兜底 | 流程无法定案时接手 | 通用 agent（有预算） | 秒级，贵 |

### 1.1 固定 SOP

**SOP** 是 Standard Operating Procedure（标准作业流程）。在本项目里，SOP 就是一份文本文件（DSL）。
这份文件写明四件事：

1. 先查什么（例如先列 pod）。
2. 按什么顺序查（例如先看调度事件，再看节点组件）。
3. 每步在什么条件下才值得查（条件叫 gate）。
4. 结论怎么拼出来。

**为什么要写成文件**：流程在文件里，就能人工评审、能 diff、能回滚。流程写在代码里，评审者只能读代码。

**DSL** 是 Domain-Specific Language（领域专用语言）。本项目的 DSL 用 YAML 编写。加载时校验。
缺签核信息、缺判定理由、写错名字，都会直接报错。

### 1.2 jev 判定

**jev** 是 judgment 的缩写。它是一个判定节点：只回答一个受限问题，只输出机器可读的结果。

jev 的输入必须受限：

- 问题原文。
- 从题面抽出的锚点（命名空间、症状）。
- 不超过 8 个候选。

jev 的输出只有三种形式：

- 从给定选项里选一个（choice）。
- 给出“成立”的概率（noul）。
- 弃权（`other` 或 -1）。弃权后，交回确定性规则。

**为什么要 jev**：让模型只做它擅长的事（在少数候选间做语义判断）。让代码做它擅长的事（解析、匹配、排序）。
jev 的每次判定都记录 `rid`（稳定标识）、概率、耗时，因此可以回放和审计。

### 1.3 escalate 兜底

**escalate** 是升级。流程用四条可机械判定的条件决定何时停止：

- 没有候选。
- 证据与假设矛盾。
- 故障类型未知。
- 预算用完仍无强候选。

触发后交给 **judge agent**。judge agent 有工具预算（默认 8 步），只读，必须输出结构化结论。
escalate 保证一件事：流程失效时，结果不会比原 agent 更差。

### 1.4 为什么这样能省钱

| 项目 | 改造前 | 改造后 |
|---|---|---|
| 主路径模型调用 | 每题 17 次 | 0 次 |
| 每题输入 token | 134,781 | 0 |
| 每题工具调用 | 3.4 | 4.14 |
| 每题墙钟 | 11.6 秒 | 0.3 秒 |

模型只在两种情况下参与：jev 遇到歧义，或流程升级。

---

## 2. 术语表

| 术语 | 全称 / 中文 | 含义 |
|---|---|---|
| case | 案例 | 集合中的一个故障快照。共 754 个。 |
| snapshot | 快照 | 冻结的集群状态、告警、日志、指标、工具缓存。诊断只读快照，不连真实集群。 |
| tool cache | 工具缓存 | 快照里预先算好的工具输出。工具调用就是查它。 |
| fault type | 故障类型 | 根因的分类，共 57 类（如 `readiness_probe_incorrect_protocol`）。 |
| fault object | 受害对象 | 根因所在的对象，格式为 `app/<服务>`、`node/<节点>` 或 `namespace/<命名空间>`。 |
| SOP | 标准作业流程 | 本项目的流程文件（DSL）。 |
| DSL | 领域专用语言 | 写 SOP 的 YAML 格式。加载时校验。 |
| probe | 探查 | 一次工具调用（如 `GetResources(pods)`）。 |
| plan | 计划 | 某个故障类型下的 probe 顺序。 |
| gate | 闸门 | probe 的执行条件。闸门为假时跳过该 probe。多个闸门写作列表时是“全部成立”（AND）。 |
| decide | 判定节点 | 五段式节点：`ask` + `space`（问什么、选什么）、`input`（喂什么）、`pre`（机械规则）、`post`（答案映射）、`out`（输出绑定）。 |
| `pre` | 前置规则 | 问模型之前的机械判断。按顺序求值，第一个命中即生效，不花 token。 |
| `post` | 答案映射 | 模型答案到动作的穷举映射。`error` 与 `other` 必须写。 |
| `intent` | 判定目的 | 写给评审者：这个判定为什么存在。 |
| `refute_if` | 证伪条件 | 写给评审者：什么证据能推翻这个判定。 |
| rid | 判定标识 | `{tag}:{case}#{node}{wave}`。答案库与回放都以它为键。 |
| jev | 判定节点 | 只做类型化判定的节点。输入受限，输出为选项或概率，可弃权。 |
| escalate | 升级 | 流程停手，交给 judge agent。 |
| judge agent | 裁决 agent | 有预算的通用 agent，只读，必须给出结构化结论。 |
| 机制口径 | mechanism mode | 不调用模型，只跑流程与机械规则。用于判断流程对不对。 |
| 含答案口径 | answers mode | 注入 jev 答案库回放。用于验证可回放性。 |
| 生产口径 | live mode | 调用真实判定模型与 judge。用于看真实成本。 |
| header / front matter | 签核头 | SOP 文件头的 `scope`、`owner`、`approved_by`、`reviewed_at`、`change_ticket`、`boundaries`。 |
| ood | out-of-distribution | 分布外数据。不参与调参，只用于熔断检查。 |

---

## 3. 指标解释

所有指标由 Cloud-OpsBench 官方评测器计算。两侧使用同一份代码，因此可以直接比较。

| 指标 | 全称 | 含义 | 方向 | 本仓库（v0.13） |
|---|---|---|---|---|
| **CA** | Component Accuracy | 受害对象是否正确 | 越高越好 | evolve 0.965 |
| **FA** | Fault-type Accuracy | 故障类型是否正确 | 越高越好 | evolve 0.957 |
| **JRA** | Joint RCA Accuracy | 对象与类型是否同时正确 | 越高越好 | **0.957** |
| **MC** | Milestone Coverage | 覆盖了多少个必需的诊断里程碑 | 越高越好 | 0.875 |
| **EOC** | Evidence-Order Consistency | 里程碑是否按可接受的因果顺序建立 | 越高越好 | 0.758 |
| **ECR** | Evidence Closure Rate | 全部里程碑是否完成（每题 0 或 1） | 越高越好 | 0.583 |
| **EE** | Evidence Efficiency | 有贡献的工具调用占总调用的比例 | 越高越好 | 0.689 |
| **Steps** | — | 每题工具调用次数 | 越低越好 | 4.59 |
| **RAR** | Redundant Action Rate | 重复的工具调用占比 | 越低越好 | 0.000 |

过程指标在三个集合上的取值（v0.13，机制口径）：

| 集合 | MC | EOC | ECR | EE |
|---|---|---|---|---|
| evolve（115 题） | 0.875 | 0.758 | 0.583 | 0.689 |
| val（43 题） | 0.853 | 0.740 | 0.558 | 0.665 |
| sealed（391 题） | 0.870 | 0.768 | 0.550 | 0.746 |

说明三条：

1. **JRA 是主指标**。它要求对象与类型同时正确。CA 或 FA 单独正确不算对。
2. **MC、EOC、ECR、EE 是过程指标**。它们检查诊断过程，不只看最终答案。里程碑（milestone）是标注好的诊断事实
   （如“发现 pod 未就绪”）。每条里程碑有可接受的工具调用与证据模式。
3. **1 个案例的分数**：evolve 集 115 题，1 题 = 0.87 个百分点；val 集 43 题，1 题 = 2.3 个百分点。因此本项目要求
   “净提升至少 2 题”，不接受单题波动。

---

## 4. 结果

机制口径（0 模型调用）：

| 集合 | 用途 | 题数 | JRA | MC | EE | 步/题 |
|---|---|---|---|---|---|---|
| id · evolve | 迭代用 | 115 | **0.957** | 0.875 | 0.689 | 4.59 |
| id · val | 接受或回滚 | 43 | **0.861** | 0.853 | 0.665 | 4.74 |
| id · sealed | 定稿后跑一次 | 391 | **0.964** | 0.870 | 0.746 | 4.33 |
| ood · 跨系统 | 熔断检查 | 84 | 0.155 | 0.587 | 0.555 | 4.44 |
| ood · 未见类型 | 熔断检查 | 36 | 0.028 | 0.463 | 0.355 | 5.03 |
| **全量 754** | 总览 | 754 | **0.714** | — | — | 4.14 |

生产口径实测（4 题，live jev + judge）：JRA 0.750，12 次模型调用，41.8k 输入 token，44.5 秒。
同一批用答案库回放：JRA 0.750，0 次模型调用，0 token，0.6 秒。

---

## 5. 数据划分：为什么不能随机切

随机切分在这个任务里会给出错误结论。原因有三条。

1. **同一故障类型的案例共享同一套取证路径**。随机切分等于把答案从训练集漏到测试集。
2. **题型越小，噪声越大**。一个故障类型只有 3 到 32 个案例。
3. **规则会记住个别案例的字面量**（如某个 pod 名）。

因此本项目按以下方式划分。泄漏单元是**故障类型**，不是案例。

| 划分 | 含义 | 题数 |
|---|---|---|
| `id · evolve` | 迭代用。每轮只激活一个故障类别（20 至 100 题）。 | 115 |
| `id · val` | 接受或回滚的判据。 | 43 |
| `id · sealed` | 定稿后只跑一次。不参与选择。 | 391 |
| `ood_cross_system` | 另一套系统的故障类型。不参与调参。 | 84 |
| `ood_unseen_type` | 承诺不触碰的故障类型。 | 36 |
| `quarantine` | 已知当前能力不足的类型。暂不评分。 | 85 |

每轮迭代必须通过四道门：

1. **正则检查器**：禁止把案例特有字面量写进 SOP。
2. **噪声门控**：val 集净提升至少 2 题，且不退化。
3. **OOD 熔断**：分布外数据上“自信答错”不能上升。
4. **sealed 终局**：定稿后跑一次。

---

## 6. 目录

```
.
├── workspace/                项目本体
│   ├── agentrun/             执行器与工具链（21 个模块）
│   │   ├── dsl_parse.py      DSL → AST，加载时校验
│   │   ├── decide.py         五段式判定节点执行器
│   │   ├── runner.py         按 flow 执行，写轨迹
│   │   ├── plans.py          probe / gate / signal 的语义
│   │   ├── rules.py          证据 → 候选根因（规则引擎）
│   │   ├── jev 客户端        workspace/jev.py（外部提供，未修改）
│   │   ├── judge.py          升级路径的裁决 agent
│   │   ├── score.py          官方评测器桥接
│   │   ├── coverage.py       全量扫描与探针自检
│   │   ├── diag.py           每次工具调用的边际证据贡献
│   │   ├── partition.py      数据划分与每轮任务文件
│   │   ├── critic.py         禁止案例字面量
│   │   └── dsl_check.py      打印 SOP / 评审检查
│   ├── dsl_versions/         14 个版本；v0.13 是当前权威
│   ├── eval/                 partition_v5.json 与每轮任务文件
│   ├── runs/                 每轮测试结果（score / coverage / compare）
│   ├── traces/               轨迹证据（baseline、各轮、final）
│   ├── jev/                  判定请求、答案库、审计
│   └── 00_charter.md … 08_final_report.md
├── README.md                 本文件
├── LICENSE                   MIT（代码）
└── repo/                     数据集，git-ignored，3.8 GB
```

## 7. 复现

前置条件：Python 3.10 或更高版本。能访问模型 API。磁盘至少 5 GB（数据集 3.8 GB）。

```bash
# 步骤 1：取数据集（不随仓库分发）
git clone --depth 1 https://github.com/LLM4Ops/Cloud-OpsBench.git repo

# 步骤 2：填凭据
cp workspace/.env.template workspace/.env
$EDITOR workspace/.env

# 步骤 3：跑机制口径（0 模型调用）
python3 -m workspace.agentrun.runner --dsl workspace/dsl_versions/v0.13.dsl.txt \
        --tasks-file workspace/eval/tasks_evolve_all.json --jev-mode mechanism \
        --save-root workspace/traces/final_evolve
python3 -m workspace.agentrun.score --label final --root workspace/traces/final_evolve \
        --layout agentrun --tasks-file workspace/eval/tasks_evolve_all.json \
        --out workspace/runs/score_final.json

# 步骤 4：跑全量 754（约 2 到 4 分钟）
python3 -m workspace.agentrun.coverage --dsl workspace/dsl_versions/v0.13.dsl.txt \
        --out workspace/runs/coverage_final.json
```

评审 SOP 不需要运行代码：

```bash
python3 -m workspace.agentrun.dsl_check --dsl workspace/dsl_versions/v0.13.dsl.txt          # 打印 SOP
python3 -m workspace.agentrun.dsl_check --dsl workspace/dsl_versions/v0.13.dsl.txt --review  # 评审检查
python3 -m workspace.agentrun.critic    --dsl workspace/dsl_versions/v0.13.dsl.txt          # 禁止字面量
```

## 8. 边界

- **只读诊断**。本流程不重启、不回滚、不扩缩容，也不对外播报。处置仍由值班工程师按 runbook 执行。
- **结论只对快照时刻成立**。系统恢复后必须重新取证。
- **不覆盖 SEV1**。SEV1 需要 incident commander 介入。
- **剩余 216 题需要新能力**。逐类清单见 `workspace/08_final_report.md` 第 6 节。
- **早期 5 题数字属于训练集**。不能当作泛化结论。

## 9. 授权

- 数据集：Cloud-OpsBench（MIT）。不包含在本仓库。
- 方法论文档 `workspace/agentrun.md`：项目方提供的外部输入，为便于复现决策链而保留。
- 本项目代码：MIT，见 `LICENSE`。
