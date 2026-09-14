from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class MessageEvent:
    text: str
    message_type: str = "group"
    sender_id: str = "member-demo"
    sender_name: str = "群友A"
    group_id: str = "group-demo"
    mentioned: bool = True


@dataclass(frozen=True)
class NormalizedMessage:
    text: str
    message_type: str
    sender_id: str
    sender_name: str
    group_id: str
    mentioned: bool


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class ContextPatch:
    namespace: str
    content: str
    priority: int = 100
    budget: int = 300
    sensitivity: str = "public"


@dataclass(frozen=True)
class PromptPlan:
    user_text: str
    context: str


@dataclass(frozen=True)
class ModelResponse:
    text: str
    provider: str = "offline-fake"


@dataclass(frozen=True)
class SendPlan:
    text: str
    channel: str = "preview"


@dataclass(frozen=True)
class ApprovedSendPlan:
    text: str
    channel: str = "preview"
    approved: bool = True


@dataclass(frozen=True)
class PreviewResult:
    text: str
    sent: bool = False
    note: str = "仅预览，未连接 QQ"


@dataclass
class TraceRecord:
    sequence: int
    node_id: str
    node_type: str
    status: str
    duration_ms: float
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


def to_jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


TYPE_NAMES = {
    MessageEvent: "MessageEvent",
    NormalizedMessage: "NormalizedMessage",
    Decision: "Decision",
    ContextPatch: "ContextPatch",
    PromptPlan: "PromptPlan",
    ModelResponse: "ModelResponse",
    SendPlan: "SendPlan",
    ApprovedSendPlan: "ApprovedSendPlan",
    PreviewResult: "PreviewResult",
    str: "String",
    bool: "Boolean",
}
