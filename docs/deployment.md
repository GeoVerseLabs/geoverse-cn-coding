# 部署与运维

服务是一个进程 + 一个 SQLite 数据文件。部署只关心两件事：镜像（代码）和数据文件。两者分开发布、分开回滚。默认以只读方式打开数据文件；开启在线编辑后可以通过管理界面、管理 API、MCP 修改数据（见 [admin.md](admin.md)）。

## 1. 部署形态

| 形态 | 做法 | 适用 |
|---|---|---|
| 单机 Docker | `docker compose up -d --build` | 试用、内网单节点 |
| 多副本 | 同一镜像起多个容器，前面挂负载均衡；数据文件每台本地一份 | 生产 |
| 私有化 / 离线 | `docker save` 导出镜像，连同 `geocoder.db` 拷到目标环境 `docker load` | 无外网环境 |

资源参考（单线程实测见 README“性能”）：

- 每个 worker 约 100–120 MB 内存（百万级 POI）；
- `WORKERS` 设为 CPU 核数，吞吐随核数线性增加；
- 磁盘为数据文件大小（百万级约 230 MB），另加日志。

## 2. 配置

全部通过环境变量注入，清单见 README“配置”。生产环境至少确认：

- `GEOCODER_DB=/data/geocoder.db`；只读部署挂载只读卷，开启在线编辑（`GEOCODER_EDITABLE=1`）时挂载可写卷（数据文件和所在目录都要可写）；
- `WORKERS` 与 CPU 核数一致；
- 需要兜底时配置 `TIANDITU_TK` 与 `TIANDITU_DAILY_QUOTA`，密钥走密钥管理；
- 需要回流时配置 `GEOCODER_REVIEW_LOG`、`GEOCODER_FEEDBACK_LOG`，指向可写卷。

- 配置 API Key（`GEOCODER_API_KEYS` 或 `GEOCODER_API_KEYS_FILE`），至少给数据维护人员一个 admin 密钥；业务方按需发 read / write 密钥，见 [auth.md](auth.md)。对公网暴露时仍建议放在网关之后，由网关做 TLS 与访问控制。
- 多 worker 共享同一个数据文件时，`GEOCODER_EDITABLE` 必须一致。

## 3. 数据发布

```bash
# 1. 建新库（写临时文件，完成后原子改名）
cngeocoder build --places ... --roads ... --boundaries ... --memory var/feedback.jsonl --out var/geocoder.next.db
# 2. 评测卡口
cngeocoder --db var/geocoder.next.db eval data/eval/gold.csv --min-district-acc 0.95 --max-confident-wrong 0
# 3. 分发到各节点并替换，滚动重启
mv var/geocoder.next.db /data/geocoder.db && docker compose restart geocoder
```

- 开启了在线编辑时，**建库前先导出变更记录**并加 `--edits` 重放，否则在线修改会随旧文件一起被替换掉：
  `cngeocoder --db /data/geocoder.db edits export --out var/edits.jsonl`，建库命令加 `--edits var/edits.jsonl`。导出到替换之间如果还有人在改，替换后再导出一次补齐。
- 进程打开数据文件后，替换文件需要**重启才会读到新数据**，重启前继续使用旧版本，不影响在线请求。
- 多副本逐台替换、逐台重启，每台重启后先过健康检查再接流量。
- 旧数据文件保留至少一个版本，回滚时换回旧文件再重启。
- 表结构（`SCHEMA_VERSION`）不一致时服务拒绝启动；升级代码前先用新代码重建数据文件。

## 4. 监控

| 项目 | 方式 | 关注点 |
|---|---|---|
| 存活 | `GET /health` | 镜像自带 HEALTHCHECK |
| 数据版本 | `GET /v1/meta` 的 `built_at`、`counts` | 各副本一致；发布后确认已更新 |
| 缓存 | `/v1/meta` 的 `cache.hits / misses` | 命中率骤降可能是流量模式变化 |
| 兜底 | `/v1/meta` 的 `fallback.breaker`、`quota_used` | 熔断打开或配额接近上限 |
| 在线编辑 | `/v1/meta` 的 `edit_version`、`counts.changes` | 各副本一致；重建前已导出 |
| 鉴权 | 管理界面“概览”的密钥调用计数、`/v1/admin/auth` 的 `denied` | 错误密钥、越权请求突增 |
| 结果质量 | 定期抽样业务请求的 `confidence` 分布与 `signals` | 低置信度占比上升说明数据覆盖不足 |

接口延迟、错误率由网关或 uvicorn 访问日志统计。

## 5. 常见问题

| 现象 | 原因与处理 |
|---|---|
| 启动报“数据文件版本不匹配” | 代码与数据文件的 `SCHEMA_VERSION` 不同，用当前代码重建数据文件 |
| 启动报“数据文件不存在” | `GEOCODER_DB` 路径或挂载错误 |
| 替换数据后结果没变 | 未重启进程；或副本间数据文件不一致，查 `/v1/meta` |
| 兜底不生效 | 未配置 `TIANDITU_TK`；熔断处于 open；当日配额用完；本地结果置信度已 ≥ 阈值 |
| 某地址结果不对 | 先看结果的 `signals` 和 `parsed`；能直接修的在管理界面改要素或加确认结果，否则按“地址匹配问题”模板提 Issue |
| 启动报“在线编辑需要数据文件及其所在目录可写” | 开了 `GEOCODER_EDITABLE=1` 但挂载为只读，或镜像内置数据文件（/app/build）；改为挂载可写卷 |
| 全部请求 401 | 配置了密钥后匿名默认不可访问；请求带上密钥，或设置 `GEOCODER_ANONYMOUS_ROLE=read` |
| 启动报密钥配置错误 | 按报错指出的条目修正（格式、角色、长度、重名） |
| 管理界面提示“服务未配置 API Key” | 管理功能必须有 admin 密钥，配置 `GEOCODER_API_KEYS` |

## 6. 回滚

- **代码**：回到上一个镜像 tag 重启。如果新版本改了 `SCHEMA_VERSION`，需同时换回旧版数据文件。
- **数据**：换回上一版数据文件后重启。开启在线编辑时，旧文件之后的在线修改不在旧文件里：从当前文件导出变更记录，换回后可按需用新建库 + `--edits` 补回。
