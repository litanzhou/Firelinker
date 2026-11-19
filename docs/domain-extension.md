# 扩展一个领域

领域差异通过访谈指引、问题类别与经验字段表达。程序共享案例、原话、证据、修订与审核约束。

所有领域共用“先查看目标与已有材料，再选择读取、整理或提问”的开场规则。没有资料及原话时先收集用户输入的文字，保存后再调用模型。领域指引补充具体关注重点，不要求用户重复已有内容。

## 配置示例

将以下合成配置存入 my-domains/craft.json：

```json
{
  "id": "ceramic_craft",
  "name": "陶艺制作",
  "version": "1.0",
  "description": "合成示例：记录材料状态与制作判断。",
  "object_label": "坯体",
  "prompts": {
    "perception": "针对“{decision}”，当时看见或触摸到了什么？",
    "stopping": "你对{object}会在什么情况下停止处理？"
  },
  "interview_guidance": "从具体制作经历开始，保留无法精确量化的感知线索和不确定性。",
  "experience_fields": [
    {"key": "statement", "label": "经验表述", "required": true},
    {"key": "observations", "label": "视觉与触感线索", "required": true},
    {"key": "material_state", "label": "材料条件", "required": true},
    {"key": "stop_criteria", "label": "停止条件及未知", "required": true}
  ]
}
```

id、问题类别及字段 key 以小写字母开头，只含小写字母、数字和下划线，最长64字符。字段 key 不能重复，必须包含 statement，字段数1至20。required 是布尔值。
prompts 是手工判断的起始问题模板，可使用 {decision} 和 {object}。动态访谈根据目标、领域指引、原话和具体缺口生成问题，不要求问遍模板。
省略 experience_fields 的旧配置继续使用原有四字段结构。新配置会在案例创建时快照，经验修订另存其字段结构。

## 使用

```sh
python -m experience_ledger serve --data-dir .local-craft --domains-dir my-domains --port 5057
```

自定义目录替换内置领域目录。若需要通用领域或现有两个领域，将对应 JSON 一并放入目录。修改配置后重启，对新案例生效。

## 内容性质和证据

每个新经验字段保存 text 与 status：

- supported：已有原文依据，审核前必须提供逐字引用。
- unknown：明确标记尚未说明，不要求编造引用或内容。
- hypothesis：待检验假设，保留提出它的原话；审核不会将其变成已验证事实。

经验 statement 本身需要原话依据。人工确认核对忠实表达；可靠性仍为 unvalidated。
字段新增不要求修改审核及证据代码，引用的 field 使用配置中的 key。

## 验证适配

使用合成案例验证：从一句目标启动，提出符合领域情境的问题，保留感知/未知，形成有字段来源的候选，修改、审核并查询。
检查新增领域所需改动是否限于配置与实际需要的工具；问题质量还需真实模型及专家核对。
仓库内置 [通用配置](../src/experience_ledger/domains/general.json)、[设备配置](../src/experience_ledger/domains/electronics_repair.json) 和 [口述史配置](../src/experience_ledger/domains/oral_history.json)。行为测试另有仅含假设与接受标准的合成领域，验证字段扩展。
