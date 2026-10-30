"""展厅 Token 鉴权。

语义与 ``lianqiuhu-ioc-executor/src/exhibition-auth.js`` 保持一致：
``X-HC-Token`` 精确映射到 exhibitionId，缺失/无效/未映射一律拒绝，
绝不回退到其他展厅或默认展厅。
"""
import hashlib
import hmac
import re
from typing import Dict, Optional

TOKEN_HEADER = "x-hc-token"
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{16,512}$")
PLACEHOLDER_VALUE_PATTERN = re.compile(r"^(?:example-|replace[_-]?with|change[_-]?me)", re.IGNORECASE)
EXHIBITION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _split_entries(raw, label: str):
    if isinstance(raw, str):
        entries = []
        for entry in raw.split(","):
            value = entry.strip()
            if not value:
                raise ValueError(f"{label} contains an empty entry")
            separator = value.find(":")
            if separator <= 0 or separator == len(value) - 1:
                raise ValueError(f"{label} entries must use token:exhibitionId format")
            entries.append((value[:separator].strip(), value[separator + 1:].strip()))
        return entries
    if isinstance(raw, dict):
        return [(str(token), str(exhibition_id)) for token, exhibition_id in raw.items()]
    raise ValueError(f"{label} must be a mapping or token:exhibitionId string")


def parse_token_map(raw, label: str = "exhibitions.token_map") -> Dict[str, str]:
    entries = _split_entries(raw, label)
    if not entries:
        raise ValueError(f"{label} must contain at least one token:exhibitionId entry")

    token_map: Dict[str, str] = {}
    for token, exhibition_id in entries:
        token = token.strip()
        exhibition_id = exhibition_id.strip()
        if not TOKEN_PATTERN.match(token):
            raise ValueError(f"{label} contains an invalid token (16-512 chars, no whitespace or comma)")
        if PLACEHOLDER_VALUE_PATTERN.match(token):
            raise ValueError(f"{label} contains a placeholder token; replace it with a real secret")
        if not EXHIBITION_ID_PATTERN.match(exhibition_id):
            raise ValueError(f"{label} contains an invalid exhibitionId")
        if token in token_map:
            raise ValueError(f"{label} contains a duplicate token")
        token_map[token] = exhibition_id
    return token_map


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def resolve_exhibition_id(presented_token, token_map: Dict[str, str]) -> Optional[str]:
    if not isinstance(presented_token, str):
        return None
    token = presented_token.strip()
    if not TOKEN_PATTERN.match(token):
        return None

    presented_digest = _digest(token)
    matched = None
    for configured_token, exhibition_id in token_map.items():
        if hmac.compare_digest(presented_digest, _digest(configured_token)):
            matched = exhibition_id
    return matched
