"""Read-only companion discovery, plus the shared v1 attention lease.

Older companions have a fixed publisher roster. Snapshot polling works with them
without patching their source or registering fake user messages.
"""
from __future__ import annotations

import copy
import time
import uuid

ROLES = ("short_term_mood", "play_together", "self_amusement", "codex_bridge")


class CompanionLink:
    def __init__(self, context, config):
        self.context, self.config = context, config
        self.owner = uuid.uuid4().hex
        state = getattr(context, "_w1_companion_v1", None)
        if not isinstance(state, dict):
            state = {"turns": {}, "revision": 0, "idle": None, "last_sent": 0, "seen": {}}
            setattr(context, "_w1_companion_v1", state)
        self.state = state

    def snapshot(self, role, origin):
        if not self.config.get("enabled", True) or not self.config.get("interop_enabled", True) or not self.config.get("link_" + role, True):
            return {}
        try:
            name = "astrbot_plugin_" + role
            allowed = self.context.get_config(umo=origin).get("plugin_set", ["*"])
            star = self.context.get_registered_star(name)
            if not star or not star.activated or not star.star_cls:
                return {}
            peer = star.star_cls
            config = getattr(peer, "config", getattr(peer, "settings", {}))
            if (getattr(peer, "closing", False) or not config.get("enabled", True)
                    or not config.get("interop_enabled", True)
                    or not any(n in allowed for n in ("*", name, getattr(star, "plugin_id", "")))):
                return {}
            value = peer.companion_snapshot(origin)
            return copy.deepcopy(value) if isinstance(value, dict) else {}
        except Exception:
            return {}

    def snapshots(self, origin):
        return {role: data for role in ROLES if (data := self.snapshot(role, origin))}

    def begin(self, event):
        token = event.get_extra("w1_companion_turn")
        if not isinstance(token, str):
            token = uuid.uuid4().hex
            event.set_extra("w1_companion_turn", token)
        self.state["revision"] += 1
        self.state["turns"].setdefault(token, {})[self.owner] = time.monotonic() + 600

    def end(self, event):
        self.state["turns"].get(event.get_extra("w1_companion_turn"), {}).pop(self.owner, None)

    def claim(self, gap):
        now = time.monotonic()
        state = self.state
        state["turns"] = {k: {o: end for o, end in v.items() if end > now}
                          for k, v in state["turns"].items() if any(end > now for end in v.values())}
        if state["turns"] or time.time() - state["last_sent"] < gap:
            return None
        if state["idle"] and state["idle"]["until"] > now:
            return None
        token = uuid.uuid4().hex
        state["idle"] = {"token": token, "owner": self.owner, "revision": state["revision"], "until": now + 120}
        return token

    def current(self, token):
        lease = self.state["idle"]
        return bool(lease and lease["token"] == token and lease["until"] > time.monotonic()
                    and lease["revision"] == self.state["revision"]
                    and not any(self.state["turns"].values()))

    def release(self, token):
        if self.state["idle"] and self.state["idle"]["token"] == token:
            self.state["idle"] = None

    def close(self):
        for owners in self.state["turns"].values():
            owners.pop(self.owner, None)
        lease = self.state["idle"]
        if lease and lease["owner"] == self.owner:
            self.state["idle"] = None
