import hashlib

from content_platform.normalize.urls import normalize_url


def build_material_record(raw: dict, collected_at: str) -> dict:
    content_text = raw.get("content_text", "").strip()
    content_hash = "sha256:" + hashlib.sha256(content_text.encode("utf-8")).hexdigest()

    return {
        "material_id": raw.get("material_id", ""),
        "canonical_url": raw["url"],
        "normalized_url": normalize_url(raw["url"]),
        "source_type": raw["source_type"],
        "source_name": raw["source_name"],
        "collected_at": collected_at,
        "title": raw.get("title", ""),
        "summary": raw.get("summary", ""),
        "content_text": content_text,
        "content_hash": content_hash,
        "tags": {},
        "relevance": {"status": "unknown", "reason": "", "model": ""},
        "dedup": {
            "cluster_id": None,
            "is_primary": True,
            "duplicate_of": None,
            "duplicate_reason": None,
        },
        "quality": {"content_chars": len(content_text)},
        "usage": {"used_by": [], "last_used_at": None},
    }
