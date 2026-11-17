"""Read-only guardian bookshelf; child grants and media delivery are future work."""

import json
import sqlite3
import time
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.v1.auth import Principal, authenticate_request, connect, error
from app.api.v1.profiles import guardian
from app.services.story_media import MediaDependencyError, cover_ref_for, page_image_ref_for

router = APIRouter(prefix="/api/v1")
MAX_ID = 9007199254740991
AgeBand = Literal["AGE_3_4", "AGE_4_5", "AGE_5_6"]
AGES = {"AGE_3_4": (3, 4), "AGE_4_5": (4, 5), "AGE_5_6": (5, 6)}
AGE_LABELS = {"AGE_3_4": "3～4 岁", "AGE_4_5": "4～5 岁", "AGE_5_6": "5～6 岁"}


def page_object(page_row) -> dict:
    return {
        "pageNumber": page_row["page_no"],
        "text": page_row["body_text"],
        "imageRef": page_image_ref_for(page_row["version_id"], page_row["page_no"], page_row["image_ref"]),
        "defaultNextPageNumber": page_row["default_next_page_number"],
    }


def version_object(db, row):
    pages = db.execute(
        "SELECT * FROM story_pages WHERE version_id = ? ORDER BY page_no", (row["id"],)
    ).fetchall()
    branches = db.execute(
        "SELECT * FROM story_branches WHERE version_id = ? AND review_status = 'APPROVED' ORDER BY id",
        (row["id"],),
    ).fetchall()
    source_type = row["storybook_source_type"] if "storybook_source_type" in row.keys() else (
        db.execute("SELECT source_type FROM storybooks WHERE id = ?", (row["storybook_id"],)).fetchone()["source_type"]
    )
    data = {
        "storybookId": row["storybook_id"], "storyVersionId": row["id"],
        "versionNumber": row["version_no"], "title": row["title"], "status": row["status"],
        "coverRef": cover_ref_for(row["storybook_id"], row["id"], source_type, row["source_ref"]),
        "pages": [page_object(p) for p in pages],
        "branches": [{"branchId": b["id"], "fromPageNumber": b["from_page_no"],
                      "nextPageNumber": b["next_page_no"], "choiceLabel": b["choice_label"],
                      "reviewStatus": b["review_status"]} for b in branches],
    }
    if row["source_session_id"] is not None:
        data["sourceStoryVersionId"] = row["source_version_id"]
        data["continuation"] = {
            "sourceReadingSessionId": row["source_session_id"],
            "sourceStoryVersionId": row["source_version_id"],
            "sourceRevision": row["source_revision"],
            "sourcePageNumber": row["source_page_no"],
            "sourceVisitedPageNumbers": json.loads(row["source_visited_pages_json"]),
            "resumePageNumber": row["resume_page_no"],
        }
    return data


class VersionApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    versionNumber: int = Field(ge=1, le=MAX_ID, strict=True)
    reviewedAllBranches: bool = Field(strict=True)


class RecommendationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    childProfileId: int = Field(ge=1, le=MAX_ID, strict=True)
    topic: str | None = Field(default=None, min_length=1, max_length=100)
    minutes: int | None = Field(default=None, ge=1, le=30, strict=True)

    @field_validator("topic", mode="before")
    @classmethod
    def clean_topic(cls, value):
        return value.strip() if isinstance(value, str) else value


# PUBLISHED is public to authenticated guardians; private versions require ownership.
# Retired versions are available only as reviewed private originals belonging to the guardian.
VISIBLE = """(v.status = 'PUBLISHED' OR
    (v.status = 'APPROVED_PRIVATE' AND b.owner_guardian_id = ?) OR
    (? AND v.status = 'RETIRED' AND b.source_type = 'ORIGINAL'
       AND b.owner_guardian_id = ? AND v.reviewed_by_id IS NOT NULL AND v.reviewed_at IS NOT NULL))"""


def accessible_story_version(db, principal: Principal, version_id: int):
    """Fetch a story_version if accessible to principal under common.versionAccess."""
    grant_filter = (
        "AND EXISTS (SELECT 1 FROM child_grant_books gb WHERE gb.grant_id = ? AND gb.version_id = v.id) "
        if principal.role == "CHILD" else ""
    )
    active_session_filter = (
        "EXISTS (SELECT 1 FROM reading_sessions s WHERE s.version_id = v.id AND s.grant_id = ? "
        "AND s.profile_id = ? AND s.status IN ('CREATED', 'IN_PROGRESS'))"
        if principal.role == "CHILD"
        else "EXISTS (SELECT 1 FROM reading_sessions s JOIN child_profiles p ON p.id = s.profile_id "
             "WHERE s.version_id = v.id AND p.guardian_id = ? AND s.status IN ('CREATED', 'IN_PROGRESS'))"
    )
    params = (
        (version_id, principal.grant_id, principal.guardian_id, True, principal.guardian_id,
         principal.grant_id, principal.profile_id)
        if principal.role == "CHILD"
        else (version_id, principal.guardian_id, True, principal.guardian_id, principal.guardian_id)
    )
    return db.execute(
        "SELECT v.*, b.source_type AS storybook_source_type FROM story_versions v "
        "JOIN storybooks b ON b.id = v.storybook_id "
        f"WHERE v.id = ? {grant_filter}AND (" + VISIBLE +
        " OR (v.status = 'RETIRED' AND v.reviewed_by_id IS NOT NULL AND v.reviewed_at IS NOT NULL "
        f"AND b.source_type = 'LIBRARY' AND {active_session_filter}))",
        params,
    ).fetchone()


@router.get("/storybooks")
def list_storybooks(request: Request, age_band: AgeBand | None = Query(default=None, alias="ageBand"),
                    topic: str | None = Query(default=None, min_length=1, max_length=100),
                    scope: Literal["mine"] | None = None,
                    page: int = Query(default=1, ge=1, le=9007199254740991),
                    page_size: int = Query(default=20, ge=1, le=50, alias="pageSize")):
    try:
        with closing(connect()) as db:
            db.execute("BEGIN")
            principal, failure = authenticate_request(db, request, write=False)
            if failure:
                return failure
            if principal.role == "CHILD" and scope == "mine":
                return error(request, 403, "FORBIDDEN", "儿童模式不支持查看个人历史书架")
            conditions = [VISIBLE]
            params = [principal.guardian_id, scope == "mine" or principal.role == "CHILD", principal.guardian_id]
            if principal.role == "CHILD":
                conditions.append("EXISTS (SELECT 1 FROM child_grant_books gb WHERE gb.grant_id = ? AND gb.version_id = v.id)")
                params.append(principal.grant_id)
            if age_band:
                low, high = AGES[age_band]
                conditions.append("v.age_min <= ? AND v.age_max >= ?")
                params.extend((high, low))
            if topic is not None:
                conditions.append("EXISTS (SELECT 1 FROM json_each(v.topics_json) WHERE value = ?)")
                params.append(topic)
            # Avoid exceeding SQLite's signed 64-bit integer range for very large pages.
            offset = min((page - 1) * page_size, 9223372036854775807)
            rows = db.execute(
                "SELECT v.*, b.source_type AS storybook_source_type FROM story_versions v "
                "JOIN storybooks b ON b.id = v.storybook_id WHERE "
                + " AND ".join(conditions) + " ORDER BY v.storybook_id, v.version_no DESC LIMIT ? OFFSET ?",
                (*params, page_size, offset),
            ).fetchall()
            items = [version_object(db, row) for row in rows]
            return {"data": {"items": items, "page": page, "pageSize": page_size}, "requestId": request.state.request_id}
    except (sqlite3.Error, MediaDependencyError):
        return error(request, 503, "DEPENDENCY_UNAVAILABLE", "服务暂不可用", retryable=True)


@router.get("/story-versions/{version_id}")
def get_story_version(request: Request, version_id: int = Path(ge=1, le=9007199254740991)):
    try:
        with closing(connect()) as db:
            db.execute("BEGIN")
            principal, failure = authenticate_request(db, request, write=False)
            if failure:
                return failure
            row = accessible_story_version(db, principal, version_id)
            if not row:
                return error(request, 404, "RESOURCE_NOT_FOUND", "绘本不可用")
            return {"data": version_object(db, row), "requestId": request.state.request_id}
    except (sqlite3.Error, MediaDependencyError):
        return error(request, 503, "DEPENDENCY_UNAVAILABLE", "服务暂不可用", retryable=True)


@router.post("/recommendations")
def recommend_storybooks(payload: RecommendationRequest, request: Request):
    """API-010: age-band and topic recommendations across accessible current PUBLISHED/APPROVED_PRIVATE versions."""
    try:
        with closing(connect()) as db:
            db.execute("BEGIN")
            owner, failure = guardian(db, request, write=True)
            if failure:
                return failure
            profile = db.execute(
                "SELECT id, age_band, interests_json FROM child_profiles WHERE id = ? AND guardian_id = ?",
                (payload.childProfileId, owner),
            ).fetchone()
            if not profile:
                return error(request, 404, "RESOURCE_NOT_FOUND", "儿童档案不可用")
            low, high = AGES[profile["age_band"]]
            conditions = [
                VISIBLE,
                "v.age_min <= ? AND v.age_max >= ?",
                "EXISTS (SELECT 1 FROM story_pages p WHERE p.version_id = v.id AND p.page_no = 1)",
            ]
            params: list[object] = [owner, False, owner, high, low]
            if payload.topic is not None:
                conditions.append("EXISTS (SELECT 1 FROM json_each(v.topics_json) WHERE value = ?)")
                params.append(payload.topic)
            rows = db.execute(
                "SELECT v.* FROM story_versions v JOIN storybooks b ON b.id = v.storybook_id WHERE "
                + " AND ".join(conditions)
                + " ORDER BY v.storybook_id, v.version_no DESC LIMIT 50",
                params,
            ).fetchall()
            interests = set(json.loads(profile["interests_json"]))
            scored = []
            for row in rows:
                topics = json.loads(row["topics_json"])
                matched = [t for t in topics if t in interests]
                scored.append((0 if matched else 1, row["storybook_id"], row, matched))
            scored.sort(key=lambda item: (item[0], item[1]))
            age_label = AGE_LABELS[profile["age_band"]]
            items = []
            for _, _, row, matched in scored[:10]:
                if payload.topic is not None:
                    reason = f"符合 {age_label} 与主题「{payload.topic}」"
                elif matched:
                    reason = f"符合 {age_label} 与兴趣「{matched[0]}」"
                else:
                    reason = f"适合 {age_label} 共读"
                items.append({"storyVersionId": row["id"], "reason": reason, "sources": []})
            return {"data": {"items": items}, "requestId": request.state.request_id}
    except sqlite3.Error:
        return error(request, 503, "DEPENDENCY_UNAVAILABLE", "服务暂不可用")


def _verify_version_for_approval(db, version_row, owner: int) -> str | None:
    """Re-verify structure, DAG path bounds (4-6 pages), branch rules and continuation snapshot."""
    if not (version_row["title"] or "").strip() or not (version_row["license_note"] or "").strip():
        return "绘本标题或授权说明为空"
    pages = db.execute(
        "SELECT * FROM story_pages WHERE version_id = ? ORDER BY page_no", (version_row["id"],)
    ).fetchall()
    if not 4 <= len(pages) <= 6:
        return "绘本页数须为 4 至 6 页"
    page_map = {p["page_no"]: p for p in pages}
    if 1 not in page_map:
        return "缺少第 1 页"
    for p in pages:
        if not (p["body_text"] or "").strip():
            return "存在空白正文页面"
        nxt = p["default_next_page_number"]
        if nxt is not None and nxt not in page_map:
            return "存在指向不存在页面的默认下一页"

    branches = db.execute(
        "SELECT * FROM story_branches WHERE version_id = ? ORDER BY id", (version_row["id"],)
    ).fetchall()
    by_from: dict[int, list] = {}
    for b in branches:
        if b["review_status"] == "REJECTED":
            return "存在已驳回的分支选项"
        by_from.setdefault(b["from_page_no"], []).append(b)
    if version_row["branching"] == 0:
        if branches:
            return "非分支版本不得包含故事分支"
    else:
        if not 1 <= len(by_from) <= 2 or any(len(opts) < 2 for opts in by_from.values()):
            return "分支版本须包含 1 至 2 个决策节点且每节点至少两个选项"

    reachable: set[int] = set()
    stack: list[tuple[int, set[int], int]] = [(1, {1}, 1)]
    while stack:
        node, path_seen, length = stack.pop()
        reachable.add(node)
        nexts: list[int] = []
        default_next = page_map[node]["default_next_page_number"]
        if default_next is not None:
            nexts.append(default_next)
        for b in by_from.get(node, ()):
            if b["next_page_no"] not in nexts:
                nexts.append(b["next_page_no"])
        if not nexts:
            if not 4 <= length <= 6:
                return "存在不符合 4 至 6 页长度的阅读路径"
            continue
        for nxt in nexts:
            if nxt in path_seen or nxt not in page_map:
                return "绘本阅读路径存在环路或无效跳转"
            stack.append((nxt, path_seen | {nxt}, length + 1))
    if reachable != set(page_map):
        return "存在不可达页面"

    if version_row["source_session_id"] is not None:
        if (version_row["source_version_id"] is None or version_row["source_revision"] is None
                or version_row["source_page_no"] is None or version_row["resume_page_no"] is None
                or version_row["source_visited_pages_json"] is None):
            return "续写版本缺少源会话快照"
        prefix = json.loads(version_row["source_visited_pages_json"])
        if not isinstance(prefix, list) or not prefix or version_row["resume_page_no"] != len(prefix) + 1:
            return "续写前缀或继续页码不合法"
        for pno in range(1, len(prefix) + 1):
            if pno not in page_map:
                return "续写前文页码不完整"
        for pno in range(1, len(prefix)):
            if page_map[pno]["default_next_page_number"] != pno + 1:
                return "续写前文阅读路径不连续"
        resume_no = version_row["resume_page_no"]
        k = len(prefix)
        k_targets = {b["next_page_no"] for b in by_from.get(k, ())}
        if page_map[k]["default_next_page_number"] is not None:
            k_targets.add(page_map[k]["default_next_page_number"])
        if resume_no not in page_map or resume_no not in k_targets:
            return "续写继续页码不可达"
        src = db.execute(
            """SELECT s.id FROM reading_sessions s JOIN child_profiles p ON p.id = s.profile_id
               WHERE s.id = ? AND p.guardian_id = ? AND s.version_id = ?""",
            (version_row["source_session_id"], owner, version_row["source_version_id"]),
        ).fetchone()
        if not src:
            return "续写源会话不存在或无权访问"
    return None


@router.post("/story-versions/{version_id}/approvals")
def approve_story_version(payload: VersionApproval, request: Request,
                          version_id: int = Path(ge=1, le=MAX_ID)):
    """API-026: re-verify and privately approve an owned ORIGINAL story version (retiring prior active version)."""
    try:
        with closing(connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            owner, failure = guardian(db, request, write=True)
            if failure:
                return failure
            row = db.execute(
                """SELECT v.*, b.source_type AS storybook_source_type FROM story_versions v
                   JOIN storybooks b ON b.id = v.storybook_id
                   WHERE v.id = ? AND b.owner_guardian_id = ? AND b.source_type = 'ORIGINAL'""",
                (version_id, owner),
            ).fetchone()
            if not row or row["status"] == "WITHDRAWN":
                return error(request, 404, "RESOURCE_NOT_FOUND", "绘本草稿不可用")
            if not payload.reviewedAllBranches:
                return error(request, 422, "VALIDATION_ERROR", "请先完成全部页面与分支审核")
            if row["status"] != "PENDING_REVIEW" or row["version_no"] != payload.versionNumber:
                return error(request, 409, "SESSION_CONFLICT", "版本状态或版本号已变化")
            reason = _verify_version_for_approval(db, row, owner)
            if reason is not None:
                return error(request, 422, "VALIDATION_ERROR", reason)
            now = int(time.time() * 1000)
            db.execute(
                """UPDATE story_versions SET status = 'RETIRED'
                   WHERE storybook_id = ? AND status IN ('PUBLISHED', 'APPROVED_PRIVATE') AND id <> ?""",
                (row["storybook_id"], version_id),
            )
            db.execute(
                "UPDATE story_branches SET review_status = 'APPROVED' WHERE version_id = ? AND review_status = 'PENDING'",
                (version_id,),
            )
            db.execute(
                """UPDATE story_versions SET status = 'APPROVED_PRIVATE', reviewed_by_id = ?, reviewed_at = ?
                   WHERE id = ? AND status = 'PENDING_REVIEW'""",
                (owner, now, version_id),
            )
            updated = db.execute(
                """SELECT v.*, b.source_type AS storybook_source_type FROM story_versions v
                   JOIN storybooks b ON b.id = v.storybook_id WHERE v.id = ?""",
                (version_id,),
            ).fetchone()
            return {"data": version_object(db, updated), "requestId": request.state.request_id}
    except (sqlite3.Error, MediaDependencyError):
        return error(request, 503, "DEPENDENCY_UNAVAILABLE", "服务暂不可用")
