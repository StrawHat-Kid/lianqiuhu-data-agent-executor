# 练秋湖 HC2026 纯问数执行器接入说明

## 用途与技术栈

本执行器用于基于练秋湖业务数据进行单轮问数。它是一个 FastAPI + Uvicorn HTTP 执行器，通过 SiliconFlow（硅基流动）OpenAI Compatible Chat Completions API 生成回答。

当前实现会在每次提问时读取 `knowedge/` 顶层的全部普通文件，将文件内容拼接到本次用户消息中。它不是向量知识库：没有 embedding、向量数据库、索引、SQL 或自动分块检索。

## 目录与配置

```text
lianqiuhu-data-agent-executor/
  knowedge/                 # HC 正式数据后续放入此目录
  doc/                      # 接入、模板与 Postman 文档
  config.example.json       # 可提交的脱敏示例（服务/模型/知识库）
  config.json               # 仅本机/部署环境使用，已被 Git 忽略
  .env.example              # 多展厅配置示例（可提交）
  .env                      # 真实展厅配置，已被 Git 忽略
  main.py
  deepseek_client.py
  file_store.py
  sessions.py
  start-executor.bat
```

目录名必须保持为 `knowedge`。这是当前配置和核心代码使用的名称。

`knowedge/` 顶层只能放入以下 4 个正式知识文件，执行器会按文件名排序后全文读取：

1. `00_回答规则.md`
2. `01_权威指标事实表.csv`（696 条事实）
3. `02_事件告警表.csv`（35 条事件）
4. `03_指标词典与口径.csv`（315 条指标词典）

不得保留旧版 7 文件，也不要把 ZIP、`validation/` 文件或子目录放入 `knowedge/`，否则会被一并注入模型上下文。设施管理的“工单数量”“告警数”“时长”中 01～31 表示本月第 1～31 日，属于月内日序列，不是小时序列。

首次部署时，将 `config.example.json` 复制为 `config.json`，再填写部署环境提供的 SiliconFlow API Key。不得提交真实 Key。当前模型服务商为 SiliconFlow（硅基流动），OpenAI Compatible Base URL 为 `https://api.siliconflow.cn/v1`，模型为 `deepseek-ai/DeepSeek-V4-Flash`。原 DeepSeek 官方 API 不再作为当前 HC 调用地址。默认监听为 `0.0.0.0:18034`。

### 多展厅 Token 鉴权与回程配置（必填）

`POST /api/commands` 必须携带 `X-HC-Token`，服务端按环境变量 `HC_TOKEN_MAP` 精确映射展厅。展厅配置只来自环境变量（可写入项目根目录 `.env`），不再读取 `config.json` 的 `exhibitions`：

```dotenv
HC_TOKEN_MAP=<A展厅Token>:HC-A7K9P2,<B展厅Token>:HC-B4M8Q1,<C展厅Token>:HC-C6N3R5

HC_HALL_HC_A7K9P2_AGENT=<A展厅Agent>
HC_HALL_HC_A7K9P2_INGRESS_HOST=<A回程主机>
HC_HALL_HC_A7K9P2_INGRESS_PORT=<A回程端口>
HC_HALL_HC_A7K9P2_INGRESS_TOKEN=<A回程Token>
HC_HALL_HC_A7K9P2_TO=<A接收方JID>

HC_HALL_HC_B4M8Q1_AGENT=<B展厅Agent>
HC_HALL_HC_B4M8Q1_INGRESS_HOST=<B回程主机>
HC_HALL_HC_B4M8Q1_INGRESS_PORT=<B回程端口>
HC_HALL_HC_B4M8Q1_INGRESS_TOKEN=<B回程Token>
HC_HALL_HC_B4M8Q1_TO=<B接收方JID>
```

- 变量名规则：`HC_HALL_` + 展厅 ID 转大写、非字母数字转 `_`。展厅数量不写死，按 `HC_TOKEN_MAP` 动态展开。
- 缺失、无效或未映射的 Token 一律返回 `401 {"ok":false,"error":"unauthorized"}`，不回退到任何展厅。
- 每个展厅必须提供 `AGENT`、`INGRESS_HOST`、`INGRESS_PORT`、`INGRESS_TOKEN`；缺失任一项执行器启动失败，不会回退到其他展厅。
- 入站 Token（`HC_TOKEN_MAP` 左侧）与回程 Token（`HC_HALL_*_INGRESS_TOKEN`）是两种不同凭据。
- 回程 Agent 只取展厅配置，请求 `context.agent` 不可覆盖；Body `exhibitionId` 也会被忽略。
- 回程地址由服务端固定为 `http://<INGRESS_HOST>:<INGRESS_PORT>/agent/send`；请求 `context.callback` 必须与协议、主机、端口和 `/agent/send` 路径完全一致才放行，否则拒绝回程并记录日志。
- 不同展厅的 `AGENT` 与 `INGRESS_HOST:PORT` 不得重复；重复会启动失败，避免串展厅。
- 示例占位值（Token、Agent 或 Ingress 主机，`REPLACE_WITH_*`、`example-*`、`change-me-*`）会被启动校验拒绝，必须替换为真实值。
- 会话按展厅分桶（`hall:<展厅>:` 前缀），不同展厅不共用历史；`HC_TOKEN_MAP` 缺失或非法时执行器启动失败。
- `X-HC-Token` 只影响 `/api/commands`；`/api/health` 与本地联调接口保持原有行为。
- 旧 `config.json` 中的 `exhibitions` 与 `callback.token` 已停用并忽略；如仍存在，启动日志会告警，请迁移后从 `config.json` 删除。
- 仅当 `HC_TOKEN_MAP` 只映射一个展厅时，旧全局 `AGENT` / `INGRESS_HOST` / `INGRESS_PORT` / `INGRESS_TOKEN` 才作为该展厅的兼容兜底；多展厅模式忽略。

## 本地启动与健康检查

Windows 下运行 `start-executor.bat`。脚本会在项目目录创建或复用 `.venv`，安装 `requirements.txt` 后执行 `main.py`。

健康检查：`GET http://127.0.0.1:18034/api/health`

Linux 可在安装依赖后，从项目根目录执行 `python main.py`。相对路径配置依赖当前工作目录。

## 接口

| 方法 | Path | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| POST | `/api/chat` | 本地直接问答 |
| POST | `/api/commands` | RUISI 指令入口 |
| POST | `/api/files/upload` | 上传本次问答附件 |
| POST | `/api/files/import` | 导入执行器机器上的本地文件，仅限受控联调 |
| GET | `/api/files` | 列出已上传附件 |
| GET | `/api/sessions` | 列出进程内 Session 条目 |
| DELETE | `/api/sessions/{sid}` | 清空指定 Session |

`POST /api/chat`：

```json
{"question":"请查询某指标","files":"可选，多个文件引用以逗号分隔"}
```

`POST /api/commands` 支持 JSON 数组或以下信封：

```json
{
  "context": {
    "agent": "产品侧智能体标识",
    "reply_to": "触发用户标识",
    "callback": "产品侧回程地址",
    "groupchat": false
  },
  "commands": [
    {
      "action": "AI智能问答",
      "params": {"问题": "请查询某指标"}
    }
  ]
}
```

可用 action：

- `AI智能问答`：必填参数 `问题`；可选参数 `文件`。
- `AI问答会话重置`：无参数。

当前为单轮问答。虽然项目保留 Session 接口，但问答主流程不会保存历史消息。

## 回程 callback

当 `context` 中同时包含 `callback` 与 `reply_to` 时，执行器在完成问答后向 callback 发起 JSON POST：

```json
{
  "agent": "该展厅 HC_HALL_*_AGENT",
  "to": "context.reply_to 或该展厅 HC_HALL_*_TO",
  "body": "完整回答文本",
  "groupchat": false
}
```

请求 Header 固定为该展厅的 `X-Auth-Token: <HC_HALL_展厅_INGRESS_TOKEN>`，POST 目标固定为 `http://<HC_HALL_展厅_INGRESS_HOST>:<HC_HALL_展厅_INGRESS_PORT>/agent/send`。请求中的 `context.callback` 只用于一致性校验，不能改变回程目标；不一致时拒绝回程并记录日志。

## 产品侧注册要点

- transport：`http`
- method：`POST`
- endpoint：`http://<执行器可达地址>:18034/api/commands`
- 请求 Header 必须携带 `X-HC-Token: <该展厅Token>`；缺失或未映射返回 401。
- command 名称必须与 `AI智能问答`、`AI问答会话重置` 完全一致。
- 如需回程，产品侧传入上述 context 信封。
- 上游 SiliconFlow 可流式读取，但本执行器对产品侧返回的是普通 HTTP JSON，不提供对外 SSE。

## 数据规模限制

每次问答会读取整个 `knowedge/` 目录。单文件最多注入 500,000 字符，全部文件内容最多注入 1,200,000 字符。超出部分会被截断。

当 HC 数据量较大，或需要严格、可复现的聚合计算时，应在正式上线前评估结构化查询、预计算或检索方案；不能仅通过继续增加 Prompt 内容解决。
