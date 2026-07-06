from __future__ import annotations

import ntpath
from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree

import requests
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    ControllerEvent,
    LibraryHandoff,
    LibraryHandoffStatus,
    ManualRequest,
    SeriesEpisodeProgress,
    Torrent,
)
from app.services.integrations import IntegrationResult, normalize_title
from app.services.metadata import MetadataResolver


class PlexService:
    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.base_url = settings.plex_base_url.rstrip("/")
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, application/xml;q=0.9,*/*;q=0.8"}
        if self.settings.plex_token:
            headers["X-Plex-Token"] = self.settings.plex_token
        return headers

    def identity(self) -> IntegrationResult:
        try:
            response = self.session.get(
                f"{self.base_url}/identity",
                headers=self._headers(),
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            return IntegrationResult(ok=False, message=str(exc), payload={})
        payload = {"reachable": True, "authenticated": bool(self.settings.plex_token)}
        if not self.settings.plex_token:
            return IntegrationResult(
                ok=True,
                message="Plex reachable; configure plex_token for library control",
                payload=payload,
            )
        return IntegrationResult(ok=True, message="Plex reachable", payload=payload)

    def library_sections(self) -> IntegrationResult:
        if not self.settings.plex_token:
            return IntegrationResult(
                ok=False,
                message="plex_token is not configured",
                payload={},
            )
        try:
            response = self.session.get(
                f"{self.base_url}/library/sections",
                headers=self._headers(),
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            return IntegrationResult(ok=False, message=str(exc), payload={})
        payload = {"content_type": response.headers.get("content-type", "")}
        return IntegrationResult(
            ok=True,
            message="Plex library sections retrieved",
            payload=payload,
        )

    def refresh_section(self, section_id: int, path: str | None = None) -> IntegrationResult:
        if not self.settings.plex_token:
            return IntegrationResult(ok=False, message="plex_token is not configured", payload={})
        params: dict[str, Any] = {}
        if path:
            params["path"] = path
        try:
            response = self.session.get(
                f"{self.base_url}/library/sections/{section_id}/refresh",
                headers=self._headers(),
                params=params,
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            return IntegrationResult(ok=False, message=str(exc), payload={"path": path})
        return IntegrationResult(
            ok=True,
            message="Plex library refresh requested",
            payload={"section_id": section_id, "path": path},
        )

    def find_in_section(
        self,
        section_id: int,
        title: str,
        *,
        year: int | None = None,
        season: int | None = None,
        episode: int | None = None,
    ) -> IntegrationResult:
        if not self.settings.plex_token:
            return IntegrationResult(ok=False, message="plex_token is not configured", payload={})
        try:
            response = self.session.get(
                f"{self.base_url}/library/sections/{section_id}/all",
                headers=self._headers(),
                params={"title": title, "X-Plex-Container-Size": 10},
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            return IntegrationResult(ok=False, message=str(exc), payload={})

        try:
            items = self._library_items(response)
        except ValueError as exc:
            return IntegrationResult(ok=False, message=str(exc), payload={})

        wanted = normalize_title(title)
        for item in items:
            candidates = [
                item.get("title"),
                item.get("grandparentTitle"),
                item.get("parentTitle"),
                item.get("originalTitle"),
            ]
            if not any(normalize_title(value or "") == wanted for value in candidates):
                continue
            if year is not None and item.get("year") is not None:
                try:
                    if int(item["year"]) != year:
                        continue
                except ValueError:
                    continue
            if season is not None and episode is not None:
                try:
                    if int(item.get("parentIndex") or 0) != season:
                        continue
                    if int(item.get("index") or 0) != episode:
                        continue
                except ValueError:
                    continue
            return IntegrationResult(
                ok=True,
                message="Plex item found",
                payload={
                    "rating_key": item.get("ratingKey"),
                    "title": item.get("title"),
                },
            )
        return IntegrationResult(ok=True, message="Plex item not found yet", payload={})

    def _library_items(self, response: requests.Response) -> list[dict[str, Any]]:
        content_type = (response.headers.get("content-type") or "").lower()
        body = response.text.lstrip()

        if "json" in content_type or body.startswith("{"):
            data = response.json()
            container = data.get("MediaContainer") if isinstance(data, dict) else None
            metadata = container.get("Metadata") if isinstance(container, dict) else []
            if isinstance(metadata, list):
                return [item for item in metadata if isinstance(item, dict)]
            raise ValueError("unexpected Plex JSON library payload")

        try:
            root = ElementTree.fromstring(response.text)
        except ElementTree.ParseError as exc:
            raise ValueError(str(exc)) from exc

        items: list[dict[str, Any]] = []
        for node in root.findall(".//Video") + root.findall(".//Directory"):
            items.append(dict(node.attrib))
        return items


class LibraryHandoffService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        session: requests.Session | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.plex = PlexService(settings, session=session)
        self.metadata = MetadataResolver(settings, session=session)

    def observe_completed_torrent(self, torrent: Torrent, now: datetime | None = None) -> None:
        when = now or datetime.now(UTC).replace(tzinfo=None)
        if torrent.progress < 1.0:
            return
        request = self._linked_request(torrent)
        if request is None:
            return
        if (request.raw_payload or {}).get("add_to_plex") is False:
            return
        existing = (
            self.db.query(LibraryHandoff)
            .filter(LibraryHandoff.torrent_id == torrent.id)
            .order_by(desc(LibraryHandoff.id))
            .first()
        )
        if existing is not None:
            return

        media_type = self._handoff_media_type(request)
        source_path = self._source_path(torrent)
        status = self._initial_status()
        section_id = self._section_id_for_media_type(media_type)
        handoff = LibraryHandoff(
            torrent_id=torrent.id,
            manual_request_id=request.id,
            media_type=media_type,
            title=request.title,
            source_path=source_path,
            section_id=section_id,
            status=status,
            priority_score=self._priority_score(request),
            payload={
                "request_media_type": request.media_type,
                "request_year": request.year,
                "request_season": request.season,
                "request_episode": request.episode,
                "selected_title": (request.chosen_payload or {}).get("title"),
                "target_section_id": section_id,
            },
        )
        self.db.add(handoff)
        self.db.add(
            ControllerEvent(
                event_type="library_handoff_queued",
                severity="info",
                message=f"Queued library handoff for {request.title}",
                payload={
                    "torrent_id": torrent.id,
                    "manual_request_id": request.id,
                    "media_type": media_type,
                    "source_path": source_path,
                },
            )
        )
        if media_type == "series":
            self._record_tv_progress(request, torrent, when)

    def process_pending(self) -> IntegrationResult:
        queued = (
            self.db.query(LibraryHandoff)
            .filter(
                LibraryHandoff.status.in_(
                    [
                        LibraryHandoffStatus.pending.value,
                        LibraryHandoffStatus.waiting_config.value,
                        LibraryHandoffStatus.scan_requested.value,
                    ]
                )
            )
            .order_by(LibraryHandoff.priority_score.desc(), LibraryHandoff.created_at.asc())
            .all()
        )
        processed = 0
        waiting = 0
        failed = 0
        now = datetime.now(UTC).replace(tzinfo=None)
        for handoff in queued:
            if not self.settings.plex_enabled or not self.settings.plex_handoff_enabled:
                handoff.status = LibraryHandoffStatus.waiting_config.value
                handoff.last_error = "plex handoff is disabled"
                self.db.add(handoff)
                waiting += 1
                continue
            if not self.settings.plex_token:
                handoff.status = LibraryHandoffStatus.waiting_config.value
                handoff.last_error = "plex_token is not configured"
                self.db.add(handoff)
                waiting += 1
                continue
            section_id = handoff.section_id or self._section_id_for_media_type(handoff.media_type)
            if section_id is None:
                handoff.status = LibraryHandoffStatus.waiting_config.value
                handoff.last_error = f"missing Plex section id for {handoff.media_type}"
                self.db.add(handoff)
                waiting += 1
                continue
            if handoff.status == LibraryHandoffStatus.scan_requested.value:
                payload = handoff.payload or {}
                lookup = self.plex.find_in_section(
                    section_id,
                    handoff.title,
                    year=payload.get("request_year"),
                    season=payload.get("request_season"),
                    episode=payload.get("request_episode"),
                )
                if not lookup.ok:
                    handoff.last_error = lookup.message
                    failed += 1
                    self.db.add(handoff)
                    continue
                if lookup.payload:
                    handoff.imported_at = now
                    handoff.status = LibraryHandoffStatus.completed.value
                    handoff.last_error = None
                    handoff.payload = {**(handoff.payload or {}), "plex_item": lookup.payload}
                    self._mark_progress_imported(handoff, now)
                    processed += 1
                else:
                    handoff.last_error = "awaiting_plex_scan"
                    waiting += 1
                self.db.add(handoff)
                continue
            result = self.plex.refresh_section(section_id, handoff.source_path)
            if result.ok:
                handoff.section_id = section_id
                handoff.status = LibraryHandoffStatus.scan_requested.value
                handoff.scan_requested_at = now
                handoff.last_error = None
                self.db.add(
                    ControllerEvent(
                        event_type="plex_refresh_requested",
                        severity="info",
                        message=f"Plex refresh requested for {handoff.title}",
                        payload={
                            "handoff_id": handoff.id,
                            "section_id": section_id,
                            "source_path": handoff.source_path,
                        },
                    )
                )
                waiting += 1
            else:
                handoff.status = LibraryHandoffStatus.failed.value
                handoff.last_error = result.message
                failed += 1
            self.db.add(handoff)

        return IntegrationResult(
            ok=failed == 0,
            message="library handoff processing complete",
            payload={"processed": processed, "waiting": waiting, "failed": failed},
        )

    def retry_handoff(self, handoff_id: int) -> tuple[LibraryHandoff | None, IntegrationResult]:
        handoff = (
            self.db.query(LibraryHandoff)
            .filter(LibraryHandoff.id == handoff_id)
            .one_or_none()
        )
        if handoff is None:
            return None, IntegrationResult(
                ok=False,
                message="library handoff not found",
                payload={},
            )
        if handoff.status == LibraryHandoffStatus.completed.value:
            return (
                handoff,
                IntegrationResult(
                    ok=False,
                    message="completed library handoff cannot be retried",
                    payload={"handoff_id": handoff.id, "status": handoff.status},
                ),
            )
        if handoff.status != LibraryHandoffStatus.failed.value:
            return (
                handoff,
                IntegrationResult(
                    ok=False,
                    message="only failed library handoffs can be retried",
                    payload={"handoff_id": handoff.id, "status": handoff.status},
                ),
            )

        handoff.status = LibraryHandoffStatus.pending.value
        handoff.last_error = None
        handoff.imported_at = None
        self.db.add(handoff)
        self.db.add(
            ControllerEvent(
                event_type="library_handoff_retry_requested",
                severity="info",
                message=f"Retry requested for library handoff {handoff.id}",
                payload={
                    "handoff_id": handoff.id,
                    "manual_request_id": handoff.manual_request_id,
                    "torrent_id": handoff.torrent_id,
                },
            )
        )
        self.db.flush()
        result = self.process_pending()
        return handoff, result

    def tv_priority_frontier(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query(SeriesEpisodeProgress).all()
        series_map: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = series_map.setdefault(
                row.normalized_series_title,
                {
                    "series_title": row.series_title,
                    "episodes": set(),
                    "seasons": set(),
                },
            )
            item["episodes"].add((row.season, row.episode))
            item["seasons"].add(row.season)

        priorities: list[dict[str, Any]] = []
        for normalized, item in series_map.items():
            next_target = self._next_episode_frontier(item["series_title"], item["episodes"])
            if next_target is None:
                continue
            season, episode = next_target
            score = self._frontier_priority_score(season, episode)
            priorities.append(
                {
                    "series_title": item["series_title"],
                    "normalized_series_title": normalized,
                    "season": season,
                    "episode": episode,
                    "priority_score": score,
                    "reason": self._frontier_reason(season, episode),
                }
            )
        priorities.sort(key=lambda row: row["priority_score"], reverse=True)
        return priorities[:limit]

    def _linked_request(self, torrent: Torrent) -> ManualRequest | None:
        request = None
        if torrent.id is not None:
            request = (
                self.db.query(ManualRequest)
                .filter(ManualRequest.torrent_id == torrent.id)
                .order_by(desc(ManualRequest.id))
                .first()
            )
        if request is None and torrent.candidate_id is not None:
            request = (
                self.db.query(ManualRequest)
                .filter(ManualRequest.candidate_id == torrent.candidate_id)
                .order_by(desc(ManualRequest.id))
                .first()
            )
        return request

    def _handoff_media_type(self, request: ManualRequest) -> str:
        if request.media_type == "movie":
            return "movie"
        return "series"

    def _initial_status(self) -> str:
        if not self.settings.plex_enabled or not self.settings.plex_handoff_enabled:
            return LibraryHandoffStatus.waiting_config.value
        if not self.settings.plex_token:
            return LibraryHandoffStatus.waiting_config.value
        return LibraryHandoffStatus.pending.value

    def _section_id_for_media_type(self, media_type: str) -> int | None:
        if media_type == "movie":
            return self.settings.plex_movies_section_id
        return self.settings.plex_series_section_id

    def _source_path(self, torrent: Torrent) -> str | None:
        path = torrent.content_path or torrent.save_path
        if not path:
            return None
        lowered = path.lower()
        if lowered.endswith((".mkv", ".mp4", ".avi", ".ts", ".m4v")):
            return ntpath.dirname(path) or path
        return path

    def _priority_score(self, request: ManualRequest) -> float:
        if request.media_type == "movie":
            return 50.0
        season = request.season or 1
        episode = request.episode or 1
        return self._frontier_priority_score(season, episode)

    def _frontier_priority_score(self, season: int, episode: int) -> float:
        season_penalty = max(0, season - 1) * 12
        if episode == 1:
            base = 100.0
        elif episode == 2:
            base = 80.0
        else:
            base = max(20.0, 70.0 - ((episode - 3) * 4.0))
        return base - season_penalty

    def _frontier_reason(self, season: int, episode: int) -> str:
        if episode == 1:
            return "season opener gets maximum priority"
        if episode == 2:
            return "episode 2 gets high priority after opener"
        return "later episode priority decays after early-season frontier"

    def _record_tv_progress(
        self,
        request: ManualRequest,
        torrent: Torrent,
        when: datetime,
    ) -> None:
        for season, episode in self._episode_coverage(request, torrent):
            existing = (
                self.db.query(SeriesEpisodeProgress)
                .filter(
                    SeriesEpisodeProgress.normalized_series_title == normalize_title(request.title),
                    SeriesEpisodeProgress.season == season,
                    SeriesEpisodeProgress.episode == episode,
                )
                .one_or_none()
            )
            if existing is None:
                existing = SeriesEpisodeProgress(
                    series_title=request.title,
                    normalized_series_title=normalize_title(request.title),
                    season=season,
                    episode=episode,
                    status="downloaded",
                    torrent_id=torrent.id,
                    manual_request_id=request.id,
                    completed_at=when,
                )
            else:
                existing.status = "downloaded"
                existing.torrent_id = torrent.id
                existing.manual_request_id = request.id
                existing.completed_at = when
            self.db.add(existing)

    def _episode_coverage(self, request: ManualRequest, torrent: Torrent) -> list[tuple[int, int]]:
        title = (
            (request.chosen_payload or {}).get("title")
            or torrent.title
            or request.title
        )
        parsed = self.metadata.parse_title(title)
        if parsed and parsed.media_type == "tv_episode" and parsed.season and parsed.episode:
            return [(parsed.season, parsed.episode)]
        if parsed and parsed.media_type == "tv_season" and parsed.season:
            numbers = self.metadata.season_episode_numbers(request.title, parsed.season)
            return [(parsed.season, number) for number in numbers]
        if request.season and request.episode:
            return [(request.season, request.episode)]
        if request.season:
            numbers = self.metadata.season_episode_numbers(request.title, request.season)
            return [(request.season, number) for number in numbers]
        return []

    def _mark_progress_imported(self, handoff: LibraryHandoff, when: datetime) -> None:
        if handoff.media_type != "series" or handoff.manual_request_id is None:
            return
        rows = (
            self.db.query(SeriesEpisodeProgress)
            .filter(SeriesEpisodeProgress.manual_request_id == handoff.manual_request_id)
            .all()
        )
        for row in rows:
            row.imported_at = when
            row.status = "imported"
            self.db.add(row)

    def _next_episode_frontier(
        self,
        series_title: str,
        completed: set[tuple[int, int]],
    ) -> tuple[int, int] | None:
        if not completed:
            return None
        seasons = sorted({season for season, _ in completed})
        for season in seasons:
            known_numbers = {
                number
                for s, number in completed
                if s == season
            }
            if not known_numbers:
                continue
            season_numbers = self.metadata.season_episode_numbers(series_title, season)
            if season_numbers:
                for episode in season_numbers:
                    if episode not in known_numbers:
                        return season, episode
                continue
            max_number = max(known_numbers)
            for episode in range(1, max_number + 2):
                if episode not in known_numbers:
                    return season, episode
        next_season = max(seasons) + 1
        return next_season, 1
