"""SQLite journal, revision history and durable reflection queue.

Runtime callers use Memory.call to keep disk work off the conversation loop.
Each accepted reflection and its deduplication marker commit atomically.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .cognition import digest, retrieve, relevant_relations


from .life import LifeStore


class Store(LifeStore):
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
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
          status TEXT NOT NULL DEFAULT 'current', description TEXT NOT NULL DEFAULT '',
          reporter TEXT NOT NULL DEFAULT '', PRIMARY KEY(scope,source,target,label));
        CREATE TABLE IF NOT EXISTS relation_history(
          id INTEGER PRIMARY KEY, scope TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,
          label TEXT NOT NULL, status TEXT NOT NULL, description TEXT NOT NULL,
          evidence TEXT NOT NULL, reporter TEXT NOT NULL, created REAL NOT NULL);
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
        CREATE TABLE IF NOT EXISTS daily_memory(
          scope TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(scope,actor));
        CREATE TABLE IF NOT EXISTS portraits(
          scope TEXT NOT NULL, actor TEXT NOT NULL, content TEXT NOT NULL,
          updated REAL NOT NULL, PRIMARY KEY(scope,actor));
        PRAGMA user_version=3;
        """)
        if "persona_prompt" not in {r[1] for r in self.db.execute("PRAGMA table_info(scopes)")}:
            self.db.execute("ALTER TABLE scopes ADD COLUMN persona_prompt TEXT NOT NULL DEFAULT ''")
        if "status" not in {r[1] for r in self.db.execute("PRAGMA table_info(relations)")}:
            with self.db:
                self.db.execute("BEGIN")
                self.db.execute("ALTER TABLE relations RENAME TO relations_v2")
                self.db.execute(
                    "CREATE TABLE relations(scope TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL, "
                    "label TEXT NOT NULL,evidence TEXT NOT NULL,confidence REAL NOT NULL,updated REAL NOT NULL,"
                    "status TEXT NOT NULL DEFAULT 'current',description TEXT NOT NULL DEFAULT '',"
                    "reporter TEXT NOT NULL DEFAULT '',PRIMARY KEY(scope,source,target,label))"
                )
                self.db.execute(
                    "INSERT INTO relations(scope,source,target,label,evidence,confidence,updated) "
                    "SELECT scope,source,target,label,evidence,confidence,updated FROM relations_v2"
                )
                self.db.execute(
                    "INSERT INTO relation_history(scope,source,target,label,status,description,evidence,reporter,created) "
                    "SELECT old.scope,old.source,old.target,old.label,'current','',old.evidence,'',old.updated FROM relations_v2 old "
                    "WHERE NOT EXISTS(SELECT 1 FROM relation_history h WHERE h.scope=old.scope AND h.source=old.source "
                    "AND h.target=old.target AND h.label=old.label)"
                )
                self.db.execute("DROP TABLE relations_v2")
        self.db.commit()
        self.initialize_life()

    def ensure_scope(self, scope, origin, persona):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO scopes(id,origin,persona) VALUES(?,?,?)", (scope, origin, persona))
            self.db.execute("UPDATE scopes SET origin=? WHERE id=?", (origin, scope))
        return self.scope(scope)

    def replace_daily(self, scope, actor, payload, generation):
        state = self.scope(scope)
        if not state or state["paused"] or state["generation"] != generation:
            return False
        payload = {**payload, "source_revision": digest(payload), "records": [r for r in payload.get("records", [])
                   if not self.db.execute("SELECT 1 FROM snapshot_receipts WHERE scope=? AND id=?",
                                         (scope, "daily:" + actor + ":" + digest(r))).fetchone()]}
        with self.db:
            self.db.execute("INSERT INTO daily_memory VALUES(?,?,?) ON CONFLICT(scope,actor) DO UPDATE SET payload=excluded.payload",
                            (scope, actor, json.dumps(payload, ensure_ascii=False)))
        return True

    def forget_daily(self, scope):
        for row in self.db.execute("SELECT actor,payload FROM daily_memory WHERE scope=?", (scope,)):
            for record in json.loads(row["payload"]).get("records", []):
                self.db.execute("INSERT OR IGNORE INTO snapshot_receipts VALUES(?,?)",
                                (scope, "daily:" + row["actor"] + ":" + digest(record)))
        self.db.execute("DELETE FROM daily_memory WHERE scope=?", (scope,))

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
            self.db.execute(
                "UPDATE intentions SET status='offered' WHERE scope=? AND actor IN (?, 'self') AND status='sent'",
                (scope, actor),
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
        saved = self.db.execute("SELECT state,payload FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if saved:
            if (
                saved["state"] == "pending"
                and json.loads(saved["payload"]).get("source_kind") == "observed"
                and job["source_kind"] == "dialogue"
            ):
                with self.db:
                    self.db.execute(
                        "UPDATE jobs SET payload=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job["id"])
                    )
                    self.db.execute("UPDATE episodes SET reply=? WHERE id=?", (job["reply"], job["id"]))
                return True
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
            if job["source_kind"] in {"dialogue", "observed"}:
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

    def next_batch(self, now, eligible_scopes, quiet_seconds=45, batch_turns=6, max_wait=600):
        rows = self.db.execute("SELECT * FROM jobs WHERE state='pending' ORDER BY created LIMIT 1024").fetchall()
        groups = {}
        for row in rows:
            if row["scope"] not in eligible_scopes:
                continue
            item = json.loads(row["payload"])
            key = (
                row["scope"],
                "scene" if item["source_kind"] in {"dialogue", "observed"} else item["source_kind"],
                item.get("provider_id", ""),
                item.get("persona", ""),
            )
            groups.setdefault(key, []).append((row, item))
        for values in groups.values():
            if values[0][0]["next_at"] > now:
                continue
            first, last = values[0][0], values[-1][0]
            if (
                len(values) < batch_turns
                and now - last["created"] < quiet_seconds
                and now - first["created"] < max_wait
            ):
                continue
            chosen = values[:12]
            job = dict(chosen[0][1])
            job["member_ids"] = [row["id"] for row, _ in chosen]
            job["mentioned_users"] = list(
                {e["id"]: e for _, item in chosen for e in item.get("mentioned_users", [])}.values()
            )
            job["turns"] = [
                {k: item.get(k, "") for k in ("human", "reply", "captured", "name", "actor", "source_kind")}
                for _, item in chosen
            ]
            job["speakers"] = list(
                {item["actor"]: {"id": item["actor"], "name": item["name"]} for _, item in chosen}.values()
            )
            job["human"] = "\n".join("[" + item["name"] + "] " + item["human"] for _, item in chosen)
            job["reply"] = "\n".join(item["reply"] for _, item in chosen)
            job["attempts"] = first["attempts"]
            return job
        return None

    def capture(self, job, activity_only=False, queue_limit=256):
        current = self.ensure_scope(job["scope"], job["origin"], job.get("persona_id", "default"))
        if current["generation"] != job["generation"] or current["paused"]:
            return False
        if job["source_kind"] in {"dialogue", "observed"}:
            self.touch(job["scope"], job["actor"], job["name"], job.get("captured", time.time()))
            with self.db:
                self.db.executemany(
                    "INSERT INTO entities(scope,id,name,kind) VALUES(?,?,?,'user') "
                    "ON CONFLICT(scope,id) DO UPDATE SET name=excluded.name",
                    [(job["scope"], e["id"], e["name"]) for e in job.get("mentioned_users", [])],
                )
        if "persona" in job:
            with self.db:
                self.db.execute("UPDATE scopes SET persona_prompt=? WHERE id=?", (job["persona"], job["scope"]))
        if activity_only:
            return True
        accepted = self.enqueue(job, queue_limit)
        if accepted:
            for question in job.get("questions", []):
                if question["question"] in job["reply"]:
                    self.mark_question(job["scope"], question["id"], time.time())
        return accepted

    def failed(self, job, now):
        attempts = job["attempts"] + 1
        with self.db:
            self.db.executemany(
                "UPDATE jobs SET attempts=?,next_at=?,state=? WHERE id=?",
                [
                    (
                        attempts,
                        now + min(3600, 30 * 2 ** min(attempts, 6)),
                        "failed" if attempts >= 5 else "pending",
                        member,
                    )
                    for member in job.get("member_ids", [job["id"]])
                ],
            )

    def retry_failed(self, scope):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='pending',attempts=0,next_at=0 WHERE scope=? AND state='failed'", (scope,)
            )

    def apply(self, job, result, now=None):
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
            if label not in {"$speaker", "$self"}:
                known = self.db.execute("SELECT id FROM entities WHERE scope=? AND id=?", (scope, label)).fetchone()
                if known:
                    return known["id"]
                aliases = self.db.execute(
                    "SELECT entity FROM entity_aliases WHERE scope=? AND alias=?", (scope, label)
                ).fetchall()
                if len(aliases) == 1:
                    return aliases[0]["entity"]
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
                self.db.execute(
                    "UPDATE memories SET cues=? WHERE scope=? AND subject=? AND slot=? AND active=1",
                    (json.dumps(row.get("cues", []), ensure_ascii=False), scope, subject, row["key"]),
                )
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
                        ("own_words" if row.get("evidence_source") == "self" else job["source_kind"]) + ":" + job["id"],
                        row["confidence"],
                        row["importance"],
                        now,
                        now,
                    ),
                )
            for row in result["memories"]:
                self.db.execute(
                    "UPDATE memories SET cues=? WHERE scope=? AND subject=? AND slot=? AND active=1",
                    (
                        json.dumps(row.get("cues", []), ensure_ascii=False),
                        scope,
                        entity("$self" if companion else row["subject"]),
                        row["key"],
                    ),
                )
            for speaker in job.get("speakers", []):
                self.db.execute(
                    "INSERT OR IGNORE INTO entities(scope,id,name,kind) VALUES(?,?,?,?)",
                    (scope, speaker["id"], speaker["name"], "character" if speaker["id"] == "self" else "user"),
                )
            if not companion:
                for row in result["relations"]:
                    source, target = entity(row["source"]), entity(row["target"])
                    status, description = row.get("status", "current"), row.get("description", "")
                    reporter = entity(row.get("reporter", "$speaker"))
                    self.db.execute(
                        "INSERT INTO relation_opinions VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,source,target,label,reporter) "
                        "DO UPDATE SET status=excluded.status,description=excluded.description,evidence=excluded.evidence,updated=excluded.updated",
                        (scope, source, target, row["label"], reporter, status, description, row["evidence"], now),
                    )
                    previous = self.db.execute(
                        "SELECT status,description FROM relations WHERE scope=? AND source=? AND target=? AND label=?",
                        (scope, source, target, row["label"]),
                    ).fetchone()
                    if previous is None or (previous["status"], previous["description"]) != (status, description):
                        self.db.execute(
                            "INSERT INTO relation_history(scope,source,target,label,status,description,evidence,reporter,created) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (scope, source, target, row["label"], status, description, row["evidence"], reporter, now),
                        )
                    self.db.execute(
                        "INSERT INTO relations VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,source,target,label) DO UPDATE SET "
                        "evidence=excluded.evidence,confidence=excluded.confidence,updated=excluded.updated,status=excluded.status,"
                        "description=excluded.description,reporter=excluded.reporter",
                        (
                            scope,
                            source,
                            target,
                            row["label"],
                            row["evidence"],
                            row["confidence"],
                            now,
                            status,
                            description,
                            reporter,
                        ),
                    )
                entity("$speaker")
                people = result.get("people") or [dict(result, actor=actor)]
                for person in people:
                    who = actor if person["actor"] == "$speaker" else person["actor"]
                    self.save_person(job, dict(person, actor=who), now)
            self.save_life(job, result, now, entity)
            if result.get("self_reflection"):
                self.save_portrait(scope, "self", {"impression": result["self_reflection"]}, now)
            self.db.executemany(
                "UPDATE jobs SET state='done',payload='{}' WHERE id=?",
                [(member,) for member in job.get("member_ids", [job["id"]])],
            )
            if companion:
                self.db.execute("INSERT OR IGNORE INTO snapshot_receipts VALUES(?,?)", (scope, job["id"]))
        return True

    def save_portrait(self, scope, actor, changes, now):
        row = self.db.execute("SELECT content FROM portraits WHERE scope=? AND actor=?", (scope, actor)).fetchone()
        portrait = json.loads(row[0]) if row else {}
        portrait.update(changes)
        self.db.execute(
            "INSERT INTO portraits VALUES(?,?,?,?) ON CONFLICT(scope,actor) DO UPDATE SET "
            "content=excluded.content,updated=excluded.updated",
            (scope, actor, json.dumps(portrait, ensure_ascii=False), now),
        )

    def read_view(self, scope):
        from .memory import Memory

        data = self.context(scope, limit=1600)
        data["all_questions"] = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM questions WHERE scope=? AND status='pending' ORDER BY created", (scope,)
            )
        ]
        data["portraits"] = {
            r["actor"]: json.loads(r["content"])
            for r in self.db.execute("SELECT * FROM portraits WHERE scope=?", (scope,))
        }
        data.update(self.life_view(scope))
        data["daily_memory"] = {r["actor"]: json.loads(r["payload"]) for r in self.db.execute("SELECT * FROM daily_memory WHERE scope=?", (scope,))}
        return self.scope(scope), Memory.index(data)

    def dashboard(self, scope, query=""):
        data = self.context(scope, query=query, limit=30)
        data["all_questions"] = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM questions WHERE scope=? AND status='pending' ORDER BY created LIMIT 50", (scope,)
            )
        ]
        data["portraits"] = {
            r["actor"]: json.loads(r["content"])
            for r in self.db.execute("SELECT * FROM portraits WHERE scope=?", (scope,))
        }
        data.update(self.life_view(scope))
        data["relation_history"] = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM relation_history WHERE scope=? ORDER BY created DESC,id DESC LIMIT 100", (scope,)
            )
        ]
        return data

    def overview(self):
        return [{**s, "stats": self.stats(s["id"])} for s in self.scopes()]

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
            for r in self.db.execute("SELECT * FROM relations WHERE scope=? ORDER BY updated DESC LIMIT 800", (scope,))
        ]
        self.enrich_relations(scope, relations)
        known_ids = {e["id"] for e in entities}
        needed = {r[k] for r in relations for k in ("source", "target", "reporter")} - known_ids
        if needed:
            entities += [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM entities WHERE scope=? AND id IN (" + ",".join("?" for _ in needed) + ")",
                    (scope, *needed),
                )
            ]
        if limit < 1600:
            relations = relevant_relations(relations, entities, query, actor, 50)
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

    def mark_question(self, scope, question_id, now, proactive=False, message=""):
        with self.db:
            self.db.execute(
                "UPDATE questions SET status=? WHERE scope=? AND id=? AND status='pending'",
                ("sent" if proactive else "offered", scope, question_id),
            )
            if proactive:
                self.db.execute("UPDATE scopes SET last_sent=? WHERE id=?", (now, scope))
                row = self.db.execute(
                    "SELECT actor FROM questions WHERE id=? AND scope=?", (question_id, scope)
                ).fetchone()
                if row and message:
                    self.save_portrait(scope, row[0], {"last_outreach": message}, now)

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
            self.db.execute("DELETE FROM portraits WHERE scope=?", (scope,))
            for table in ("experiences", "characters", "intentions"):
                self.db.execute(f"DELETE FROM {table} WHERE scope=?", (scope,))
        return True

    def clear(self, scope):
        with self.db:
            self.forget_daily(scope)
            self.remember_snapshot_receipts(scope)
            for table in (
                "memories",
                "entities",
                "relations",
                "relation_history",
                "feelings",
                "questions",
                "jobs",
                "episodes",
                "portraits",
                "experiences",
                "characters",
                "intentions",
                "entity_aliases",
                "relation_opinions",
            ):
                self.db.execute(f"DELETE FROM {table} WHERE scope=?", (scope,))
            self.db.execute("UPDATE scopes SET generation=generation+1,last_human=0,last_sent=0 WHERE id=?", (scope,))

    def initiative(self, scope, now, allow_questions=True, considered=()):
        if self.db.execute("SELECT 1 FROM questions WHERE scope=? AND status='sent'", (scope,)).fetchone():
            return None
        intent = self.next_intention(scope, now, allow_questions, considered)
        if intent:
            return intent
        if self.db.execute("SELECT 1 FROM intentions WHERE scope=? AND status='sent'", (scope,)).fetchone():
            return None
        if not allow_questions:
            return None
        row = self.db.execute(
            "SELECT * FROM questions WHERE scope=? AND status='pending' AND created>? ORDER BY created DESC LIMIT 1",
            (scope, now - 30 * 86400),
        ).fetchone()
        return dict(row) if row else None

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
