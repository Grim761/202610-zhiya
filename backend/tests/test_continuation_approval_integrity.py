"""Regression tests for story version approval lineage, branches, and pinned media."""

import sqlite3
from contextlib import closing
from io import BytesIO

import pytest
from PIL import Image

from test_continuations import _seed_continuation_draft, cont_env


def _seed_draft(cont_env, *, source_branch=False, branching=0):
    client, _, db_path, profile_id = cont_env
    headers = {"X-CSRF-Token": client.cookies["zhiya_csrf"]}
    created = client.post("/api/v1/reading-sessions", headers=headers, json={
        "childProfileId": profile_id, "storyVersionId": 301, "mode": "PARENT_CHILD",
    })
    assert created.status_code == 201, created.text
    session_url = f"/api/v1/reading-sessions/{created.json()['data']['readingSessionId']}"
    started = client.post(f"{session_url}/events", headers=headers, json={
        "eventType": "STARTED", "pageNumber": 1, "expectedRevision": 0, "clientEventId": "source-start",
    })
    assert started.status_code == 201, started.text
    step = {"expectedRevision": 1, "clientEventId": "source-next"}
    if source_branch:
        advanced = client.post(f"{session_url}/branches", headers=headers, json={**step, "branchId": 801})
        source_page = 3
    else:
        advanced = client.post(f"{session_url}/events", headers=headers, json={
            **step, "eventType": "PAGE_CHANGED", "pageNumber": 2,
        })
        source_page = 2
    assert advanced.status_code == 201, advanced.text
    _seed_continuation_draft(
        db_path, 302, 2, created.json()["data"]["readingSessionId"], 2, source_page, [1, source_page],
        branching=branching,
    )


def _approve(client):
    return client.post(
        "/api/v1/story-versions/302/approvals",
        headers={"X-CSRF-Token": client.cookies["zhiya_csrf"]},
        json={"versionNumber": 2, "reviewedAllBranches": True},
    )


def _version_states(db_path):
    with closing(sqlite3.connect(db_path)) as db:
        return db.execute("SELECT id, status FROM story_versions ORDER BY id").fetchall()


def _png(color):
    buffer = BytesIO()
    Image.new("RGB", (4, 4), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def illustrated_draft(cont_env, tmp_path, monkeypatch):
    client, _, db_path, _ = cont_env
    media_root = tmp_path / "media"
    media_root.mkdir()
    monkeypatch.setenv("ZHIYA_MEDIA_ROOT", str(media_root))
    original_png = _png("green")
    (media_root / "prefix.png").write_bytes(original_png)
    (media_root / "other.png").write_bytes(_png("red"))
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("UPDATE story_versions SET branching = 1 WHERE id = 301")
        db.execute("INSERT INTO story_branches VALUES (801, 301, 1, 3, '走近路', 'APPROVED')")
        db.execute("UPDATE story_pages SET image_ref = 'prefix.png' WHERE version_id = 301 AND page_no = 3")
    _seed_draft(cont_env, source_branch=True)
    original = client.get("/api/v1/media/asset-301-page-3")
    assert original.status_code == 200 and original.content == original_png
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("UPDATE story_pages SET image_ref = 'prefix.png' WHERE version_id = 302 AND page_no = 2")
    return media_root, original_png


def test_approval_rejects_continuation_of_another_owned_book(cont_env):
    client, _, db_path, _ = cont_env
    _seed_draft(cont_env)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("INSERT INTO storybooks VALUES (202, 1, 'ORIGINAL', 1)")
        db.execute("""INSERT INTO story_versions
            (id, storybook_id, version_no, title, topics_json, age_min, age_max, license_note,
             status, reviewed_by_id, reviewed_at, created_at, branching)
            VALUES (310, 202, 1, '独立故事B', '[]', 3, 5, '原创授权',
                    'APPROVED_PRIVATE', 1, 100, 1, 0)""")
        db.execute("""INSERT INTO story_pages
            (version_id, page_no, body_text, image_ref, default_next_page_number)
            SELECT 310, page_no, body_text, image_ref, default_next_page_number
            FROM story_pages WHERE version_id = 301""")
        db.execute("UPDATE story_versions SET storybook_id = 202 WHERE id = 302")

    rejected = _approve(client)
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _version_states(db_path) == [(301, "APPROVED_PRIVATE"), (302, "PENDING_REVIEW"), (310, "APPROVED_PRIVATE")]


def test_approval_rejects_distinct_branch_labels_with_same_destination(cont_env):
    client, _, db_path, _ = cont_env
    _seed_draft(cont_env, branching=1)
    with closing(sqlite3.connect(db_path)) as db, db:
        # Keep page 6 reachable even when both options lead to page 4.
        db.execute("UPDATE story_pages SET default_next_page_number = 6 WHERE version_id = 302 AND page_no = 5")
        db.execute("UPDATE story_branches SET next_page_no = 4 WHERE id = 902")

    rejected = _approve(client)
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _version_states(db_path) == [(301, "APPROVED_PRIVATE"), (302, "PENDING_REVIEW")]
    with closing(sqlite3.connect(db_path)) as db, db:
        assert db.execute("SELECT review_status FROM story_branches ORDER BY id").fetchall() == [("PENDING",)] * 2
        db.execute("UPDATE story_branches SET next_page_no = 6 WHERE id = 902")

    corrected = _approve(client)
    assert corrected.status_code == 200, corrected.text


def test_approval_requires_matching_prefix_image_and_reuses_frozen_source(cont_env, illustrated_draft):
    client, _, db_path, _ = cont_env
    media_root, original_png = illustrated_draft
    replacement_png = _png("blue")
    (media_root / "prefix.png").write_bytes(replacement_png)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("UPDATE story_pages SET image_ref = 'other.png' WHERE version_id = 302 AND page_no = 2")

    rejected = _approve(client)
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _version_states(db_path) == [(301, "APPROVED_PRIVATE"), (302, "PENDING_REVIEW")]

    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("UPDATE story_pages SET image_ref = 'prefix.png' WHERE version_id = 302 AND page_no = 2")
    approved = _approve(client)
    assert approved.status_code == 200, approved.text
    fetched = client.get("/api/v1/story-versions/302")
    assert fetched.status_code == 200, fetched.text
    for response in (approved, fetched):
        carried = response.json()["data"]["pages"][1]
        assert carried["pageNumber"] == 2 and carried["text"] == "v1第3页"
        image = client.get(f"/api/v1/media/{carried['imageRef']}")
        assert image.status_code == 200 and image.content == original_png


def test_approval_commit_failure_keeps_previous_version_and_retry_uses_current_image(cont_env, illustrated_draft):
    client, _, db_path, _ = cont_env
    media_root, original_png = illustrated_draft
    (media_root / "resume.png").write_bytes(_png("red"))
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("UPDATE story_pages SET image_ref = 'resume.png' WHERE version_id = 302 AND page_no = 3")
        # A deferred FK violation fails at commit, after the approval writes succeed.
        db.execute("CREATE TABLE approval_parent (id INTEGER PRIMARY KEY)")
        db.execute("""CREATE TABLE approval_commit_fault (
            id INTEGER REFERENCES approval_parent(id) DEFERRABLE INITIALLY DEFERRED
        )""")
        db.execute("""CREATE TRIGGER fail_approval_commit AFTER UPDATE OF status ON story_versions
            WHEN NEW.id = 302 AND NEW.status = 'APPROVED_PRIVATE'
            BEGIN INSERT INTO approval_commit_fault VALUES (1); END""")

    failed = _approve(client)
    assert failed.status_code == 503, failed.text
    assert failed.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert _version_states(db_path) == [(301, "APPROVED_PRIVATE"), (302, "PENDING_REVIEW")]
    assert client.get("/api/v1/media/asset-302-page-3").status_code == 404
    with closing(sqlite3.connect(db_path)) as db, db:
        assert db.execute("SELECT COUNT(*) FROM approval_commit_fault").fetchone() == (0,)
        db.execute("DROP TRIGGER fail_approval_commit")

    current_png = _png("blue")
    (media_root / "resume.png").write_bytes(current_png)
    approved = _approve(client)
    assert approved.status_code == 200, approved.text
    assert _version_states(db_path) == [(301, "RETIRED"), (302, "APPROVED_PRIVATE")]

    (media_root / "resume.png").write_bytes(_png("red"))
    pages = approved.json()["data"]["pages"]
    for page, expected in ((pages[1], original_png), (pages[2], current_png)):
        image = client.get(f"/api/v1/media/{page['imageRef']}")
        assert image.status_code == 200 and image.content == expected


def test_approval_freezes_image_for_original_without_continuation(cont_env, tmp_path, monkeypatch):
    client, _, db_path, _ = cont_env
    monkeypatch.setenv("ZHIYA_MEDIA_ROOT", str(tmp_path))
    image_file = tmp_path / "original.png"
    original_png = _png("green")
    image_file.write_bytes(original_png)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("""INSERT INTO story_versions
            (id, storybook_id, version_no, title, topics_json, age_min, age_max,
             license_note, status, created_at, branching)
            VALUES (302, 201, 2, '小狐狸原创第二版', '[]', 3, 5, '原创授权', 'PENDING_REVIEW', 1, 0)""")
        db.execute("""INSERT INTO story_pages
            (version_id, page_no, body_text, image_ref, default_next_page_number)
            SELECT 302, page_no, body_text,
                   CASE WHEN page_no = 1 THEN 'original.png' ELSE image_ref END,
                   default_next_page_number
            FROM story_pages WHERE version_id = 301""")

    approved = _approve(client)
    assert approved.status_code == 200, approved.text
    assert _version_states(db_path) == [(301, "RETIRED"), (302, "APPROVED_PRIVATE")]
    image_ref = approved.json()["data"]["pages"][0]["imageRef"]

    image_file.write_bytes(_png("red"))
    fetched = client.get("/api/v1/story-versions/302")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["data"]["pages"][0]["imageRef"] == image_ref
    image = client.get(f"/api/v1/media/{image_ref}")
    assert image.status_code == 200 and image.content == original_png
