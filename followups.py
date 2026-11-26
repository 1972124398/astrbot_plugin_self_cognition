"""Evidence-backed question state; importance never grants another reminder."""

import re

from .cognition import digest, text, tokens

POLICY = """同一问题首次提问后最多补问一次，换措辞、换插件、隔夜或重启都不重新计数。
没有回应时由你判断重要性：不重要的闲聊丢掉；重要的事静默保留，等对方主动接续，不能一直追问。
对方换题、只打招呼或认真反馈问题时，先接眼前的话。旧梗、自己的追问和未获回应不构成对方欠答或长期性格。
question_threads 是已问问题的状态；asks>=2、parked、dropped 的问题不得主动再问。用户明确接续时正常回应，不把它变成重新催答的机会。"""


def same_topic(topic, other):
    if topic == other:
        return True
    dates, other_dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", topic)), set(re.findall(r"\d{4}-\d{2}-\d{2}", other))
    if dates and other_dates and dates != other_dates:
        return False  # A new dated event is not a paraphrase of yesterday's event.
    a, b = tokens(topic), tokens(other)
    return bool(a and b and len(a & b) / len(a | b) >= 0.7)


def parse_updates(data, human, reply):
    result = []
    values = data.get("question_updates", [])
    for item in values[:24] if isinstance(values, list) else []:
        if not isinstance(item, dict) or not text(item.get("topic"), 120):
            continue
        asked = item.get("asked", [])
        asked = list(dict.fromkeys(text(q, 500) for q in asked
                                  if isinstance(q, str) and q.strip() and q.strip() in reply)) if isinstance(asked, list) else []
        answer = text(item.get("answer_evidence"), 500)
        if answer not in human:
            answer = ""
        important = (item.get("important") is True and bool(text(item.get("reason"), 200))) if "important" in item else None
        result.append({"actor": text(item.get("actor"), 80) or "$speaker",
                       "topic": text(item["topic"], 120), "asked": asked,
                       "answer_evidence": answer, "important": important,
                       "reason": text(item.get("reason"), 200) if important else "",
                       "unanswered": item.get("unanswered") is True})
    return result


class FollowupStore:
    def initialize_followups(self):
        self.db.execute("""CREATE TABLE IF NOT EXISTS question_threads(
            scope TEXT NOT NULL, actor TEXT NOT NULL, topic TEXT NOT NULL,
            asks INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'waiting',
            important INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
            updated REAL NOT NULL, PRIMARY KEY(scope,actor,topic))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS question_receipts(
            scope TEXT NOT NULL, actor TEXT NOT NULL, topic TEXT NOT NULL, turn TEXT NOT NULL,
            PRIMARY KEY(scope,actor,topic,turn))""")
        # Old records do not prove how many times a question was repeated. Keep
        # them as silent background instead of granting a fresh reminder budget.
        self.db.execute("INSERT OR IGNORE INTO question_threads "
                        "SELECT scope,actor,topic,2,'parked',0,'',updated FROM intentions "
                        "WHERE kind='ask' AND status IN ('sent','offered')")

    def question_threads(self, scope):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM question_threads WHERE scope=? ORDER BY updated DESC", (scope,))]

    def record_question_sent(self, scope, actor, topic, now, turn_id=""):
        old = next((r for r in self.question_threads(scope)
                    if r["actor"] == actor and same_topic(topic, r["topic"])), None)
        if old:
            topic = old["topic"]
        if turn_id and not self.db.execute("INSERT OR IGNORE INTO question_receipts VALUES(?,?,?,?)",
                                           (scope, actor, topic, digest(turn_id, topic))).rowcount:
            return
        asks = min(2, (old["asks"] if old else 0) + 1)
        important = old["important"] if old else 0
        state = ("parked" if important else "dropped") if asks >= 2 else "waiting"
        self.db.execute("INSERT INTO question_threads VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(scope,actor,topic) DO UPDATE SET asks=excluded.asks,state=excluded.state,updated=excluded.updated",
                        (scope, actor, topic, asks, state, important, old["reason"] if old else "", now))

    def save_question_updates(self, job, updates, now, entity):
        for item in updates:
            actor = entity(item["actor"])
            # A question belongs to its recipient, never a global self persona.
            recipients = {t.get("actor", job["actor"]) for t in job.get("turns", [job])}
            if actor == "self" or actor not in recipients:
                continue
            old = next((r for r in self.question_threads(job["scope"])
                        if r["actor"] == actor and same_topic(r["topic"], item["topic"])), None)
            topic = old["topic"] if old else item["topic"]
            asks = old["asks"] if old else 0
            for index, turn in enumerate(job.get("turns", [job])):
                if turn.get("actor", job["actor"]) != actor:
                    continue
                if any(quote in turn.get("reply", "") for quote in item["asked"]):
                    # One topic once per actual turn, not once per paraphrase/analysis.
                    members = job.get("member_ids", [])
                    source = members[index] if index < len(members) else (
                        job["id"] if len(job.get("turns", [job])) == 1 else digest(job["id"], index))
                    receipt = digest(source, topic)
                    inserted = self.db.execute("INSERT OR IGNORE INTO question_receipts VALUES(?,?,?,?)",
                                               (job["scope"], actor, topic, receipt)).rowcount
                    asks = min(2, asks + inserted)
            if not old and not asks:
                continue
            state = "waiting"
            important = int(item["important"]) if item["important"] is not None else (old["important"] if old else 0)
            reason = item["reason"] if item["important"] is not None else (old["reason"] if old else "")
            answered = item["answer_evidence"] and any(
                item["answer_evidence"] in t.get("human", "")
                for t in job.get("turns", [job]) if t.get("actor", job["actor"]) == actor)
            if answered:
                state = "answered"
            elif old and old["state"] in {"dropped", "parked", "answered"}:
                state = old["state"]  # A reflection cannot reopen a closed question.
            elif asks >= 2 or item["unanswered"]:
                state = "parked" if important else "dropped"
            self.db.execute("INSERT INTO question_threads VALUES(?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(scope,actor,topic) DO UPDATE SET asks=excluded.asks,state=excluded.state,"
                            "important=excluded.important,reason=excluded.reason,updated=excluded.updated",
                            (job["scope"], actor, topic, asks, state, important, reason, now))
            if state in {"dropped", "parked", "answered"}:
                # These topics are historical background, not outstanding chores.
                for intent in self.db.execute("SELECT id,topic FROM intentions WHERE scope=? AND actor IN (?, 'self')",
                                              (job["scope"], actor)).fetchall():
                    if same_topic(topic, intent["topic"]):
                        self.db.execute("UPDATE intentions SET status=? WHERE id=?",
                                        ("done" if state == "answered" else "dropped", intent["id"]))
                for question in self.db.execute("SELECT id,question FROM questions WHERE scope=? AND actor=?",
                                                (job["scope"], actor)).fetchall():
                    if same_topic(topic, question["question"]):
                        self.db.execute("UPDATE questions SET status='resolved' WHERE id=?", (question["id"],))


def blocked(topic, threads):
    return any(same_topic(topic, row["topic"]) and
               (row["asks"] >= 2 or row["state"] in {"parked", "dropped", "answered"}) for row in threads)
