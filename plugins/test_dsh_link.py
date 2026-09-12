# -*- coding: utf-8 -*-
"""Pure tests for the event-local DSH plugin coordination contract."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dsh_link as m


class Event:
    def __init__(self):
        self.extra = {}

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value


e = Event()
assert m.get_claim(e) is None
assert m.claim_turn(e, m.TurnClaim("proactive", "candidate", 20, block_proactive=True))
assert m.blocks(e, "proactive")
assert not m.blocks(e, "repeat")
assert not m.claim_turn(e, m.TurnClaim("repeat", "sent", 20, block_repeat=True))
assert m.get_claim(e)["owner"] == "proactive"
assert m.claim_turn(e, m.TurnClaim("guard", "moderation", 100, block_repeat=True,
                                   block_proactive=True, already_replied=True))
assert m.get_claim(e)["owner"] == "guard"
assert m.blocks(e, "repeat") and m.blocks(e, "proactive")
assert m.get_claim(e)["already_replied"] is True
print("DSH_LINK_TEST_OK")
