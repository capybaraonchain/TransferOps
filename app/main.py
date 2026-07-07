from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base, SessionLocal, engine, get_db, migrate_sqlite_schema
from app.models import (
    Alert,
    BucketStats,
    ControllerEvent,
    Decision,
    IntegrationState,
    LibraryHandoff,
    ManualRequest,
    MetadataCache,
    ReleaseCandidate,
    Torrent,
    WantedItem,
)
from app.services.controller import ControllerService
from app.services.integrations import (
    ConnectivityService,
    IntegrationResult,
    WantedSyncService,
    extract_year,
    record_integration_result,
)
from app.services.library import LibraryHandoffService
from app.services.lifecycle import LifecycleReconciler
from app.services.logging import configure_logging
from app.services.manual_requests import ArrRequestService
from app.services.metadata import MetadataResolver
from app.services.qbittorrent import QBittorrentClient
from app.services.rss import import_rss
from app.services.schemas import (
    ManualCandidateSelectionPayload,
    ManualFulfillPayload,
    ManualRequestPayload,
)
from app.services.settings import SettingsService
from app.units import BYTES_PER_GB

security = HTTPBasic()
templates = Jinja2Templates(directory="app/templates")
AUTOBRR_BRIDGE_STATE_PATH = Path(".runtime/source-bridge-state.json")


def resolve_settings(db: Session) -> Settings:
    return SettingsService(db).resolve()


def get_runtime_settings(db: Session = Depends(get_db)) -> Settings:
    return resolve_settings(db)


def get_controller(settings: Settings = Depends(get_runtime_settings)) -> ControllerService:
    return ControllerService(settings, qb=QBittorrentClient(settings))


def auth(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    db: Session = Depends(get_db),
) -> str:
    settings = resolve_settings(db)
    user_ok = secrets.compare_digest(credentials.username, settings.dashboard_username)
    pass_ok = secrets.compare_digest(credentials.password, settings.dashboard_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def agent_auth(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> str:
    settings = resolve_settings(db)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, settings.agent_api_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token")
    return "agent"


class SchedulerManager:
    def __init__(self) -> None:
        self.scheduler = BackgroundScheduler()

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def reload_from_db(self) -> None:
        db = SessionLocal()
        try:
            settings = resolve_settings(db)
        finally:
            db.close()
        self._apply(settings)

    def _apply(self, settings: Settings) -> None:
        self._replace_job(
            "qbit-sync", _scheduler_job_sync, "interval", seconds=settings.poll_interval_seconds
        )
        self._replace_job(
            "reconcile",
            _scheduler_job_reconcile,
            "interval",
            seconds=settings.reconcile_interval_seconds,
        )
        self._replace_optional_job(
            "rss-import",
            settings.rss_enabled and bool(settings.rss_url),
            _scheduler_job_rss,
            "interval",
            minutes=settings.rss_poll_interval_minutes,
        )
        wanted_enabled = settings.radarr_enabled or settings.sonarr_enabled
        minutes = min(settings.radarr_poll_interval_minutes, settings.sonarr_poll_interval_minutes)
        self._replace_optional_job(
            "wanted-refresh",
            wanted_enabled,
            _scheduler_job_wanted,
            "interval",
            minutes=max(minutes, 1),
        )
        self._replace_optional_job(
            "library-handoff",
            settings.plex_handoff_enabled,
            _scheduler_job_library_handoff,
            "interval",
            seconds=max(settings.plex_handoff_interval_seconds, 30),
        )

    def _replace_job(self, job_id: str, func_ref: Any, trigger: str, **kwargs: Any) -> None:
        self.scheduler.add_job(func_ref, trigger, id=job_id, replace_existing=True, **kwargs)

    def _replace_optional_job(
        self,
        job_id: str,
        enabled: bool,
        func_ref: Any,
        trigger: str,
        **kwargs: Any,
    ) -> None:
        if enabled:
            self._replace_job(job_id, func_ref, trigger, **kwargs)
        elif self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)


def _scheduler_job_sync() -> None:
    db = SessionLocal()
    try:
        settings = resolve_settings(db)
        controller = ControllerService(settings, qb=QBittorrentClient(settings))
        synced = controller.sync_from_qb(db)
        record_integration_result(
            db,
            "qbit",
            True,
            IntegrationResult(
                ok=True,
                message="qBittorrent sync complete",
                payload={"synced": synced},
            ),
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        record_integration_result(
            db,
            "qbit",
            True,
            IntegrationResult(ok=False, message=str(exc), payload={}),
        )
        db.commit()
    finally:
        db.close()


def _scheduler_job_reconcile() -> None:
    db = SessionLocal()
    try:
        settings = resolve_settings(db)
        LifecycleReconciler(settings).reconcile(db)
        ControllerService(settings, qb=QBittorrentClient(settings)).record_snapshot(db)
        db.commit()
    finally:
        db.close()


def _scheduler_job_rss() -> None:
    db = SessionLocal()
    try:
        settings = resolve_settings(db)
        controller = ControllerService(settings, qb=QBittorrentClient(settings))
        imported = 0
        for payload in import_rss(settings):
            controller.intake_candidate(db, payload)
            imported += 1
        record_integration_result(
            db,
            "rss",
            settings.rss_enabled,
            IntegrationResult(
                ok=True,
                message="RSS import complete",
                payload={"imported": imported},
            ),
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        record_integration_result(
            db,
            "rss",
            settings.rss_enabled if "settings" in locals() else True,
            IntegrationResult(ok=False, message=str(exc), payload={}),
        )
        db.commit()
    finally:
        db.close()


def _scheduler_job_wanted() -> None:
    db = SessionLocal()
    try:
        settings = resolve_settings(db)
        results = WantedSyncService(db, settings).refresh()
        record_integration_result(
            db,
            "radarr",
            settings.radarr_enabled,
            results.get(
                "radarr",
                IntegrationResult(ok=False, message="Radarr integration disabled", payload={}),
            ),
        )
        record_integration_result(
            db,
            "sonarr",
            settings.sonarr_enabled,
            results.get(
                "sonarr",
                IntegrationResult(ok=False, message="Sonarr integration disabled", payload={}),
            ),
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        if "settings" in locals():
            if settings.radarr_enabled:
                record_integration_result(
                    db,
                    "radarr",
                    True,
                    IntegrationResult(ok=False, message=str(exc), payload={}),
                )
            if settings.sonarr_enabled:
                record_integration_result(
                    db,
                    "sonarr",
                    True,
                    IntegrationResult(ok=False, message=str(exc), payload={}),
                )
            db.commit()
    finally:
        db.close()


def _scheduler_job_library_handoff() -> None:
    db = SessionLocal()
    try:
        settings = resolve_settings(db)
        result = LibraryHandoffService(db, settings).process_pending()
        record_integration_result(
            db,
            "plex",
            settings.plex_enabled,
            result,
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        if "settings" in locals():
            record_integration_result(
                db,
                "plex",
                settings.plex_enabled,
                IntegrationResult(ok=False, message=str(exc), payload={}),
            )
            db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    Base.metadata.create_all(bind=engine)
    migrate_sqlite_schema()
    db = SessionLocal()
    try:
        _backfill_release_years(db)
        _backfill_manual_learning_exclusions(db)
        db.commit()
    finally:
        db.close()
    app.state.scheduler_manager = SchedulerManager()
    app.state.scheduler_manager.start()
    app.state.scheduler_manager.reload_from_db()
    try:
        yield
    finally:
        app.state.scheduler_manager.shutdown()


app = FastAPI(title="TransferOps", lifespan=lifespan)


def _masked_settings(db: Session) -> dict[str, Any]:
    return SettingsService(db).masked()


def _source_counts(db: Session) -> dict[str, int]:
    rows = db.query(
        ReleaseCandidate.source,
        ReleaseCandidate.dedupe_key,
        ReleaseCandidate.id,
    ).all()
    counts: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for source, dedupe_key, candidate_id in rows:
        marker = dedupe_key or f"legacy:{candidate_id}"
        pair = (source, marker)
        if pair in seen:
            continue
        seen.add(pair)
        counts[source] = counts.get(source, 0) + 1
    return counts


def _backfill_release_years(db: Session) -> int:
    settings = resolve_settings(db)
    resolver = MetadataResolver(settings)
    updated = 0
    rows = db.query(ReleaseCandidate).filter(ReleaseCandidate.release_year.is_(None)).all()
    for row in rows:
        year = extract_year(row.title)
        if year is not None:
            row.release_year = year
            db.add(row)
            updated += 1
            continue
        try:
            if resolver.enrich_release_candidate(db, row):
                updated += 1
        except Exception:
            continue
    return updated


def _backfill_manual_learning_exclusions(db: Session) -> int:
    updated = 0
    manual_sources = {"manual", "manual_request"}
    candidates = (
        db.query(ReleaseCandidate)
        .filter(
            ReleaseCandidate.source.in_(manual_sources),
            ReleaseCandidate.exclude_from_learning.is_(False),
        )
        .all()
    )
    for row in candidates:
        row.exclude_from_learning = True
        db.add(row)
        updated += 1
    requests = (
        db.query(ManualRequest)
        .filter(ManualRequest.exclude_from_learning.is_(False))
        .all()
    )
    for row in requests:
        row.exclude_from_learning = True
        db.add(row)
        updated += 1
    torrents = (
        db.query(Torrent)
        .join(ReleaseCandidate, Torrent.candidate_id == ReleaseCandidate.id)
        .filter(
            ReleaseCandidate.source.in_(manual_sources),
            Torrent.exclude_from_learning.is_(False),
        )
        .all()
    )
    for row in torrents:
        row.exclude_from_learning = True
        db.add(row)
        updated += 1
    return updated


def _integration_status(db: Session) -> dict[str, Any]:
    settings = resolve_settings(db)
    states = {row.name: row for row in db.query(IntegrationState).all()}
    last_autobrr_event = (
        db.query(ControllerEvent)
        .filter(ControllerEvent.event_type == "autobrr_event")
        .order_by(desc(ControllerEvent.created_at))
        .first()
    )

    def state_payload(name: str, **extra: Any) -> dict[str, Any]:
        state = states.get(name)
        payload = {
            "enabled": extra.pop("enabled"),
            "base_url": extra.pop("base_url", None),
            "last_success_at": (
                state.last_success_at.isoformat()
                if state and state.last_success_at
                else None
            ),
            "last_failure_at": (
                state.last_failure_at.isoformat()
                if state and state.last_failure_at
                else None
            ),
            "consecutive_failures": state.consecutive_failures if state else 0,
            "last_error": state.last_error if state else None,
            "payload": state.payload if state else {},
        }
        payload.update(extra)
        return payload

    bridge_status: dict[str, Any] = {
        "enabled": False,
        "mode": "direct_webhook",
        "last_seen_at": None,
        "last_release_id": None,
    }
    if AUTOBRR_BRIDGE_STATE_PATH.exists():
        bridge_status["enabled"] = True
        bridge_status["mode"] = "db_bridge"
        bridge_status["last_seen_at"] = datetime.fromtimestamp(
            AUTOBRR_BRIDGE_STATE_PATH.stat().st_mtime,
            tz=UTC,
        ).replace(tzinfo=None).isoformat()
        try:
            bridge_state = json.loads(AUTOBRR_BRIDGE_STATE_PATH.read_text())
        except json.JSONDecodeError:
            bridge_state = {}
        bridge_status["last_release_id"] = bridge_state.get("last_release_id")

    return {
        "autobrr": state_payload(
            "autobrr",
            enabled=settings.autobrr_enabled,
            base_url=settings.autobrr_base_url,
            last_event_at=(
                last_autobrr_event.created_at.isoformat()
                if last_autobrr_event
                else None
            ),
            delivery=bridge_status,
        ),
        "rss": state_payload(
            "rss",
            enabled=settings.rss_enabled,
            url="********" if settings.rss_url else "",
            assume_freeleech=settings.rss_assume_freeleech,
        ),
        "radarr": state_payload(
            "radarr",
            enabled=settings.radarr_enabled,
            base_url=settings.radarr_base_url,
        ),
        "sonarr": state_payload(
            "sonarr",
            enabled=settings.sonarr_enabled,
            base_url=settings.sonarr_base_url,
        ),
        "prowlarr": state_payload(
            "prowlarr",
            enabled=settings.prowlarr_enabled,
            base_url=settings.prowlarr_base_url,
        ),
        "plex": state_payload(
            "plex",
            enabled=settings.plex_enabled,
            base_url=settings.plex_base_url,
        ),
        "qbit": state_payload("qbit", enabled=True, base_url=settings.qbit_base_url),
    }


def _test_result(result: Any) -> JSONResponse:
    status_code = 200 if result.ok else 400
    return JSONResponse(
        status_code=status_code,
        content={"ok": result.ok, "message": result.message, "payload": result.payload},
    )


def _recorded_test_result(
    db: Session,
    name: str,
    enabled: bool,
    result: IntegrationResult,
) -> JSONResponse:
    record_integration_result(db, name, enabled, result)
    db.commit()
    return _test_result(result)


def _manual_request_failure_category(row: ManualRequest) -> str | None:
    if not row.last_error:
        return None
    error = row.last_error.lower()
    if "arr_fallback_requires_explicit_confirmation" in error:
        return "arr_fallback_guarded"
    if "blocked_by_banned_term" in error:
        return "blocked_by_preferences"
    if "no_candidate_match" in error:
        return "not_found_exact_match"
    if "request_not_executable" in error:
        return "configuration_blocked"
    if "disabled" in error or "required" in error or "not configured" in error:
        return "configuration_blocked"
    if "rejected" in error or "policy" in error or "high_water" in error or "violation" in error:
        return "policy_rejected"
    if "not present in radarr" in error or "not present in sonarr" in error:
        return "missing_arr_item"
    if "lookup found no matching" in error:
        return "lookup_failed"
    return "execution_failed"


def _manual_request_timeline(db: Session, row: ManualRequest) -> list[dict[str, Any]]:
    handoff = (
        db.query(LibraryHandoff)
        .filter(LibraryHandoff.manual_request_id == row.id)
        .order_by(desc(LibraryHandoff.created_at))
        .first()
    )
    steps: list[dict[str, Any]] = [
        {
            "step": "request_created",
            "status": "complete",
            "timestamp": row.created_at.isoformat(),
            "detail": f"{row.media_type} request created",
        }
    ]
    if row.execution_path:
        steps.append(
            {
                "step": "planning",
                "status": "complete",
                "timestamp": row.updated_at.isoformat(),
                "detail": f"execution path {row.execution_path}",
            }
        )
    if row.last_error == "arr_fallback_requires_explicit_confirmation":
        steps.append(
            {
                "step": "execution_guard",
                "status": "waiting",
                "timestamp": row.updated_at.isoformat(),
                "detail": "ARR fallback requires explicit confirmation",
            }
        )
    if row.chosen_payload:
        steps.append(
            {
                "step": "candidate_selected",
                "status": "complete",
                "timestamp": row.updated_at.isoformat(),
                "detail": row.chosen_payload.get("title"),
            }
        )
    if row.decision_id:
        steps.append(
            {
                "step": "admission_decision",
                "status": "complete" if row.status == "admitted" else "failed",
                "timestamp": row.updated_at.isoformat(),
                "detail": (row.result_payload or {}).get("reason") or row.status,
            }
        )
    elif row.last_error:
        steps.append(
            {
                "step": "admission_decision",
                "status": "failed",
                "timestamp": row.updated_at.isoformat(),
                "detail": row.last_error,
            }
        )
    if row.torrent_id:
        torrent = db.query(Torrent).filter(Torrent.id == row.torrent_id).one_or_none()
        if torrent is not None:
            if torrent.progress >= 1.0:
                status = "complete"
                detail = "download complete"
            elif torrent.progress > 0:
                status = "active"
                detail = f"downloading {torrent.progress * 100:.0f}%"
            else:
                status = "pending"
                detail = "queued in qBittorrent"
            steps.append(
                {
                    "step": "download",
                    "status": status,
                    "timestamp": (torrent.last_seen_at or row.updated_at).isoformat(),
                    "detail": detail,
                }
            )
    if handoff is not None:
        detail = handoff.last_error or handoff.status
        if handoff.status == "completed":
            status = "complete"
        elif handoff.status == "failed":
            status = "failed"
        elif handoff.status == "scan_requested":
            status = "active"
        else:
            status = "pending"
        steps.append(
            {
                "step": "plex_handoff",
                "status": status,
                "timestamp": (
                    handoff.imported_at or handoff.scan_requested_at or handoff.updated_at
                ).isoformat(),
                "detail": detail,
            }
        )
    return steps


def _manual_request_dict_with_context(db: Session, row: ManualRequest) -> dict[str, Any]:
    handoff = (
        db.query(LibraryHandoff)
        .filter(LibraryHandoff.manual_request_id == row.id)
        .order_by(desc(LibraryHandoff.created_at))
        .first()
    )
    next_frontier = None
    if row.media_type in {"series", "episode"}:
        frontiers = LibraryHandoffService(db, resolve_settings(db)).tv_priority_frontier(limit=100)
        normalized_title = row.title.lower().strip()
        for item in frontiers:
            if item["series_title"].lower().strip() == normalized_title:
                next_frontier = item
                break
    linked_torrent = None
    if row.torrent_id is not None:
        torrent = db.query(Torrent).filter(Torrent.id == row.torrent_id).one_or_none()
        if torrent is not None:
            linked_torrent = {
                "id": torrent.id,
                "title": torrent.title,
                "state": torrent.state,
                "progress": torrent.progress,
                "ratio": torrent.ratio,
                "uploaded_bytes": torrent.uploaded_bytes,
                "seed_time_seconds": torrent.seed_time_seconds,
                "save_path": torrent.save_path,
                "last_seen_at": torrent.last_seen_at.isoformat() if torrent.last_seen_at else None,
            }
    handoff_phase = None
    if handoff is not None:
        if handoff.status == "scan_requested":
            handoff_phase = "refresh_requested"
        elif handoff.status == "completed":
            handoff_phase = "import_confirmed"
        elif handoff.status == "failed":
            handoff_phase = "failed"
        elif handoff.status == "waiting_config":
            handoff_phase = "config_needed"
        else:
            handoff_phase = "queued"
    return {
        "id": row.id,
        "media_type": row.media_type,
        "title": row.title,
        "year": row.year,
        "season": row.season,
        "episode": row.episode,
        "quality_hint": row.quality_hint,
        "language_hint": row.language_hint,
        "freeleech_preferred": row.freeleech_preferred,
        "exclude_from_learning": row.exclude_from_learning,
        "status": row.status,
        "execution_path": row.execution_path,
        "arr_source": row.arr_source,
        "arr_item_id": row.arr_item_id,
        "arr_command_id": row.arr_command_id,
        "matched_title": row.matched_title,
        "matched_year": row.matched_year,
        "candidate_id": row.candidate_id,
        "decision_id": row.decision_id,
        "torrent_id": row.torrent_id,
        "chosen_payload": row.chosen_payload,
        "last_error": row.last_error,
        "failure_category": _manual_request_failure_category(row),
        "result_payload": row.result_payload,
        "add_to_plex": bool((row.raw_payload or {}).get("add_to_plex", True)),
        "linked_torrent": linked_torrent,
        "library_handoff": (
            {
                "id": handoff.id,
                "status": handoff.status,
                "phase": handoff_phase,
                "last_error": handoff.last_error,
                "scan_requested_at": (
                    handoff.scan_requested_at.isoformat() if handoff.scan_requested_at else None
                ),
                "imported_at": handoff.imported_at.isoformat() if handoff.imported_at else None,
            }
            if handoff is not None
            else None
        ),
        "next_tv_frontier": next_frontier,
        "timeline": _manual_request_timeline(db, row),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _bucket_dict(row: BucketStats) -> dict[str, Any]:
    return {
        "bucket_key": row.bucket_key,
        "definition": row.definition,
        "sample_count": row.sample_count,
        "training_state": "untrained" if row.sample_count == 0 else "trained",
        "ewma_upload_1h": row.ewma_upload_1h,
        "ewma_upload_6h": row.ewma_upload_6h,
        "ewma_upload_24h": row.ewma_upload_24h,
        "ewma_upload_7d": row.ewma_upload_7d,
        "ewma_time_to_safe_hours": row.ewma_time_to_safe_hours,
        "stall_probability": row.stall_probability,
        "uncertainty_bonus": row.uncertainty_bonus,
    }


def _metadata_summary(db: Session) -> dict[str, Any]:
    settings = resolve_settings(db)
    rows = db.query(MetadataCache).all()
    total = len(rows)
    resolved = sum(1 for row in rows if row.status == "resolved")
    missed = sum(1 for row in rows if row.status == "miss")
    provider_counts: dict[str, int] = {}
    for row in rows:
        provider_counts[row.provider] = provider_counts.get(row.provider, 0) + 1
    unresolved_candidates = (
        db.query(ReleaseCandidate).filter(ReleaseCandidate.release_year.is_(None)).count()
    )
    return {
        "enabled": settings.metadata_enrichment_enabled,
        "tmdb_configured": bool(settings.tmdb_api_key),
        "cache_total": total,
        "cache_resolved": resolved,
        "cache_missed": missed,
        "provider_counts": provider_counts,
        "remaining_release_year_null": unresolved_candidates,
    }


def _budget_summary(db: Session) -> dict[str, Any]:
    settings = resolve_settings(db)
    controller = ControllerService(settings, qb=QBittorrentClient(settings))
    snapshot = controller.snapshot_model(db)
    protocol_lane = controller.lane_status(db, candidate_is_manual=False)
    manual_lane = controller.lane_status(db, candidate_is_manual=True)
    protocol_lane = controller.lane_status(db, candidate_is_manual=False)
    manual_lane = controller.lane_status(db, candidate_is_manual=True)
    return {
        "protocol": {
            "usage_bytes": snapshot.protocol_usage_bytes,
            "projected_usage_bytes": snapshot.protocol_projected_usage_bytes,
            "cap_gb": settings.managed_disk_cap_gb,
            "high_water_gb": settings.admission_high_water_mark_gb,
            "status": protocol_lane,
        },
        "manual": {
            "usage_bytes": snapshot.manual_usage_bytes,
            "projected_usage_bytes": snapshot.manual_projected_usage_bytes,
            "cap_gb": settings.manual_disk_cap_gb,
            "high_water_gb": settings.manual_admission_high_water_mark_gb,
            "status": manual_lane,
        },
        "shared": {
            "managed_usage_bytes": snapshot.managed_usage_bytes,
            "free_host_disk_bytes": snapshot.free_host_disk_bytes,
            "host_disk_check_path": settings.host_disk_check_path,
            "disk_reserve_gb": settings.disk_reserve_gb,
            "minimum_free_host_disk_gb": settings.minimum_free_host_disk_gb,
        },
    }


def _agent_recommendations(db: Session) -> list[dict[str, str]]:
    recs: list[dict[str, str]] = []
    settings = resolve_settings(db)
    controller = ControllerService(settings, qb=QBittorrentClient(settings))
    snapshot = controller.snapshot_model(db)
    manual_lane = controller.lane_status(db, candidate_is_manual=True)
    integrations = _integration_status(db)
    if snapshot.emergency_mode:
        recs.append(
            {
                "severity": "critical",
                "message": (
                    "Emergency mode is active. Resolve active critical alerts "
                    "before admitting new torrents."
                ),
            }
        )
    if integrations["autobrr"]["enabled"] and not integrations["autobrr"].get("last_event_at"):
        recs.append(
            {
                "severity": "warning",
                "message": "autobrr is enabled but no intake events have been recorded yet.",
            }
        )
    if settings.radarr_enabled or settings.sonarr_enabled:
        wanted_count = db.query(WantedItem).count()
        if wanted_count == 0:
            recs.append(
                {
                    "severity": "info",
                    "message": (
                        "Wanted-set integrations are enabled but no monitored "
                        "items are currently synced."
                    ),
                }
            )
    pending = (
        db.query(ManualRequest)
        .filter(ManualRequest.status.in_(["awaiting_execution", "pending", "planned"]))
        .count()
    )
    if pending:
        recs.append(
            {
                "severity": "info",
                "message": f"There are {pending} manual requests awaiting execution.",
            }
        )
    if snapshot.disk_pressure >= 0.8:
        recs.append(
            {
                "severity": "warning",
                "message": (
                    "Disk pressure is elevated. Manual requests may be rejected "
                    "until managed usage drops."
                ),
            }
        )
    if (
        "managed_cap_or_reserve_violation" in snapshot.reasons.get("reasons", [])
        and not manual_lane["reject_new_admits"]
    ):
        recs.append(
            {
                "severity": "info",
                "message": (
                    "Protocol intake is blocked, but the manual lane is still open. "
                    "Agent-driven manual requests can continue if they fit the manual pool."
                ),
            }
        )
    if not recs:
        recs.append({"severity": "ok", "message": "System posture is healthy."})
    return recs


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/settings")
def api_settings(_: Annotated[str, Depends(auth)], db: Session = Depends(get_db)) -> dict[str, Any]:
    settings_service = SettingsService(db)
    valid, message = settings_service.validate_host_path(settings_service.resolve())
    return {
        "settings": settings_service.masked(),
        "host_path_validation": {"ok": valid, "message": message},
        "integrations": _integration_status(db),
    }


@app.put("/api/settings")
async def update_settings(
    request: Request,
    _: Annotated[str, Depends(auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    payload = await request.json()
    settings_service = SettingsService(db)
    result = settings_service.update(payload)
    app.state.scheduler_manager._apply(result.settings)
    valid, message = settings_service.validate_host_path(result.settings)
    db.commit()
    return {
        "changed_keys": result.changed_keys,
        "settings": settings_service.masked(),
        "host_path_validation": {"ok": valid, "message": message},
    }


@app.post("/api/settings/test-qb")
def test_qb(_: Annotated[str, Depends(auth)], db: Session = Depends(get_db)) -> JSONResponse:
    settings = resolve_settings(db)
    return _recorded_test_result(
        db,
        "qbit",
        True,
        ConnectivityService(settings).test_qb(),
    )


@app.post("/api/settings/test-rss")
def test_rss(_: Annotated[str, Depends(auth)], db: Session = Depends(get_db)) -> JSONResponse:
    settings = resolve_settings(db)
    result = ConnectivityService(settings).test_rss()
    if result.ok:
        result.payload["preview_count"] = len(import_rss(settings))
    return _recorded_test_result(db, "rss", settings.rss_enabled, result)


@app.post("/api/settings/test-autobrr")
def test_autobrr(_: Annotated[str, Depends(auth)], db: Session = Depends(get_db)) -> JSONResponse:
    settings = resolve_settings(db)
    return _recorded_test_result(
        db,
        "autobrr",
        settings.autobrr_enabled,
        ConnectivityService(settings).test_autobrr(),
    )


@app.post("/api/settings/test-radarr")
def test_radarr(_: Annotated[str, Depends(auth)], db: Session = Depends(get_db)) -> JSONResponse:
    settings = resolve_settings(db)
    return _recorded_test_result(
        db,
        "radarr",
        settings.radarr_enabled,
        ConnectivityService(settings).test_radarr(),
    )


@app.post("/api/settings/test-sonarr")
def test_sonarr(_: Annotated[str, Depends(auth)], db: Session = Depends(get_db)) -> JSONResponse:
    settings = resolve_settings(db)
    return _recorded_test_result(
        db,
        "sonarr",
        settings.sonarr_enabled,
        ConnectivityService(settings).test_sonarr(),
    )


@app.post("/api/settings/test-prowlarr")
def test_prowlarr(_: Annotated[str, Depends(auth)], db: Session = Depends(get_db)) -> JSONResponse:
    settings = resolve_settings(db)
    return _recorded_test_result(
        db,
        "prowlarr",
        settings.prowlarr_enabled,
        ConnectivityService(settings).test_prowlarr(),
    )


@app.post("/api/settings/test-plex")
def test_plex(_: Annotated[str, Depends(auth)], db: Session = Depends(get_db)) -> JSONResponse:
    settings = resolve_settings(db)
    return _recorded_test_result(
        db,
        "plex",
        settings.plex_enabled,
        ConnectivityService(settings).test_plex(),
    )


@app.post("/api/manual/workbench/preview")
def manual_workbench_preview(
    payload: ManualFulfillPayload,
    _: Annotated[str, Depends(auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = ArrRequestService(db, resolve_settings(db))
    plan, candidates = service.preview_request(payload)
    return {
        "plan": plan.model_dump(mode="json"),
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
    }


@app.post("/api/manual/workbench/fulfill")
def manual_workbench_fulfill(
    payload: ManualFulfillPayload,
    _: Annotated[str, Depends(auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = ArrRequestService(db, resolve_settings(db)).fulfill_request(payload, actor="dashboard")
    db.commit()
    request = db.query(ManualRequest).filter(ManualRequest.id == result.request["id"]).one()
    return {
        "request": _manual_request_dict_with_context(db, request),
        "plan": result.plan,
        "selected_candidate": result.selected_candidate,
        "candidates_considered": result.candidates_considered,
        "message": result.message,
    }


@app.get("/api/agent/overview")
def agent_overview(
    _: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = resolve_settings(db)
    controller = ControllerService(settings, qb=QBittorrentClient(settings))
    snapshot = controller.snapshot_model(db)
    latest_decisions = (
        db.query(Decision, ReleaseCandidate)
        .join(ReleaseCandidate)
        .order_by(desc(Decision.created_at))
        .limit(5)
        .all()
    )
    return {
        "snapshot": {
            "managed_usage_bytes": snapshot.managed_usage_bytes,
            "protocol_usage_bytes": snapshot.protocol_usage_bytes,
            "protocol_projected_usage_bytes": snapshot.protocol_projected_usage_bytes,
            "manual_usage_bytes": snapshot.manual_usage_bytes,
            "manual_projected_usage_bytes": snapshot.manual_projected_usage_bytes,
            "protocol_cap_gb": settings.managed_disk_cap_gb,
            "manual_cap_gb": settings.manual_disk_cap_gb,
            "free_host_disk_bytes": snapshot.free_host_disk_bytes,
            "unresolved_must_keep": snapshot.unresolved_must_keep,
            "hot_count": snapshot.hot_count,
            "safe_anchor_count": snapshot.safe_anchor_count,
            "emergency_mode": snapshot.emergency_mode,
            "final_threshold": snapshot.final_threshold,
            "reasons": snapshot.reasons,
        },
        "lane_status": {
            "protocol": controller.lane_status(db, candidate_is_manual=False),
            "manual": controller.lane_status(db, candidate_is_manual=True),
        },
        "integrations": _integration_status(db),
        "source_counts": _source_counts(db),
        "recommendations": _agent_recommendations(db),
        "recent_decisions": [
            {
                "title": candidate.title,
                "action": decision.action,
                "score": decision.score,
                "reason": decision.rejection_reason,
                "source": candidate.source,
                "created_at": decision.created_at.isoformat(),
            }
            for decision, candidate in latest_decisions
        ],
    }


@app.get("/api/agent/integrations")
def agent_integrations(
    _: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _integration_status(db)


@app.get("/api/agent/budget")
def agent_budget(
    _: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _budget_summary(db)


@app.get("/api/agent/manual-preview")
def agent_manual_preview(
    _: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = resolve_settings(db)
    controller = ControllerService(settings, qb=QBittorrentClient(settings))
    snapshot = controller.snapshot_model(db)
    return {
        "manual": controller.lane_status(db, candidate_is_manual=True),
        "shared": {
            "free_host_disk_bytes": snapshot.free_host_disk_bytes,
            "host_disk_check_path": settings.host_disk_check_path,
            "minimum_free_host_disk_gb": settings.minimum_free_host_disk_gb,
            "disk_reserve_gb": settings.disk_reserve_gb,
        },
    }


@app.get("/api/agent/recommendations")
def agent_recommendations(
    _: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> list[dict[str, str]]:
    return _agent_recommendations(db)


@app.get("/api/agent/buckets")
def agent_buckets(
    _: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.query(BucketStats).order_by(BucketStats.sample_count.desc()).all()
    return [_bucket_dict(row) for row in rows]


@app.get("/api/agent/metadata")
def agent_metadata(
    _: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _metadata_summary(db)


@app.get("/api/agent/library-handoffs")
def agent_library_handoffs(
    _: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.query(LibraryHandoff).order_by(desc(LibraryHandoff.created_at)).limit(50).all()
    return [
        {
            "id": row.id,
            "torrent_id": row.torrent_id,
            "manual_request_id": row.manual_request_id,
            "media_type": row.media_type,
            "target": row.target,
            "title": row.title,
            "source_path": row.source_path,
            "section_id": row.section_id,
            "status": row.status,
            "priority_score": row.priority_score,
            "last_error": row.last_error,
            "scan_requested_at": (
                row.scan_requested_at.isoformat() if row.scan_requested_at else None
            ),
            "imported_at": row.imported_at.isoformat() if row.imported_at else None,
            "payload": row.payload,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@app.get("/api/agent/tv-priorities")
def agent_tv_priorities(
    _: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return LibraryHandoffService(db, resolve_settings(db)).tv_priority_frontier()


@app.get("/api/agent/manual-requests")
def agent_manual_requests(
    _: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.query(ManualRequest).order_by(desc(ManualRequest.created_at)).limit(50).all()
    return [_manual_request_dict_with_context(db, row) for row in rows]


@app.post("/api/agent/actions/sync")
def agent_sync(
    actor: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, int]:
    settings = resolve_settings(db)
    controller = ControllerService(settings, qb=QBittorrentClient(settings))
    synced = controller.sync_from_qb(db)
    db.add(
        ControllerEvent(
            event_type="agent_action",
            message="Agent triggered qB sync",
            payload={"actor": actor, "action": "sync", "synced": synced},
        )
    )
    db.commit()
    return {"synced": synced}


@app.post("/api/agent/actions/reconcile")
def agent_reconcile(
    actor: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = resolve_settings(db)
    result = LifecycleReconciler(settings).reconcile(db)
    ControllerService(settings, qb=QBittorrentClient(settings)).record_snapshot(db)
    db.add(
        ControllerEvent(
            event_type="agent_action",
            message="Agent triggered reconcile",
            payload={"actor": actor, "action": "reconcile", "result": result},
        )
    )
    db.commit()
    return result


@app.post("/api/agent/actions/prune-retirable")
def agent_prune_retirable(
    request: Request,
    actor: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = resolve_settings(db)
    delete_files = str(request.query_params.get("delete_files", "true")).lower() not in {
        "0",
        "false",
        "no",
    }
    reconciler = LifecycleReconciler(settings)
    result = reconciler.prune_retirable(
        db,
        qb=QBittorrentClient(settings),
        delete_files=delete_files,
    )
    ControllerService(settings, qb=QBittorrentClient(settings)).record_snapshot(db)
    db.add(
        ControllerEvent(
            event_type="agent_action",
            message="Agent pruned retirable transfers",
            payload={"actor": actor, "action": "prune_retirable", "result": result},
        )
    )
    db.commit()
    return result


@app.post("/api/agent/actions/refresh-wanted")
def agent_refresh_wanted(
    actor: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = resolve_settings(db)
    service = WantedSyncService(db, settings)
    results = service.refresh()
    for name in ("radarr", "sonarr"):
        enabled = getattr(settings, f"{name}_enabled")
        result = results.get(
            name,
            IntegrationResult(ok=False, message=f"{name} integration disabled", payload={}),
        )
        record_integration_result(db, name, enabled, result)
    db.add(
        ControllerEvent(
            event_type="agent_action",
            message="Agent refreshed wanted set",
            payload={"actor": actor, "action": "refresh_wanted"},
        )
    )
    db.commit()
    return {
        key: {"ok": value.ok, "message": value.message, "payload": value.payload}
        for key, value in results.items()
    }


@app.post("/api/agent/actions/test-integrations")
def agent_test_integrations(
    actor: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = resolve_settings(db)
    connectivity = ConnectivityService(settings)
    results = {
        "qbit": connectivity.test_qb(),
        "rss": connectivity.test_rss(),
        "autobrr": connectivity.test_autobrr(),
        "radarr": connectivity.test_radarr(),
        "sonarr": connectivity.test_sonarr(),
        "prowlarr": connectivity.test_prowlarr(),
        "plex": connectivity.test_plex(),
    }
    for name, result in results.items():
        enabled = getattr(settings, f"{name}_enabled", True)
        record_integration_result(db, name, enabled, result)
    db.add(
        ControllerEvent(
            event_type="agent_action",
            message="Agent tested integrations",
            payload={"actor": actor, "action": "test_integrations"},
        )
    )
    db.commit()
    return {
        key: {"ok": value.ok, "message": value.message, "payload": value.payload}
        for key, value in results.items()
    }


@app.post("/api/agent/actions/process-library")
def agent_process_library(
    actor: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = resolve_settings(db)
    result = LibraryHandoffService(db, settings).process_pending()
    record_integration_result(db, "plex", settings.plex_enabled, result)
    db.add(
        ControllerEvent(
            event_type="agent_action",
            message="Agent processed library handoffs",
            payload={"actor": actor, "action": "process_library", "result": result.payload},
        )
    )
    db.commit()
    return {"ok": result.ok, "message": result.message, "payload": result.payload}


@app.post("/api/agent/handoffs/{handoff_id}/retry")
def agent_retry_library_handoff(
    handoff_id: int,
    actor: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = resolve_settings(db)
    service = LibraryHandoffService(db, settings)
    handoff, result = service.retry_handoff(handoff_id)
    if handoff is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=result.message)
    if result.message in {
        "completed library handoff cannot be retried",
        "only failed library handoffs can be retried",
    }:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result.message)
    record_integration_result(db, "plex", settings.plex_enabled, result)
    db.add(
        ControllerEvent(
            event_type="agent_action",
            message="Agent retried library handoff",
            payload={
                "actor": actor,
                "action": "retry_library_handoff",
                "handoff_id": handoff_id,
                "result": result.payload,
            },
        )
    )
    db.commit()
    return {
        "ok": result.ok,
        "message": result.message,
        "payload": {
            **result.payload,
            "handoff_id": handoff.id,
            "status": handoff.status,
            "last_error": handoff.last_error,
        },
    }


@app.post("/api/agent/requests")
def agent_create_request(
    payload: ManualRequestPayload,
    actor: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = ArrRequestService(db, resolve_settings(db)).create_request(payload, actor=actor)
    db.commit()
    request = db.query(ManualRequest).filter(ManualRequest.id == result.request_id).one()
    return {"request": _manual_request_dict_with_context(db, request), "message": result.message}


@app.post("/api/agent/fulfill")
def agent_fulfill_request(
    payload: ManualFulfillPayload,
    actor: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = ArrRequestService(db, resolve_settings(db)).fulfill_request(payload, actor=actor)
    db.commit()
    request_id = result.request["id"]
    request = db.query(ManualRequest).filter(ManualRequest.id == request_id).one()
    return {
        "request": _manual_request_dict_with_context(db, request),
        "plan": result.plan,
        "selected_candidate": result.selected_candidate,
        "candidates_considered": result.candidates_considered,
        "message": result.message,
    }


@app.get("/api/agent/requests/{request_id}")
def agent_get_request(
    request_id: int,
    _: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.query(ManualRequest).filter(ManualRequest.id == request_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="request not found")
    return {"request": _manual_request_dict_with_context(db, row)}


@app.get("/api/agent/requests/{request_id}/plan")
def agent_plan_request(
    request_id: int,
    _: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.query(ManualRequest).filter(ManualRequest.id == request_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="request not found")
    plan = ArrRequestService(db, resolve_settings(db)).plan_request(row)
    return {
        "request": _manual_request_dict_with_context(db, row),
        "plan": plan.model_dump(mode="json"),
    }


@app.get("/api/agent/requests/{request_id}/candidates")
def agent_request_candidates(
    request_id: int,
    _: Annotated[str, Depends(agent_auth)],
    limit: int = 5,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.query(ManualRequest).filter(ManualRequest.id == request_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="request not found")
    results = ArrRequestService(db, resolve_settings(db)).candidate_preview(row, limit=limit)
    return {
        "request": _manual_request_dict_with_context(db, row),
        "candidates": [result.model_dump(mode="json") for result in results],
    }


@app.post("/api/agent/requests/{request_id}/execute")
def agent_execute_request(
    request_id: int,
    actor: Annotated[str, Depends(agent_auth)],
    allow_arr_fallback: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.query(ManualRequest).filter(ManualRequest.id == request_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="request not found")
    result = ArrRequestService(db, resolve_settings(db)).execute_request(
        row,
        actor=actor,
        allow_arr_fallback=allow_arr_fallback,
    )
    db.commit()
    db.refresh(row)
    return {"request": _manual_request_dict_with_context(db, row), "message": result.message}


@app.post("/api/agent/requests/{request_id}/select-candidate")
def agent_select_request_candidate(
    request_id: int,
    payload: ManualCandidateSelectionPayload,
    actor: Annotated[str, Depends(agent_auth)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.query(ManualRequest).filter(ManualRequest.id == request_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="request not found")
    result = ArrRequestService(db, resolve_settings(db)).submit_selected_candidate(
        row,
        payload,
        actor=actor,
    )
    db.commit()
    db.refresh(row)
    return {"request": _manual_request_dict_with_context(db, row), "message": result.message}


@app.post("/api/autobrr/intake")
async def autobrr_intake(
    request: Request,
    x_transferops_signature: str | None = Header(default=None),
    controller: ControllerService = Depends(get_controller),
    db: Session = Depends(get_db),
) -> dict:
    settings = controller.settings
    raw = await request.body()
    if not controller.verify_autobrr(raw, settings.autobrr_shared_secret, x_transferops_signature):
        record_integration_result(
            db,
            "autobrr",
            settings.autobrr_enabled,
            IntegrationResult(ok=False, message="invalid shared secret or signature", payload={}),
        )
        db.commit()
        raise HTTPException(status_code=401, detail="invalid shared secret or signature")
    payload = await request.json()
    payload["source"] = "autobrr"
    candidate = controller.normalize_candidate(payload)
    response = controller.intake_candidate(db, candidate)
    record_integration_result(
        db,
        "autobrr",
        settings.autobrr_enabled,
        IntegrationResult(
            ok=True,
            message="autobrr intake processed",
            payload={"candidate_id": response.candidate_id, "action": response.action},
        ),
    )
    db.commit()
    return response.model_dump()


@app.post("/api/radarr/webhook")
async def radarr_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    payload = await request.json()
    settings = resolve_settings(db)
    service = WantedSyncService(db, settings)
    movie = payload.get("movie", {})
    title = movie.get("title") or payload.get("title")
    event_type = str(payload.get("eventType") or payload.get("eventTypeName") or "").lower()
    external_id = str(movie.get("tmdbId") or "")
    if title:
        if "delete" in event_type or movie.get("monitored") is False:
            service.delete_item(
                "radarr",
                title=title,
                year=movie.get("year"),
                external_id=external_id,
            )
        elif "test" not in event_type:
            service._upsert(
                source="radarr",
                item_type="movie",
                title=title,
                year=movie.get("year"),
                external_id=external_id,
                reason="radarr_webhook",
                raw_payload=payload,
            )
    record_integration_result(
        db,
        "radarr",
        settings.radarr_enabled,
        IntegrationResult(
            ok=True,
            message="Radarr webhook processed",
            payload={"event_type": event_type},
        ),
    )
    db.commit()
    return {"status": "ok"}


@app.post("/api/sonarr/webhook")
async def sonarr_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    payload = await request.json()
    settings = resolve_settings(db)
    service = WantedSyncService(db, settings)
    series = payload.get("series", {})
    title = series.get("title") or payload.get("title")
    event_type = str(payload.get("eventType") or payload.get("eventTypeName") or "").lower()
    external_id = str(series.get("tvdbId") or "")
    if title:
        if "delete" in event_type or series.get("monitored") is False:
            service.delete_item(
                "sonarr",
                title=title,
                year=series.get("year"),
                external_id=external_id,
            )
        elif "test" not in event_type:
            service._upsert(
                source="sonarr",
                item_type="series",
                title=title,
                year=series.get("year"),
                external_id=external_id,
                reason="sonarr_webhook",
                raw_payload=payload,
            )
    record_integration_result(
        db,
        "sonarr",
        settings.sonarr_enabled,
        IntegrationResult(
            ok=True,
            message="Sonarr webhook processed",
            payload={"event_type": event_type},
        ),
    )
    db.commit()
    return {"status": "ok"}


@app.post("/api/rss/import")
def rss_import(
    _: Annotated[str, Depends(auth)],
    db: Session = Depends(get_db),
) -> dict[str, int]:
    settings = resolve_settings(db)
    controller = ControllerService(settings, qb=QBittorrentClient(settings))
    imported = 0
    try:
        for payload in import_rss(settings):
            controller.intake_candidate(db, payload)
            imported += 1
        record_integration_result(
            db,
            "rss",
            settings.rss_enabled,
            IntegrationResult(
                ok=True,
                message="RSS import complete",
                payload={"imported": imported},
            ),
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        record_integration_result(
            db,
            "rss",
            settings.rss_enabled,
            IntegrationResult(ok=False, message=str(exc), payload={}),
        )
        db.commit()
        raise
    return {"imported": imported}


@app.post("/api/wanted/refresh")
def wanted_refresh(
    _: Annotated[str, Depends(auth)], db: Session = Depends(get_db)
) -> dict[str, Any]:
    settings = resolve_settings(db)
    service = WantedSyncService(db, settings)
    results = service.refresh()
    for name in ("radarr", "sonarr"):
        enabled = getattr(settings, f"{name}_enabled")
        result = results.get(
            name,
            IntegrationResult(ok=False, message=f"{name} integration disabled", payload={}),
        )
        record_integration_result(db, name, enabled, result)
    db.commit()
    return {
        key: {"ok": value.ok, "message": value.message, "payload": value.payload}
        for key, value in results.items()
    }


@app.post("/api/reconcile")
def reconcile(_: Annotated[str, Depends(auth)], db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = resolve_settings(db)
    result = LifecycleReconciler(settings).reconcile(db)
    ControllerService(settings, qb=QBittorrentClient(settings)).record_snapshot(db)
    db.commit()
    return result


@app.post("/api/sync")
def sync(_: Annotated[str, Depends(auth)], db: Session = Depends(get_db)) -> dict[str, int]:
    settings = resolve_settings(db)
    controller = ControllerService(settings, qb=QBittorrentClient(settings))
    synced = controller.sync_from_qb(db)
    LifecycleReconciler(settings).reconcile(db)
    db.commit()
    return {"synced": synced}


@app.get("/api/status")
def api_status(_: Annotated[str, Depends(auth)], db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = resolve_settings(db)
    controller = ControllerService(settings, qb=QBittorrentClient(settings))
    snapshot = controller.snapshot_model(db)
    protocol_lane = controller.lane_status(db, candidate_is_manual=False)
    manual_lane = controller.lane_status(db, candidate_is_manual=True)
    wanted = db.query(WantedItem).order_by(desc(WantedItem.updated_at)).limit(50).all()
    return {
        "snapshot": {
            "managed_usage_bytes": snapshot.managed_usage_bytes,
            "projected_usage_bytes": snapshot.projected_usage_bytes,
            "protocol_usage_bytes": snapshot.protocol_usage_bytes,
            "protocol_projected_usage_bytes": snapshot.protocol_projected_usage_bytes,
            "manual_usage_bytes": snapshot.manual_usage_bytes,
            "manual_projected_usage_bytes": snapshot.manual_projected_usage_bytes,
            "free_host_disk_bytes": snapshot.free_host_disk_bytes,
            "unresolved_must_keep": snapshot.unresolved_must_keep,
            "hot_count": snapshot.hot_count,
            "safe_anchor_count": snapshot.safe_anchor_count,
            "emergency_mode": snapshot.emergency_mode,
            "final_threshold": snapshot.final_threshold,
            "reasons": snapshot.reasons,
        },
        "lane_status": {
            "protocol": protocol_lane,
            "manual": manual_lane,
        },
        "integrations": _integration_status(db),
        "source_counts": _source_counts(db),
        "wanted_count": db.query(WantedItem).count(),
        "wanted_items": [
            {"source": row.source, "title": row.title, "year": row.year, "reason": row.reason}
            for row in wanted
        ],
        "alerts": [
            {
                "alert_type": alert.alert_type,
                "severity": alert.severity,
                "message": alert.message,
            }
            for alert in (
                db.query(Alert)
                .filter(Alert.active.is_(True))
                .order_by(desc(Alert.created_at))
                .limit(20)
            )
        ],
    }


@app.get("/api/status/manual-preview")
def api_status_manual_preview(
    _: Annotated[str, Depends(auth)], db: Session = Depends(get_db)
) -> dict[str, Any]:
    settings = resolve_settings(db)
    controller = ControllerService(settings, qb=QBittorrentClient(settings))
    snapshot = controller.snapshot_model(db)
    return {
        "manual": controller.lane_status(db, candidate_is_manual=True),
        "shared": {
            "free_host_disk_bytes": snapshot.free_host_disk_bytes,
            "host_disk_check_path": settings.host_disk_check_path,
            "minimum_free_host_disk_gb": settings.minimum_free_host_disk_gb,
            "disk_reserve_gb": settings.disk_reserve_gb,
        },
    }


@app.get("/api/decisions")
def api_decisions(
    _: Annotated[str, Depends(auth)], db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = (
        db.query(Decision, ReleaseCandidate)
        .join(ReleaseCandidate)
        .order_by(desc(Decision.created_at))
        .limit(50)
    )
    return [
        {
            "title": candidate.title,
            "size_bytes": candidate.size_bytes,
            "freeleech": candidate.freeleech,
            "source": candidate.source,
            "wanted": candidate.wanted,
            "wanted_reason": candidate.wanted_reason,
            "bucket_key": decision.bucket_key,
            "score": decision.score,
            "threshold": decision.threshold,
            "action": decision.action,
            "reason": decision.rejection_reason,
            "components": decision.utility_components,
            "created_at": decision.created_at.isoformat(),
        }
        for decision, candidate in rows
    ]


@app.get("/api/torrents")
def api_torrents(
    _: Annotated[str, Depends(auth)], db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = (
        db.query(Torrent)
        .filter(Torrent.managed.is_(True))
        .order_by(Torrent.updated_at.desc())
        .limit(200)
    )
    return [
        {
            "title": torrent.title,
            "state": torrent.state,
            "size_bytes": torrent.size_bytes,
            "uploaded_bytes": torrent.uploaded_bytes,
            "ratio": torrent.ratio,
            "seed_time_seconds": torrent.seed_time_seconds,
            "last_activity_at": torrent.last_activity_at.isoformat()
            if torrent.last_activity_at
            else None,
            "tracker_safe_estimate_hours": torrent.tracker_safe_estimate_hours,
            "tags": torrent.tags,
            "category": torrent.category,
            "save_path": torrent.save_path,
        }
        for torrent in rows
    ]


@app.get("/api/buckets")
def api_buckets(
    _: Annotated[str, Depends(auth)], db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.query(BucketStats).order_by(BucketStats.sample_count.desc()).all()
    return [_bucket_dict(row) for row in rows]


@app.get("/metrics")
def metrics(db: Session = Depends(get_db)) -> Response:
    settings = resolve_settings(db)
    controller = ControllerService(settings, qb=QBittorrentClient(settings))
    snapshot = controller.snapshot_model(db)
    body = "\n".join(
        [
            f"transferops_managed_usage_bytes {snapshot.managed_usage_bytes}",
            f"transferops_protocol_usage_bytes {snapshot.protocol_usage_bytes}",
            f"transferops_manual_usage_bytes {snapshot.manual_usage_bytes}",
            f"transferops_unresolved_must_keep {snapshot.unresolved_must_keep}",
            f"transferops_hot_count {snapshot.hot_count}",
            f"transferops_safe_anchor_count {snapshot.safe_anchor_count}",
            f"transferops_emergency_mode {1 if snapshot.emergency_mode else 0}",
        ]
    )
    return Response(content=body, media_type="text/plain")


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    _: Annotated[str, Depends(auth)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    settings = resolve_settings(db)
    controller = ControllerService(settings, qb=QBittorrentClient(settings))
    snapshot = controller.snapshot_model(db)
    protocol_lane = controller.lane_status(db, candidate_is_manual=False)
    manual_lane = controller.lane_status(db, candidate_is_manual=True)
    decisions = (
        db.query(Decision, ReleaseCandidate)
        .join(ReleaseCandidate)
        .order_by(desc(Decision.created_at))
        .limit(20)
    )
    torrents = (
        db.query(Torrent)
        .filter(Torrent.managed.is_(True))
        .order_by(Torrent.updated_at.desc())
        .limit(50)
        .all()
    )
    buckets = db.query(BucketStats).order_by(BucketStats.sample_count.desc()).limit(20).all()
    alerts = (
        db.query(Alert).filter(Alert.active.is_(True)).order_by(desc(Alert.created_at)).limit(10)
    ).all()
    wanted_items = db.query(WantedItem).order_by(desc(WantedItem.updated_at)).limit(20).all()
    manual_request_rows = (
        db.query(ManualRequest).order_by(desc(ManualRequest.created_at)).limit(20).all()
    )
    manual_requests = [_manual_request_dict_with_context(db, row) for row in manual_request_rows]
    library_handoffs = (
        db.query(LibraryHandoff).order_by(desc(LibraryHandoff.created_at)).limit(20).all()
    )
    tv_priorities = LibraryHandoffService(db, settings).tv_priority_frontier(limit=10)
    recent_upload = sum(t.uploaded_bytes for t in torrents) / BYTES_PER_GB
    today_cutoff = (
        datetime.now(UTC).replace(tzinfo=None).replace(hour=0, minute=0, second=0, microsecond=0)
    )
    admit_count = (
        db.query(Decision)
        .filter(Decision.action.in_(["admit", "dry_run"]), Decision.created_at >= today_cutoff)
        .count()
    )
    reject_count = (
        db.query(Decision)
        .filter(Decision.action == "reject", Decision.created_at >= today_cutoff)
        .count()
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "snapshot": snapshot,
            "decisions": decisions,
            "torrents": torrents,
            "buckets": buckets,
            "alerts": alerts,
            "wanted_items": wanted_items,
            "manual_requests": manual_requests,
            "library_handoffs": library_handoffs,
            "tv_priorities": tv_priorities,
            "integrations": _integration_status(db),
            "settings": _masked_settings(db),
            "source_counts": _source_counts(db),
            "recent_upload_gb": round(recent_upload, 2),
            "admit_count": admit_count,
            "reject_count": reject_count,
            "managed_cap_gb": settings.managed_disk_cap_gb,
            "manual_cap_gb": settings.manual_disk_cap_gb,
            "protocol_lane": protocol_lane,
            "manual_lane": manual_lane,
        },
    )
