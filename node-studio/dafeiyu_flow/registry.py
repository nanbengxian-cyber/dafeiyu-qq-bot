from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Dict, Mapping

from .model import NodeSpec, Port, Registry

MANDATORY_BLOCKED_TERMS = frozenset({"show-token", "password=", "token=", "secret="})
BLOCKED_PREVIEW_TEXT = "内容未通过出口审核。"

from .types import (
    ApprovedSendPlan,
    ContextPatch,
    Decision,
    MessageEvent,
    ModelResponse,
    NormalizedMessage,
    PreviewResult,
    PromptPlan,
    SendPlan,
)


def _input(config: Mapping[str, Any], _: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = config.get("message") or {}
    allowed = {"text", "message_type", "sender_id", "sender_name", "group_id", "mentioned"}
    return {"event": MessageEvent(**{k: v for k, v in raw.items() if k in allowed})}


def _normalize(_: Mapping[str, Any], inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    event = inputs["event"]
    text = re.sub(r"\s+", " ", event.text).strip()[:4096]
    return {"message": NormalizedMessage(
        text=text, message_type=event.message_type.lower(), sender_id=event.sender_id,
        sender_name=event.sender_name.strip()[:80], group_id=event.group_id,
        mentioned=bool(event.mentioned),
    )}


def _is_group(_: Mapping[str, Any], inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    msg = inputs["message"]
    ok = msg.message_type == "group"
    return {"message": msg, "decision": Decision(ok, "群消息" if ok else "不是群消息")}


def _acl(config: Mapping[str, Any], inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    msg, previous = inputs["message"], inputs["decision"]
    denied = set(str(x) for x in config.get("denied_ids", []))
    ok = previous.allowed and msg.sender_id not in denied
    reason = previous.reason if not previous.allowed else ("通过权限检查" if ok else "发送者在拒绝名单")
    return {"message": msg, "decision": Decision(ok, reason)}


def _should_reply(config: Mapping[str, Any], inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    msg, previous = inputs["message"], inputs["decision"]
    require_mention = bool(config.get("require_mention", True))
    command = msg.text.startswith(("/", "!", "！", "／"))
    ok = previous.allowed and not command and (msg.mentioned or not require_mention)
    if not previous.allowed:
        reason = previous.reason
    elif command:
        reason = "命令不进入聊天图"
    elif require_mention and not msg.mentioned:
        reason = "未被点名"
    else:
        reason = "允许回复"
    return {"message": msg, "decision": Decision(ok, reason)}


def _memory(config: Mapping[str, Any], inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    msg, decision = inputs["message"], inputs["decision"]
    text = str(config.get("memory_text", "演示档案：喜欢轻松聊天。"))
    return {"context": ContextPatch("memory.member", text if decision.allowed else "", 300, 320, "private")}


def _relationship(config: Mapping[str, Any], inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    decision = inputs["decision"]
    text = str(config.get("relationship_text", "演示关系：正常群友，保持自然友好。"))
    return {"context": ContextPatch("social.relationship", text if decision.allowed else "", 250, 260, "private")}


def _merge(config: Mapping[str, Any], inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    msg, decision = inputs["message"], inputs["decision"]
    patches = inputs.get("contexts", [])
    if not isinstance(patches, list):
        patches = [patches]
    total_budget = max(0, min(int(config.get("budget", 900)), 4000))
    used, blocks = 0, []
    for patch in sorted((p for p in patches if isinstance(p, ContextPatch)), key=lambda p: -p.priority):
        if not patch.content or used >= total_budget:
            continue
        text = patch.content[:min(patch.budget, total_budget - used)]
        blocks.append("[%s] %s" % (patch.namespace, text))
        used += len(text)
    user_text = msg.text if decision.allowed else ""
    return {"prompt": PromptPlan(user_text, "\n".join(blocks))}


def _fake_llm(config: Mapping[str, Any], inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    prompt = inputs["prompt"]
    if not prompt.user_text:
        return {"response": ModelResponse("")}
    prefix = str(config.get("prefix", "离线演示回复："))[:100]
    return {"response": ModelResponse(prefix + prompt.user_text[:180])}


def _moderation(config: Mapping[str, Any], inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    response = inputs["response"]
    configured = {str(item).strip().lower() for item in config.get("blocked_words", []) if str(item).strip()}
    blocked_words = MANDATORY_BLOCKED_TERMS | configured
    lowered = response.text.lower()
    blocked = any(word in lowered for word in blocked_words)
    return {"plan": ApprovedSendPlan(text=response.text if not blocked else "", approved=not blocked)}


def _humanize(config: Mapping[str, Any], inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    plan = inputs["plan"]
    text = plan.text.strip()
    if text and bool(config.get("shorten", True)) and len(text) > 120:
        text = text[:117] + "…"
    return {"plan": replace(plan, text=text)}


def _preview(_: Mapping[str, Any], inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    plan = inputs["plan"]
    text = plan.text if plan.approved else BLOCKED_PREVIEW_TEXT
    return {"result": PreviewResult(text)}


def build_registry() -> Registry:
    registry = Registry()
    specs = [
        NodeSpec("input.message", "消息输入", "输入", [], [Port("event", MessageEvent)], _input,
                 {"message": {"kind": "object", "format": "message", "label": "脱敏消息"}}),
        NodeSpec("message.normalize", "消息规范化", "输入", [Port("event", MessageEvent)], [Port("message", NormalizedMessage)], _normalize),
        NodeSpec("gate.is_group", "是否群消息", "判断", [Port("message", NormalizedMessage)], [Port("message", NormalizedMessage), Port("decision", Decision)], _is_group),
        NodeSpec("gate.acl", "权限检查", "判断", [Port("message", NormalizedMessage), Port("decision", Decision)], [Port("message", NormalizedMessage), Port("decision", Decision)], _acl,
                 {"denied_ids": {"kind": "array", "label": "拒绝名单", "default": []}}),
        NodeSpec("gate.should_reply", "是否应该回复", "判断", [Port("message", NormalizedMessage), Port("decision", Decision)], [Port("message", NormalizedMessage), Port("decision", Decision)], _should_reply,
                 {"require_mention": {"kind": "boolean", "label": "需要点名", "default": True}}),
        NodeSpec("context.member_memory_mock", "群员记忆（模拟）", "上下文", [Port("message", NormalizedMessage), Port("decision", Decision)], [Port("context", ContextPatch)], _memory,
                 {"memory_text": {"kind": "string", "label": "模拟档案", "default": "演示档案：喜欢轻松聊天。", "maxLength": 500}}),
        NodeSpec("context.relationship_mock", "社交关系（模拟）", "上下文", [Port("message", NormalizedMessage), Port("decision", Decision)], [Port("context", ContextPatch)], _relationship,
                 {"relationship_text": {"kind": "string", "label": "模拟关系", "default": "演示关系：正常群友，保持自然友好。", "maxLength": 500}}),
        NodeSpec("context.merge", "上下文合并", "编排", [Port("message", NormalizedMessage), Port("decision", Decision), Port("contexts", ContextPatch, required=False, many=True)], [Port("prompt", PromptPlan)], _merge,
                 {"budget": {"kind": "integer", "label": "总字符预算", "default": 900, "min": 0, "max": 4000}}),
        NodeSpec("model.fake_llm", "假模型（离线）", "模型", [Port("prompt", PromptPlan)], [Port("response", ModelResponse)], _fake_llm,
                 {"prefix": {"kind": "string", "label": "回复前缀", "default": "离线演示回复：", "maxLength": 100}}),
        NodeSpec("safety.output_moderation", "出口审核", "安全", [Port("response", ModelResponse)], [Port("plan", ApprovedSendPlan)], _moderation,
                 {"blocked_words": {"kind": "array", "label": "演示拦截词", "default": ["show-token", "password="]}}),
        NodeSpec("transform.humanize", "人味处理", "输出", [Port("plan", ApprovedSendPlan)], [Port("plan", ApprovedSendPlan)], _humanize,
                 {"shorten": {"kind": "boolean", "label": "过长时缩短", "default": True}}),
        NodeSpec("output.preview", "预览输出", "输出", [Port("plan", ApprovedSendPlan)], [Port("result", PreviewResult)], _preview),
    ]
    for spec in specs:
        registry.add(spec)
    return registry
