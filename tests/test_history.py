import asyncio
import copy
import json
import time
import unittest
from types import SimpleNamespace

import test_hooks as fixtures
from astrbot.api.provider import ProviderRequest
from astrbot_plugin_self_cognition.cognition import scope_key
from astrbot_plugin_self_cognition.history import (
    ACK,
    MARKER,
    align_times,
    encoded,
    extract_summary,
    parse_summary,
    project_history,
    summary_pair,
)


def turns(count=8, size=400):
    return [
        message
        for i in range(count)
        for message in (
            {"role": "user", "content": f"用户-{i}:" + "a" * size},
            {"role": "assistant", "content": f"回复-{i}:" + "b" * size},
        )
    ]


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = fixtures.Hooks("test_frontend_no_model_call_and_temp_context")
        await self.case.asyncSetUp()
        self.plugin, self.context = self.case.plugin, self.case.context
        self.plugin.config.update(history_char_limit=3000, history_recent_turns=2)
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text=json.dumps({"facts": ["Alice 喜欢无糖茶"], "threads": []})
        )
        self.event = fixtures.Event("聊个新话题")
        self.scope = scope_key(self.event.unified_msg_origin, "friend")
        self.state = self.plugin.memory.identity(self.scope, self.event.unified_msg_origin, "friend")
        await self.plugin.memory.call(
            self.plugin.store.ensure_scope, self.scope, self.event.unified_msg_origin, "friend"
        )

    async def asyncTearDown(self):
        await self.case.asyncTearDown()
        self.case.doCleanups()

    def request(self, messages=None, cid="conversation-1", updated=None):
        return ProviderRequest(
            prompt="新话题",
            contexts=messages if messages is not None else turns(),
            conversation=SimpleNamespace(cid=cid, updated_at=updated or time.time()),
        )

    async def prepare(self, request, event=None):
        await self.plugin.history.prepare(event or self.event, request, self.scope, self.state)

    async def finish(self):
        await asyncio.gather(*list(self.plugin.history.tasks.values()))

    async def test_background_summary_then_prefix_swap_preserves_new_tail_and_archives(self):
        request = self.request()
        original = copy.deepcopy(request.contexts)
        await self.prepare(request)
        self.assertEqual(request.contexts, original)
        await self.finish()
        fresh = turns(1, 10)
        fresh[0]["content"] = "刚刚新增的问题"
        request.contexts += fresh
        await self.prepare(request)
        self.assertIn(MARKER, request.contexts[0]["content"])
        self.assertEqual(request.contexts[1]["content"], ACK)
        self.assertEqual(request.contexts[-2:], fresh)
        self.assertLess(len(encoded(request.contexts)), len(encoded(original)))
        archive = self.plugin.store.db.execute("SELECT original FROM history_archives").fetchone()
        self.assertEqual(json.loads(archive[0]), original + fresh)

    async def test_failure_and_retry_cooldown_leave_original_intact(self):
        request = self.request()
        original = copy.deepcopy(request.contexts)
        self.context.llm_generate.side_effect = RuntimeError("offline")
        await self.prepare(request)
        await self.finish()
        await self.prepare(request)
        self.assertEqual(request.contexts, original)
        self.context.llm_generate.assert_awaited_once()
        self.assertIsNone(self.plugin.store.history_summary(self.scope, request.conversation.cid))

    async def test_idle_cleans_all_raw_turns_and_notifications_do_not_refresh_human_clock(self):
        self.plugin.config["history_char_limit"] = 16000
        request = self.request(turns(2, 10), updated=time.time() - 7200)
        self.event.set_extra("codex_bridge", {"kind": "task_result"})
        await self.prepare(request)
        saved = self.plugin.store.history_state(self.scope, request.conversation.cid)
        self.assertLess(saved["human_at"], time.time() - 7000)
        await self.finish()
        await self.prepare(request)
        self.assertEqual(len(request.contexts), 2)

    async def test_age_expires_older_turns_during_continuous_chat(self):
        self.plugin.config.update(history_char_limit=16000, history_max_age_minutes=1)
        request = self.request(turns(2, 10))
        old = align_times(request.contexts, [], time.time() - 120, time.time())
        self.plugin.store.save_history_state(self.scope, request.conversation.cid, old, time.time(), time.time())
        fresh = [{"role": "user", "content": "刚刚聊的"}, {"role": "assistant", "content": "刚刚答的"}]
        request.contexts += fresh
        await self.prepare(request)
        await self.finish()
        await self.prepare(request)
        self.assertEqual(request.contexts[2:], fresh)

    async def test_ready_prefix_does_not_keep_overnight_raw_tail(self):
        request = self.request()
        await self.prepare(request)
        await self.finish()
        saved = self.plugin.store.history_state(self.scope, request.conversation.cid)
        old = [[fingerprint, time.time() - 40000] for fingerprint, _ in saved["timeline"]]
        self.plugin.store.save_history_state(self.scope, request.conversation.cid, old,
                                            time.time() - 40000, time.time())
        original = copy.deepcopy(request.contexts)
        await self.prepare(request)
        self.assertNotIn("回复-7", encoded(request.contexts))
        self.assertEqual(len(request.contexts), 2)
        self.assertIn("Alice 喜欢无糖茶", encoded(request.contexts))
        archived = self.plugin.store.db.execute("SELECT original FROM history_archives ORDER BY id DESC").fetchone()
        self.assertEqual(json.loads(archived[0]), original)
        await self.finish()
        await self.prepare(request)
        # Later messages append to the replacement, preserving its summary
        # anchor without resurrecting an expired raw tail.
        request.contexts += turns(1, 5)
        await self.prepare(request)
        self.assertIn(MARKER, encoded(request.contexts))
        self.assertNotIn("回复-7", encoded(request.contexts))

    async def test_tool_call_results_remain_in_same_round(self):
        history = turns(6)
        recent = [
            {"role": "user", "content": "查天气"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "weather", "type": "function", "function": {"name": "weather", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "weather", "content": "晴"},
            {"role": "assistant", "content": "今天晴"},
        ]
        request = self.request(history + recent)
        await self.prepare(request)
        await self.finish()
        await self.prepare(request)
        self.assertEqual(request.contexts[-4:], recent)

    async def test_changed_prefix_cannot_use_stale_summary(self):
        request = self.request()
        await self.prepare(request)
        await self.finish()
        request.contexts[0]["content"] = "历史被其他插件修改"
        original = copy.deepcopy(request.contexts)
        await self.prepare(request)
        self.assertEqual(request.contexts, original)

    async def test_clear_during_summary_prevents_resurrection(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def generate(**kwargs):
            entered.set()
            await finish.wait()
            return SimpleNamespace(completion_text='{"facts":["旧事实"],"threads":[]}')

        self.context.llm_generate.side_effect = generate
        request = self.request()
        await self.prepare(request)
        await entered.wait()
        await self.plugin.memory.call(self.plugin.store.clear, self.scope)
        finish.set()
        await self.finish()
        self.assertIsNone(self.plugin.store.history_summary(self.scope, request.conversation.cid))

    async def test_summary_survives_manager_recreation_and_is_isolated_by_cid(self):
        from astrbot_plugin_self_cognition.history import HistoryCompactor

        request = self.request()
        await self.prepare(request)
        await self.finish()
        self.plugin.history = HistoryCompactor(self.plugin)
        other = self.request(turns(1, 10), cid="another-conversation")
        await self.prepare(other)
        self.assertFalse(other.contexts[0]["content"].startswith(MARKER))
        await self.prepare(request)
        self.assertTrue(request.contexts[0]["content"].startswith(MARKER))

    async def test_expired_threads_removed_without_model_or_losing_stable_facts(self):
        summary = {"facts": ["Alice 喜欢茶"], "threads": ["昨天临时的事"], "expires_at": time.time() - 1}
        request = self.request(summary_pair(summary))
        await self.prepare(request)
        result, _ = extract_summary(request.contexts)
        self.assertEqual(result["facts"], summary["facts"])
        self.assertEqual(result["threads"], [])
        self.context.llm_generate.assert_not_awaited()

    async def test_disabled_and_short_history_never_call_model(self):
        request = self.request(turns(1, 10))
        await self.prepare(request)
        self.context.llm_generate.assert_not_awaited()
        self.plugin.config["history_cleanup_enabled"] = False
        request = self.request()
        await self.prepare(request)
        self.assertFalse(self.plugin.history.tasks)

    async def test_hook_cleans_internal_report_without_capturing_it_as_human(self):
        request = self.request()
        self.event.set_extra("codex_bridge", {"kind": "task_result"})
        await self.plugin.prepare(self.event, request)
        await self.finish()
        self.assertIsNone(self.event.get_extra(fixtures.TURN))
        self.assertIsNotNone(self.plugin.store.history_summary(self.scope, request.conversation.cid))

    async def test_background_generation_does_not_block_frontend(self):
        release = asyncio.Event()

        async def generate(**kwargs):
            await release.wait()
            return SimpleNamespace(completion_text='{"facts":[],"threads":[]}')

        self.context.llm_generate.side_effect = generate
        await asyncio.wait_for(self.prepare(self.request()), 0.3)
        release.set()
        await self.finish()

    async def test_archive_pruning_and_scope_clear_cover_new_tables(self):
        request = self.request()
        await self.prepare(request)
        await self.finish()
        await self.prepare(request)
        self.plugin.store.prune(1, time.time() + 172800)
        for table in ("history_archives", "history_states", "history_summaries"):
            self.assertEqual(self.plugin.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    async def test_later_batch_failure_cannot_apply_partial_summary(self):
        request = self.request(turns(5, 8000))
        original = copy.deepcopy(request.contexts)
        self.context.llm_generate.side_effect = [
            SimpleNamespace(completion_text='{"facts":["部分整理"],"threads":[]}'),
            RuntimeError("second batch"),
        ]
        await self.prepare(request)
        await self.finish()
        self.assertEqual(request.contexts, original)
        self.assertIsNone(self.plugin.store.history_summary(self.scope, request.conversation.cid))

    async def test_recompression_does_not_renew_previous_thread_expiry(self):
        expiry = time.time() + 600
        previous = summary_pair({"facts": [], "threads": ["旧委托"], "expires_at": expiry})
        request = self.request(previous + turns())
        self.context.llm_generate.return_value = SimpleNamespace(completion_text='{"facts":[],"threads":["旧委托"]}')
        await self.prepare(request)
        await self.finish()
        await self.prepare(request)
        summary, _ = extract_summary(request.contexts)
        self.assertEqual(summary["expires_at"], expiry)

    async def test_system_prompt_and_current_request_are_unchanged(self):
        system = {"role": "system", "content": "当前人格：禁用 Emoji"}
        request = self.request([system] + turns())
        request.system_prompt = "正式人格"
        await self.prepare(request)
        await self.finish()
        await self.prepare(request)
        self.assertEqual(request.contexts[0], system)
        self.assertEqual(request.system_prompt, "正式人格")
        self.assertEqual(request.prompt, "新话题")

    async def test_scope_pause_invalidates_ready_summary(self):
        request = self.request()
        await self.prepare(request)
        await self.finish()
        await self.plugin.memory.call(self.plugin.store.pause, self.scope, True)
        original = copy.deepcopy(request.contexts)
        await self.prepare(request)
        self.assertEqual(request.contexts, original)

    async def test_model_serialization_preserves_summary_marker_for_next_cycle(self):
        from astrbot.core.agent.message import bind_checkpoint_messages, dump_messages_with_checkpoints

        request = self.request()
        await self.prepare(request)
        await self.finish()
        await self.prepare(request)
        request.contexts = dump_messages_with_checkpoints(bind_checkpoint_messages(request.contexts))
        summary, remaining = extract_summary(request.contexts)
        self.assertIsNotNone(summary)
        self.assertEqual(len(remaining), 2)


class SummaryRules(unittest.TestCase):
    def test_saved_plugin_drafts_are_removed_from_summarizer_evidence(self):
        data = [{"role": "user", "content": "早上好\n[reply_merge:thought:abcd1234]\n旧的催问草稿\n[/reply_merge:thought:abcd1234]"}]
        self.assertEqual(project_history(data), [{"role": "user", "content": "早上好"}])

    def test_schema_budget_dedup_and_expiry(self):
        result = parse_summary('{"facts":["喜欢茶","喜欢茶"],"threads":["临时话题"]}', 200, 100, 50)
        self.assertEqual(result["facts"], ["喜欢茶"])
        self.assertEqual(result["threads"], [])
        with self.assertRaises(ValueError):
            parse_summary("模型格式错误", 200, 100, 200)

    def test_latest_bridge_snapshot_deduplicated_with_attribution(self):
        def notice(progress):
            return {
                "role": "user",
                "content": "<codex_task_event>\n"
                + json.dumps(
                    {
                        "source": "codex_bridge",
                        "is_user_message": False,
                        "data": {"thread_id": "task", "progress": progress},
                    },
                    ensure_ascii=False,
                )
                + "</codex_task_event>",
            }

        output = project_history(
            [
                notice("旧进度"),
                {"role": "assistant", "content": "还要问第三遍"},
                {"role": "user", "content": "喜欢无糖茶"},
                notice("新进度"),
            ]
        )
        value = encoded(output)
        self.assertIn("喜欢无糖茶", value)
        self.assertIn("新进度", value)
        self.assertNotIn("旧进度", value)
        self.assertNotIn("问第三遍", value)
        self.assertIn('"role":"tool"', value)

    def test_multimodal_projection_drops_binary_but_keeps_all_text(self):
        content = "开头" + "长" * 20000 + "结尾"
        output = project_history(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET"}},
                        {"type": "text", "text": content},
                    ],
                }
            ]
        )
        self.assertNotIn("SECRET", encoded(output))
        self.assertEqual("".join(m["content"] for m in output), content)

    def test_timestamps_follow_retained_suffix(self):
        messages = turns(3, 1)
        before = align_times(messages, [], 10, 20)
        after = align_times(messages[2:] + turns(1, 3), before, 20, 30)
        self.assertTrue(all(at == 10 for _, at in after[:4]))
        self.assertTrue(all(at == 30 for _, at in after[4:]))
