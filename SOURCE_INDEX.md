# 实现与示例索引

| 内容 | 位置 |
| --- | --- |
| 业务校验和基础存储 | [service.py](src/experience_ledger/service.py) · [schema.sql](src/experience_ledger/schema.sql) · [迁移](src/experience_ledger/migrations/) |
| 访谈规划、动作和暂停恢复 | [提示词](src/experience_ledger/prompts/interviewer.zh.md) · [动作契约](src/experience_ledger/interviews/contracts.py) · [运行器](src/experience_ledger/interviews/runtime.py) |
| 经验生命周期和文件原件 | [经验管理](src/experience_ledger/knowledge.py) · [文件处理](src/experience_ledger/files.py) · [导出](src/experience_ledger/exports.py) |
| 合成演示和评测 | [设备排查](src/experience_ledger/examples/electronics_repair.json) · [口述史编辑](src/experience_ledger/examples/oral_history.json) · [访谈评测](src/experience_ledger/examples/interview_evaluation.json) |
| 验证和开发 | [自动化测试](tests/) · [浏览器验收](tools/) · [贡献说明](CONTRIBUTING.md) |

LangGraph 的等待和恢复机制参见 [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)。文件传给模型的请求格式和限制按所选服务的接口及模型能力配置。
