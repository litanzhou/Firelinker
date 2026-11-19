# 访谈智能体实现规格

适用于 Firelinker 1.0.0。代码与数据接口见下述模块，文件及经验生命周期分别见相关说明。

## 组成

- interviews/contracts.py：严格结构化动作、HTTP输入和来源契约。
- prompts/interviewer.zh.md：版本化访谈方法、未知处理、中立提问和收束指引。
- interviews/planner.py：用户显式选模，Chat Completions / Responses适配、能力声明和独立连接测试。
- interviews/runtime.py：一个规划器的plan→execute→wait循环、持久检查点与恢复。
- interviews/store.py：事务性原话、片段关联、工具执行、焦点、预算和执行租约。
- service.py、knowledge.py、files.py：共享记录、来源、修订、人工审核、关系、反馈与原件。

## 启动与交互

POST /api/interviews接受goal、request_id，可选domain_id(default general)、limits、case_ids及明确初始焦点case_id/decision_id。start_only=true只保存状态，工作台随后继续，允许首次调用时暂停。

新访谈不强制创建案例或判断。先查看目标和已授权的材料，再选择读取、整理或提问。已有资料时，模型先检索或读取相关原文，区分操作规定、一般建议和实际经历，再确定展开的起点。已有原话直接利用，不统一要求从头重述或按时间顺序提问。

只有目标、没有资料及原话时，程序通过 `origin=initial_text` 的本地输入邀请进入waiting，请用户先用文字提供一段经历、做法或说明；这一步不调用模型，也可在模型未配置时使用。文字通过原回答接口原样保存后，才调用模型理解与追问。暂停、刷新或重启保留同一输入邀请，重复提交不重复保存原话。目标和案例背景不作为可引用的专家原话。

模型上下文的 `input_summary` 给出已关联文本资料版本、文件版本及可读取原话的数量，独立于被截断的目录和历史。未关联材料不计入。`source_catalog` 仍然只是目录，模型需要通过工具读取正文；原话先保存，再整理案例、判断和片段关联。已有案例由人选择关联，模型只能提出关联建议；切换重点由人接受或选择继续当前。

每轮按已有信息选择下一步，不要求问遍固定清单。重复措辞、可能诱导、重访已处理缺口等只作提示；专家可反馈问题前提不成立、重复、偏题等。未知、感知和事后解释如实保留，结束允许保留缺口。超范围内容通过defer_topic列为后续话题。

## 状态与动作

访谈状态：ready / running / waiting / paused / review / error / closed。缺口独立保存open / covered / deferred / conflict / not_applicable；回答不自动等于覆盖，covered是带来源的模型建议。

| 动作 | 行为 |
|---|---|
| ask_expert | 一个重点，可无gap；保存问题和理由后interrupt |
| search_sources | 搜索本访谈、关联案例的资料、文件提取和授权原话片段 |
| read_source | 按固定ID读取允许范围；文件可带locator或native读取原件 |
| create_case / create_decision | 从叙述整理案例或判断，不自动切换重点 |
| link_case / suggest_focus | 提出已有案例关联或焦点建议，等待人工处理 |
| link_turn | 关联原话片段，不复制原话，不扩大其他场次授权片段 |
| draft_experience | 保存带修订范围与来源的待审核经验，无审核权限 |
| suggest_finish / pause | 给出理由并暂停，保留缺口，不自动宣告充分或确认 |
| defer_topic | 保存后续话题建议 |

propose_relation只用于另一个明确选择两端修订的经验比较入口；访谈图内不自行审核、撤下或裁决经验。

## 上下文、预算与恢复

默认每场40次调用、30次回答、累计60000个已报告token，每次最多5个动作。上下文依据用户声明的模型字符容量，减去提示词和余量后选择近期问答、当前重点缺口、目录等；记录截断和省略，原话不删，旧内容可搜索和按ID读取。容量不足清楚暂停，不能当作质量收束。

预算只统计本场，取得回答与覆盖程度分别判断。动作错误反馈模型修复一轮，仍失败则暂停。模型与网络失败保留原话，不切换模拟模型。

业务记录和检查点分开；原话先存再恢复。启动、回答、补充使用request_id去重。人工操作有expected_revision并更新context_epoch，旧动作按新范围重新规划。缓存成功输出先落业务记录再执行，崩溃重放不重复候选或等待已答问题。

旧图节点、状态和动作ID算法保留；缺失旧epoch按0处理。执行租约防重叠运行，暂停/结束立即收回写权限，原工作线程退出才释放执行占用。备份要一并停写复制两个数据库及原件。

## HTTP

| 路径 | 输入或输出 |
|---|---|
| GET /api/interview-config | 配置存在、用户型号、协议、能力声明、测试状态；无密钥 |
| POST /api/model-probe | 一次合成文本请求，只测连接与结构化动作 |
| GET /api/model-settings | 本机连接配置、配置版本和是否有 Key；不返回 Key |
| PUT /api/model-settings | expected_revision、provider、base_url、model、api_key、protocol、input_types、context_chars、max_output_tokens、max_attachment_bytes |
| POST /api/model-settings/probe | 同上，对当前表单执行一次测试，不保存或切换正在使用的配置 |
| GET/POST /api/interviews | 列表或启动 |
| GET /api/interviews/<id> | 当前问题、本场原话、案例、缺口、提案、模型记录与本场候选 |
| POST .../answers | question_id、request_id、answer、speaker |
| POST .../control | command=continue/pause/skip/close/draft、expected_revision、note |
| POST .../supplements | request_id、answer、speaker，可选correction_of和continue_interview |
| POST .../cases | expected_revision、case_id、可选note，由人关联已有案例 |
| POST .../focus | expected_revision，case_id/decision_id或proposal_id，decision=accept/reject |
| POST .../turn-contexts | expected_revision、turn_id、start、end、case_id、actor，可选decision_id/gap_id |
| POST .../question-feedback | expected_revision、question_id、kind、note、actor |

## 验证

脚本模型、本地模拟服务及真实SDK协议测试验证程序闭环；原生文件和人工核对见[文件说明](file-inputs.md)，模型无关合成情境见[评测说明](evaluation.md)。真实模型的中立性、追问价值和跨领域质量仍需实际运行与专家核对。
