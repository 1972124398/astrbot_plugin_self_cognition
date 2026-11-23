import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_self_cognition.cognition import actor_key, parse_reflection, scope_key, tokens
from astrbot_plugin_self_cognition.store import Store


HUMAN = "我喜欢无糖茶，小明是我的同事。谢谢你一直记得我的事，最近在练琴。"


def reflection(content="喜欢无糖茶", affection=3):
    return {
        "memories": [
            {
                "subject": "$speaker",
                "key": "drink",
                "kind": "preference",
                "content": content,
                "evidence": "我喜欢无糖茶",
                "confidence": 0.95,
                "importance": 0.8,
            }
        ],
        "relations": [
            {"source": "$speaker", "target": "小明", "label": "同事", "evidence": "小明是我的同事", "confidence": 0.95}
        ],
        "feeling": {"affection": affection, "trust": 0.08, "reason": "真诚感谢", "evidence": "谢谢你一直记得我的事"},
        "curiosity": [{"question": "最近在练哪首曲子？", "evidence": "最近在练琴"}],
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.store = Store(self.path)
        self.scope = scope_key("test:FriendMessage:alice", "friend")
        self.store.ensure_scope(self.scope, "test:FriendMessage:alice", "friend")
        self.actor = actor_key("test:FriendMessage:alice", "alice")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def job(self, name="one", **changes):
        job = {
            "id": name,
            "scope": self.scope,
            "generation": self.store.scope(self.scope)["generation"],
            "actor": self.actor,
            "name": "Alice",
            "origin": "test:FriendMessage:alice",
            "source_kind": "dialogue",
            "human": HUMAN,
            "reply": "我记得。",
            "provider_id": "",
            "persona": "朋友",
        }
        job.update(changes)
        return job

    def apply(self, name="one", data=None, now=1800000000, cap=10):
        job = self.job(name)
        self.store.enqueue(job)
        result = parse_reflection(json.dumps(data or reflection(), ensure_ascii=False), HUMAN)
        self.assertTrue(self.store.apply(job, result, cap, now))
        return job, result

    def test_replay_does_not_double_affection(self):
        job, result = self.apply()
        self.assertFalse(self.store.apply(job, result))
        self.assertFalse(self.store.enqueue(job))
        self.assertEqual(self.store.context(self.scope, self.actor)["feeling"]["affection"], 3)

    def test_affection_is_durable_and_never_time_decays(self):
        self.apply()
        self.store.prune(1, 2800000000)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.context(self.scope, self.actor)["feeling"]["affection"], 3)

    def test_daily_cap_and_next_day(self):
        for i in range(7):
            self.apply(str(i))
        self.assertEqual(self.store.context(self.scope, self.actor)["feeling"]["affection"], 10)
        self.apply("tomorrow", now=1800000000 + 86400)
        self.assertEqual(self.store.context(self.scope, self.actor)["feeling"]["affection"], 13)

    def test_trust_can_fall_without_erasing_affection(self):
        self.apply()
        data = reflection(affection=-100)
        data["feeling"]["trust"] = -10
        for i in range(8):
            self.apply(f"negative{i}", data)
        feeling = self.store.context(self.scope, self.actor)["feeling"]
        self.assertEqual(feeling["affection"], 3)
        self.assertEqual(feeling["trust"], 0)

    def test_revision_keeps_history_and_only_recalls_current(self):
        self.apply()
        data = reflection("现在偏爱咖啡")
        data["memories"][0]["evidence"] = "小明是我的同事"  # Literal provenance is necessary, not semantic proof.
        self.apply("revision", data)
        rows = self.store.context(self.scope)["memories"]
        self.assertEqual([r["content"] for r in rows], ["现在偏爱咖啡"])
        self.assertEqual(self.store.stats(self.scope)["memories"], 2)

    def test_companion_has_no_human_feelings_or_relations(self):
        job = self.job(source_kind="companion", actor="self")
        self.store.enqueue(job)
        result = parse_reflection(json.dumps(reflection(), ensure_ascii=False), HUMAN, companion=True)
        self.store.apply(job, result)
        self.assertEqual(self.store.stats(self.scope)["feelings"], 0)
        self.assertEqual(self.store.stats(self.scope)["relations"], 0)
        self.assertEqual(self.store.context(self.scope)["memories"][0]["subject"], "self")

    def test_clear_during_llm_prevents_late_result_resurrection(self):
        job = self.job()
        self.store.enqueue(job)
        self.store.clear(self.scope)
        result = parse_reflection(json.dumps(reflection(), ensure_ascii=False), HUMAN)
        self.assertFalse(self.store.apply(job, result))
        self.assertFalse(self.store.enqueue(job))
        self.assertEqual(self.store.stats(self.scope)["memories"], 0)

    def test_clear_does_not_reimport_identical_companion_snapshot(self):
        job = self.job("snapshot", source_kind="companion", actor="self")
        self.store.enqueue(job)
        self.store.apply(job, parse_reflection(json.dumps(reflection(), ensure_ascii=False), HUMAN, companion=True))
        self.store.clear(self.scope)
        job["generation"] = self.store.scope(self.scope)["generation"]
        self.assertFalse(self.store.enqueue(job))
        self.assertEqual(self.store.stats(self.scope)["memories"], 0)

    def test_forget_removes_history_raw_and_inflight_jobs(self):
        self.apply()
        stale = self.job("inflight")
        self.store.enqueue(stale)
        memory_id = self.store.context(self.scope)["memories"][0]["id"]
        self.assertTrue(self.store.forget_memory(self.scope, memory_id))
        result = parse_reflection(json.dumps(reflection(), ensure_ascii=False), HUMAN)
        self.assertFalse(self.store.apply(stale, result))
        self.assertEqual(self.store.stats(self.scope)["episodes"], 0)

    def test_isolation_and_no_nickname_identity_merge(self):
        self.apply()
        other = scope_key("test:FriendMessage:bob", "friend")
        self.store.ensure_scope(other, "test:FriendMessage:bob", "friend")
        self.assertEqual(self.store.context(other)["memories"], [])
        self.assertNotEqual(actor_key("test:a", "alice"), actor_key("test:a", "bob"))
        self.assertNotEqual(actor_key("test:a", "alice"), actor_key("other:a", "alice"))
        self.assertNotEqual(self.scope, scope_key("test:FriendMessage:alice", "other_persona"))

    def test_group_split_sessions_share_group_scope_but_not_senders(self):
        self.assertEqual(
            scope_key("qq:GroupMessage:x|group|123|alice", "p"), scope_key("qq:GroupMessage:x|group|123|bob", "p")
        )
        self.assertNotEqual(actor_key("qq:GroupMessage:123", "alice"), actor_key("qq:GroupMessage:123", "bob"))

    def test_queue_survives_restart_and_retry_exhaustion(self):
        self.store.enqueue(self.job())
        self.store.close()
        self.store = Store(self.path)
        for i in range(5):
            job = self.store.next_job(3000000000)
            self.assertEqual(job["attempts"], i)
            self.store.failed(job, 1800000000)
        self.assertIsNone(self.store.next_job(3000000000))
        self.assertEqual(self.store.stats(self.scope)["failed"], 1)
        self.store.retry_failed(self.scope)
        self.assertEqual(self.store.next_job(3000000000)["attempts"], 0)

    def test_disabled_scope_does_not_block_another_queue(self):
        self.store.enqueue(self.job())
        other = scope_key("test:FriendMessage:bob", "p")
        self.store.ensure_scope(other, "test:FriendMessage:bob", "p")
        self.store.enqueue(self.job("other", scope=other))
        self.assertEqual(self.store.next_job(3000000000, [other])["id"], "other")

    def test_queue_overflow_is_explicit_and_preserves_existing_job(self):
        self.store.enqueue(self.job(), 1)
        with self.assertRaises(OverflowError):
            self.store.enqueue(self.job("other"), 1)
        self.assertEqual(self.store.next_job(3000000000)["id"], "one")

    def test_relation_direction_is_not_auto_reversed(self):
        self.apply()
        rows = self.store.context(self.scope)["relations"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], self.actor)

    def test_question_offered_resolved_and_unanswered_states(self):
        self.apply()
        q = self.store.context(self.scope, self.actor)["curiosity"][0]
        self.store.mark_question(self.scope, q["id"], 1800000000, proactive=True)
        self.assertEqual(self.store.db.execute("SELECT status FROM questions").fetchone()[0], "sent")
        self.store.touch(self.scope, self.actor, "Alice", 1800010000)
        self.assertEqual(self.store.db.execute("SELECT status FROM questions").fetchone()[0], "answered_turn")

    def test_pause_invalidates_pending_results(self):
        job = self.job()
        self.store.enqueue(job)
        self.store.pause(self.scope, True)
        self.assertFalse(self.store.apply(job, parse_reflection(json.dumps(reflection(), ensure_ascii=False), HUMAN)))
        self.store.pause(self.scope, False)
        self.assertFalse(self.store.enqueue(job))

    def test_retrieval_understands_chinese_bigrams(self):
        self.apply()
        self.assertIn("无糖", tokens("喜欢无糖茶"))
        self.assertEqual(self.store.context(self.scope, self.actor, "无糖茶")["memories"][0]["content"], "喜欢无糖茶")


class ValidationTests(unittest.TestCase):
    def test_no_literal_evidence_no_memory_or_feelings(self):
        parsed = parse_reflection(json.dumps(reflection(), ensure_ascii=False), "没有这些原文")
        self.assertEqual(parsed["memories"], [])
        self.assertEqual(parsed["relations"], [])
        self.assertEqual(parsed["affection"], 0)

    def test_reject_nan_nonobject_and_malformed_json(self):
        for raw in ('{"bad":NaN}', "[]", "null", "not json"):
            with self.assertRaises((ValueError, TypeError)):
                parse_reflection(raw, HUMAN)

    def test_fenced_json_and_nonfinite_numeric_strings(self):
        data = reflection()
        data["feeling"]["affection"] = "Infinity"
        parsed = parse_reflection("```json\n" + json.dumps(data, ensure_ascii=False) + "\n```", HUMAN)
        self.assertEqual(parsed["affection"], 0)

    def test_unmentioned_named_entity_is_rejected(self):
        data = reflection()
        data["relations"][0]["target"] = "凭空的人"
        self.assertEqual(parse_reflection(json.dumps(data, ensure_ascii=False), HUMAN)["relations"], [])
