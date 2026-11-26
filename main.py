from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
import weakref
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, At
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import MessageChain, ResultContentType
from astrbot.core.utils.session_lock import session_lock_manager

from .cognition import (
    CONTEXT_FRAME,
    REFLECT_SYSTEM,
    INITIATIVE_SYSTEM,
    living_context,
    choose_initiative,
    actor_key,
    conversation_key,
    digest,
    event_kind,
    number,
    parse_reflection,
    scope_key,
    text,
)
from .interop import COMPANION_CLEANUP, CompanionLink
from .store import Store
from .memory import Memory
from .history import HistoryCompactor
from .followups import POLICY, blocked

NAME = "astrbot_plugin_self_cognition"
TURN = NAME + ".turn"


class ContextPersonaChanged(Exception):
    pass


class ObservedGroup(filter.CustomFilter):
    owner = None

    def filter(self, event, cfg):
        plugin = self.owner() if self.owner else None
        if not plugin:
            return False
        origin = str(event.unified_msg_origin)
        targets = plugin.config.get("observation_origins", [])
        return bool(
            targets
            and ":GroupMessage:" in origin
            and plugin.allowed(origin)
            and event_kind(event) == "dialogue"
            and any(value in targets for value in (origin, conversation_key(origin)))
        )


class SelfCognition(Star):
    """自主认知：长期记忆、相处经验、关系图谱、永久好感与好奇心。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        ObservedGroup.owner = weakref.ref(self)
        self.store = Store(Path(StarTools.get_data_dir(NAME)) / "cognition.sqlite3")
        self.link = CompanionLink(context, config)
        self.closing = False
        self.worker = self.scheduler = self.writer = self.analysis_task = None
        self.memory = Memory(self.store)
        self.history = HistoryCompactor(self)
        self.memory.daily_enabled = lambda scope: bool(
            self.link.peer("daily_routine", self.memory.states.get(scope, {}).get("origin", "")))
        self.memory.daily_context_enabled = lambda scope: bool(
            (peer := self.link.peer("daily_routine", self.memory.states.get(scope, {}).get("origin", "")))
            and peer.config.get("context_enabled", True))
        self.capture_queue = asyncio.Queue()
        self.frontend = {}
        self.initiative_task = None
        self.companions = {}
        self.clearing_companions = set()
        self.companion_generations = {}
        self.initiative_wait = {}
        self.initiative_considered = {}
        self.analysis_independent = False
        self.front_origins = {}
        self.provider_cache = {}
        self.wake = asyncio.Event()
        self.settings_lock = asyncio.Lock()
        self.control_lock = asyncio.Lock()
        self.last_error = ""
        self.schema = json.loads((Path(__file__).parent / "_conf_schema.json").read_text(encoding="utf-8"))
        # Views are optional: older supported AstrBot versions still have schema UI.
        try:
            from astrbot.api import web  # noqa: F401
        except ImportError:
            self.has_views = False
        else:
            self.has_views = True
            for endpoint, method, handler in (
                ("overview", "GET", self.page_overview),
                ("scope", "GET", self.page_scope),
                ("sessions", "GET", self.page_sessions),
                ("settings", "GET", self.page_settings),
                ("settings", "POST", self.save_settings),
                ("forget", "POST", self.page_forget),
                ("control", "POST", self.page_control),
                ("companion_cleanup", "POST", self.page_companion_cleanup),
            ):
                context.register_web_api(f"/{NAME}/{endpoint}", handler, [method], "自主认知 " + endpoint)

    def n(self, key, default, low, high):
        return number(self.config.get(key, default), default, low, high)

    def allowed(self, origin):
        if self.closing or not self.config.get("enabled", True):
            return False
        targets = self.config.get("allowed_origins", [])
        if targets and origin not in targets and conversation_key(origin) not in targets:
            return False
        try:
            allowed = self.context.get_config(umo=origin).get("plugin_set", ["*"])
            return "*" in allowed or NAME in allowed
        except Exception:
            return False

    def identity(self, event, create=True):
        origin = str(event.unified_msg_origin)
        persona = text(event.get_extra("_trace_persona_id"), 300) or "default"
        scope = scope_key(origin, persona)
        sender = str(event.get_sender_id() or "")
        actor = actor_key(origin, sender) if sender else ""
        name = text(event.get_sender_name(), 120) or sender
        state = self.memory.identity(scope, origin, persona) if create else self.memory.states.get(scope)
        return scope, actor, name, state

    def persona_prompt(self, event, request):
        persona = event.get_extra("_trace_persona_id")
        if persona == "[%None]":
            return ""
        if persona == "_chatui_default_":
            from astrbot.core.astr_main_agent_resources import CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT

            return (
                CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT
                if event.get_extra("enable_default_system_prompt") is not False
                else ""
            )
        if persona and persona not in {"default", "_chatui_default_"}:
            value = self.context.persona_manager.get_persona_v3_by_id(persona)
            if value:
                return text(value.get("prompt"), 12000)
        return text(request.system_prompt, 12000)

    async def command_identity(self, event):
        if event.get_extra("_trace_persona_id") is None:
            manager = self.context.conversation_manager
            origin = str(event.unified_msg_origin)
            conversation_id = await manager.get_curr_conversation_id(origin)
            conversation = await manager.get_conversation(origin, conversation_id) if conversation_id else None
            persona_id, _, _, _ = await self.context.persona_manager.resolve_selected_persona(
                umo=origin,
                conversation_persona_id=conversation.persona_id if conversation else None,
                platform_name=event.get_platform_name(),
                provider_settings=self.context.get_config(umo=origin).get("provider_settings", {}),
            )
            event.set_extra("_trace_persona_id", persona_id)
        return self.identity(event)

    def expression_preferences(self):
        value = text(self.config.get("behavior_preferences"), 1000)
        # Older releases supplied an opinionated default. Leave custom preferences intact.
        old_defaults = {
            "像自己一样相处，有自己的喜好、玩心和小脾气；熟悉后自然接梗、惦记共同经历。想问时再问，不用每轮都热情，也不需要反复说我记得。",
            "自然记住共同经历，有自己的偏好和边界；相关时回忆，关心时再问，不把聊天变成盘问。",
        }
        return "" if value in old_defaults else value

    def memory_companions(self, origin):
        # The mood plugin injects its live state itself. Its polled snapshot is
        # stale by definition and must not become a second mood or a lasting trait.
        return {role: value for role, value in self.companions.get(origin, {}).items()
                if role != "short_term_mood" and self.link.peer(role, origin) is not None
                and (conversation_key(origin), role) not in self.clearing_companions}

    async def initialize(self):
        await self.memory.load()
        self.link.connect()
        self.ensure_worker()
        self.scheduler = asyncio.create_task(self.tick(), name="self-cognition-curiosity")

    def ensure_worker(self):
        if self.closing:
            return
        if self.writer is None or self.writer.done():
            self.writer = asyncio.create_task(self.save_observations(), name="self-cognition-save")
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self.run(), name="self-cognition-reflection")
        self.wake.set()

    def enqueue(self, job, activity_only=False):
        if role := job.get("companion_role"):
            key = (conversation_key(job["origin"]), role)
            job = {**job, "companion_generation": self.companion_generations.get(key, 0)}
        self.capture_queue.put_nowait((copy.deepcopy(job), activity_only))
        self.ensure_worker()

    def chatting(self):
        now = time.monotonic()
        self.frontend = {key: until for key, until in self.frontend.items() if until > now}
        self.front_origins = {key: origin for key, origin in self.front_origins.items() if key in self.frontend}
        return bool(self.frontend)

    async def independent_provider(self):
        provider = text(self.config.get("analysis_provider_id"), 200)
        if not provider:
            return False
        for token in list(self.frontend):
            origin = self.front_origins.get(token)
            if not origin:
                return False
            self.provider_cache[origin] = await self.context.get_current_chat_provider_id(origin)
            if self.provider_cache[origin] == provider:
                return False
        return True

    @filter.custom_filter(ObservedGroup, priority=-1000)
    async def observe_group(self, event: AstrMessageEvent):
        origin = str(event.unified_msg_origin)
        targets = self.config.get("observation_origins", [])
        if (
            not targets
            or ":GroupMessage:" not in origin
            or not self.allowed(origin)
            or event_kind(event) != "dialogue"
            or not any(value in targets for value in (origin, conversation_key(origin)))
        ):
            return
        states = [
            s
            for s in self.memory.states.values()
            if conversation_key(s["origin"]) == conversation_key(origin) and not s["paused"]
        ]
        persona_id = event.get_extra("_trace_persona_id")
        if persona_id:
            states = [s for s in states if s["persona"] == persona_id]
        if len(states) != 1:
            return
        human = text(event.get_message_str(), 12000)
        sender = str(event.get_sender_id() or "")
        if not human or not sender or human.startswith("/"):
            return
        state = states[0]
        mid = str(getattr(event.message_obj, "message_id", "") or uuid.uuid4().hex)
        event.set_extra("self_cognition.observation_id", mid)
        self.enqueue(
            {
                "id": digest(state["id"], mid, actor_key(origin, sender)),
                "scope": state["id"],
                "origin": origin,
                "persona_id": state["persona"],
                "persona": state.get("persona_prompt", ""),
                "generation": state["generation"],
                "actor": actor_key(origin, sender),
                "name": text(event.get_sender_name(), 120) or sender,
                "source_kind": "observed",
                "human": human,
                "reply": "",
                "provider_id": "",
                "captured": time.time(),
            }
        )

    async def save_observations(self):
        while True:
            job, activity_only = await self.capture_queue.get()
            try:
                if (role := job.get("companion_role")) and job["companion_generation"] != self.companion_generations.get(
                    (conversation_key(job["origin"]), role), 0
                ):
                    continue
                await self.memory.call(self.store.capture, job, activity_only, int(self.n("queue_limit", 256, 8, 4096)))
                state = await self.memory.call(self.store.scope, job["scope"])
                if state:
                    self.memory.states[job["scope"]] = state
                if not activity_only:
                    view = self.memory.views.get(job["scope"], {})
                    view["all_questions"] = [
                        q for q in view.get("all_questions", []) if q["question"] not in job["reply"]
                    ]
                self.wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = "这段相处暂时没能记下来：" + type(error).__name__
                logger.warning("Self-cognition capture unavailable (%s)", type(error).__name__)
            finally:
                self.capture_queue.task_done()

    @filter.on_llm_request(priority=-50)
    async def prepare(self, event: AstrMessageEvent, request: ProviderRequest):
        origin = str(event.unified_msg_origin)
        if not self.allowed(origin) or event.get_extra("self_cognition_skip"):
            return
        # Internal reports share the same real conversation history. They still
        # need cleanup, but must never enter the human cognition pipeline.
        if not event.get_extra(NAME + ".history_prepared"):
            event.set_extra(NAME + ".history_prepared", True)
            scope, _, _, state = self.identity(event, create=False)
            if not state or not state["paused"]:
                try:
                    await self.history.prepare(event, request, scope, state)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning("Self-cognition history cleanup deferred (%s); original history retained",
                                   type(error).__name__)
        # Task reports and autonomous messages are not new human experiences.
        if event_kind(event) != "dialogue":
            return
        if not any(str(getattr(p, "text", "")).startswith("<question_policy>")
                   for p in (request.extra_user_content_parts or [])):
            if request.extra_user_content_parts is None:
                request.extra_user_content_parts = []
            request.extra_user_content_parts.append(TextPart(text="<question_policy>\n" + POLICY
                                                            + "\n</question_policy>").mark_as_temp())
        existing = event.get_extra(TURN)
        if isinstance(existing, dict):
            return
        scope, actor, name, state = self.identity(event)
        if state["paused"] or not actor:
            return
        human = text(event.get_message_str(), 12000)
        if not human:
            return
        self.link.begin(event)
        mid = str(
            getattr(event.message_obj, "message_id", "")
            or event.get_extra("self_cognition.observation_id")
            or uuid.uuid4().hex
        )
        job = {
            "id": digest(scope, mid, actor),
            "scope": scope,
            "generation": state["generation"],
            "actor": actor,
            "name": name,
            "origin": origin,
            "source_kind": "dialogue",
            "persona_id": state["persona"],
            "human": human,
            "reply": "",
            "persona": self.persona_prompt(event, request),
            "provider_id": text(self.config.get("analysis_provider_id"), 200),
            "captured": time.time(),
            "mentioned_users": [
                {"id": actor_key(origin, str(part.qq)), "name": text(part.name, 120) or str(part.qq)}
                for part in getattr(event, "get_messages", lambda: [])()
                if isinstance(part, At) and str(part.qq) not in {"all", "0"}
            ],
        }
        self.frontend[job["id"]] = time.monotonic() + 600
        self.front_origins[job["id"]] = origin
        for task in (self.analysis_task, self.initiative_task):
            if task is not None and not task.done():
                if (
                    task is self.initiative_task
                    or not self.analysis_independent
                    or self.provider_cache.get(origin) in {None, text(self.config.get("analysis_provider_id"), 200)}
                ):
                    task.cancel()
        state["last_human"] = job["captured"]
        self.initiative_considered.pop(scope, None)
        self.enqueue(job, activity_only=True)
        recalled = self.memory.recall(scope, actor, human, int(self.n("recall_limit", 6, 1, 30)), conversation=True)
        states = recalled.get("question_threads", [])[:20]
        if states:
            request.extra_user_content_parts.append(TextPart(
                text="<question_threads>\n" + json.dumps(states, ensure_ascii=False) + "\n</question_threads>"
            ).mark_as_temp())
        job["questions"] = recalled["curiosity"]
        event.set_extra(TURN, job)
        if not self.config.get("curiosity_enabled", True):
            recalled["curiosity"] = []
            recalled["intentions"] = [idea for idea in recalled["intentions"] if idea["kind"] != "ask"]
        lines = living_context(recalled, name, self.expression_preferences(), self.memory_companions(origin))
        existing_text = "\n".join(str(getattr(p, "text", "")) for p in (request.extra_user_content_parts or []))
        lines = [line for line in lines if not (line.startswith("以前记下：") and line[5:] in existing_text)]
        budget = int(self.n("context_char_limit", 4500, 1000, 30000))
        while lines and len("\n".join(lines)) > budget:
            lines.pop()
        if not lines:
            return
        parts = getattr(request, "extra_user_content_parts", None)
        if parts is None:
            parts = request.extra_user_content_parts = []
        if not any(str(getattr(p, "text", "")).startswith("<self_cognition>") for p in parts):
            parts.append(TextPart(text=CONTEXT_FRAME.format(payload="\n".join(lines))).mark_as_temp())

    @filter.on_llm_response(priority=-100)
    async def response(self, event: AstrMessageEvent, response):
        job = event.get_extra(TURN)
        if not isinstance(job, dict) or getattr(response, "tools_call_args", None):
            return
        if getattr(response, "role", "assistant") == "assistant":
            job["reply"] = text(getattr(response, "completion_text", ""), 12000)
        self.link.end(event)
        self.frontend.pop(job["id"], None)
        self.wake.set()

    @filter.on_agent_done(priority=-100)
    async def agent_done(self, event: AstrMessageEvent, run_context, response):
        await self.response(event, response)
        self.link.end(event)
        job = event.get_extra(TURN)
        if isinstance(job, dict):
            self.frontend.pop(job["id"], None)
        result = getattr(event, "get_result", lambda: None)()
        if result and result.result_content_type == ResultContentType.STREAMING_RESULT:
            # AstrBot 4.29's streaming respond path returns before after_message_sent.
            # The completed agent response is the available commit boundary there.
            await self.delivered(event)

    @filter.after_message_sent()
    async def delivered(self, event: AstrMessageEvent):
        job = event.get_extra(TURN)
        if not isinstance(job, dict) or job.get("stored") or not job.get("reply"):
            return
        if not self.allowed(job["origin"]):
            return
        job["stored"] = True
        self.enqueue(job)

    def observe_snapshots(self, state, snapshots, persona=""):
        # Snapshot evidence is explicitly attributed to a plugin, never the human.
        for role in ("play_together", "self_amusement", "codex_bridge"):
            if self.link.connected(role) or (conversation_key(state["origin"]), role) in self.clearing_companions:
                continue
            data = snapshots.get(role, {})
            if role == "play_together":
                data = {"last_game": data.get("last_game")} if data.get("last_game") else {}
            elif role == "self_amusement":
                data = (
                    {"recent_own_messages": data.get("recent_own_messages")} if data.get("recent_own_messages") else {}
                )
            # Codex status is transient: let chat use it, do not memorize job internals.
            elif role == "codex_bridge":
                continue
            if not data:
                continue
            evidence = json.dumps({"source": role, "snapshot": data}, ensure_ascii=False)[:12000]
            self.enqueue(
                {
                    "id": digest(state["id"], role, evidence),
                    "scope": state["id"],
                    "generation": state["generation"],
                    "origin": state["origin"],
                    "actor": "self",
                    "name": "我",
                    "human": evidence,
                    "reply": "",
                    "persona": persona or state.get("persona_prompt", ""),
                    "source_kind": "companion",
                    "companion_role": role,
                    "persona_id": state["persona"],
                    "provider_id": text(self.config.get("analysis_provider_id"), 200),
                    "captured": time.time(),
                    "questions": [],
                }
            )

    def companion_observe(self, packet):
        source, origin, kind = packet.get("source"), packet.get("origin", ""), packet.get("kind")
        if (
            source not in {"play_together", "self_amusement", "daily_routine"}
            or not self.allowed(origin)
            or not self.config.get("interop_enabled", True)
            or not self.config.get("link_" + source, True)
            or kind not in {"game_turn", "leisure_experience", "initiative", "task_result", "work_report"}
        ):
            return
        if self.link.peer(source, origin) is None:
            return
        if (conversation_key(origin), source) in self.clearing_companions:
            return
        states = [
            s
            for s in self.memory.states.values()
            if conversation_key(s["origin"]) == conversation_key(origin) and not s["paused"]
        ]
        data = packet.get("data", {})
        if len(states) > 1:
            states = [s for s in states if data.get("persona") and s.get("persona_prompt") == data["persona"]]
        if len(states) != 1:
            return
        state = states[0]
        details = {
            key: value
            for key, value in data.items()
            if key
            in {
                "game",
                "action",
                "outcome",
                "human_text",
                "reply",
                "idea",
                "private_note",
                "materials",
                "text",
                "summary",
            }
        }
        if not details:
            return
        evidence = json.dumps({"source": source, "kind": kind, "experience": details}, ensure_ascii=False)[:16000]
        self.enqueue(
            {
                "id": digest(state["id"], source, packet.get("id") or evidence),
                "scope": state["id"],
                "origin": origin,
                "generation": state["generation"],
                "persona_id": state["persona"],
                "persona": state.get("persona_prompt", ""),
                "actor": "self",
                "name": "我",
                "human": evidence,
                "reply": "",
                "source_kind": "companion",
                "companion_role": source,
                "companion_kind": kind,
                "own_utterance": text(data.get("text"), 4000) if kind == "initiative" else "",
                "provider_id": "",
                "captured": time.time(),
            }
        )

    async def process(self, job):
        state = await self.memory.call(self.store.scope, job["scope"])
        if not state or not self.allowed(job["origin"]) or state["paused"] or state["generation"] != job["generation"]:
            return False
        if job.get("companion_kind") == "initiative":
            # Own speech proves only that we said it, not that the recipient promised it.
            utterances = [turn.get("own_utterance", "") for turn in job.get("turns", [job])]
            story = "\n".join("我曾主动说过（对方是否认可未知）：" + value for value in utterances if value)
            result = parse_reflection("{}", "", companion=True)
            result["experience"] = {"title": "我主动说过的话", "story": story[:1800]}
            applied = await self.memory.call(self.store.apply, job, result)
            if applied:
                await self.memory.refresh(job["scope"])
            return applied
        recalled = self.memory.recall(job["scope"], job["actor"], job["human"], 12)
        payload = {
            key: job.get(key) for key in ("human", "reply", "turns", "persona", "source_kind", "name", "captured")
        }
        persona = state["persona"]
        if persona not in {"default", "_chatui_default_", "[%None]"}:
            current = self.context.persona_manager.get_persona_v3_by_id(persona)
            payload["persona"] = text(current.get("prompt"), 12000) if current else ""
        else:
            payload["persona"] = state.get("persona_prompt", job.get("persona", ""))
        prompt_used = payload["persona"]
        payload["known"] = recalled
        # Reflection must see closed topics too; selective recall is for answers,
        # not permission to rename a previously dropped question.
        payload["known"]["question_threads"] = self.memory.views.get(job["scope"], {}).get("question_threads", [])
        payload["mentioned_users"] = job.get("mentioned_users", [])
        payload["speakers"] = job.get("speakers", [{"id": job["actor"], "name": job["name"]}])
        payload["people_known"] = {
            speaker["id"]: self.memory.recall(job["scope"], speaker["id"], "", 3)["portrait"]
            for speaker in payload["speakers"]
        }
        payload["now"] = datetime.now(ZoneInfo(str(self.config.get("timezone", "Asia/Shanghai")))).isoformat()
        payload["companion"] = self.memory_companions(job["origin"])
        if catalog := self.link.sticker_catalog(job["origin"]):
            payload["sticker_library"] = {
                "count": len(catalog["items"]),
                "examples": catalog["items"][:24],
            }
        self.provider_cache[job["origin"]] = await self.context.get_current_chat_provider_id(job["origin"])
        provider = text(self.config.get("analysis_provider_id"), 200) or self.provider_cache[job["origin"]]
        async with asyncio.timeout(self.n("analysis_timeout_seconds", 60, 5, 180)):
            response = await self.context.llm_generate(
                chat_provider_id=provider, system_prompt=REFLECT_SYSTEM, prompt=json.dumps(payload, ensure_ascii=False)
            )
        if not self.allowed(job["origin"]):
            return False
        if persona not in {"default", "_chatui_default_", "[%None]"}:
            current = self.context.persona_manager.get_persona_v3_by_id(persona)
            if (text(current.get("prompt"), 12000) if current else "") != prompt_used:
                return False
        result = parse_reflection(
            response.completion_text,
            job["human"],
            companion=job["source_kind"] == "companion",
            known_entities=recalled["entities"] + job.get("mentioned_users", []) + payload["speakers"],
            reply=job["reply"],
        )
        if not self.config.get("curiosity_enabled", True):
            result["curiosity"] = []
            result["intentions"] = [idea for idea in result.get("intentions", []) if idea["kind"] != "ask"]
            for person in result.get("people", []):
                person["curiosity"] = []
        applied = await self.memory.call(self.store.apply, job, result)
        if applied:
            await self.memory.refresh(job["scope"])
        return applied

    async def run(self):
        while not self.closing:
            self.wake.clear()
            eligible = [s["id"] for s in self.memory.states.values() if not s["paused"] and self.allowed(s["origin"])]
            job = None
            independent = await self.independent_provider() if self.chatting() else False
            if not self.chatting() or independent:
                job = await self.memory.call(
                    self.store.next_batch,
                    time.time(),
                    eligible,
                    self.n("reflection_idle_seconds", 45, 0, 600),
                    int(self.n("reflection_batch_turns", 6, 1, 30)),
                    self.n("reflection_max_wait_minutes", 10, 1, 60) * 60,
                )
            if job and (not self.chatting() or independent):
                try:
                    self.analysis_independent = independent
                    self.analysis_task = asyncio.create_task(self.process(job))
                    if await self.analysis_task:
                        self.last_error = ""
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    # A new real chat takes priority; the batch stays on disk.
                    await self.memory.refresh(job["scope"])
                except Exception as error:
                    self.last_error = "这段经历还没整理好：" + type(error).__name__
                    await self.memory.call(self.store.failed, job, time.time())
                    logger.warning("Self-cognition reflection failed (%s)", type(error).__name__)
                finally:
                    self.analysis_task = None
                    self.analysis_independent = False
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=5)
            except TimeoutError:
                pass

    def in_quiet_hours(self):
        try:
            zone = ZoneInfo(str(self.config.get("timezone", "Asia/Shanghai")))
        except (ZoneInfoNotFoundError, ValueError):
            return True  # Invalid timezone cannot unexpectedly trigger outbound messages.
        hour = datetime.now(zone).hour
        start = int(self.n("quiet_start", 23, 0, 23))
        end = int(self.n("quiet_end", 8, 0, 23))
        return (start <= hour < end) if start < end else (hour >= start or hour < end) if start != end else False

    async def curiosity_step(self, state):
        origin = state["origin"]
        if (
            not self.allowed(origin)
            or state["paused"]
            or state["no_contact"]
            or not self.config.get("proactive_enabled", False)
            or origin not in self.config.get("proactive_origins", [])
            or self.in_quiet_hours()
        ):
            return
        now = time.time()
        routine = self.link.peer("daily_routine", origin)
        if routine and not routine.companion_allow_initiative(origin, "self_cognition"):
            return
        if self.initiative_wait.get(state["id"], 0) > now:
            return
        if (
            not state["last_human"]
            or now - max(state["last_human"], state["last_sent"]) < self.n("proactive_gap_hours", 24, 1, 720) * 3600
        ):
            return
        if now - state["last_human"] > self.n("proactive_max_idle_days", 7, 1, 365) * 86400:
            return
        considered = self.initiative_considered.setdefault(state["id"], set())
        pending = {
            i["id"] for i in self.memory.views.get(state["id"], {}).get("intentions", []) if i["status"] == "pending"
        }
        considered.intersection_update(pending)
        if pending and considered == pending:
            considered.clear()
        question = await self.memory.call(
            self.store.initiative, state["id"], now, self.config.get("curiosity_enabled", True), tuple(considered)
        )
        if not question:
            return
        choices = [
            dict(idea, choice=("intention:" if idea.get("intent_id") else "question:") + str(idea["id"]))
            for idea in question.get("candidates", [question])
        ]
        # A shared group cannot identify the intended person without a platform mention.
        if ":GroupMessage:" in origin:
            return
        snapshots = self.companions.get(origin, {})
        if snapshots.get("self_amusement", {}).get("wants_space") or snapshots.get("play_together", {}).get(
            "current_game"
        ):
            return
        token = self.link.claim(self.n("interop_gap_minutes", 10, 1, 1440) * 60)
        if not token:
            return
        try:
            self.initiative_wait[state["id"]] = now + 3600
            provider = text(
                self.config.get("analysis_provider_id"), 200
            ) or await self.context.get_current_chat_provider_id(origin)
            recalled = self.memory.recall(
                state["id"], question["actor"], " ".join(idea["question"] for idea in choices), 5
            )
            prompt_now = state.get("persona_prompt", "")
            persona_id = state["persona"]
            if persona_id not in {"default", "_chatui_default_", "[%None]"}:
                current = self.context.persona_manager.get_persona_v3_by_id(persona_id)
                prompt_now = text(current.get("prompt"), 12000) if current else ""
            recalled["intentions"] = [idea for idea in recalled["intentions"] if idea["status"] != "pending"]
            if not self.config.get("curiosity_enabled", True):
                recalled["curiosity"] = []
                recalled["intentions"] = [idea for idea in recalled["intentions"] if idea["kind"] != "ask"]
            async with asyncio.timeout(self.n("analysis_timeout_seconds", 60, 5, 180)):
                self.initiative_task = asyncio.create_task(
                    self.context.llm_generate(
                        chat_provider_id=provider,
                        system_prompt=INITIATIVE_SYSTEM,
                        prompt=json.dumps(
                            {
                                "persona": prompt_now,
                                "memory": recalled,
                                "choices": choices,
                                "now": datetime.now(
                                    ZoneInfo(str(self.config.get("timezone", "Asia/Shanghai")))
                                ).isoformat(),
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                reply = await self.initiative_task
            question, message = choose_initiative(reply.completion_text, choices)
            if not question or not message or message.strip().upper() == "WAIT":
                considered.update(idea["intent_id"] for idea in choices if idea.get("intent_id"))
                self.initiative_wait[state["id"]] = now + 3600
                return
            current = await self.memory.call(self.store.scope, state["id"])
            if persona_id not in {"default", "_chatui_default_", "[%None]"}:
                persona = self.context.persona_manager.get_persona_v3_by_id(persona_id)
                if (text(persona.get("prompt"), 12000) if persona else "") != prompt_now:
                    return
            if (
                not current
                or current["no_contact"]
                or current["generation"] != state["generation"]
                or not self.link.current(token)
                or not self.allowed(origin)
                or not self.config.get("proactive_enabled", False)
                or origin not in self.config.get("proactive_origins", [])
                or self.in_quiet_hours()
            ):
                return
            threads = await self.memory.call(self.store.question_threads, state["id"])
            if blocked(question.get("topic", question["question"]), threads):
                return
            async with asyncio.timeout(30):
                sent = await self.context.send_message(origin, MessageChain(chain=[Plain(message)]))
            if sent:
                if question.get("intent_id"):
                    await self.memory.call(self.store.mark_intention, state["id"], question["intent_id"], message, now)
                else:
                    await self.memory.call(
                        self.store.mark_question, state["id"], question["id"], now, proactive=True, message=message
                    )
                await self.memory.refresh(state["id"])
                self.link.sent(origin, message, event_id=f"cognition:{state['id']}:{question['id']}:{now}")
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        finally:
            self.initiative_task = None
            self.link.release(token)

    async def tick(self):
        while not self.closing:
            try:
                now = time.time()
                self.link.connect()
                await self.memory.call(self.store.prune, int(self.n("raw_retention_days", 30, 0, 3650)), now)
                for state in list(self.memory.states.values()):
                    if self.allowed(state["origin"]) and not state["paused"]:
                        await self.sync_daily_memory(state)
                        snapshots = self.link.snapshots(state["origin"])
                        self.companions[state["origin"]] = snapshots
                        self.observe_snapshots(state, snapshots)
                        await self.curiosity_step(state)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Self-cognition scheduler unavailable (%s)", type(error).__name__)
            await asyncio.sleep(15)

    async def sync_daily_memory(self, state):
        if (conversation_key(state["origin"]), "daily_routine") in self.clearing_companions:
            return
        peer = self.link.peer("daily_routine", state["origin"])
        read = getattr(peer, "companion_memory", None)
        if not read:
            return
        view = self.memory.views.get(state["id"], {})
        for entity in view.get("entities", []):
            if entity.get("kind") != "user":
                continue
            actor = entity["id"]
            try:
                data = read(state["origin"], persona=state["persona"], actor=actor)
            except Exception:
                continue
            if not isinstance(data, dict) or data.get("version") != 1:
                continue
            if (data.get("persona") != state["persona"] or data.get("actor") != actor
                    or data.get("origin") != conversation_key(state["origin"])):
                continue
            if view.get("daily_memory", {}).get(actor, {}).get("source_revision") == digest(data):
                continue
            if await self.memory.call(self.store.replace_daily, state["id"], actor, data, state["generation"]):
                await self.memory.refresh(state["id"])

    def companion_memory(self, origin, *, persona, actor):
        if not self.allowed(origin) or not self.link.peer("daily_routine", origin):
            return {}
        scopes = [s for s in self.memory.states.values() if conversation_key(s["origin"]) == conversation_key(origin)
                  and s["persona"] == persona and not s["paused"]]
        if len(scopes) != 1:
            return {}
        view = self.memory.views.get(scopes[0]["id"], {})
        records = [{"key": str(r["id"]), "text": r["content"], "evidence": r["evidence"], "updated": r["updated"]}
                   for r in view.get("memories", []) if r["subject"] == actor and r["kind"] in {"fact", "preference"}]
        return {"version": 1, "origin": conversation_key(origin), "persona": persona, "actor": actor,
                "records": records}

    def companion_snapshot(self, origin="", persona=None):
        # No origin means no cross-conversation memory dump. New peers can opt in.
        if not origin or not self.allowed(origin) or not self.config.get("interop_enabled", True):
            return {}
        scopes = [
            s
            for s in self.memory.states.values()
            if conversation_key(s["origin"]) == conversation_key(origin) and not s["paused"]
            and (persona is None or s["persona"] == persona)
        ]
        if len(scopes) != 1:
            return {}  # Ambiguous persona: do not mix identities for old protocol callers.
        recalled = self.memory.recall(scopes[0]["id"], limit=3)
        portraits = self.memory.views.get(scopes[0]["id"], {}).get("portraits", {})
        spoken = list(
            dict.fromkeys(
                [i["last_message"] for i in recalled["intentions"] if i.get("last_message")]
                + [p["last_outreach"] for p in portraits.values() if p.get("last_outreach")]
            )
        )[:3]
        names = {e["id"]: e["name"] for e in self.memory.views.get(scopes[0]["id"], {}).get("entities", [])}
        character = {
            key: (value[:300] if isinstance(value, str) else value[:3]) for key, value in recalled["character"].items()
        }
        return {
            "question_policy": POLICY,
            "question_threads": recalled.get("question_threads", [])[:20],
            "guidance": "这是长期相处背景，相关时才取用，不需要找旧话题续聊；历史感受不代表当前心情，当前心境以短期情绪插件状态为准。",
            "character": character,
            "recent_own_messages": [message[:500] for message in spoken]
            if scopes[0]["last_sent"] and time.time() - scopes[0]["last_sent"] <= 86400 else [],
            "people": {
                names.get(key, key): {field: value.get(field, "")[:300] for field in ("impression", "style")}
                for key, value in list(portraits.items())[-5:]
                if key != "self"
            },
            "remembered": [row["content"][:300] for row in recalled["memories"]
                           if row["kind"] in {"fact", "preference", "lesson", "self"} and row["importance"] >= 0.5],
            "experiences": [],  # No query: do not choose a past episode for another plugin's current chat.
            "self_reflection": recalled["self_story"].get("impression", "")[:400],
            "relationships": [line[5:][:350] for line in living_context(recalled, "") if line.startswith("人物之间：")][
                :5
            ],
            "open_threads": [],  # Autonomous thoughts stay with this plugin's own initiative selection.
        }

    @filter.command("cognition", alias={"自主认知"})
    async def status(self, event: AstrMessageEvent):
        scope, actor, _, state = await self.command_identity(event)
        if not self.allowed(event.unified_msg_origin):
            yield event.plain_result("自主认知未在此会话启用。")
            return
        recalled = self.memory.recall(scope, actor)
        stats = await self.memory.call(self.store.stats, scope)
        yield event.plain_result(
            f"自主认知 · {'暂停' if state['paused'] else '运行中'}\n"
            f"{recalled['portrait'].get('impression') or '还在慢慢熟悉，一起经历的事会记在心里。'}\n"
            f"记忆 {stats['memories']} · 关系 {stats['relations']} · 待整理 {stats['pending']} · 失败 {stats['failed']}\n"
            f"会话 UMO：{event.unified_msg_origin}\n使用 /cognition_recall 关键词 查看回忆；管理员在插件配置或认知面板管理。"
        )

    @filter.command("cognition_recall")
    async def recall(self, event: AstrMessageEvent, query: str = ""):
        if not self.allowed(event.unified_msg_origin):
            return
        scope, actor, _, state = await self.command_identity(event)
        if state["paused"]:
            yield event.plain_result("此会话的自主认知已暂停。")
            return
        rows = self.memory.recall(scope, actor, query, 6)["memories"]
        yield event.plain_result(
            "\n".join(f"#{r['id']} [{r['kind']}] {r['content']}（依据：{r['evidence']}）" for r in rows)
            or "暂时没有相关回忆。"
        )

    @filter.command("cognition_control")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def control(self, event: AstrMessageEvent, action: str = ""):
        scope, _, _, _ = await self.command_identity(event)
        if action not in {"pause", "resume", "retry", "clear_confirm"}:
            yield event.plain_result(
                "/cognition_control pause|resume|retry|clear_confirm（最后一项删除本会话人格的全部认知）"
            )
            return
        await self.do_control(scope, action)
        yield event.plain_result("自主认知操作已完成：" + action)

    @filter.command("cognition_contact")
    async def contact(self, event: AstrMessageEvent, mode: str = "off"):
        if ":GroupMessage:" in str(event.unified_msg_origin) or mode not in {"on", "off"}:
            yield event.plain_result("私聊中使用 /cognition_contact off 关闭主动提问，on 恢复（仍需管理员配置允许）。")
            return
        scope, _, _, _ = await self.command_identity(event)
        await self.memory.call(
            self.store.ensure_scope, scope, event.unified_msg_origin, self.memory.states[scope]["persona"]
        )
        await self.memory.call(self.store.contact, scope, mode == "on")
        await self.memory.refresh(scope)
        yield event.plain_result("此会话的主动提问已" + ("允许。" if mode == "on" else "关闭。"))

    async def do_control(self, scope, action):
        async with self.control_lock:
            state = await self.memory.call(self.store.scope, scope)
            if not state and (state := self.memory.states.get(scope)):
                state = await self.memory.call(self.store.ensure_scope, scope, state["origin"], state["persona"])
            if not state:
                raise ValueError("unknown scope")
            if action in {"clear_context", "clear_records"}:
                await self.clear_chat_data(state, action)
                return
            if action in {"pause", "resume"}:
                await self.memory.call(self.store.pause, scope, action == "pause")
            elif action == "clear_confirm":
                await self.memory.call(self.store.clear, scope)
                self.history.reset(scope)
            elif action == "clear_memory":
                await self.memory.call(self.store.clear_memory, scope)
            elif action == "retry":
                await self.memory.call(self.store.retry_failed, scope)
                self.ensure_worker()
            elif action in {"contact_on", "contact_off"}:
                await self.memory.call(self.store.contact, scope, action == "contact_on")
            else:
                raise ValueError("unknown action")
            if action in {"clear_memory", "clear_confirm"}:
                self.reset_focus(scope)
                self.initiative_wait.pop(scope, None)
                self.initiative_considered.pop(scope, None)
            await self.memory.refresh(scope)

    def reset_focus(self, scope):
        for key in list(self.memory.focus):
            if key[0] == scope:
                self.memory.focus.pop(key)

    async def clear_chat_data(self, state, action):
        scope, origin = state["id"], state["origin"]
        # Serialize with AstrBot's normal built-in agent history writeback.
        async with session_lock_manager.acquire_lock(origin):
            if action == "clear_context":
                await self.clear_current_conversation(state)
                if ":GroupMessage:" in origin:
                    builtin = self.context.get_registered_star("astrbot")
                    group_context = getattr(getattr(builtin, "star_cls", None), "group_chat_context", None)
                    if group_context:
                        await group_context.remove_session(SimpleNamespace(unified_msg_origin=origin))
                await self.memory.call(self.store.clear_context, scope)
            else:
                await self.clear_platform_records(origin)
                await self.memory.call(self.store.clear_records, scope)
            self.reset_focus(scope)
            self.history.reset(scope)
            await self.memory.refresh(scope)

    async def clear_current_conversation(self, state):
        origin = state["origin"]
        manager = self.context.conversation_manager
        cid = await manager.get_curr_conversation_id(origin)
        conversation = await manager.get_conversation(origin, cid) if cid else None
        if not conversation:
            return
        if conversation.user_id != origin:
            raise ContextPersonaChanged("AstrBot 当前对话和所选会话不一致。")
        platform_id = origin.split(":", 1)[0]
        platform = self.context.get_platform_inst(platform_id)
        persona_id, _, _, _ = await self.context.persona_manager.resolve_selected_persona(
            umo=origin,
            conversation_persona_id=conversation.persona_id,
            platform_name=platform.meta().name if platform else platform_id,
            provider_settings=self.context.get_config(umo=origin).get("provider_settings", {}),
        )
        if (text(persona_id, 300) or "default") != state["persona"]:
            raise ContextPersonaChanged("AstrBot 当前使用了其他人格，请在面板选择正在使用的人格后重试。")
        await manager.update_conversation(origin, cid, history=[])

    async def clear_platform_records(self, origin):
        platform_id, _, session_id = origin.split(":", 2)
        user_id = origin  # AstrBot stores external group messages with the full UMO.
        if platform_id == "webchat":
            creator, separator, user_id = session_id.removeprefix("webchat!").rpartition("!")
            if not separator:
                raise ValueError("invalid WebChat session")
            db = self.context.get_db()
            session = await db.get_platform_session_by_id(user_id)
            if not session or session.creator != creator or session.platform_id != platform_id:
                thread = await db.get_webchat_thread_by_id(user_id)
                if not thread or thread.creator != creator:
                    return
                platform_id = "webchat_thread"
        # The native SQLite API deletes rows newer than this many seconds ago.
        await self.context.message_history_manager.delete(
            platform_id=platform_id, user_id=user_id, offset_sec=int(time.time()) + 86400,
        )

    async def page_overview(self):
        from astrbot.api.web import error_response, json_response, request
        from astrbot.core.umo_alias import serialize_umo_alias

        if not request.username:
            return error_response("需要登录 AstrBot 管理面板", status_code=401)
        scopes = await self.memory.call(self.store.overview)
        if scopes:
            origins = list({scope["origin"] for scope in scopes})
            try:
                aliases = {alias.umo: alias for alias in await self.context.get_db().get_umo_aliases(origins)}
            except Exception:
                logger.exception("自主认知读取会话名称失败")
            else:
                for scope in scopes:
                    origin = scope["origin"]
                    scope["display_name"] = serialize_umo_alias(aliases.get(origin), origin)["display_name"]
        return json_response({"scopes": scopes, "last_error": self.last_error})

    async def page_scope(self):
        from astrbot.api.web import error_response, json_response, request

        if not request.username:
            return error_response("需要登录", status_code=401)
        scope = request.query.get("scope", "")
        state = await self.memory.call(self.store.scope, scope)
        if not state:
            return error_response("会话不存在", status_code=404)
        data = await self.memory.call(self.store.dashboard, scope, request.query.get("query", ""))
        data["companion_cleanup_origin"] = state["origin"]
        data["companion_cleanup"] = [
            {"id": role, "label": label, "description": description}
            for role, (label, description) in COMPANION_CLEANUP.items()
            if callable(getattr(self.link.peer(role, state["origin"]), "companion_clear", None))
        ]
        return json_response(data)

    async def page_companion_cleanup(self):
        from astrbot.api.web import error_response, json_response, request

        if not request.username:
            return error_response("需要登录", status_code=401)
        payload = await request.json(default={})
        if not isinstance(payload, dict) or not isinstance(payload.get("scope"), str):
            return error_response("请求无效")
        roles = payload.get("plugins")
        if not isinstance(roles, list) or not roles or len(roles) > len(COMPANION_CLEANUP) or any(
            not isinstance(role, str) or role not in COMPANION_CLEANUP for role in roles
        ):
            return error_response("联动插件选择无效")
        async with self.control_lock:
            state = await self.memory.call(self.store.scope, payload["scope"])
            if not state:
                return error_response("会话不存在", status_code=404)
            if payload.get("origin") != state["origin"]:
                return error_response("会话来源已变化，请刷新面板后重试。", status_code=409)
            results = []
            for role in dict.fromkeys(roles):
                status = await self.clear_companion(state, role)
                results.append({"id": role, "label": COMPANION_CLEANUP[role][0], "status": status})
        return json_response({"results": results})

    async def clear_companion(self, state, role):
        origin, scope = state["origin"], state["id"]
        clear = getattr(self.link.peer(role, origin), "companion_clear", None)
        if not callable(clear):
            return "unavailable"
        key = (conversation_key(origin), role)
        self.clearing_companions.add(key)
        try:
            status = await clear(origin, persona=state["persona"])
            if status != "cleared":
                return status if status in ("busy", "unavailable") else "failed"
            self.companion_generations[key] = self.companion_generations.get(key, 0) + 1
            for umo, snapshots in self.companions.items():
                if conversation_key(umo) == key[0]:
                    snapshots.pop(role, None)
            scopes = {scope, *(item["id"] for item in self.memory.states.values()
                               if conversation_key(item["origin"]) == key[0])}
            if role == "daily_routine":
                await self.memory.call(self.store.clear_companion_cache, [scope], role)
                await self.memory.refresh(scope)
            elif role in {"play_together", "self_amusement"}:
                await self.memory.call(self.store.clear_companion_cache, scopes, role)
            return "cleared"
        except Exception:
            logger.exception("联动清理失败 (%s)", role)
            return "failed"
        finally:
            self.clearing_companions.discard(key)

    async def page_settings(self):
        from astrbot.api.web import error_response, json_response, request

        if not request.username:
            return error_response("需要登录", status_code=401)
        return json_response(
            {"schema": self.schema, "values": {k: self.config.get(k, v.get("default")) for k, v in self.schema.items()}}
        )

    async def page_sessions(self):
        from astrbot.api.web import error_response, json_response, request

        if not request.username:
            return error_response("需要登录", status_code=401)
        try:
            from astrbot.dashboard.services.session_management_service import SessionManagementService

            # Use the native UMO picker's catalog. Listing needs only the database.
            service = SessionManagementService(core_lifecycle=None, db_helper=self.context.get_db())
            sessions = (await service.list_active_umos())["umo_infos"]
        except Exception:
            logger.exception("自主认知读取 AstrBot 会话列表失败")
            return error_response("读取会话失败，请重试或在 AstrBot 插件配置中选择会话。", status_code=503)
        return json_response({"sessions": sessions})

    async def save_settings(self):
        from astrbot.api.web import error_response, json_response, request

        if not request.username:
            return error_response("需要登录", status_code=401)
        values = await request.json(default={})
        try:
            self.validate_settings(values)
        except ValueError as error:
            return error_response(str(error))
        async with self.settings_lock:
            previous = dict(self.config)
            try:
                self.config.update(values)
                await asyncio.to_thread(self.config.save_config)
            except Exception:
                self.config.clear()
                self.config.update(previous)
                return error_response("配置保存失败", status_code=500)
        self.wake.set()
        return json_response({"saved": True})

    def validate_settings(self, values):
        if not isinstance(values, dict) or any(k not in self.schema for k in values):
            raise ValueError("配置字段无效")
        for key, value in values.items():
            spec = self.schema[key]
            kind = spec["type"]
            valid = (
                (kind == "bool" and isinstance(value, bool))
                or (kind in {"string", "text"} and isinstance(value, str) and len(value) <= 12000)
                or (kind == "int" and type(value) is int)
                or (kind == "float" and type(value) in {int, float})
                or (
                    kind == "list"
                    and isinstance(value, list)
                    and len(value) <= 200
                    and all(isinstance(v, str) and len(v) < 1000 for v in value)
                )
            )
            if not valid:
                raise ValueError("配置类型无效：" + key)
            if "slider" in spec and not spec["slider"]["min"] <= value <= spec["slider"]["max"]:
                raise ValueError("配置超出范围：" + key)
            if key == "timezone":
                try:
                    ZoneInfo(value)
                except (ZoneInfoNotFoundError, ValueError) as error:
                    raise ValueError("时区无效") from error

    async def page_forget(self):
        from astrbot.api.web import error_response, json_response, request

        if not request.username:
            return error_response("需要登录", status_code=401)
        payload = await request.json(default={})
        if (
            not isinstance(payload, dict)
            or type(payload.get("id")) is not int
            or not isinstance(payload.get("scope"), str)
        ):
            return error_response("请求无效")
        deleted = await self.memory.call(self.store.forget_memory, payload["scope"], payload["id"])
        await self.memory.refresh(payload["scope"])
        return json_response({"deleted": deleted})

    async def page_control(self):
        from astrbot.api.web import error_response, json_response, request

        if not request.username:
            return error_response("需要登录", status_code=401)
        payload = await request.json(default={})
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("scope"), str)
            or not await self.memory.call(self.store.scope, payload["scope"])
        ):
            return error_response("会话不存在")
        action = payload.get("action")
        if not isinstance(action, str) or action not in {
            "pause", "resume", "retry", "contact_on", "contact_off",
            "clear_confirm", "clear_memory", "clear_context", "clear_records",
        }:
            return error_response("操作无效")
        try:
            await self.do_control(payload["scope"], action)
        except ContextPersonaChanged as error:
            return error_response(str(error), status_code=409)
        except Exception:
            logger.exception("自主认知管理操作失败 (%s)", action)
            return error_response("操作未完成，可能已有部分数据清理成功。请重试或查看 AstrBot 日志。", status_code=500)
        return json_response({"saved": True})

    async def terminate(self):
        if self.closing:
            return
        self.closing = True
        if ObservedGroup.owner and ObservedGroup.owner() is self:
            ObservedGroup.owner = None
        self.link.close()
        tasks = [t for t in (self.worker, self.scheduler, self.analysis_task, self.initiative_task,
                            *self.history.tasks.values()) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.writer is not None:
            await self.capture_queue.join()
            self.writer.cancel()
            await asyncio.gather(self.writer, return_exceptions=True)
        await self.memory.call(self.store.close)
