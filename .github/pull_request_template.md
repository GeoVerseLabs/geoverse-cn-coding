## 改了什么

<!-- 一两句话说明这次改动和原因；关联 Issue 用 Closes #编号 -->

## 改动类型

- [ ] 功能
- [ ] 缺陷修复
- [ ] 数据（新增或更新 data/ 下的文件）
- [ ] 文档
- [ ] 工程 / CI

## 验证

- [ ] `make test` 通过
- [ ] `make lint` 通过
- [ ] `make eval` 通过（改动解析、检索或置信度时，在下面贴前后对比）
- [ ] 影响检索路径时附 `scripts/bench.py` 前后数字

<!-- 评测 / 性能对比贴在这里 -->

## 数据与兼容性

- [ ] 新增或更新的数据来源可追溯、许可允许再分发，已更新 data/README.md、docs/data-sources.md、NOTICE
- [ ] 改了表结构并递增了 `SCHEMA_VERSION`（需要重新建库）
- [ ] 接口、配置或数据格式有变化，已更新 README / docs 和 CHANGELOG
