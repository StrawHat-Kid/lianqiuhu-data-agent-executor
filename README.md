# 练秋湖 HC2026 纯问数执行器

FastAPI + Uvicorn 的单轮问数执行器：接收 RUISI-OSCA 的 `POST /api/commands`，通过 OpenAI-compatible 模型服务生成回答（默认与本机 OpenCode 使用同一服务商、模型与接口），并按展厅隔离的 RUISI Ingress 回程。

- 入站鉴权：`X-HC-Token` → `exhibitionId`
- 展厅回程：按展厅环境变量提供的 Agent、Ingress Host、Ingress Port、Ingress Token、接收方
- 会话隔离：`hall:<exhibitionId>:...`
- 展厅配置只来自环境变量（可写入项目根目录 `.env`），不再读取 `config.json` 的 `exhibitions`

## 目录

```text
lianqiuhu-data-agent-executor/
  knowedge/                 # HC 正式知识库（4 个正式文件）
  doc/                      # 接入、模板与 Postman 资料
  config.example.json       # 可提交的脱敏示例（服务/模型/知识库等）
  config.json               # 仅本机/部署环境使用，已被 Git 忽略
  .env.example              # 多展厅配置示例（可提交）
  .env                      # 真实展厅配置，已被 Git 忽略
  main.py
  llm_config.py             # 模型服务配置解析（默认值 / llm 段 / LLM_* 环境变量）
  llm_client.py             # OpenAI-compatible 对话客户端（通用，不绑定厂商）
  exhibition_auth.py
  file_store.py
  sessions.py
  start-executor.bat
```

`knowedge/` 顶层只能放入以下 4 个正式知识文件，执行器按文件名排序后全文读取：

1. `00_回答规则.md`
2. `01_权威指标事实表.csv`
3. `02_事件告警表.csv`
4. `03_指标词典与口径.csv`

## 启动

Windows 运行 `start-executor.bat`：脚本会创建或复用 `.venv`，安装 `requirements.txt` 后执行 `main.py`。

Linux：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python main.py
```

`main.py` 启动时加载项目根目录 `.env`（`load_dotenv(override=False)`），已存在的进程环境变量优先。相对路径配置依赖当前工作目录。

健康检查：`GET http://127.0.0.1:18034/api/health`

## 配置

### config.json

仅保留服务、模型、知识库、上传与会话配置：

```json
{
  "server": {"host": "0.0.0.0", "port": 18034},
  "llm": {
    "provider": "ruisi",
    "base_url": "https://llm.rsagent.net/v1",
    "api_key": "",
    "model": "dh-dev",
    "thinking_mode": "none",
    "timeout_seconds": 60
  },
  "knowledge_base": {"dir": "knowedge"},
  "upload": {"dir": "data/uploads"},
  "sessions": {"max_history": 20}
}
```

`config.json` 已被 Git 忽略，真实 API Key 不得提交。推荐把密钥写入部署环境变量 `LLM_API_KEY`，`config.json` 中的 `llm.api_key` 留空。

### 模型服务环境变量

| 变量 | 说明 |
| --- | --- |
| `LLM_PROVIDER` | 服务商标识，默认 `ruisi`（仅用于日志/健康检查） |
| `LLM_BASE_URL` | OpenAI-compatible API 根地址，默认 `https://llm.rsagent.net/v1` |
| `LLM_MODEL` | 模型名，默认 `dh-dev` |
| `LLM_API_KEY` | 部署时填写的独立 API Key，缺失时问答返回明确配置错误，不会回退旧 DeepSeek 配置 |
| `LLM_TIMEOUT_SECONDS` | 单次请求超时，默认 `60` |
| `LLM_MAX_TOKENS` | 单次回答上限，默认 `8192` |
| `LLM_STREAM` | 是否使用 SSE 流式，默认 `true` |
| `LLM_THINKING_MODE` | `none`（默认，OpenAI-compatible 通用）或 `deepseek`（额外发送 `thinking`/`reasoning_effort`） |

优先级：`LLM_*` 环境变量 > `config.json` 的 `llm` 段 > 内置默认值。

旧 `config.json` 的 `deepseek` 段不再提供连接参数（`base_url` / `model` / `api_key` 一律忽略），只继承 `system_prompt`、`max_tokens`、`stream` 等业务字段，并在启动日志告警；请迁移到 `llm` 段，避免继续指向 DeepSeek / SiliconFlow。

### 多展厅环境变量（必填）

| 变量 | 说明 |
| --- | --- |
| `HC_TOKEN_MAP` | 必填。`token:exhibitionId[,token:exhibitionId...]`；缺失或非法时启动失败 |
| `HC_HALL_<展厅>_AGENT` | 必填。该展厅回程 Agent；请求 `context.agent` 一律忽略 |
| `HC_HALL_<展厅>_INGRESS_HOST` | 必填。回程主机（仅主机名/IP，不带协议、端口或路径） |
| `HC_HALL_<展厅>_INGRESS_PORT` | 必填。回程端口 |
| `HC_HALL_<展厅>_INGRESS_TOKEN` | 必填。回程 `X-Auth-Token`；与入站 Token 是两种不同凭据 |
| `HC_HALL_<展厅>_TO` | 可选。接收方兜底 JID，仅当请求缺少 `context.reply_to` 时使用 |

变量名规则：`HC_HALL_` + 展厅 ID 转大写、非字母数字转 `_`。例如 `HC-A7K9P2` → `HC_HALL_HC_A7K9P2_*`。

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

HC_HALL_HC_C6N3R5_AGENT=<C展厅Agent>
HC_HALL_HC_C6N3R5_INGRESS_HOST=<C回程主机>
HC_HALL_HC_C6N3R5_INGRESS_PORT=<C回程端口>
HC_HALL_HC_C6N3R5_INGRESS_TOKEN=<C回程Token>
HC_HALL_HC_C6N3R5_TO=<C接收方JID>
```

示例值只表示结构，禁止作为生产参数。`example-*`、`REPLACE_WITH_*`、`change-me-*` 会被启动校验拒绝。

### 迁移与兼容规则

- `HC_TOKEN_MAP` 与 `HC_HALL_*` 是唯一可信展厅配置源。
- 旧 `config.json` 的 `exhibitions.token_map` / `exhibitions.halls` / `callback.token` 已停用并忽略；若仍存在，启动日志会给出迁移告警。请从 `config.json` 删除旧配置，避免两套配置产生误解。
- 仅当 `HC_TOKEN_MAP` 只映射一个展厅时，允许旧全局 `AGENT` / `INGRESS_HOST` / `INGRESS_PORT` / `INGRESS_TOKEN` 作为该展厅的兼容兜底。
- 多展厅模式下任一展厅缺少 `AGENT`、`INGRESS_HOST`、`INGRESS_PORT`、`INGRESS_TOKEN` 都会启动失败，不会回退到其他展厅。
- 不同展厅的 `AGENT` 与 `INGRESS_HOST:PORT` 不得重复。

## 接口

| 方法 | Path | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（匿名） |
| POST | `/api/chat` | 本地直接问答（不带展厅鉴权） |
| POST | `/api/commands` | RUISI 指令入口（必须携带 `X-HC-Token`） |
| POST | `/api/files/upload` | 上传本次问答附件 |
| POST | `/api/files/import` | 导入执行器机器上的本地文件，仅限受控联调 |
| GET | `/api/files` | 列出已上传附件 |
| GET | `/api/sessions` | 列出进程内 Session 条目 |
| DELETE | `/api/sessions/{sid}` | 清空指定 Session |

### `POST /api/commands`

```json
{
  "context": {
    "agent": "产品侧智能体标识",
    "reply_to": "触发用户标识",
    "callback": "http://<该展厅回程主机>:<该展厅回程端口>/agent/send",
    "groupchat": false
  },
  "commands": [
    { "action": "AI智能问答", "params": { "问题": "请查询某指标" } }
  ]
}
```

- 缺失、无效或未映射 `X-HC-Token` 返回 `401 {"ok":false,"error":"unauthorized"}`。
- `Body exhibitionId` 与 `context.agent` 不能覆盖 Token 鉴权结果。
- 可用 action：`AI智能问答`（必填 `问题`，可选 `文件`）、`AI问答会话重置`。
- 会话按展厅分桶：`hall:<exhibitionId>:`；不同展厅不共用历史。

### 回程

当请求带有 `context` 且完成问答后，执行器向该展厅可信 Ingress 发起：

```text
POST http://<INGRESS_HOST>:<INGRESS_PORT>/agent/send
Content-Type: application/json
X-Auth-Token: <INGRESS_TOKEN>
```

```json
{
  "agent": "<HC_HALL_展厅_AGENT>",
  "to": "<context.reply_to 或 HC_HALL_展厅_TO>",
  "body": "完整回答文本",
  "groupchat": false
}
```

- Agent 只取展厅配置，请求 `context.agent` 不能覆盖。
- 请求 `context.callback` 必须与展厅 `INGRESS_HOST`、`INGRESS_PORT`、`http` 协议和固定 `/agent/send` 路径完全一致才回程；不一致时拒绝回程并记录日志，不会改投其他地址。
- 回程失败只记录日志，不影响 `/api/commands` 已返回的业务结果。

## 产品侧注册要点

- transport：`http`
- method：`POST`
- endpoint：`http://<执行器可达地址>:18034/api/commands`
- Header：`X-HC-Token: <该展厅Token>`
- command 名称必须与 `AI智能问答`、`AI问答会话重置` 完全一致
- 如需回程，产品侧传入 context envelope，并确保 callback 与展厅 Ingress 一致

## 测试

```bash
python -m unittest discover -s test -p "test_*.py" -v
```

使用 mock RUISI callback 与 Fake LLM client，不调用真实模型和真实展厅。多展厅用例见 `test/test_multi_hall.py`，模型服务配置与 OpenAI-compatible 请求/流式解析用例见 `test/test_llm_provider.py`。

## 数据规模限制

每次问答读取整个 `knowedge/`。单文件最多注入 500,000 字符，全部文件最多注入 1,200,000 字符，超出会截断。正式上线前如数据量较大，应评估结构化查询、预计算或检索方案。
