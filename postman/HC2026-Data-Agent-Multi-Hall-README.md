# 纯问数执行器多展厅 Postman 测试材料

## 导入

1. 导入 `HC2026-Data-Agent-Multi-Hall.postman_collection.json`。
2. 导入 `HC2026-Data-Agent-Multi-Hall.postman_environment.json`。
3. 选择环境 `HC2026 Data Agent Multi-Hall Local`，把 `tokenA/tokenB/tokenC` 替换为真实展厅入站 Token（只保存在本地，禁止提交）。
4. 核对 `baseUrl`（默认 `http://localhost:18034`）与 `config.json` 的 `server.port` 一致。
5. 核对 `dataCallbackA/B/C` 分别等于对应展厅 `HC_HALL_*_INGRESS_HOST`、`INGRESS_PORT` 与固定 `/agent/send`。

## 前置条件

执行器启动前必须已经在 `.env` 或进程环境中配置：

```dotenv
HC_TOKEN_MAP=<A展厅Token>:HC-A7K9P2,<B展厅Token>:HC-B4M8Q1,<C展厅Token>:HC-C6N3R5
HC_HALL_HC_A7K9P2_AGENT=<A展厅Agent>
HC_HALL_HC_A7K9P2_INGRESS_HOST=<A回程主机>
HC_HALL_HC_A7K9P2_INGRESS_PORT=<A回程端口>
HC_HALL_HC_A7K9P2_INGRESS_TOKEN=<A回程Token>
HC_HALL_HC_B4M8Q1_AGENT=<B展厅Agent>
HC_HALL_HC_B4M8Q1_INGRESS_HOST=<B回程主机>
HC_HALL_HC_B4M8Q1_INGRESS_PORT=<B回程端口>
HC_HALL_HC_B4M8Q1_INGRESS_TOKEN=<B回程Token>
HC_HALL_HC_C6N3R5_AGENT=<C展厅Agent>
HC_HALL_HC_C6N3R5_INGRESS_HOST=<C回程主机>
HC_HALL_HC_C6N3R5_INGRESS_PORT=<C回程端口>
HC_HALL_HC_C6N3R5_INGRESS_TOKEN=<C回程Token>
```

缺少任一必需配置执行器会直接启动失败，不会回退到其他展厅。

## 用例判定

| 用例 | 预期 |
| --- | --- |
| 01 健康检查 | 200，`ok=true`，无需 Token |
| 02/03/04 A/B/C 会话重置 | 200；对应展厅回程服务收到 `agent`、`X-Auth-Token`，且只收到本展厅请求 |
| 05 A 智能问答 | 200，`reply` 非空；回程正文等于回答文本 |
| 06 缺少 Token | 401 `{"ok":false,"error":"unauthorized"}`，无回程 |
| 07 未知 Token | 401，无回程 |
| 08 Body 伪造展厅 + Agent | 200；回程仍使用 B 展厅 Agent 与回程 Token |
| 09 A Token + B callback | 200（业务执行），但不向 B 回程；执行器日志记录回程目标不一致 |
| 10 直接问答 | 200；不触发回程 |

## 本地回程服务

可用任意 HTTP mock 在 `dataCallbackA/B/C` 对应端口监听 `/agent/send`，记录请求 Header 与 Body。建议分别启动三个端口，验证 A/B/C 不串用：

- `31001` → A 展厅
- `31002` → B 展厅
- `31003` → C 展厅

真实 Token 不得写入集合或环境 JSON；仓库内只保留占位值。
