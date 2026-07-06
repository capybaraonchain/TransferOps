from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Alert, ControllerEvent, IntegrationState, WantedItem


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def extract_year(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\b(19|20)\d{2}\b", value)
    if match:
        return int(match.group(0))
    return None


def _integration_alert_type(name: str) -> str:
    return f"integration_{name}"


@dataclass(slots=True)
class IntegrationResult:
    ok: bool
    message: str
    payload: dict[str, Any]


def record_integration_result(
    db: Session,
    name: str,
    enabled: bool,
    result: IntegrationResult,
) -> IntegrationState:
    state = db.query(IntegrationState).filter(IntegrationState.name == name).one_or_none()
    if state is None:
        state = IntegrationState(name=name, consecutive_failures=0, payload={})
    now = datetime.now(UTC).replace(tzinfo=None)
    state.enabled = enabled
    state.payload = result.payload
    if result.ok:
        state.last_success_at = now
        state.consecutive_failures = 0
        state.last_error = None
        alert = (
            db.query(Alert)
            .filter(Alert.alert_type == _integration_alert_type(name), Alert.active.is_(True))
            .one_or_none()
        )
        if alert is not None:
            alert.active = False
            db.add(alert)
    else:
        state.last_failure_at = now
        state.consecutive_failures = (state.consecutive_failures or 0) + 1
        state.last_error = result.message
        if state.consecutive_failures >= 3:
            alert = (
                db.query(Alert)
                .filter(Alert.alert_type == _integration_alert_type(name), Alert.active.is_(True))
                .one_or_none()
            )
            if alert is None:
                alert = Alert(
                    alert_type=_integration_alert_type(name),
                    severity="warning",
                    message=f"{name} integration failing repeatedly",
                    payload={},
                )
            alert.message = f"{name} integration failing repeatedly: {result.message}"
            alert.payload = {
                "consecutive_failures": state.consecutive_failures,
                "last_error": result.message,
            }
            alert.active = True
            db.add(alert)
    db.add(state)
    return state


class ConnectivityService:
    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()

    def _request(self, url: str, headers: dict[str, str] | None = None) -> IntegrationResult:
        try:
            response = self.session.get(url, headers=headers or {}, timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            return IntegrationResult(ok=False, message=str(exc), payload={})
        data = (
            response.json()
            if "application/json" in response.headers.get("content-type", "")
            else {}
        )
        return IntegrationResult(ok=True, message="ok", payload=data)

    def test_qb(self) -> IntegrationResult:
        from app.services.qbittorrent import QBittorrentClient

        try:
            client = QBittorrentClient(self.settings)
            client._ensure_auth()
        except Exception as exc:  # noqa: BLE001
            return IntegrationResult(ok=False, message=str(exc), payload={})
        return IntegrationResult(
            ok=True, message="qBittorrent authentication succeeded", payload={}
        )

    def test_rss(self) -> IntegrationResult:
        if not self.settings.rss_url:
            return IntegrationResult(ok=False, message="RSS URL is not configured", payload={})
        try:
            response = self.session.get(self.settings.rss_url, timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            return IntegrationResult(ok=False, message=str(exc), payload={})
        return IntegrationResult(
            ok=True, message="RSS fetch succeeded", payload={"bytes": len(response.text)}
        )

    def test_autobrr(self) -> IntegrationResult:
        if not self.settings.autobrr_base_url:
            return IntegrationResult(
                ok=False, message="autobrr base URL is not configured", payload={}
            )
        headers = (
            {"X-API-Key": self.settings.autobrr_api_key} if self.settings.autobrr_api_key else {}
        )
        base_url = self.settings.autobrr_base_url.rstrip("/")
        # autobrr exposes liveness/readiness probes rather than a generic /api/healthz endpoint.
        for path in ("/api/healthz/liveness", "/api/healthz/readiness"):
            result = self._request(f"{base_url}{path}", headers)
            if result.ok:
                return result
        return result

    def test_radarr(self) -> IntegrationResult:
        if not self.settings.radarr_base_url:
            return IntegrationResult(
                ok=False, message="Radarr base URL is not configured", payload={}
            )
        headers = {"X-Api-Key": self.settings.radarr_api_key or ""}
        return self._request(
            f"{self.settings.radarr_base_url.rstrip('/')}/api/v3/system/status", headers
        )

    def test_sonarr(self) -> IntegrationResult:
        if not self.settings.sonarr_base_url:
            return IntegrationResult(
                ok=False, message="Sonarr base URL is not configured", payload={}
            )
        headers = {"X-Api-Key": self.settings.sonarr_api_key or ""}
        return self._request(
            f"{self.settings.sonarr_base_url.rstrip('/')}/api/v3/system/status", headers
        )

    def test_prowlarr(self) -> IntegrationResult:
        if not self.settings.prowlarr_base_url:
            return IntegrationResult(
                ok=False, message="Prowlarr base URL is not configured", payload={}
            )
        headers = {"X-Api-Key": self.settings.prowlarr_api_key or ""}
        return self._request(
            f"{self.settings.prowlarr_base_url.rstrip('/')}/api/v1/system/status", headers
        )

    def test_plex(self) -> IntegrationResult:
        from app.services.library import PlexService

        return PlexService(self.settings, session=self.session).identity()


class WantedSyncService:
    def __init__(
        self, db: Session, settings: Settings, session: requests.Session | None = None
    ) -> None:
        self.db = db
        self.settings = settings
        self.session = session or requests.Session()

    def _upsert(
        self,
        source: str,
        item_type: str,
        title: str,
        year: int | None,
        external_id: str | None,
        reason: str,
        raw_payload: dict[str, Any],
    ) -> None:
        normalized = normalize_title(title)
        row = (
            self.db.query(WantedItem)
            .filter(
                WantedItem.source == source,
                WantedItem.normalized_title == normalized,
                WantedItem.year == year,
            )
            .one_or_none()
        )
        if row is None:
            row = WantedItem(
                source=source,
                item_type=item_type,
                title=title,
                normalized_title=normalized,
                year=year,
                external_id=external_id,
                reason=reason,
                raw_payload=raw_payload,
            )
        else:
            row.title = title
            row.external_id = external_id
            row.reason = reason
            row.raw_payload = raw_payload
        self.db.add(row)

    def _identity_key(
        self,
        source: str,
        normalized_title: str,
        year: int | None,
        external_id: str | None,
    ) -> str:
        if external_id:
            return f"{source}:id:{external_id}"
        return f"{source}:title:{normalized_title}:{year or 0}"

    def _remove_stale(self, source: str, seen_keys: set[str]) -> int:
        removed = 0
        rows = self.db.query(WantedItem).filter(WantedItem.source == source).all()
        for row in rows:
            row_key = self._identity_key(source, row.normalized_title, row.year, row.external_id)
            if row_key in seen_keys:
                continue
            self.db.delete(row)
            removed += 1
        return removed

    def delete_item(
        self,
        source: str,
        title: str | None,
        year: int | None = None,
        external_id: str | None = None,
    ) -> int:
        normalized = normalize_title(title or "")
        query = self.db.query(WantedItem).filter(WantedItem.source == source)
        if external_id:
            rows = query.filter(WantedItem.external_id == external_id).all()
        else:
            rows = query.filter(
                WantedItem.normalized_title == normalized,
                WantedItem.year == year,
            ).all()
        for row in rows:
            self.db.delete(row)
        return len(rows)

    def sync_radarr(self) -> IntegrationResult:
        if not self.settings.radarr_enabled or not self.settings.radarr_base_url:
            return IntegrationResult(ok=False, message="Radarr integration disabled", payload={})
        headers = {"X-Api-Key": self.settings.radarr_api_key or ""}
        response = self.session.get(
            f"{self.settings.radarr_base_url.rstrip('/')}/api/v3/movie", headers=headers, timeout=15
        )
        response.raise_for_status()
        items = response.json()
        seen_keys: set[str] = set()
        monitored_count = 0
        for item in items:
            monitored = bool(item.get("monitored", True))
            if not monitored:
                continue
            monitored_count += 1
            title = item.get("title") or "unknown"
            external_id = str(item.get("tmdbId") or item.get("id") or "")
            normalized = normalize_title(title)
            seen_keys.add(self._identity_key("radarr", normalized, item.get("year"), external_id))
            self._upsert(
                source="radarr",
                item_type="movie",
                title=title,
                year=item.get("year"),
                external_id=external_id,
                reason="radarr_monitored",
                raw_payload=item,
            )
        removed = self._remove_stale("radarr", seen_keys)
        self.db.add(
            ControllerEvent(
                event_type="wanted_sync",
                message="Radarr wanted sync complete",
                payload={"count": monitored_count, "removed": removed, "source": "radarr"},
            )
        )
        return IntegrationResult(
            ok=True,
            message="Radarr wanted sync complete",
            payload={"count": monitored_count, "removed": removed},
        )

    def sync_sonarr(self) -> IntegrationResult:
        if not self.settings.sonarr_enabled or not self.settings.sonarr_base_url:
            return IntegrationResult(ok=False, message="Sonarr integration disabled", payload={})
        headers = {"X-Api-Key": self.settings.sonarr_api_key or ""}
        response = self.session.get(
            f"{self.settings.sonarr_base_url.rstrip('/')}/api/v3/series",
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        items = response.json()
        seen_keys: set[str] = set()
        monitored_count = 0
        for item in items:
            monitored = bool(item.get("monitored", True))
            if not monitored:
                continue
            monitored_count += 1
            title = item.get("title") or "unknown"
            external_id = str(item.get("tvdbId") or item.get("id") or "")
            normalized = normalize_title(title)
            seen_keys.add(self._identity_key("sonarr", normalized, item.get("year"), external_id))
            self._upsert(
                source="sonarr",
                item_type="series",
                title=title,
                year=item.get("year"),
                external_id=external_id,
                reason="sonarr_monitored",
                raw_payload=item,
            )
        removed = self._remove_stale("sonarr", seen_keys)
        self.db.add(
            ControllerEvent(
                event_type="wanted_sync",
                message="Sonarr wanted sync complete",
                payload={"count": monitored_count, "removed": removed, "source": "sonarr"},
            )
        )
        return IntegrationResult(
            ok=True,
            message="Sonarr wanted sync complete",
            payload={"count": monitored_count, "removed": removed},
        )

    def refresh(self) -> dict[str, IntegrationResult]:
        results: dict[str, IntegrationResult] = {}
        if self.settings.radarr_enabled:
            try:
                results["radarr"] = self.sync_radarr()
            except requests.RequestException as exc:
                results["radarr"] = IntegrationResult(False, str(exc), {})
        if self.settings.sonarr_enabled:
            try:
                results["sonarr"] = self.sync_sonarr()
            except requests.RequestException as exc:
                results["sonarr"] = IntegrationResult(False, str(exc), {})
        return results


def match_wanted_items(db: Session, title: str) -> list[WantedItem]:
    normalized = normalize_title(title)
    candidate_year = extract_year(title)
    tokens = {token for token in normalized.split() if len(token) >= 3}
    candidates = db.query(WantedItem).all()
    matches: list[WantedItem] = []
    for item in candidates:
        if item.year and candidate_year and item.year != candidate_year:
            continue
        wanted_phrase = item.normalized_title
        wanted_tokens = [token for token in wanted_phrase.split() if len(token) >= 3]
        if wanted_phrase and f" {wanted_phrase} " in f" {normalized} ":
            matches.append(item)
            continue
        if len(wanted_tokens) < 3:
            continue
        if tokens and set(wanted_tokens).issubset(tokens):
            matches.append(item)
    return matches
