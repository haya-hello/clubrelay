"""与青链本地服务通信的纯函数。 / Pure helpers for the local Qinglian bridge."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx


def encode_payload(payload: dict[str, Any]) -> bytes:
    """生成稳定 JSON，便于签名与测试。 / Produce stable JSON for signing and tests."""

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sign_payload(timestamp: str, body: bytes, secret: str) -> str:
    """使用和 Django 端一致的 HMAC。 / Match the Django-side HMAC format."""

    message = timestamp.encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


async def post_event(
    bridge_url: str,
    secret: str,
    payload: dict[str, Any],
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """只向显式配置的本地桥发送事件。 / Send an event only to the configured local bridge."""

    body = encode_payload(payload)
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Qinglian-Timestamp": timestamp,
        "X-Qinglian-Signature": sign_payload(timestamp, body, secret),
    }
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = await client.post(bridge_url, content=body, headers=headers)
    try:
        data = response.json()
    except ValueError:
        data = {"ok": False, "error": "invalid_bridge_response"}
    if response.status_code >= 400 and "error" not in data:
        data["error"] = f"bridge_http_{response.status_code}"
    data["http_status"] = response.status_code
    return data


def capture_reply(result: dict[str, Any]) -> str | None:
    """仅对收录动作返回群内提示。 / Reply in-group only for capture commands."""

    capture = result.get("capture")
    messages = {
        "created": "已加入本周待整理，负责人可在青链后台审核。",
        "duplicate": "这条经验已经收录过了。",
        "manager_required": "只有已登记的负责人可以收录经验。",
        "reply_required": "请回复需要保存的消息，再发送“收录经验”。",
        "target_missing": "没有找到被回复的原消息，请稍后再试。",
    }
    return messages.get(str(capture))

