"""Small in-memory reading view; database work belongs to background threads."""

from __future__ import annotations

import asyncio
import copy
import time
import json

from .cognition import retrieve, tokens, relevant_relations


class Memory:
    def __init__(self, store):
        self.store = store
        self.lock = asyncio.Lock()
        self.states = {}
        self.views = {}
        self.focus = {}

    async def call(self, function, *args, **kwargs):
        async with self.lock:
            writing = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
            try:
                return await asyncio.shield(writing)
            except asyncio.CancelledError:
                await writing
                raise

    async def load(self):
        for state in await self.call(self.store.scopes):
            await self.refresh(state["id"])

    async def refresh(self, scope):
        state, view = await self.call(self.store.read_view, scope)
        if state:
            self.states[scope] = state
            self.views[scope] = view

    def identity(self, scope, origin, persona):
        return self.states.setdefault(
            scope,
            {
                "id": scope,
                "origin": origin,
                "persona": persona,
                "persona_prompt": "",
                "generation": 0,
                "paused": 0,
                "last_human": 0,
                "last_sent": 0,
                "no_contact": 0,
            },
        )

    def recall(self, scope, actor="", query="", limit=8):
        view = self.views.get(scope, {})
        previous = self.focus.get((scope, actor))
        effective = query
        if (
            previous
            and time.monotonic() - previous[1] < 1800
            and any(word in query for word in ("他", "她", "那个", "这件", "后来", "继续", "上次", "怎么样"))
        ):
            effective += " " + previous[0]
        if query:
            self.focus[scope, actor] = (effective[-600:], time.monotonic())
        rows = retrieve(view.get("memories", []), effective, actor, limit)
        terms = tokens(effective)
        events = sorted(
            view.get("experiences", []),
            key=lambda e: (len(terms & e.get("_terms", set())) * 4 + (actor in e["actors"]), e["created"]),
            reverse=True,
        )[:2]
        return {
            "memories": [{k: v for k, v in row.items() if k != "_terms"} for row in rows],
            "entities": copy.deepcopy(view.get("entities", [])),
            "relations": copy.deepcopy(
                relevant_relations(view.get("relations", []), view.get("entities", []), effective, actor)
            ),
            "feeling": next((dict(e) for e in view.get("entities", []) if e["id"] == actor), {}),
            "portrait": dict(view.get("portraits", {}).get(actor, {})),
            "relation_focus": any(
                e["id"] != "self" and e["name"] and e["name"] in query for e in view.get("entities", [])
            ),
            "self_story": dict(view.get("portraits", {}).get("self", {})),
            "character": copy.deepcopy(view.get("character", {})),
            "experiences": [{k: v for k, v in event.items() if k != "_terms"} for event in events],
            "intentions": [dict(i) for i in view.get("intentions", []) if not actor or i["actor"] == actor],
            "curiosity": [dict(q) for q in view.get("all_questions", []) if q["actor"] == actor][:2],
        }

    @staticmethod
    def index(view):
        aliases = {}
        for alias in view.get("aliases", []):
            aliases.setdefault(alias["entity"], []).append(alias["alias"])
        for entity in view.get("entities", []):
            entity["aliases"] = aliases.get(entity["id"], [])
        for row in view["memories"]:
            cues = row.get("cues", [])
            row["cues"] = json.loads(cues) if isinstance(cues, str) else cues
            row["_terms"] = tokens(
                " ".join([row["content"], row["subject"], *row["cues"], *aliases.get(row["subject"], [])])
            )
        for event in view.get("experiences", []):
            event["_terms"] = tokens(event["title"] + " " + event["story"] + " " + " ".join(event["cues"]))
        return view
