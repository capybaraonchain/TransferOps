from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TorrentState(StrEnum):
    candidate = "candidate"
    hot = "hot"
    must_keep = "must_keep"
    safe_anchor = "safe_anchor"
    retirable = "retirable"
    error = "error"


class DecisionAction(StrEnum):
    admit = "admit"
    reject = "reject"
    dry_run = "dry_run"


class ExecutorState(StrEnum):
    pending = "pending"
    confirmed = "confirmed"
    failed = "failed"
    orphaned = "orphaned"


class ManualRequestStatus(StrEnum):
    pending = "pending"
    planned = "planned"
    awaiting_execution = "awaiting_execution"
    submitted_to_arr = "submitted_to_arr"
    candidate_found = "candidate_found"
    admitted = "admitted"
    rejected = "rejected"
    completed = "completed"
    failed = "failed"


class LibraryHandoffStatus(StrEnum):
    pending = "pending"
    waiting_config = "waiting_config"
    scan_requested = "scan_requested"
    completed = "completed"
    failed = "failed"


class ReleaseCandidate(Base):
    __tablename__ = "release_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), index=True)
    guid: Mapped[str | None] = mapped_column(String(255), index=True)
    tracker: Mapped[str] = mapped_column(String(128), default="unknown")
    category: Mapped[str] = mapped_column(String(128), default="other")
    release_year: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer)
    freeleech: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    seeders: Mapped[int | None] = mapped_column(Integer)
    leechers: Mapped[int | None] = mapped_column(Integer)
    download_url: Mapped[str | None] = mapped_column(Text)
    info_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), index=True, unique=True)
    source: Mapped[str] = mapped_column(String(64), default="autobrr")
    source_confidence: Mapped[float] = mapped_column(Float, default=0.5)
    exclude_from_learning: Mapped[bool] = mapped_column(Boolean, default=False)
    wanted: Mapped[bool] = mapped_column(Boolean, default=False)
    wanted_reason: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    decisions: Mapped[list[Decision]] = relationship(back_populates="candidate")


class Torrent(Base):
    __tablename__ = "torrents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("release_candidates.id"))
    title: Mapped[str] = mapped_column(String(512))
    info_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(String(32), default=TorrentState.hot.value, index=True)
    category: Mapped[str] = mapped_column(String(128), default="other")
    save_path: Mapped[str | None] = mapped_column(Text)
    content_path: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    ratio: Mapped[float] = mapped_column(Float, default=0.0)
    uploaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    downloaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    seed_time_seconds: Mapped[int] = mapped_column(Integer, default=0)
    dl_speed: Mapped[int] = mapped_column(Integer, default=0)
    up_speed: Mapped[int] = mapped_column(Integer, default=0)
    freeleech: Mapped[bool] = mapped_column(Boolean, default=False)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_learning_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    safely_seeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    retirable_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    tags: Mapped[str] = mapped_column(String(512), default="")
    tracker: Mapped[str | None] = mapped_column(String(128))
    tracker_safe_estimate_hours: Mapped[float | None] = mapped_column(Float)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    managed: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    exclude_from_learning: Mapped[bool] = mapped_column(Boolean, default=False)
    executor_state: Mapped[str] = mapped_column(
        String(32), default=ExecutorState.confirmed.value, index=True
    )
    executor_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    executor_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow_naive,
        onupdate=utcnow_naive,
    )

    candidate: Mapped[ReleaseCandidate | None] = relationship()
    observations: Mapped[list[Observation]] = relationship(back_populates="torrent")


class Observation(Base):
    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    torrent_id: Mapped[int] = mapped_column(ForeignKey("torrents.id"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    uploaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    downloaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    ratio: Mapped[float] = mapped_column(Float, default=0.0)
    seed_time_seconds: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    state: Mapped[str] = mapped_column(String(32), default="")
    up_speed: Mapped[int] = mapped_column(Integer, default=0)
    dl_speed: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    torrent: Mapped[Torrent] = relationship(back_populates="observations")


class BucketStats(Base):
    __tablename__ = "bucket_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bucket_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    definition: Mapped[dict] = mapped_column(JSON, default=dict)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    ewma_upload_1h: Mapped[float] = mapped_column(Float, default=0.0)
    ewma_upload_6h: Mapped[float] = mapped_column(Float, default=0.0)
    ewma_upload_24h: Mapped[float] = mapped_column(Float, default=0.0)
    ewma_upload_7d: Mapped[float] = mapped_column(Float, default=0.0)
    ewma_time_to_safe_hours: Mapped[float] = mapped_column(Float, default=336.0)
    stall_probability: Mapped[float] = mapped_column(Float, default=0.5)
    uncertainty_bonus: Mapped[float] = mapped_column(Float, default=0.0)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("release_candidates.id"), index=True)
    action: Mapped[str] = mapped_column(String(16), index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    threshold: Mapped[float] = mapped_column(Float, default=0.0)
    utility_components: Mapped[dict] = mapped_column(JSON, default=dict)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    bucket_key: Mapped[str] = mapped_column(String(255))
    pressure_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    candidate: Mapped[ReleaseCandidate] = relationship(back_populates="decisions")


class SystemSnapshot(Base):
    __tablename__ = "system_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
    managed_usage_bytes: Mapped[int] = mapped_column(Integer, default=0)
    projected_usage_bytes: Mapped[int] = mapped_column(Integer, default=0)
    protocol_usage_bytes: Mapped[int] = mapped_column(Integer, default=0)
    protocol_projected_usage_bytes: Mapped[int] = mapped_column(Integer, default=0)
    manual_usage_bytes: Mapped[int] = mapped_column(Integer, default=0)
    manual_projected_usage_bytes: Mapped[int] = mapped_column(Integer, default=0)
    free_host_disk_bytes: Mapped[int | None] = mapped_column(Integer)
    unresolved_must_keep: Mapped[int] = mapped_column(Integer, default=0)
    hot_count: Mapped[int] = mapped_column(Integer, default=0)
    safe_anchor_count: Mapped[int] = mapped_column(Integer, default=0)
    emergency_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    disk_pressure: Mapped[float] = mapped_column(Float, default=0.0)
    unresolved_pressure: Mapped[float] = mapped_column(Float, default=0.0)
    underperformance_penalty: Mapped[float] = mapped_column(Float, default=0.0)
    final_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    reasons: Mapped[dict] = mapped_column(JSON, default=dict)


class ControllerEvent(Base):
    __tablename__ = "controller_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(32), default="info")
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)


class DiskBudgetEvent(Base):
    __tablename__ = "disk_budget_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    managed_usage_bytes: Mapped[int] = mapped_column(Integer, default=0)
    free_host_disk_bytes: Mapped[int | None] = mapped_column(Integer)
    projected_usage_bytes: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(32), default="warning")
    message: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)


class RuntimeSettings(Base):
    __tablename__ = "runtime_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive
    )


class MetadataCache(Base):
    __tablename__ = "metadata_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    media_type: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="local")
    query_title: Mapped[str] = mapped_column(String(512))
    normalized_title: Mapped[str] = mapped_column(String(512), index=True)
    season: Mapped[int | None] = mapped_column(Integer)
    episode: Mapped[int | None] = mapped_column(Integer)
    resolved_title: Mapped[str | None] = mapped_column(String(512))
    release_year: Mapped[int | None] = mapped_column(Integer)
    series_year: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="miss", index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive
    )


class InboundEvent(Base):
    __tablename__ = "inbound_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), index=True)
    title: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), index=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("release_candidates.id"))
    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)


class IntegrationState(Base):
    __tablename__ = "integration_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive
    )


class ManualRequest(Base):
    __tablename__ = "manual_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    year: Mapped[int | None] = mapped_column(Integer)
    season: Mapped[int | None] = mapped_column(Integer)
    episode: Mapped[int | None] = mapped_column(Integer)
    quality_hint: Mapped[str | None] = mapped_column(String(128))
    language_hint: Mapped[str | None] = mapped_column(String(64))
    freeleech_preferred: Mapped[bool] = mapped_column(Boolean, default=True)
    exclude_from_learning: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), default=ManualRequestStatus.pending.value, index=True
    )
    execution_path: Mapped[str | None] = mapped_column(String(64))
    request_source: Mapped[str] = mapped_column(String(32), default="agent")
    arr_source: Mapped[str | None] = mapped_column(String(32))
    arr_item_id: Mapped[int | None] = mapped_column(Integer)
    arr_lookup_term: Mapped[str | None] = mapped_column(String(255))
    arr_command_id: Mapped[int | None] = mapped_column(Integer)
    matched_title: Mapped[str | None] = mapped_column(String(255))
    matched_year: Mapped[int | None] = mapped_column(Integer)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("release_candidates.id"))
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"))
    torrent_id: Mapped[int | None] = mapped_column(ForeignKey("torrents.id"))
    chosen_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive
    )


class LibraryHandoff(Base):
    __tablename__ = "library_handoffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    torrent_id: Mapped[int | None] = mapped_column(ForeignKey("torrents.id"), index=True)
    manual_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("manual_requests.id"), index=True
    )
    media_type: Mapped[str] = mapped_column(String(32), index=True)
    target: Mapped[str] = mapped_column(String(32), default="plex", index=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    source_path: Mapped[str | None] = mapped_column(Text)
    section_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(32), default=LibraryHandoffStatus.pending.value, index=True
    )
    priority_score: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive
    )
    scan_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class SeriesEpisodeProgress(Base):
    __tablename__ = "series_episode_progress"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_title: Mapped[str] = mapped_column(String(255), index=True)
    normalized_series_title: Mapped[str] = mapped_column(String(255), index=True)
    season: Mapped[int] = mapped_column(Integer, index=True)
    episode: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(32), default="downloaded", index=True)
    torrent_id: Mapped[int | None] = mapped_column(ForeignKey("torrents.id"), index=True)
    manual_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("manual_requests.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class WantedItem(Base):
    __tablename__ = "wanted_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    item_type: Mapped[str] = mapped_column(String(32), default="movie")
    title: Mapped[str] = mapped_column(String(255), index=True)
    normalized_title: Mapped[str] = mapped_column(String(255), index=True)
    year: Mapped[int | None] = mapped_column(Integer)
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive
    )
