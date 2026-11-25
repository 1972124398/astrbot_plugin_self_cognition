"""The lasting parts of a character's life, independent of AstrBot adapters."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .cognition import digest, tokens


class LifeStore:
    def initialize_life(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS experiences(
          id TEXT PRIMARY KEY,scope TEXT NOT NULL,title TEXT NOT NULL,story TEXT NOT NULL,
          actors TEXT NOT NULL,cues TEXT NOT NULL,feeling TEXT NOT NULL,created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS experience_scope ON experiences(scope,created);
        CREATE TABLE IF NOT EXISTS characters(scope TEXT PRIMARY KEY,content TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS intentions(
          id INTEGER PRIMARY KEY,scope TEXT NOT NULL,actor TEXT NOT NULL,topic TEXT NOT NULL,
          kind TEXT NOT NULL,thought TEXT NOT NULL,occasion TEXT NOT NULL,not_before REAL NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending',last_message TEXT NOT NULL DEFAULT '',updated REAL NOT NULL,
          UNIQUE(scope,actor,topic));
        CREATE TABLE IF NOT EXISTS entity_aliases(
          scope TEXT NOT NULL,alias TEXT NOT NULL,entity TEXT NOT NULL,PRIMARY KEY(scope,alias,entity));
        CREATE TABLE IF NOT EXISTS relation_opinions(
          scope TEXT NOT NULL,source TEXT NOT NULL,target TEXT NOT NULL,label TEXT NOT NULL,reporter TEXT NOT NULL,
          status TEXT NOT NULL,description TEXT NOT NULL,evidence TEXT NOT NULL,updated REAL NOT NULL,
          PRIMARY KEY(scope,source,target,label,reporter));
        """)
        if "cues" not in {r[1] for r in self.db.execute("PRAGMA table_info(memories)")}:
            self.db.execute("ALTER TABLE memories ADD COLUMN cues TEXT NOT NULL DEFAULT '[]'")
        if "source_kind" not in {r[1] for r in self.db.execute("PRAGMA table_info(experiences)")}:
            self.db.execute("ALTER TABLE experiences ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'legacy'")
        self.db.execute(
            "INSERT OR IGNORE INTO relation_opinions SELECT scope,source,target,label,reporter,status,description,evidence,updated "
            "FROM relations WHERE reporter!=''"
        )
        self.db.execute("PRAGMA user_version=5")
        self.db.commit()

    def save_person(self, job, person, now):
        scope, actor = job["scope"], person["actor"]
        row = self.db.execute("SELECT name FROM entities WHERE scope=? AND id=?", (scope, actor)).fetchone()
        if not row or actor == "self":
            return
        delta = person.get("affection", 0)
        if delta or person.get("trust"):
            # No feeling change needs no ledger entry; quiet interactions can stay quiet.
            day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            self.db.execute(
                "INSERT INTO feelings VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    digest(job["id"], actor),
                    scope,
                    actor,
                    day,
                    delta,
                    person.get("trust", 0),
                    person.get("feeling_reason", ""),
                    person.get("feeling_evidence", ""),
                    now,
                ),
            )
        self.db.execute(
            "UPDATE entities SET affection=affection+?,trust=max(0,min(1,trust+?)) WHERE scope=? AND id=?",
            (delta, person.get("trust", 0), scope, actor),
        )
        if person.get("portrait"):
            self.save_portrait(scope, actor, person["portrait"], now)
        for item in person.get("curiosity", [])[:1]:
            existing = self.db.execute(
                "SELECT question FROM questions WHERE scope=? AND actor=?", (scope, actor)
            ).fetchall()
            words = tokens(item["question"])
            if any(len(words & tokens(q[0])) / max(1, len(words | tokens(q[0]))) > 0.7 for q in existing):
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO questions(scope,actor,question,evidence,created) VALUES(?,?,?,?,?)",
                (scope, actor, item["question"], item["evidence"], now),
            )
        for question in person.get("resolve", []):
            self.db.execute(
                "UPDATE questions SET status='resolved' WHERE scope=? AND actor=? AND question=?",
                (scope, actor, question),
            )

    def save_identity(self, job, item, now, entity):
        who = entity(item["entity"])
        identity = dict(item["identity"])
        old = self.db.execute("SELECT content FROM portraits WHERE scope=? AND actor=?", (job["scope"], who)).fetchone()
        previous = json.loads(old[0]).get("identity", {}) if old else {}
        merged = {**previous, **identity}
        if "titles" in identity:
            merged["titles"] = list(dict.fromkeys([*previous.get("titles", []), *identity["titles"]]))[:16]
        evidence = dict(previous.get("evidence", {}))
        title_evidence = dict(previous.get("title_evidence", {}))
        legacy = evidence.get("titles", {})
        for title in previous.get("titles", []):
            if title in legacy.get("quote", ""):
                title_evidence.setdefault(title, dict(legacy))
        source = {"quote": item["evidence"], "source": job["id"], "updated": now}
        for key in identity:
            if key != "titles":
                evidence[key] = dict(source)
        for title in identity.get("titles", []):
            title_evidence[title] = dict(source)
        if title_evidence:
            merged["title_evidence"] = {title: title_evidence[title] for title in merged.get("titles", [])
                                         if title in title_evidence}
        merged["evidence"] = evidence
        self.save_portrait(job["scope"], who, {"identity": merged}, now)
        names = [identity.get(key, "") for key in ("name", "nickname", "preferred_name")]
        names.extend(identity.get("titles", []))
        self.db.executemany("INSERT OR IGNORE INTO entity_aliases VALUES(?,?,?)",
                            [(job["scope"], name, who) for name in names if name])

    def save_life(self, job, result, now, entity):
        scope = job["scope"]
        experience = result.get("experience", {})
        if experience.get("story"):
            actors = list(dict.fromkeys(t.get("actor", job["actor"]) for t in job.get("turns", [job])))
            kind = job["source_kind"]
            if kind != "companion" and job.get("turns"):
                kind = "dialogue" if any(t.get("reply") for t in job["turns"]) else "observed"
            self.db.execute(
                "INSERT OR IGNORE INTO experiences(id,scope,title,story,actors,cues,feeling,created,source_kind) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    job["id"],
                    scope,
                    experience.get("title", "一段相处"),
                    experience["story"],
                    json.dumps(actors),
                    json.dumps(experience.get("cues", []), ensure_ascii=False),
                    experience.get("feeling", ""),
                    job.get("captured", now),
                    kind,
                ),
            )
        character = result.get("character", {})
        if character:
            old = self.db.execute("SELECT content FROM characters WHERE scope=?", (scope,)).fetchone()
            merged = json.loads(old[0]) if old else {}
            merged.update(character)
            self.db.execute(
                "INSERT INTO characters VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET "
                "content=excluded.content,updated=excluded.updated",
                (scope, json.dumps(merged, ensure_ascii=False), now),
            )
        for item in result.get("aliases", []):
            who = entity(item["entity"])
            self.db.executemany(
                "INSERT OR IGNORE INTO entity_aliases VALUES(?,?,?)", [(scope, alias, who) for alias in item["names"]]
            )
        for item in result.get("intentions", []):
            actor = entity(item["actor"])
            # A remembered intention belongs to a real participant or an already known person.
            if not self.db.execute("SELECT 1 FROM entities WHERE scope=? AND id=?", (scope, actor)).fetchone():
                continue
            if not item["thought"]:
                old = self.db.execute(
                    "SELECT thought FROM intentions WHERE scope=? AND actor=? AND topic=?",
                    (scope, actor, item["topic"]),
                ).fetchone()
                item = dict(item, thought=old[0] if old else "")
            self.db.execute(
                "INSERT INTO intentions(scope,actor,topic,kind,thought,occasion,not_before,status,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,actor,topic) DO UPDATE SET "
                "kind=excluded.kind,thought=excluded.thought,occasion=excluded.occasion,not_before=excluded.not_before,"
                "status=excluded.status,updated=excluded.updated",
                (
                    scope,
                    actor,
                    item["topic"],
                    item["kind"],
                    item["thought"],
                    item["occasion"],
                    item["not_before"],
                    item["status"],
                    now,
                ),
            )

    def life_view(self, scope):
        row = self.db.execute("SELECT content FROM characters WHERE scope=?", (scope,)).fetchone()
        experiences = []
        for saved in self.db.execute(
            "SELECT * FROM experiences WHERE scope=? ORDER BY created DESC LIMIT 300", (scope,)
        ):
            event = dict(saved)
            event["actors"], event["cues"] = json.loads(event["actors"]), json.loads(event["cues"])
            experiences.append(event)
        return {
            "character": json.loads(row[0]) if row else {},
            "experiences": experiences,
            "intentions": [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM intentions WHERE scope=? AND status NOT IN ('done','dropped') ORDER BY updated DESC",
                    (scope,),
                )
            ],
            "aliases": [dict(r) for r in self.db.execute("SELECT * FROM entity_aliases WHERE scope=?", (scope,))],
        }

    def enrich_relations(self, scope, relations):
        opinions = {}
        for r in self.db.execute("SELECT * FROM relation_opinions WHERE scope=? ORDER BY updated DESC", (scope,)):
            opinions.setdefault((r["source"], r["target"], r["label"]), []).append(dict(r))
        for row in relations:
            row["perspectives"] = opinions.get((row["source"], row["target"], row["label"]), [])[:3]
            row["disputed"] = len({p["status"] for p in row["perspectives"]}) > 1
        return relations

    def next_intention(self, scope, now, allow_questions=True, considered=()):
        if self.db.execute("SELECT 1 FROM intentions WHERE scope=? AND status='sent' AND updated>=?",
                           (scope, now - 30 * 86400)).fetchone():
            return None
        order = "id IN (" + ",".join("?" for _ in considered) + ")," if considered else ""
        rows = self.db.execute(
            "SELECT * FROM intentions WHERE scope=? AND status='pending' AND not_before<=? "
            "AND max(updated,not_before)>=? "
            + ("" if allow_questions else "AND kind!='ask' ")
            + "ORDER BY "
            + order
            + "updated DESC,id DESC LIMIT 3",
            (scope, now, now - 30 * 86400, *considered),
        ).fetchall()
        choices = [dict(row, intent_id=row["id"], question=row["thought"]) for row in rows]
        if choices:
            return dict(choices[0], candidates=choices)
        return None

    def mark_intention(self, scope, intent_id, message, now):
        with self.db:
            self.db.execute(
                "UPDATE intentions SET status='sent',last_message=?,updated=? WHERE scope=? AND id=?",
                (message, now, scope, intent_id),
            )
            self.db.execute("UPDATE scopes SET last_sent=? WHERE id=?", (now, scope))
            row = self.db.execute("SELECT actor FROM intentions WHERE scope=? AND id=?", (scope, intent_id)).fetchone()
            if row:
                self.save_portrait(scope, row[0], {"last_outreach": message}, now)
