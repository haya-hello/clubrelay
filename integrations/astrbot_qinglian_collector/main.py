"""AstrBot 到青链校园版的 QQ 消息桥。 / QQ message bridge from AstrBot to Qinglian Campus."""

from __future__ import annotations

from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Reply
from astrbot.api.star import Context, Star, register

from .bridge import capture_reply, post_event


def _text(value: Any) -> str:
    """安全规范化平台字段。 / Safely normalize platform values."""

    return "" if value is None else str(value)


def _reply_component(event: AstrMessageEvent) -> Reply | None:
    for component in event.get_messages():
        if isinstance(component, Reply):
            return component
    return None


def build_payload(event: AstrMessageEvent) -> dict[str, Any]:
    """把不同 QQ 适配器消息归一成青链事件。 / Normalize QQ adapter events for Qinglian."""

    message = event.message_obj
    reply = _reply_component(event)
    group = getattr(message, "group", None)
    payload: dict[str, Any] = {
        "platform": _text(event.get_platform_name()),
        "group_id": _text(event.get_group_id()),
        "group_name": _text(getattr(group, "group_name", "")),
        "message_id": _text(getattr(message, "message_id", "")),
        "sender_id": _text(event.get_sender_id()),
        "sender_name": _text(event.get_sender_name()),
        "timestamp": getattr(message, "timestamp", None),
        "text": _text(event.get_message_str()),
        "message_type": "text",
        "reply_to_id": _text(getattr(reply, "id", "")) if reply else "",
    }
    if reply and getattr(reply, "id", None):
        payload["reply_snapshot"] = {
            "message_id": _text(reply.id),
            "sender_id": _text(getattr(reply, "sender_id", "")) or "unknown",
            "sender_name": _text(getattr(reply, "sender_nickname", "")),
            "timestamp": getattr(reply, "time", None) or payload["timestamp"],
            "text": _text(getattr(reply, "message_str", "")),
            "message_type": "text",
        }
    return payload


@register(
    "qinglian_collector",
    "Qinglian Campus",
    "将授权 QQ 群消息安全送入青链经验知识库",
    "0.1.0",
)
class QinglianCollector(Star):
    """轻量转发插件；业务规则全部留在青链服务端。 / Thin relay; rules stay in Qinglian."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    @filter.command("青链接入信息")
    async def connection_info(self, event: AstrMessageEvent):
        """帮助负责人取得白名单所需标识。 / Show IDs required for source allowlisting."""

        if not event.get_group_id():
            yield event.plain_result("请在准备接入的 QQ 群里使用 /青链接入信息。")
            return
        yield event.plain_result(
            "青链接入信息\n"
            f"平台：{event.get_platform_name()}\n"
            f"群标识：{event.get_group_id()}\n"
            f"你的负责人标识：{event.get_sender_id()}\n"
            "请只复制到本机青链后台的“授权群”页面。"
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    async def relay_group_message(self, event: AstrMessageEvent):
        bridge_url = _text(self.config.get("bridge_url")).strip()
        bridge_secret = _text(self.config.get("bridge_secret")).strip()
        if not self.config.get("enabled", True) or not bridge_url or not bridge_secret:
            return
        try:
            result = await post_event(
                bridge_url=bridge_url,
                secret=bridge_secret,
                payload=build_payload(event),
                timeout_seconds=float(self.config.get("timeout_seconds", 5)),
            )
        except Exception as exc:
            # 不记录消息正文或密钥。 / Never log message bodies or secrets.
            logger.warning(f"Qinglian bridge unavailable: {type(exc).__name__}")
            return
        response = capture_reply(result)
        if response:
            yield event.plain_result(response)
