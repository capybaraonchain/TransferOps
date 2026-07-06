from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.models import ReleaseCandidate
from app.services.learning import BucketPrediction
from app.services.schemas import ScoreBreakdown
from app.units import BYTES_PER_GB


@dataclass(slots=True)
class PressureState:
    disk_pressure: float
    unresolved_pressure: float
    underperformance_penalty: float
    final_threshold: float
    emergency_mode: bool
    reject_new_admits: bool
    reasons: list[str]


class Scorer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def score(
        self,
        candidate: ReleaseCandidate,
        prediction: BucketPrediction,
        pressure: PressureState,
    ) -> ScoreBreakdown:
        size_gb = candidate.size_bytes / BYTES_PER_GB
        charged_download_cost = 0.0 if candidate.freeleech else size_gb
        seeding_debt = (size_gb / 10.0) * (
            prediction.time_to_safe_hours / self.settings.tracker_safe_hours
        )
        seeding_debt *= 0.5 + prediction.stall_probability
        if not candidate.freeleech:
            seeding_debt *= 1.15
        hnr_tail_risk = prediction.stall_probability * (
            1.2 if prediction.time_to_safe_hours > 200 else 0.8
        )
        source_adjustment = self._source_adjustment(candidate)
        confidence_adjustment = (
            (candidate.source_confidence - 0.5) * self.settings.source_confidence_weight
        )
        wanted_adjustment = self.settings.wanted_score_boost if candidate.wanted else 0.0
        utility = (
            self.settings.alpha * prediction.upload_6h
            + self.settings.beta * prediction.upload_24h
            + self.settings.gamma * prediction.upload_7d
            - self.settings.delta * charged_download_cost
            - self.settings.lambda_penalty * seeding_debt
            - self.settings.mu * hnr_tail_risk
            + prediction.uncertainty_bonus
            + source_adjustment
            + confidence_adjustment
            + wanted_adjustment
        )
        utility -= (
            pressure.disk_pressure
            + pressure.unresolved_pressure
            + pressure.underperformance_penalty
        )
        return ScoreBreakdown(
            expected_upload_6h=prediction.upload_6h,
            expected_upload_24h=prediction.upload_24h,
            expected_upload_7d=prediction.upload_7d,
            charged_download_cost=charged_download_cost,
            seeding_debt=seeding_debt,
            hnr_tail_risk=hnr_tail_risk,
            exploration_bonus=prediction.uncertainty_bonus,
            source_adjustment=source_adjustment,
            confidence_adjustment=confidence_adjustment,
            wanted_adjustment=wanted_adjustment,
            utility=utility,
        )

    def _source_adjustment(self, candidate: ReleaseCandidate) -> float:
        if candidate.source == "autobrr":
            return self.settings.autobrr_source_boost
        if candidate.source == "rss":
            return -self.settings.rss_source_penalty
        if candidate.source == "prowlarr_backfill":
            return -self.settings.prowlarr_source_penalty
        return 0.0
