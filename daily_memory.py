"""Read cached daily knowledge without turning it into new human evidence."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def linked_rows(snapshots, actor, query, now, conversation=False):
    if (re.search(r"帮|请|给我|写一|生成|分析|解释|修复|翻译|总结|设计|编写|科普", query)
            and re.search(r"代码|程序|制度|文案|报告|工作流|函数|算法|作文|论文|科普|模板|总结|管理办法", query)
            and not re.search(r"根据我|按我|我的作息|我的日常|我的习惯|记得我", query)):
        return []
    result = []
    for subject, snapshot in snapshots.items():
        if actor and subject != actor:
            continue
        if conversation and not snapshot.get("context_enabled", True):
            continue
        zone = snapshot.get("timezone", "Asia/Shanghai")
        day = datetime.fromtimestamp(now, ZoneInfo(zone)).date()
        offset = next((n for word, n in (("后天", 2), ("明天", 1), ("昨天", -1), ("前天", -2)) if word in query), 0)
        date = (day + timedelta(days=offset)).isoformat()
        for row in snapshot.get("records", []):
            if ((row.get("date_from") and date < row["date_from"]) or (row.get("date_to") and date > row["date_to"])) and not re.search(r"以前|过去|之前", query):
                continue
            content = row["text"]
            if row.get("date_from") or row.get("date_to"):
                period = row.get("date_from", "") + (" 至 " + row["date_to"] if row.get("date_to") != row.get("date_from") and row.get("date_to") else "")
                content = period.strip() + "的安排：" + content
            if now - row.get("updated", now) > 120 * 86400:
                content = "以前了解，近期未确认：" + content
            result.append({"id": "daily:" + subject + ":" + row["key"], "subject": subject,
                           "slot": row["key"], "kind": "fact", "content": content,
                           "evidence": row.get("evidence", ""), "source": "daily_routine",
                           "confidence": 0.8, "importance": 0.5, "updated": row.get("updated", 0)})
    return result
