from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import sqrt

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BucketStats, Observation, ReleaseCandidate, Torrent
from app.services.schemas import BucketDefinition
from app.units import BYTES_PER_GB


def _ewma(previous: float, value: float, alpha: float = 0.3) -> float:
    return alpha * value + (1 - alpha) * previous


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


@dataclass(slots=True)
class BucketPrediction:
    key: str
    definition: BucketDefinition
    upload_1h: float
    upload_6h: float
    upload_24h: float
    upload_7d: float
    time_to_safe_hours: float
    stall_probability: float
    uncertainty_bonus: float
    sample_count: int


class BucketLearner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _year_bucket(self, release_year: int | None) -> str:
        if release_year is None:
            return "unknown"
        current_year = datetime.now(UTC).year
        age = max(0, current_year - release_year)
        if age <= 1:
            return "current"
        if age <= 2:
            return "last2"
        if age <= 5:
            return "recent_catalog"
        if age <= 14:
            return "older_catalog"
        return "deep_catalog"

    def _bucket_torrent_count(self, db: Session, bucket_key: str) -> int:
        count = 0
        rows = (
            db.query(Torrent, ReleaseCandidate)
            .join(ReleaseCandidate, Torrent.candidate_id == ReleaseCandidate.id)
            .all()
        )
        for torrent, candidate in rows:
            if torrent.exclude_from_learning or candidate.exclude_from_learning:
                continue
            if self.key_for_definition(self.bucket_for_candidate(candidate)) != bucket_key:
                continue
            if torrent.id:
                count += 1
        return count

    def _prediction_from_stats(
        self,
        key: str,
        definition: BucketDefinition,
        stats: BucketStats,
    ) -> BucketPrediction:
        return BucketPrediction(
            key=key,
            definition=definition,
            upload_1h=stats.ewma_upload_1h,
            upload_6h=stats.ewma_upload_6h,
            upload_24h=stats.ewma_upload_24h,
            upload_7d=stats.ewma_upload_7d,
            time_to_safe_hours=stats.ewma_time_to_safe_hours,
            stall_probability=stats.stall_probability,
            uncertainty_bonus=stats.uncertainty_bonus,
            sample_count=stats.sample_count,
        )

    def _aggregate_prediction(
        self,
        key: str,
        definition: BucketDefinition,
        rows: list[BucketStats],
    ) -> BucketPrediction | None:
        if not rows:
            return None
        total_weight = sum(max(1, row.sample_count) for row in rows)

        def weighted(attr: str) -> float:
            return sum(getattr(row, attr) * max(1, row.sample_count) for row in rows) / total_weight

        return BucketPrediction(
            key=key,
            definition=definition,
            upload_1h=weighted("ewma_upload_1h"),
            upload_6h=weighted("ewma_upload_6h"),
            upload_24h=weighted("ewma_upload_24h"),
            upload_7d=weighted("ewma_upload_7d"),
            time_to_safe_hours=weighted("ewma_time_to_safe_hours"),
            stall_probability=weighted("stall_probability"),
            uncertainty_bonus=min(row.uncertainty_bonus for row in rows),
            sample_count=sum(row.sample_count for row in rows),
        )

    def bucket_for_candidate(self, candidate: ReleaseCandidate) -> BucketDefinition:
        size_gb = candidate.size_bytes / BYTES_PER_GB
        if size_gb < 5:
            size_bucket = "tiny"
        elif size_gb < 20:
            size_bucket = "small"
        elif size_gb < 60:
            size_bucket = "medium"
        else:
            size_bucket = "large"

        age_hours = 9999.0
        if candidate.published_at:
            published_at = _naive_utc(candidate.published_at)
            age_hours = max(
                0.0,
                (datetime.now(UTC).replace(tzinfo=None) - published_at).total_seconds() / 3600,
            )
        if age_hours <= 1:
            age_bucket = "fresh"
        elif age_hours <= 6:
            age_bucket = "recent"
        elif age_hours <= 24:
            age_bucket = "stale"
        else:
            age_bucket = "old"

        seeders = candidate.seeders or 0
        leechers = candidate.leechers or 0
        ratio = leechers / max(seeders, 1)
        if ratio >= 2:
            swarm_bucket = "demand_heavy"
        elif ratio >= 0.75:
            swarm_bucket = "balanced"
        else:
            swarm_bucket = "supply_heavy"

        return BucketDefinition(
            size_bucket=size_bucket,
            age_bucket=age_bucket,
            year_bucket=self._year_bucket(candidate.release_year),
            freeleech=candidate.freeleech,
            swarm_bucket=swarm_bucket,
            category=(candidate.category or "other").lower(),
        )

    def key_for_definition(self, definition: BucketDefinition) -> str:
        return "|".join(
            [
                definition.size_bucket,
                definition.age_bucket,
                definition.year_bucket,
                str(definition.freeleech).lower(),
                definition.swarm_bucket,
                definition.category,
            ]
        )

    def get_prediction(self, db: Session, candidate: ReleaseCandidate) -> BucketPrediction:
        definition = self.bucket_for_candidate(candidate)
        key = self.key_for_definition(definition)
        stats = db.query(BucketStats).filter(BucketStats.bucket_key == key).one_or_none()
        if stats:
            return self._prediction_from_stats(key, definition, stats)

        rows = db.query(BucketStats).all()
        definitions: list[tuple[BucketStats, dict]] = [(row, row.definition or {}) for row in rows]
        fallback_groups = [
            [
                row
                for row, row_def in definitions
                if row_def.get("size_bucket") == definition.size_bucket
                and row_def.get("year_bucket") == definition.year_bucket
                and row_def.get("freeleech") == definition.freeleech
                and row_def.get("category") == definition.category
            ],
            [
                row
                for row, row_def in definitions
                if row_def.get("size_bucket") == definition.size_bucket
                and row_def.get("year_bucket") == definition.year_bucket
                and row_def.get("freeleech") == definition.freeleech
            ],
            [
                row
                for row, row_def in definitions
                if row_def.get("year_bucket") == definition.year_bucket
                and row_def.get("freeleech") == definition.freeleech
            ],
            [
                row
                for row, row_def in definitions
                if row_def.get("freeleech") == definition.freeleech
            ],
        ]
        for group in fallback_groups:
            prediction = self._aggregate_prediction(key, definition, group)
            if prediction is not None:
                return prediction
        return BucketPrediction(
            key=key,
            definition=definition,
            upload_1h=0.1,
            upload_6h=0.4,
            upload_24h=0.8,
            upload_7d=1.2,
            time_to_safe_hours=240.0 if candidate.freeleech else 300.0,
            stall_probability=0.4,
            uncertainty_bonus=self.settings.exploration_bonus,
            sample_count=0,
        )

    def update_from_outcome(
        self,
        db: Session,
        torrent: Torrent,
        observation: Observation,
    ) -> BucketStats | None:
        observed_at = observation.observed_at or datetime.now(UTC).replace(tzinfo=None)
        observation.observed_at = observed_at
        if torrent.candidate_id is None:
            return None
        candidate = (
            db.query(ReleaseCandidate)
            .filter(ReleaseCandidate.id == torrent.candidate_id)
            .one_or_none()
        )
        if not candidate:
            return None
        if torrent.exclude_from_learning or candidate.exclude_from_learning:
            return None
        definition = self.bucket_for_candidate(candidate)
        key = self.key_for_definition(definition)
        stats = db.query(BucketStats).filter(BucketStats.bucket_key == key).one_or_none()
        if not stats:
            stats = BucketStats(
                bucket_key=key,
                definition=definition.model_dump(),
                sample_count=0,
                ewma_upload_1h=0.0,
                ewma_upload_6h=0.0,
                ewma_upload_24h=0.0,
                ewma_upload_7d=0.0,
                ewma_time_to_safe_hours=336.0,
                stall_probability=0.5,
                uncertainty_bonus=self.settings.exploration_bonus,
            )
            db.add(stats)
            db.flush()
        learning_interval_hours = max(self.settings.learning_interval_minutes / 60, 0.25)
        if torrent.last_learning_at:
            elapsed_since_learning = (observed_at - torrent.last_learning_at).total_seconds() / 3600
            if elapsed_since_learning < learning_interval_hours:
                return stats

        baseline = None
        if torrent.last_learning_at:
            baseline = (
                db.query(Observation)
                .filter(
                    Observation.torrent_id == torrent.id,
                    Observation.observed_at <= torrent.last_learning_at,
                    Observation.id != observation.id,
                )
                .order_by(Observation.observed_at.desc())
                .first()
            )
        else:
            window_start = observed_at - timedelta(hours=learning_interval_hours)
            baseline = (
                db.query(Observation)
                .filter(
                    Observation.torrent_id == torrent.id,
                    Observation.observed_at <= window_start,
                    Observation.id != observation.id,
                )
                .order_by(Observation.observed_at.desc())
                .first()
            )

        if baseline:
            elapsed_hours = max(
                (observed_at - baseline.observed_at).total_seconds() / 3600,
                learning_interval_hours,
            )
            upload_delta_gb = max(
                0.0, observation.uploaded_bytes - baseline.uploaded_bytes
            ) / BYTES_PER_GB
        else:
            seed_hours = max(observation.seed_time_seconds / 3600, learning_interval_hours)
            if seed_hours < learning_interval_hours:
                return stats
            elapsed_hours = min(seed_hours, learning_interval_hours)
            upload_delta_gb = (observation.uploaded_bytes / BYTES_PER_GB) * (
                elapsed_hours / max(seed_hours, 1e-6)
            )

        upload_rate_gb_per_hour = upload_delta_gb / max(elapsed_hours, 1e-6)
        stalled = self._stall_sample(db, torrent, observation, observed_at)
        distinct_torrents = max(1, self._bucket_torrent_count(db, key))
        stats.sample_count = distinct_torrents
        stats.ewma_upload_1h = _ewma(stats.ewma_upload_1h, upload_rate_gb_per_hour)
        stats.ewma_upload_6h = _ewma(stats.ewma_upload_6h, upload_rate_gb_per_hour * 6)
        stats.ewma_upload_24h = _ewma(stats.ewma_upload_24h, upload_rate_gb_per_hour * 24)
        stats.ewma_upload_7d = _ewma(stats.ewma_upload_7d, upload_rate_gb_per_hour * 24 * 7)
        if torrent.safely_seeded_at:
            time_to_safe_hours = max(observation.seed_time_seconds / 3600, 1.0)
            stats.ewma_time_to_safe_hours = _ewma(stats.ewma_time_to_safe_hours, time_to_safe_hours)
        stats.stall_probability = _ewma(stats.stall_probability, stalled)
        stats.uncertainty_bonus = min(
            self.settings.exploration_bonus,
            self.settings.exploration_bonus / max(1.0, sqrt(distinct_torrents)),
        )
        stats.last_updated_at = datetime.now(UTC).replace(tzinfo=None)
        torrent.last_learning_at = observed_at
        db.add(torrent)
        return stats

    def _stall_sample(
        self,
        db: Session,
        torrent: Torrent,
        observation: Observation,
        observed_at: datetime,
    ) -> float:
        if observation.progress < 1.0 or torrent.safely_seeded_at:
            return 0.0
        stall_window_hours = max(self.settings.stall_window_hours, 1)
        if observation.seed_time_seconds / 3600 < stall_window_hours:
            return 0.0
        cutoff = observed_at - timedelta(hours=stall_window_hours)
        baseline = (
            db.query(Observation)
            .filter(
                Observation.torrent_id == torrent.id,
                Observation.observed_at <= cutoff,
                Observation.id != observation.id,
            )
            .order_by(Observation.observed_at.desc())
            .first()
        )
        if baseline is None:
            return 0.0
        uploaded_delta = max(0, observation.uploaded_bytes - baseline.uploaded_bytes)
        return 1.0 if uploaded_delta == 0 else 0.0
