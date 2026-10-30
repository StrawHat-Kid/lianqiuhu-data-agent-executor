# HC2026 IOC + 纯问数执行器多展厅回程配置说明

> 适用范围：`lianqiuhu-ioc-executor`、`lianqiuhu-data-agent-executor`
> 目标：与安防、设施、能耗、会议四个已验收执行器使用同一套多展厅配置规则。

## 一、统一配置结构

两个执行器都使用以下环境变量。展厅数量不写死，按 `HC_TOKEN_MAP` 动态展开。

```dotenv
HC_TOKEN_MAP=<A展厅Token>:HC-A7K9P2,<B展厅Token>:HC-B4M8Q1[,<C展厅Token>:HC-C6N3R5...]

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

变量名规则：`HC_HALL_` + 展厅 ID 转大写、非字母数字转 `_`。
例如 `HC-A7K9P2` → `HC_HALL_HC_A7K9P2_*`。

## 二、两种 Token 的区别

| 凭据 | 变量 | 用途 |
| --- | --- | --- |
| 入站 Token | `HC_TOKEN_MAP` 左侧 | 产品调用执行器时放在 `X-HC-Token`，用于识别展厅 |
| 回程 Token | `HC_HALL_*_INGRESS_TOKEN` | 执行器回程到 RUISI Ingress 时放在 `X-Auth-Token` |

两种 Token 不可混用，也不要互相替代。真实值由产品/运维签发，禁止提交 Git。

## 三、回程规则

- 每个展厅的回程目标固定为 `http://<INGRESS_HOST>:<INGRESS_PORT>/agent/send`。
- 回程请求体仍保持原有 RUISI 格式：

```json
{
  "agent": "<该展厅 AGENT>",
  "to": "<请求 context.reply_to；缺失时用该展厅 TO>",
  "body": "回答/播报文本",
  "groupchat": false
}
```

- 请求携带的 `context.callback` 只用于一致性校验：必须与上述协议、主机、端口、`/agent/send` 路径完全一致，否则拒绝回程/请求（IOC 返回 403）。
- 请求中的 `agent`、`exhibitionId`、`callback` 不能覆盖 Token 已确定的展厅配置。
- 多展厅模式下缺少任一必需配置直接启动失败，不会使用其他展厅配置回退。
- 不同展厅的 `AGENT` 与 `INGRESS_HOST:PORT` 不得重复，避免串展厅。
- `TO` 可选：IOC 与纯问数都优先使用请求 `context.reply_to`，仅在其缺失时使用 `TO` 兜底。若产品需要固定接收方，请先确认是否会破坏按用户回复的业务语义。

## 四、IOC 执行器

配置文件：项目根目录 `.env`（已被 Git 忽略）。

新增/变更：

```dotenv
HC_TOKEN_MAP=...
HC_HALL_HC_A7K9P2_AGENT=...
HC_HALL_HC_A7K9P2_INGRESS_HOST=...
HC_HALL_HC_A7K9P2_INGRESS_PORT=...
HC_HALL_HC_A7K9P2_INGRESS_TOKEN=...
HC_HALL_HC_A7K9P2_TO=...
```

迁移说明：

- 旧 `HC_HALL_*_CALLBACK_ALLOWLIST` 已停用并忽略，请改为 `INGRESS_HOST` + `INGRESS_PORT` + `INGRESS_TOKEN`。
- 仅当 `HC_TOKEN_MAP` 只映射一个展厅时，旧全局 `AGENT` / `INGRESS_HOST` / `INGRESS_PORT` / `INGRESS_TOKEN` 才作为兜底；多展厅模式忽略。
- `INGRESS_HOST` / `INGRESS_PORT` 仍可供本地 `scripts/mock-ruisi-ingress.js` 使用，但不参与正式多展厅可信回程选择。
- MQTT 单 Topic、消息信封 `exhibitionId`、Narration、动态问答等业务行为不变。

## 五、纯问数执行器

配置文件：

- `config.json`：服务、模型、知识库、上传、会话（不含展厅配置，已被 Git 忽略）。
- 项目根目录 `.env`：多展厅 `HC_TOKEN_MAP` 与 `HC_HALL_*`（已被 Git 忽略）。

启动时 `main.py` 通过 `python-dotenv` 加载 `.env`，已存在的进程环境变量优先。依赖中已加入 `python-dotenv`，`start-executor.bat` 会随 `requirements.txt` 安装。

迁移说明：

- 旧 `config.json` 的 `exhibitions.token_map`、`exhibitions.halls`、`exhibitions.halls.*.callback_token`、`callback_allowlist`、`callback.token` 已停用并忽略；如仍存在，启动日志会告警。
- 请从 `config.json` 删除旧 `exhibitions` / `callback` 段，避免两套可信配置源。
- 纯问数业务、模型调用、上下文、会话隔离与回程 Body 格式不变。

## 六、两个执行器需要的实际参数

请产品提供每个展厅：

1. 入站 `X-HC-Token`（每展厅一个，可轮换）。
2. 展厅 ID（`exhibitionId`，如 `HC-A7K9P2`）。
3. 回程 `AGENT`。
4. 回程 `INGRESS_HOST`（主机名或 IP，不带协议/端口/路径）。
5. 回程 `INGRESS_PORT`。
6. 回程 `INGRESS_TOKEN`。
7. 可选：接收方 `TO`（JID），仅在请求缺少 `reply_to` 时使用。
8. 执行器对外可达地址与端口（IOC 默认 8008，纯问数默认 18034，以实际配置为准）。

## 七、部署后验证

1. 启动日志应显示展厅数量与每个展厅的 Agent / Ingress Host / Port（不显示 Token）。
2. 分别用 A/B/C 入站 Token 调用一次普通业务：
   - 响应使用正确的展览身份；
   - 回程服务只收到本展厅 Agent、回程 Token 和正文；
   - 其他展厅回程服务无请求。
3. 使用 A Token + B callback：IOC 应 403；纯问数业务可执行但不向 B 回程。
4. 删除某展厅必填变量后重启：应启动失败，且错误信息指出缺失的变量名。
5. 原有业务回归：IOC 普通指令 / Narration / 动态问答，纯问数 AI 智能问答 / 会话重置。

## 八、仍需真实部署验证的风险

- 产品实际 RUISI Ingress 是否为 `http://host:port/agent/send`；如使用 HTTPS 或不同路径，需要产品先确认协议，再调整配置模型（当前与四个已验收执行器保持一致）。
- 产品是否要求多展厅共享同一回程 Host:Port；当前按参考基线禁止共享，串展厅风险优先于共享便利。
- `context.callback` 在产品侧是否始终等于配置的 Ingress；若产品无法保证，需要在联调时确认是否改为完全忽略 callback。
- 真实 TTS 完成事件当前不可用，IOC Narration 仍按 duration 控制节奏。
- 纯问数模型、知识库与真实回程服务的连通性需部署环境实测。
