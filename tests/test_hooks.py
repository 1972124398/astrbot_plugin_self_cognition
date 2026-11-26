import asyncio
import json
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import At
from astrbot_plugin_self_cognition.cognition import actor_key, scope_key
from astrbot.api.web import PluginRequest, bind_request_context
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.platform_message_history_mgr import PlatformMessageHistoryManager
from starlette.requests import Request
from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
from astrbot.core.utils.session_lock import session_lock_manager
from astrbot_plugin_self_cognition.main import SelfCognition, TURN
from test_cognition import HUMAN, reflection


class Event:
    unified_msg_origin = "test:FriendMessage:alice"

    def __init__(self, message=HUMAN):
        self.message = message
        self.message_obj = SimpleNamespace(message_id="msg-1")
        self.extras = {"_trace_persona_id": "friend"}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_sender_id(self):
        return "alice"

    def get_sender_name(self):
        return "Alice"

    def get_message_str(self):
        return self.message

    def plain_result(self, value):
        return value


class Config(dict):
    def save_config(self):
        self.saved = True


class Hooks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stars = {}
        self.context = SimpleNamespace(
            get_config=lambda **_: {"plugin_set": ["*"]},
            get_registered_star=lambda name: self.stars.get(name),
            get_current_chat_provider_id=AsyncMock(return_value="test-provider"),
            persona_manager=SimpleNamespace(get_persona_v3_by_id=lambda _: {"prompt": "你是有边界的朋友"}),
            llm_generate=AsyncMock(
                return_value=SimpleNamespace(completion_text=json.dumps(reflection(), ensure_ascii=False))
            ),
            register_web_api=lambda *args: None,
            send_message=AsyncMock(return_value=True),
        )
        with patch("astrbot_plugin_self_cognition.main.StarTools.get_data_dir", return_value=Path(self.temp.name)):
            self.plugin = SelfCognition(self.context, Config(behavior_preferences="按自己的性格自然相处"))

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def prepare(self, event=None):
        event = event or Event()
        request = ProviderRequest(prompt=event.message)
        await self.plugin.prepare(event, request)
        return event, request

    async def exchange(self):
        event, request = await self.prepare()
        await self.plugin.response(event, SimpleNamespace(completion_text="我记得。", tools_call_args=[]))
        await self.plugin.delivered(event)
        await self.plugin.capture_queue.join()
        # Exercise the same durable job path as the worker, without a timing race.
        if self.plugin.worker:
            self.plugin.worker.cancel()
            await asyncio.gather(self.plugin.worker, return_exceptions=True)
        job = await self.plugin.memory.call(
            self.plugin.store.next_batch, time.time() + 1, [event.get_extra(TURN)["scope"]], 0
        )
        if job:
            await self.plugin.process(job)
        return event, request

    async def test_frontend_no_model_call_and_temp_context(self):
        event, request = await self.prepare()
        self.context.llm_generate.assert_not_awaited()
        self.assertTrue(any(p.text.startswith("<self_cognition>") for p in request.extra_user_content_parts))
        self.assertEqual(request.contexts, [])
        await self.plugin.prepare(event, request)
        self.assertEqual(len(request.extra_user_content_parts), 2)

    async def test_delivered_hook_is_idempotent_and_preserves_persona(self):
        event, _ = await self.exchange()
        await self.plugin.delivered(event)
        scope = event.get_extra(TURN)["scope"]
        self.assertEqual(self.plugin.store.stats(scope)["feelings"], 1)
        self.assertEqual(self.plugin.store.context(scope, event.get_extra(TURN)["actor"])["feeling"]["affection"], 3)
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertEqual(payload["persona"], "你是有边界的朋友")

    async def test_task_events_never_enter_human_memory_pipeline(self):
        for key, value in (
            ("codex_bridge", {"kind": "task_result"}),
            ("w1_event", {"is_user_message": False}),
            ("self_amusement", True),
        ):
            event = Event()
            event.set_extra(key, value)
            await self.prepare(event)
            self.assertIsNone(event.get_extra(TURN))
        self.assertEqual(self.plugin.store.scopes(), [])

    async def test_unsent_and_tool_responses_are_not_committed(self):
        event, _ = await self.prepare()
        await self.plugin.response(event, SimpleNamespace(completion_text="tool", tools_call_args=[{}]))
        await self.plugin.delivered(event)
        self.assertEqual(self.plugin.store.stats(event.get_extra(TURN)["scope"])["pending"], 0)

    async def test_streaming_agent_completion_commits_without_after_sent_hook(self):
        event, _ = await self.prepare()
        event.get_result = lambda: MessageEventResult(result_content_type=ResultContentType.STREAMING_RESULT)
        response = SimpleNamespace(completion_text="我记得。", tools_call_args=[], role="assistant")
        await self.plugin.agent_done(event, None, response)
        await self.plugin.agent_done(event, None, response)
        await self.plugin.capture_queue.join()
        self.assertEqual(self.plugin.store.stats(event.get_extra(TURN)["scope"])["pending"], 1)

    async def test_clear_while_provider_inflight(self):
        event, _ = await self.prepare()
        await self.plugin.capture_queue.join()
        self.plugin.worker.cancel()
        await asyncio.gather(self.plugin.worker, return_exceptions=True)
        job = event.get_extra(TURN)
        job["reply"] = "reply"
        self.plugin.store.enqueue(job)
        entered, finish = asyncio.Event(), asyncio.Event()

        async def provider(**kwargs):
            entered.set()
            await finish.wait()
            return SimpleNamespace(completion_text=json.dumps(reflection(), ensure_ascii=False))

        self.context.llm_generate.side_effect = provider
        task = asyncio.create_task(self.plugin.process(job))
        await entered.wait()
        self.plugin.store.clear(job["scope"])
        finish.set()
        self.assertFalse(await task)
        self.assertEqual(self.plugin.store.stats(job["scope"])["memories"], 0)

    async def test_proactive_default_off_and_success_only_once(self):
        event, _ = await self.exchange()
        scope = event.get_extra(TURN)["scope"]
        self.plugin.store.db.execute("UPDATE scopes SET last_human=? WHERE id=?", (time.time() - 90000, scope))
        self.plugin.store.db.commit()
        state = self.plugin.store.scope(scope)
        await self.plugin.curiosity_step(state)
        self.context.send_message.assert_not_awaited()
        self.plugin.config.update(
            proactive_enabled=True, proactive_origins=[event.unified_msg_origin], quiet_start=0, quiet_end=0
        )
        self.context.llm_generate.return_value = SimpleNamespace(completion_text="上次说在练琴，最近练得怎么样？")
        await self.plugin.curiosity_step(state)
        self.context.send_message.assert_awaited_once()
        self.assertIn(
            "最近练得怎么样",
            self.plugin.memory.recall(scope, event.get_extra(TURN)["actor"])["portrait"]["last_outreach"],
        )
        await self.plugin.curiosity_step(self.plugin.store.scope(scope))
        self.context.send_message.assert_awaited_once()

    async def test_frontend_reads_natural_cached_impressions_without_database(self):
        data = reflection()
        data["portrait"] = {
            "impression": "熟悉以后能放松地开玩笑。",
            "shared_story": "一起聊过练琴。",
            "style": "不用每次都认真鼓励。",
        }
        data["self_reflection"] = "我也有想安静陪着的时候。"
        self.context.llm_generate.return_value = SimpleNamespace(completion_text=json.dumps(data, ensure_ascii=False))
        await self.exchange()
        event = Event("今天继续练琴")
        event.message_obj.message_id = "next-message"
        with (
            patch.object(self.plugin.store, "context", side_effect=AssertionError("front must not read SQLite")),
            patch.object(self.plugin.store, "ensure_scope", side_effect=AssertionError("front must not write SQLite")),
        ):
            _, request = await self.prepare(event)
            hint = "\n".join(p.text for p in request.extra_user_content_parts)
        self.assertIn("一起聊过练琴", hint)
        self.assertNotIn("熟悉以后能放松地开玩笑", hint)
        self.assertNotIn("我也有想安静陪着的时候", hint)
        self.assertNotIn('"affection"', hint)
        self.assertNotIn('"confidence"', hint)
        self.assertNotIn("真诚感谢", hint)  # The raw ledger is not part of ordinary chat.

    async def test_slow_storage_keeps_frontend_responsive(self):
        entered, release = threading.Event(), threading.Event()
        capture = self.plugin.store.capture

        def slow_capture(*args):
            entered.set()
            release.wait(2)
            return capture(*args)

        with patch.object(self.plugin.store, "capture", side_effect=slow_capture):
            await self.prepare()
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(entered.is_set())
            try:
                event = Event("先和我聊会儿")
                event.message_obj.message_id = "new"
                await asyncio.wait_for(self.prepare(event), 0.2)
                self.context.llm_generate.assert_not_awaited()
            finally:
                release.set()
            await self.plugin.capture_queue.join()

    async def test_new_chat_interrupts_background_inference_without_losing_batch(self):
        self.plugin.config["reflection_idle_seconds"] = 0
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def provider(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.context.llm_generate.side_effect = provider
        event, _ = await self.prepare()
        await self.plugin.response(event, SimpleNamespace(completion_text="好呀。", tools_call_args=[]))
        await self.plugin.delivered(event)
        await asyncio.wait_for(entered.wait(), 2)
        next_event = Event("先回复我这个")
        next_event.message_obj.message_id = "new-turn"
        await asyncio.wait_for(self.prepare(next_event), 0.2)
        await asyncio.wait_for(cancelled.wait(), 1)
        await self.plugin.capture_queue.join()
        stats = await self.plugin.memory.call(self.plugin.store.stats, event.get_extra(TURN)["scope"])
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["failed"], 0)

    async def test_initiative_can_choose_silence_without_repeated_model_calls(self):
        event, _ = await self.exchange()
        scope = event.get_extra(TURN)["scope"]
        self.plugin.store.db.execute("UPDATE scopes SET last_human=? WHERE id=?", (time.time() - 90000, scope))
        self.plugin.store.db.commit()
        self.plugin.config.update(
            proactive_enabled=True, proactive_origins=[event.unified_msg_origin], quiet_start=0, quiet_end=0
        )
        self.context.llm_generate.reset_mock()
        self.context.llm_generate.return_value = SimpleNamespace(completion_text="WAIT")
        await self.plugin.curiosity_step(self.plugin.store.scope(scope))
        await self.plugin.curiosity_step(self.plugin.store.scope(scope))
        self.context.llm_generate.assert_awaited_once()
        self.context.send_message.assert_not_awaited()

    async def test_failed_delivery_does_not_consume_question(self):
        event, _ = await self.exchange()
        scope = event.get_extra(TURN)["scope"]
        self.plugin.store.db.execute("UPDATE scopes SET last_human=? WHERE id=?", (time.time() - 90000, scope))
        self.plugin.store.db.commit()
        self.plugin.config.update(
            proactive_enabled=True, proactive_origins=[event.unified_msg_origin], quiet_start=0, quiet_end=0
        )
        self.context.send_message.return_value = False
        await self.plugin.curiosity_step(self.plugin.store.scope(scope))
        self.assertEqual(len(self.plugin.store.context(scope, event.get_extra(TURN)["actor"])["curiosity"]), 1)

    async def test_private_contact_opt_out_and_busy_lease(self):
        event, _ = await self.exchange()
        scope = event.get_extra(TURN)["scope"]
        self.plugin.store.db.execute("UPDATE scopes SET last_human=? WHERE id=?", (time.time() - 90000, scope))
        self.plugin.store.db.commit()
        self.plugin.config.update(
            proactive_enabled=True, proactive_origins=[event.unified_msg_origin], quiet_start=0, quiet_end=0
        )
        self.plugin.store.contact(scope, False)
        await self.plugin.curiosity_step(self.plugin.store.scope(scope))
        self.context.send_message.assert_not_awaited()
        self.plugin.store.contact(scope, True)
        self.plugin.link.begin(Event())
        await self.plugin.curiosity_step(self.plugin.store.scope(scope))
        self.context.send_message.assert_not_awaited()

    async def test_companion_optional_disabled_and_broken(self):
        self.assertEqual(self.plugin.link.snapshots(Event.unified_msg_origin), {})
        self.stars["astrbot_plugin_play_together"] = SimpleNamespace(
            activated=True,
            star_cls=SimpleNamespace(config={}, companion_snapshot=lambda _: {"last_game": "一起完成猜数"}),
        )
        self.assertIn("play_together", self.plugin.link.snapshots(Event.unified_msg_origin))
        self.plugin.config["link_play_together"] = False
        self.assertEqual(self.plugin.link.snapshots(Event.unified_msg_origin), {})

    async def test_view_api_uses_real_sdk_auth_and_schema_validation(self):
        req = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})
        with bind_request_context(PluginRequest(req, username=None)):
            response = await self.plugin.page_overview()
            self.assertEqual(response.status_code, 401)
            response = await self.plugin.page_sessions()
            self.assertEqual(response.status_code, 401)
        with bind_request_context(PluginRequest(req, username="admin")):
            response = await self.plugin.page_settings()
            self.assertIn("schema", json.loads(response.body))
        for value in (
            {"enabled": "yes"},
            {"quiet_start": 50},
            {"queue_limit": True},
            {"unknown": 1},
            {"timezone": "Invalid/Zone"},
        ):
            with self.assertRaises(ValueError):
                self.plugin.validate_settings(value)

    async def test_panel_cleanup_with_native_history_and_separate_personas(self):
        event, _ = await self.exchange()
        store = self.plugin.store
        job = event.get_extra(TURN)
        origin, scope = job["origin"], job["scope"]
        other = scope_key(origin, "other")
        store.ensure_scope(other, origin, "other")
        self.assertTrue(store.enqueue({**job, "id": "other-persona", "scope": other}))
        pending = {**job, "id": "waiting"}
        self.assertTrue(store.enqueue(pending))
        now = time.time()
        self.plugin.memory.focus[scope, job["actor"]] = ("旧话题", now)

        database = SQLiteDatabase(str(Path(self.temp.name) / "astrbot.sqlite3"))
        self.addAsyncCleanup(database.engine.dispose)
        await database.initialize()
        history = [{"role": "user", "content": "之前的对话"}]
        current = await database.create_conversation(origin, "test", content=history, persona_id="friend")
        cid = current.conversation_id
        previous = await database.create_conversation(origin, "test", content=history, persona_id="friend")
        manager = ConversationManager(database)
        manager.session_conversations[origin] = cid
        self.context.conversation_manager = manager
        self.context.get_platform_inst = lambda _: None
        self.context.persona_manager.resolve_selected_persona = AsyncMock(return_value=("friend", None, None, False))
        messages = self.context.message_history_manager = PlatformMessageHistoryManager(database)
        other_origin = "test:FriendMessage:bob"
        for user_id in (origin, other_origin):
            await messages.insert("test", user_id, {"type": "user", "message": []})

        self.assertTrue(store.archive_history(scope, cid, history, [], now))
        self.assertTrue(store.save_history_summary(scope, cid, 1, "digest", {"facts": ["旧事实"]}, 0, now))
        self.assertTrue(store.save_history_state(scope, cid, [], now, now, generation=0))

        async def control(action, target=scope, username="admin"):
            async def receive():
                return {"type": "http.request", "body": json.dumps({"scope": target, "action": action}).encode()}

            req = Request({"type": "http", "method": "POST", "path": "/", "query_string": b"", "headers": []},
                          receive=receive)
            with bind_request_context(PluginRequest(req, username=username)):
                return (await self.plugin.page_control()).status_code

        self.assertEqual(await control("clear_records", username=None), 401)
        self.assertEqual(await control("clear_context", other), 409)
        self.assertEqual(json.loads((await manager.get_conversation(origin, cid)).history), history)

        started = asyncio.Event()

        async def request_cleanup():
            started.set()
            return await control("clear_context")

        async with session_lock_manager.acquire_lock(origin):
            cleanup = asyncio.create_task(request_cleanup())
            await started.wait()
            history += [{"role": "assistant", "content": "清理前完成的回复"}]
            await manager.update_conversation(origin, cid, history=history)
            self.assertFalse(cleanup.done())
            self.assertEqual(json.loads((await manager.get_conversation(origin, cid)).history), history)
        self.assertEqual(await cleanup, 200)
        self.assertEqual(json.loads((await manager.get_conversation(origin, cid)).history), [])
        self.assertEqual(json.loads((await manager.get_conversation(origin, previous.conversation_id)).history),
                         history[:1])
        self.assertEqual(await messages.count("test", origin), 1)
        self.assertGreater(store.stats(scope)["memories"], 0)
        self.assertEqual(store.stats(scope)["pending"], 1)
        self.assertNotIn((scope, job["actor"]), self.plugin.memory.focus)
        self.assertIsNone(store.history_summary(scope, cid))
        self.assertFalse(store.save_history_state(scope, cid, [], now, now, generation=0))
        self.assertFalse(store.save_history_summary(scope, cid, 1, "digest", {}, 0, now))

        self.assertEqual(await control("clear_records"), 200)
        self.assertEqual(await control("clear_records"), 200)
        self.assertEqual(await messages.count("test", origin), 0)
        self.assertEqual(await messages.count("test", other_origin), 1)
        for table in ("episodes", "history_archives"):
            self.assertIsNone(store.db.execute(f"SELECT 1 FROM {table} WHERE scope=?", (scope,)).fetchone())
        self.assertGreater(store.stats(scope)["memories"], 0)
        self.assertEqual(store.stats(scope)["pending"], 0)
        self.assertFalse(store.capture(pending))
        self.assertEqual(store.stats(other)["pending"], 1)

        fresh = {**pending, "id": "fresh", "generation": store.scope(scope)["generation"]}
        self.assertTrue(store.capture(fresh))
        self.assertEqual(await control("clear_memory"), 200)
        self.assertEqual(store.stats(scope)["memories"], 0)
        self.assertEqual(store.stats(scope)["feelings"], 0)
        self.assertEqual(store.stats(scope)["pending"], 0)
        self.assertFalse(store.capture(fresh))
        self.assertIsNotNone(store.db.execute("SELECT 1 FROM episodes WHERE id='fresh'").fetchone())
        self.assertEqual(store.stats(other)["pending"], 1)

    async def test_webchat_cleanup_checks_session_and_thread_owners(self):
        database = SQLiteDatabase(str(Path(self.temp.name) / "astrbot.sqlite3"))
        self.addAsyncCleanup(database.engine.dispose)
        await database.initialize()
        session = await database.create_platform_session("owner")
        thread = await database.create_webchat_thread("owner", session.session_id, 1, "checkpoint", "selected")
        self.context.get_db = lambda: database
        messages = self.context.message_history_manager = PlatformMessageHistoryManager(database)
        stored = {"type": "user", "message": []}
        await messages.insert("webchat", session.session_id, stored)
        await messages.insert("webchat_thread", thread.thread_id, stored)
        await self.plugin.clear_platform_records(f"webchat:FriendMessage:webchat!other!{session.session_id}")
        await self.plugin.clear_platform_records(f"webchat:FriendMessage:webchat!other!{thread.thread_id}")
        self.assertEqual(await messages.count("webchat", session.session_id), 1)
        self.assertEqual(await messages.count("webchat_thread", thread.thread_id), 1)
        await self.plugin.clear_platform_records(f"webchat:FriendMessage:webchat!owner!{session.session_id}")
        self.assertEqual(await messages.count("webchat", session.session_id), 0)
        self.assertEqual(await messages.count("webchat_thread", thread.thread_id), 1)
        await self.plugin.clear_platform_records(f"webchat:FriendMessage:webchat!owner!{thread.thread_id}")
        self.assertEqual(await messages.count("webchat_thread", thread.thread_id), 0)

    async def test_companion_cleanup_validates_scope_and_only_clears_selected_peers(self):
        from astrbot_plugin_self_cognition.cognition import conversation_key

        origin = Event.unified_msg_origin
        scope = scope_key(origin, "friend")
        store = self.plugin.store
        state = store.ensure_scope(scope, origin, "friend")
        await self.plugin.memory.refresh(scope)
        play_clear = AsyncMock(return_value="cleared")
        self.stars["astrbot_plugin_play_together"] = SimpleNamespace(
            activated=True, star_cls=SimpleNamespace(config={}, companion_clear=play_clear),
        )
        self.stars["astrbot_plugin_short_term_mood"] = SimpleNamespace(
            activated=True, star_cls=SimpleNamespace(config={}, companion_clear=AsyncMock(return_value="busy")),
        )
        self.stars["astrbot_plugin_daily_routine"] = SimpleNamespace(
            activated=True, star_cls=SimpleNamespace(config={}),
        )
        self.plugin.companions[origin] = {"play_together": {"last_game": "game"}, "self_amusement": {"idea": "idea"}}
        job = {"scope": scope, "generation": state["generation"], "source_kind": "companion",
               "origin": origin, "actor": "self", "name": "我", "reply": ""}
        for role in ("play_together", "self_amusement"):
            store.enqueue({**job, "id": role, "human": json.dumps({"source": role})})

        async def request_cleanup(roles, target_origin=origin, username="admin"):
            async def receive():
                return {"type": "http.request", "body": json.dumps({
                    "scope": scope, "origin": target_origin, "plugins": roles,
                }).encode()}

            req = Request({"type": "http", "method": "POST", "path": "/", "query_string": b"", "headers": []},
                          receive=receive)
            with bind_request_context(PluginRequest(req, username=username)):
                return await self.plugin.page_companion_cleanup()

        self.assertEqual((await request_cleanup(["play_together"], username=None)).status_code, 401)
        self.assertEqual((await request_cleanup(["play_together"], "wrong")).status_code, 409)
        play_clear.assert_not_awaited()
        response = await request_cleanup(["play_together", "short_term_mood", "daily_routine", "play_together"])
        self.assertEqual([result["status"] for result in json.loads(response.body)["results"]],
                         ["cleared", "busy", "unavailable"])
        play_clear.assert_awaited_once_with(origin, persona="friend")
        self.assertEqual(store.stats(scope)["pending"], 1)
        self.assertEqual(store.next_job(time.time() + 1)["id"], "self_amusement")
        self.assertNotIn("play_together", self.plugin.companions[origin])
        self.assertIn("self_amusement", self.plugin.companions[origin])
        self.assertEqual(self.plugin.companion_generations[conversation_key(origin), "play_together"], 1)

    async def test_view_uses_astrbot_session_names_and_preserves_configured_origins(self):
        friend = Event.unified_msg_origin
        group = "test:GroupMessage:book-club"
        legacy = "test:group:old-club"
        database = SQLiteDatabase(str(Path(self.temp.name) / "astrbot.sqlite3"))
        self.addAsyncCleanup(database.engine.dispose)
        await database.initialize()
        await database.upsert_umo_alias(friend, "alice", "Alice", "老朋友")
        await database.upsert_umo_alias(group, "alice", "读书会", None)
        self.context.get_db = lambda: database
        self.plugin.config["allowed_origins"] = [legacy]
        await self.plugin.memory.call(self.plugin.store.ensure_scope, scope_key(friend, "friend"), friend, "friend")

        req = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})
        with bind_request_context(PluginRequest(req, username="admin")):
            response = await self.plugin.page_sessions()
            catalog = {item["umo"]: item for item in json.loads(response.body)["sessions"]}
            self.assertEqual(catalog[friend]["display_name"], "老朋友")
            self.assertEqual(catalog[group]["message_type"], "GroupMessage")
            response = await self.plugin.page_overview()
            self.assertEqual(json.loads(response.body)["scopes"][0]["display_name"], "老朋友")
            response = await self.plugin.page_settings()
            settings = json.loads(response.body)
            self.assertEqual(settings["schema"]["allowed_origins"]["_special"], "umo")
            self.assertEqual(settings["values"]["allowed_origins"], [legacy])

        async def receive():
            return {"type": "http.request", "body": json.dumps({"allowed_origins": [legacy, group]}).encode()}

        req = Request({"type": "http", "method": "POST", "path": "/", "query_string": b"", "headers": []}, receive=receive)
        with bind_request_context(PluginRequest(req, username="admin")):
            response = await self.plugin.save_settings()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.plugin.config.saved)
        self.assertEqual(self.plugin.config["allowed_origins"], [legacy, group])
        self.assertTrue(self.plugin.allowed(group))
        self.assertTrue(self.plugin.allowed("test:GroupMessage:old-club"))
        self.assertFalse(self.plugin.allowed(friend))

    async def test_group_mention_connects_relationship_tags_to_actual_member(self):
        event = Event("这是我的同事，也是一位老朋友。")
        event.unified_msg_origin = "test:GroupMessage:123"
        event.get_messages = lambda: [At(qq="bob", name="小明")]
        bob = actor_key(event.unified_msg_origin, "bob")
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text=json.dumps(
                {
                    "relations": [
                        {"source": "$speaker", "target": bob, "labels": ["同事", "朋友"], "evidence": "我的同事"}
                    ]
                },
                ensure_ascii=False,
            )
        )
        await self.prepare(event)
        await self.plugin.response(event, SimpleNamespace(completion_text="原来你们早就认识。", tools_call_args=[]))
        await self.plugin.delivered(event)
        await self.plugin.capture_queue.join()
        self.plugin.worker.cancel()
        await asyncio.gather(self.plugin.worker, return_exceptions=True)
        batch = await self.plugin.memory.call(
            self.plugin.store.next_batch, time.time(), [event.get_extra(TURN)["scope"]], 0
        )
        await self.plugin.process(batch)
        data = self.plugin.memory.recall(batch["scope"], batch["actor"], "小明")
        self.assertEqual({r["target"] for r in data["relations"]}, {bob})
        self.assertEqual({r["label"] for r in data["relations"]}, {"朋友", "同事"})
