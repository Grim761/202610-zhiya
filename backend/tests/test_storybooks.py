"""Permission, filtering and serialization checks for the guardian bookshelf."""

import sqlite3
import hashlib
import json
import time
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.v1.auth import _login_attempts
from app.main import app

SCHEMA = Path(__file__).resolve().parents[2] / "database" / "schema.sql"


@pytest.fixture
def bookshelf(tmp_path, monkeypatch):
    path = tmp_path / "test.sqlite3"
    monkeypatch.setenv("ZHIYA_DB_PATH", str(path))
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA.read_text(encoding="utf-8"))
    _login_attempts.clear()
    with TestClient(app, base_url="https://testserver") as first, TestClient(app, base_url="https://testserver") as second:
        for client, account in ((first, "guardian-one"), (second, "guardian-two")):
            credentials = {"account": account, "password": "strong-pass-123"}
            assert client.post("/api/v1/auth/registrations", json=credentials).status_code == 201
            assert client.post("/api/v1/auth/sessions", json=credentials).status_code == 201
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA foreign_keys = ON")
            for book_id, owner, source in ((1, None, "LIBRARY"), (2, 1, "ORIGINAL"),
                                           (3, 2, "ORIGINAL"), (4, None, "LIBRARY")):
                db.execute("INSERT INTO storybooks VALUES (?, ?, ?, 1)", (book_id, owner, source))
            for version_id, book_id, version_no, status, reviewer in (
                (11, 1, 1, "PUBLISHED", 1), (21, 2, 1, "RETIRED", 1),
                (22, 2, 2, "APPROVED_PRIVATE", 1), (31, 3, 1, "APPROVED_PRIVATE", 1),
                (41, 4, 1, "DRAFT", None), (42, 4, 2, "WITHDRAWN", 1),
                (43, 4, 3, "RETIRED", 1),
            ):
                db.execute("""INSERT INTO story_versions
                    (id, storybook_id, version_no, title, topics_json, age_min, age_max,
                     license_note, status, reviewed_by_id, reviewed_at, created_at, branching)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'test', ?, ?, ?, 1, 1)""",
                    (version_id, book_id, version_no, f"story-{version_id}",
                     '["动物"]' if book_id == 1 else '["家庭"]',
                     3 if book_id == 1 else 5, 4 if book_id == 1 else 6,
                     status, reviewer, 1 if reviewer else None))
                db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref) VALUES (?, 1, ?, ?)",
                           (version_id, f"text-{version_id}", "/assets/placeholder.png"))
            db.execute("INSERT INTO story_branches VALUES (1, 11, 1, 1, 'approved', 'APPROVED')")
            db.execute("INSERT INTO story_branches VALUES (2, 11, 1, 1, 'pending', 'PENDING')")
        yield first, second, path
    _login_attempts.clear()


def test_guardian_visibility_history_and_private_isolation(bookshelf):
    first, second, _ = bookshelf
    def ids(client, **params):
        response = client.get("/api/v1/storybooks", params=params)
        assert response.status_code == 200
        return [v["storyVersionId"] for v in response.json()["data"]["items"]]
    assert ids(first) == [11, 22]
    assert ids(first, scope="mine") == [11, 22, 21]
    assert ids(second, scope="mine") == [11, 31]
    assert first.get("/api/v1/story-versions/21").status_code == 200
    for version in (31, 41, 42, 43, 999):
        response = first.get(f"/api/v1/story-versions/{version}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_filters_pagination_and_safe_media_projection(bookshelf):
    first, _, _ = bookshelf
    result = first.get("/api/v1/storybooks", params={"ageBand": "AGE_3_4", "topic": "动物"}).json()["data"]
    assert [item["storyVersionId"] for item in result["items"]] == [11]
    assert first.get("/api/v1/storybooks", params={"topic": "无"}).json()["data"]["items"] == []
    assert first.get("/api/v1/storybooks", params={"page": 2, "pageSize": 1}).json()["data"]["items"][0]["storyVersionId"] == 22
    assert first.get("/api/v1/storybooks", params={"pageSize": 51}).status_code == 422
    book = first.get("/api/v1/story-versions/11").json()["data"]
    assert book["coverRef"] is None and book["pages"][0]["imageRef"] is None
    assert book["pages"][0]["text"] == "text-11"
    assert [branch["branchId"] for branch in book["branches"]] == [1]


def test_authentication_and_dependency_failure(bookshelf, monkeypatch, tmp_path):
    first, _, _ = bookshelf
    with TestClient(app, base_url="https://testserver") as anonymous:
        assert anonymous.get("/api/v1/storybooks").status_code == 401
        assert anonymous.get("/api/v1/story-versions/11").status_code == 401
    monkeypatch.setenv("ZHIYA_DB_PATH", str(tmp_path / "missing.sqlite3"))
    response = first.get("/api/v1/storybooks")
    assert response.status_code == 503 and response.json()["error"]["retryable"] is True


def test_reading_session_create_recovery_and_isolation(bookshelf):
    first, second, path = bookshelf
    headers = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    other_headers = {"X-CSRF-Token": second.cookies["zhiya_csrf"]}
    profile = first.post("/api/v1/child-profiles", headers=headers,
                         json={"ageBand": "AGE_3_4", "preferredMinutes": 10}).json()["data"]
    body = {"childProfileId": profile["childProfileId"], "storyVersionId": 11, "mode": "PARENT_CHILD"}
    assert first.post("/api/v1/reading-sessions", json=body).status_code == 403
    assert second.post("/api/v1/reading-sessions", headers=other_headers, json=body).status_code == 404
    assert first.post("/api/v1/reading-sessions", headers=headers,
                      json={**body, "storyVersionId": 31}).status_code == 404
    assert first.post("/api/v1/reading-sessions", headers=headers,
                      json={**body, "storyVersionId": 41}).status_code == 404
    # Continuation switching is deliberately not accepted by the ordinary create path.
    assert first.post("/api/v1/reading-sessions", headers=headers,
                      json={**body, "sourceReadingSessionId": 1}).status_code == 422
    response = first.post("/api/v1/reading-sessions", headers=headers, json=body)
    assert response.status_code == 201
    session = response.json()["data"]
    session_id = session["readingSessionId"]
    assert (session["currentPage"], session["visitedPageNumbers"], session["status"], session["revision"]) == (1, [], "CREATED", 0)
    assert first.get(f"/api/v1/reading-sessions/{session_id}").json()["data"] == {
        "session": session, "events": [], "report": None}
    assert second.get(f"/api/v1/reading-sessions/{session_id}").status_code == 404
    assert second.get("/api/v1/reading-sessions").json()["data"]["items"] == []
    assert first.get("/api/v1/reading-sessions").json()["data"]["items"] == [session]
    assert first.get("/api/v1/reading-sessions", params={"pageSize": 51}).status_code == 422
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE story_versions SET status = 'WITHDRAWN' WHERE id = 11")
    assert first.get(f"/api/v1/reading-sessions/{session_id}").status_code == 404
    assert first.get("/api/v1/reading-sessions").json()["data"]["items"] == []


def test_reading_session_history_and_report_projection(bookshelf):
    first, _, path = bookshelf
    headers = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    profile = first.post("/api/v1/child-profiles", headers=headers,
                         json={"ageBand": "AGE_5_6", "preferredMinutes": 10}).json()["data"]
    body = {"childProfileId": profile["childProfileId"], "storyVersionId": 21, "mode": "PARENT_CHILD"}
    result = first.post("/api/v1/reading-sessions", headers=headers, json=body)
    assert result.status_code == 201  # reviewed, owned private history
    session_id = result.json()["data"]["readingSessionId"]
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO reading_reports VALUES (?, 'PENDING', '事实摘要', '[{\"name\":\"hidden\"}]', 1)", (session_id,))
    report = first.get(f"/api/v1/reading-sessions/{session_id}").json()["data"]["report"]
    assert report == {"readingSessionId": session_id, "status": "PENDING", "summary": "事实摘要", "activities": []}
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE reading_reports SET status = 'READY' WHERE session_id = ?", (session_id,))
    assert first.get(f"/api/v1/reading-sessions/{session_id}").json()["data"]["report"]["activities"] == [{"name": "hidden"}]


def test_reading_event_transitions_and_idempotency(bookshelf):
    first, second, path = bookshelf
    headers = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    other = {"X-CSRF-Token": second.cookies["zhiya_csrf"]}
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref) VALUES (11, 2, 'second', 'none')")
        db.execute("UPDATE story_pages SET default_next_page_number = 2 WHERE version_id = 11 AND page_no = 1")
    profile = first.post("/api/v1/child-profiles", headers=headers,
                         json={"ageBand": "AGE_3_4", "preferredMinutes": 10}).json()["data"]
    session = first.post("/api/v1/reading-sessions", headers=headers, json={
        "childProfileId": profile["childProfileId"], "storyVersionId": 11, "mode": "PARENT_CHILD"
    }).json()["data"]["readingSessionId"]
    url = f"/api/v1/reading-sessions/{session}/events"
    started = {"eventType": "STARTED", "pageNumber": 1, "expectedRevision": 0, "clientEventId": "start"}
    assert second.post(url, headers=other, json=started).status_code == 404
    assert first.post(url, json=started).status_code == 403
    initial = first.post(url, headers=headers, json=started)
    assert initial.status_code == 201 and initial.json()["data"]["revision"] == 1
    replay = first.post(url, headers=headers, json=started)
    assert replay.status_code == 200 and replay.json() == initial.json()
    assert first.post(url, headers=headers, json={**started, "pageNumber": 2}).json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    change = {"eventType": "PAGE_CHANGED", "pageNumber": 2, "expectedRevision": 1, "clientEventId": "page"}
    assert first.post(url, headers=headers, json={**change, "pageNumber": 5}).status_code == 409
    assert first.post(url, headers=headers, json=change).status_code == 201
    assert first.post(url, headers=headers, json={**change, "clientEventId": "stale"}).status_code == 409
    data = first.get(f"/api/v1/reading-sessions/{session}").json()["data"]
    assert data["session"]["visitedPageNumbers"] == [1, 2]
    assert data["session"]["revision"] == 2 and len(data["events"]) == 2
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""INSERT INTO request_executions
            (owner_account_id, session_id, scope_type, scope_id, client_request_id, request_digest,
             status, execution_token, started_at, deadline_at) VALUES (1, ?, 'SESSION_EVENT', ?,
             'other-endpoint', 'different-digest', 'RUNNING', 'token', 1, 100)""", (session, session))
    collision = first.post(url, headers=headers, json={**change, "clientEventId": "other-endpoint"})
    assert collision.status_code == 409 and collision.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE story_versions SET status = 'WITHDRAWN' WHERE id = 11")
    assert first.post(url, headers=headers, json=started).status_code == 404


def test_running_request_expires_to_unknown_without_reexecution(bookshelf):
    first, _, path = bookshelf
    headers = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    profile = first.post("/api/v1/child-profiles", headers=headers,
                         json={"ageBand": "AGE_3_4", "preferredMinutes": 10}).json()["data"]
    session = first.post("/api/v1/reading-sessions", headers=headers, json={
        "childProfileId": profile["childProfileId"], "storyVersionId": 11, "mode": "PARENT_CHILD"
    }).json()["data"]["readingSessionId"]
    url = f"/api/v1/reading-sessions/{session}/events"
    body = {"eventType": "STARTED", "pageNumber": 1, "expectedRevision": 0, "clientEventId": "running"}
    canonical = json.dumps({"method": "POST", "path": url, "body": body}, sort_keys=True,
                           separators=(',', ':'), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    now = int(time.time() * 1000)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""INSERT INTO request_executions
            (owner_account_id, session_id, scope_type, scope_id, client_request_id, request_digest,
             status, execution_token, started_at, deadline_at) VALUES (1, ?, 'SESSION_EVENT', ?,
             'running', ?, 'RUNNING', 'token', ?, ?)""", (session, session, digest, now, now + 60000))
    response = first.post(url, headers=headers, json=body)
    assert response.status_code == 409 and response.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
    assert response.headers["Retry-After"] == "1"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE request_executions SET started_at = 1, deadline_at = 2 WHERE client_request_id = 'running'")
    assert first.post(url, headers=headers, json=body).json()["error"]["code"] == "REQUEST_OUTCOME_UNKNOWN"
    assert first.post(url, headers=headers, json=body).status_code == 504
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT status FROM request_executions WHERE client_request_id = 'running'").fetchone()[0] == "UNKNOWN"
        assert db.execute("SELECT COUNT(*) FROM interaction_events WHERE session_id = ?", (session,)).fetchone()[0] == 0
