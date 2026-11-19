"""Controlled image media resolution, full decode validation, and per-version pinning (DCL-005 / API-024)."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
from io import BytesIO
from pathlib import Path, PurePosixPath

from PIL import Image

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
MEDIA_TYPE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP")
ASSET_KEY_RE = re.compile(r"^asset-([1-9][0-9]{0,15})-(cover|page-([1-9][0-9]{0,15}))$")
BLOB_NAME_RE = re.compile(r"^[0-9a-f]{64}\.(png|jpg|webp)$")
MAX_ID = 9007199254740991
DEFAULT_DB_PATH = REPO_ROOT / "database" / "instance" / "zhiya.sqlite3"

_PIN_LOCK = threading.RLock()
_DIGEST_LOCK = threading.Lock()
_VERIFIED_DIGESTS: dict[str, str] = {}


class MediaDependencyError(RuntimeError):
    """The media source or the version snapshot store is temporarily unavailable."""


def verify_and_detect_image(data: bytes) -> str | None:
    """Return the actual MIME type only after verifying the container and decoding every frame."""
    try:
        with Image.open(BytesIO(data), formats=IMAGE_FORMATS) as image:
            fmt = image.format
            if fmt == "PNG" and not data.endswith(b"\0\0\0\0IEND\xae\x42\x60\x82"):
                return None
            if fmt == "JPEG" and not data.endswith(b"\xff\xd9"):
                return None
            if fmt == "WEBP" and int.from_bytes(data[4:8], "little") + 8 != len(data):
                return None
            image.verify()

        # Pillow requires reopening the image after verify() to decode its pixels.
        with Image.open(BytesIO(data), formats=(fmt,)) as image:
            for frame in range(getattr(image, "n_frames", 1)):
                image.seek(frame)
                image.load()
        return Image.MIME[fmt]
    except (OSError, SyntaxError, ValueError, EOFError, Image.DecompressionBombError):
        return None


def _media_root() -> Path:
    """Directory that every media reference is resolved against and confined to."""
    configured = os.environ.get("ZHIYA_MEDIA_ROOT", "").strip()
    target = Path(configured) if configured else DEFAULT_MEDIA_ROOT
    try:
        resolved = target.resolve()
        if not resolved.is_dir():
            raise MediaDependencyError("Media root is unavailable")
    except OSError as exc:
        raise MediaDependencyError("Media root is unavailable") from exc
    return resolved


def _cover_mapping_entries() -> list[dict]:
    configured = os.environ.get("ZHIYA_COVER_MAPPING_PATH", "").strip()
    mapping_path = Path(configured) if configured else DEFAULT_COVER_MAPPING
    try:
        entries = json.loads(mapping_path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise MediaDependencyError("Invalid cover mapping format")
        for item in entries:
            if not isinstance(item, dict) or "storybook_id" not in item or "cover_path" not in item:
                raise MediaDependencyError("Invalid cover mapping entry")
            int(item["storybook_id"])
            if "version_no" in item and item["version_no"] is not None:
                int(item["version_no"])
            if "story_version_id" in item and item["story_version_id"] is not None:
                int(item["story_version_id"])
            if "storyVersionId" in item and item["storyVersionId"] is not None:
                int(item["storyVersionId"])
        return entries
    except MediaDependencyError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise MediaDependencyError("Cover mapping is unavailable") from exc


def ensure_media_dependencies() -> tuple[Path, list[dict]]:
    """Fail closed with MediaDependencyError whenever the active media root or cover mapping is missing."""
    return _media_root(), _cover_mapping_entries()


def _select_cover_path(entries: list[dict], storybook_id: int, version_id: int, version_no: int) -> str | None:
    by_version_id: str | None = None
    by_version_no: str | None = None
    fallback_by_book: str | None = None
    for item in entries:
        raw_vid = item.get("story_version_id")
        if raw_vid is None:
            raw_vid = item.get("storyVersionId")
        if raw_vid is not None:
            if int(raw_vid) == version_id:
                by_version_id = str(item["cover_path"])
            continue
        if int(item["storybook_id"]) != storybook_id:
            continue
        if "version_no" in item and item["version_no"] is not None:
            if int(item["version_no"]) == version_no:
                by_version_no = str(item["cover_path"])
        else:
            fallback_by_book = str(item["cover_path"])
    return by_version_id if by_version_id is not None else (
        by_version_no if by_version_no is not None else fallback_by_book
    )


def _resolved_db_path(db_path: Path | None = None) -> Path:
    if db_path is not None:
        return db_path.resolve()
    configured = os.environ.get("ZHIYA_DB_PATH", "").strip()
    return (Path(configured) if configured else DEFAULT_DB_PATH).resolve()


def _pin_store_root(db_path: Path | None = None) -> Path:
    resolved = _resolved_db_path(db_path)
    return resolved.parent / f".{resolved.name}.media_pins"


def clear_pin_store(db_path: Path | None = None) -> None:
    root = _pin_store_root(db_path)
    with _PIN_LOCK:
        shutil.rmtree(root, ignore_errors=True)


def _resolve_verified_source(raw_ref: str | None, root: Path) -> tuple[Path, str, str, bytes] | None:
    if not raw_ref or not isinstance(raw_ref, str):
        return None
    ref = raw_ref.strip()
    if not ref or ref == "none" or ref.startswith(("/", "\\")) or "\\" in ref or ":" in ref:
        return None
    posix = PurePosixPath(ref)
    if posix.is_absolute() or ".." in posix.parts:
        return None
    parts = posix.parts
    if parts[:len(MEDIA_DIR_PREFIX)] == MEDIA_DIR_PREFIX:
        parts = parts[len(MEDIA_DIR_PREFIX):]
    if not parts or posix.suffix.lower() not in ALLOWED_IMAGE_TYPES:
        return None
    try:
        candidate = root.joinpath(*parts).resolve()
    except OSError as exc:
        raise MediaDependencyError("Media path resolution failed") from exc
    if not candidate.is_relative_to(root):
        return None
    try:
        if not candidate.is_file():
            return None
        data = candidate.read_bytes()
    except OSError as exc:
        raise MediaDependencyError("Media storage check failed") from exc

    digest = hashlib.sha256(data).hexdigest()
    media_type = _verified_media_type(data, digest)
    if media_type is None:
        return None
    return candidate, digest, media_type, data


def _verified_media_type(data: bytes, digest: str) -> str | None:
    with _DIGEST_LOCK:
        media_type = _VERIFIED_DIGESTS.get(digest)
    if media_type is None:
        media_type = verify_and_detect_image(data)
        if media_type is not None:
            with _DIGEST_LOCK:
                if len(_VERIFIED_DIGESTS) > 512:
                    _VERIFIED_DIGESTS.clear()
                _VERIFIED_DIGESTS[digest] = media_type
    return media_type


def _write_snapshot_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp-{path.name}-{os.getpid()}-{threading.get_ident()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_blob_once(store_root: Path, digest: str, media_type: str, data: bytes) -> str:
    blob_name = digest + MEDIA_TYPE_EXTENSIONS[media_type]
    target = store_root / "blobs" / blob_name
    if not target.is_file():
        _write_snapshot_file(target, data)
    return blob_name


def _read_pinned_blob(blob_name: str) -> tuple[bytes, str] | None:
    if not isinstance(blob_name, str) or not BLOB_NAME_RE.fullmatch(blob_name):
        return None
    try:
        blobs_dir = (_pin_store_root() / "blobs").resolve()
        candidate = (blobs_dir / blob_name).resolve()
        if not candidate.is_relative_to(blobs_dir):
            return None
        data = candidate.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        return None
    except OSError as exc:
        raise MediaDependencyError("Pinned media storage is unavailable") from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest != blob_name.partition(".")[0]:
        return None
    media_type = _verified_media_type(data, digest)
    if media_type is None:
        return None
    return data, media_type


def _identity_for_row(version_row: sqlite3.Row | dict, source_type: str) -> dict:
    return {
        "storybookId": int(version_row["storybook_id"]),
        "versionNo": int(version_row["version_no"]),
        "sourceType": source_type,
        "sourceRef": version_row["source_ref"],
    }


def _load_version_manifest(store_root: Path, version_id: int, expected_identity: dict) -> dict | None:
    versions_dir = store_root / "versions"
    meta_path = versions_dir / f"{version_id}.json"
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise MediaDependencyError("Invalid version media snapshot") from exc
    if (not isinstance(data, dict) or "identity" not in data or "cover" not in data
            or not isinstance(data.get("pages"), dict)):
        raise MediaDependencyError("Invalid version media snapshot")
    if data["identity"] != expected_identity:
        return None
    return data


def _save_version_manifest(store_root: Path, version_id: int, manifest: dict) -> None:
    path = store_root / "versions" / f"{version_id}.json"
    _write_snapshot_file(path, json.dumps(manifest, ensure_ascii=False).encode("utf-8"))


def _pin_cover_into_manifest(store_root: Path, manifest: dict, root: Path, entries: list[dict],
                             storybook_id: int, version_id: int, version_no: int,
                             source_type: str, source_ref: str | None) -> bool:
    if source_type != "LIBRARY" or source_ref != SEED_COVER_SOURCE_REF:
        if manifest.get("cover") is not None:
            manifest["cover"] = None
            return True
        return False
    cover_path = _select_cover_path(entries, storybook_id, version_id, version_no)
    resolved = _resolve_verified_source(cover_path, root)
    if resolved is None:
        return False
    _, digest, media_type, data = resolved
    blob_name = _write_blob_once(store_root, digest, media_type, data)
    manifest["cover"] = {"blob": blob_name, "mediaType": media_type}
    return True


def _pin_page_into_manifest(store_root: Path, manifest: dict, root: Path, page_no: int, raw_ref: str | None) -> bool:
    key = str(page_no)
    existing = manifest["pages"].get(key)
    if isinstance(existing, dict) and existing.get("rawRef") == raw_ref:
        return False
    resolved = _resolve_verified_source(raw_ref, root)
    if resolved is None:
        manifest["pages"].pop(key, None)
        return existing is not None
    _, digest, media_type, data = resolved
    blob_name = _write_blob_once(store_root, digest, media_type, data)
    manifest["pages"][key] = {"rawRef": raw_ref, "blob": blob_name, "mediaType": media_type}
    return True


def pin_version_media(db: sqlite3.Connection, version_row: sqlite3.Row | dict, *,
                      source_type: str | None = None, pages: list[sqlite3.Row] | None = None,
                      db_path: Path | None = None, only_if_missing: bool = False) -> dict:
    """Resolve and immutably snapshot verified cover and page assets for one storyVersionId."""
    version_id = int(version_row["id"])
    storybook_id = int(version_row["storybook_id"])
    version_no = int(version_row["version_no"])
    cur = db.cursor()
    cur.row_factory = sqlite3.Row
    src_type = source_type
    if src_type is None:
        if hasattr(version_row, "keys") and "storybook_source_type" in version_row.keys():
            src_type = version_row["storybook_source_type"]
        else:
            src_type = cur.execute("SELECT source_type FROM storybooks WHERE id = ?", (storybook_id,)).fetchone()[0]
    if pages is None:
        pages = cur.execute("SELECT page_no, image_ref FROM story_pages WHERE version_id = ? ORDER BY page_no",
                            (version_id,)).fetchall()
    identity = _identity_for_row(version_row, src_type)
    try:
        store_root = _pin_store_root(db_path)
        with _PIN_LOCK:
            manifest = _load_version_manifest(store_root, version_id, identity)
            if only_if_missing and manifest is not None:
                return manifest
            root, entries = ensure_media_dependencies()
            changed = False
            if manifest is None:
                manifest = {"identity": identity, "cover": None, "pages": {}}
                _pin_cover_into_manifest(store_root, manifest, root, entries, storybook_id, version_id,
                                         version_no, src_type, version_row["source_ref"])
                changed = True
            for p in pages:
                if _pin_page_into_manifest(store_root, manifest, root, int(p["page_no"]), p["image_ref"]):
                    changed = True
            if changed:
                _save_version_manifest(store_root, version_id, manifest)
            return manifest
    except OSError as exc:
        raise MediaDependencyError("Version media snapshot storage is unavailable") from exc


def pin_database_versions(db: sqlite3.Connection, *, db_path: Path) -> None:
    """Pin missing version media during initial seed import and when upgrading an existing database."""
    cur = db.cursor()
    cur.row_factory = sqlite3.Row
    versions = cur.execute(
        """SELECT v.*, b.source_type AS storybook_source_type FROM story_versions v
           JOIN storybooks b ON b.id = v.storybook_id ORDER BY v.id"""
    ).fetchall()
    for row in versions:
        pin_version_media(db, row, source_type=row["storybook_source_type"], db_path=db_path, only_if_missing=True)


def _pinned_blob_is_readable(entry: object) -> bool:
    return isinstance(entry, dict) and _read_pinned_blob(entry.get("blob")) is not None


def cover_ref_from_manifest(version_id: int, manifest: dict) -> str | None:
    return f"asset-{version_id}-cover" if _pinned_blob_is_readable(manifest.get("cover")) else None


def page_image_ref_from_manifest(version_id: int, page_no: int, manifest: dict) -> str | None:
    entry = manifest.get("pages", {}).get(str(page_no))
    return f"asset-{version_id}-page-{page_no}" if _pinned_blob_is_readable(entry) else None


def page_image_ref_for(db: sqlite3.Connection, version_id: int, page_no: int) -> str | None:
    """Ensure the parent story_version is pinned and return a controlled key if page_no has a valid image."""
    row = db.execute(
        """SELECT v.*, b.source_type AS storybook_source_type FROM story_versions v
           JOIN storybooks b ON b.id = v.storybook_id WHERE v.id = ?""",
        (version_id,),
    ).fetchone()
    if row is None:
        ensure_media_dependencies()
        return None
    manifest = pin_version_media(db, row, source_type=row["storybook_source_type"])
    return page_image_ref_from_manifest(version_id, page_no, manifest)


def read_pinned_asset_bytes(db: sqlite3.Connection, version_row: sqlite3.Row | dict,
                            page_no: int | None) -> tuple[bytes, str] | None:
    """Validate media dependencies and serve verified immutable bytes pinned for this storyVersionId."""
    manifest = pin_version_media(db, version_row)
    if page_no is None:
        entry = manifest.get("cover")
    else:
        entry = manifest.get("pages", {}).get(str(page_no))
    if not isinstance(entry, dict):
        return None
    return _read_pinned_blob(entry.get("blob", ""))


def resolve_cover_asset(storybook_id: int, version_no: int, source_type: str,
                        source_ref: str | None) -> tuple[Path, str] | None:
    root, entries = ensure_media_dependencies()
    if source_type != "LIBRARY" or source_ref != SEED_COVER_SOURCE_REF:
        return None
    cover_path = _select_cover_path(entries, storybook_id, 0, version_no)
    resolved = _resolve_verified_source(cover_path, root)
    if resolved is None:
        return None
    return resolved[0], resolved[2]


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
