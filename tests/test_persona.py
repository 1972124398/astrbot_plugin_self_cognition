import asyncio
import json
import time
import unittest
from types import SimpleNamespace

import test_hooks as fixtures
from astrbot_plugin_self_cognition.cognition import living_context, parse_reflection
from astrbot_plugin_self_cognition.memory import Memory


class RecallSelectionTests(unittest.TestCase):
    def setUp(self):
        self.memory = Memory(None)
        self.memory.views["room"] = self.memory.index(
            {
                "memories": [
                    {
                        "id": 1,
                        "content": "今天一起聊过练琴",
                        "subject": "alice",
                        "importance": 0.8,
                        "confidence": 0.9,
                        "updated": time.time(),
                        "cues": ["那首曲子", "弹顺了"],
                    }
                ],
                "experiences": [
                    {
                        "title": "练琴",
                        "story": "今天一起聊过钢琴练习",
                        "actors": ["alice"],
                        "cues": ["那首曲子", "弹顺了"],
                        "created": time.time(),
                    }
                ],
                "entities": [{"id": "bob", "name": "小明"}, {"id": "carol", "name": "小红"}],
                "relations": [{"source": "bob", "target": "carol", "label": "同事", "updated": time.time()}],
                "portraits": {},
                "all_questions": [],
            }
        )

    def test_unrelated_chat_does_not_force_old_stories_or_relationships(self):
        recalled = self.memory.recall("room", "alice", "今天午饭吃什么", conversation=True)
        self.assertEqual(recalled["memories"], [])
        self.assertEqual(recalled["experiences"], [])
        self.assertEqual(recalled["relations"], [])
        relevant = self.memory.recall("room", "alice", "终于弹顺了", conversation=True)
        self.assertEqual(len(relevant["memories"]), 1)
        self.assertEqual(len(relevant["experiences"]), 1)

    def test_background_reading_does_not_change_live_topic(self):
        self.memory.recall("room", "alice", "小明和小红现在还在一起工作吗", conversation=True)
        self.memory.recall("room", "alice", "后台回顾钢琴经历")
        self.assertIn("小明", self.memory.focus["room", "alice"][0])
        result = self.memory.recall("room", "alice", "后来他怎么样了", conversation=True)
        self.assertEqual(result["relations"][0]["source"], "bob")

    def test_same_open_thread_is_not_repeated_in_three_formats(self):
        context = {
            "portrait": {"unfinished": "还想知道演出怎么样"},
            "curiosity": [{"question": "演出怎么样"}],
            "intentions": [{"thought": "还想知道演出怎么样", "status": "pending", "occasion": "演出后"}],
        }
        lines = living_context(context, "小林")
        self.assertEqual(sum("演出怎么样" in line for line in lines), 1)

    def test_persona_can_describe_its_own_interest_without_becoming_care(self):
        data = parse_reflection(
            json.dumps(
                {
                    "intentions": [
                        {"actor": "$speaker", "topic": "古籍", "kind": "考据", "thought": "想接着讨论那本书的断句"}
                    ]
                },
                ensure_ascii=False,
            ),
            "那本古籍的断句有趣。",
        )
        self.assertEqual(data["intentions"][0]["kind"], "考据")
        plain = parse_reflection(
            json.dumps(
                {"intentions": [{"actor": "$speaker", "topic": "古籍", "thought": "有个新想法"}]}, ensure_ascii=False
            ),
            "古籍",
        )
        self.assertEqual(plain["intentions"][0]["kind"], "thought")


class ActualPersonaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = fixtures.Hooks("test_frontend_no_model_call_and_temp_context")
        await self.case.asyncSetUp()
        self.plugin, self.context = self.case.plugin, self.case.context

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_each_actual_persona_reaches_background_unchanged(self):
        prompts = {
            "reserved": "你是寡言的古书收藏者，不太关心别人的日常，只对古籍感兴趣。",
            "playful": "你是热爱比赛、喜欢夸口的冒险者，说话爽快。",
        }
        self.context.persona_manager.get_persona_v3_by_id = lambda key: {"prompt": prompts[key]}
        self.context.llm_generate.return_value = SimpleNamespace(completion_text="{}")
        self.plugin.config["behavior_preferences"] = ""
        for index, (key, prompt) in enumerate(prompts.items()):
            event = fixtures.Event("今天没什么特别的。")
            event.set_extra("_trace_persona_id", key)
            event.message_obj.message_id = "persona-" + str(index)
            _, request = await self.case.prepare(event)
            self.assertEqual(request.extra_user_content_parts, [])
            await self.plugin.response(event, SimpleNamespace(completion_text="嗯。", tools_call_args=[]))
            await self.plugin.delivered(event)
            await self.plugin.capture_queue.join()
            self.plugin.worker.cancel()
            await asyncio.gather(self.plugin.worker, return_exceptions=True)
            scope = event.get_extra(fixtures.TURN)["scope"]
            scene = await self.plugin.memory.call(self.plugin.store.next_batch, time.time(), [scope], 0)
            await self.plugin.process(scene)
            payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
            self.assertEqual(payload["persona"], prompt)
            self.assertEqual(self.plugin.store.stats(scope)["feelings"], 0)
            data = self.plugin.store.dashboard(scope)
            self.assertEqual(data["character"], {})
            self.assertEqual(data["experiences"], [])
            self.assertEqual(data["intentions"], [])

    async def test_old_builtin_style_is_ignored_but_user_preference_is_kept(self):
        self.plugin.config["behavior_preferences"] = (
            "像自己一样相处，有自己的喜好、玩心和小脾气；熟悉后自然接梗、惦记共同经历。想问时再问，不用每轮都热情，也不需要反复说我记得。"
        )
        self.assertEqual(self.plugin.expression_preferences(), "")
        self.plugin.config["behavior_preferences"] = "少用动作描写，保留自己的简短口吻。"
        self.assertEqual(self.plugin.expression_preferences(), "少用动作描写，保留自己的简短口吻。")

    async def test_chatui_default_uses_real_persona_instead_of_injected_background(self):
        from astrbot.core.astr_main_agent_resources import CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT

        event = fixtures.Event()
        event.set_extra("_trace_persona_id", "_chatui_default_")
        self.assertEqual(
            self.plugin.persona_prompt(event, SimpleNamespace(system_prompt="别的插件注入了很热情的资料")),
            CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT,
        )
        event.set_extra("enable_default_system_prompt", False)
        self.assertEqual(self.plugin.persona_prompt(event, SimpleNamespace(system_prompt="其他资料")), "")
