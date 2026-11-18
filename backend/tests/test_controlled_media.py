"""Tests for DCL-005 controlled cover/page image resolution and API-024 image delivery."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.v1.auth import _login_attempts
from app.config import Settings
from app.main import app, create_app
from app.services.story_media import REPO_ROOT, SEED_COVER_SOURCE_REF
from app.speech_config import SpeechSettings

SCHEMA = Path(__file__).resolve().parents[2] / "database" / "schema.sql"


@pytest.fixture
def media_env(tmp_path, monkeypatch):
    db_path = tmp_path / "media.sqlite3"
    monkeypatch.setenv("ZHIYA_DB_PATH", str(db_path))
    with closing(sqlite3.connect(db_path)) as db:
        db.executescript(SCHEMA.read_text(encoding="utf-8"))
    _login_attempts.clear()
    with TestClient(app, base_url="https://testserver") as first, TestClient(app, base_url="https://testserver") as second:
        for client, account in ((first, "guardian-one"), (second, "guardian-two")):
            creds = {"account": account, "password": "strong-pass-123"}
            assert client.post("/api/v1/auth/registrations", json=creds).status_code == 201
            assert client.post("/api/v1/auth/sessions", json=creds).status_code == 201
        yield first, second, db_path, tmp_path
    _login_attempts.clear()


def test_seed_cover_resolution_and_private_media_delivery(media_env):
    first, second, db_path, _ = media_env
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("INSERT INTO storybooks VALUES (2, NULL, 'LIBRARY', 1)")
        db.execute("""INSERT INTO story_versions
            (id, storybook_id, version_no, title, topics_json, age_min, age_max,
             source_ref, license_note, status, reviewed_by_id, reviewed_at, created_at, branching)
            VALUES (2, 2, 1, '小狐狸的雨伞', '["勇气"]', 3, 4, ?, 'ok', 'PUBLISHED', 1, 1, 1, 0)""",
            (SEED_COVER_SOURCE_REF,))
        db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref) VALUES (2, 1, '下雨了', '/assets/placeholder.png')")

    detail = first.get("/api/v1/story-versions/2")
    assert detail.status_code == 200
    data = detail.json()["data"]
    assert data["coverRef"] == "asset-2-cover"
    assert data["pages"][0]["imageRef"] is None
    shelf = first.get("/api/v1/storybooks").json()["data"]["items"]
    assert len(shelf) == 1 and shelf[0]["coverRef"] == "asset-2-cover" and shelf[0]["pages"][0]["imageRef"] is None

    with TestClient(app, base_url="https://testserver") as anon:
        assert anon.get("/api/v1/media/asset-2-cover").status_code == 401

    resp = second.get("/api/v1/media/asset-2-cover")
    expected_bytes = (REPO_ROOT / "knowledge-base_v2.5" / "图片" / "PIC-B01_小狐狸的雨伞.png").read_bytes()
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "private, no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.content == expected_bytes


def test_page_image_refs_access_control_child_scope_withdrawn_and_traversal(media_env, monkeypatch):
    first, second, db_path, tmp_path = media_env
    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "p1.png").write_bytes(b"\x89PNG\r\n\x1a\npage1")
    (media_root / "p2.jpg").write_bytes(b"\xff\xd8\xff\xe0page2")
    (media_root / "note.txt").write_bytes(b"not-image")
    (tmp_path / "secret.png").write_bytes(b"outside-root")
    cover_map = tmp_path / "cover_mapping.json"
    cover_map.write_text(json.dumps([{"book_no": "B-002", "storybook_id": 2, "cover_path": "p1.png"}]), encoding="utf-8")
    monkeypatch.setenv("ZHIYA_MEDIA_ROOT", str(media_root))
    monkeypatch.setenv("ZHIYA_COVER_MAPPING_PATH", str(cover_map))

    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        # id=2 is guardian-one ORIGINAL (must NOT match seed LIBRARY cover_mapping id=2).
        for bid, owner, src in ((2, 1, "ORIGINAL"), (10, None, "LIBRARY"), (11, 1, "ORIGINAL")):
            db.execute("INSERT INTO storybooks VALUES (?, ?, ?, 1)", (bid, owner, src))
        db.execute("""INSERT INTO story_versions
            (id, storybook_id, version_no, title, topics_json, age_min, age_max,
             source_ref, license_note, status, reviewed_by_id, reviewed_at, created_at, branching)
            VALUES (20, 2, 1, '我的原创', '["动物"]', 3, 5, NULL, 'ok', 'APPROVED_PRIVATE', 1, 1, 1, 0)""")
        db.execute("""INSERT INTO story_versions
            (id, storybook_id, version_no, title, topics_json, age_min, age_max,
             source_ref, license_note, status, reviewed_by_id, reviewed_at, created_at, branching)
            VALUES (100, 10, 1, '公开绘本', '["动物"]', 3, 5, NULL, 'ok', 'PUBLISHED', 1, 1, 1, 0)""")
        db.execute("""INSERT INTO story_versions
            (id, storybook_id, version_no, title, topics_json, age_min, age_max,
             source_ref, license_note, status, reviewed_by_id, reviewed_at, created_at, branching)
            VALUES (110, 11, 1, '历史私有', '["动物"]', 3, 5, NULL, 'ok', 'RETIRED', 1, 1, 1, 0)""")

        for pno, ref in (
            (1, "p1.png"),
            (2, "p2.jpg"),
            (3, "/assets/placeholder.png"),
            (4, "小兔子站在屋子中间"),
            (5, "../secret.png"),
            (6, "note.txt"),
            (7, "missing.png"),
            (8, "C:p1.png"),
        ):
            db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref) VALUES (20, ?, ?, ?)",
                       (pno, f"body-{pno}", ref))
        db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref) VALUES (100, 1, 'pub', 'p1.png')")
        db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref) VALUES (110, 1, 'ret', 'p2.jpg')")

    own_book = first.get("/api/v1/story-versions/20").json()["data"]
    assert own_book["coverRef"] is None
    assert [p["imageRef"] for p in own_book["pages"]] == [
        "asset-20-page-1", "asset-20-page-2", None, None, None, None, None, None,
    ]
    assert first.get("/api/v1/media/asset-20-page-1").content == b"\x89PNG\r\n\x1a\npage1"
    p2_resp = first.get("/api/v1/media/asset-20-page-2")
    assert p2_resp.status_code == 200 and p2_resp.headers["content-type"] == "image/jpeg"
    for blocked in (
        "asset-20-cover", "asset-20-page-3", "asset-20-page-4", "asset-20-page-5",
        "asset-20-page-6", "asset-20-page-7", "asset-20-page-8",
    ):
        assert first.get(f"/api/v1/media/{blocked}").status_code == 404
    assert second.get("/api/v1/media/asset-20-page-1").status_code == 404
    assert first.get("/api/v1/media/asset-110-page-1").content == b"\xff\xd8\xff\xe0page2"

    # CHILD_SOLO only accesses versions explicitly granted in child_grant_books.
    h1 = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
    pid = first.post("/api/v1/child-profiles", headers=h1,
                     json={"ageBand": "AGE_3_4", "preferredMinutes": 10}).json()["data"]["childProfileId"]
    sid = first.post("/api/v1/reading-sessions", headers=h1, json={
        "childProfileId": pid, "storyVersionId": 20, "mode": "PARENT_CHILD",
    }).json()["data"]["readingSessionId"]
    assert first.put(f"/api/v1/reading-sessions/{sid}/mode-settings", headers=h1, json={
        "mode": "CHILD_SOLO", "allowedStoryVersionIds": [20], "timeLimitSeconds": 300,
    }).status_code == 200
    assert first.get("/api/v1/media/asset-20-page-1").status_code == 200
    assert first.get("/api/v1/media/asset-100-page-1").status_code == 404  # PUBLISHED but ungranted

    # Withdrawing version 20 immediately blocks media access.
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("UPDATE story_versions SET status = 'WITHDRAWN' WHERE id = 20")
    assert first.get("/api/v1/media/asset-20-page-1").status_code == 404

    # RETIRED LIBRARY version media is accessible only during an active reading session.
    assert second.get("/api/v1/media/asset-100-page-1").status_code == 200
    h2 = {"X-CSRF-Token": second.cookies["zhiya_csrf"]}
    p2 = second.post("/api/v1/child-profiles", headers=h2,
                     json={"ageBand": "AGE_3_4", "preferredMinutes": 10}).json()["data"]["childProfileId"]
    s2 = second.post("/api/v1/reading-sessions", headers=h2, json={
        "childProfileId": p2, "storyVersionId": 100, "mode": "PARENT_CHILD",
    }).json()["data"]["readingSessionId"]
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("UPDATE story_versions SET status = 'RETIRED' WHERE id = 100")
    assert second.get("/api/v1/media/asset-100-page-1").status_code == 200
    assert second.delete(f"/api/v1/reading-sessions/{s2}", headers=h2).status_code == 204
    assert second.get("/api/v1/media/asset-100-page-1").status_code == 404


def test_media_dependency_unavailable_returns_retryable_503(media_env, monkeypatch):
    first, _, db_path, tmp_path = media_env
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("INSERT INTO storybooks VALUES (2, NULL, 'LIBRARY', 1)")
        db.execute("""INSERT INTO story_versions
            (id, storybook_id, version_no, title, topics_json, age_min, age_max,
             source_ref, license_note, status, reviewed_by_id, reviewed_at, created_at, branching)
            VALUES (2, 2, 1, '小狐狸', '["动物"]', 3, 4, ?, 'ok', 'PUBLISHED', 1, 1, 1, 0)""",
            (SEED_COVER_SOURCE_REF,))
        db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref) VALUES (2, 1, 't', 'p1.png')")

    monkeypatch.setenv("ZHIYA_MEDIA_ROOT", str(tmp_path / "missing-media-dir"))
    for url in ("/api/v1/storybooks", "/api/v1/story-versions/2", "/api/v1/media/asset-2-page-1"):
        res = first.get(url)
        assert res.status_code == 503
        assert res.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert res.json()["error"]["retryable"] is True


def test_branch_media_expired_child_grant_and_voice_media_compatibility(tmp_path, monkeypatch):
    db_path = tmp_path / "media-branch.sqlite3"
    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "p1.png").write_bytes(b"\x89PNG\r\n\x1a\npage1")
    (media_root / "p3.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBPpage3")
    monkeypatch.setenv("ZHIYA_DB_PATH", str(db_path))
    monkeypatch.setenv("ZHIYA_MEDIA_ROOT", str(media_root))
    with closing(sqlite3.connect(db_path)) as db:
        db.executescript(SCHEMA.read_text(encoding="utf-8"))

    class _FakeVoiceGateway:
        async def synthesize(self, *, text, voice_id, speech_rate):
            return b"RIFFfakewav"

    _login_attempts.clear()
    application = create_app(Settings(), SpeechSettings(api_key="test-key", tts_api_url="https://space.example.test/tts"))
    with TestClient(application, base_url="https://testserver") as first:
        application.state.voice_gateway = _FakeVoiceGateway()
        creds = {"account": "guardian-media", "password": "strong-pass-123"}
        assert first.post("/api/v1/auth/registrations", json=creds).status_code == 201
        assert first.post("/api/v1/auth/sessions", json=creds).status_code == 201
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("INSERT INTO storybooks VALUES (30, NULL, 'LIBRARY', 1)")
            db.execute("""INSERT INTO story_versions
                (id, storybook_id, version_no, title, topics_json, age_min, age_max,
                 source_ref, license_note, status, reviewed_by_id, reviewed_at, created_at, branching)
                VALUES (30, 30, 1, '分支绘本', '["动物"]', 3, 5, '第三方绘本', 'ok', 'PUBLISHED', 1, 1, 1, 1)""")
            db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref, default_next_page_number) VALUES (30, 1, '第1页', 'p1.png', 2)")
            db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref, default_next_page_number) VALUES (30, 2, '第2页', 'none', NULL)")
            db.execute("INSERT INTO story_pages (version_id, page_no, body_text, image_ref, default_next_page_number) VALUES (30, 3, '第3页', 'p3.webp', NULL)")
            db.execute("INSERT INTO story_branches (id, version_id, from_page_no, next_page_no, choice_label, review_status) VALUES (301, 30, 1, 3, '走右边', 'APPROVED')")

        # Non-seed LIBRARY version never maps to seed cover_mapping.
        assert first.get("/api/v1/story-versions/30").json()["data"]["coverRef"] is None

        h1 = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
        pid = first.post("/api/v1/child-profiles", headers=h1,
                         json={"ageBand": "AGE_3_4", "preferredMinutes": 10}).json()["data"]["childProfileId"]
        sid = first.post("/api/v1/reading-sessions", headers=h1, json={
            "childProfileId": pid, "storyVersionId": 30, "mode": "PARENT_CHILD",
        }).json()["data"]["readingSessionId"]
        assert first.post(f"/api/v1/reading-sessions/{sid}/events", headers=h1, json={
            "eventType": "STARTED", "pageNumber": 1, "expectedRevision": 0, "clientEventId": "st",
        }).status_code == 201

        # API-018 returns controlled nextPage.imageRef and serves image/webp via API-024.
        chosen = first.post(f"/api/v1/reading-sessions/{sid}/branches", headers=h1, json={
            "branchId": 301, "expectedRevision": 1, "clientEventId": "br",
        })
        assert chosen.status_code == 201
        assert chosen.json()["data"]["nextPage"]["imageRef"] == "asset-30-page-3"
        webp = first.get("/api/v1/media/asset-30-page-3")
        assert webp.status_code == 200 and webp.headers["content-type"] == "image/webp"

        # Existing G08 voice audio media route keeps working alongside asset-* image media.
        tts = first.post("/api/v1/voice/syntheses", headers=h1, json={
            "readingSessionId": sid, "pageNumber": 3, "revision": 2,
        })
        assert tts.status_code == 201
        audio = first.get(tts.json()["data"]["audioUrl"])
        assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav" and audio.content == b"RIFFfakewav"

        # Expired CHILD_SOLO grant is rejected with 401 AUTH_REQUIRED and settles TIME_LIMIT.
        sid2 = first.post("/api/v1/reading-sessions", headers=h1, json={
            "childProfileId": pid, "storyVersionId": 30, "mode": "PARENT_CHILD",
        }).json()["data"]["readingSessionId"]
        assert first.put(f"/api/v1/reading-sessions/{sid2}/mode-settings", headers=h1, json={
            "mode": "CHILD_SOLO", "allowedStoryVersionIds": [30], "timeLimitSeconds": 60,
        }).status_code == 200
        child_headers = {"X-CSRF-Token": first.cookies["zhiya_csrf"]}
        assert first.post(f"/api/v1/reading-sessions/{sid2}/events", headers=child_headers, json={
            "eventType": "STARTED", "pageNumber": 1, "expectedRevision": 0, "clientEventId": "cst",
        }).status_code == 201
        assert first.get("/api/v1/media/asset-30-page-1").status_code == 200
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("UPDATE child_grants SET issued_at = 1, expires_at = 60001 WHERE revoked_at IS NULL")
        expired = first.get("/api/v1/media/asset-30-page-1")
        assert expired.status_code == 401 and expired.json()["error"]["code"] == "AUTH_REQUIRED"
        with closing(sqlite3.connect(db_path)) as db:
            settled = db.execute("SELECT status, end_reason FROM reading_sessions WHERE id = ?", (sid2,)).fetchone()
        assert settled == ("FINISHED", "TIME_LIMIT")
    _login_attempts.clear()
