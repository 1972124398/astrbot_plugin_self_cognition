"""Optional bidirectional companion discovery, plus the shared v1 attention lease.

Recognized v1 rosters are extended reversibly; unsupported versions fall back to
snapshots. No peer source files or methods are changed.
"""

from __future__ import annotations

import copy
import time
import uuid

ROLES = ("short_term_mood", "play_together", "self_amusement", "codex_bridge", "daily_routine")
COMPANION_CLEANUP = {
    "daily_routine": ("日常调查", "所选会话和人格的日常画像、记录、挂念和待整理片段"),
    "short_term_mood": ("短期共享情绪", "整个会话的时间线、近期情绪笔记和待评估内容"),
    "self_amusement": ("自娱自乐", "整个会话的相处印象、共同兴趣、念头和近期主动发言记录"),
    "play_together": ("一起玩", "整个会话的游戏房间、待处理回合和最近游戏记录"),
    "codex_bridge": ("Codex 桥接", "所示 AstrBot 会话的已结束任务记录、专属播报和本会话的播报投递记录"),
    "active_sticker": ("主动表情", "所示 AstrBot 会话所有人格的已发表情记录和风格包使用状态"),
}


class CompanionLink:
    def __init__(self, context, config):
        self.context, self.config = context, config
        self.owner = uuid.uuid4().hex
        state = getattr(context, "_w1_companion_v1", None)
        if not isinstance(state, dict):
            state = {"turns": {}, "revision": 0, "idle": None, "last_sent": 0, "seen": {}}
            setattr(context, "_w1_companion_v1", state)
        self.state = state
        self.registrations = {}

    def peer(self, role, origin=""):
        if (
            not self.config.get("enabled", True)
            or not self.config.get("interop_enabled", True)
            or not self.config.get("link_" + role, True)
        ):
            return None
        try:
            name = "astrbot_plugin_" + role
            star = self.context.get_registered_star(name)
            peer = star.star_cls if star and star.activated else None
            config = getattr(peer, "config", getattr(peer, "settings", {}))
            if (
                not peer
                or getattr(peer, "closing", False)
                or not config.get("enabled", True)
                or not config.get("interop_enabled", True)
                or not config.get("link_self_cognition", True)
            ):
                return None
            if origin:
                allowed = self.context.get_config(umo=origin).get("plugin_set", ["*"])
                if not any(n in allowed for n in ("*", name, getattr(star, "plugin_id", ""))):
                    return None
            return peer
        except Exception:
            return None

    def connect(self):
        """Register a participant in the bundled v1 roster without editing peer files.

        V1's own publish/context_for methods already implement both directions.
        Only an explicitly recognized NAMES mapping is extended, and only while
        both plugins opt in. No methods are replaced; owned entries are reversible.
        """
        for role in ROLES:
            peer = self.peer(role)
            read = getattr(getattr(peer, "link", None), "context_for", None)
            mapping = getattr(read, "__globals__", {}).get("NAMES")
            previous = self.registrations.get(role)
            if previous and previous[0] is not mapping:
                self.disconnect(role)
            if not isinstance(mapping, dict) or mapping.get(role) != "astrbot_plugin_" + role:
                self.disconnect(role)
                continue
            if role not in self.registrations:
                owned = "self_cognition" not in mapping
                mapping.setdefault("self_cognition", "astrbot_plugin_self_cognition")
                self.registrations[role] = (mapping, owned)

    def disconnect(self, role):
        previous = self.registrations.pop(role, None)
        if previous and previous[1] and previous[0].get("self_cognition") == "astrbot_plugin_self_cognition":
            previous[0].pop("self_cognition", None)

    def connected(self, role):
        return role in self.registrations and self.peer(role) is not None

    def snapshot(self, role, origin):
        if (
            not self.config.get("enabled", True)
            or not self.config.get("interop_enabled", True)
            or not self.config.get("link_" + role, True)
        ):
            return {}
        try:
            name = "astrbot_plugin_" + role
            allowed = self.context.get_config(umo=origin).get("plugin_set", ["*"])
            star = self.context.get_registered_star(name)
            if not star or not star.activated or not star.star_cls:
                return {}
            peer = star.star_cls
            config = getattr(peer, "config", getattr(peer, "settings", {}))
            if (
                getattr(peer, "closing", False)
                or not config.get("enabled", True)
                or not config.get("interop_enabled", True)
                or not any(n in allowed for n in ("*", name, getattr(star, "plugin_id", "")))
            ):
                return {}
            value = peer.companion_snapshot(origin)
            return copy.deepcopy(value) if isinstance(value, dict) else {}
        except Exception:
            return {}

    def sticker_catalog(self, origin):
        """Read the active sticker library for background reflection."""
        peer = self.peer("active_sticker", origin)
        read = getattr(peer, "companion_sticker_catalog", None)
        if not read:
            return {}
        try:
            value = read(origin)
            return value if isinstance(value, dict) and value.get("version") == 1 and isinstance(value.get("items"), list) else {}
        except Exception:
            return {}


    def snapshots(self, origin):
        return {role: data for role in ROLES if (data := self.snapshot(role, origin))}

    def sent(self, origin, message, event_id=""):
        """Share confirmed speech through the peers' existing v1 observation hook."""
        self.state["last_sent"] = time.time()
        packet = {
            "version": 1,
            "source": "self_cognition",
            "kind": "initiative",
            "origin": origin,
            "id": event_id or uuid.uuid4().hex,
            "time": time.time(),
            "data": {"text": message},
        }
        seen = self.state["seen"]
        for role in ROLES:
            receive = getattr(self.peer(role, origin), "companion_observe", None)
            key = (role, packet["id"])
            if not receive or key in seen:
                continue
            try:
                receive(copy.deepcopy(packet))
                seen[key] = packet["time"]
            except Exception:
                continue
        while len(seen) > 256:
            seen.pop(next(iter(seen)))

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
        state["turns"] = {
            k: {o: end for o, end in v.items() if end > now}
            for k, v in state["turns"].items()
            if any(end > now for end in v.values())
        }
        if state["turns"] or time.time() - state["last_sent"] < gap:
            return None
        if state["idle"] and state["idle"]["until"] > now:
            return None
        token = uuid.uuid4().hex
        state["idle"] = {"token": token, "owner": self.owner, "revision": state["revision"], "until": now + 120}
        return token

    def current(self, token):
        lease = self.state["idle"]
        return bool(
            lease
            and lease["token"] == token
            and lease["until"] > time.monotonic()
            and lease["revision"] == self.state["revision"]
            and not any(self.state["turns"].values())
        )

    def release(self, token):
        if self.state["idle"] and self.state["idle"]["token"] == token:
            self.state["idle"] = None

    def close(self):
        for role in list(self.registrations):
            self.disconnect(role)
        for owners in self.state["turns"].values():
            owners.pop(self.owner, None)
        lease = self.state["idle"]
        if lease and lease["owner"] == self.owner:
            self.state["idle"] = None
