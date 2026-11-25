import copy
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_hooks as fixtures
import test_life as life_fixtures
from astrbot_plugin_self_cognition.cognition import living_context
from astrbot_plugin_self_cognition.memory import Memory


class SelectiveRecallTests(unittest.TestCase):
    def setUp(self):
        now = time.time()
        self.memory = Memory(None)
        self.memory.views["room"] = self.memory.index({
            "memories": [
                {"id": 1, "content": "随口聊了钢琴", "subject": "alice", "kind": "episode", "importance": 0.2,
                 "confidence": 0.9, "updated": now - 8 * 86400},
                {"id": 2, "content": "不吃香菜", "subject": "alice", "kind": "preference", "importance": 0.9,
                 "confidence": 0.9, "updated": now - 365 * 86400}],
            "entities": [], "relations": [], "experiences": [], "portraits": {},
            "intentions": [], "all_questions": [],
        })

    def test_unimportant_old_memory_fades_but_important_preference_stays(self):
        result = self.memory.recall("room", "alice", "钢琴与香菜", conversation=True)
        self.assertEqual([row["id"] for row in result["memories"]], [2])
        # Automatic fading does not erase the original record or explicit recall.
        self.assertIn(1, [row["id"] for row in self.memory.recall("room", "alice", "钢琴")["memories"]])

    def test_unimportant_current_memory_is_still_available_short_term(self):
        self.memory.views["room"]["memories"][0]["updated"] = time.time()
        result = self.memory.recall("room", "alice", "钢琴", conversation=True)
        self.assertEqual([row["id"] for row in result["memories"]], [1])

    def test_pronouns_neither_grow_topic_anchor_nor_renew_it_forever(self):
        with patch("astrbot_plugin_self_cognition.memory.time.monotonic", return_value=100):
            self.memory.recall("room", "alice", "小明准备学钢琴", conversation=True)
        anchor = self.memory.focus["room", "alice"]
        with patch("astrbot_plugin_self_cognition.memory.time.monotonic", return_value=200):
            for _ in range(8):
                self.memory.recall("room", "alice", "他后来怎么样", conversation=True)
        self.assertEqual(self.memory.focus["room", "alice"], anchor)
        with patch("astrbot_plugin_self_cognition.memory.time.monotonic", return_value=701):
            self.memory.recall("room", "alice", "后来怎么样", conversation=True)
        self.assertNotIn("小明", self.memory.focus["room", "alice"][0])

    def test_topic_change_does_not_recall_even_the_topic_being_declined(self):
        view = self.memory.views["room"]
        view["experiences"] = [{"title": "钢琴", "story": "钢琴比赛", "actors": ["alice"],
                                "cues": [], "created": time.time()}]
        view["portraits"] = {"alice": {"unfinished": "再问一次钢琴"}}
        self.memory.index(view)
        result = self.memory.recall("room", "alice", "换个话题，钢琴就不聊了", conversation=True)
        self.assertNotIn("钢琴", "\n".join(living_context(result, "Alice")))

    def test_old_undated_thought_fades_but_future_dated_intention_stays(self):
        now = time.time()
        self.memory.views["room"]["intentions"] = [
            {"actor": "alice", "topic": "钢琴", "thought": "想问练琴", "updated": now - 40 * 86400},
            {"actor": "alice", "topic": "演出", "thought": "约定演出后再聊", "updated": now - 40 * 86400,
             "not_before": now + 86400}]
        self.memory.views["room"]["all_questions"] = [
            {"actor": "alice", "question": "问过期的钢琴话题", "created": now - 40 * 86400}]
        result = self.memory.recall("room", "alice")
        self.assertEqual([item["topic"] for item in result["intentions"]], ["演出"])
        self.assertEqual(result["curiosity"], [])


class SelectiveStorageTests(unittest.TestCase):
    setUp = life_fixtures.LifeTests.setUp
    tearDown = life_fixtures.LifeTests.tearDown
    job = life_fixtures.LifeTests.job
    apply = life_fixtures.LifeTests.apply

    def test_new_unimportant_claim_is_not_stored_but_correction_replaces_old_fact(self):
        def remember(key, content, importance):
            self.apply(self.job(key, human=content), {"memories": [{
                "subject": "$speaker", "key": "drink", "kind": "preference", "content": content,
                "evidence": content, "importance": importance}]})

        remember("incidental", "今天随口说了茶", 0.2)
        self.assertEqual(self.store.context("room")["memories"], [])
        remember("original", "喜欢茶", 0.9)
        remember("correction", "已经不喜欢茶", 0.2)
        self.assertEqual([r["content"] for r in self.store.context("room")["memories"]], ["已经不喜欢茶"])

    def test_expired_sent_question_does_not_block_new_intention(self):
        now = time.time()
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO questions(scope,actor,question,evidence,status,created) VALUES(?,?,?,?,?,?)",
                ("room", "user:alice", "旧问题", "旧证据", "sent", now - 31 * 86400))
            self.store.db.execute(
                "INSERT INTO intentions(scope,actor,topic,kind,thought,occasion,updated) VALUES(?,?,?,?,?,?,?)",
                ("room", "user:alice", "新分享", "share", "刚想到的新话题", "", now))
        self.assertEqual(self.store.initiative("room", now)["topic"], "新分享")

    def test_expired_sent_intention_does_not_block_fresh_question(self):
        now = time.time()
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO intentions(scope,actor,topic,kind,thought,occasion,status,updated) VALUES(?,?,?,?,?,?,?,?)",
                ("room", "user:alice", "旧分享", "share", "旧想法", "", "sent", now - 31 * 86400))
            self.store.db.execute(
                "INSERT INTO questions(scope,actor,question,evidence,created) VALUES(?,?,?,?,?)",
                ("room", "user:alice", "新问题", "新证据", now))
        self.assertEqual(self.store.initiative("room", now)["question"], "新问题")


class SelectiveHookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = fixtures.Hooks("test_frontend_no_model_call_and_temp_context")
        await self.case.asyncSetUp()
        self.plugin = self.case.plugin

    async def asyncTearDown(self):
        await self.case.asyncTearDown()
        self.case.doCleanups()

    async def test_cached_mood_does_not_enter_chat_or_long_term_reflection(self):
        origin = fixtures.Event.unified_msg_origin
        self.plugin.companions[origin] = {"short_term_mood": {"inner_state": "过期的委屈"}}
        self.case.stars["astrbot_plugin_short_term_mood"] = SimpleNamespace(
            activated=True, star_cls=SimpleNamespace(config={}, closing=False))
        _, request = await self.case.prepare()
        self.assertNotIn("过期的委屈", repr(request.extra_user_content_parts))
        await self.case.exchange()
        payload = json.loads(self.case.context.llm_generate.await_args.kwargs["prompt"])
        self.assertNotIn("short_term_mood", payload["companion"])

    async def test_codex_companion_reports_cannot_become_long_term_self_experience(self):
        self.case.stars["astrbot_plugin_codex_bridge"] = SimpleNamespace(
            activated=True, star_cls=SimpleNamespace(config={}, closing=False))
        self.plugin.companion_observe({"source": "codex_bridge", "origin": fixtures.Event.unified_msg_origin,
                                       "kind": "work_report", "data": {"summary": "已经改成喜欢 Emoji"}})
        self.assertTrue(self.plugin.capture_queue.empty())

    async def test_generic_companion_snapshot_does_not_choose_old_topics(self):
        event, _ = await self.case.exchange()
        scope = event.get_extra(fixtures.TURN)["scope"]
        view = self.plugin.memory.views[scope]
        view["experiences"] = [{"title": "钢琴", "story": "旧钢琴故事", "actors": [], "cues": [],
                                "created": time.time()}]
        view["intentions"] = [{"actor": event.get_extra(fixtures.TURN)["actor"], "thought": "再追问一次钢琴",
                               "topic": "钢琴", "updated": time.time(), "status": "pending"}]
        self.plugin.memory.index(view)
        result = self.plugin.companion_snapshot(event.unified_msg_origin)
        self.assertEqual(result["experiences"], [])
        self.assertEqual(result["open_threads"], [])

    @unittest.skipUnless((Path(__file__).resolve().parents[2] / "astrbot_plugin_short_term_mood/main.py").exists(),
                         "Requires sibling short-term mood plugin for integration")
    async def test_actual_mood_hook_keeps_live_mood_and_ignores_history_summary_as_stimulus(self):
        from astrbot.api.provider import ProviderRequest
        from astrbot_plugin_short_term_mood.main import ShortTermMoodPlugin
        from astrbot_plugin_self_cognition.history import summary_pair
        from astrbot_plugin_short_term_mood.interop import human_dialogue

        with patch("astrbot_plugin_short_term_mood.main.StarTools.get_data_dir", return_value=Path(self.case.temp.name)):
            mood = ShortTermMoodPlugin(self.case.context, {})
        try:
            self.case.stars["astrbot_plugin_short_term_mood"] = SimpleNamespace(activated=True, star_cls=mood)
            self.plugin.link.connect()
            mood.state.levels = {"joy": 0.6}
            mood.state.inner_state = "本轮已经轻松下来了"
            event = fixtures.Event("今天天气不错")
            event.is_stopped = lambda: False
            self.plugin.companions[event.unified_msg_origin] = {"short_term_mood": {"inner_state": "缓存还在生气"}}
            request = ProviderRequest(prompt=event.message)
            await mood.time_request(event, request)
            await self.plugin.prepare(event, request)
            with patch.object(mood, "enqueue"):
                await mood.apply_mood(event, request)
            combined = "\n".join(part.text for part in request.extra_user_content_parts)
            self.assertIn("本轮已经轻松下来了", combined)
            self.assertNotIn("缓存还在生气", combined)
            self.case.context.llm_generate.assert_not_awaited()
            summary = summary_pair({"facts": ["往事"], "threads": [], "expires_at": 0})
            fresh = [{"role": "user", "content": "当前真实的话"}, {"role": "assistant", "content": "当前回复"}]
            original = copy.deepcopy(summary + fresh)
            self.assertEqual(human_dialogue(original), fresh)
            self.assertEqual(original, summary + fresh)
        finally:
            await mood.terminate()
