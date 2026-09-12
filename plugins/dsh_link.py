# -*- coding: utf-8 -*-
"""Import-safe alias for the shared event-local coordination contract."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

KEY = "dsh.link.v1.turn_claim"


@dataclass(frozen=True)
class TurnClaim:
    owner: str
    kind: str
    priority: int
    block_repeat: bool = False
    block_proactive: bool = False
    already_replied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "owner": self.owner,
            "kind": self.kind,
            "priority": int(self.priority),
            "block_repeat": bool(self.block_repeat),
            "block_proactive": bool(self.block_proactive),
            "already_replied": bool(self.already_replied),
        }


def get_claim(event: Any) -> dict[str, Any] | None:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return None
    try:
        value = getter(KEY)
    except BaseException:
        return None
    return value if isinstance(value, dict) and value.get("version") == 1 else None


def claim_turn(event: Any, claim: TurnClaim) -> bool:
    setter = getattr(event, "set_extra", None)
    if not callable(setter):
        return False
    current = get_claim(event)
    if current and int(current.get("priority", 0) or 0) >= int(claim.priority):
        return False
    try:
        setter(KEY, claim.as_dict())
        return True
    except BaseException:
        return False


def blocks(event: Any, capability: str) -> bool:
    current = get_claim(event) or {}
    return bool(current.get("block_%s" % capability, False))
