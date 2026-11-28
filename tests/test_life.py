import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_hooks as fixtures
from astrbot_plugin_self_cognition.main import ObservedGroup
from astrbot_plugin_self_cognition.cognition import parse_reflection, actor_key
from astrbot_plugin_self_cognition.memory import Memory
from astrbot_plugin_self_cognition.store import Store


class LifeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "life.sqlite3")
        self.store.ensure_scope("room", "test:GroupMessage:1", "friend")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def job(self, key, actor="user:alice", name="小林", human="今天继续练琴。", kind="dialogue"):
        job = {
            "id": key,
            "scope": "room",
            "generation": 0,
            "actor": actor,
            "name": name,
            "origin": "test:GroupMessage:1",
            "source_kind": kind,
            "human": human,
            "reply": "好呀。",
            "persona": "朋友",
            "provider_id": "",
            "captured": time.time(),
        }
        self.store.capture(job)
        return job

    def apply(self, job, data, known=()):
        parsed = parse_reflection(json.dumps(data, ensure_ascii=False), job["human"], known_entities=known)
        self.assertTrue(self.store.apply(job, parsed))

    def test_whole_scene_keeps_speakers_separate(self):
        self.job("a", human="我练琴卡住了。")
        self.job("b", "user:bob", "小明", "我陪你慢慢练。")
        scene = self.store.next_batch(time.time(), ["room"], 0)
        self.assertEqual([t["actor"] for t in scene["turns"]], ["user:alice", "user:bob"])
        self.apply(
            scene,
            {
                "experience": {"title": "一起练琴", "story": "小林练琴卡住，小明愿意陪他一起练。", "cues": ["弹顺了"]},
                "people": [
                    {
                        "actor": "user:alice",
                        "portrait": {"impression": "愿意坦白遇到困难"},
                        "feeling": {"affection": 1, "evidence": "练琴卡住了"},
                    },
                    {
                        "actor": "user:bob",
                        "portrait": {"impression": "会主动陪伴朋友"},
                        "feeling": {"affection": 2, "evidence": "陪你慢慢练"},
                    },
                ],
            },
            scene["speakers"],
        )
        data = self.store.dashboard("room")
        feelings = {e["id"]: e["affection"] for e in data["entities"]}
        self.assertEqual(feelings, {"user:alice": 1, "user:bob": 2})
        self.assertEqual(len(data["experiences"]), 1)
        self.assertIsNone(self.store.next_job(time.time()))

    def test_character_continues_without_resetting_unmentioned_preferences(self):
        self.apply(self.job("a"), {"character": {"preferences": ["喜欢一起编故事"], "habits": ["先听对方讲完"]}})
        self.apply(self.job("b"), {"self_reflection": "今天有点想安静。", "character": {"interests": ["钢琴曲"]}})
        character = self.store.dashboard("room")["character"]
        self.assertEqual(character["preferences"], ["喜欢一起编故事"])
        self.assertEqual(character["habits"], ["先听对方讲完"])
        self.assertEqual(character["interests"], ["钢琴曲"])

    def test_semantic_cues_recall_old_event_with_different_words(self):
        self.apply(
            self.job("a"),
            {
                "memories": [
                    {
                        "subject": "$speaker",
                        "kind": "episode",
                        "key": "piano",
                        "content": "一起聊过练琴遇到的难处。",
                        "evidence": "练琴",
                        "cues": ["终于弹顺了", "那首曲子"],
                    }
                ],
                "experience": {"story": "小林最近在学钢琴，我陪他聊过卡住的地方。", "cues": ["终于弹顺了"]},
            },
        )
        state, view = self.store.read_view("room")
        cache = Memory(self.store)
        cache.states["room"], cache.views["room"] = state, view
        recalled = cache.recall("room", "user:alice", "终于弹顺了")
        self.assertEqual(recalled["memories"][0]["slot"], "piano")
        self.assertIn("钢琴", recalled["experiences"][0]["story"])

    def test_intention_is_due_then_offered_and_completed(self):
        known = [{"id": "user:alice", "name": "小林"}]
        self.apply(
            self.job("a"),
            {
                "intentions": [
                    {
                        "actor": "user:alice",
                        "topic": "演出",
                        "kind": "care",
                        "thought": "想听他讲演出怎么样",
                        "occasion": "演出结束后",
                        "not_before": "2099-01-01T00:00:00+08:00",
                    }
                ]
            },
            known,
        )
        self.assertIsNone(self.store.initiative("room", time.time()))
        with self.store.db:
            self.store.db.execute("UPDATE intentions SET not_before=0")
        idea = self.store.initiative("room", time.time())
        self.assertEqual(idea["kind"], "care")
        self.store.mark_intention("room", idea["id"], "演出怎么样？", time.time())
        self.assertIsNone(self.store.initiative("room", time.time()))
        self.store.touch("room", "user:alice", "小林", time.time())
        self.assertEqual(self.store.dashboard("room")["intentions"][0]["status"], "offered")
        self.apply(self.job("b"), {"intentions": [{"actor": "user:alice", "topic": "演出", "status": "done"}]}, known)
        self.assertEqual(self.store.dashboard("room")["intentions"], [])

    def test_explicit_alias_and_conflicting_views_remain_distinct(self):
        first = self.job("a", human="小红和小明还是同事，明哥就是小明。")
        self.apply(
            first,
            {
                "relations": [{"source": "小红", "target": "小明", "labels": ["同事"], "evidence": "还是同事"}],
                "aliases": [{"entity": "小明", "names": ["明哥"]}],
            },
        )
        second = self.job("b", "user:bob", "小张", "小红和明哥早就不是同事了。")
        self.apply(
            second,
            {"relations": [{"source": "小红", "target": "明哥", "ended_labels": ["同事"], "evidence": "早就不是同事"}]},
        )
        relations = self.store.dashboard("room")["relations"]
        self.assertEqual(len(relations), 1)
        self.assertTrue(relations[0]["disputed"])
        self.assertEqual({r["reporter"] for r in relations[0]["perspectives"]}, {"user:alice", "user:bob"})

    def test_passive_observation_upgrades_to_reply_without_duplicate_experience(self):
        job = self.job("a", kind="observed")
        self.assertTrue(self.store.capture(dict(job, source_kind="dialogue", reply="我陪你练。")))
        self.assertEqual(self.store.stats("room")["pending"], 1)
        self.assertEqual(self.store.next_batch(time.time(), ["room"], 0)["turns"][0]["reply"], "我陪你练。")

    def test_own_promise_is_remembered_as_own_words(self):
        job = self.job("promise", human="下次我们还能接着编故事吗？")
        job["reply"] = "下次我陪你接着编。"
        parsed = parse_reflection(
            json.dumps(
                {
                    "memories": [
                        {
                            "subject": "$self",
                            "kind": "self",
                            "key": "promise",
                            "content": "我答应下次陪小林继续编故事。",
                            "evidence_source": "self",
                            "evidence": "我陪你接着编",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            job["human"],
            reply=job["reply"],
        )
        self.store.apply(job, parsed)
        memory = self.store.context("room")["memories"][0]
        self.assertTrue(memory["source"].startswith("own_words:"))
        self.assertIn("答应", memory["content"])

    def test_long_lived_character_and_experiences_survive_restart(self):
        self.apply(
            self.job("a"), {"experience": {"story": "今天一起练琴。"}, "character": {"preferences": ["听对方慢慢弹完"]}}
        )
        path = Path(self.temp.name) / "life.sqlite3"
        self.store.close()
        self.store = Store(path)
        self.assertEqual(self.store.dashboard("room")["character"]["preferences"], ["听对方慢慢弹完"])
        self.assertEqual(self.store.dashboard("room")["experiences"][0]["story"], "今天一起练琴。")
        self.store.clear("room")
        data = self.store.dashboard("room")
        self.assertEqual(data["experiences"], [])
        self.assertEqual(data["character"], {})


class RuntimeLifeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = fixtures.Hooks("test_frontend_no_model_call_and_temp_context")
        await self.case.asyncSetUp()
        self.plugin, self.context = self.case.plugin, self.case.context

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_standalone_intention_can_initiate_without_a_question(self):
        event, _ = await self.case.exchange()
        scope, actor = event.get_extra(fixtures.TURN)["scope"], event.get_extra(fixtures.TURN)["actor"]
        with self.plugin.store.db:
            self.plugin.store.db.execute("DELETE FROM questions")
            self.plugin.store.db.execute("UPDATE scopes SET last_human=?", (time.time() - 90000,))
            self.plugin.store.db.execute(
                "INSERT INTO intentions(scope,actor,topic,kind,thought,occasion,updated) VALUES(?,?,?,?,?,?,?)",
                (scope, actor, "接着编故事", "share", "想到一个好玩的故事结尾，想分享", "闲下来时", time.time()),
            )
        await self.plugin.memory.refresh(scope)
        self.plugin.config.update(
            proactive_enabled=True, proactive_origins=[event.unified_msg_origin], quiet_start=0, quiet_end=0
        )
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text="我刚想到，上回那个故事结尾还能更好玩一点。"
        )
        await self.plugin.curiosity_step(self.plugin.memory.states[scope])
        self.context.send_message.assert_awaited_once()
        self.assertEqual(self.plugin.store.dashboard(scope)["intentions"][0]["status"], "sent")

    async def test_observation_filter_does_not_wake_unconfigured_conversations(self):
        check = ObservedGroup()
        event = fixtures.Event()
        self.assertFalse(check.filter(event, {}))
        event.unified_msg_origin = "test:GroupMessage:123"
        self.assertFalse(check.filter(event, {}))
        self.plugin.config["observation_origins"] = [event.unified_msg_origin]
        self.assertTrue(check.filter(event, {}))
        self.plugin.config["enabled"] = False
        self.assertFalse(check.filter(event, {}))

    async def test_group_observation_records_other_people_without_frontend_generation(self):
        event = fixtures.Event("先聊一句")
        event.unified_msg_origin = "test:GroupMessage:123"
        await self.case.prepare(event)
        await self.plugin.response(event, SimpleNamespace(completion_text="嗯。", tools_call_args=[]))
        await self.plugin.delivered(event)
        await self.plugin.capture_queue.join()
        self.plugin.worker.cancel()
        await asyncio.gather(self.plugin.worker, return_exceptions=True)
        self.plugin.config["observation_origins"] = [event.unified_msg_origin]
        for index, (who, words) in enumerate((("alice", "我今天有点难过"), ("bob", "我陪你坐一会儿"))):
            observed = fixtures.Event(words)
            observed.unified_msg_origin = event.unified_msg_origin
            observed.message_obj.message_id = "observation-" + str(index)
            observed.get_sender_id = lambda who=who: who
            observed.get_sender_name = lambda who=who: who
            await self.plugin.observe_group(observed)
        await self.plugin.capture_queue.join()
        self.context.llm_generate.assert_not_awaited()
        scene = await self.plugin.memory.call(
            self.plugin.store.next_batch, time.time(), list(self.plugin.memory.states), 0
        )
        self.assertEqual(
            {s["id"] for s in scene["speakers"]},
            {actor_key(event.unified_msg_origin, "alice"), actor_key(event.unified_msg_origin, "bob")},
        )

    async def test_independent_background_model_can_continue_during_live_chat(self):
        event, _ = await self.case.exchange()
        previous = event.get_extra(fixtures.TURN)
        pending = dict(previous, id="not-yet-reflected", reply="好的。")
        await self.plugin.memory.call(self.plugin.store.capture, pending)
        self.plugin.config.update(analysis_provider_id="background-model", reflection_idle_seconds=0)
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def provider(**kwargs):
            self.assertEqual(kwargs["chat_provider_id"], "background-model")
            entered.set()
            try:
                await release.wait()
                return SimpleNamespace(completion_text="{}")
            except asyncio.CancelledError:
                cancelled.set()
                raise

        self.context.llm_generate.side_effect = provider
        chat = fixtures.Event("先聊新的话题")
        chat.message_obj.message_id = "foreground-2"
        await self.case.prepare(chat)
        await asyncio.wait_for(entered.wait(), 2)
        next_chat = fixtures.Event("我还在说")
        next_chat.message_obj.message_id = "foreground-3"
        await self.case.prepare(next_chat)
        self.assertFalse(cancelled.is_set())
        release.set()
        await self.plugin.capture_queue.join()

    async def test_reflection_worker_retries_provider_and_queue_lookup_without_a_new_message(self):
        event, _ = await self.case.exchange()
        pending = dict(event.get_extra(fixtures.TURN), id="reflection-retry", reply="好。")
        await self.plugin.memory.call(self.plugin.store.capture, pending)
        self.plugin.config.update(analysis_provider_id="background-model", reflection_idle_seconds=0)
        self.plugin.frontend["foreground"] = time.monotonic() + 60
        self.plugin.front_origins["foreground"] = event.unified_msg_origin
        lookup = self.context.get_current_chat_provider_id
        lookup.reset_mock()
        lookup.side_effect = [RuntimeError("provider unavailable"), "test-provider", "test-provider", "test-provider"]
        next_batch = self.plugin.store.next_batch
        applied = asyncio.Event()
        process = self.plugin.process
        fail_read = True

        def read(*args):
            nonlocal fail_read
            if fail_read:
                fail_read = False
                raise OSError("read unavailable")
            return next_batch(*args)

        async def observe(job):
            result = await process(job)
            if result:
                applied.set()
            return result

        with (
            patch.object(self.plugin.store, "next_batch", side_effect=read),
            patch.object(self.plugin, "process", side_effect=observe),
        ):
            self.plugin.ensure_worker()
            worker = self.plugin.worker
            for error in ("RuntimeError", "OSError"):
                async with asyncio.timeout(1):
                    while not self.plugin.last_error.endswith(error) and not worker.done():
                        await asyncio.sleep(0.01)
                self.assertFalse(worker.done())
                self.plugin.wake.set()
            await asyncio.wait_for(applied.wait(), 2)
            self.assertIs(self.plugin.worker, worker)
            self.assertEqual(self.plugin.store.stats(pending["scope"])["pending"], 0)
