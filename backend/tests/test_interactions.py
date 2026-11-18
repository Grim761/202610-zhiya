"""Tests for API-010 recommendations, API-015 ANSWER/FEEDBACK events, and API-018 branch selection."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.v1.auth import _login_attempts
from app.main import app

SCHEMA = Path(__file__).resolve().parents[2] / "database" / "schema.sql"


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "interactions.sqlite3"
    monkeypatch.setenv("ZHIYA_DB_PATH", str(path))
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA.read_text(encoding="utf-8"))
    _login_attempts.clear()
    with TestClient(app, base_url="https://testserver") as first, TestClient(app, base_url="https://testserver") as second:
        for client, account in ((first, "guardian-one"), (second, "guardian-two")):
            creds = {"account": account, "password": "strong-pass-123"}
            assert client.post("/api/v1/auth/registrations", json=creds).status_code == 201
            assert client.post("/api/v1/auth/sessions", json=creds).status_code == 201
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA foreign_keys = ON")
            for bid, owner, src in ((1, None, "LIBRARY"), (2, 1, "ORIGINAL"), (3, 2, "ORIGINAL"), (4, None, "LIBRARY")):
                db.execute("INSERT INTO storybooks VALUES (?, ?, ?, 1)", (bid, owner, src))
            for vid, bid, vno, topics, age_min, age_max, status in (
                (11, 1, 1, '["互助"]', 3, 5, "PUBLISHED"),
                (21, 2, 1, '["动物"]', 3, 4, "APPROVED_PRIVATE"),
                (31, 3, 1, '["动物"]', 3, 4, "APPROVED_PRIVATE"),
                (41, 4, 1, '["动物"]', 3, 4, "RETIRED"),
                (42, 4, 2, '["动物"]', 3, 4, "WITHDRAWN"),
            ):
                db.execute("""INSERT INTO story_versions
                    (id, storybook_id, version_no, title, topics_json, age_min, age_max,
                     license_note, status, reviewed_by_id, reviewed_at, created_at, branching)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'ok', ?, 1, 1, 1, 1)""",
                    (vid, bid, vno, f"story-{vid}", topics, age_min, age_max, status))
                db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref) VALUES (?, 4, '分支第四页', 'none')", (vid,))
                db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref, default_next_page_number) VALUES (?, 3, '常规第三页', 'none', 4)", (vid,))
                db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref, default_next_page_number) VALUES (?, 2, '第二页', 'none', 3)", (vid,))
                db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref, default_next_page_number) VALUES (?, 1, '第一页', 'none', 2)", (vid,))
            db.execute("INSERT INTO story_branches VALUES (501, 11, 2, 4, '走小桥', 'APPROVED')")
            db.execute("INSERT INTO story_branches VALUES (502, 11, 2, 3, '待审分支', 'PENDING')")
            db.execute("INSERT INTO story_branches VALUES (503, 11, 1, 3, '第一页分支', 'APPROVED')")
        yield first, second, path
    _login_attempts.clear()


def test_recommendations_filter_age_interests_and_access(env):
    first, second, _ = env
    headers = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    other_headers = {"X-CSRF-Token": second.cookies["zhiya_csrf"]}
    profile = first.post("/api/v1/child-profiles", headers=headers, json={
        "ageBand": "AGE_3_4", "preferredMinutes": 10, "interests": ["动物"]
    }).json()["data"]
    p_id = profile["childProfileId"]

    with TestClient(app, base_url="https://testserver") as anon:
        assert anon.post("/api/v1/recommendations", json={"childProfileId": p_id}).status_code == 401
    assert first.post("/api/v1/recommendations", json={"childProfileId": p_id}).status_code == 403
    assert second.post("/api/v1/recommendations", headers=other_headers, json={"childProfileId": p_id}).status_code == 404
    assert first.post("/api/v1/recommendations", headers=headers, json={"childProfileId": p_id, "minutes": 31}).status_code == 422

    # Prioritizes matching interest (21) before other accessible age-matched books (11); excludes 31, 41, 42.
    rec = first.post("/api/v1/recommendations", headers=headers, json={"childProfileId": p_id, "minutes": 10})
    assert rec.status_code == 200 and rec.json()["requestId"] == rec.headers["X-Request-ID"]
    items = rec.json()["data"]["items"]
    assert [item["storyVersionId"] for item in items] == [21, 11]
    assert all(item["sources"] == [] and item["reason"] for item in items)

    by_topic = first.post("/api/v1/recommendations", headers=headers, json={"childProfileId": p_id, "topic": " 互助 "})
    assert [item["storyVersionId"] for item in by_topic.json()["data"]["items"]] == [11]
    empty = first.post("/api/v1/recommendations", headers=headers, json={"childProfileId": p_id, "topic": "太空"})
    assert empty.status_code == 200 and empty.json()["data"]["items"] == []


def test_answer_feedback_and_branch_choice_lifecycle(env):
    first, second, path = env
    headers = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    other = {"X-CSRF-Token": second.cookies["zhiya_csrf"]}
    profile = first.post("/api/v1/child-profiles", headers=headers,
                         json={"ageBand": "AGE_3_4", "preferredMinutes": 10}).json()["data"]
    sid = first.post("/api/v1/reading-sessions", headers=headers, json={
        "childProfileId": profile["childProfileId"], "storyVersionId": 11, "mode": "PARENT_CHILD"
    }).json()["data"]["readingSessionId"]
    events_url = f"/api/v1/reading-sessions/{sid}/events"
    branches_url = f"/api/v1/reading-sessions/{sid}/branches"

    # Branch selection is rejected while CREATED.
    assert first.post(branches_url, headers=headers,
                      json={"branchId": 501, "expectedRevision": 0, "clientEventId": "early-b"}).status_code == 409

    assert first.post(events_url, headers=headers,
                      json={"eventType": "STARTED", "pageNumber": 1, "expectedRevision": 0, "clientEventId": "e1"}).status_code == 201
    assert first.post(events_url, headers=headers,
                      json={"eventType": "PAGE_CHANGED", "pageNumber": 2, "expectedRevision": 1, "clientEventId": "e2"}).status_code == 201

    # Validation for ANSWER and FEEDBACK payloads.
    assert first.post(events_url, headers=headers,
                      json={"eventType": "ANSWER", "pageNumber": 2, "expectedRevision": 2, "clientEventId": "bad-a", "inputText": "   "}).status_code == 422
    assert first.post(events_url, headers=headers,
                      json={"eventType": "FEEDBACK", "pageNumber": 2, "expectedRevision": 2, "clientEventId": "bad-f"}).status_code == 422

    # FEEDBACK requires a prior QUESTION event on the current page.
    no_q = first.post(events_url, headers=headers,
                      json={"eventType": "FEEDBACK", "pageNumber": 2, "expectedRevision": 2, "clientEventId": "f1", "feedback": "UNDERSTOOD"})
    assert no_q.status_code == 409 and no_q.json()["error"]["code"] == "PROMPT_REQUIRED"

    # ANSWER succeeds on current page without changing currentPage.
    ans = first.post(events_url, headers=headers,
                     json={"eventType": "ANSWER", "pageNumber": 2, "expectedRevision": 2, "clientEventId": "a1", "inputText": " 小兔子帮了朋友 "})
    assert ans.status_code == 201 and ans.json()["data"]["revision"] == 3
    ans_replay = first.post(events_url, headers=headers,
                            json={"eventType": "ANSWER", "pageNumber": 2, "expectedRevision": 2, "clientEventId": "a1", "inputText": "小兔子帮了朋友"})
    assert ans_replay.status_code == 200 and ans_replay.json()["data"] == ans.json()["data"]

    # Seed a QUESTION on page 2, then FEEDBACK succeeds.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""INSERT INTO interaction_events
            (session_id, version_id, request_key, request_digest, response_json, page_no,
             event_type, output_text, result_status, revision_after, created_at)
            VALUES (?, 11, 'q1', 'd', '{}', 2, 'QUESTION', '小兔子在做什么？', 'SUCCESS', 3, 1)""", (sid,))
    fb = first.post(events_url, headers=headers,
                    json={"eventType": "FEEDBACK", "pageNumber": 2, "expectedRevision": 3, "clientEventId": "f2", "feedback": "SKIPPED"})
    assert fb.status_code == 201 and fb.json()["data"]["revision"] == 4

    # Branch validation: auth/ownership/missing/unapproved/wrong-page/shared-idempotency.
    assert first.post(branches_url, json={"branchId": 501, "expectedRevision": 4, "clientEventId": "b1"}).status_code == 403
    assert second.post(branches_url, headers=other, json={"branchId": 501, "expectedRevision": 4, "clientEventId": "b1"}).status_code == 404
    assert first.post(branches_url, headers=headers, json={"branchId": 9999, "expectedRevision": 4, "clientEventId": "b-miss"}).status_code == 404
    assert first.post(branches_url, headers=headers, json={"branchId": 502, "expectedRevision": 4, "clientEventId": "b-pend"}).status_code == 409
    assert first.post(branches_url, headers=headers, json={"branchId": 503, "expectedRevision": 4, "clientEventId": "b-page"}).status_code == 409
    assert first.post(branches_url, headers=headers, json={"branchId": 501, "expectedRevision": 4, "clientEventId": "a1"}).json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    chosen = first.post(branches_url, headers=headers, json={"branchId": 501, "expectedRevision": 4, "clientEventId": "b1"})
    assert chosen.status_code == 201 and chosen.json()["requestId"] == chosen.headers["X-Request-ID"]
    cdata = chosen.json()["data"]
    assert cdata["currentPage"] == 4 and cdata["revision"] == 5 and cdata["visitedPageNumbers"] == [1, 2, 4]
    assert cdata["nextPage"] == {"pageNumber": 4, "text": "分支第四页", "imageRef": None, "defaultNextPageNumber": None}

    b_replay = first.post(branches_url, headers=headers, json={"branchId": 501, "expectedRevision": 4, "clientEventId": "b1"})
    assert b_replay.status_code == 200 and b_replay.json()["data"] == cdata
    assert b_replay.json()["requestId"] == b_replay.headers["X-Request-ID"] != chosen.json()["requestId"]
    assert first.post(branches_url, headers=headers, json={"branchId": 501, "expectedRevision": 5, "clientEventId": "b1"}).json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    with closing(sqlite3.connect(path)) as db:
        row = db.execute("SELECT page_no, branch_id, event_type FROM interaction_events WHERE id = ?",
                         (cdata["interactionEventId"],)).fetchone()
        assert tuple(row) == (2, 501, "BRANCH_CHOSEN")
        ans_row = db.execute("SELECT input_text FROM interaction_events WHERE id = ?",
                             (ans.json()["data"]["interactionEventId"],)).fetchone()
        assert ans_row[0] == "小兔子帮了朋友"


def test_child_mode_can_choose_branch_and_cannot_call_recommendations(env):
    first, _, _ = env
    headers = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    profile = first.post("/api/v1/child-profiles", headers=headers,
                         json={"ageBand": "AGE_3_4", "preferredMinutes": 10}).json()["data"]
    sid = first.post("/api/v1/reading-sessions", headers=headers, json={
        "childProfileId": profile["childProfileId"], "storyVersionId": 11, "mode": "PARENT_CHILD"
    }).json()["data"]["readingSessionId"]
    assert first.put(f"/api/v1/reading-sessions/{sid}/mode-settings", headers=headers, json={
        "mode": "CHILD_SOLO", "allowedStoryVersionIds": [11], "timeLimitSeconds": 600
    }).status_code == 200
    child_headers = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    assert first.post("/api/v1/recommendations", headers=child_headers,
                      json={"childProfileId": profile["childProfileId"]}).status_code == 403
    assert first.post(f"/api/v1/reading-sessions/{sid}/events", headers=child_headers, json={
        "eventType": "STARTED", "pageNumber": 1, "expectedRevision": 0, "clientEventId": "cs"
    }).status_code == 201
    assert first.post(f"/api/v1/reading-sessions/{sid}/events", headers=child_headers, json={
        "eventType": "PAGE_CHANGED", "pageNumber": 2, "expectedRevision": 1, "clientEventId": "cp"
    }).status_code == 201
    branch = first.post(f"/api/v1/reading-sessions/{sid}/branches", headers=child_headers, json={
        "branchId": 501, "expectedRevision": 2, "clientEventId": "cb"
    })
    assert branch.status_code == 201 and branch.json()["data"]["currentPage"] == 4
