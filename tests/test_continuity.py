import json
import time
import unittest
from types import SimpleNamespace

import test_hooks as fixtures
from astrbot_plugin_self_cognition.cognition import living_context
from astrbot_plugin_self_cognition.memory import Memory


class SelfContinuityTests(unittest.TestCase):
    def test_open_question_can_use_learned_interest_without_retelling_old_event(self):
        memory = Memory(None)
        memory.views["room"] = memory.index(
            {
                "memories": [],
                "entities": [],
                "relations": [],
                "experiences": [],
                "portraits": {},
                "all_questions": [],
                "character": {"interests": ["潮间带植物", "古籍"], "outlook": "上次看文章时，觉得那片海滩很有趣。"},
            }
        )
        result = memory.recall("room", "alice", "你现在想做什么", conversation=True)
        note = "\n".join(living_context(result, "Alice"))
        self.assertIn("潮间带植物", note)
        self.assertNotIn("古籍", note)
        self.assertNotIn("上次看文章", note)
        direct = "\n".join(
            living_context(memory.recall("room", "alice", "潮间带植物后来了解多少了", conversation=True), "Alice")
        )
        self.assertEqual(direct.count("潮间带植物"), 1)


class ConsiderationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = fixtures.Hooks("test_frontend_no_model_call_and_temp_context")
        await self.case.asyncSetUp()
        self.plugin, self.context = self.case.plugin, self.case.context

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_waiting_lets_older_thoughts_surface_without_completing_them(self):
        event, _ = await self.case.exchange()
        scope, actor = event.get_extra(fixtures.TURN)["scope"], event.get_extra(fixtures.TURN)["actor"]
        with self.plugin.store.db:
            self.plugin.store.db.execute("UPDATE scopes SET last_human=? WHERE id=?", (time.time() - 90000, scope))
            for index in range(4):
                self.plugin.store.db.execute(
                    "INSERT INTO intentions(scope,actor,topic,kind,thought,occasion,updated) VALUES(?,?,?,?,?,?,?)",
                    (scope, actor, "念头" + str(index), "share", "想说的话" + str(index), "闲下来时", 100 + index),
                )
        await self.plugin.memory.refresh(scope)
        self.plugin.config.update(
            proactive_enabled=True, proactive_origins=[event.unified_msg_origin], quiet_start=0, quiet_end=0
        )
        batches = []

        async def wait(**kwargs):
            batches.append([idea["topic"] for idea in json.loads(kwargs["prompt"])["choices"]])
            return SimpleNamespace(completion_text="WAIT")

        self.context.llm_generate.side_effect = wait
        await self.plugin.curiosity_step(self.plugin.memory.states[scope])
        self.assertNotIn("念头0", batches[0])
        self.plugin.initiative_wait.clear()  # The next eligible check, without sleeping an hour.
        await self.plugin.curiosity_step(self.plugin.memory.states[scope])
        self.assertIn("念头0", batches[1])
        self.assertEqual({idea["status"] for idea in self.plugin.store.dashboard(scope)["intentions"]}, {"pending"})
        self.context.send_message.assert_not_awaited()
        self.assertEqual(len(batches), 2)  # One existing generation call per check.
