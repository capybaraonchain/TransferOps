from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRANSFEROPS_", env_file=".env", extra="ignore")

    app_name: str = "transferops"
    env: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./transferops.db"
    log_level: str = "INFO"
    dry_run: bool = True

    qbit_base_url: str = "http://127.0.0.1:8080"
    qbit_username: str = "admin"
    qbit_password: str = "adminadmin"
    qbit_category: str = "transferops.managed"
    qbit_tag: str = "transferops.managed"
    qbit_save_path: str = r"C:\TransferOps\managed"
    manual_movies_save_path: str = r"C:\TransferOps\manual\assets"
    manual_series_save_path: str = r"C:\TransferOps\manual\collections"
    host_disk_check_path: str = r"C:\TransferOps\managed"
    qbit_timeout_seconds: int = 15

    autobrr_enabled: bool = True
    autobrr_base_url: str | None = None
    autobrr_api_key: str | None = None
    autobrr_shared_secret: str | None = None
    autobrr_require_signature: bool = False
    autobrr_source_priority: int = 100

    managed_disk_cap_gb: float = 250.0
    manual_disk_cap_gb: float = 150.0
    disk_reserve_gb: float = 25.0
    admission_high_water_mark_gb: float = 220.0
    manual_admission_high_water_mark_gb: float = 135.0
    minimum_free_host_disk_gb: float | None = 30.0
    anchor_count: int = 15

    debt_budget: int = 18
    soft_unresolved_cap: int = 18
    hard_unresolved_cap: int = 24
    manual_debt_budget: int = 8
    manual_soft_unresolved_cap: int = 8
    manual_hard_unresolved_cap: int = 12
    tracker_safe_ratio: float = 1.0
    tracker_safe_hours: int = 336
    tracker_safe_grace_ratio: float = 0.05
    tracker_safe_grace_hours: int = 6

    poll_interval_seconds: int = 60
    reconcile_interval_seconds: int = 120
    rss_enabled: bool = True
    rss_url: str | None = None
    rss_limit: int = 25
    rss_assume_freeleech: bool = True
    rss_default_tracker: str = "demo"
    rss_parse_description: bool = True
    rss_compute_info_hash: bool = False
    rss_poll_interval_minutes: int = 15
    learning_interval_minutes: int = 180
    stall_window_hours: int = 6
    metadata_enrichment_enabled: bool = True
    metadata_lookup_timeout_seconds: int = 10
    tmdb_api_key: str | None = None

    radarr_enabled: bool = False
    radarr_base_url: str | None = None
    radarr_api_key: str | None = None
    radarr_poll_interval_minutes: int = 30
    radarr_root_folder_path: str | None = None
    radarr_quality_profile_id: int | None = None

    sonarr_enabled: bool = False
    sonarr_base_url: str | None = None
    sonarr_api_key: str | None = None
    sonarr_poll_interval_minutes: int = 30
    sonarr_root_folder_path: str | None = None
    sonarr_quality_profile_id: int | None = None
    sonarr_language_profile_id: int | None = None

    prowlarr_enabled: bool = False
    prowlarr_base_url: str | None = None
    prowlarr_api_key: str | None = None
    prowlarr_manual_indexer_ids: str | None = None
    plex_enabled: bool = False
    plex_base_url: str = "http://127.0.0.1:32400"
    plex_token: str | None = None
    plex_movies_section_id: int | None = None
    plex_series_section_id: int | None = None
    plex_handoff_enabled: bool = True
    plex_handoff_interval_seconds: int = 120
    agent_api_token: str = "transferops-local-agent"
    manual_requests_allow_arr_add: bool = True
    manual_requests_require_existing_arr_item: bool = False
    manual_requests_arr_timeout_minutes: int = 20
    manual_request_candidate_limit: int = 5
    manual_request_preferred_resolutions: str = "2160p,1080p"
    manual_request_preferred_languages: str = "english,spanish"
    manual_request_banned_terms: str = "hdr,dolby vision,dv"

    alpha: float = 1.0
    beta: float = 0.8
    gamma: float = 0.4
    delta: float = 1.2
    lambda_penalty: float = 1.3
    mu: float = 1.5
    exploration_bonus: float = 0.35
    base_admission_threshold: float = -1.0
    pressure_threshold_multiplier: float = 4.0
    wanted_score_boost: float = 1.0
    autobrr_source_boost: float = 0.8
    rss_source_penalty: float = 0.25
    prowlarr_source_penalty: float = 0.1
    source_confidence_weight: float = 0.6

    dashboard_username: str = "admin"
    dashboard_password: str = "change-me"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
