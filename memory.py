"""Small in-memory reading view; database work belongs to background threads."""

from __future__ import annotations

import asyncio
import copy

from .cognition import retrieve, tokens


class Memory:
    def __init__(self, store):
        self.store = store
        self.lock = asyncio.Lock()
        self.states = {}
        self.views = {}

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
        rows = retrieve(view.get("memories", []), query, actor, limit)
        return {
            "memories": [{k: v for k, v in row.items() if k != "_terms"} for row in rows],
            "entities": copy.deepcopy(view.get("entities", [])),
            "relations": copy.deepcopy(view.get("relations", [])),
            "feeling": next((dict(e) for e in view.get("entities", []) if e["id"] == actor), {}),
            "portrait": dict(view.get("portraits", {}).get(actor, {})),
            "self_story": dict(view.get("portraits", {}).get("self", {})),
            "curiosity": [dict(q) for q in view.get("all_questions", []) if q["actor"] == actor][:2],
        }

    @staticmethod
    def index(view):
        for row in view["memories"]:
            row["_terms"] = tokens(row["content"] + " " + row["subject"])
        return view
