from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import RuntimeSettings

SECRET_FIELDS = {
    "qbit_password",
    "rss_url",
    "autobrr_api_key",
    "autobrr_shared_secret",
    "radarr_api_key",
    "sonarr_api_key",
    "prowlarr_api_key",
    "plex_token",
    "dashboard_password",
    "agent_api_token",
    "tmdb_api_key",
}
MASK = "********"


@dataclass(slots=True)
class SettingsUpdateResult:
    settings: Settings
    changed_keys: list[str]


class SettingsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def bootstrap(self) -> Settings:
        return get_settings()

    def get_record(self) -> RuntimeSettings:
        record = self.db.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one_or_none()
        if record is None:
            record = RuntimeSettings(id=1, payload={})
            self.db.add(record)
            self.db.flush()
        return record

    def resolve(self) -> Settings:
        record = self.get_record()
        merged = self.bootstrap().model_dump()
        merged.update(record.payload or {})
        return Settings.model_validate(merged)

    def masked(self) -> dict[str, Any]:
        settings = self.resolve().model_dump()
        for field in SECRET_FIELDS:
            if settings.get(field):
                settings[field] = MASK
        settings["host_disk_check_path_suggested"] = self.suggest_host_path(
            settings.get("qbit_save_path")
        )
        return settings

    def update(self, payload: dict[str, Any]) -> SettingsUpdateResult:
        record = self.get_record()
        current_payload = dict(record.payload or {})
        clean_payload = {
            key: value
            for key, value in payload.items()
            if not (key in SECRET_FIELDS and (value in {None, "", MASK}))
        }
        merged = self.bootstrap().model_dump()
        merged.update(current_payload)
        merged.update(clean_payload)
        settings = Settings.model_validate(merged)
        next_payload: dict[str, Any] = {}
        bootstrap = self.bootstrap().model_dump()
        for key, value in settings.model_dump().items():
            if bootstrap.get(key) != value:
                next_payload[key] = value
        record.payload = next_payload
        self.db.add(record)
        self.db.flush()
        changed = sorted(set(clean_payload).union(set(current_payload) ^ set(next_payload)))
        return SettingsUpdateResult(settings=settings, changed_keys=changed)

    def validate_host_path(self, settings: Settings | None = None) -> tuple[bool, str]:
        resolved = settings or self.resolve()
        path = Path(resolved.host_disk_check_path)
        if not path.exists():
            suggestion = self.suggest_host_path(resolved.qbit_save_path)
            if suggestion and suggestion != resolved.host_disk_check_path:
                return False, f"Path does not exist. Suggested WSL path: {suggestion}"
            return False, "Path does not exist"
        if not path.is_dir():
            return False, "Path exists but is not a directory"
        return True, "Path is valid"

    def suggest_host_path(self, windows_path: str | None) -> str | None:
        if not windows_path:
            return None
        match = re.match(r"^(?P<drive>[A-Za-z]):\\(?P<tail>.*)$", windows_path)
        if not match:
            return None
        drive = match.group("drive").lower()
        tail = match.group("tail").replace("\\", "/")
        return f"/mnt/{drive}/{tail}"


def get_runtime_settings(db: Session) -> Settings:
    return SettingsService(db).resolve()


def settings_validation_error(error: ValidationError) -> dict[str, Any]:
    return {"status": "error", "errors": error.errors()}
