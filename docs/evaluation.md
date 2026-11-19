# 访谈规划评测

框架提供七个纯合成情境，比较不同用户自选模型的单次规划结果。它不执行模型提出的检索、建案例、提问或审核动作，也不更改工作台记录。

情境覆盖：已经说明的信息、感知与未知、中立提问、不同案例复用相同措辞、保留缺口收束、资料规定与实际经历的区别、专家纠正原回答及问题前提。

## 默认运行

```python
from experience_ledger.evaluation import run_evaluation

report = run_evaluation()
```

默认只核对合成包的结构、来源标识、原话与上下文一致性，不读取模型配置、不调用模型。即使传入规划器，仍需显式设置 `run_model=True` 才会调用。

## 使用自己配置的模型

```python
from experience_ledger.evaluation import run_evaluation
from experience_ledger.interviews.planner import planner_from_env

report = run_evaluation(planner_from_env(), run_model=True)
```

所需模型与连接由调用者明确配置。每个情境调用一次 `planner.propose(context)`，独立规划下一步。可以传入任意实现现有规划器契约的对象，不要求特定提供商。运行会消耗所选服务的实际调用额度；未配置时报告错误，不自行选择模型。

报告记录合成包哈希、提示词版本、请求及实际模型名称、返回动作、已报告用量、耗时、错误、结构检查、逐字引用检查，以及人工评阅问题。调用失败按情境保存，后续情境继续运行。未知异常只记录类型，不保存可能含密钥的原始异常正文。

`reported_tokens` 是已成功报告的用量之和；`calls_with_unknown_usage` 单独列出缺少有效用量的调用，不能把它们当作免费或零用量。

## 如何解释结果

- `fixtures_validated`：合成包检查通过，本次未调用模型。
- `contains_failures`：配置、调用、动作结构或可机械核对的引用出现问题，应检查对应条目。
- `awaiting_human_review`：有模型结果等待人工评阅，不能解释为访谈质量合格。

动作结构检查使用项目的 `InterviewAction`。引用检查核对合成来源ID、逐字引文、Unicode字符位置及草稿声明的案例范围。相同问句只形成带案例信息的信号，不会自动判定重复错误。

是否诱导、是否遗漏值得追问的信息、是否忠实表达专家意思、是否恰当收束，均保留在 `human_review` 中，由评阅者填写 `verdict` 和说明。没有固定金标准问句，没有自动语义总分；真实多轮访谈、业务工具权限和专业经验可靠性需要各自验证。

合成资源位于包内 `examples/interview_evaluation.json`，安装后的wheel同样可以读取。扩展情境时，保留明确的合成标识、原文来源和可比较的人工评阅维度。
