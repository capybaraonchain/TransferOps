from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from math import log10
from typing import Any

import requests
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ControllerEvent, ManualRequest, ManualRequestStatus, Torrent
from app.services.controller import ControllerService
from app.services.integrations import normalize_title
from app.services.qbittorrent import QBittorrentClient
from app.services.rss import canonicalize_download_url, looks_like_torrent_url
from app.services.schemas import (
    CandidatePayload,
    ManualCandidatePreview,
    ManualCandidateSelectionPayload,
    ManualFulfillPayload,
    ManualFulfillResponse,
    ManualRequestPayload,
    ManualRequestPlan,
    ManualRequestResponse,
)
from app.units import BYTES_PER_GB


@dataclass(slots=True)
class ArrExecutionResult:
    ok: bool
    message: str
    payload: dict[str, Any]


class ArrRequestService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        session: requests.Session | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.session = session or requests.Session()

    def _controller(self) -> ControllerService:
        try:
            return ControllerService(self.settings, qb=QBittorrentClient(self.settings))
        except TypeError:
            return ControllerService(self.settings)

    def plan_request(self, request: ManualRequest) -> ManualRequestPlan:
        execution_path = self._default_execution_path(request)
        requirements: list[str] = []
        warnings: list[str] = []
        payload: dict[str, Any] = {
            "lookup_term": self._lookup_term(request),
            "preferred_resolutions": self._preferred_resolutions(),
            "preferred_languages": self._preferred_languages(),
            "banned_terms": self._banned_terms(),
            "target_save_path": self._target_save_path(request),
        }
        executable = True
        if execution_path == "radarr":
            if not self.settings.radarr_enabled or not self.settings.radarr_base_url:
                executable = False
                requirements.append("radarr integration must be enabled")
            if (
                self.settings.manual_requests_allow_arr_add
                and not self.settings.radarr_root_folder_path
            ):
                executable = False
                requirements.append("radarr_root_folder_path is required")
            if (
                self.settings.manual_requests_allow_arr_add
                and not self.settings.radarr_quality_profile_id
            ):
                executable = False
                requirements.append("radarr_quality_profile_id is required")
        elif execution_path == "sonarr":
            if not self.settings.sonarr_enabled or not self.settings.sonarr_base_url:
                executable = False
                requirements.append("sonarr integration must be enabled")
            if (
                self.settings.manual_requests_allow_arr_add
                and not self.settings.sonarr_root_folder_path
            ):
                executable = False
                requirements.append("sonarr_root_folder_path is required")
            if (
                self.settings.manual_requests_allow_arr_add
                and not self.settings.sonarr_quality_profile_id
            ):
                executable = False
                requirements.append("sonarr_quality_profile_id is required")
        else:
            executable = False
            requirements.append("unsupported media type")

        if not self.settings.prowlarr_enabled or not self.settings.prowlarr_base_url:
            warnings.append("prowlarr candidate search is unavailable")
        return ManualRequestPlan(
            request_id=request.id or 0,
            executable=executable,
            execution_path=execution_path,
            requirements=requirements,
            warnings=warnings,
            payload=payload,
        )

    def create_request(
        self,
        payload: ManualRequestPayload,
        actor: str = "agent",
    ) -> ManualRequestResponse:
        request = ManualRequest(
            media_type=payload.media_type,
            title=payload.title,
            year=payload.year,
            season=payload.season,
            episode=payload.episode,
            quality_hint=payload.quality_hint,
            language_hint=payload.language_hint,
            freeleech_preferred=payload.freeleech_preferred,
            exclude_from_learning=True,
            notes=payload.notes,
            status=ManualRequestStatus.awaiting_execution.value,
            request_source=actor,
            raw_payload=payload.model_dump(),
        )
        request.execution_path = self._default_execution_path(request)
        request.arr_lookup_term = self._lookup_term(request)
        self.db.add(request)
        self.db.flush()
        self.db.add(
            ControllerEvent(
                event_type="manual_request_created",
                severity="info",
                message=f"Manual request created for {request.title}",
                payload={"request_id": request.id, "actor": actor},
            )
        )
        return ManualRequestResponse(
            request_id=request.id,
            status=request.status,
            execution_path=request.execution_path,
            exclude_from_learning=request.exclude_from_learning,
            message="manual request created",
            payload={"lookup_term": request.arr_lookup_term},
        )

    def preview_request(
        self,
        payload: ManualRequestPayload | ManualFulfillPayload,
    ) -> tuple[ManualRequestPlan, list[ManualCandidatePreview]]:
        request = ManualRequest(
            media_type=payload.media_type,
            title=payload.title,
            year=payload.year,
            season=payload.season,
            episode=payload.episode,
            quality_hint=payload.quality_hint,
            language_hint=payload.language_hint,
            freeleech_preferred=payload.freeleech_preferred,
            exclude_from_learning=True,
            notes=payload.notes,
            status=ManualRequestStatus.awaiting_execution.value,
            request_source="dashboard_preview",
            raw_payload={},
        )
        request.execution_path = self._default_execution_path(request)
        request.arr_lookup_term = self._lookup_term(request)
        plan = self.plan_request(request)
        limit = getattr(payload, "candidate_limit", None)
        candidates = self.candidate_preview(request, limit=limit)
        return plan, candidates

    def fulfill_request(
        self,
        payload: ManualFulfillPayload,
        actor: str = "agent",
    ) -> ManualFulfillResponse:
        request_result = self.create_request(payload, actor=actor)
        request = (
            self.db.query(ManualRequest)
            .filter(ManualRequest.id == request_result.request_id)
            .one()
        )
        request.raw_payload = {
            **request.raw_payload,
            "preferred_resolutions": payload.preferred_resolutions,
            "preferred_languages": payload.preferred_languages,
            "add_to_plex": payload.add_to_plex,
            "exact_match_required": payload.exact_match_required,
            "candidate_limit": payload.candidate_limit,
        }
        plan = self.plan_request(request)
        if not plan.executable:
            request.status = ManualRequestStatus.failed.value
            request.last_error = "request_not_executable"
            self._audit(
                "manual_request_failed",
                request,
                actor,
                "manual fulfill request is not executable with current configuration",
            )
            self.db.add(request)
            return ManualFulfillResponse(
                request=self._request_dict(request),
                plan=plan.model_dump(mode="json"),
                selected_candidate=None,
                candidates_considered=0,
                message="manual request is not executable with current configuration",
            )

        candidates = self.candidate_preview(request, limit=payload.candidate_limit)
        selected = self._choose_candidate(
            request,
            candidates,
            exact_match_required=payload.exact_match_required,
            preferred_resolutions=payload.preferred_resolutions,
            preferred_languages=payload.preferred_languages,
        )
        if selected is None:
            request.status = ManualRequestStatus.failed.value
            request.last_error = "no_candidate_match"
            request.result_payload = {"candidates_considered": len(candidates)}
            self._audit(
                "manual_request_failed",
                request,
                actor,
                "no candidate matched fulfill constraints",
            )
            self.db.add(request)
            return ManualFulfillResponse(
                request=self._request_dict(request),
                plan=plan.model_dump(mode="json"),
                selected_candidate=None,
                candidates_considered=len(candidates),
                message="no candidate matched fulfill constraints",
            )

        submit = self.submit_selected_candidate(request, selected, actor=actor)
        return ManualFulfillResponse(
            request=self._request_dict(request),
            plan=plan.model_dump(mode="json"),
            selected_candidate=selected.model_dump(mode="json"),
            candidates_considered=len(candidates),
            message=submit.message,
        )

    def execute_request(
        self,
        request: ManualRequest,
        actor: str = "agent",
        allow_arr_fallback: bool = False,
    ) -> ManualRequestResponse:
        request.execution_path = self._default_execution_path(request)
        if request.execution_path == "unsupported":
            request.status = ManualRequestStatus.failed.value
            request.last_error = "unsupported_media_type"
            self._audit("manual_request_failed", request, actor, request.last_error)
            self.db.add(request)
            return self._response(request, "manual request media type is unsupported")

        if not allow_arr_fallback:
            request.last_error = "arr_fallback_requires_explicit_confirmation"
            request.result_payload = {
                **(request.result_payload or {}),
                "guard": "arr_fallback_requires_explicit_confirmation",
            }
            self._audit(
                "manual_request_guarded",
                request,
                actor,
                "ARR fallback requires explicit confirmation",
            )
            self.db.add(request)
            return self._response(
                request,
                (
                    "ARR fallback requires explicit confirmation; "
                    "use exact candidate selection unless broad ARR search is intentional"
                ),
            )

        result = self._execute_arr_path(request)
        if result.ok:
            request.status = ManualRequestStatus.submitted_to_arr.value
            request.result_payload = result.payload
            request.last_error = None
            self._audit("manual_request_submitted", request, actor, result.message)
            self.db.add(request)
            return self._response(request, result.message)

        request.status = ManualRequestStatus.failed.value
        request.last_error = result.message
        request.result_payload = result.payload
        self._audit("manual_request_failed", request, actor, result.message)
        self.db.add(request)
        return self._response(request, result.message)

    def submit_selected_candidate(
        self,
        request: ManualRequest,
        selected: ManualCandidateSelectionPayload,
        actor: str = "agent",
    ) -> ManualRequestResponse:
        request.execution_path = "transferops_exact_candidate"
        lowered = selected.title.lower()
        banned = self._matched_banned_term(lowered)
        if banned:
            request.status = ManualRequestStatus.rejected.value
            request.last_error = f"blocked_by_banned_term:{banned}"
            request.chosen_payload = selected.model_dump(mode="json")
            self._audit(
                "manual_request_rejected",
                request,
                actor,
                f"selected candidate blocked by banned term: {banned}",
            )
            self.db.add(request)
            return self._response(
                request,
                f"selected candidate blocked by banned term: {banned}",
            )

        download_url = self._resolve_download_url(
            selected.title,
            selected.indexer,
            selected.download_url,
            selected.info_url,
        )
        candidate_payload = CandidatePayload(
            title=selected.title,
            guid=selected.info_url or download_url or selected.download_url,
            tracker=selected.indexer or "unknown",
            category="movie" if request.media_type == "movie" else "tv",
            release_year=request.year,
            size_bytes=selected.size_bytes,
            freeleech=selected.freeleech,
            published_at=selected.publish_date,
            seeders=selected.seeders,
            leechers=selected.leechers,
            download_url=download_url,
            source="manual_request",
            source_confidence=0.8,
            exclude_from_learning=True,
            raw_payload={
                "selected_candidate": selected.model_dump(mode="json"),
                "manual_request_id": request.id,
                "request_media_type": request.media_type,
                "request_title": request.title,
                "request_year": request.year,
                "request_season": request.season,
                "request_episode": request.episode,
                "request_quality_hint": request.quality_hint,
                "request_language_hint": request.language_hint,
                "save_path": self._target_save_path(request),
                "source": "manual_request",
            },
        )

        controller = self._controller()
        result = controller.intake_candidate(self.db, candidate_payload)
        if result.action == "admit" and hasattr(controller, "sync_from_qb"):
            controller.sync_from_qb(self.db)
        request.status = (
            ManualRequestStatus.admitted.value
            if result.action == "admit"
            else ManualRequestStatus.rejected.value
        )
        request.last_error = (
            result.reason if request.status == ManualRequestStatus.rejected.value else None
        )
        request.chosen_payload = selected.model_dump(mode="json")
        request.result_payload = {
            "action": result.action,
            "reason": result.reason,
            "score": result.score,
            "threshold": result.threshold,
        }
        request.candidate_id = result.candidate_id
        request.decision_id = result.decision_id
        matched_torrent = None
        if request.candidate_id is not None:
            matched_torrent = (
                self.db.query(Torrent)
                .filter(Torrent.candidate_id == request.candidate_id)
                .order_by(Torrent.id.desc())
                .first()
            )
        request.torrent_id = matched_torrent.id if matched_torrent is not None else None
        message = (
            "selected candidate submitted to transferops"
            if request.status == ManualRequestStatus.admitted.value
            else f"selected candidate rejected: {result.reason or 'policy rejection'}"
        )
        self._audit("manual_request_selected_candidate", request, actor, message)
        self.db.add(request)
        return self._response(request, message)

    def candidate_preview(
        self,
        request: ManualRequest,
        limit: int | None = None,
    ) -> list[ManualCandidatePreview]:
        if not self.settings.prowlarr_enabled or not self.settings.prowlarr_base_url:
            return []
        previews: list[ManualCandidatePreview] = []
        seen_titles: set[str] = set()
        for query in self._candidate_queries(request):
            params: dict[str, Any] = {"query": query, "type": "search"}
            if self.settings.prowlarr_manual_indexer_ids:
                params["indexerIds"] = self.settings.prowlarr_manual_indexer_ids
            response = self._request(
                self.settings.prowlarr_base_url or "",
                "/api/v1/search",
                self.settings.prowlarr_api_key or "",
                params=params,
            )
            items = response.json() if isinstance(response.json(), list) else []
            for item in items:
                preview = self._preview_from_item(request, item)
                if preview is None:
                    continue
                marker = normalize_title(preview.title)
                if marker in seen_titles:
                    continue
                seen_titles.add(marker)
                previews.append(preview)
        previews.sort(
            key=lambda row: self._preview_sort_key(request, row),
            reverse=True,
        )
        return previews[: max(1, min(limit or self.settings.manual_request_candidate_limit, 10))]

    def _response(self, request: ManualRequest, message: str) -> ManualRequestResponse:
        return ManualRequestResponse(
            request_id=request.id,
            status=request.status,
            execution_path=request.execution_path,
            exclude_from_learning=request.exclude_from_learning,
            message=message,
            payload=request.result_payload or {},
        )

    def _request_dict(self, request: ManualRequest) -> dict[str, Any]:
        return {
            "id": request.id,
            "media_type": request.media_type,
            "title": request.title,
            "year": request.year,
            "season": request.season,
            "episode": request.episode,
            "quality_hint": request.quality_hint,
            "language_hint": request.language_hint,
            "freeleech_preferred": request.freeleech_preferred,
            "exclude_from_learning": request.exclude_from_learning,
            "status": request.status,
            "execution_path": request.execution_path,
            "arr_source": request.arr_source,
            "arr_item_id": request.arr_item_id,
            "arr_command_id": request.arr_command_id,
            "matched_title": request.matched_title,
            "matched_year": request.matched_year,
            "candidate_id": request.candidate_id,
            "decision_id": request.decision_id,
            "torrent_id": request.torrent_id,
            "chosen_payload": request.chosen_payload,
            "last_error": request.last_error,
            "result_payload": request.result_payload,
            "created_at": request.created_at.isoformat(),
            "updated_at": request.updated_at.isoformat(),
        }

    def _audit(self, event_type: str, request: ManualRequest, actor: str, message: str) -> None:
        self.db.add(
            ControllerEvent(
                event_type=event_type,
                severity="info" if event_type.endswith("submitted") else "warning",
                message=message,
                payload={
                    "request_id": request.id,
                    "actor": actor,
                    "execution_path": request.execution_path,
                },
            )
        )

    def _default_execution_path(self, request: ManualRequest) -> str:
        if request.media_type == "movie":
            return "radarr"
        if request.media_type in {"series", "episode"}:
            return "sonarr"
        return "unsupported"

    def _lookup_term(self, request: ManualRequest) -> str:
        base = request.title
        if request.year:
            base = f"{base} {request.year}"
        if request.media_type == "episode" and request.season and request.episode:
            base = f"{base} S{request.season:02d}E{request.episode:02d}"
        return base

    def _target_save_path(self, request: ManualRequest) -> str:
        if request.media_type == "movie":
            return self.settings.manual_movies_save_path
        return self.settings.manual_series_save_path

    def _candidate_queries(self, request: ManualRequest) -> list[str]:
        queries = [self._lookup_term(request)]
        if request.media_type == "episode" and request.season:
            queries.append(f"{request.title} S{request.season:02d}")
        queries.append(request.title if not request.year else f"{request.title} {request.year}")
        deduped: list[str] = []
        for query in queries:
            if query not in deduped:
                deduped.append(query)
        return deduped

    def _choose_candidate(
        self,
        request: ManualRequest,
        candidates: list[ManualCandidatePreview],
        *,
        exact_match_required: bool | None,
        preferred_resolutions: list[str],
        preferred_languages: list[str],
    ) -> ManualCandidateSelectionPayload | None:
        require_exact = (
            exact_match_required
            if exact_match_required is not None
            else request.media_type == "episode"
        )
        filtered = list(candidates)
        if require_exact:
            filtered = [row for row in filtered if self._is_exact_match(request, row)]
        if not filtered:
            return None

        effective_resolutions = preferred_resolutions or self._preferred_resolutions()
        effective_languages = preferred_languages or self._preferred_languages()

        resolution_order = {
            value.lower(): idx for idx, value in enumerate(effective_resolutions) if value
        }
        language_order = {
            value.lower(): idx for idx, value in enumerate(effective_languages) if value
        }

        def sort_key(row: ManualCandidatePreview) -> tuple[int, int, float]:
            resolution_rank = resolution_order.get(
                (row.resolution or "").lower(),
                len(resolution_order),
            )
            language_rank = language_order.get(
                (row.language_match or "").lower(),
                len(language_order),
            )
            return (resolution_rank, language_rank, -row.ranking_score)

        chosen = sorted(filtered, key=sort_key)[0]
        return ManualCandidateSelectionPayload(
            title=chosen.title,
            indexer=chosen.indexer,
            size_bytes=chosen.size_bytes,
            seeders=chosen.seeders,
            leechers=chosen.leechers,
            freeleech=chosen.freeleech,
            download_url=chosen.download_url or chosen.info_url or "",
            info_url=chosen.info_url,
            publish_date=chosen.publish_date,
            resolution=chosen.resolution,
            language_match=chosen.language_match,
            ranking_score=chosen.ranking_score,
            rationale=chosen.rationale,
        )

    def _is_exact_match(self, request: ManualRequest, candidate: ManualCandidatePreview) -> bool:
        lowered = candidate.title.lower()
        if request.media_type == "episode" and request.season and request.episode:
            target = f"s{request.season:02d}e{request.episode:02d}"
            if target not in lowered:
                return False
        if request.year:
            extracted = self._extract_year_from_title(candidate.title)
            if (
                extracted is not None
                and extracted != request.year
                and request.media_type == "movie"
            ):
                return False
        return normalize_title(request.title) in normalize_title(candidate.title)

    def _extract_year_from_title(self, title: str) -> int | None:
        for token in title.replace(".", " ").replace("-", " ").split():
            if len(token) == 4 and token.isdigit():
                year = int(token)
                if 1900 <= year <= 2100:
                    return year
        return None

    def _preferred_resolutions(self) -> list[str]:
        return [
            item.strip().lower()
            for item in self.settings.manual_request_preferred_resolutions.split(",")
            if item.strip()
        ]

    def _preferred_languages(self) -> list[str]:
        return [
            item.strip().lower()
            for item in self.settings.manual_request_preferred_languages.split(",")
            if item.strip()
        ]

    def _banned_terms(self) -> list[str]:
        return [
            item.strip().lower()
            for item in self.settings.manual_request_banned_terms.split(",")
            if item.strip()
        ]

    def _matched_banned_term(self, lowered_title: str) -> str | None:
        for term in self._banned_terms():
            if term == "dolby vision" and re.search(r"dolby[ ._-]*vision", lowered_title):
                return term
            if term == "hdr" and re.search(
                r"(?<![a-z0-9])hdr(?:10(?:\+|plus)?)?(?![a-z0-9])",
                lowered_title,
            ):
                return term
            if term == "dv" and re.search(r"(?<![a-z0-9])dv(?![a-z0-9])", lowered_title):
                return term
            if term not in {"dolby vision", "hdr", "dv"} and re.search(
                rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])",
                lowered_title,
            ):
                return term
        return None

    def _preview_from_item(
        self,
        request: ManualRequest,
        item: dict[str, Any],
    ) -> ManualCandidatePreview | None:
        title = item.get("title") or item.get("releaseTitle") or ""
        if not title:
            return None
        lowered = title.lower()
        rationale: list[str] = []
        if self._matched_banned_term(lowered):
            return None
        resolution = self._extract_resolution(lowered)
        language_match = self._detect_language(lowered)
        seeders = self._coerce_int(item.get("seeders"))
        leechers = self._coerce_int(item.get("leechers"))
        size_bytes = self._coerce_int(item.get("size")) or 0
        score = 0.0
        score += self._resolution_score(request, resolution, rationale)
        score += self._language_score(language_match, rationale)
        score += self._swarm_score(seeders, leechers, rationale)
        score -= self._size_penalty(size_bytes, request, rationale)
        freeleech = bool(item.get("freeleech") or item.get("downloadVolumeFactor") == 0)
        if request.freeleech_preferred and freeleech:
            score += 0.75
            rationale.append("freeleech preferred")
        if self._is_exact_match(
            request,
            ManualCandidatePreview(
                title=title,
                indexer="",
                size_bytes=size_bytes,
                seeders=seeders,
                leechers=leechers,
                freeleech=freeleech,
                download_url=None,
                info_url=None,
                publish_date=None,
                resolution=resolution,
                language_match=language_match,
                ranking_score=0.0,
                rationale=[],
            ),
        ):
            score += 1.5
            rationale.append("exact request match")
        info_url = item.get("guid") or item.get("infoUrl")
        download_url = self._resolve_download_url(
            title,
            str(item.get("indexer") or item.get("indexerName") or "unknown"),
            item.get("downloadUrl"),
            info_url,
        )
        return ManualCandidatePreview(
            title=title,
            indexer=str(item.get("indexer") or item.get("indexerName") or "unknown"),
            size_bytes=size_bytes,
            seeders=seeders,
            leechers=leechers,
            freeleech=freeleech,
            download_url=download_url,
            info_url=info_url,
            publish_date=self._parse_date(item.get("publishDate") or item.get("publishDateUtc")),
            resolution=resolution,
            language_match=language_match,
            ranking_score=round(score, 3),
            rationale=rationale,
        )

    def _resolve_download_url(
        self,
        title: str,
        indexer: str | None,
        download_url: str | None,
        info_url: str | None,
    ) -> str | None:
        tracker = indexer or "unknown"
        for candidate in (download_url, info_url):
            resolved = canonicalize_download_url(self.settings, tracker, candidate, title)
            if looks_like_torrent_url(resolved):
                return resolved
        return download_url or info_url

    def _preview_sort_key(
        self,
        request: ManualRequest,
        preview: ManualCandidatePreview,
    ) -> tuple[int, float]:
        return (1 if self._is_exact_match(request, preview) else 0, preview.ranking_score)

    def _resolution_score(
        self,
        request: ManualRequest,
        resolution: str | None,
        rationale: list[str],
    ) -> float:
        prefs = self._preferred_resolutions()
        if resolution is None:
            return -0.2
        bonus = 0.0
        if request.quality_hint and request.quality_hint.lower() == resolution:
            bonus = 0.75
            rationale.append(f"matches requested quality {resolution}")
        if resolution in prefs:
            rank = max(0, len(prefs) - prefs.index(resolution))
            rationale.append(f"preferred resolution {resolution}")
            return float(rank) + bonus
        rationale.append(f"non-preferred resolution {resolution}")
        return -0.5 + bonus

    def _language_score(self, language: str | None, rationale: list[str]) -> float:
        prefs = self._preferred_languages()
        if language is None:
            return 0.0
        if language in prefs:
            rank = max(0, len(prefs) - prefs.index(language))
            rationale.append(f"preferred language {language}")
            return 0.5 * float(rank)
        return -0.25

    def _swarm_score(
        self,
        seeders: int | None,
        leechers: int | None,
        rationale: list[str],
    ) -> float:
        seeders = seeders or 0
        leechers = leechers or 0
        if seeders <= 0:
            rationale.append("no seeders reported")
            return -2.0
        score = min(3.0, log10(seeders + 1) + (leechers / max(seeders, 1)))
        rationale.append(f"swarm {seeders} seeders/{leechers} leechers")
        return score

    def _size_penalty(
        self,
        size_bytes: int,
        request: ManualRequest,
        rationale: list[str],
    ) -> float:
        if size_bytes <= 0:
            return 0.0
        size_gb = size_bytes / BYTES_PER_GB
        if request.media_type == "episode":
            penalty = max(0.0, size_gb - 4.0) * 0.3
        else:
            penalty = max(0.0, size_gb - 20.0) * 0.05
        if penalty:
            rationale.append(f"size penalty {size_gb:.1f} GB")
        return penalty

    def _extract_resolution(self, lowered_title: str) -> str | None:
        if "2160p" in lowered_title or "4k" in lowered_title:
            return "2160p"
        if "1080p" in lowered_title:
            return "1080p"
        if "720p" in lowered_title:
            return "720p"
        return None

    def _detect_language(self, lowered_title: str) -> str | None:
        if "english" in lowered_title or "eng" in lowered_title:
            return "english"
        if "spa" in lowered_title or "spanish" in lowered_title or "castellano" in lowered_title:
            return "spanish"
        return None

    def _coerce_int(self, value: Any) -> int | None:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _parse_date(self, value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    def _execute_arr_path(self, request: ManualRequest) -> ArrExecutionResult:
        if request.execution_path == "radarr":
            return self._execute_radarr(request)
        if request.execution_path == "sonarr":
            return self._execute_sonarr(request)
        return ArrExecutionResult(False, "unsupported execution path", {})

    def _request(
        self,
        base_url: str,
        path: str,
        api_key: str,
        method: str = "GET",
        **kwargs: Any,
    ) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["X-Api-Key"] = api_key
        response = self.session.request(
            method,
            f"{base_url.rstrip('/')}{path}",
            headers=headers,
            timeout=max(self.settings.manual_requests_arr_timeout_minutes * 60, 15),
            **kwargs,
        )
        response.raise_for_status()
        return response

    def _find_existing_movie(self, request: ManualRequest) -> dict[str, Any] | None:
        response = self._request(
            self.settings.radarr_base_url or "",
            "/api/v3/movie",
            self.settings.radarr_api_key or "",
        )
        wanted = normalize_title(request.title)
        for item in response.json():
            if normalize_title(item.get("title") or "") != wanted:
                continue
            if request.year and item.get("year") != request.year:
                continue
            return item
        return None

    def _find_existing_series(self, request: ManualRequest) -> dict[str, Any] | None:
        response = self._request(
            self.settings.sonarr_base_url or "",
            "/api/v3/series",
            self.settings.sonarr_api_key or "",
        )
        wanted = normalize_title(request.title)
        for item in response.json():
            if normalize_title(item.get("title") or "") != wanted:
                continue
            if request.year and item.get("year") != request.year:
                continue
            return item
        return None

    def _lookup_movie(self, request: ManualRequest) -> dict[str, Any] | None:
        response = self._request(
            self.settings.radarr_base_url or "",
            "/api/v3/movie/lookup",
            self.settings.radarr_api_key or "",
            params={"term": self._lookup_term(request)},
        )
        wanted = normalize_title(request.title)
        for item in response.json():
            if normalize_title(item.get("title") or "") != wanted:
                continue
            if request.year and item.get("year") != request.year:
                continue
            return item
        items = response.json()
        return items[0] if items else None

    def _lookup_series(self, request: ManualRequest) -> dict[str, Any] | None:
        response = self._request(
            self.settings.sonarr_base_url or "",
            "/api/v3/series/lookup",
            self.settings.sonarr_api_key or "",
            params={"term": self._lookup_term(request)},
        )
        wanted = normalize_title(request.title)
        for item in response.json():
            if normalize_title(item.get("title") or "") != wanted:
                continue
            if request.year and item.get("year") != request.year:
                continue
            return item
        items = response.json()
        return items[0] if items else None

    def _add_movie(self, request: ManualRequest, lookup: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.radarr_root_folder_path or not self.settings.radarr_quality_profile_id:
            raise RuntimeError("radarr add requires root folder path and quality profile id")
        payload = {
            **lookup,
            "rootFolderPath": self.settings.radarr_root_folder_path,
            "qualityProfileId": self.settings.radarr_quality_profile_id,
            "monitored": True,
            "addOptions": {"searchForMovie": False},
        }
        response = self._request(
            self.settings.radarr_base_url or "",
            "/api/v3/movie",
            self.settings.radarr_api_key or "",
            method="POST",
            json=payload,
        )
        return response.json()

    def _add_series(self, request: ManualRequest, lookup: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.sonarr_root_folder_path or not self.settings.sonarr_quality_profile_id:
            raise RuntimeError("sonarr add requires root folder path and quality profile id")
        payload = {
            **lookup,
            "rootFolderPath": self.settings.sonarr_root_folder_path,
            "qualityProfileId": self.settings.sonarr_quality_profile_id,
            "monitored": True,
            "addOptions": {"searchForMissingEpisodes": False},
        }
        if self.settings.sonarr_language_profile_id is not None:
            payload["languageProfileId"] = self.settings.sonarr_language_profile_id
        response = self._request(
            self.settings.sonarr_base_url or "",
            "/api/v3/series",
            self.settings.sonarr_api_key or "",
            method="POST",
            json=payload,
        )
        return response.json()

    def _trigger_radarr_search(self, request: ManualRequest, item_id: int) -> dict[str, Any]:
        response = self._request(
            self.settings.radarr_base_url or "",
            "/api/v3/command",
            self.settings.radarr_api_key or "",
            method="POST",
            json={"name": "MoviesSearch", "movieIds": [item_id]},
        )
        return response.json()

    def _trigger_sonarr_search(self, request: ManualRequest, item_id: int) -> dict[str, Any]:
        if request.media_type == "episode" and request.season and request.episode:
            episode_ids = self._episode_ids_for_request(item_id, request)
            if not episode_ids:
                raise RuntimeError("unable to resolve Sonarr episode ids for request")
            payload: dict[str, Any] = {"name": "EpisodeSearch", "episodeIds": episode_ids}
        else:
            payload = {"name": "SeriesSearch", "seriesId": item_id}
        response = self._request(
            self.settings.sonarr_base_url or "",
            "/api/v3/command",
            self.settings.sonarr_api_key or "",
            method="POST",
            json=payload,
        )
        return response.json()

    def _episode_ids_for_request(self, series_id: int, request: ManualRequest) -> list[int]:
        response = self._request(
            self.settings.sonarr_base_url or "",
            "/api/v3/episode",
            self.settings.sonarr_api_key or "",
            params={"seriesId": series_id},
        )
        matches = []
        for item in response.json():
            if item.get("seasonNumber") != request.season:
                continue
            if item.get("episodeNumber") != request.episode:
                continue
            episode_id = item.get("id")
            if episode_id is not None:
                matches.append(int(episode_id))
        return matches

    def _execute_radarr(self, request: ManualRequest) -> ArrExecutionResult:
        if not self.settings.radarr_enabled or not self.settings.radarr_base_url:
            return ArrExecutionResult(False, "radarr integration disabled", {})
        existing = self._find_existing_movie(request)
        item = existing
        created = False
        if item is None:
            if self.settings.manual_requests_require_existing_arr_item:
                return ArrExecutionResult(False, "movie not present in Radarr", {})
            if not self.settings.manual_requests_allow_arr_add:
                return ArrExecutionResult(False, "Radarr add is disabled for manual requests", {})
            lookup = self._lookup_movie(request)
            if lookup is None:
                return ArrExecutionResult(False, "Radarr lookup found no matching movie", {})
            try:
                item = self._add_movie(request, lookup)
            except Exception as exc:  # noqa: BLE001
                return ArrExecutionResult(False, str(exc), {})
            created = True
        command = self._trigger_radarr_search(request, int(item["id"]))
        request.arr_source = "radarr"
        request.arr_item_id = int(item["id"])
        request.arr_command_id = int(command.get("id") or 0) or None
        request.matched_title = item.get("title") or request.title
        request.matched_year = item.get("year") or request.year
        payload = {
            "arr_item_id": request.arr_item_id,
            "arr_command_id": request.arr_command_id,
            "created_item": created,
            "matched_title": request.matched_title,
            "matched_year": request.matched_year,
        }
        return ArrExecutionResult(True, "submitted to Radarr search", payload)

    def _execute_sonarr(self, request: ManualRequest) -> ArrExecutionResult:
        if not self.settings.sonarr_enabled or not self.settings.sonarr_base_url:
            return ArrExecutionResult(False, "sonarr integration disabled", {})
        existing = self._find_existing_series(request)
        item = existing
        created = False
        if item is None:
            if self.settings.manual_requests_require_existing_arr_item:
                return ArrExecutionResult(False, "series not present in Sonarr", {})
            if not self.settings.manual_requests_allow_arr_add:
                return ArrExecutionResult(False, "Sonarr add is disabled for manual requests", {})
            lookup = self._lookup_series(request)
            if lookup is None:
                return ArrExecutionResult(False, "Sonarr lookup found no matching series", {})
            try:
                item = self._add_series(request, lookup)
            except Exception as exc:  # noqa: BLE001
                return ArrExecutionResult(False, str(exc), {})
            created = True
        command = self._trigger_sonarr_search(request, int(item["id"]))
        request.arr_source = "sonarr"
        request.arr_item_id = int(item["id"])
        request.arr_command_id = int(command.get("id") or 0) or None
        request.matched_title = item.get("title") or request.title
        request.matched_year = item.get("year") or request.year
        payload = {
            "arr_item_id": request.arr_item_id,
            "arr_command_id": request.arr_command_id,
            "created_item": created,
            "matched_title": request.matched_title,
            "matched_year": request.matched_year,
        }
        return ArrExecutionResult(True, "submitted to Sonarr search", payload)
