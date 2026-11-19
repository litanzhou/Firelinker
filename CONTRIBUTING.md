# 参与开发

欢迎提交可复现的问题、领域示例和改进。请先阅读 [领域术语](CONTEXT.md) 与 [当前范围](PROJECT_STATE.md)，让修改服务于案例、来源与审核闭环。

## 本地检查

```sh
python -m venv .venv
# 激活虚拟环境后：
python -m pip install -c requirements-lock.txt -e ".[dev,files]"
python -m pytest -q
python -m build
```

CI 配置在 Windows、Ubuntu 的 Python 3.11 / 3.13 上安装、测试和构建 wheel。涉及界面的修改还应在本地浏览器操作受影响流程。

浏览器流程使用临时合成数据。安装依赖后，可以运行以下命令。

```sh
python -m pip install -e ".[browser]"
python -m playwright install chromium
python tools/browser_smoke.py
python tools/interview_browser_smoke.py
python tools/lifecycle_browser_smoke.py
python tools/file_browser_smoke.py
python tools/model_settings_browser_smoke.py
```

## 修改约定

- 业务规则写入 `Ledger` 或事务性 `InterviewStore`，HTTP、工作流和界面调用相同业务约束；业务核心不依赖模型 SDK 或 LangGraph。
- 保留原始文字、资料版本和历史修订。修改已确认经验应生成待审核的新修订。
- 引用使用 Python Unicode 码点的半开区间 `[start, end)`；前端处理非 BMP 字符时需转换。
- 对行为变化补充有意义的测试，特别是错误归属、引用不一致、审核冲突和失败回滚。
- 数据库结构变化需明确版本与迁移方式。当前 schema 7，迁移文件在 `src/experience_ledger/migrations/`。升级保留旧记录并拒绝未知版本。
- 新领域优先补充配置和合成案例；需要改变共享模型时，说明现有模型无法表达的具体场景。

## 数据与贡献内容

示例使用合成内容，或确认可公开再分发的资料并注明来源和许可。不要提交密钥、真实访谈、私有手册、运行数据库或本地导出。`.gitignore` 是辅助措施，提交前仍需检查 diff 与文件清单。

问题报告应包含复现步骤、程序版本、Python 版本及实际报错。请用合成输入复现，避免将私人数据放进公开问题。

拉取请求说明具体问题、修改后的行为和验证结果。贡献内容按项目 [MIT 许可证](LICENSE) 分发；新增依赖前核对版本、许可和必要性。
