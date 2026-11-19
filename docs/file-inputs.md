# 文件输入与模型能力

本页说明当前项目接口。具体型号由部署者显式选择，程序不回填默认型号，也不自动切换模型。配置示例见 [配置变量](../.env.example)。

## 原文件与本地解析

每个上传原件最多 25 MiB。原件按 SHA-256 保存，下载时重新核对字节、大小与哈希。同一逻辑文件可增加版本，旧版本继续保留。解析结果与原件分开，每次重新解析产生新的 `parse_id`；旧引用继续指向原来的原件版本与解析版本。

| 格式 | 原件保存 | 可选本地解析 |
|---|---|---|
| TXT / Markdown | 支持 | UTF-8 文字，以固定文本片段定位 |
| PDF | 支持 | 按页提取文字与页面尺寸；不执行 OCR |
| DOCX | 支持 | 段落与表格单元格；不提取嵌入图片、页眉页脚或分页 |
| XLSX | 支持 | 非空单元格的值或公式原文；不计算公式，不提取图表或嵌入图片 |
| PPTX | 支持 | 按幻灯片顺序提取正文和表格文字。备注、版式、图形和嵌入图片请对照原件 |
| PNG / JPEG / WEBP | 支持 | 读取图片尺寸；不执行 OCR 或图像语义识别 |
| 旧 DOC / XLS / PPT | 支持 | 本地解析不支持，可另存新版格式或选择支持的原生输入 |

安装可选解析依赖：

```powershell
python -m pip install ".[files]"
```

缺少解析依赖、文件损坏、加密或超过本地提取限制时，原件继续保留；解析状态明确显示 `unavailable`、`failed` 或 `unsupported`。不会把空解析结果当作成功识别。当前本地提取限制为两百万字符、两万个锚点，工作表使用区域最多一百万个单元格，压缩文档展开总大小最多 100 MiB。

Python 扩展者可调用 `ledger.parse_file(version_id, extractor=my_parser)`。解析器接收 `(filename, original_bytes)`，返回 `(parser_identity, anchors, warnings)`。锚点使用下面列出的稳定位置，解析器身份应含版本。HTTP 不接受解析器代码或磁盘路径。

PPTX 提取使用 Python 标准库。程序按演示文稿中的幻灯片关系顺序定位，不访问外部链接。每个幻灯片 XML 部件的本地读取上限为 8 MiB。

## 两种模型输入路径

本地解析和原生输入可以按所选模型与资料选择；上传原件本身不会自动把文件发送给模型。

- **本地解析**：模型通过 `read_source` 读取解析文字和稳定锚点。默认的 Chat Completions 适配器仅走文本与结构化动作。
- **显式原生输入**：部署者设置 `EXPERIENCE_LLM_PROTOCOL=responses`，并在 `EXPERIENCE_LLM_INPUT_TYPES` 声明所选模型支持的扩展名。模型提出 `read_source` 且 `source_type=file,native=true` 后，后台先核对关联案例权限，再按固定来源 ID 读取原件，将文件或图片送入 Responses 请求。

文件采用 `input_file`，图片采用 `input_image`。原件字节只在实际调用时加载，不写入工具结果、访谈检查点或浏览器 JSON。请求设置 `store=false`；提供商的数据处理政策由部署者自行了解。当前没有实现 DeepSeek 原生文件协议；`compatible` 提供商需要部署者确认其端点实际支持所选协议。

如需直接发送演示文稿，先确认服务端点和模型接受该原件格式，再显式声明 `ppt` 或 `pptx`。文件允许上传保存并不代表所选服务支持读取。

`ModelCapabilities` 记录结构化动作、输入类型、上下文字符预算、输出 token 预算与附件字节预算。字符预算包含提示词、访谈文本与反馈；它不是提供商精确 token 估算。原生文件的额外模型 token 用量由提供商报告，附件另受字节预算限制。未声明格式、接口路线不支持或输入超限时明确报错，原始记录保留。

`configured` 表示连接参数已配置。`POST /api/model-probe` 发出一次合成文本请求，检查实际连接和结构化动作返回；结果单独保存在 `probe`。该测试不包含真实访谈，也不证明文件理解、问题质量或专业判断可靠。文件内容与真实模型质量需要使用获授权资料分别评测。

## 引用与人工核对

文件引用绑定 `source_type=file`、固定版本 `source_id` 与位置 `locator`。有本地解析时，程序将选定解析版本的 `parse_id` 固定到引用，并核对文字引文与锚点逐字一致；图片区域可以没有文字引文，此时保留空 `quote` 与空字符区间。

只使用原生模型、未做本地解析，或扫描图像文字尚未经过提取核对时，模型提出的引用可以保存为 `pending_manual` 草稿。程序仍检查原件存在、格式及位置的基本结构；未知页数、尺寸或文字内容不会标成程序已核验。待人工核对的文件引用不能直接通过经验审核。

审核人对照原件后，调用 `confirm_file_reference` 或对应 HTTP 接口，记录核对人、说明和固定原件/位置/引文，获得 `manual_check_id`。将返回引用保存为新修订后，引用状态为 `human_checked`，再进行经验审核。原有草稿和核对前的状态保留。调用者自行填写 `verification=human_checked` 无效，核对记录也不能挪用于另一版本、位置或引文。程序核对过本地锚点的状态为 `position_checked`，仍需人工审核文字含义。

| 原件 | 位置示例 | 序号与单位 |
|---|---|---|
| TXT / MD | `{"kind":"text"}` | 锚点内字符位置 |
| PDF | `{"kind":"pdf","page":1}` | 页码从 1 开始 |
| DOCX 段落 | `{"kind":"docx","paragraph":0}` | 段落从 0 开始 |
| DOCX 表格 | `{"kind":"docx","table":0,"row":0,"column":0}` | 表格、行、列从 0 开始 |
| XLSX | `{"kind":"xlsx","sheet":"Sheet1","range":"A1:B3"}` | 保留实际工作表名称与单元格范围 |
| PPT / PPTX | `{"kind":"slide","slide":1}` | 按演示稿顺序，从第 1 张开始。旧 PPT 的位置需人工核对 |
| 图片 | `{"kind":"image","bbox":[10,20,100,80]}` | 左上角起算的像素 `[x,y,width,height]` |

PDF 视觉引用需在页码之外提供 `bbox`，单位为页面坐标点，相对该页 MediaBox 左下角，边界按所存页面宽高检查。它与图片左上角像素坐标不同。Word 本地解析不声称具有稳定页码。

程序能核对原件版本、解析版本、页面/工作表是否存在、区域是否越界，以及可提取文字是否逐字一致。区域可定位与内容解释正确分别核对。原生模型给出的页码、单元格或区域是待核对的引用；审核人仍需打开原件确认其是否支持经验表述。

## HTTP 与 Python 接口

- `POST /api/cases/<case_id>/files`：multipart 字段 `file`，可选 `file_id` 更新同一逻辑文件；`parse` 指定是否请求本地解析。
- `GET /api/files/<version_id>`：原件元数据、解析状态及锚点。
- `GET /api/files/<version_id>/download`：经完整性校验后的原件下载。
- `GET /api/files/<version_id>/preview`：仅图片可内联预览。
- `POST /api/files/<version_id>/parse`：重新解析，生成新的解析版本。
- `POST /api/files/<version_id>/reference-checks`：提交 `locator`、`quote`、`actor`、`note` 与可选 `start/end`，记录人工对照原件的结果，返回带 `manual_check_id` 的引用；将其保存进新修订才会改变经验引用。
- Python：`add_file`、`list_files`、`get_file`、`file_bytes`、`parse_file`、`read_file`。`read_file(..., native=True)` 返回受控附件引用，文件读取器在实际模型调用前加载字节。

## 协议来源

核对日期：2026-11-17。先检查本地 OpenAI Python SDK 2.54.0 的 `Responses.create`、`ResponseInputFileParam`、`ResponseInputImageParam` 与函数工具类型，再对照官方说明确认原生请求格式。

- [OpenAI File inputs](https://developers.openai.com/api/docs/guides/file-inputs)：Responses 文件类型、`input_file` 与 Base64 文件内容；非 PDF 文档和表格有各自提取范围。
- [OpenAI Images and vision](https://developers.openai.com/api/docs/guides/images-vision)：`input_image` 与图片 Data URL。
- [Microsoft PresentationML 文本提取](https://learn.microsoft.com/en-us/office/open-xml/presentation/how-to-get-all-the-text-in-all-slides-in-a-presentation)：根据演示稿关系定位幻灯片并读取文字。

这些来源说明所实现的协议格式；具体端点与模型能力仍由部署者确认。可选本地解析依赖许可原文见 [文件依赖许可记录](FILE_DEPENDENCY_NOTICES.md)。
