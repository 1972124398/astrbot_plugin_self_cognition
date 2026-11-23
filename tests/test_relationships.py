import json
import tempfile
import time
import unittest
from pathlib import Path

import test_cognition as samples
from astrbot_plugin_self_cognition.cognition import parse_reflection, digest, living_context
from astrbot_plugin_self_cognition.memory import Memory
from astrbot_plugin_self_cognition.store import Store


class RelationshipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.store = Store(self.path)
        self.store.ensure_scope("room", "test:GroupMessage:123", "friend")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def remember(self, key, words, labels, ended=()):
        job = {
            "id": key,
            "scope": "room",
            "generation": 0,
            "actor": "user:narrator",
            "name": "小林",
            "origin": "test:GroupMessage:123",
            "source_kind": "dialogue",
            "human": words,
            "reply": "原来如此。",
        }
        self.store.enqueue(job)
        data = {
            "relations": [
                {
                    "source": "小明",
                    "target": "小红",
                    "labels": labels,
                    "ended_labels": list(ended),
                    "evidence": words,
                    "description": words,
                }
            ]
        }
        self.assertTrue(self.store.apply(job, parse_reflection(json.dumps(data, ensure_ascii=False), words)))

    def test_people_can_have_multiple_independent_relationship_tags(self):
        self.remember("one", "小明和小红既是同事，也是朋友，还很信任对方。", ["同事", "朋友", "互相信任"])
        data = self.store.dashboard("room")
        self.assertEqual({r["label"] for r in data["relations"]}, {"同事", "朋友", "互相信任"})
        self.assertEqual({r["source"] for r in data["relations"]}, {"named:" + digest("小明")[:24]})
        self.assertEqual({r["target"] for r in data["relations"]}, {"named:" + digest("小红")[:24]})
        self.assertEqual({r["reporter"] for r in data["relations"]}, {"user:narrator"})

    def test_adding_a_tag_does_not_replace_any_other_tag(self):
        self.remember("one", "小明和小红是朋友。", ["朋友"])
        self.remember("two", "小明和小红现在也成了同事。", ["同事"])
        self.assertEqual({r["label"] for r in self.store.context("room")["relations"]}, {"朋友", "同事"})

    def test_ending_one_tag_keeps_others_and_its_history(self):
        self.remember("one", "小明和小红是朋友也是同事。", ["朋友", "同事"])
        self.remember("two", "小明离职了，他和小红不再是同事，但还是朋友。", ["朋友"], ["同事"])
        data = self.store.dashboard("room")
        self.assertEqual({r["label"]: r["status"] for r in data["relations"]}, {"朋友": "current", "同事": "ended"})
        self.assertEqual({r["status"] for r in data["relation_history"] if r["label"] == "同事"}, {"current", "ended"})

    def test_old_pair_graph_migrates_without_losing_its_relation(self):
        self.remember("one", "小明和小红是朋友。", ["朋友"])
        with self.store.db:
            self.store.db.execute("ALTER TABLE relations RENAME TO latest_relations")
            self.store.db.execute(
                "CREATE TABLE relations(scope TEXT,source TEXT,target TEXT,label TEXT,evidence TEXT,confidence REAL,updated REAL,PRIMARY KEY(scope,source,target))"
            )
            self.store.db.execute(
                "INSERT INTO relations SELECT scope,source,target,label,evidence,confidence,updated FROM latest_relations"
            )
            self.store.db.execute("DROP TABLE latest_relations")
        self.store.close()
        self.store = Store(self.path)
        self.remember("two", "小明和小红现在也是同事。", ["同事"])
        self.assertEqual({r["label"] for r in self.store.context("room")["relations"]}, {"朋友", "同事"})

    def test_relevant_people_are_recalled_before_recent_unrelated_relations(self):
        self.remember("one", "小明和小红是朋友。", ["朋友"])
        for i in range(20):
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO relations VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        "room",
                        "other" + str(i),
                        "another" + str(i),
                        "同学",
                        "其他人的事",
                        0.8,
                        time.time() + 10,
                        "current",
                        "",
                        "",
                    ),
                )
        state, view = self.store.read_view("room")
        cache = Memory(self.store)
        cache.states["room"], cache.views["room"] = state, view
        recalled = cache.recall("room", "user:narrator", "小红与小明最近怎么样？")
        self.assertEqual(recalled["relations"][0]["label"], "朋友")
        lines = living_context(recalled, "小林")
        self.assertIn("小明", lines[0])
        self.assertIn("小红", lines[0])
        self.assertIn("小林提到", lines[0])

    def test_legacy_single_label_still_parses(self):
        parsed = parse_reflection(json.dumps(samples.reflection(), ensure_ascii=False), samples.HUMAN)
        self.assertEqual(parsed["relations"][0]["label"], "同事")
