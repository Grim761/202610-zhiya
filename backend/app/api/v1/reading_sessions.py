"""Guardian reading sessions: normal creation and recovery (no continuation switching)."""

import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.auth import connect, error
from app.api.v1.profiles import guardian
from app.api.v1.storybooks import VISIBLE

router = APIRouter(prefix="/api/v1/reading-sessions")
MAX_ID = 9007199254740991


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    childProfileId: int = Field(ge=1, le=MAX_ID, strict=True)
    storyVersionId: int = Field(ge=1, le=MAX_ID, strict=True)
    mode: Literal["PARENT_CHILD"]


def session_object(row):
    expiry = row["grant_expires_at"]
    return {
        "readingSessionId": row["id"], "storyVersionId": row["version_id"],
        "currentPage": row["current_page_no"], "visitedPageNumbers": json.loads(row["visited_pages_json"]),
        "mode": row["mode"], "status": row["status"], "revision": row["revision"],
        "expiresAt": (datetime.fromtimestamp(expiry / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
                      if expiry is not None else None),
    }


# Retired library versions remain readable only by an existing active session.
# WITHDRAWN content is never readable, even through an existing session.
SESSION_ACCESS = """(v.status = 'PUBLISHED' OR
    (v.status = 'APPROVED_PRIVATE' AND b.owner_guardian_id = ?) OR
    (v.status = 'RETIRED' AND v.reviewed_by_id IS NOT NULL AND v.reviewed_at IS NOT NULL
       AND ((b.source_type = 'ORIGINAL' AND b.owner_guardian_id = ?)
         OR (b.source_type = 'LIBRARY' AND s.status IN ('CREATED', 'IN_PROGRESS')))))"""


@router.post("", status_code=201)
def create_session(payload: SessionCreate, request: Request):
    try:
        with closing(connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            owner, failure = guardian(db, request, write=True)
            if failure:
                return failure
            profile = db.execute("SELECT 1 FROM child_profiles WHERE id = ? AND guardian_id = ?",
                                 (payload.childProfileId, owner)).fetchone()
            version = db.execute(
                "SELECT v.id FROM story_versions v JOIN storybooks b ON b.id = v.storybook_id "
                "WHERE v.id = ? AND " + VISIBLE +
                " AND EXISTS (SELECT 1 FROM story_pages p WHERE p.version_id = v.id AND p.page_no = 1)",
                (payload.storyVersionId, owner, True, owner),
            ).fetchone()
            if not profile or not version:
                return error(404, "RESOURCE_NOT_FOUND", "档案或绘本不可用")
            cursor = db.execute(
                """INSERT INTO reading_sessions
                (profile_id, version_id, current_page_no, mode, status, created_at)
                VALUES (?, ?, 1, 'PARENT_CHILD', 'CREATED', ?)""",
                (payload.childProfileId, payload.storyVersionId, int(time.time() * 1000)),
            )
            row = db.execute("SELECT *, NULL AS grant_expires_at FROM reading_sessions WHERE id = ?",
                             (cursor.lastrowid,)).fetchone()
            return {"data": session_object(row), "requestId": str(uuid4())}
    except sqlite3.Error:
        return error(503, "DEPENDENCY_UNAVAILABLE", "服务暂不可用")


@router.get("")
def list_sessions(request: Request, page: int = Query(default=1, ge=1, le=MAX_ID),
                  pageSize: int = Query(default=20, ge=1, le=50)):
    try:
        with closing(connect()) as db:
            db.execute("BEGIN")
            owner, failure = guardian(db, request, write=False)
            if failure:
                return failure
            offset = min((page - 1) * pageSize, 9223372036854775807)
            rows = db.execute(
                """SELECT s.*, g.expires_at AS grant_expires_at FROM reading_sessions s
                LEFT JOIN child_grants g ON g.id = s.grant_id
                JOIN child_profiles p ON p.id = s.profile_id
                JOIN story_versions v ON v.id = s.version_id
                JOIN storybooks b ON b.id = v.storybook_id
                WHERE p.guardian_id = ? AND """ + SESSION_ACCESS +
                " ORDER BY s.created_at DESC, s.id DESC LIMIT ? OFFSET ?",
                (owner, owner, owner, pageSize, offset),
            ).fetchall()
            return {"data": {"items": [session_object(row) for row in rows],
                             "page": page, "pageSize": pageSize}, "requestId": str(uuid4())}
    except sqlite3.Error:
        return error(503, "DEPENDENCY_UNAVAILABLE", "服务暂不可用", retryable=True)


@router.get("/{session_id}")
def get_session(request: Request, session_id: int = Path(ge=1, le=MAX_ID)):
    try:
        with closing(connect()) as db:
            db.execute("BEGIN")
            owner, failure = guardian(db, request, write=False)
            if failure:
                return failure
            row = db.execute(
                """SELECT s.*, g.expires_at AS grant_expires_at FROM reading_sessions s
                LEFT JOIN child_grants g ON g.id = s.grant_id
                JOIN child_profiles p ON p.id = s.profile_id
                JOIN story_versions v ON v.id = s.version_id
                JOIN storybooks b ON b.id = v.storybook_id
                WHERE s.id = ? AND p.guardian_id = ? AND """ + SESSION_ACCESS,
                (session_id, owner, owner, owner),
            ).fetchone()
            if not row:
                return error(404, "RESOURCE_NOT_FOUND", "会话不可用")
            events = db.execute(
                "SELECT id, event_type, page_no FROM interaction_events WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            report = db.execute("SELECT * FROM reading_reports WHERE session_id = ?", (session_id,)).fetchone()
            report_data = None
            if report:
                report_data = {"readingSessionId": session_id, "status": report["status"],
                               "summary": report["summary"],
                               "activities": (json.loads(report["activities_json"] or "[]")
                                              if report["status"] == "READY" else [])}
            return {"data": {"session": session_object(row),
                             "events": [{"interactionEventId": e["id"], "eventType": e["event_type"],
                                         "pageNumber": e["page_no"]} for e in events],
                             "report": report_data}, "requestId": str(uuid4())}
    except sqlite3.Error:
        return error(503, "DEPENDENCY_UNAVAILABLE", "服务暂不可用", retryable=True)
