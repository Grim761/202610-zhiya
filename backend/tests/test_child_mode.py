"""Child-mode grant issuance (API-020), scoped access/projection, voice integration and parent unlock (API-021)."""

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.v1.auth import _login_attempts
from app.config import Settings
from app.main import create_app
from app.schemas.agent import ModelGenerateResult
from app.speech_config import SpeechSettings
from app.voice_gateway import Transcription
from test_voice_api import wav

SCHEMA = Path(__file__).resolve().parents[2] / "database" / "schema.sql"


def _login(client, account="guardian-one", password="strong-pass-123"):
    creds = {"account": account, "password": password}
    assert client.post("/api/v1/auth/registrations", json=creds).status_code == 201
    assert client.post("/api/v1/auth/sessions", json=creds).status_code == 201
    return {"X-CSRF-Token": client.cookies["zhiya_csrf"]}


@pytest.fixture
def child_env(tmp_path, monkeypatch):
    path = tmp_path / "child-mode.sqlite3"
    monkeypatch.setenv("ZHIYA_DB_PATH", str(path))
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA.read_text(encoding="utf-8"))
    _login_attempts.clear()
    application = create_app(Settings(), SpeechSettings(api_key="test-key", tts_api_url="https://space.example.test/tts"))
    with TestClient(application, base_url="https://testserver") as first, \
            TestClient(application, base_url="https://testserver") as second:
        application.state.voice_gateway = AsyncMock()
        application.state.voice_gateway.synthesize.return_value = wav(24000)
        application.state.voice_gateway.transcribe.return_value = Transcription("儿童朗读识别")
        h1 = _login(first, "guardian-one")
        _login(second, "guardian-two")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA foreign_keys = ON")
            for bid, owner, src in ((1, None, "LIBRARY"), (2, None, "LIBRARY"), (3, 1, "ORIGINAL"), (4, 2, "ORIGINAL")):
                db.execute("INSERT INTO storybooks VALUES (?, ?, ?, 1)", (bid, owner, src))
            for vid, bid, vno, status in (
                (11, 1, 1, "PUBLISHED"), (12, 2, 1, "PUBLISHED"),
                (13, 2, 2, "RETIRED"), (31, 3, 1, "RETIRED"), (32, 3, 2, "APPROVED_PRIVATE"),
                (41, 4, 1, "APPROVED_PRIVATE"), (14, 1, 2, "WITHDRAWN"),
            ):
                db.execute("""INSERT INTO story_versions
                    (id, storybook_id, version_no, title, topics_json, age_min, age_max,
                     license_note, status, reviewed_by_id, reviewed_at, created_at, branching)
                    VALUES (?, ?, ?, ?, '["动物"]', 3, 6, 'ok', ?, 1, 1, 1, 0)""",
                    (vid, bid, vno, f"book-{vid}", status))
                db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref) VALUES (?, 2, '第二页', 'none')", (vid,))
                db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref, default_next_page_number) VALUES (?, 1, '第一页', 'none', 2)", (vid,))
        profile_id = first.post("/api/v1/child-profiles", headers=h1,
                                json={"ageBand": "AGE_4_5", "preferredMinutes": 10}).json()["data"]["childProfileId"]
        yield application, first, second, path, profile_id
    _login_attempts.clear()


def test_enter_child_mode_scopes_access_voice_and_parent_unlock(child_env, monkeypatch):
    application, first, second, path, profile_id = child_env
    guardian_headers = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    old_guardian_token = first.cookies["zhiya_session"]

    s_child = first.post("/api/v1/reading-sessions", headers=guardian_headers, json={
        "childProfileId": profile_id, "storyVersionId": 11, "mode": "PARENT_CHILD"
    }).json()["data"]["readingSessionId"]
    s_other = first.post("/api/v1/reading-sessions", headers=guardian_headers, json={
        "childProfileId": profile_id, "storyVersionId": 12, "mode": "PARENT_CHILD"
    }).json()["data"]["readingSessionId"]

    mode_url = f"/api/v1/reading-sessions/{s_child}/mode-settings"
    # Validation & versionAccess checks before issuing grant.
    assert first.put(mode_url, headers=guardian_headers, json={
        "mode": "CHILD_SOLO", "allowedStoryVersionIds": [12], "timeLimitSeconds": 600
    }).status_code == 422  # must include session's storyVersionId (11)
    for bad_vid in (13, 14, 41):  # library RETIRED, WITHDRAWN, other guardian's private
        assert first.put(mode_url, headers=guardian_headers, json={
            "mode": "CHILD_SOLO", "allowedStoryVersionIds": [11, bad_vid], "timeLimitSeconds": 600
        }).status_code == 404

    # Enter CHILD_SOLO granting 11 (PUBLISHED) and 31 (owned reviewed ORIGINAL RETIRED).
    res = first.put(mode_url, headers=guardian_headers, json={
        "mode": "CHILD_SOLO", "allowedStoryVersionIds": [11, 31], "timeLimitSeconds": 600
    })
    assert res.status_code == 200 and res.json()["requestId"] == res.headers["X-Request-ID"]
    data = res.json()["data"]
    assert data["mode"] == "CHILD_SOLO" and data["allowedStoryVersionIds"] == [11, 31]
    assert data["expiresAt"].endswith("Z")
    child_token = first.cookies["zhiya_session"]
    child_headers = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    assert child_token != old_guardian_token

    # Old guardian cookie on the same device was revoked in the same transaction.
    stale = TestClient(application, base_url="https://testserver")
    stale.cookies.set("zhiya_session", old_guardian_token)
    assert stale.get("/api/v1/child-profiles").status_code == 401

    # Child cannot access guardian-only endpoints or scope=mine.
    assert first.get("/api/v1/child-profiles").status_code == 403
    assert first.post("/api/v1/child-profiles", headers=child_headers,
                      json={"ageBand": "AGE_3_4", "preferredMinutes": 10}).status_code == 403
    assert first.get("/api/v1/reading-sessions").status_code == 403
    assert first.delete(f"/api/v1/reading-sessions/{s_child}", headers=child_headers).status_code == 403
    assert first.put(mode_url, headers=child_headers, json={
        "mode": "CHILD_SOLO", "allowedStoryVersionIds": [11], "timeLimitSeconds": 300
    }).status_code == 403
    assert first.get("/api/v1/storybooks", params={"scope": "mine"}).status_code == 403

    # Child bookshelf and version details are scoped to allowedStoryVersionIds.
    shelf_ids = [b["storyVersionId"] for b in first.get("/api/v1/storybooks").json()["data"]["items"]]
    assert shelf_ids == [11, 31]
    assert first.get("/api/v1/story-versions/31").status_code == 200
    assert first.get("/api/v1/story-versions/12").status_code == 404  # PUBLISHED but not granted

    # Child can read/advance bound session, cannot read sibling session, and report stays null.
    assert first.get(f"/api/v1/reading-sessions/{s_other}").status_code == 404
    session_view = first.get(f"/api/v1/reading-sessions/{s_child}").json()["data"]
    assert session_view["session"]["mode"] == "CHILD_SOLO" and session_view["session"]["expiresAt"] == data["expiresAt"]
    assert session_view["report"] is None
    assert first.post(f"/api/v1/reading-sessions/{s_child}/events", headers=child_headers, json={
        "eventType": "STARTED", "pageNumber": 1, "expectedRevision": 0, "clientEventId": "c-start"
    }).status_code == 201

    # Voice synthesis/media/transcription work in child mode via GuardianVoiceAuthenticator -> child_grant_id.
    synth = first.post("/api/v1/voice/syntheses", headers=child_headers, json={
        "readingSessionId": s_child, "pageNumber": 1, "revision": 1
    })
    assert synth.status_code == 201
    assert first.get(synth.json()["data"]["audioUrl"]).content == wav(24000)
    assert first.post("/api/v1/voice/transcriptions", headers=child_headers,
                      data={"readingSessionId": s_child, "pageNumber": 1, "revision": 1},
                      files={"file": ("input.wav", wav(), "audio/wav")}).status_code == 200

    # Child finishes session: report is projected as null on finish and replay, while saved for parent.
    class _FakeAdapter:
        async def generate(self, _req):
            return ModelGenerateResult(success=True, text=json.dumps({"suggestionId": "TALK_ABOUT_STORY"}))

    monkeypatch.setattr(application.state, "model_adapter", _FakeAdapter(), raising=False)
    finish_body = {"reason": "USER_FINISH", "expectedRevision": 1, "clientEventId": "c-end"}
    done = first.post(f"/api/v1/reading-sessions/{s_child}/completions", headers=child_headers, json=finish_body)
    assert done.status_code == 200 and done.json()["data"]["report"] is None
    replay = first.post(f"/api/v1/reading-sessions/{s_child}/completions", headers=child_headers, json=finish_body)
    assert replay.status_code == 200 and replay.json()["data"]["report"] is None
    assert replay.json()["requestId"] == replay.headers["X-Request-ID"] != done.json()["requestId"]

    # Wrong credentials or wrong guardian account fail 401 without revoking child grant.
    assert first.post("/api/v1/auth/parent-unlocks", headers=child_headers,
                      json={"account": "guardian-two", "password": "strong-pass-123"}).status_code == 401
    assert first.post("/api/v1/auth/parent-unlocks", headers=child_headers,
                      json={"account": "guardian-one", "password": "wrong-password"}).status_code == 401
    assert first.get(f"/api/v1/reading-sessions/{s_child}").status_code == 200

    # Correct parent unlock revokes child grant, issues a fresh guardian cookie, and shows the report.
    unlock = first.post("/api/v1/auth/parent-unlocks", headers=child_headers,
                        json={"account": "guardian-one", "password": "strong-pass-123"})
    assert unlock.status_code == 200 and unlock.json()["data"]["accountId"] == 1
    assert first.cookies["zhiya_session"] not in (old_guardian_token, child_token)
    assert first.get(f"/api/v1/reading-sessions/{s_child}").json()["data"]["report"]["status"] == "READY"
    old_child = TestClient(application, base_url="https://testserver")
    old_child.cookies.set("zhiya_session", child_token)
    assert old_child.get("/api/v1/storybooks").status_code == 401


def test_shorten_grant_from_second_guardian_device_and_unlock_after_expiry(child_env):
    application, first, _, path, profile_id = child_env
    h1 = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    sid = first.post("/api/v1/reading-sessions", headers=h1, json={
        "childProfileId": profile_id, "storyVersionId": 11, "mode": "PARENT_CHILD"
    }).json()["data"]["readingSessionId"]
    assert first.put(f"/api/v1/reading-sessions/{sid}/mode-settings", headers=h1, json={
        "mode": "CHILD_SOLO", "allowedStoryVersionIds": [11], "timeLimitSeconds": 900
    }).status_code == 200
    child_headers = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    assert first.post(f"/api/v1/reading-sessions/{sid}/events", headers=child_headers, json={
        "eventType": "STARTED", "pageNumber": 1, "expectedRevision": 0, "clientEventId": "start"
    }).status_code == 201

    # Second logged-in guardian device can shorten expiresAt, cannot extend or change allowed versions.
    with TestClient(application, base_url="https://testserver") as second_device:
        assert second_device.post("/api/v1/auth/sessions",
                                  json={"account": "guardian-one", "password": "strong-pass-123"}).status_code == 201
        h2 = {"X-CSRF-Token": second_device.cookies["zhiya_csrf"]}
        mode_url = f"/api/v1/reading-sessions/{sid}/mode-settings"
        assert second_device.put(mode_url, headers=h2, json={
            "mode": "CHILD_SOLO", "allowedStoryVersionIds": [11, 31], "timeLimitSeconds": 300
        }).status_code == 409
        assert second_device.put(mode_url, headers=h2, json={
            "mode": "CHILD_SOLO", "allowedStoryVersionIds": [11], "timeLimitSeconds": 1800
        }).status_code == 409
        shortened = second_device.put(mode_url, headers=h2, json={
            "mode": "CHILD_SOLO", "allowedStoryVersionIds": [11], "timeLimitSeconds": 120
        })
        assert shortened.status_code == 200

    # Once expired, child reading calls fail 401, but parent-unlocks can still unlock and interrupt session.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE child_grants SET issued_at = 1, expires_at = 60001")
    assert first.get(f"/api/v1/reading-sessions/{sid}").status_code == 401
    unlock = first.post("/api/v1/auth/parent-unlocks", headers=child_headers,
                        json={"account": "guardian-one", "password": "strong-pass-123"})
    assert unlock.status_code == 200
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT status, end_reason FROM reading_sessions WHERE id = ?", (sid,)).fetchone() == (
            "INTERRUPTED", "GRANT_REVOKED"
        )


def test_child_logout_revokes_grant_and_interrupts_session(child_env):
    application, first, _, path, profile_id = child_env
    h1 = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    sid = first.post("/api/v1/reading-sessions", headers=h1, json={
        "childProfileId": profile_id, "storyVersionId": 11, "mode": "PARENT_CHILD"
    }).json()["data"]["readingSessionId"]
    assert first.put(f"/api/v1/reading-sessions/{sid}/mode-settings", headers=h1, json={
        "mode": "CHILD_SOLO", "allowedStoryVersionIds": [11], "timeLimitSeconds": 600
    }).status_code == 200
    child_token = first.cookies["zhiya_session"]
    child_headers = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    assert first.delete("/api/v1/auth/sessions/current", headers=child_headers).status_code == 204
    with TestClient(application, base_url="https://testserver") as replay:
        replay.cookies.set("zhiya_session", child_token)
        assert replay.get("/api/v1/storybooks").status_code == 401
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT revoked_at FROM child_grants").fetchone()[0] is not None
        assert db.execute("SELECT status, end_reason FROM reading_sessions WHERE id = ?", (sid,)).fetchone() == (
            "INTERRUPTED", "GRANT_REVOKED"
        )
