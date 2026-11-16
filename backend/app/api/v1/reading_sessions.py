"""Guardian reading sessions: normal creation and recovery (no continuation switching)."""

import json
import hashlib
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
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


class ReadingEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    eventType: Literal["STARTED", "PAGE_CHANGED"]
    pageNumber: int = Field(ge=1, le=MAX_ID, strict=True)
    expectedRevision: int = Field(ge=0, le=MAX_ID, strict=True)
    clientEventId: str = Field(min_length=1, max_length=64)


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


@router.post("/{session_id}/events", status_code=201)
def add_reading_event(payload: ReadingEvent, request: Request, session_id: int = Path(ge=1, le=MAX_ID)):
    canonical = json.dumps({"method": "POST", "path": f"/api/v1/reading-sessions/{session_id}/events",
                            "body": payload.model_dump()}, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    try:
        with closing(connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            owner, failure = guardian(db, request, write=True)
            if failure:
                return failure
            row = db.execute(
                """SELECT s.* FROM reading_sessions s
                JOIN child_profiles p ON p.id = s.profile_id
                JOIN story_versions v ON v.id = s.version_id
                JOIN storybooks b ON b.id = v.storybook_id
                WHERE s.id = ? AND p.guardian_id = ? AND """ + SESSION_ACCESS,
                (session_id, owner, owner, owner),
            ).fetchone()
            if not row:
                return error(404, "RESOURCE_NOT_FOUND", "会话不可用")
            previous = db.execute("""SELECT request_digest, status, response_status, response_body, deadline_at
                                  FROM request_executions WHERE scope_type = 'SESSION_EVENT'
                                  AND scope_id = ? AND client_request_id = ?""",
                                  (session_id, payload.clientEventId)).fetchone()
            if previous:
                if previous["request_digest"] != digest:
                    return error(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他操作")
                if previous["status"] == "RUNNING":
                    now = int(time.time() * 1000)
                    if previous["deadline_at"] <= now:
                        failure = error(504, "REQUEST_OUTCOME_UNKNOWN", "请求结果未知，请先查询会话")
                        db.execute("""UPDATE request_executions SET status = 'UNKNOWN', completed_at = ?,
                                      response_status = 504, response_body = ? WHERE scope_type = 'SESSION_EVENT'
                                      AND scope_id = ? AND client_request_id = ? AND status = 'RUNNING'
                                      AND deadline_at <= ?""",
                                   (now, failure.body.decode('utf-8'), session_id, payload.clientEventId, now))
                        return failure
                    return error(409, "REQUEST_IN_PROGRESS", "请求正在处理", retryable=True, headers={"Retry-After": "1"})
                if previous["status"] == "UNKNOWN":
                    return error(504, "REQUEST_OUTCOME_UNKNOWN", "请求结果未知，请先查询会话")
                return JSONResponse(status_code=200 if previous["status"] == "SUCCEEDED" else previous["response_status"],
                                    content=json.loads(previous["response_body"]))
            if row["revision"] != payload.expectedRevision:
                return error(409, "SESSION_CONFLICT", "会话状态已变化")
            if payload.eventType == "STARTED":
                valid = row["status"] == "CREATED" and payload.pageNumber == row["start_page_number"]
            else:
                page = db.execute("SELECT default_next_page_number FROM story_pages WHERE version_id = ? AND page_no = ?",
                                  (row["version_id"], row["current_page_no"])).fetchone()
                valid = row["status"] == "IN_PROGRESS" and page and page[0] == payload.pageNumber
            if not valid:
                return error(409, "SESSION_CONFLICT", "当前状态或页面不允许此操作")
            now = int(time.time() * 1000)
            revision = row["revision"] + 1
            visited = json.loads(row["visited_pages_json"])
            visited.append(payload.pageNumber)
            db.execute("""UPDATE reading_sessions SET status = 'IN_PROGRESS', current_page_no = ?,
                       started_at = COALESCE(started_at, ?), revision = ?, visited_pages_json = ? WHERE id = ?""",
                       (payload.pageNumber, now, revision, json.dumps(visited), session_id))
            cursor = db.execute("""INSERT INTO interaction_events
                (session_id, version_id, request_key, request_digest, response_json, page_no, event_type,
                 result_status, revision_after, created_at) VALUES (?, ?, ?, ?, '{}', ?, ?, 'SUCCESS', ?, ?)""",
                (session_id, row["version_id"], payload.clientEventId, digest, payload.pageNumber,
                 payload.eventType, revision, now))
            response = {"data": {"interactionEventId": cursor.lastrowid, "revision": revision}, "requestId": str(uuid4())}
            db.execute("UPDATE interaction_events SET response_json = ? WHERE id = ?",
                       (json.dumps(response, ensure_ascii=False), cursor.lastrowid))
            db.execute("""INSERT INTO request_executions
                (owner_account_id, session_id, scope_type, scope_id, client_request_id, request_digest,
                 status, execution_token, started_at, deadline_at, completed_at, response_status, response_body)
                VALUES (?, ?, 'SESSION_EVENT', ?, ?, ?, 'SUCCEEDED', ?, ?, ?, ?, 201, ?)""",
                (owner, session_id, session_id, payload.clientEventId, digest, str(uuid4()), now, now + 25000,
                 now, json.dumps(response, ensure_ascii=False)))
            return JSONResponse(status_code=201, content=response)
    except sqlite3.Error:
        return error(503, "DEPENDENCY_UNAVAILABLE", "服务暂不可用")
