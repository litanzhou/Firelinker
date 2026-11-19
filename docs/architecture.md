# 架构与记录边界

0.3沿用Python、Flask、SQLite、LangGraph和Pydantic。一个主访谈规划器通过可替换模型接口提出动作；Ledger与InterviewStore管理权威业务记录，业务核心不依赖模型SDK或HTTP。

```text
浏览器工作台 → Flask → LangGraph(plan / execute / wait) → 用户配置的模型
                       ↓
               Ledger / InterviewStore
                       ├─ ledger.sqlite3：业务记录、修订、原话关联、审核
                       ├─ blobs/：不可变文本及文件原件
                       └─ interview-checkpoints.sqlite3：执行检查点
```

## 数据关系

- interviews保存目标、领域快照及可空当前焦点；interview_cases关联多个案例。案例包含多个判断。
- turns只保存一次原话，turn_contexts用Unicode码点半开区间关联案例、判断和缺口。主动补充、纠正追加记录，保留原ID和原文。
- 缺口归属判断，可跨访谈继续补充；访谈历史、预算与其他场次分开。
- candidates独立保存领域，revisions固定每次表述，revision_contexts固定该修订的案例范围，evidence逐字段引用来源。
- current_revision指最新草稿，active_revision指默认可查阅确认版；待审新版不撤下旧确认版。新审核通过才切换，人可撤下，历史保留。
- experience_relations绑定双方修订、条件和原话依据；人工处理追加审查记录。experience_feedback固定所用版本、条件、采用情况、做法、结果及原始反馈，可靠性保持unvalidated。

## 来源与权限

模型工具只读本访谈与明确关联案例；其他访谈的回答只读已关联片段。经验引用受其修订的明确范围约束，不能仅凭来源存在就引用。

来源包括turn/document/file/feedback。文本核对固定版本、Unicode位置和逐字引文。文件引用绑定固定派生解析或原件人工核对记录。模型原生读取后尚不能本地核对的位置可存pending_manual草稿，人对照原件确认后必须形成带核对依据的新修订才能批准。位置可核对与表述含义正确分别检查。

file_versions保存原件元数据和哈希，file_parses保存不可变提取快照。原件字节仅在模型请求时通过受控来源ID加载，不进入图状态和动作日志。可选本地解析与原生输入分开，见[文件输入](file-inputs.md)。

模型比较只读取选定修订、实际引用片段，不发送完整历史、其他草稿或共享原话的其他片段。comparison_requests保存请求ID、输出、租约和结果，成功重放不重复调用，业务已写未标完成也可恢复。模型响应尚未保存时崩溃，重试仍可能再次请求提供商。

## 一致性与恢复

正常写入开启外键、WAL和事务。修订、审核与审计事件同事务提交；expected_revision拒绝过期编辑，撤下额外检查expected_active_revision，避免旧页面撤下刚批准的新版。

schema为7，按打包迁移文件顺序升级。表重建在迁移连接BEGIN之前临时关闭外键强制检查，提交前foreign_key_check，失败回滚；正常连接仍启用外键。原有ID、原文及旧字段保留，拒绝高于当前打包版本的数据库。

LangGraph保持plan/execute/wait节点与旧动作ID算法。人工焦点、关联、补充改变context_epoch，旧动作改为重新规划；已回答或跳过的问题不重问。暂停和结束立即阻止晚返回动作继续写入。

## 检索、导出与备份

默认检索只返回active确认版的正文和该版信息，提示另有修订、版本相关分歧和反馈。检索采用文字包含，不声称自动条件匹配。

格式3 JSON是所选案例范围摘录：共享回答按授权片段去重，跨案例综合内容列外部依赖，所选案例原件嵌入精确字节。它不是全库恢复备份；尚无JSON导入。完整备份需停止写入，复制整个数据目录以及自定义领域配置。

当前仅本机单用户，无身份认证或团队权限。外部LangSmith追踪关闭。错误不透传提供商原始响应或凭据。
