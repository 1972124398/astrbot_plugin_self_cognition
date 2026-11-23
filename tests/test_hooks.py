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
from astrbot_plugin_self_cognition.cognition import actor_key
from astrbot.api.web import PluginRequest, bind_request_context
from starlette.requests import Request
from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
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
        self.temp.cleanup()

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
        self.assertTrue(request.extra_user_content_parts[0].text.startswith("<self_cognition>"))
        self.assertEqual(request.contexts, [])
        await self.plugin.prepare(event, request)
        self.assertEqual(len(request.extra_user_content_parts), 1)

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
            hint = request.extra_user_content_parts[0].text
        self.assertIn("熟悉以后能放松地开玩笑", hint)
        self.assertIn("我也有想安静陪着的时候", hint)
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
