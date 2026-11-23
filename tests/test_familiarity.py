import asyncio
import json
import unittest
from types import SimpleNamespace

import test_hooks as fixtures
from astrbot_plugin_self_cognition.cognition import living_context
from astrbot_plugin_self_cognition.memory import Memory


class FamiliarityTests(unittest.TestCase):
    def setUp(self):
        self.memory = Memory(None)
        self.memory.views["r"] = self.memory.index(
            {
                "memories": [
                    {
                        "id": 1,
                        "content": "不吃香菜",
                        "subject": "a",
                        "kind": "preference",
                        "importance": 0.9,
                        "confidence": 0.9,
                        "updated": 100,
                    },
                    {
                        "id": 2,
                        "content": "一起聊过钢琴",
                        "subject": "a",
                        "kind": "episode",
                        "importance": 0.8,
                        "confidence": 0.9,
                        "updated": 100,
                    },
                ],
                "entities": [{"id": "ming", "name": "小明"}, {"id": "hong", "name": "小红"}],
                "relations": [{"source": "ming", "target": "hong", "label": "同事", "updated": 100}],
                "experiences": [],
                "portraits": {},
                "all_questions": [],
            }
        )

    def test_implicit_dinner_question_keeps_small_preferences_without_old_stories(self):
        recalled = self.memory.recall("r", "a", "晚饭做什么", conversation=True)
        prompt = "\n".join(living_context(recalled, "小林"))
        self.assertIn("不吃香菜", prompt)
        self.assertNotIn("钢琴", prompt)
        self.assertNotIn("小明", prompt)
        self.assertEqual(recalled["memories"], [])

    def test_explicit_topic_change_clears_previous_person_even_with_how_question(self):
        self.memory.recall("r", "a", "小明最近被辞退了", conversation=True)
        result = self.memory.recall("r", "a", "换个话题，今天怎么样", conversation=True)
        self.assertNotIn("小明", self.memory.focus["r", "a"][0])
        self.assertEqual(result["relations"], [])
        continued = self.memory.recall("r", "a", "那后来呢", conversation=True)
        self.assertNotIn("小明", self.memory.focus["r", "a"][0])
        self.assertEqual(continued["relations"], [])

    def test_directly_recalled_preference_is_not_repeated_as_familiar_background(self):
        result = self.memory.recall("r", "a", "要放香菜吗", conversation=True)
        prompt = "\n".join(living_context(result, "小林"))
        self.assertEqual(prompt.count("不吃香菜"), 1)


class CorrectionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = fixtures.Hooks("test_frontend_no_model_call_and_temp_context")
        await self.case.asyncSetUp()
        self.plugin, self.context = self.case.plugin, self.case.context

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_one_background_response_can_correct_fact_portrait_and_old_intention(self):
        original = {
            "memories": [
                {
                    "subject": "$speaker",
                    "kind": "preference",
                    "key": "drink",
                    "content": "喜欢无糖茶",
                    "evidence": "我喜欢无糖茶",
                }
            ],
            "portrait": {"impression": "喜欢无糖茶。"},
            "intentions": [{"actor": "$speaker", "topic": "买茶", "thought": "下次聊聊买茶", "kind": "ask"}],
        }
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text=json.dumps(original, ensure_ascii=False)
        )
        previous, _ = await self.case.exchange()
        actor, scope = previous.get_extra(fixtures.TURN)["actor"], previous.get_extra(fixtures.TURN)["scope"]

        async def revise(**kwargs):
            payload = json.loads(kwargs["prompt"])
            self.assertIn("喜欢无糖茶", payload["people_known"][actor]["impression"])
            self.assertIn("drink", {m["slot"] for m in payload["known"]["memories"]})
            return SimpleNamespace(
                completion_text=json.dumps(
                    {
                        "memories": [
                            {
                                "subject": "$speaker",
                                "kind": "preference",
                                "key": "drink",
                                "content": "现在只喜欢咖啡",
                                "evidence": "只喜欢咖啡",
                            }
                        ],
                        "people": [{"actor": actor, "portrait": {"impression": "现在偏爱咖啡。"}}],
                        "intentions": [{"actor": actor, "topic": "买茶", "status": "dropped"}],
                    },
                    ensure_ascii=False,
                )
            )

        self.context.llm_generate.side_effect = revise
        event = fixtures.Event("我现在只喜欢咖啡，不喝茶了。")
        event.message_obj.message_id = "correction"
        await self.case.prepare(event)
        await self.plugin.response(event, SimpleNamespace(completion_text="记得了。", tools_call_args=[]))
        await self.plugin.delivered(event)
        await self.plugin.capture_queue.join()
        self.plugin.worker.cancel()
        await asyncio.gather(self.plugin.worker, return_exceptions=True)
        batch = await self.plugin.memory.call(self.plugin.store.next_batch, 9999999999, [scope], 0)
        await self.plugin.process(batch)
        result = self.plugin.memory.recall(scope, actor, "喝点什么", conversation=True)
        prompt = "\n".join(living_context(result, "Alice"))
        self.assertIn("咖啡", prompt)
        self.assertNotIn("喜欢无糖茶", prompt)
        self.assertEqual(self.plugin.store.dashboard(scope)["intentions"], [])
        self.assertEqual(self.plugin.store.stats(scope)["memories"], 2)  # Historical fact is retained.
