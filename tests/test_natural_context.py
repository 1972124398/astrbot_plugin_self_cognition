import json
import time
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

import test_hooks as fixtures
import test_life as life_fixtures
from astrbot_plugin_self_cognition.cognition import living_context, choose_initiative
from astrbot_plugin_self_cognition.memory import Memory
from astrbot_plugin_self_cognition.store import Store


class NaturalContextTests(unittest.TestCase):
    def test_old_story_and_self_analysis_do_not_enter_everyday_chat(self):
        cache = Memory(None)
        cache.views["room"] = cache.index(
            {
                "memories": [],
                "entities": [],
                "relations": [],
                "experiences": [],
                "all_questions": [],
                "portraits": {
                    "alice": {
                        "impression": "已经熟悉。",
                        "style": "可以直接说自己的想法。",
                        "shared_story": "一起聊过钢琴。",
                    },
                    "self": {"impression": "弹琴让我有些新感受。"},
                },
                "character": {
                    "interests": ["钢琴曲"],
                    "preferences": ["听人弹琴"],
                    "outlook": "最近在琢磨一首钢琴曲。",
                },
            }
        )
        lunch = "\n".join(living_context(cache.recall("room", "alice", "午饭吃什么", conversation=True), "Alice"))
        self.assertNotIn("钢琴", lunch)
        self.assertNotIn("新感受", lunch)
        self.assertIn("已经熟悉", lunch)
        self.assertIn("直接说自己的想法", lunch)
        piano = "\n".join(living_context(cache.recall("room", "alice", "最近那首钢琴曲呢", conversation=True), "Alice"))
        self.assertIn("钢琴", piano)
        self.assertEqual(cache.recall("room", "alice")["portrait"]["shared_story"], "一起聊过钢琴。")

    def test_record_update_time_is_not_claimed_as_event_time(self):
        lines = living_context({"memories": [{"content": "2020年一起玩过接龙。", "updated": 2051222400}]}, "Alice")
        self.assertIn("2020年", lines[0])
        self.assertNotIn("2035", lines[0])

    def test_observation_and_own_activities_are_not_called_shared_participation(self):
        context = {
            "experiences": [
                {"story": "小明安慰了小红。", "source_kind": "observed"},
                {"story": "自己读了一篇文章。", "source_kind": "companion"},
                {"story": "和小林一起玩过接龙。", "source_kind": "dialogue"},
                {"story": "以前留下的记录。"},
            ]
        }
        lines = living_context(context, "Alice")
        self.assertTrue(lines[0].startswith("听他们聊过"))
        self.assertTrue(lines[1].startswith("自己的活动经历"))
        self.assertTrue(lines[2].startswith("相处片段"))
        self.assertTrue(lines[3].startswith("记得一段往事"))

    def test_model_can_choose_older_idea_or_wait_without_consuming_others(self):
        ideas = [{"choice": "intention:2", "id": 2}, {"choice": "intention:1", "id": 1}]
        selected, message = choose_initiative('{"choice":"intention:1","message":"刚想起上次那个故事。"}', ideas)
        self.assertEqual(selected["id"], 1)
        self.assertEqual(message, "刚想起上次那个故事。")
        self.assertEqual(choose_initiative("WAIT", ideas), (None, ""))


class ExperienceSourceTests(unittest.TestCase):
    job = life_fixtures.LifeTests.job
    apply = life_fixtures.LifeTests.apply

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "life.sqlite3"
        self.store = Store(self.path)
        self.store.ensure_scope("room", "test:GroupMessage:1", "friend")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_source_survives_storage_and_old_records_migrate_without_guessing(self):
        job = self.job("heard", human="小明和小红聊了一会儿。", kind="observed")
        self.apply(job, {"experience": {"story": "听到小明和小红聊天。"}})
        self.assertEqual(self.store.dashboard("room")["experiences"][0]["source_kind"], "observed")
        with self.store.db:
            self.store.db.execute("ALTER TABLE experiences DROP COLUMN source_kind")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.dashboard("room")["experiences"][0]["source_kind"], "legacy")


class InitiativeChoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = fixtures.Hooks("test_frontend_no_model_call_and_temp_context")
        await self.case.asyncSetUp()
        self.plugin, self.context = self.case.plugin, self.case.context

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_curiosity_off_preserves_sharing_and_marks_only_the_selected_idea(self):
        event, _ = await self.case.exchange()
        scope, actor = event.get_extra(fixtures.TURN)["scope"], event.get_extra(fixtures.TURN)["actor"]
        with self.plugin.store.db:
            self.plugin.store.db.execute("UPDATE scopes SET last_human=?", (time.time() - 90000,))
            for index, (topic, kind) in enumerate((("旧故事", "share"), ("新故事", "share"), ("想问的问题", "ask"))):
                self.plugin.store.db.execute(
                    "INSERT INTO intentions(scope,actor,topic,kind,thought,occasion,updated) VALUES(?,?,?,?,?,?,?)",
                    (scope, actor, topic, kind, topic, "闲下来时", time.time() + index),
                )
        await self.plugin.memory.refresh(scope)
        self.plugin.config.update(
            curiosity_enabled=False,
            proactive_enabled=True,
            proactive_origins=[event.unified_msg_origin],
            quiet_start=0,
            quiet_end=0,
        )

        async def choose(**kwargs):
            payload = json.loads(kwargs["prompt"])
            self.assertEqual({idea["topic"] for idea in payload["choices"]}, {"旧故事", "新故事"})
            self.assertEqual(payload["memory"]["curiosity"], [])
            chosen = next(idea for idea in payload["choices"] if idea["topic"] == "旧故事")
            return SimpleNamespace(
                completion_text=json.dumps(
                    {"choice": chosen["choice"], "message": "上次那个故事，我想到个结尾。"}, ensure_ascii=False
                )
            )

        self.context.llm_generate.side_effect = choose
        await self.plugin.curiosity_step(self.plugin.memory.states[scope])
        self.context.send_message.assert_awaited_once()
        states = {row["topic"]: row["status"] for row in self.plugin.store.dashboard(scope)["intentions"]}
        self.assertEqual(states, {"旧故事": "sent", "新故事": "pending", "想问的问题": "pending"})
