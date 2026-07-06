from __future__ import annotations

import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi.encoders import jsonable_encoder
from sqlalchemy import desc, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Alert,
    ControllerEvent,
    Decision,
    DecisionAction,
    ExecutorState,
    InboundEvent,
    ManualRequest,
    Observation,
    ReleaseCandidate,
    SystemSnapshot,
    Torrent,
    TorrentState,
)
from app.services.disk import DiskBudgetManager
from app.services.integrations import extract_year, match_wanted_items, normalize_title
from app.services.learning import BucketLearner
from app.services.library import LibraryHandoffService
from app.services.logging import logger
from app.services.metadata import MetadataResolver
from app.services.qbittorrent import QBittorrentClient
from app.services.rss import canonicalize_download_url, extract_ipt_torrent_id
from app.services.schemas import CandidatePayload, IntakeResponse
from app.services.scope import is_managed_qb_torrent
from app.services.scoring import PressureState, Scorer

LIFECYCLE_TAGS = {
    "transferops.hot",
    "transferops.mustkeep",
    "transferops.safe",
    "transferops.anchor",
    "transferops.retirable",
    "transferops.error",
}
STATE_TAGS = {
    TorrentState.candidate.value: [],
    TorrentState.hot.value: ["transferops.hot"],
    TorrentState.must_keep.value: ["transferops.mustkeep"],
    TorrentState.safe_anchor.value: ["transferops.safe", "transferops.anchor"],
    TorrentState.retirable.value: ["transferops.retirable"],
    TorrentState.error.value: ["transferops.error"],
}


class ControllerService:
    def __init__(self, settings: Settings, qb: QBittorrentClient | None = None) -> None:
        self.settings = settings
        self.qb = qb or QBittorrentClient(settings)
        self.disk = DiskBudgetManager(settings)
        self.learner = BucketLearner(settings)
        self.metadata = MetadataResolver(settings)
        self.scorer = Scorer(settings)

    def verify_autobrr(
        self,
        raw_body: bytes,
        shared_secret: str | None,
        signature: str | None,
    ) -> bool:
        if not shared_secret:
            return True
        if self.settings.autobrr_require_signature:
            if not signature:
                return False
            digest = hmac.new(shared_secret.encode(), raw_body, sha256).hexdigest()
            return hmac.compare_digest(digest, signature)
        try:
            parsed = json.loads(raw_body.decode() or "{}")
        except json.JSONDecodeError:
            return False
        provided = parsed.get("secret") or parsed.get("shared_secret")
        return secrets.compare_digest(str(provided), shared_secret)

    def normalize_candidate(self, payload: dict) -> CandidatePayload:
        source = payload.get("source") or "autobrr"
        title = payload.get("title") or payload.get("releaseName") or "unknown"
        tracker = payload.get("indexer") or payload.get("tracker") or "unknown"
        download_url = (
            payload.get("downloadUrl") or payload.get("torrentUrl") or payload.get("link")
        )
        data = {
            "title": title,
            "guid": payload.get("guid") or payload.get("release_id") or payload.get("id"),
            "tracker": tracker,
            "category": payload.get("category") or payload.get("type") or "other",
            "release_year": (
                payload.get("release_year") or payload.get("year") or extract_year(title)
            ),
            "size_bytes": int(payload.get("size_bytes") or payload.get("size") or 0),
            "freeleech": bool(payload.get("freeleech") or payload.get("isFreeleech") or False),
            "seeders": payload.get("seeders"),
            "leechers": payload.get("leechers"),
            "download_url": canonicalize_download_url(self.settings, tracker, download_url, title),
            "info_hash": payload.get("infoHash") or payload.get("info_hash"),
            "source": source,
            "source_confidence": self._source_confidence(source),
            "exclude_from_learning": bool(
                payload.get("exclude_from_learning")
                or source in {"manual", "manual_request"}
            ),
            "wanted": bool(payload.get("wanted") or False),
            "wanted_reason": payload.get("wanted_reason"),
            "raw_payload": jsonable_encoder(payload),
            "published_at": payload.get("published_at"),
        }
        if payload.get("pubDate"):
            data["published_at"] = datetime.fromisoformat(
                payload["pubDate"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
        return CandidatePayload.model_validate(data)

    def _source_confidence(self, source: str) -> float:
        priorities = {
            "autobrr": 1.0,
            "rss": 0.65,
            "radarr_signal": 0.7,
            "sonarr_signal": 0.7,
            "prowlarr_backfill": 0.55,
            "manual": 0.75,
            "manual_request": 0.8,
        }
        return priorities.get(source, 0.5)

    def _source_priority(self, source: str) -> int:
        priorities = {
            "autobrr": self.settings.autobrr_source_priority,
            "manual": 80,
            "manual_request": 85,
            "rss": 60,
            "radarr_signal": 45,
            "sonarr_signal": 45,
            "prowlarr_backfill": 40,
        }
        return priorities.get(source, 30)

    def _normalized_url(self, value: str) -> str:
        parsed = urlparse(value)
        query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        return urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path,
                parsed.params,
                query,
                parsed.fragment,
            )
        )

    def _candidate_dedupe_key(self, candidate: CandidatePayload) -> str:
        torrent_id = extract_ipt_torrent_id(candidate.download_url or candidate.guid)
        tracker = (candidate.tracker or "").lower()
        if candidate.guid and candidate.guid.startswith("provider-"):
            torrent_id = candidate.guid.split("-", 1)[1]
        if torrent_id and (tracker == "demo" or "demo" in tracker):
            return f"provider:{torrent_id}"
        if candidate.info_hash:
            return f"hash:{candidate.info_hash.lower()}"
        if candidate.guid:
            return f"guid:{candidate.guid}"
        if candidate.download_url:
            return f"url:{self._normalized_url(candidate.download_url)}"
        published = candidate.published_at.isoformat() if candidate.published_at else ""
        return f"title:{normalize_title(candidate.title)}:{published}"

    def _record_inbound_event(
        self,
        db: Session,
        source: str,
        dedupe_key: str,
        title: str,
        status: str,
        payload: dict,
        candidate_id: int | None = None,
        message: str | None = None,
    ) -> None:
        db.add(
            InboundEvent(
                source=source,
                dedupe_key=dedupe_key,
                title=title,
                status=status,
                candidate_id=candidate_id,
                message=message,
                payload=payload,
            )
        )

    def _apply_wanted_signal(self, db: Session, candidate: ReleaseCandidate) -> None:
        if candidate.wanted:
            return
        matches = match_wanted_items(db, candidate.title)
        if not matches:
            return
        candidate.wanted = True
        candidate.wanted_reason = "; ".join(f"{row.source}:{row.title}" for row in matches[:3])

    def _merge_candidate(
        self,
        db: Session,
        existing: ReleaseCandidate,
        candidate: CandidatePayload,
        dedupe_key: str,
    ) -> bool:
        incoming_rank = self._source_priority(candidate.source)
        existing_rank = self._source_priority(existing.source)
        changed = False
        if incoming_rank > existing_rank:
            existing.source = candidate.source
            changed = True
        incoming_confidence = candidate.source_confidence or 0.0
        if incoming_confidence > existing.source_confidence:
            existing.source_confidence = incoming_confidence
            changed = True
        if candidate.exclude_from_learning and not existing.exclude_from_learning:
            existing.exclude_from_learning = True
            changed = True
        for attr in ("tracker", "category", "download_url", "info_hash", "wanted_reason"):
            incoming = getattr(candidate, attr)
            current = getattr(existing, attr)
            if not incoming:
                continue
            if not current or incoming_rank >= existing_rank:
                if current != incoming:
                    setattr(existing, attr, incoming)
                    changed = True
        if candidate.size_bytes and (existing.size_bytes <= 0 or incoming_rank >= existing_rank):
            if existing.size_bytes != candidate.size_bytes:
                existing.size_bytes = candidate.size_bytes
                changed = True
        for attr in ("seeders", "leechers", "published_at"):
            incoming = getattr(candidate, attr)
            current = getattr(existing, attr)
            if incoming is None:
                continue
            if current is None or incoming_rank >= existing_rank:
                if current != incoming:
                    setattr(existing, attr, incoming)
                    changed = True
        if candidate.freeleech and not existing.freeleech:
            existing.freeleech = True
            changed = True
        if candidate.wanted and not existing.wanted:
            existing.wanted = True
            changed = True
        existing.dedupe_key = dedupe_key
        if candidate.raw_payload:
            merged_payload = dict(existing.raw_payload or {})
            merged_payload.update(candidate.raw_payload)
            if merged_payload != (existing.raw_payload or {}):
                existing.raw_payload = merged_payload
                changed = True
        self._apply_wanted_signal(db, existing)
        return changed

    def _latest_decision(self, db: Session, candidate_id: int) -> Decision | None:
        return (
            db.query(Decision)
            .filter(Decision.candidate_id == candidate_id)
            .order_by(desc(Decision.created_at), desc(Decision.id))
            .first()
        )

    def _find_existing_candidate(
        self,
        db: Session,
        candidate: CandidatePayload,
        dedupe_key: str,
    ) -> ReleaseCandidate | None:
        clauses = [ReleaseCandidate.dedupe_key == dedupe_key]
        if candidate.guid:
            clauses.append(ReleaseCandidate.guid == candidate.guid)
        if candidate.info_hash:
            clauses.append(ReleaseCandidate.info_hash == candidate.info_hash)
        if candidate.download_url:
            clauses.append(ReleaseCandidate.download_url == candidate.download_url)
        return (
            db.query(ReleaseCandidate)
            .filter(or_(*clauses))
            .order_by(desc(ReleaseCandidate.id))
            .first()
        )

    def _persist_candidate(
        self,
        db: Session,
        candidate: CandidatePayload,
    ) -> tuple[ReleaseCandidate, bool]:
        dedupe_key = self._candidate_dedupe_key(candidate)
        existing = self._find_existing_candidate(db, candidate, dedupe_key)
        if existing:
            should_evaluate = self._merge_candidate(db, existing, candidate, dedupe_key)
            self._record_inbound_event(
                db,
                source=candidate.source,
                dedupe_key=dedupe_key,
                title=candidate.title,
                status="updated" if should_evaluate else "duplicate_ignored",
                payload=candidate.raw_payload,
                candidate_id=existing.id,
                message="candidate deduplicated",
            )
            db.add(existing)
            db.flush()
            return existing, should_evaluate or self._latest_decision(db, existing.id) is None

        release = ReleaseCandidate(**candidate.model_dump(), dedupe_key=dedupe_key)
        self._apply_wanted_signal(db, release)
        db.add(release)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            existing = self._find_existing_candidate(db, candidate, dedupe_key)
            if existing is None:
                raise
            should_evaluate = self._merge_candidate(db, existing, candidate, dedupe_key)
            self._record_inbound_event(
                db,
                source=candidate.source,
                dedupe_key=dedupe_key,
                title=candidate.title,
                status="updated" if should_evaluate else "duplicate_ignored",
                payload=candidate.raw_payload,
                candidate_id=existing.id,
                message="candidate deduplicated after integrity collision",
            )
            db.add(existing)
            db.flush()
            return existing, should_evaluate or self._latest_decision(db, existing.id) is None
        self._record_inbound_event(
            db,
            source=candidate.source,
            dedupe_key=dedupe_key,
            title=candidate.title,
            status="accepted",
            payload=candidate.raw_payload,
            candidate_id=release.id,
        )
        return release, True

    def _alert_for_hash(self, db: Session, alert_type: str, info_hash: str | None) -> Alert | None:
        for alert in (
            db.query(Alert).filter(Alert.alert_type == alert_type, Alert.active.is_(True)).all()
        ):
            if (alert.payload or {}).get("info_hash") == info_hash:
                return alert
        return None

    def _clear_hash_alert(self, db: Session, alert_type: str, info_hash: str | None) -> None:
        alert = self._alert_for_hash(db, alert_type, info_hash)
        if alert is not None:
            alert.active = False
            db.add(alert)

    def _upsert_hash_alert(
        self,
        db: Session,
        alert_type: str,
        severity: str,
        message: str,
        info_hash: str | None,
        payload: dict,
    ) -> None:
        alert = self._alert_for_hash(db, alert_type, info_hash)
        if alert is None:
            alert = Alert(
                alert_type=alert_type,
                severity=severity,
                message=message,
                payload=payload,
            )
        else:
            alert.severity = severity
            alert.message = message
            alert.payload = payload
            alert.active = True
        db.add(alert)

    def _is_safe(self, torrent: Torrent) -> bool:
        ratio_target = self.settings.tracker_safe_ratio - self.settings.tracker_safe_grace_ratio
        hour_target = self.settings.tracker_safe_hours - self.settings.tracker_safe_grace_hours
        return torrent.ratio >= ratio_target or torrent.seed_time_seconds / 3600 >= hour_target

    def _managed_tags(self, state: str, existing_tags: str = "") -> str:
        tags: list[str] = []
        for tag in existing_tags.split(","):
            normalized = tag.strip()
            if not normalized or normalized in LIFECYCLE_TAGS:
                continue
            if normalized not in tags:
                tags.append(normalized)
        if self.settings.qbit_tag and self.settings.qbit_tag not in tags:
            tags.append(self.settings.qbit_tag)
        for tag in STATE_TAGS.get(state, []):
            if tag not in tags:
                tags.append(tag)
        return ",".join(tags)

    def _heal_qb_scope(self, torrent: Torrent, payload: dict[str, object]) -> None:
        if not torrent.info_hash:
            return
        hash_value = torrent.info_hash
        desired_category = self.settings.qbit_category.strip()
        current_category = str(payload.get("category") or "").strip()
        if desired_category and current_category != desired_category:
            self.qb.set_category(hash_value, desired_category)

        desired_tags = [
            tag for tag in self._managed_tags(torrent.state, torrent.tags).split(",") if tag
        ]
        current_tags = {
            tag.strip() for tag in str(payload.get("tags") or "").split(",") if tag.strip()
        }
        missing_tags = [tag for tag in desired_tags if tag not in current_tags]
        if missing_tags:
            self.qb.set_tags(hash_value, missing_tags)

    def _recent_underperformance_penalty(self, db: Session) -> float:
        stall_window_hours = max(self.settings.stall_window_hours, 1)
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=stall_window_hours)
        torrents = (
            db.query(Torrent)
            .filter(
                Torrent.managed.is_(True),
                Torrent.progress >= 1.0,
                Torrent.safely_seeded_at.is_(None),
            )
            .all()
        )
        eligible = 0
        stalled = 0
        for torrent in torrents:
            latest = (
                db.query(Observation)
                .filter(Observation.torrent_id == torrent.id)
                .order_by(Observation.observed_at.desc())
                .first()
            )
            if latest is None or latest.seed_time_seconds / 3600 < stall_window_hours:
                continue
            baseline = (
                db.query(Observation)
                .filter(
                    Observation.torrent_id == torrent.id,
                    Observation.observed_at
                    <= max(
                        cutoff,
                        latest.observed_at - timedelta(hours=stall_window_hours),
                    ),
                )
                .order_by(Observation.observed_at.desc())
                .first()
            )
            if baseline is None:
                continue
            eligible += 1
            if latest.uploaded_bytes <= baseline.uploaded_bytes:
                stalled += 1
        if eligible == 0:
            return 0.0
        return min(1.5, stalled / eligible)

    def _confirmed_executor_filter(self):
        return or_(
            Torrent.executor_state == ExecutorState.confirmed.value,
            Torrent.executor_state.is_(None),
        )

    def _live_emergency_torrents(self, db: Session) -> list[Torrent]:
        torrents = (
            db.query(Torrent)
            .filter(
                Torrent.managed.is_(True),
                self._confirmed_executor_filter(),
            )
            .all()
        )
        return [
            torrent
            for torrent in torrents
            if not self._is_safe(torrent)
            and (torrent.paused or torrent.state == TorrentState.error.value)
        ]

    def _reconcile_active_alerts(self, db: Session) -> None:
        for alert in db.query(Alert).filter(Alert.active.is_(True)).all():
            if alert.alert_type not in {"managed_torrent_missing", "unsafe_paused"}:
                continue
            payload = alert.payload or {}
            info_hash = payload.get("info_hash")
            if not info_hash:
                alert.active = False
                db.add(alert)
                continue
            torrent = (
                db.query(Torrent)
                .filter(
                    Torrent.info_hash == info_hash,
                    Torrent.managed.is_(True),
                    self._confirmed_executor_filter(),
                )
                .one_or_none()
            )
            if torrent is None or self._is_safe(torrent):
                alert.active = False
                db.add(alert)
                continue
            if (
                alert.alert_type == "managed_torrent_missing"
                and torrent.state != TorrentState.error.value
            ):
                alert.active = False
                db.add(alert)
                continue
            if alert.alert_type == "unsafe_paused" and not torrent.paused:
                alert.active = False
                db.add(alert)

    def _find_confirmed_twin(self, db: Session, torrent: Torrent) -> Torrent | None:
        twins = (
            db.query(Torrent)
            .filter(
                Torrent.id != torrent.id,
                self._confirmed_executor_filter(),
            )
            .all()
        )
        own_normalized = normalize_title(torrent.title)
        for twin in twins:
            if torrent.info_hash and twin.info_hash == torrent.info_hash:
                return twin
            if torrent.candidate_id and twin.candidate_id == torrent.candidate_id:
                return twin
            if own_normalized and normalize_title(twin.title) == own_normalized:
                return twin
        return None

    def _expire_pending_torrents(self, db: Session, now: datetime) -> None:
        pending = (
            db.query(Torrent)
            .filter(
                Torrent.executor_state == ExecutorState.pending.value,
                Torrent.executor_deadline_at.is_not(None),
                Torrent.executor_deadline_at <= now,
            )
            .all()
        )
        for torrent in pending:
            twin = self._find_confirmed_twin(db, torrent)
            torrent.executor_state = (
                ExecutorState.orphaned.value if twin is not None else ExecutorState.failed.value
            )
            torrent.managed = False
            torrent.state = TorrentState.error.value
            torrent.tags = self._managed_tags(TorrentState.error.value, torrent.tags)
            self._clear_hash_alert(db, "managed_torrent_missing", torrent.info_hash)
            db.add(
                ControllerEvent(
                    event_type="executor_placeholder_expired",
                    severity="warning",
                    message=f"Executor placeholder expired for {torrent.title}",
                    payload={
                        "torrent_id": torrent.id,
                        "info_hash": torrent.info_hash,
                        "executor_state": torrent.executor_state,
                        "twin_id": twin.id if twin is not None else None,
                    },
                )
            )

    def _current_pressure(
        self,
        db: Session,
        candidate_size_bytes: int,
        candidate_is_manual: bool = False,
    ) -> PressureState:
        self._reconcile_active_alerts(db)
        disk = self.disk.snapshot(
            db,
            candidate_size_bytes=candidate_size_bytes,
            candidate_is_manual=candidate_is_manual,
        )
        unresolved = self._unresolved_count(db, candidate_is_manual=candidate_is_manual)
        soft_cap, hard_cap, debt_budget = self._pressure_limits(candidate_is_manual)
        reasons = list(disk.reasons)
        unresolved_pressure = unresolved / max(1, soft_cap)
        reject = disk.reject_new_admits
        if unresolved >= hard_cap:
            reject = True
            unresolved_pressure += 1.0
            reasons.append(
                "manual_hard_unresolved_cap" if candidate_is_manual else "hard_unresolved_cap"
            )
        elif unresolved >= soft_cap:
            unresolved_pressure += 0.75
            reasons.append(
                "manual_soft_unresolved_cap" if candidate_is_manual else "soft_unresolved_cap"
            )
        if unresolved >= debt_budget:
            unresolved_pressure += 0.5
            reasons.append(
                "manual_debt_budget_exceeded"
                if candidate_is_manual
                else "debt_budget_exceeded"
            )
        emergency_torrents = self._live_emergency_torrents(db)
        emergency = len(emergency_torrents) > 0
        if emergency:
            reject = True
            reasons.append("emergency_mode")
        underperformance_penalty = self._recent_underperformance_penalty(db)
        threshold = (
            self.settings.base_admission_threshold
            + self.settings.pressure_threshold_multiplier
            * (disk.pressure + unresolved_pressure + underperformance_penalty)
        )
        return PressureState(
            disk_pressure=disk.pressure,
            unresolved_pressure=unresolved_pressure,
            underperformance_penalty=underperformance_penalty,
            final_threshold=threshold,
            emergency_mode=emergency,
            reject_new_admits=reject,
            reasons=reasons,
        )

    def lane_status(self, db: Session, candidate_is_manual: bool) -> dict[str, object]:
        disk = self.disk.snapshot(db, candidate_is_manual=candidate_is_manual)
        pressure = self._current_pressure(db, 0, candidate_is_manual=candidate_is_manual)
        soft_cap, hard_cap, debt_budget = self._pressure_limits(candidate_is_manual)
        return {
            "lane": "manual" if candidate_is_manual else "protocol",
            "usage_bytes": (
                disk.manual_usage_bytes
                if candidate_is_manual
                else disk.protocol_usage_bytes
            ),
            "projected_usage_bytes": (
                disk.manual_projected_usage_bytes
                if candidate_is_manual
                else disk.protocol_projected_usage_bytes
            ),
            "cap_gb": (
                self.settings.manual_disk_cap_gb
                if candidate_is_manual
                else self.settings.managed_disk_cap_gb
            ),
            "high_water_gb": (
                self.settings.manual_admission_high_water_mark_gb
                if candidate_is_manual
                else self.settings.admission_high_water_mark_gb
            ),
            "unresolved_must_keep": self._unresolved_count(
                db, candidate_is_manual=candidate_is_manual
            ),
            "soft_unresolved_cap": soft_cap,
            "hard_unresolved_cap": hard_cap,
            "debt_budget": debt_budget,
            "disk_pressure": pressure.disk_pressure,
            "unresolved_pressure": pressure.unresolved_pressure,
            "underperformance_penalty": pressure.underperformance_penalty,
            "final_threshold": pressure.final_threshold,
            "reject_new_admits": pressure.reject_new_admits,
            "reasons": pressure.reasons,
            "free_host_disk_bytes": disk.free_host_disk_bytes,
        }

    def _unresolved_count(self, db: Session, candidate_is_manual: bool) -> int:
        query = db.query(Torrent).filter(
            Torrent.managed.is_(True),
            self._confirmed_executor_filter(),
            Torrent.state == TorrentState.must_keep.value,
            Torrent.exclude_from_learning.is_(candidate_is_manual),
        )
        return query.count()

    def _pressure_limits(self, candidate_is_manual: bool) -> tuple[int, int, int]:
        if candidate_is_manual:
            return (
                self.settings.manual_soft_unresolved_cap,
                self.settings.manual_hard_unresolved_cap,
                self.settings.manual_debt_budget,
            )
        return (
            self.settings.soft_unresolved_cap,
            self.settings.hard_unresolved_cap,
            self.settings.debt_budget,
        )

    def evaluate_candidate(self, db: Session, candidate: ReleaseCandidate) -> Decision:
        prediction = self.learner.get_prediction(db, candidate)
        pressure = self._current_pressure(
            db,
            candidate.size_bytes,
            candidate_is_manual=candidate.exclude_from_learning,
        )
        score = self.scorer.score(candidate, prediction, pressure)
        action = DecisionAction.admit.value
        reason = None
        if not candidate.freeleech and not candidate.exclude_from_learning:
            action = DecisionAction.reject.value
            reason = "automation_default_freeleech_only"
        if pressure.reject_new_admits:
            action = DecisionAction.reject.value
            reason = ",".join(pressure.reasons) or "pressure_block"
        if candidate.exclude_from_learning and not pressure.reject_new_admits:
            action = DecisionAction.admit.value
            reason = None
        elif score.utility < pressure.final_threshold:
            action = DecisionAction.reject.value
            reason = reason or "score_below_threshold"
        if self.settings.dry_run and action == DecisionAction.admit.value:
            action = DecisionAction.dry_run.value
        decision = Decision(
            candidate_id=candidate.id,
            action=action,
            score=score.utility,
            threshold=pressure.final_threshold,
            utility_components=score.model_dump(),
            rejection_reason=reason,
            bucket_key=prediction.key,
            pressure_snapshot={
                "disk_pressure": pressure.disk_pressure,
                "unresolved_pressure": pressure.unresolved_pressure,
                "underperformance_penalty": pressure.underperformance_penalty,
                "final_threshold": pressure.final_threshold,
                "emergency_mode": pressure.emergency_mode,
                "reject_new_admits": pressure.reject_new_admits,
                "reasons": pressure.reasons,
            },
        )
        db.add(decision)
        candidate.status = action
        db.add(candidate)
        db.add(
            ControllerEvent(
                event_type="candidate_evaluated",
                severity="info",
                message=f"{action} {candidate.title}",
                payload={
                    "candidate_id": candidate.id,
                    "bucket_key": prediction.key,
                    "reason": reason,
                    "score": score.utility,
                    "threshold": pressure.final_threshold,
                    "source": candidate.source,
                    "wanted": candidate.wanted,
                    "wanted_reason": candidate.wanted_reason,
                    "dedupe_key": candidate.dedupe_key,
                },
            )
        )
        if candidate.source == "autobrr":
            db.add(
                ControllerEvent(
                    event_type="autobrr_event",
                    severity="info",
                    message=f"autobrr intake: {candidate.title}",
                    payload={"candidate_id": candidate.id, "dedupe_key": candidate.dedupe_key},
                )
            )
        return decision

    def admit_candidate(self, db: Session, candidate: ReleaseCandidate, decision: Decision) -> None:
        if decision.action != DecisionAction.admit.value:
            return
        existing = (
            db.query(Torrent)
            .filter(
                Torrent.managed.is_(True),
                (Torrent.candidate_id == candidate.id) if candidate.id is not None else False,
            )
            .one_or_none()
        )
        if existing is None and candidate.info_hash:
            existing = (
                db.query(Torrent)
                .filter(Torrent.managed.is_(True), Torrent.info_hash == candidate.info_hash)
                .one_or_none()
            )
        if existing is not None:
            return
        try:
            self.qb.add_torrent(
                candidate.raw_payload
                | {
                    "download_url": candidate.download_url,
                    "info_hash": candidate.info_hash,
                    "title": candidate.title,
                }
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            decision.action = DecisionAction.reject.value
            decision.rejection_reason = f"executor_failure:{message}"
            candidate.status = "executor_error"
            db.add(candidate)
            db.add(decision)
            db.add(
                ControllerEvent(
                    event_type="qb_add_failed",
                    severity="warning",
                    message=f"qBittorrent add failed for {candidate.title}",
                    payload={"candidate_id": candidate.id, "error": message},
                )
            )
            self._upsert_hash_alert(
                db,
                alert_type="qb_add_failed",
                severity="warning",
                message=f"qBittorrent add failed for {candidate.title}: {message}",
                info_hash=candidate.info_hash,
                payload={
                    "candidate_id": candidate.id,
                    "info_hash": candidate.info_hash,
                    "error": message,
                },
            )
            return
        deadline = datetime.now(UTC).replace(tzinfo=None) + timedelta(
            seconds=max(self.settings.poll_interval_seconds * 3, 300)
        )
        torrent = Torrent(
            candidate_id=candidate.id,
            title=candidate.title,
            info_hash=candidate.info_hash,
            state=TorrentState.candidate.value,
            category=candidate.category,
            save_path=self.settings.qbit_save_path,
            size_bytes=candidate.size_bytes,
            freeleech=candidate.freeleech,
            tags=self._managed_tags(TorrentState.candidate.value),
            tracker=candidate.tracker,
            managed=True,
            exclude_from_learning=candidate.exclude_from_learning,
            executor_state=ExecutorState.pending.value,
            executor_deadline_at=deadline,
        )
        db.add(torrent)

    def intake_candidate(self, db: Session, payload: CandidatePayload) -> IntakeResponse:
        candidate, should_evaluate = self._persist_candidate(db, payload)
        should_evaluate = self.metadata.enrich_release_candidate(db, candidate) or should_evaluate
        latest = self._latest_decision(db, candidate.id)
        existing_torrent = (
            db.query(Torrent)
            .filter(Torrent.candidate_id == candidate.id, Torrent.managed.is_(True))
            .one_or_none()
        )
        if (
            existing_torrent is not None
            and latest is not None
            and latest.action == DecisionAction.admit.value
        ):
            should_evaluate = False
        if should_evaluate or latest is None:
            decision = self.evaluate_candidate(db, candidate)
            self.admit_candidate(db, candidate, decision)
        else:
            decision = latest
        snapshot = self.record_snapshot(db)
        logger.info(
            "candidate_processed",
            candidate_id=candidate.id,
            action=decision.action,
            score=decision.score,
            threshold=decision.threshold,
            snapshot_id=snapshot.id,
        )
        db.flush()
        return IntakeResponse(
            candidate_id=candidate.id,
            decision_id=decision.id,
            action=decision.action,
            reason=decision.rejection_reason,
            score=decision.score,
            threshold=decision.threshold,
        )

    def _pending_placeholder(self, db: Session, payload: dict) -> Torrent | None:
        title = payload.get("name")
        if not title:
            return None
        normalized = normalize_title(title)
        candidates = (
            db.query(Torrent)
            .filter(
                Torrent.managed.is_(True),
                Torrent.executor_state == ExecutorState.pending.value,
                Torrent.state == TorrentState.candidate.value,
            )
            .order_by(desc(Torrent.created_at))
            .all()
        )
        for row in candidates:
            if normalize_title(row.title) == normalized:
                return row
        return None

    def _adopt_placeholder_metadata(
        self,
        db: Session,
        confirmed: Torrent,
        payload: dict[str, object],
        now: datetime,
    ) -> None:
        normalized = normalize_title(str(payload.get("name") or confirmed.title or ""))
        if not normalized:
            return
        placeholders = (
            db.query(Torrent)
            .filter(
                Torrent.managed.is_(True),
                Torrent.id != confirmed.id,
                Torrent.info_hash.is_(None),
            )
            .order_by(desc(Torrent.created_at))
            .all()
        )
        placeholder = next(
            (row for row in placeholders if normalize_title(row.title) == normalized),
            None,
        )
        if placeholder is None:
            return
        if placeholder.candidate_id is not None and confirmed.candidate_id is None:
            confirmed.candidate_id = placeholder.candidate_id
        if placeholder.exclude_from_learning:
            confirmed.exclude_from_learning = True
        if placeholder.freeleech and not confirmed.freeleech:
            confirmed.freeleech = True
        requests = list(
            db.query(ManualRequest).filter(ManualRequest.torrent_id == placeholder.id).all()
        )
        if not requests and placeholder.candidate_id is not None:
            requests = list(
                db.query(ManualRequest)
                .filter(ManualRequest.candidate_id == placeholder.candidate_id)
                .all()
            )
        for request in requests:
            request.torrent_id = confirmed.id
            if confirmed.candidate_id is None and request.candidate_id is not None:
                confirmed.candidate_id = request.candidate_id
            if request.candidate_id is None and confirmed.candidate_id is not None:
                request.candidate_id = confirmed.candidate_id
            db.add(request)
        placeholder.managed = False
        placeholder.executor_state = ExecutorState.orphaned.value
        placeholder.last_seen_at = now
        db.add(placeholder)

    def _repair_manual_request_links(self, db: Session) -> None:
        managed_torrents = (
            db.query(Torrent)
            .filter(
                Torrent.managed.is_(True),
                self._confirmed_executor_filter(),
            )
            .all()
        )
        for request in db.query(ManualRequest).all():
            current = (
                db.query(Torrent).filter(Torrent.id == request.torrent_id).one_or_none()
                if request.torrent_id
                else None
            )
            if current is not None and current.managed and current.info_hash:
                continue

            target = None
            if request.candidate_id is not None:
                target = next(
                    (row for row in managed_torrents if row.candidate_id == request.candidate_id),
                    None,
                )
            if target is None:
                chosen_title = (request.chosen_payload or {}).get("title") or request.title
                normalized = normalize_title(chosen_title or "")
                if normalized:
                    target = next(
                        (
                            row
                            for row in managed_torrents
                            if normalize_title(row.title) == normalized
                        ),
                        None,
                    )
            if target is None:
                continue
            request.torrent_id = target.id
            if target.candidate_id is None and request.candidate_id is not None:
                target.candidate_id = request.candidate_id
            if request.exclude_from_learning and not target.exclude_from_learning:
                target.exclude_from_learning = True
            db.add(target)
            db.add(request)

    def _reconcile_missing_torrents(
        self,
        db: Session,
        seen_hashes: set[str],
        now: datetime,
    ) -> None:
        grace = timedelta(seconds=max(self.settings.poll_interval_seconds * 3, 300))
        missing_cutoff = now - grace
        self._expire_pending_torrents(db, now)
        torrents = (
            db.query(Torrent)
            .filter(
                Torrent.managed.is_(True),
                self._confirmed_executor_filter(),
            )
            .all()
        )
        for torrent in torrents:
            if torrent.info_hash:
                if torrent.info_hash in seen_hashes:
                    continue
                if torrent.last_seen_at and torrent.last_seen_at > missing_cutoff:
                    continue
            elif (
                torrent.state == TorrentState.candidate.value
                and torrent.created_at > missing_cutoff
            ):
                continue

            if self._is_safe(torrent):
                torrent.managed = False
                torrent.state = TorrentState.retirable.value
                torrent.retirable_at = now
                torrent.tags = self._managed_tags(TorrentState.retirable.value, torrent.tags)
                self._clear_hash_alert(db, "managed_torrent_missing", torrent.info_hash)
                db.add(
                    ControllerEvent(
                        event_type="managed_torrent_missing",
                        severity="info",
                        message=f"Safe torrent missing from qB scope: {torrent.title}",
                        payload={"info_hash": torrent.info_hash, "torrent_id": torrent.id},
                    )
                )
                continue

            torrent.state = TorrentState.error.value
            torrent.tags = self._managed_tags(TorrentState.error.value, torrent.tags)
            self._upsert_hash_alert(
                db,
                alert_type="managed_torrent_missing",
                severity="critical",
                message=f"Unsafe managed torrent missing from qB scope: {torrent.title}",
                info_hash=torrent.info_hash,
                payload={"info_hash": torrent.info_hash, "torrent_id": torrent.id},
            )
            db.add(
                ControllerEvent(
                    event_type="managed_torrent_missing",
                    severity="critical",
                    message=f"Unsafe managed torrent missing from qB scope: {torrent.title}",
                    payload={"info_hash": torrent.info_hash, "torrent_id": torrent.id},
                )
            )

    def sync_from_qb(self, db: Session) -> int:
        synced = 0
        now = datetime.now(UTC).replace(tzinfo=None)
        seen_hashes: set[str] = set()
        for payload in self.qb.get_torrents():
            hash_value = payload.get("hash")
            torrent = None
            if hash_value:
                torrent = (
                    db.query(Torrent)
                    .filter(Torrent.managed.is_(True), Torrent.info_hash == hash_value)
                    .one_or_none()
                )
            if torrent is None and not is_managed_qb_torrent(payload, self.settings):
                continue
            if hash_value:
                seen_hashes.add(hash_value)
            if torrent is None:
                torrent = self._pending_placeholder(db, payload)
            if torrent is None:
                torrent = Torrent(
                    title=payload.get("name", hash_value or "unknown"),
                    info_hash=hash_value,
                    managed=True,
                    exclude_from_learning=False,
                    state=TorrentState.candidate.value,
                    executor_state=ExecutorState.confirmed.value,
                    executor_confirmed_at=now,
                )
                db.add(torrent)
                db.flush()
            else:
                torrent.executor_state = ExecutorState.confirmed.value
                torrent.executor_confirmed_at = now
            self._heal_qb_scope(torrent, payload)
            self._adopt_placeholder_metadata(db, torrent, payload, now)
            if torrent.candidate_id is not None and not torrent.exclude_from_learning:
                candidate = (
                    db.query(ReleaseCandidate)
                    .filter(ReleaseCandidate.id == torrent.candidate_id)
                    .one_or_none()
                )
                if candidate is not None and candidate.exclude_from_learning:
                    torrent.exclude_from_learning = True

            previous_uploaded = torrent.uploaded_bytes
            previous_progress = torrent.progress
            torrent.title = payload.get("name", torrent.title)
            torrent.info_hash = hash_value or torrent.info_hash
            torrent.save_path = payload.get("save_path")
            torrent.content_path = payload.get("content_path")
            torrent.size_bytes = int(payload.get("size") or 0)
            torrent.progress = float(payload.get("progress") or 0.0)
            torrent.ratio = float(payload.get("ratio") or 0.0)
            torrent.uploaded_bytes = int(payload.get("uploaded") or 0)
            torrent.downloaded_bytes = int(payload.get("downloaded") or 0)
            torrent.seed_time_seconds = int(payload.get("seeding_time") or 0)
            torrent.dl_speed = int(payload.get("dlspeed") or 0)
            torrent.up_speed = int(payload.get("upspeed") or 0)
            torrent.tags = self._managed_tags(
                torrent.state,
                payload.get("tags") or torrent.tags or "",
            )
            torrent.category = (
                self.settings.qbit_category or payload.get("category") or torrent.category
            )
            torrent.paused = payload.get("state", "").startswith("paused")
            torrent.managed = True
            torrent.executor_state = ExecutorState.confirmed.value
            torrent.executor_confirmed_at = now
            torrent.last_seen_at = now
            if (
                torrent.up_speed > 0
                or torrent.dl_speed > 0
                or torrent.uploaded_bytes > previous_uploaded
                or torrent.progress > previous_progress
                or torrent.last_activity_at is None
            ):
                torrent.last_activity_at = now
            remaining_ratio = max(0.0, self.settings.tracker_safe_ratio - torrent.ratio)
            remaining_hours = max(
                0.0,
                self.settings.tracker_safe_hours - (torrent.seed_time_seconds / 3600),
            )
            torrent.tracker_safe_estimate_hours = min(remaining_hours, remaining_ratio * 336)
            observation = Observation(
                torrent_id=torrent.id,
                uploaded_bytes=torrent.uploaded_bytes,
                downloaded_bytes=torrent.downloaded_bytes,
                ratio=torrent.ratio,
                seed_time_seconds=torrent.seed_time_seconds,
                progress=torrent.progress,
                state=payload.get("state", ""),
                up_speed=torrent.up_speed,
                dl_speed=torrent.dl_speed,
                payload=payload,
            )
            db.add(observation)
            self.learner.update_from_outcome(db, torrent, observation)
            LibraryHandoffService(db, self.settings).observe_completed_torrent(torrent, now)
            self._clear_hash_alert(db, "managed_torrent_missing", torrent.info_hash)
            synced += 1
        self._reconcile_missing_torrents(db, seen_hashes, now)
        self._repair_manual_request_links(db)
        self.record_snapshot(db)
        return synced

    def snapshot_model(self, db: Session) -> SystemSnapshot:
        disk = self.disk.snapshot(db)
        unresolved = (
            db.query(Torrent)
            .filter(
                Torrent.managed.is_(True),
                self._confirmed_executor_filter(),
                Torrent.state == TorrentState.must_keep.value,
            )
            .count()
        )
        hot_count = (
            db.query(Torrent)
            .filter(
                Torrent.managed.is_(True),
                self._confirmed_executor_filter(),
                Torrent.state == TorrentState.hot.value,
            )
            .count()
        )
        safe_anchor_count = (
            db.query(Torrent)
            .filter(
                Torrent.managed.is_(True),
                self._confirmed_executor_filter(),
                Torrent.state == TorrentState.safe_anchor.value,
            )
            .count()
        )
        pressure = self._current_pressure(db, 0)
        return SystemSnapshot(
            managed_usage_bytes=disk.managed_usage_bytes,
            projected_usage_bytes=disk.projected_usage_bytes,
            protocol_usage_bytes=disk.protocol_usage_bytes,
            protocol_projected_usage_bytes=disk.protocol_projected_usage_bytes,
            manual_usage_bytes=disk.manual_usage_bytes,
            manual_projected_usage_bytes=disk.manual_projected_usage_bytes,
            free_host_disk_bytes=disk.free_host_disk_bytes,
            unresolved_must_keep=unresolved,
            hot_count=hot_count,
            safe_anchor_count=safe_anchor_count,
            emergency_mode=pressure.emergency_mode,
            disk_pressure=pressure.disk_pressure,
            unresolved_pressure=pressure.unresolved_pressure,
            underperformance_penalty=pressure.underperformance_penalty,
            final_threshold=pressure.final_threshold,
            reasons={"reasons": pressure.reasons},
        )

    def record_snapshot(self, db: Session) -> SystemSnapshot:
        snapshot = self.snapshot_model(db)
        db.add(snapshot)
        return snapshot

    def latest_snapshot(self, db: Session) -> SystemSnapshot | None:
        return db.query(SystemSnapshot).order_by(desc(SystemSnapshot.taken_at)).first()
