"""Controlled image media resolution and path-confined reading (DCL-005 / API-024)."""

import json
import os
import re
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[3]
MEDIA_DIR_PREFIX = ("knowledge-base_v2.5", "图片")
DEFAULT_MEDIA_ROOT = (REPO_ROOT.joinpath(*MEDIA_DIR_PREFIX)).resolve()
DEFAULT_COVER_MAPPING = REPO_ROOT / "data" / "seed" / "cover_mapping.json"
SEED_COVER_SOURCE_REF = "妈妈夸我说的队 · 知芽项目自研绘本"
ALLOWED_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
ASSET_KEY_RE = re.compile(r"^asset-([1-9][0-9]{0,15})-(cover|page-([1-9][0-9]{0,15}))$")
MAX_ID = 9007199254740991
SNIFF_BYTES = 12


class MediaDependencyError(RuntimeError):
    """Raised when the configured media root or cover mapping cannot be read."""


def detect_image_type(header: bytes) -> str | None:
    """Return the media type implied by the leading bytes, or None for empty or unsupported content."""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= SNIFF_BYTES and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def _media_root() -> Path:
    """Directory that every media reference is resolved against and confined to."""
    configured = os.environ.get("ZHIYA_MEDIA_ROOT", "").strip()
    if not configured:
        return DEFAULT_MEDIA_ROOT
    try:
        resolved = Path(configured).resolve()
        if not resolved.is_dir():
            raise MediaDependencyError("Configured media root is unavailable")
    except OSError as exc:
        raise MediaDependencyError("Configured media root is unavailable") from exc
    return resolved


def _cover_mapping() -> dict[tuple[int, int], str]:
    """Map (storybook_id, version_no) to a cover reference. Entries without version_no apply to version 1."""
    configured = os.environ.get("ZHIYA_COVER_MAPPING_PATH", "").strip()
    mapping_path = Path(configured) if configured else DEFAULT_COVER_MAPPING
    try:
        entries = json.loads(mapping_path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise MediaDependencyError("Invalid cover mapping format")
        return {
            (int(item["storybook_id"]), int(item.get("version_no", 1))): str(item["cover_path"])
            for item in entries
            if isinstance(item, dict) and "storybook_id" in item and "cover_path" in item
        }
    except (OSError, ValueError, TypeError) as exc:
        raise MediaDependencyError("Cover mapping is unavailable") from exc


def _resolve_image_path(raw_ref: str | None) -> tuple[Path, str] | None:
    """Resolve a reference inside the media root to an existing, non-empty image file and its real type."""
    if not raw_ref or not isinstance(raw_ref, str):
        return None
    ref = raw_ref.strip()
    if not ref or ref == "none" or ref.startswith(("/", "\\")) or "\\" in ref or ":" in ref:
        return None
    posix = PurePosixPath(ref)
    if posix.is_absolute() or ".." in posix.parts:
        return None
    parts = posix.parts
    # Repository-relative mapping paths and media-root-relative paths share one base.
    if parts[:len(MEDIA_DIR_PREFIX)] == MEDIA_DIR_PREFIX:
        parts = parts[len(MEDIA_DIR_PREFIX):]
    if not parts or posix.suffix.lower() not in ALLOWED_IMAGE_TYPES:
        return None

    root = _media_root()
    try:
        candidate = root.joinpath(*parts).resolve()
    except OSError as exc:
        raise MediaDependencyError("Media path resolution failed") from exc
    if not candidate.is_relative_to(root):
        return None
    try:
        if not candidate.is_file():
            return None
        with candidate.open("rb") as handle:
            media_type = detect_image_type(handle.read(SNIFF_BYTES))
    except OSError as exc:
        raise MediaDependencyError("Media storage check failed") from exc
    if media_type is None:
        return None
    return candidate, media_type


def resolve_cover_asset(storybook_id: int, version_no: int, source_type: str,
                        source_ref: str | None) -> tuple[Path, str] | None:
    if source_type != "LIBRARY" or source_ref != SEED_COVER_SOURCE_REF:
        return None
    cover_path = _cover_mapping().get((storybook_id, version_no))
    return _resolve_image_path(cover_path)


def resolve_page_asset(raw_image_ref: str | None) -> tuple[Path, str] | None:
    return _resolve_image_path(raw_image_ref)


def cover_ref_for(storybook_id: int, version_id: int, version_no: int, source_type: str,
                  source_ref: str | None) -> str | None:
    if resolve_cover_asset(storybook_id, version_no, source_type, source_ref) is not None:
        return f"asset-{version_id}-cover"
    return None


def page_image_ref_for(version_id: int, page_no: int, raw_image_ref: str | None) -> str | None:
    if resolve_page_asset(raw_image_ref) is not None:
        return f"asset-{version_id}-page-{page_no}"
    return None


def parse_asset_key(resource_key: str) -> tuple[int, int | None] | None:
    """Parse asset-{version_id}-cover or asset-{version_id}-page-{page_no} into (version_id, page_no)."""
    match = ASSET_KEY_RE.fullmatch(resource_key)
    if not match:
        return None
    version_id = int(match.group(1))
    page_no = int(match.group(3)) if match.group(3) is not None else None
    if version_id > MAX_ID or (page_no is not None and page_no > MAX_ID):
        return None
    return version_id, page_no
