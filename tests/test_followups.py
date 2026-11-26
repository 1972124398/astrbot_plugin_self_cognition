import json
import unittest

import test_life as fixtures
from astrbot_plugin_self_cognition.cognition import parse_reflection
from astrbot_plugin_self_cognition.followups import blocked, same_topic
from astrbot_plugin_self_cognition.store import Store


class FollowupTests(unittest.TestCase):
    setUp = fixtures.LifeTests.setUp
    tearDown = fixtures.LifeTests.tearDown
    job = fixtures.LifeTests.job

    def test_a_new_dated_event_does_not_inherit_yesterdays_question_count(self):
        self.assertFalse(same_topic("2026-11-26 晚饭", "2026-11-27 晚饭"))
        self.assertTrue(same_topic("晚饭", "晚饭？"))

    def update(self, key, reply="", human="在", **changes):
        job = self.job(key, human=human)
        job["reply"] = reply
        item = {"actor": "$speaker", "topic": "晚饭", "asked": [reply] if reply else [], **changes}
        result = parse_reflection(json.dumps({"question_updates": [item]}, ensure_ascii=False), human, reply=reply)
        self.assertTrue(self.store.apply(job, result))
        return self.store.question_threads("room")[0]

    def test_initial_question_one_followup_then_important_topic_is_silent(self):
        self.assertEqual(self.update("first", "饭吃了吗？")["asks"], 1)
        state = self.update("second", "刚才问的晚饭吃了吗？", important=True, reason="对方明确委托记录饮食")
        self.assertEqual((state["asks"], state["state"]), (2, "parked"))
        self.assertTrue(blocked("晚饭", self.store.question_threads("room")))
        self.update("reflection", important=True, reason="仍可保留背景")
        self.assertEqual(self.store.question_threads("room")[0]["state"], "parked")

    def test_unimportant_unanswered_question_is_dropped_without_followup(self):
        self.update("first", "饭吃了吗？")
        row = self.update("ignored", human="聊点别的", unanswered=True, important=False)
        self.assertEqual((row["asks"], row["state"], row["reason"]), (1, "dropped", ""))
        self.assertNotIn("question", row)  # No retained question body for a dropped curiosity.

    def test_answer_requires_recipient_evidence_and_closes_question(self):
        self.update("first", "饭吃了吗？")
        state = self.update("answer", human="早吃过了", answer_evidence="早吃过了")
        self.assertEqual(state["state"], "answered")

    def test_fabricated_question_or_answer_does_not_count(self):
        job = self.job("fabricated")
        raw = {"question_updates": [{"actor": "$speaker", "topic": "晚饭", "asked": ["饭吃了吗？"]}]}
        result = parse_reflection(json.dumps(raw), job["human"], reply="你好")
        self.store.apply(job, result)
        self.assertEqual(self.store.question_threads("room"), [])
        self.update("real", "饭吃了吗？")
        state = self.update("lie", answer_evidence="早吃过了")
        self.assertEqual(state["state"], "waiting")

    def test_restart_preserves_count_and_does_not_reopen_dropped_topic(self):
        self.update("first", "饭吃了吗？")
        self.update("second", "晚饭吃了吗？")
        path = self.store.db.execute("PRAGMA database_list").fetchone()[2]
        self.store.close()
        self.store = Store(path)
        self.assertEqual(self.store.question_threads("room")[0]["asks"], 2)
        self.assertEqual(self.update("third", "你晚饭到底吃了吗？")["state"], "dropped")
        self.assertEqual(self.store.question_threads("room")[0]["asks"], 2)

    def test_two_paraphrases_in_one_turn_count_once_and_clear_removes_receipts(self):
        job = self.job("same")
        job["reply"] = "饭吃了吗？晚饭怎么样？"
        raw = {"question_updates": [{"actor": "$speaker", "topic": "晚饭",
                                     "asked": ["饭吃了吗？", "晚饭怎么样？"]}]}
        parsed = parse_reflection(json.dumps(raw), job["human"], reply=job["reply"])
        self.store.apply(job, parsed)
        self.assertEqual(self.store.question_threads("room")[0]["asks"], 1)
        self.store.clear("room")
        self.assertEqual(self.store.question_threads("room"), [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM question_receipts").fetchone()[0], 0)

    def test_closed_topic_cannot_return_as_new_intention(self):
        self.update("first", "饭吃了吗？", unanswered=True)
        job = self.job("new-name")
        raw = {"intentions": [{"actor": "$speaker", "topic": "晚饭", "kind": "ask", "thought": "换个说法再问"}]}
        self.store.apply(job, parse_reflection(json.dumps(raw), job["human"]))
        self.assertEqual(self.store.dashboard("room")["intentions"], [])

    def test_same_send_seen_by_capture_and_reflection_counts_once(self):
        job = self.job("actual-send")
        job["reply"] = "饭吃了吗？"
        with self.store.db:
            self.store.record_question_sent("room", "user:alice", "晚饭", 1000, turn_id=job["id"])
        raw = {"question_updates": [{"actor": "$speaker", "topic": "晚饭", "asked": [job["reply"]]}]}
        self.store.apply(job, parse_reflection(json.dumps(raw), job["human"], reply=job["reply"]))
        self.assertEqual(self.store.question_threads("room")[0]["asks"], 1)

    def test_unrelated_recipient_does_not_inherit_question_limit(self):
        self.update("alice", "饭吃了吗？", unanswered=True)
        job = self.job("bob", actor="user:bob", name="小明")
        raw = {"intentions": [{"actor": "$speaker", "topic": "晚饭", "kind": "ask", "thought": "想问小明吃饭没"}]}
        self.store.apply(job, parse_reflection(json.dumps(raw), job["human"]))
        self.assertEqual(self.store.dashboard("room")["intentions"][0]["actor"], "user:bob")
