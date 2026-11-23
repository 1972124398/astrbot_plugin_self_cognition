"""Exercise the actual bundled v1 protocol with independently installable peers."""

import importlib.util
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import test_hooks as fixtures
from astrbot_plugin_self_cognition.main import NAME


class OptionalCompanionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = fixtures.Hooks("test_frontend_no_model_call_and_temp_context")
        await self.case.asyncSetUp()
        self.plugin = self.case.plugin
        self.modules = []
        self.case.stars[NAME] = SimpleNamespace(activated=True, star_cls=self.plugin)

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    def load_protocol(self, role):
        path = Path(__file__).resolve().parents[2] / ("astrbot_plugin_" + role) / "interop.py"
        if not path.exists():
            path = Path(__file__).parent / "fixtures" / "companion_v1.py"
        spec = importlib.util.spec_from_file_location("companion_v1_" + role, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.modules.append(module)
        return module

    def install(self, role):
        module = self.load_protocol(role)
        peer = SimpleNamespace(config={}, closing=False)
        peer.link = module.CompanionLink(self.case.context, role, peer.config)
        peer.companion_snapshot = lambda origin: {"last_game": "一起猜过数字"} if role == "play_together" else {}
        self.case.stars["astrbot_plugin_" + role] = SimpleNamespace(activated=True, star_cls=peer)
        return module, peer

    async def test_late_install_automatically_receives_events_and_reads_cognition(self):
        event, _ = await self.case.exchange()
        scope = event.get_extra(fixtures.TURN)["scope"]
        with self.plugin.store.db:
            self.plugin.store.save_portrait(scope, "self", {"impression": "喜欢一起玩猜数"}, 100)
        await self.plugin.memory.refresh(scope)
        self.plugin.link.connect()  # No peers installed yet: standalone still works.
        self.assertEqual(self.plugin.link.registrations, {})
        module, peer = self.install("play_together")
        self.plugin.link.connect()
        self.assertIn("self_cognition", module.NAMES)
        self.assertIn("self_cognition", peer.link.context_for(event.unified_msg_origin))
        peer.link.publish(
            "game_turn", event.unified_msg_origin, {"outcome": "一起猜中了答案", "game": "猜数"}, "round-1"
        )
        await self.plugin.capture_queue.join()
        queued = self.plugin.store.next_batch(9999999999, [scope], 0)
        self.assertEqual(queued["source_kind"], "companion")
        self.assertIn("一起猜中了答案", queued["human"])
        self.assertEqual(self.plugin.store.stats(scope)["feelings"], 1)
        self.plugin.config["interop_enabled"] = False
        self.plugin.link.connect()
        self.assertNotIn("self_cognition", module.NAMES)
        self.plugin.config["interop_enabled"] = True
        self.plugin.link.connect()
        self.assertIn("self_cognition", module.NAMES)
        self.plugin.link.close()
        self.assertNotIn("self_cognition", module.NAMES)

    async def test_each_companion_opt_out_and_allowlist_are_respected(self):
        event, _ = await self.case.exchange()
        for role in ("play_together", "self_amusement", "short_term_mood", "codex_bridge"):
            module, peer = self.install(role)
            self.plugin.link.connect()
            self.assertIn("self_cognition", module.NAMES)
            self.plugin.config["link_" + role] = False
            self.plugin.link.connect()
            self.assertNotIn("self_cognition", module.NAMES)
            self.plugin.config["link_" + role] = True
            peer.config["interop_enabled"] = False
            self.plugin.link.connect()
            self.assertNotIn("self_cognition", module.NAMES)
            peer.config["interop_enabled"] = True
        self.case.context.get_config = lambda **_: {"plugin_set": ["astrbot_plugin_play_together"]}
        self.plugin.link.connect()
        peer = self.case.stars["astrbot_plugin_play_together"].star_cls
        self.assertNotIn("self_cognition", peer.link.context_for(event.unified_msg_origin))

    async def test_companion_experience_does_not_count_as_new_human_affection(self):
        event, _ = await self.case.exchange()
        scope = event.get_extra(fixtures.TURN)["scope"]
        _, peer = self.install("self_amusement")
        self.plugin.link.connect()
        peer.link.publish(
            "leisure_experience",
            event.unified_msg_origin,
            {"idea": "看到一篇有趣的文章", "materials": [{"title": "海边的植物"}]},
            "read-1",
        )
        await self.plugin.capture_queue.join()
        queued = self.plugin.store.next_batch(9999999999, [scope], 0)
        self.case.context.llm_generate.return_value = SimpleNamespace(
            completion_text=json.dumps(
                {
                    "experience": {"story": "自己读到海边植物的文章。"},
                    "character": {"interests": ["海边植物"]},
                    "feeling": {"affection": 3, "evidence": "海边的植物"},
                },
                ensure_ascii=False,
            )
        )
        await self.plugin.process(queued)
        self.assertEqual(self.plugin.store.stats(scope)["feelings"], 1)
        self.assertEqual(self.plugin.memory.recall(scope)["character"]["interests"], ["海边植物"])

    async def test_own_discovery_can_leave_a_thought_and_later_be_shared(self):
        event, _ = await self.case.exchange()
        actor, scope = event.get_extra(fixtures.TURN)["actor"], event.get_extra(fixtures.TURN)["scope"]
        _, peer = self.install("self_amusement")
        self.plugin.link.connect()
        peer.link.publish(
            "leisure_experience",
            event.unified_msg_origin,
            {"idea": "看到海边植物的文章", "materials": [{"title": "海边植物"}]},
            "discovery",
        )
        await self.plugin.capture_queue.join()
        batch = self.plugin.store.next_batch(9999999999, [scope], 0)
        self.case.context.llm_generate.return_value = SimpleNamespace(
            completion_text=json.dumps(
                {
                    "experience": {"story": "自己读到海边植物的文章。"},
                    "intentions": [
                        {
                            "actor": actor,
                            "topic": "海边植物",
                            "kind": "share",
                            "thought": "看到一段有意思的植物故事，想讲给他听",
                        },
                        {"actor": "$self", "topic": "潮间带", "kind": "explore", "thought": "有点想继续了解潮间带植物"},
                    ],
                },
                ensure_ascii=False,
            )
        )
        await self.plugin.process(batch)
        thoughts = self.plugin.store.dashboard(scope)["intentions"]
        self.assertEqual({i["kind"] for i in thoughts}, {"share", "explore"})
        self.assertEqual(self.plugin.store.stats(scope)["feelings"], 1)
        with self.plugin.store.db:
            self.plugin.store.db.execute("UPDATE scopes SET last_human=? WHERE id=?", (time.time() - 90000, scope))
        await self.plugin.memory.refresh(scope)
        self.plugin.config.update(
            proactive_enabled=True, proactive_origins=[event.unified_msg_origin], quiet_start=0, quiet_end=0
        )

        async def share(**kwargs):
            choices = json.loads(kwargs["prompt"])["choices"]
            selected = next(i for i in choices if i["kind"] == "share")
            return SimpleNamespace(
                completion_text=json.dumps(
                    {"choice": selected["choice"], "message": "我刚读到一段海边植物的故事，有点意思。"},
                    ensure_ascii=False,
                )
            )

        self.case.context.llm_generate.side_effect = share
        await self.plugin.curiosity_step(self.plugin.memory.states[scope])
        self.case.context.send_message.assert_awaited_once()
        statuses = {i["kind"]: i["status"] for i in self.plugin.store.dashboard(scope)["intentions"]}
        self.assertEqual(statuses, {"share": "sent", "explore": "pending"})
        own = next(i for i in self.plugin.store.dashboard(scope)["intentions"] if i["actor"] == "self")
        self.plugin.store.mark_intention(scope, own["id"], "我还想弄明白潮间带的事。", time.time())
        self.plugin.store.touch(scope, actor, "Alice", time.time())
        own = next(i for i in self.plugin.store.dashboard(scope)["intentions"] if i["actor"] == "self")
        self.assertEqual(own["status"], "offered")

    async def test_confirmed_speech_is_shared_once_and_available_to_peer_planning(self):
        event, _ = await self.case.exchange()
        scope = event.get_extra(fixtures.TURN)["scope"]
        _, peer = self.install("self_amusement")
        packets = []
        peer.companion_observe = packets.append
        self.plugin.link.connect()
        with self.plugin.store.db:
            self.plugin.store.db.execute("UPDATE scopes SET last_human=? WHERE id=?", (time.time() - 90000, scope))
        await self.plugin.memory.refresh(scope)
        self.plugin.config.update(
            proactive_enabled=True, proactive_origins=[event.unified_msg_origin], quiet_start=0, quiet_end=0
        )
        message = "上次说在练琴，那首曲子后来练顺了吗？"
        self.case.context.llm_generate.return_value = SimpleNamespace(completion_text=message)
        await self.plugin.curiosity_step(self.plugin.memory.states[scope])
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0]["kind"], "initiative")
        self.assertEqual(packets[0]["data"]["text"], message)
        background = peer.link.context_for(event.unified_msg_origin)["self_cognition"]
        self.assertIn(message, background["recent_own_messages"])
        self.plugin.link.sent(event.unified_msg_origin, message, packets[0]["id"])
        self.assertEqual(len(packets), 1)
        self.plugin.config["link_self_amusement"] = False
        self.plugin.link.sent(event.unified_msg_origin, "这条不应联动。", "disabled-link")
        self.assertEqual(len(packets), 1)

    async def test_failed_platform_send_does_not_claim_speech_to_companions(self):
        event, _ = await self.case.exchange()
        scope = event.get_extra(fixtures.TURN)["scope"]
        _, peer = self.install("self_amusement")
        packets = []
        peer.companion_observe = packets.append
        self.plugin.link.connect()
        with self.plugin.store.db:
            self.plugin.store.db.execute("UPDATE scopes SET last_human=? WHERE id=?", (time.time() - 90000, scope))
        await self.plugin.memory.refresh(scope)
        self.plugin.config.update(
            proactive_enabled=True, proactive_origins=[event.unified_msg_origin], quiet_start=0, quiet_end=0
        )
        self.case.context.llm_generate.return_value = SimpleNamespace(completion_text="那首曲子后来怎么样？")
        self.case.context.send_message.return_value = False
        await self.plugin.curiosity_step(self.plugin.memory.states[scope])
        self.assertEqual(packets, [])
        self.assertEqual(self.plugin.companion_snapshot(event.unified_msg_origin)["recent_own_messages"], [])
