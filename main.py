"""AI问答客户端执行器（HTTP 类型睿司执行器）。

接收 RUISI-OSCA 分发过来的指令（HTTP JSON 数组或带 context 的信封），
与 DeepSeek 官方 API 进行聊天交互，支持上传数据文件作为上下文，
并把 AI 输出通过回程通道（callback）返回给睿思。

常用端口：8009
"""
import json
import logging
import os
import re
import sys
import threading
import traceback
from typing import List, Optional
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from deepseek_client import DeepSeekClient, DeepSeekError
from exhibition_auth import PLACEHOLDER_VALUE_PATTERN, TOKEN_HEADER, parse_token_map, resolve_exhibition_id
from file_store import FileStore
from sessions import SessionStore

# 与四个专题执行器一致：展厅配置全部来自环境变量（可写在项目根目录 .env）。
# 已存在的进程环境变量优先，不会被 .env 覆盖。
load_dotenv(override=False)

# ---------------------------------------------------------------------------
# 日志（显式 UTF-8，避免中文日志乱码）
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _init_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if root.handlers:
        root.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(console)
    try:
        os.makedirs("logs", exist_ok=True)
        fh = logging.FileHandler("logs/executor.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(fh)
    except Exception as e:
        print(f"日志文件初始化失败: {e}")


_init_logging()
log = logging.getLogger("main")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("EXECUTOR_CONFIG", "config.json")
DEFAULT_CONFIG = {
    "server": {"host": "0.0.0.0", "port": 8009},
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-v4-flash",
        "thinking": "enabled",
        "reasoning_effort": "medium",
        "max_tokens": 8192,
        "stream": True,
        "timeout_seconds": 300,
        "max_retries": 2,
        "retry_delay_seconds": 2,
        "system_prompt": "你是一个严谨的数据智能分析助手。用户每次提问时，都会在问题末尾附上知识库全部文件内容。请务必以知识库内容为依据作答，认真阅读并精确计算，不要编造数据；如果知识库中没有相关信息，请如实说明。回答时直接给出结论和计算过程摘要，使用中文作答。",
    },
    "knowledge_base": {"dir": "knowedge", "max_file_chars": 500000, "max_total_chars": 1200000},
    "upload": {"dir": "data/uploads", "max_file_size_mb": 64, "max_file_chars": 500000},
    "sessions": {"max_history": 20},
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        cfg = _deep_merge(cfg, user_cfg)
    else:
        log.warning("未找到配置文件 %s，使用内置默认配置。", CONFIG_PATH)
    return cfg


def _deep_merge(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


CONFIG = load_config()

DEEPSEEK_CFG = CONFIG["deepseek"]
KB_CFG = CONFIG["knowledge_base"]
UPLOAD_CFG = CONFIG["upload"]
SESSION_CFG = CONFIG["sessions"]
SERVER_CFG = CONFIG["server"]

HALL_ENV_PREFIX = "HC_HALL_"
RUISI_SEND_PATH = "/agent/send"


def _read_env(env, name: str) -> str:
    value = env.get(name)
    return value.strip() if isinstance(value, str) else ""


def _hall_env_prefix(exhibition_id: str) -> str:
    return HALL_ENV_PREFIX + re.sub(r"[^A-Z0-9]", "_", exhibition_id.upper())


def _require_non_placeholder(value: str, name: str) -> str:
    if not value:
        raise ValueError(f"missing required environment variable: {name}")
    if PLACEHOLDER_VALUE_PATTERN.match(value):
        raise ValueError(f"{name} contains a placeholder value; replace it with a real value")
    return value


def _assert_no_hall_conflicts(halls: dict) -> None:
    prefixes = {}
    for hall_id in halls:
        prefix = _hall_env_prefix(hall_id)
        if prefix in prefixes:
            raise ValueError(
                "HC_TOKEN_MAP contains exhibition ids that share the same environment prefix "
                f"{prefix}: {prefixes[prefix]} and {hall_id}"
            )
        prefixes[prefix] = hall_id

    agents = {}
    targets = {}
    for hall_id, cfg in halls.items():
        agent = cfg["agent"]
        if agent in agents:
            raise ValueError(
                f"HC hall config conflict: AGENT is shared by {agents[agent]} and {hall_id}"
            )
        agents[agent] = hall_id

        target = f"{cfg['ingress_host']}:{cfg['ingress_port']}"
        if target in targets:
            raise ValueError(
                f"HC hall config conflict: ingress target {target} is shared by {targets[target]} and {hall_id}"
            )
        targets[target] = hall_id


def read_token_map(env=None):
    env = os.environ if env is None else env
    raw = _read_env(env, "HC_TOKEN_MAP")
    if not raw:
        raise ValueError(
            "HC_TOKEN_MAP is required; config.json exhibitions are no longer read"
        )
    return parse_token_map(raw, label="HC_TOKEN_MAP")


def parse_halls(env=None, token_map=None) -> dict:
    """按 HC_TOKEN_MAP 中的展厅动态读取 HC_HALL_<展厅>_* 配置。

    多展厅模式下每个展厅必须独立提供 AGENT / INGRESS_HOST / INGRESS_PORT /
    INGRESS_TOKEN，缺任何一项都启动失败，不会回退到其他展厅。仅当 HC_TOKEN_MAP
    只映射一个展厅时，才允许用旧版 AGENT / INGRESS_HOST / INGRESS_PORT /
    INGRESS_TOKEN 作为兼容回退。
    """
    env = os.environ if env is None else env
    token_map = TOKEN_MAP if token_map is None else token_map
    hall_ids = sorted(set(token_map.values()))
    allow_legacy_fallback = len(hall_ids) == 1

    halls = {}
    for hall_id in hall_ids:
        prefix = _hall_env_prefix(hall_id)

        def resolve(suffix: str, legacy_name: Optional[str] = None) -> str:
            scoped = _read_env(env, f"{prefix}_{suffix}")
            if scoped:
                return scoped
            if allow_legacy_fallback and legacy_name:
                return _read_env(env, legacy_name)
            return ""

        agent = _require_non_placeholder(resolve("AGENT", "AGENT"), f"{prefix}_AGENT")
        ingress_host = _require_non_placeholder(
            resolve("INGRESS_HOST", "INGRESS_HOST"), f"{prefix}_INGRESS_HOST"
        )
        if any(char in ingress_host for char in (" ", "\t", "\r", "\n", "/", ":")):
            raise ValueError(
                f"{prefix}_INGRESS_HOST must be a hostname or IP address without protocol or path"
            )
        raw_port = _require_non_placeholder(
            resolve("INGRESS_PORT", "INGRESS_PORT"), f"{prefix}_INGRESS_PORT"
        )
        try:
            ingress_port = int(raw_port)
        except ValueError as exc:
            raise ValueError(f"{prefix}_INGRESS_PORT must be a valid TCP port") from exc
        if ingress_port < 1 or ingress_port > 65535:
            raise ValueError(f"{prefix}_INGRESS_PORT must be a valid TCP port")
        ingress_token = _require_non_placeholder(
            resolve("INGRESS_TOKEN", "INGRESS_TOKEN"), f"{prefix}_INGRESS_TOKEN"
        )
        to = resolve("TO", None) or None

        halls[hall_id] = {
            "exhibition_id": hall_id,
            "agent": agent,
            "ingress_host": ingress_host,
            "ingress_port": ingress_port,
            "ingress_token": ingress_token,
            "to": to,
            "ingress_url": f"http://{ingress_host}:{ingress_port}{RUISI_SEND_PATH}",
        }

    _assert_no_hall_conflicts(halls)
    return halls


TOKEN_MAP = read_token_map(os.environ)
HALLS = parse_halls(os.environ, TOKEN_MAP)

_LEGACY_EXHIBITIONS = CONFIG.get("exhibitions")
_LEGACY_CALLBACK_TOKEN = (CONFIG.get("callback") or {}).get("token")
if _LEGACY_EXHIBITIONS or _LEGACY_CALLBACK_TOKEN:
    log.warning(
        "config.json 中的 exhibitions/callback.token 已停用并忽略；展厅配置只读取 "
        "HC_TOKEN_MAP 与 HC_HALL_<展厅>_* 环境变量，请从 config.json 中移除旧配置。"
    )
log.info("多展厅配置已加载: exhibitions=%s", ", ".join(sorted(HALLS)))


def hall_config(exhibition_id: Optional[str]) -> dict:
    if not exhibition_id:
        return {}
    value = HALLS.get(exhibition_id)
    return value if isinstance(value, dict) else {}


def _effective_port(url) -> int:
    return url.port or (443 if url.scheme == "https" else 80)


def callback_matches_hall(callback, hall: dict) -> bool:
    """回程地址必须与展厅可信 Ingress 配置完全一致：http + host + port + /agent/send。"""
    if not isinstance(callback, str) or not callback.strip():
        return False
    if not isinstance(hall, dict) or not hall:
        return False
    try:
        target = urlsplit(callback)
    except ValueError:
        return False
    if target.scheme != "http":
        return False
    if (target.hostname or "").lower() != str(hall.get("ingress_host") or "").lower():
        return False
    try:
        if _effective_port(target) != int(hall.get("ingress_port")):
            return False
    except (TypeError, ValueError):
        return False
    return target.path == RUISI_SEND_PATH

client = DeepSeekClient(DEEPSEEK_CFG)
file_store = FileStore(UPLOAD_CFG)
sessions = SessionStore(SESSION_CFG)

# 指令名（与《指令设计总表》保持一致）
CMD_AI_QA = "AI智能问答"
CMD_AI_RESET = "AI问答会话重置"

app = FastAPI(title="AI问答客户端执行器", version="1.0.0")


# ---------------------------------------------------------------------------
# 指令处理
# ---------------------------------------------------------------------------
def _session_id(context: Optional[dict], exhibition_id: Optional[str] = None) -> str:
    """会话由执行器内部自动管理，无需外部传参。

    有上下文信封时按触发者 reply_to 区分用户；否则使用 default 会话。
    已认证展厅时再按展厅分桶，避免不同展厅共用同一会话历史。
    """
    prefix = f"hall:{exhibition_id}:" if exhibition_id else ""
    if context and context.get("reply_to"):
        return prefix + "user:" + str(context["reply_to"])
    return prefix + "default"


def _split_refs(raw):
    if raw is None:
        return []
    text = str(raw).replace("，", ",").replace(";", ",").replace("；", ",")
    return [x.strip() for x in text.split(",") if x.strip()]


def _resolve_file_list(params: dict):
    refs = []
    for key in ("文件", "files", "附件", "attachments"):
        if params.get(key):
            refs.extend(_split_refs(params[key]))
    if not refs:
        return []
    paths = []
    skipped = []
    for ref in refs:
        p = file_store.resolve_path(ref)
        if p:
            paths.append(p)
        else:
            skipped.append(ref)
    if skipped:
        log.warning("未能解析的文件引用: %s", skipped)
    return paths


def load_knowledge_base() -> str:
    """读取知识库目录下全部文本文件，拼成一段知识库内容。

    单轮会话模式：每次提问都会把知识库内容拼接到问题尾部一起发给大模型。
    知识库路径为相对路径（相对程序主目录，默认 knowedge），支持自动识别编码。
    """
    kb_dir = KB_CFG.get("dir", "knowedge")
    max_file_chars = int(KB_CFG.get("max_file_chars", 500000))
    max_total_chars = int(KB_CFG.get("max_total_chars", 1200000))
    if not os.path.isdir(kb_dir):
        log.warning("知识库目录不存在: %s", kb_dir)
        return ""

    files = [os.path.join(kb_dir, name) for name in sorted(os.listdir(kb_dir))
             if os.path.isfile(os.path.join(kb_dir, name))]
    if not files:
        log.warning("知识库目录为空: %s", kb_dir)
        return ""

    blocks = []
    total = 0
    capped = False
    for i, p in enumerate(files, 1):
        fname = os.path.basename(p)
        try:
            content = file_store.read_text(p, max_chars=max_file_chars)
        except Exception as e:
            content = f"（读取失败：{e}）"
        remaining = max_total_chars - total
        if remaining <= 0:
            capped = True
            break
        if len(content) > remaining:
            content = content[:remaining] + "\n...[知识库总长受限，已截断]"
            capped = True
        blocks.append(f"===== 知识库文件 {i}: {fname} =====\n{content}")
        total += len(content)
        if capped:
            break
    log.info("知识库加载: %d 个文件, 约 %d 字符%s", len(files), total, "（已按上限截断）" if capped else "")
    body = "\n\n".join(blocks)
    if capped:
        body += "\n\n（知识库内容过长，已按上限截断）"
    return body


def _build_user_content(question: str, file_paths: list, kb_text: str) -> str:
    blocks = [question]
    if file_paths:
        blocks.append("以下是用户本次额外指定的文件内容：")
        for i, p in enumerate(file_paths, 1):
            fname = os.path.basename(p)
            try:
                content = file_store.read_text(p)
            except Exception as e:
                content = f"（读取失败：{e}）"
            blocks.append(f"===== 文件 {i}: {fname} =====\n{content}")
    if kb_text:
        blocks.append("以下是知识库全部文件内容，请严格基于这些内容作答：\n\n" + kb_text)
    return "\n\n".join(blocks)


def handle_ai_qa(params: dict, context: Optional[dict], exhibition_id: Optional[str] = None) -> dict:
    question = params.get("问题") or params.get("question") or ""
    question = question.strip()
    if not question:
        return {"ok": False, "error": "缺少必填参数：问题"}
    sid = _session_id(context, exhibition_id)
    file_paths = _resolve_file_list(params)
    kb_text = load_knowledge_base()

    # 单轮会话：每次提问都是独立请求，只发送 系统提示词 + 本次问题（尾部拼接知识库）
    messages = [
        {"role": "system", "content": DEEPSEEK_CFG.get("system_prompt", "")},
        {"role": "user", "content": _build_user_content(question, file_paths, kb_text)},
    ]

    log.info("[%s] 提问: %s | 知识库字符数: %d | 额外文件数: %d",
             sid, question, len(kb_text), len(file_paths))
    try:
        answer = client.chat(messages)
    except Exception as e:
        log.error("[%s] DeepSeek 调用失败: %s", sid, e)
        return {"ok": False, "error": str(e), "question": question}

    log.info("[%s] 回答长度: %d 字符", sid, len(answer))
    return {
        "ok": True,
        "question": question,
        "answer": answer,
        "session_id": sid,
        "files_used": len(file_paths),
        "knowledge_base_chars": len(kb_text),
    }


def handle_ai_reset(params: dict, context: Optional[dict], exhibition_id: Optional[str] = None) -> dict:
    sid = _session_id(context, exhibition_id)
    sessions.clear(sid)
    return {"ok": True, "message": "会话已重置", "session_id": sid}


HANDLERS = {
    CMD_AI_QA: handle_ai_qa,
    CMD_AI_RESET: handle_ai_reset,
}


def process_commands(commands: list, context: Optional[dict], exhibition_id: Optional[str] = None) -> list:
    results = []
    for cmd in commands or []:
        action = cmd.get("action", "")
        params = cmd.get("params", {}) or {}
        handler = HANDLERS.get(action)
        if not handler:
            log.warning("未知指令: %s", action)
            results.append({"action": action, "ok": False, "error": "未知指令"})
            continue
        try:
            result = handler(params, context, exhibition_id)
        except Exception as e:
            log.error("处理指令 %s 异常: %s\n%s", action, e, traceback.format_exc())
            result = {"ok": False, "error": str(e)}
        result.setdefault("action", action)
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# 回程通道（把 AI 输出返回给睿思）
# ---------------------------------------------------------------------------
def send_callback(context: Optional[dict], body: str, exhibition_id: Optional[str] = None):
    if not context or not body:
        return
    hall = hall_config(exhibition_id)
    if not hall:
        log.error("展厅未配置可信回程信息，跳过本次回程: exhibition=%s", exhibition_id or "-")
        return

    callback = context.get("callback")
    if not callback:
        log.warning("回程上下文缺少 callback，跳过本次回程。context=%s", context)
        return
    if not callback_matches_hall(callback, hall):
        log.error(
            "回程目标与展厅 Ingress 配置不一致，跳过本次回程: exhibition=%s callback=%s trusted=%s",
            exhibition_id or "-", callback, hall["ingress_url"],
        )
        return

    to = context.get("reply_to") or hall.get("to")
    if not to:
        log.warning("回程接收方缺失（reply_to 且展厅未配置 TO），跳过本次回程: exhibition=%s",
                    exhibition_id or "-")
        return

    payload = {
        # Agent 只取 Token 映射到的展厅配置，请求上下文中的 agent 不可覆盖。
        "agent": hall["agent"],
        "to": to,
        "body": body,
        "groupchat": bool(context.get("groupchat", False)),
    }
    headers = {"X-Auth-Token": hall["ingress_token"]}
    try:
        resp = requests.post(hall["ingress_url"], headers=headers, json=payload, timeout=15)
        log.info("回程投递: HTTP %s exhibition=%s agent=%s body=%s",
                 resp.status_code, exhibition_id or "-", payload["agent"], payload["body"][:100])
    except Exception as e:
        log.error("回程投递失败: exhibition=%s error=%s", exhibition_id or "-", e)


def _pick_reply(results: list) -> str:
    for r in results:
        if r.get("ok") and r.get("answer"):
            return r["answer"]
    msgs = [r.get("message") or r.get("error") for r in results if r.get("message") or r.get("error")]
    return "；".join(msgs) if msgs else "处理完成"


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"ok": True, "name": "AI问答客户端执行器", "model": DEEPSEEK_CFG.get("model")}


@app.post("/api/commands")
async def api_commands(request: Request):
    """RUISI-OSCA 指令入口。兼容两种载荷：纯 JSON 数组 或 信封 {context, commands}。"""
    presented = request.headers.get(TOKEN_HEADER)
    exhibition_id = resolve_exhibition_id(presented, TOKEN_MAP)
    if not exhibition_id:
        reason = "invalid_token" if (presented or "").strip() else "missing_token"
        log.warning("[鉴权] 请求被拒绝，未处理任何指令 reason=%s client=%s",
                    reason, request.client.host if request.client else "-")
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体必须是合法的 JSON"})
    if isinstance(body, list):
        commands, context = body, None
    elif isinstance(body, dict):
        commands = body.get("commands", [])
        context = body.get("context")
        if "exhibitionId" in body:
            log.warning("[鉴权] 忽略请求体中的 exhibitionId，使用服务端 Token 映射 body=%s mapped=%s",
                        body.get("exhibitionId"), exhibition_id)
    else:
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求体必须是 JSON 数组或信封对象"})

    results = process_commands(commands, context, exhibition_id)
    reply = _pick_reply(results)

    # 有信封且拿到结果时，通过回程通道把 AI 输出返回给睿思
    if context:
        send_callback(context, reply, exhibition_id)

    return {"ok": True, "reply": reply, "results": results}


class ChatBody(BaseModel):
    question: str
    files: Optional[str] = None


@app.post("/api/chat")
async def direct_chat(body: ChatBody):
    """直接聊天接口（便于本地/Postman 联调）。"""
    params = {"问题": body.question, "文件": body.files}
    result = handle_ai_qa(params, None)
    return result


@app.post("/api/files/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    """multipart 表单上传数据文件，返回 file_id 列表。"""
    saved = []
    errors = []
    for up in files:
        data = await up.read()
        try:
            info = file_store.save_bytes(up.filename or "upload.bin", data)
            saved.append(info)
        except Exception as e:
            errors.append({"filename": up.filename, "error": str(e)})
    return {"ok": True, "files": saved, "errors": errors}


class ImportBody(BaseModel):
    paths: List[str]


@app.post("/api/files/import")
async def import_files(body: ImportBody):
    """导入服务器本地文件路径（联调用），生成 file_id。"""
    saved = []
    errors = []
    for p in body.paths:
        try:
            info = file_store.save_file(p)
            saved.append(info)
        except Exception as e:
            errors.append({"path": p, "error": str(e)})
    return {"ok": True, "files": saved, "errors": errors}


@app.get("/api/files")
def list_files():
    found = []
    for root, _, files in os.walk(file_store.upload_dir):
        for fn in files:
            if fn.startswith("file-") and "_" in fn:
                fid = fn.split("_", 1)[0]
                found.append({"id": fid, "filename": fn.split("_", 1)[1], "path": os.path.join(root, fn)})
    found.sort(key=lambda x: x["filename"])
    return {"ok": True, "files": found}


@app.get("/api/sessions")
def list_sessions():
    return {"ok": True, "sessions": sessions.list()}


@app.delete("/api/sessions/{sid}")
def clear_session(sid: str):
    sessions.clear(sid)
    return {"ok": True, "cleared": sid}


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    host = SERVER_CFG.get("host", "0.0.0.0")
    port = int(SERVER_CFG.get("port", 8009))
    log.info("AI问答客户端执行器启动中: http://%s:%s (model=%s)", host, port, DEEPSEEK_CFG.get("model"))
    uvicorn.run(app, host=host, port=port, log_level="info")
