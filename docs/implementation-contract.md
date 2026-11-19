# 记录、版本与知识接口

适用于 Firelinker 1.0.0，兼容手工案例与访谈路径。动态访谈见[访谈规格](interview-agent.md)，原件与引用见[文件说明](file-inputs.md)。服务入口为 `experience_ledger.service.Ledger(data_dir, domains_dir=None)`，返回可序列化为 JSON 的对象。`LedgerError` 包含 `message` 和 HTTP 状态码。

## 案例与原话

- domains / create_case / list_cases / get_case：领域快照与案例。get_case含资料、文件、判断、缺口、关联原话、经验、反馈、访谈及事件。
- add_source(case_id,title,text,source_id=None)：固定文本版本及SHA-256，同一逻辑资料重复正文复用版本。
- add_decision(case_id,title,description)：按领域prompts建立手工起始缺口，数量依配置。
- add_turn(case_id,gap_id,answer,speaker,question=None)：保留原话、署名与Unicode位置；记录回答不表示信息充分。
- draft_candidate(case_id,decision_id)：手工逐字草稿，缺失内容可暂存；审核仍要求相应字段依据。

## 经验与修订

revise_candidate与revise_content使用expected_revision防止过期编辑，追加可选contexts=[{case_id,decision_id可空}]，默认继承上版。来源范围固定于修订。引用包含field、source_type、source_id、start/end、quote；file可带locator，类型包括turn/document/file/feedback。

get_candidate(id,revision=None,*,include_history=True)默认读取最新草稿，支持固定修订。返回revision、latest_revision、latest_status、active_revision、contexts、evidence、relations、feedback及历史。自动比较用include_history=False，仅读取选定来源，传模型的引用也不含source_text全文。

review_candidate(id,expected_revision,decision,reviewer,note)：decision=approve/reject；只处理pending版本，审核同事务保存。旧四字段路径要求内容与依据，新结构允许unknown但主表述有来源。pending_manual文件引文先人工核对、保存新修订再批准。

search_knowledge只返回active确认版。编辑新版保持旧active，确认新版才切换；驳回不撤下旧版。人工确认不提升unvalidated。

| HTTP | 输入与行为 |
|---|---|
| GET /api/candidates/<id>?revision=N | N须为正整数，读取固定版本 |
| POST /api/candidates/<id>/revisions | expected_revision、title、kind、statement、scope、rationale、exceptions、evidence、actor，可选contexts |
| POST /api/candidates/<id>/content | expected_revision、title、kind、content、evidence、actor，可选contexts |
| POST /api/candidates/<id>/reviews | expected_revision、decision、reviewer、note |
| POST /api/candidates/<id>/withdraw | expected_revision、expected_active_revision、actor、note；撤下默认版，保留历史 |
| GET /api/knowledge?q=&domain_id= | 当前可查阅确认版的文字包含查询，不声称条件自动匹配 |

## 关系与反馈

关系固定双方修订、条件及两端原文。修改或整合经验仍需新增修订，不自动删除或改正文。关系状态proposed/accepted/rejected/resolved；人工处理追加记录。

反馈固定实际参考的版本、是否采用、条件、做法、结果及原始说明。反馈可作为新修订证据，但其案例需明确纳入该修订范围。反馈与可靠性验证分开。

| HTTP | 输入 |
|---|---|
| GET /api/experience-relations | 可选candidate_id、status |
| POST /api/experience-relations | left_id/left_revision/right_id/right_revision、kind=duplicate/supplement/conflict、conditions、reason、evidence、actor；证据标side=left/right |
| POST /api/experience-relations/suggest | 两端ID及revision、request_id；调用用户模型，保存proposed；重复请求和已提交结果可复用 |
| POST /api/experience-relations/<id>/reviews | decision=accept/reject/resolve、reviewer、note、expected_status |
| GET /api/candidates/<id>/feedback?revision=N | 固定版反馈；不传revision才返回全版本，非法值400 |
| POST /api/candidates/<id>/feedback | revision、actor、original、actual_used(bool/null)、conditions、actions、outcome，可选case_id；版本必须曾获确认 |

GET /experience/<id>?revision=N提供确认版/历史、关系、比较与反馈页面；编辑和审核仍在案例工作台。

## 运行与数据

GET /settings 提供模型配置界面。GET /api/model-settings 返回可编辑配置和 expected_revision 所需的 revision，仅返回 has_api_key。PUT /api/model-settings 原子保存并应用；POST /api/model-settings/probe 测试表单内容，不保存。api_key 为 null 时保留当前 Key，空字符串表示清除，非空字符串表示替换。地址或服务商变化时不得复用旧 Key。过期配置返回409。

本机文件 model.keys.local.json 优先于启动环境变量。保存时不改访谈记录；一次访谈执行或经验比较固定使用其开始时的连接，完成后释放旧连接。

CLI提供serve/demo/export/evaluate。demo只有合成待审核数据，不伪造确认。JSON格式3导出是案例范围摘录，原件可精确恢复字节，跨案例依赖显式列出；整库备份必须停写复制两个数据库与blobs。

HTTP仅本机同源，无身份认证；JSON上限2 MiB，原件上限25 MiB，总上传请求26 MiB。错误返回{error:message}，不透传内部异常或模型凭据。
