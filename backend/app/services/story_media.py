"""Controlled image media resolution, full decode validation, and per-version pinning (DCL-005 / API-024)."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import threading
import zlib
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
MEDIA_TYPE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
PNG_BIT_DEPTHS = {
    0: frozenset({1, 2, 4, 8, 16}),
    2: frozenset({8, 16}),
    3: frozenset({1, 2, 4, 8}),
    4: frozenset({8, 16}),
    6: frozenset({8, 16}),
}
PNG_SAMPLES = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
ADAM7_PASSES = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)
JPEG_SOF_MARKERS = frozenset({
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
})
ASSET_KEY_RE = re.compile(r"^asset-([1-9][0-9]{0,15})-(cover|page-([1-9][0-9]{0,15}))$")
BLOB_NAME_RE = re.compile(r"^[0-9a-f]{64}\.(png|jpg|webp)$")
MAX_ID = 9007199254740991
SNIFF_BYTES = 12
DEFAULT_DB_PATH = REPO_ROOT / "database" / "instance" / "zhiya.sqlite3"

_PIN_LOCK = threading.RLock()
_DIGEST_LOCK = threading.Lock()
_VERIFIED_DIGESTS: dict[str, str] = {}


class MediaDependencyError(RuntimeError):
    """Raised when the configured or default media root or cover mapping cannot be read."""


def _expected_png_raw_bytes(width: int, height: int, bit_depth: int, color_type: int, interlace: int) -> int:
    bpp = PNG_SAMPLES[color_type] * bit_depth
    if interlace == 0:
        row_bytes = (width * bpp + 7) // 8
        return (row_bytes + 1) * height
    total = 0
    for x0, y0, dx, dy in ADAM7_PASSES:
        pw = (width - x0 + dx - 1) // dx if width > x0 else 0
        ph = (height - y0 + dy - 1) // dy if height > y0 else 0
        if pw > 0 and ph > 0:
            total += ((pw * bpp + 7) // 8 + 1) * ph
    return total


def _verify_png(data: bytes) -> bool:
    if len(data) < 33 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    pos = 8
    size = len(data)
    ihdr: tuple[int, int, int, int, int] | None = None
    idat = bytearray()
    seen_plte = False
    seen_iend = False
    while pos < size:
        if pos + 12 > size:
            return False
        length, chunk_type = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + length
        if end > size:
            return False
        payload = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:end])
        if (zlib.crc32(chunk_type + payload) & 0xFFFFFFFF) != crc:
            return False
        if chunk_type == b"IHDR":
            if pos != 8 or length != 13:
                return False
            width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(">IIBBBBB", payload)
            if (width == 0 or height == 0 or color_type not in PNG_BIT_DEPTHS
                    or bit_depth not in PNG_BIT_DEPTHS[color_type]
                    or comp != 0 or filt != 0 or interlace not in (0, 1)):
                return False
            ihdr = (width, height, bit_depth, color_type, interlace)
        else:
            if ihdr is None or seen_iend:
                return False
            if chunk_type == b"PLTE":
                if length % 3 != 0 or not 3 <= length <= 768:
                    return False
                seen_plte = True
            elif chunk_type == b"IDAT":
                idat.extend(payload)
            elif chunk_type == b"IEND":
                if length != 0:
                    return False
                seen_iend = True
                pos = end
                break
        pos = end
    if ihdr is None or not seen_iend or pos != size or not idat:
        return False
    width, height, bit_depth, color_type, interlace = ihdr
    if color_type == 3 and not seen_plte:
        return False
    try:
        decomp = zlib.decompressobj()
        raw = decomp.decompress(bytes(idat)) + decomp.flush()
    except zlib.error:
        return False
    if not decomp.eof or decomp.unused_data:
        return False
    return len(raw) == _expected_png_raw_bytes(width, height, bit_depth, color_type, interlace)


def _verify_jpeg(data: bytes) -> bool:
    size = len(data)
    if size < 4 or not data.startswith(b"\xff\xd8"):
        return False
    pos = 2
    seen_sof = False
    seen_sos = False
    while pos < size:
        if data[pos] != 0xFF:
            return False
        while pos < size and data[pos] == 0xFF:
            pos += 1
        if pos >= size:
            return False
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            return seen_sof and seen_sos and pos == size
        if marker == 0x00 or 0xD0 <= marker <= 0xD8:
            return False
        if marker == 0x01:
            continue
        if pos + 2 > size:
            return False
        (seg_len,) = struct.unpack(">H", data[pos:pos + 2])
        if seg_len < 2 or pos + seg_len > size:
            return False
        if marker in JPEG_SOF_MARKERS:
            if seg_len < 8:
                return False
            precision, height, width, num_comp = struct.unpack(">BHHB", data[pos + 2:pos + 8])
            if precision == 0 or height == 0 or width == 0 or num_comp == 0:
                return False
            seen_sof = True
        pos += seg_len
        if marker == 0xDA:
            seen_sos = True
            while True:
                idx = data.find(0xFF, pos)
                if idx == -1:
                    return False
                scan = idx
                while scan < size and data[scan] == 0xFF:
                    scan += 1
                if scan >= size:
                    return False
                next_marker = data[scan]
                if next_marker == 0x00 or 0xD0 <= next_marker <= 0xD7:
                    pos = scan + 1
                    continue
                pos = scan - 1
                break
    return False


def _verify_webp(data: bytes) -> bool:
    size = len(data)
    if size < 20 or not data.startswith(b"RIFF") or data[8:12] != b"WEBP":
        return False
    riff_size = int.from_bytes(data[4:8], "little")
    end_riff = 8 + riff_size
    if riff_size < 12 or size != end_riff:
        return False
    pos = 12
    seen_frame = False
    while pos < end_riff:
        if pos + 8 > end_riff:
            return False
        fourcc = data[pos:pos + 4]
        chunk_size = int.from_bytes(data[pos + 4:pos + 8], "little")
        payload_start = pos + 8
        payload_end = payload_start + chunk_size
        if payload_end > end_riff:
            return False
        payload = data[payload_start:payload_end]
        if fourcc == b"VP8 ":
            if chunk_size < 10 or payload[3:6] != b"\x9d\x01\x2a":
                return False
            width = int.from_bytes(payload[6:8], "little") & 0x3FFF
            height = int.from_bytes(payload[8:10], "little") & 0x3FFF
            if width == 0 or height == 0:
                return False
            seen_frame = True
        elif fourcc == b"VP8L":
            if chunk_size < 5 or payload[0] != 0x2F:
                return False
            bits = int.from_bytes(payload[1:5], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            if width == 0 or height == 0:
                return False
            seen_frame = True
        elif fourcc == b"ANMF":
            if chunk_size < 16:
                return False
            seen_frame = True
        padded_end = payload_end + (chunk_size & 1)
        if padded_end > end_riff:
            return False
        pos = padded_end
    return seen_frame and pos == end_riff


def verify_and_detect_image(data: bytes) -> str | None:
    """Validate that bytes represent a complete decodable PNG, JPEG, or WebP and return its real MIME type."""
    if len(data) < SNIFF_BYTES:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and _verify_png(data):
        return "image/png"
    if data.startswith(b"\xff\xd8") and _verify_jpeg(data):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and _verify_webp(data):
        return "image/webp"
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
    with _DIGEST_LOCK:
        media_type = _VERIFIED_DIGESTS.get(digest)
    if media_type is None:
        media_type = verify_and_detect_image(data)
        if media_type is None:
            return None
        with _DIGEST_LOCK:
            if len(_VERIFIED_DIGESTS) > 512:
                _VERIFIED_DIGESTS.clear()
            _VERIFIED_DIGESTS[digest] = media_type
    return candidate, digest, media_type, data


def _write_blob_once(store_root: Path, digest: str, media_type: str, data: bytes) -> str:
    ext = MEDIA_TYPE_EXTENSIONS[media_type]
    blob_name = f"{digest}{ext}"
    if not BLOB_NAME_RE.fullmatch(blob_name):
        raise ValueError("Invalid pinned blob name")
    blobs_dir = store_root / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    target = blobs_dir / blob_name
    if target.is_file():
        return blob_name
    tmp = blobs_dir / f".tmp-{blob_name}-{os.getpid()}-{threading.get_ident()}"
    tmp.write_bytes(data)
    try:
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        if not target.is_file():
            raise
    return blob_name


def _read_pinned_blob(store_root: Path, blob_name: str) -> tuple[bytes, str] | None:
    if not isinstance(blob_name, str) or not BLOB_NAME_RE.fullmatch(blob_name):
        return None
    blobs_dir = (store_root / "blobs").resolve()
    candidate = (blobs_dir / blob_name).resolve()
    if not candidate.is_relative_to(blobs_dir):
        return None
    try:
        if not candidate.is_file():
            return None
        data = candidate.read_bytes()
    except OSError as exc:
        raise MediaDependencyError("Pinned media storage check failed") from exc
    media_type = verify_and_detect_image(data)
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
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("identity") != expected_identity:
        return None
    if "cover" not in data or not isinstance(data.get("pages"), dict):
        return None
    return data


def _save_version_manifest(store_root: Path, version_id: int, manifest: dict) -> None:
    versions_dir = store_root / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    meta_path = versions_dir / f"{version_id}.json"
    tmp = versions_dir / f".tmp-{version_id}-{os.getpid()}-{threading.get_ident()}.json"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, meta_path)


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
                      db_path: Path | None = None) -> dict:
    """Resolve and immutably snapshot verified cover and page assets for one storyVersionId."""
    root, entries = ensure_media_dependencies()
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
    store_root = _pin_store_root(db_path)

    with _PIN_LOCK:
        manifest = _load_version_manifest(store_root, version_id, identity)
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


def pin_database_versions(db: sqlite3.Connection, *, db_path: Path) -> None:
    """Pin all story_versions in a freshly populated database during seed import."""
    cur = db.cursor()
    cur.row_factory = sqlite3.Row
    versions = cur.execute(
        """SELECT v.*, b.source_type AS storybook_source_type FROM story_versions v
           JOIN storybooks b ON b.id = v.storybook_id ORDER BY v.id"""
    ).fetchall()
    for row in versions:
        pin_version_media(db, row, source_type=row["storybook_source_type"], db_path=db_path)


def _pinned_blob_exists(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    blob_name = entry.get("blob")
    return isinstance(blob_name, str) and bool(BLOB_NAME_RE.fullmatch(blob_name)) and (_pin_store_root() / "blobs" / blob_name).is_file()


def cover_ref_from_manifest(version_id: int, manifest: dict) -> str | None:
    return f"asset-{version_id}-cover" if _pinned_blob_exists(manifest.get("cover")) else None


def page_image_ref_from_manifest(version_id: int, page_no: int, manifest: dict) -> str | None:
    entry = manifest.get("pages", {}).get(str(page_no))
    return f"asset-{version_id}-page-{page_no}" if _pinned_blob_exists(entry) else None


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
    store_root = _pin_store_root()
    if page_no is None:
        entry = manifest.get("cover")
    else:
        entry = manifest.get("pages", {}).get(str(page_no))
    if not isinstance(entry, dict):
        return None
    return _read_pinned_blob(store_root, entry.get("blob", ""))


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
