# 贡献指南

## 开发环境

```bash
python -m pip install -e ".[dev]"          # 运行与测试依赖
python -m pip install -e ".[sources]"      # 需要重新生成内置数据时（scripts/sources/）
pre-commit install                          # 可选：提交前自动跑 ruff
make data && make test && make lint && make eval
```

要求 Python 3.10+。改动前后都应能在本地跑通 `make test lint eval`，CI 跑的是同一套命令（`.github/workflows/ci.yml`）。

## 分支与提交

- `main` 受保护，只通过 PR 合并；不直接推送。
- 分支命名：`feat/<主题>`、`fix/<主题>`、`data/<主题>`、`docs/<主题>`、`chore/<主题>`。
- 提交信息用中文或英文均可，第一行概括改了什么（50 字以内），空一行后写原因和影响范围。一个提交只做一件事。
- 数据文件（`data/`）和代码改动尽量分开提交，方便审核数据来源。

## PR 要求

PR 模板（`.github/pull_request_template.md`）里的检查项都要过：

1. **测试**：新增行为要有测试；修 bug 先写能复现的测试。测试只用合成数据或 `data/sample/`，不依赖外网。
2. **评测**：改动解析、检索或置信度逻辑时，在 PR 描述里贴 `cngeocoder eval` 前后对比（至少 `data/eval/gold_sample.csv` 和 `data/eval/colloquial_sample.csv`）。“高置信度但错误”（`confident_wrong`）不允许上升。
3. **性能**：影响检索路径的改动，附 `python scripts/bench.py --n 200000` 前后数字。
4. **文档**：行为、接口、配置、数据格式有变化时同步更新 `README.md` 和 `docs/` 下对应文档，并在 `CHANGELOG.md` 的“未发布”下记一笔。
5. **数据文件版本**：改了 `store.py` 的表结构要递增 `SCHEMA_VERSION`，并在 CHANGELOG 注明需要重新建库。

合并方式：默认 squash merge；保留有意义的多提交历史时用 merge commit。

## 数据贡献

数据会随仓库分发，来源必须可追溯、许可允许再分发。提交前对照 [docs/data-sources.md](docs/data-sources.md)：

- 只接受政府公开数据、开放许可数据（CC0、CC BY、ODbL、WTFPL 等）和明确授权的数据；
- 不接受抓取或转存高德、百度、腾讯等商业地图服务的数据，也不接受由它们派生的数据包；
- 生成类数据通过 `scripts/sources/` 下的脚本产出，文件头写明来源、版本、许可和生成脚本，不手改；
- 新增数据源时更新 `data/README.md`、`docs/data-sources.md` 和 `NOTICE`；
- 业务数据（订单地址、签收坐标）不进仓库。

## 代码风格

- `ruff` 配置见 `pyproject.toml`（行宽 120）。
- 注释、文档字符串用中文，讲“为什么”而不是复述代码。
- 新模块保持“单进程 + 单数据文件”的设计前提（见 [docs/tech-selection.md](docs/tech-selection.md)）；引入新的运行时依赖或外部服务需在 PR 里说明理由。

## 报告问题

- 缺陷、需求：用 Issue 模板。
- 某个地址解析或定位不对：用“地址匹配问题”模板，附上原文、期望结果和实际返回（去掉个人信息）。
- 安全问题：见 [SECURITY.md](SECURITY.md)，不要公开提 Issue。
