from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CandidatePayload(BaseModel):
    title: str
    guid: str | None = None
    tracker: str = "unknown"
    category: str = "other"
    release_year: int | None = None
    size_bytes: int = Field(ge=0)
    freeleech: bool = False
    published_at: datetime | None = None
    seeders: int | None = Field(default=None, ge=0)
    leechers: int | None = Field(default=None, ge=0)
    download_url: str | None = None
    info_hash: str | None = None
    source: str = "autobrr"
    source_confidence: float | None = None
    exclude_from_learning: bool = False
    wanted: bool = False
    wanted_reason: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class IntakeResponse(BaseModel):
    candidate_id: int
    decision_id: int
    action: str
    reason: str | None
    score: float
    threshold: float


class BucketDefinition(BaseModel):
    size_bucket: str
    age_bucket: str
    year_bucket: str
    freeleech: bool
    swarm_bucket: str
    category: str


class ScoreBreakdown(BaseModel):
    expected_upload_6h: float
    expected_upload_24h: float
    expected_upload_7d: float
    charged_download_cost: float
    seeding_debt: float
    hnr_tail_risk: float
    exploration_bonus: float
    source_adjustment: float
    confidence_adjustment: float
    wanted_adjustment: float
    utility: float


class PressureSnapshot(BaseModel):
    disk_pressure: float
    unresolved_pressure: float
    underperformance_penalty: float
    final_threshold: float
    emergency_mode: bool
    reject_new_admits: bool
    reasons: list[str]


class ManualRequestPayload(BaseModel):
    media_type: str
    title: str
    year: int | None = None
    season: int | None = Field(default=None, ge=1)
    episode: int | None = Field(default=None, ge=1)
    quality_hint: str | None = None
    language_hint: str | None = None
    freeleech_preferred: bool = True
    add_to_plex: bool = True
    notes: str | None = None


class ManualRequestResponse(BaseModel):
    request_id: int
    status: str
    execution_path: str | None
    exclude_from_learning: bool = True
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ManualRequestPlan(BaseModel):
    request_id: int
    executable: bool
    execution_path: str | None
    requirements: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class ManualCandidatePreview(BaseModel):
    title: str
    indexer: str
    size_bytes: int
    seeders: int | None = None
    leechers: int | None = None
    freeleech: bool = False
    download_url: str | None = None
    info_url: str | None = None
    publish_date: datetime | None = None
    resolution: str | None = None
    language_match: str | None = None
    ranking_score: float
    rationale: list[str] = Field(default_factory=list)


class ManualCandidateSelectionPayload(BaseModel):
    title: str
    indexer: str
    size_bytes: int = Field(ge=0)
    seeders: int | None = Field(default=None, ge=0)
    leechers: int | None = Field(default=None, ge=0)
    freeleech: bool = False
    download_url: str
    info_url: str | None = None
    publish_date: datetime | None = None
    resolution: str | None = None
    language_match: str | None = None
    ranking_score: float | None = None
    rationale: list[str] = Field(default_factory=list)


class ManualFulfillPayload(ManualRequestPayload):
    preferred_resolutions: list[str] = Field(default_factory=list)
    preferred_languages: list[str] = Field(default_factory=list)
    add_to_plex: bool = True
    exact_match_required: bool | None = None
    candidate_limit: int = Field(default=5, ge=1, le=10)


class ManualFulfillResponse(BaseModel):
    request: dict[str, Any]
    plan: dict[str, Any]
    selected_candidate: dict[str, Any] | None = None
    candidates_considered: int = 0
    message: str
