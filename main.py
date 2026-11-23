from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import MessageChain, ResultContentType

from .cognition import (
    CONTEXT_FRAME,
    REFLECT_SYSTEM,
    actor_key,
    conversation_key,
    digest,
    event_kind,
    number,
    parse_reflection,
    scope_key,
    text,
)
from .interop import CompanionLink
from .store import Store

NAME = "astrbot_plugin_self_cognition"
TURN = NAME + ".turn"


class SelfCognition(Star):
    """自主认知：长期记忆、相处经验、关系图谱、永久好感与好奇心。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = Store(Path(StarTools.get_data_dir(NAME)) / "cognition.sqlite3")
        self.link = CompanionLink(context, config)
        self.closing = False
        self.worker = self.scheduler = None
        self.wake = asyncio.Event()
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
                ("settings", "GET", self.page_settings),
                ("settings", "POST", self.save_settings),
                ("forget", "POST", self.page_forget),
                ("control", "POST", self.page_control),
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
        state = self.store.ensure_scope(scope, origin, persona) if create else self.store.scope(scope)
        return scope, actor, name, state

    def persona_prompt(self, event, request):
        persona = event.get_extra("_trace_persona_id")
        if persona == "[%None]":
            return ""
        if persona and persona not in {"default", "_chatui_default_"}:
            value = self.context.persona_manager.get_persona_v3_by_id(persona)
            if value:
                return text(value.get("prompt"), 12000)
        return text(request.system_prompt, 12000)

    async def initialize(self):
        self.worker = asyncio.create_task(self.run(), name="self-cognition-reflection")
        self.scheduler = asyncio.create_task(self.tick(), name="self-cognition-curiosity")
        self.wake.set()

    def ensure_worker(self):
        if not self.closing and (self.worker is None or self.worker.done()):
            self.worker = asyncio.create_task(self.run(), name="self-cognition-reflection")
        self.wake.set()

    def enqueue(self, job):
        try:
            accepted = self.store.enqueue(job, int(self.n("queue_limit", 256, 8, 4096)))
        except OverflowError:
            self.last_error = "整理队列已满，本轮未存储；请检查模型或增加队列上限。"
            logger.warning("Self-cognition reflection queue full")
            return
        if accepted:
            self.ensure_worker()

    @filter.on_llm_request(priority=-50)
    async def prepare(self, event: AstrMessageEvent, request: ProviderRequest):
        origin = str(event.unified_msg_origin)
        if not self.allowed(origin) or event.get_extra("self_cognition_skip"):
            return
        # Task reports and autonomous messages are not new human experiences.
        if event_kind(event) != "dialogue":
            return
        existing = event.get_extra(TURN)
        if isinstance(existing, dict):
            return
        scope, actor, name, state = self.identity(event)
        if state["paused"] or not actor:
            return
        human = text(event.get_message_str(), 12000)
        if not human:
            return
        self.store.touch(scope, actor, name, time.time())
        self.link.begin(event)
        mid = str(getattr(event.message_obj, "message_id", "") or uuid.uuid4().hex)
        job = {
            "id": digest(scope, mid, actor),
            "scope": scope,
            "generation": state["generation"],
            "actor": actor,
            "name": name,
            "origin": origin,
            "source_kind": "dialogue",
            "human": human,
            "reply": "",
            "persona": self.persona_prompt(event, request),
            "provider_id": text(self.config.get("analysis_provider_id"), 200),
            "captured": time.time(),
        }
        recalled = self.store.context(scope, actor, human, int(self.n("recall_limit", 10, 1, 30)))
        companions = self.link.snapshots(origin)
        job["companions"] = companions
        job["questions"] = recalled["curiosity"]
        event.set_extra(TURN, job)
        payload = {
            "speaker": {"id": actor, "name": name},
            "memories": recalled["memories"],
            "relations": recalled["relations"],
            "entities": recalled["entities"],
            "feeling": recalled["feeling"],
            "curiosity": recalled["curiosity"] if self.config.get("curiosity_enabled", True) else [],
            "companion": copy.deepcopy(companions),
            "preferences": text(self.config.get("behavior_preferences"), 1200),
        }
        self.fit_context(payload)
        parts = getattr(request, "extra_user_content_parts", None)
        if parts is None:
            parts = request.extra_user_content_parts = []
        if not any(str(getattr(p, "text", "")).startswith("<self_cognition>") for p in parts):
            parts.append(
                TextPart(text=CONTEXT_FRAME.format(payload=json.dumps(payload, ensure_ascii=False))).mark_as_temp()
            )
        self.observe_snapshots(state, companions, job["persona"])

    def fit_context(self, payload):
        budget = int(self.n("context_char_limit", 9000, 2000, 30000))
        # Remove complete records rather than cutting JSON or an evidence quote.
        for key in ("companion", "entities", "relations", "memories", "curiosity"):
            while len(json.dumps(payload, ensure_ascii=False)) > budget and payload.get(key):
                if isinstance(payload[key], dict):
                    payload[key].pop(next(reversed(payload[key])))
                else:
                    payload[key].pop()

    @filter.on_llm_response(priority=-100)
    async def response(self, event: AstrMessageEvent, response):
        job = event.get_extra(TURN)
        if (
            not isinstance(job, dict)
            or getattr(response, "tools_call_args", None)
            or getattr(response, "role", "assistant") != "assistant"
        ):
            return
        job["reply"] = text(getattr(response, "completion_text", ""), 12000)
        self.link.end(event)

    @filter.on_agent_done(priority=-100)
    async def agent_done(self, event: AstrMessageEvent, run_context, response):
        await self.response(event, response)
        self.link.end(event)
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
        if self.config.get("curiosity_enabled", True):
            for question in job["questions"]:
                if question["question"] in job["reply"]:
                    self.store.mark_question(job["scope"], question["id"], time.time())

    def observe_snapshots(self, state, snapshots, persona=""):
        # Snapshot evidence is explicitly attributed to a plugin, never the human.
        for role in ("play_together", "self_amusement", "codex_bridge"):
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
                    "persona": persona,
                    "source_kind": "companion",
                    "provider_id": text(self.config.get("analysis_provider_id"), 200),
                    "captured": time.time(),
                    "questions": [],
                }
            )

    async def process(self, job):
        state = self.store.scope(job["scope"])
        if not state or not self.allowed(job["origin"]) or state["paused"] or state["generation"] != job["generation"]:
            return False
        recalled = self.store.context(job["scope"], job["actor"], job["human"], 12)
        payload = {key: job.get(key) for key in ("human", "reply", "persona", "source_kind", "name", "captured")}
        payload["known"] = recalled
        provider = job["provider_id"] or await self.context.get_current_chat_provider_id(job["origin"])
        async with asyncio.timeout(self.n("analysis_timeout_seconds", 45, 5, 180)):
            response = await self.context.llm_generate(
                chat_provider_id=provider, system_prompt=REFLECT_SYSTEM, prompt=json.dumps(payload, ensure_ascii=False)
            )
        if not self.allowed(job["origin"]):
            return False
        result = parse_reflection(response.completion_text, job["human"], companion=job["source_kind"] == "companion")
        if not self.config.get("curiosity_enabled", True):
            result["curiosity"] = []
        return self.store.apply(job, result, self.n("affection_daily_cap", 10, 0, 100))

    async def run(self):
        while not self.closing:
            self.wake.clear()
            eligible = [s["id"] for s in self.store.scopes() if not s["paused"] and self.allowed(s["origin"])]
            job = self.store.next_job(time.time(), eligible)
            if job and self.allowed(job["origin"]):
                try:
                    if await self.process(job):
                        self.last_error = ""
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.last_error = "后台整理失败：" + type(error).__name__
                    self.store.failed(job, time.time())
                    logger.warning("Self-cognition reflection failed (%s)", type(error).__name__)
                await asyncio.sleep(0.1)
            else:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=10)
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
            or not self.config.get("curiosity_enabled", True)
            or not self.config.get("proactive_enabled", False)
            or origin not in self.config.get("proactive_origins", [])
            or self.in_quiet_hours()
        ):
            return
        now = time.time()
        if (
            not state["last_human"]
            or now - max(state["last_human"], state["last_sent"]) < self.n("proactive_gap_hours", 24, 1, 720) * 3600
        ):
            return
        if now - state["last_human"] > self.n("proactive_max_idle_days", 7, 1, 365) * 86400:
            return
        if self.store.db.execute("SELECT 1 FROM questions WHERE scope=? AND status='sent'", (state["id"],)).fetchone():
            return
        question = self.store.db.execute(
            "SELECT * FROM questions WHERE scope=? AND status='pending' AND created>? ORDER BY created LIMIT 1",
            (state["id"], now - 30 * 86400),
        ).fetchone()
        if not question:
            return
        # A shared group cannot identify the intended person without a platform mention.
        if ":GroupMessage:" in origin:
            return
        snapshots = self.link.snapshots(origin)
        if snapshots.get("self_amusement", {}).get("wants_space") or snapshots.get("play_together", {}).get(
            "current_game"
        ):
            return
        token = self.link.claim(self.n("interop_gap_minutes", 10, 1, 1440) * 60)
        if not token:
            return
        try:
            current = self.store.scope(state["id"])
            if (
                not current
                or current["no_contact"]
                or current["generation"] != state["generation"]
                or not self.link.current(token)
                or not self.allowed(origin)
            ):
                return
            async with asyncio.timeout(30):
                sent = await self.context.send_message(origin, MessageChain(chain=[Plain(question["question"])]))
            if sent:
                self.store.mark_question(state["id"], question["id"], now, proactive=True)
                self.link.state["last_sent"] = now
        finally:
            self.link.release(token)

    async def tick(self):
        while not self.closing:
            try:
                now = time.time()
                self.store.prune(int(self.n("raw_retention_days", 30, 0, 3650)), now)
                for state in self.store.scopes():
                    if self.allowed(state["origin"]) and not state["paused"]:
                        self.observe_snapshots(state, self.link.snapshots(state["origin"]))
                        await self.curiosity_step(state)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Self-cognition scheduler unavailable (%s)", type(error).__name__)
            await asyncio.sleep(60)

    def companion_snapshot(self, origin=""):
        # No origin means no cross-conversation memory dump. New peers can opt in.
        if not origin or not self.allowed(origin):
            return {}
        scopes = [
            s
            for s in self.store.scopes()
            if conversation_key(s["origin"]) == conversation_key(origin) and not s["paused"]
        ]
        if len(scopes) != 1:
            return {}  # Ambiguous persona: do not mix identities for old protocol callers.
        return self.store.context(scopes[0]["id"], limit=4)

    @filter.command("cognition", alias={"自主认知"})
    async def status(self, event: AstrMessageEvent):
        scope, actor, _, state = self.identity(event)
        if not self.allowed(event.unified_msg_origin):
            yield event.plain_result("自主认知未在此会话启用。")
            return
        feeling = self.store.context(scope, actor)["feeling"]
        stats = self.store.stats(scope)
        yield event.plain_result(
            f"自主认知 · {'暂停' if state['paused'] else '运行中'}\n"
            f"长期好感 {feeling.get('affection', 0):.1f} · 信任 {feeling.get('trust', 0.5):.2f}\n"
            f"记忆 {stats['memories']} · 关系 {stats['relations']} · 待整理 {stats['pending']} · 失败 {stats['failed']}\n"
            f"会话 UMO：{event.unified_msg_origin}\n使用 /cognition_recall 关键词 查看回忆；管理员在插件配置或认知面板管理。"
        )

    @filter.command("cognition_recall")
    async def recall(self, event: AstrMessageEvent, query: str = ""):
        if not self.allowed(event.unified_msg_origin):
            return
        scope, actor, _, state = self.identity(event)
        if state["paused"]:
            yield event.plain_result("此会话的自主认知已暂停。")
            return
        rows = self.store.context(scope, actor, query, 6)["memories"]
        yield event.plain_result(
            "\n".join(f"#{r['id']} [{r['kind']}] {r['content']}（依据：{r['evidence']}）" for r in rows)
            or "暂时没有相关回忆。"
        )

    @filter.command("cognition_control")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def control(self, event: AstrMessageEvent, action: str = ""):
        scope, _, _, _ = self.identity(event)
        if action not in {"pause", "resume", "retry", "clear_confirm"}:
            yield event.plain_result(
                "/cognition_control pause|resume|retry|clear_confirm（最后一项删除本会话人格的全部认知）"
            )
            return
        self.do_control(scope, action)
        yield event.plain_result("自主认知操作已完成：" + action)

    @filter.command("cognition_contact")
    async def contact(self, event: AstrMessageEvent, mode: str = "off"):
        if ":GroupMessage:" in str(event.unified_msg_origin) or mode not in {"on", "off"}:
            yield event.plain_result("私聊中使用 /cognition_contact off 关闭主动提问，on 恢复（仍需管理员配置允许）。")
            return
        scope, _, _, _ = self.identity(event)
        self.store.contact(scope, mode == "on")
        yield event.plain_result("此会话的主动提问已" + ("允许。" if mode == "on" else "关闭。"))

    def do_control(self, scope, action):
        if action in {"pause", "resume"}:
            self.store.pause(scope, action == "pause")
        elif action == "clear_confirm":
            self.store.clear(scope)
        elif action == "retry":
            self.store.retry_failed(scope)
            self.ensure_worker()
        elif action in {"contact_on", "contact_off"}:
            self.store.contact(scope, action == "contact_on")
        else:
            raise ValueError("unknown action")

    async def page_overview(self):
        from astrbot.api.web import error_response, json_response, request

        if not request.username:
            return error_response("需要登录 AstrBot 管理面板", status_code=401)
        return json_response(
            {
                "scopes": [{**s, "stats": self.store.stats(s["id"])} for s in self.store.scopes()],
                "last_error": self.last_error,
            }
        )

    async def page_scope(self):
        from astrbot.api.web import error_response, json_response, request

        if not request.username:
            return error_response("需要登录", status_code=401)
        scope = request.query.get("scope", "")
        if not self.store.scope(scope):
            return error_response("会话不存在", status_code=404)
        data = self.store.context(scope, query=request.query.get("query", ""), limit=30)
        data["all_questions"] = [
            dict(r)
            for r in self.store.db.execute(
                "SELECT * FROM questions WHERE scope=? AND status='pending' ORDER BY created LIMIT 50", (scope,)
            )
        ]
        return json_response(data)

    async def page_settings(self):
        from astrbot.api.web import error_response, json_response, request

        if not request.username:
            return error_response("需要登录", status_code=401)
        return json_response(
            {"schema": self.schema, "values": {k: self.config.get(k, v.get("default")) for k, v in self.schema.items()}}
        )

    async def save_settings(self):
        from astrbot.api.web import error_response, json_response, request

        if not request.username:
            return error_response("需要登录", status_code=401)
        values = await request.json(default={})
        try:
            self.validate_settings(values)
        except ValueError as error:
            return error_response(str(error))
        previous = dict(self.config)
        try:
            self.config.update(values)
            self.config.save_config()
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
        return json_response({"deleted": self.store.forget_memory(payload["scope"], payload["id"])})

    async def page_control(self):
        from astrbot.api.web import error_response, json_response, request

        if not request.username:
            return error_response("需要登录", status_code=401)
        payload = await request.json(default={})
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("scope"), str)
            or not self.store.scope(payload["scope"])
        ):
            return error_response("会话不存在")
        try:
            self.do_control(payload["scope"], payload.get("action"))
        except (ValueError, TypeError):
            return error_response("操作无效")
        return json_response({"saved": True})

    async def terminate(self):
        if self.closing:
            return
        self.closing = True
        self.link.close()
        tasks = [t for t in (self.worker, self.scheduler) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()
