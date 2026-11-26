"""Small in-memory reading view; database work belongs to background threads."""

from __future__ import annotations

import asyncio
import copy
import time
import json

from .cognition import retrieve, tokens, relevant_relations
from .daily_memory import linked_rows
from .followups import blocked


def current_thought(item, now):
    # Reuse the existing 30-day curiosity window. A dated future intention
    # starts aging when it becomes due, rather than when it was first written.
    return max(item.get("updated", item.get("created", now)), item.get("not_before", 0)) >= now - 30 * 86400


class Memory:
    def __init__(self, store):
        self.store = store
        self.lock = asyncio.Lock()
        self.states = {}
        self.views = {}
        self.focus = {}
        self.daily_enabled = lambda scope: True
        self.daily_context_enabled = lambda scope: True

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

    def recall(self, scope, actor="", query="", limit=8, *, conversation=False):
        view = self.views.get(scope, {})
        now = time.time()
        previous = self.focus.get((scope, actor)) if conversation else None
        changed_topic = conversation and any(
            phrase in query.casefold()
            for phrase in ("换个话题", "不聊这个", "不说这个", "聊点别的", "change the subject", "new topic")
        )
        if changed_topic:
            previous = None
            self.focus.pop((scope, actor), None)
        effective = query
        if (
            previous
            and time.monotonic() - previous[1] < 600
            and any(word in query for word in ("他", "她", "那个", "这件", "后来", "继续", "上次", "怎么样"))
        ):
            effective += " " + previous[0]
        if query and conversation and effective == query:
            # A follow-up can borrow the anchor, but cannot grow it or renew its
            # lifetime indefinitely just by saying "后来呢" again.
            self.focus[scope, actor] = (query[-600:], time.monotonic())
        terms = recall_terms(effective)
        threads = [dict(row) for row in view.get("question_threads", []) if not actor or row["actor"] in {actor, "self"}]
        candidates = list(view.get("memories", []))
        if conversation or not query:
            candidates = [r for r in candidates
                          if r.get("importance", 0.5) >= 0.5 or now - r.get("updated", now) <= 7 * 86400]
        if self.daily_enabled(scope) and (not conversation or self.daily_context_enabled(scope)):
            native_text = {row["content"] for row in candidates}
            for row in linked_rows(view.get("daily_memory", {}), actor, effective, time.time(), conversation):
                if row["content"] not in native_text:
                    row["_terms"] = tokens(row["content"])
                    candidates.append(row)
        if conversation:
            candidates = [row for row in candidates if terms & row.get("_terms", set())]
        rows = retrieve(candidates, effective, actor, limit)
        familiar = (
            sorted(
                (
                    row
                    for row in view.get("memories", [])
                    if row["subject"] == actor and row.get("kind") == "preference" and row.get("importance", 0.5) >= 0.5
                ),
                key=lambda row: (row["importance"], row["updated"]),
                reverse=True,
            )[:2]
            if conversation
            else []
        )
        selected_ids = {row["id"] for row in rows}
        events = sorted(
            view.get("experiences", []),
            key=lambda e: (len(terms & e.get("_terms", set())) * 4 + (actor in e["actors"]), e["created"]),
            reverse=True,
        )[:2]
        relations = relevant_relations(view.get("relations", []), view.get("entities", []), effective, actor)
        intentions = [dict(i) for i in view.get("intentions", [])
                       if (not actor or i["actor"] == actor) and current_thought(i, now)
                       and not blocked(i["topic"], threads)]
        questions = [dict(q) for q in view.get("all_questions", [])
                     if q["actor"] == actor and current_thought(q, now)][:2]
        portrait = dict(view.get("portraits", {}).get(actor, {}))
        character = copy.deepcopy(view.get("character", {}))
        own_interest = character.get("interests", [])[:1]
        self_story = dict(view.get("portraits", {}).get("self", {}))
        if conversation:
            # Remembering nothing is useful: ordinary greetings need no old stories.
            events = [event for event in events if relevant(effective, event.get("_terms", set()), event.get("cues", []))]
            named = {
                e["id"]
                for e in view.get("entities", [])
                if e["id"] != "self" and any(name and name in effective for name in [e["name"], *e.get("aliases", [])])
            }
            relations = [
                r
                for r in relations
                if named & {r["source"], r["target"]}
                or terms & recall_terms(r["label"] + " " + r.get("description", ""))
            ]
            intentions = [i for i in intentions if terms & recall_terms(i["topic"] + " " + i["thought"])]
            questions = [q for q in questions if terms & recall_terms(q["question"])]
            # Familiarity can stay in the background; old stories need a reason to surface.
            for key in ("shared_story", "unfinished", "last_outreach"):
                if not terms & recall_terms(portrait.get(key, "")):
                    portrait.pop(key, None)
            for key in ("impression", "style"):
                if portrait.get(key):
                    if relevant(effective, recall_terms(portrait[key])):
                        portrait[key] = portrait[key][:180]
                    else:
                        portrait.pop(key, None)
            character["habits"] = [item for item in character.get("habits", [])
                                   if relevant(effective, recall_terms(item))]
            for key in ("preferences", "interests"):
                character[key] = [item for item in character.get(key, []) if terms & recall_terms(item)]
            if not character.get("interests") and own_interest and not changed_topic:
                # One brief interest is part of being oneself, not a story to retell.
                character["interests"] = [own_interest[0][:120]]
            if not terms & recall_terms(character.get("outlook", "")):
                character.pop("outlook", None)
            if not terms & recall_terms(self_story.get("impression", "")):
                self_story = {}
            if changed_topic:
                rows, events, relations, intentions, questions = [], [], [], [], []
                for key in ("shared_story", "unfinished", "last_outreach"):
                    portrait.pop(key, None)
                character["interests"] = []
                character["preferences"] = []
                character.pop("outlook", None)
                self_story = {}
        return {
            "question_threads": threads,
            "memories": [{k: v for k, v in row.items() if k != "_terms"} for row in rows],
            "familiar_facts": [row["content"] for row in familiar if row["id"] not in selected_ids],
            "entities": copy.deepcopy(view.get("entities", [])),
            "relations": copy.deepcopy(relations),
            "feeling": next((dict(e) for e in view.get("entities", []) if e["id"] == actor), {}),
            "portrait": portrait,
            "relation_focus": any(
                e["id"] != "self" and e["name"] and e["name"] in query for e in view.get("entities", [])
            ),
            "self_story": self_story,
            "character": character,
            "experiences": [{k: v for k, v in event.items() if k != "_terms"} for event in events],
            "intentions": intentions,
            "curiosity": [] if intentions else questions,
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


def recall_terms(value):
    return tokens(value) - {
        "今天",
        "最近",
        "现在",
        "这个",
        "那个",
        "然后",
        "后来",
        "我们",
        "你们",
        "他们",
        "她们",
        "什么",
        "怎么",
        "么样",
        "一下",
        "一点",
        "可以",
        "事情",
        "时候",
        "你好",
        "早上",
        "晚上",
        "上好",
        "晚好",
        "解释",
    }


def relevant(query, terms, cues=()):
    words = recall_terms(query)
    return len(words & terms) >= 2 or any(cue and cue in query for cue in cues)
