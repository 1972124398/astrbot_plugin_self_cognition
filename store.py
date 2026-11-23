"""SQLite journal, revision history and durable reflection queue.

All methods run synchronously on the plugin event loop; no transaction spans an
await. Each accepted reflection and its deduplication marker commit atomically.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .cognition import digest, retrieve


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS scopes(
          id TEXT PRIMARY KEY, origin TEXT NOT NULL, persona TEXT NOT NULL,
          generation INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0,
          last_human REAL NOT NULL DEFAULT 0, last_sent REAL NOT NULL DEFAULT 0,
          no_contact INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS entities(
          scope TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
          affection REAL NOT NULL DEFAULT 0, trust REAL NOT NULL DEFAULT .5,
          PRIMARY KEY(scope,id));
        CREATE TABLE IF NOT EXISTS memories(
          id INTEGER PRIMARY KEY, scope TEXT NOT NULL, subject TEXT NOT NULL, slot TEXT NOT NULL,
          kind TEXT NOT NULL, content TEXT NOT NULL, evidence TEXT NOT NULL,
          source TEXT NOT NULL, confidence REAL NOT NULL, importance REAL NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS memory_scope ON memories(scope,active,updated);
        CREATE INDEX IF NOT EXISTS memory_slot ON memories(scope,subject,slot,active);
        CREATE TABLE IF NOT EXISTS relations(
          scope TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL, label TEXT NOT NULL,
          evidence TEXT NOT NULL, confidence REAL NOT NULL, updated REAL NOT NULL,
          PRIMARY KEY(scope,source,target));
        CREATE TABLE IF NOT EXISTS feelings(
          job TEXT PRIMARY KEY, scope TEXT NOT NULL, actor TEXT NOT NULL, day TEXT NOT NULL,
          delta REAL NOT NULL, trust_delta REAL NOT NULL, reason TEXT NOT NULL,
          evidence TEXT NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS feeling_day ON feelings(scope,actor,day);
        CREATE TABLE IF NOT EXISTS questions(
          id INTEGER PRIMARY KEY, scope TEXT NOT NULL, actor TEXT NOT NULL,
          question TEXT NOT NULL, evidence TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          created REAL NOT NULL, UNIQUE(scope,actor,question));
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, generation INTEGER NOT NULL,
          payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          next_at REAL NOT NULL DEFAULT 0, created REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending');
        CREATE INDEX IF NOT EXISTS job_pending ON jobs(state,next_at,created);
        CREATE TABLE IF NOT EXISTS episodes(
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, actor TEXT NOT NULL,
          human TEXT NOT NULL, reply TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshot_receipts(
          scope TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY(scope,id));
        PRAGMA user_version=1;
        """)
        self.db.commit()

    def ensure_scope(self, scope, origin, persona):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO scopes(id,origin,persona) VALUES(?,?,?)", (scope, origin, persona))
            self.db.execute("UPDATE scopes SET origin=? WHERE id=?", (origin, scope))
        return self.scope(scope)

    def scope(self, scope):
        row = self.db.execute("SELECT * FROM scopes WHERE id=?", (scope,)).fetchone()
        return dict(row) if row else None

    def scopes(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM scopes ORDER BY last_human DESC")]

    def touch(self, scope, actor, name, now):
        with self.db:
            self.db.execute("UPDATE scopes SET last_human=? WHERE id=?", (now, scope))
            self.db.execute(
                "INSERT INTO entities(scope,id,name,kind) VALUES(?,?,?,'user') "
                "ON CONFLICT(scope,id) DO UPDATE SET name=excluded.name",
                (scope, actor, name),
            )
            # Only one unanswered proactive question per person until a real reply.
            self.db.execute(
                "UPDATE questions SET status='answered_turn' WHERE scope=? AND actor=? AND status='sent'",
                (scope, actor),
            )

    def enqueue(self, job, queue_limit=256):
        current = self.scope(job["scope"])
        if not current or current["paused"] or job["generation"] != current["generation"]:
            return False
        if self.db.execute("SELECT 1 FROM jobs WHERE id=?", (job["id"],)).fetchone():
            return False
        if (
            job["source_kind"] == "companion"
            and self.db.execute(
                "SELECT 1 FROM snapshot_receipts WHERE scope=? AND id=?", (job["scope"], job["id"])
            ).fetchone()
        ):
            return False
        count = self.db.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0]
        if count >= queue_limit:
            raise OverflowError("reflection queue is full")
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO jobs(id,scope,generation,payload,created) VALUES(?,?,?,?,?)",
                (job["id"], job["scope"], job["generation"], json.dumps(job, ensure_ascii=False), now),
            )
            if job["source_kind"] == "dialogue":
                self.db.execute(
                    "INSERT OR IGNORE INTO episodes VALUES(?,?,?,?,?,?)",
                    (job["id"], job["scope"], job["actor"], job["human"], job["reply"], now),
                )
        return True

    def next_job(self, now, eligible_scopes=None):
        sql = "SELECT * FROM jobs WHERE state='pending' AND next_at<=?"
        args = [now]
        if eligible_scopes is not None:
            if not eligible_scopes:
                return None
            sql += " AND scope IN (" + ",".join("?" for _ in eligible_scopes) + ")"
            args.extend(eligible_scopes)
        row = self.db.execute(sql + " ORDER BY created LIMIT 1", args).fetchone()
        if not row:
            return None
        job = json.loads(row["payload"])
        job["attempts"] = row["attempts"]
        return job

    def failed(self, job, now):
        attempts = job["attempts"] + 1
        with self.db:
            self.db.execute(
                "UPDATE jobs SET attempts=?,next_at=?,state=? WHERE id=?",
                (
                    attempts,
                    now + min(3600, 30 * 2 ** min(attempts, 6)),
                    "failed" if attempts >= 5 else "pending",
                    job["id"],
                ),
            )

    def retry_failed(self, scope):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='pending',attempts=0,next_at=0 WHERE scope=? AND state='failed'", (scope,)
            )

    def apply(self, job, result, daily_cap=10, now=None):
        now = now if now is not None else time.time()
        state = self.scope(job["scope"])
        queued = self.db.execute("SELECT state FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if (
            not state
            or state["paused"]
            or state["generation"] != job["generation"]
            or not queued
            or queued[0] != "pending"
        ):
            return False
        scope, actor = job["scope"], job["actor"]
        companion = job["source_kind"] == "companion"

        def entity(label):
            key = actor if label == "$speaker" else ("self" if label == "$self" else "named:" + digest(label)[:24])
            name = job["name"] if label == "$speaker" else ("我" if label == "$self" else label)
            self.db.execute(
                "INSERT OR IGNORE INTO entities(scope,id,name,kind) VALUES(?,?,?,?)",
                (scope, key, name, "user" if key == actor and not companion else "character"),
            )
            return key

        with self.db:
            for row in result["memories"]:
                subject = entity("$self" if companion else row["subject"])
                old = self.db.execute(
                    "SELECT id,content FROM memories WHERE scope=? AND subject=? AND slot=? AND active=1",
                    (scope, subject, row["key"]),
                ).fetchone()
                if old and old["content"] == row["content"]:
                    self.db.execute(
                        "UPDATE memories SET updated=?,confidence=?,importance=? WHERE id=?",
                        (now, row["confidence"], row["importance"], old["id"]),
                    )
                    continue
                self.db.execute(
                    "UPDATE memories SET active=0 WHERE scope=? AND subject=? AND slot=? AND active=1",
                    (scope, subject, row["key"]),
                )
                self.db.execute(
                    "INSERT INTO memories(scope,subject,slot,kind,content,evidence,source,confidence,importance,created,updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        scope,
                        subject,
                        row["key"],
                        row["kind"],
                        row["content"],
                        row["evidence"],
                        job["source_kind"] + ":" + job["id"],
                        row["confidence"],
                        row["importance"],
                        now,
                        now,
                    ),
                )
            if not companion:
                for row in result["relations"]:
                    self.db.execute(
                        "INSERT INTO relations VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope,source,target) DO UPDATE SET "
                        "label=excluded.label,evidence=excluded.evidence,confidence=excluded.confidence,updated=excluded.updated",
                        (
                            scope,
                            entity(row["source"]),
                            entity(row["target"]),
                            row["label"],
                            row["evidence"],
                            row["confidence"],
                            now,
                        ),
                    )
                entity("$speaker")
                day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
                total = self.db.execute(
                    "SELECT coalesce(sum(delta),0) FROM feelings WHERE scope=? AND actor=? AND day=?",
                    (scope, actor, day),
                ).fetchone()[0]
                delta = min(result["affection"], max(0, daily_cap - total))
                self.db.execute(
                    "INSERT INTO feelings VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        job["id"],
                        scope,
                        actor,
                        day,
                        delta,
                        result["trust"],
                        result["feeling_reason"],
                        result.get("feeling_evidence", ""),
                        now,
                    ),
                )
                self.db.execute(
                    "UPDATE entities SET affection=affection+?,trust=max(0,min(1,trust+?)) WHERE scope=? AND id=?",
                    (delta, result["trust"], scope, actor),
                )
                for row in result["curiosity"][:1]:
                    count = self.db.execute(
                        "SELECT count(*) FROM questions WHERE scope=? AND actor=? AND status='pending'", (scope, actor)
                    ).fetchone()[0]
                    if count < 8:
                        self.db.execute(
                            "INSERT OR IGNORE INTO questions(scope,actor,question,evidence,created) VALUES(?,?,?,?,?)",
                            (scope, actor, row["question"], row["evidence"], now),
                        )
                for question in result["resolve"]:
                    self.db.execute(
                        "UPDATE questions SET status='resolved' WHERE scope=? AND actor=? AND question=?",
                        (scope, actor, question),
                    )
            self.db.execute("UPDATE jobs SET state='done',payload='{}' WHERE id=?", (job["id"],))
            if companion:
                self.db.execute("INSERT OR IGNORE INTO snapshot_receipts VALUES(?,?)", (scope, job["id"]))
        return True

    def context(self, scope, actor="", query="", limit=10):
        # Mix recent and important memories so old important facts remain retrievable.
        rows = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM memories WHERE scope=? AND active=1 ORDER BY updated DESC LIMIT 800", (scope,)
            )
        ]
        extra = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM memories WHERE scope=? AND active=1 ORDER BY importance DESC,updated DESC LIMIT 800",
                (scope,),
            )
        ]
        rows = list({r["id"]: r for r in rows + extra}.values())
        memories = retrieve(rows, query, actor, limit)
        entities = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM entities WHERE scope=? ORDER BY affection DESC,id LIMIT 200", (scope,)
            )
        ]
        feeling = self.db.execute(
            "SELECT name,affection,trust FROM entities WHERE scope=? AND id=?", (scope, actor)
        ).fetchone()
        relations = [
            dict(r)
            for r in self.db.execute("SELECT * FROM relations WHERE scope=? ORDER BY updated DESC LIMIT 50", (scope,))
        ]
        questions = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM questions WHERE scope=? AND actor=? AND status='pending' ORDER BY created LIMIT 3",
                (scope, actor),
            )
        ]
        return {
            "memories": memories,
            "entities": entities,
            "relations": relations,
            "feeling": dict(feeling) if feeling else {},
            "curiosity": questions,
        }

    def mark_question(self, scope, question_id, now, proactive=False):
        with self.db:
            self.db.execute(
                "UPDATE questions SET status=? WHERE scope=? AND id=? AND status='pending'",
                ("sent" if proactive else "offered", scope, question_id),
            )
            if proactive:
                self.db.execute("UPDATE scopes SET last_sent=? WHERE id=?", (now, scope))

    def pause(self, scope, paused):
        with self.db:
            self.db.execute("UPDATE scopes SET paused=?,generation=generation+1 WHERE id=?", (int(paused), scope))
            self.db.execute("DELETE FROM jobs WHERE scope=? AND state IN ('pending','failed')", (scope,))

    def contact(self, scope, allowed):
        with self.db:
            self.db.execute("UPDATE scopes SET no_contact=? WHERE id=?", (int(not allowed), scope))

    def forget_memory(self, scope, memory_id):
        with self.db:
            row = self.db.execute(
                "SELECT subject,slot FROM memories WHERE id=? AND scope=?", (memory_id, scope)
            ).fetchone()
            if not row:
                return False
            self.db.execute("DELETE FROM memories WHERE scope=? AND subject=? AND slot=?", (scope, *row))
            # Invalidate in-flight analysis and remove its raw sources to avoid resurrection.
            self.db.execute("UPDATE scopes SET generation=generation+1 WHERE id=?", (scope,))
            self.remember_snapshot_receipts(scope)
            self.db.execute("DELETE FROM jobs WHERE scope=?", (scope,))
            self.db.execute("DELETE FROM episodes WHERE scope=?", (scope,))
        return True

    def clear(self, scope):
        with self.db:
            self.remember_snapshot_receipts(scope)
            for table in ("memories", "entities", "relations", "feelings", "questions", "jobs", "episodes"):
                self.db.execute(f"DELETE FROM {table} WHERE scope=?", (scope,))
            self.db.execute("UPDATE scopes SET generation=generation+1,last_human=0,last_sent=0 WHERE id=?", (scope,))

    def remember_snapshot_receipts(self, scope):
        # Retain only content-free fingerprints of externally owned snapshots so a
        # poll cannot resurrect a memory the administrator just deleted.
        for row in self.db.execute("SELECT id,payload FROM jobs WHERE scope=? AND state!='done'", (scope,)).fetchall():
            if json.loads(row["payload"]).get("source_kind") == "companion":
                self.db.execute("INSERT OR IGNORE INTO snapshot_receipts VALUES(?,?)", (scope, row["id"]))

    def prune(self, days, now):
        if days > 0:
            with self.db:
                self.db.execute("DELETE FROM episodes WHERE created<?", (now - days * 86400,))
        # Completed IDs remain as durable replay protection; their payload was removed.

    def stats(self, scope):
        return {
            table: self.db.execute(f"SELECT count(*) FROM {table} WHERE scope=?", (scope,)).fetchone()[0]
            for table in ("memories", "relations", "feelings", "questions", "episodes")
        } | {
            "pending": self.db.execute(
                "SELECT count(*) FROM jobs WHERE scope=? AND state='pending'", (scope,)
            ).fetchone()[0],
            "failed": self.db.execute(
                "SELECT count(*) FROM jobs WHERE scope=? AND state='failed'", (scope,)
            ).fetchone()[0],
        }

    def close(self):
        self.db.close()
