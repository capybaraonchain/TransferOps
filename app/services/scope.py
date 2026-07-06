from __future__ import annotations

from pathlib import PureWindowsPath

from app.config import Settings


def normalize_windows_path(value: str | None) -> str:
    if not value:
        return ""
    text = str(PureWindowsPath(value))
    return text.rstrip("\\/").lower()


def tags_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {part.strip().lower() for part in value.split(",") if part.strip()}


def is_managed_qb_torrent(payload: dict, settings: Settings) -> bool:
    category = str(payload.get("category") or "").strip().lower()
    tags = tags_set(payload.get("tags"))
    save_path = normalize_windows_path(payload.get("save_path"))
    expected_root = normalize_windows_path(settings.qbit_save_path)
    expected_category = settings.qbit_category.strip().lower()
    expected_tag = settings.qbit_tag.strip().lower()

    category_match = bool(expected_category and category == expected_category)
    tag_match = bool(expected_tag and expected_tag in tags)
    path_match = bool(
        expected_root and (save_path == expected_root or save_path.startswith(f"{expected_root}\\"))
    )
    if expected_category or expected_tag:
        return category_match or tag_match
    return path_match
