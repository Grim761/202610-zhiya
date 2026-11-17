"""Reading report lifecycle: fact summary, out-of-transaction generation and startup recovery.

Rules follow common.reportLifecycle in docs/design/api-contract-v0.4.json:
- the first finish stores a PENDING report holding the fact summary in the same transaction;
- generation runs after commit and only moves a report that is still PENDING;
- on startup, leftover PENDING reports become FAILED without calling the model.
"""

import json
import logging
import sqlite3
import time
from contextlib import closing

from app.api.v1.auth import connect

logger = logging.getLogger("zhiya.reports")
MAX_SUMMARY_CHARS = 500
SUMMARY_PROMPT = (
    "你是儿童绘本共读助手。请根据给出的阅读事实和已读页面正文，为家长写一段不超过120字的中文共读小结。"
    "只陈述已发生的阅读事实和可在家延续的温和建议，不评价孩子能力，不编造未出现的互动。"
)


def fact_summary(visited_pages: list[int]) -> str:
    pages = list(dict.fromkeys(visited_pages))
    if not pages:
        return "本次共读尚未阅读页面。"
    return "本次共读阅读了第 " + "、".join(str(page) for page in pages) + " 页。"


def report_object(session_id: int, row) -> dict | None:
    if row is None:
        return None
    activities = json.loads(row["activities_json"] or "[]") if row["status"] == "READY" else []
    return {"readingSessionId": session_id, "summary": row["summary"],
            "activities": activities, "status": row["status"]}


def _settle(session_id: int, status: str, summary: str | None = None) -> bool:
    """Conditionally move a PENDING report; never overrides READY/FAILED written elsewhere."""
    now = int(time.time() * 1000)
    with closing(connect()) as db, db:
        if status == "READY":
            cursor = db.execute(
                """UPDATE reading_reports SET status = 'READY', summary = ?, activities_json = '[]',
                   updated_at = ? WHERE session_id = ? AND status = 'PENDING'""",
                (summary, now, session_id))
        else:
            cursor = db.execute(
                """UPDATE reading_reports SET status = 'FAILED', activities_json = '[]', updated_at = ?
                   WHERE session_id = ? AND status = 'PENDING'""",
                (now, session_id))
        return cursor.rowcount == 1


async def generate_report(session_id: int, facts: str, page_texts: list[str], adapter) -> None:
    """Runs after the finish transaction commits. One model attempt at most, no retries."""
    text = ""
    if adapter is not None:
        try:
            from app.schemas.agent import ModelGenerateRequest
            result = await adapter.generate(ModelGenerateRequest(
                system_prompt=SUMMARY_PROMPT,
                context_text="\n".join(page_texts)[:4000],
                user_input=facts,
            ))
            if result.success:
                text = (result.text or "").strip()
        except Exception:  # model or adapter failure is a definite generation failure
            logger.warning("report_generation_failed session_id=%s", session_id)
            text = ""
    try:
        if text and len(text) <= MAX_SUMMARY_CHARS:
            _settle(session_id, "READY", text)
        else:
            _settle(session_id, "FAILED")
    except sqlite3.Error:
        # Left PENDING; startup recovery will settle it to FAILED.
        logger.warning("report_settle_failed session_id=%s", session_id)


def recover_pending_reports() -> int:
    """Single-worker startup recovery, before the app is ready. Does not call the model."""
    try:
        with closing(connect()) as db, db:
            cursor = db.execute(
                """UPDATE reading_reports SET status = 'FAILED', activities_json = '[]', updated_at = ?
                   WHERE status = 'PENDING'""",
                (int(time.time() * 1000),))
            return cursor.rowcount
    except sqlite3.Error:
        logger.warning("report_recovery_skipped database_unavailable")
        return 0
