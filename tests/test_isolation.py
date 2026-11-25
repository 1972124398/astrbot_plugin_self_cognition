import asyncio
import json
import time
import unittest
from types import SimpleNamespace
import test_hooks as fixtures
from astrbot_plugin_self_cognition.cognition import scope_key


class IsolationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.Hooks.asyncSetUp
    asyncTearDown = fixtures.Hooks.asyncTearDown

    async def test_own_outreach_cannot_create_a_user_promise_or_followup(self):
        origin = fixtures.Event.unified_msg_origin
        scope = scope_key(origin, "friend")
        self.plugin.memory.identity(scope, origin, "friend")
        peer = SimpleNamespace(config={}, closing=False)
        self.stars["astrbot_plugin_self_amusement"] = SimpleNamespace(activated=True, star_cls=peer)
        self.plugin.link.connect()
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text=json.dumps(
                {
                    "intentions": [
                        {"actor": "$self", "topic": "token", "thought": "对方答应的 token 呢", "kind": "ask"}
                    ],
                    "character": {"interests": ["替对方追问 token"]},
                    "experience": {"title": "对方承诺", "story": "对方答应送 token"},
                }
            )
        )
        self.plugin.companion_observe(
            {
                "source": "self_amusement",
                "origin": origin,
                "kind": "initiative",
                "id": "sent-7391",
                "data": {"text": "那个 token 到底算不算数？"},
            }
        )
        await self.plugin.capture_queue.join()
        if self.plugin.worker:
            self.plugin.worker.cancel()
            await asyncio.gather(self.plugin.worker, return_exceptions=True)
        job = self.plugin.store.next_batch(time.time() + 1, [scope], 0)
        self.assertIsNotNone(job)
        await self.plugin.process(job)
        self.context.llm_generate.assert_not_awaited()
        self.assertEqual(self.plugin.store.db.execute("SELECT count(*) FROM intentions").fetchone()[0], 0)
        self.assertEqual(self.plugin.store.db.execute("SELECT count(*) FROM characters").fetchone()[0], 0)
        story = self.plugin.store.db.execute("SELECT story FROM experiences WHERE scope=?", (scope,)).fetchone()[0]
        self.assertIn("那个 token 到底算不算数", story)
        self.assertIn("对方是否认可未知", story)

    async def test_outreach_and_real_companion_activity_are_batched_separately(self):
        origin = fixtures.Event.unified_msg_origin
        scope = scope_key(origin, "friend")
        self.plugin.memory.identity(scope, origin, "friend")
        self.stars["astrbot_plugin_self_amusement"] = SimpleNamespace(
            activated=True, star_cls=SimpleNamespace(config={}, closing=False)
        )
        self.plugin.link.connect()
        for kind in ("initiative", "leisure_experience"):
            self.plugin.companion_observe(
                {
                    "source": "self_amusement",
                    "origin": origin,
                    "kind": kind,
                    "id": kind,
                    "data": {"text": "自己说过的话", "idea": "一段真实见闻"},
                }
            )
        await self.plugin.capture_queue.join()
        if self.plugin.worker:
            self.plugin.worker.cancel()
            await asyncio.gather(self.plugin.worker, return_exceptions=True)
        job = self.plugin.store.next_batch(time.time() + 1, [scope], 0)
        self.assertEqual(len(job["member_ids"]), 1)
