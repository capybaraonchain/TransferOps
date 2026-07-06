import base64

from app.config import Settings
from app.main import _backfill_manual_learning_exclusions, _backfill_release_years
from app.models import (
    BucketStats,
    Decision,
    IntegrationState,
    LibraryHandoff,
    ManualRequest,
    MetadataCache,
    ReleaseCandidate,
    RuntimeSettings,
    SeriesEpisodeProgress,
    SystemSnapshot,
    Torrent,
    WantedItem,
)
from app.services.integrations import ConnectivityService, IntegrationResult
from app.services.library import LibraryHandoffService, PlexService
from app.services.manual_requests import ArrRequestService
from app.services.schemas import IntakeResponse, ManualCandidatePreview, ManualRequestPlan


def auth_header(username="admin", password="secret"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def agent_header(token="agent-secret"):
    return {"Authorization": f"Bearer {token}"}


def test_autobrr_intake_endpoint_records_source(client, db_session):
    payload = {
        "title": "From Autobrr",
        "indexer": "demo",
        "category": "tv",
        "size": 5 * 1024**3,
        "freeleech": True,
        "seeders": 20,
        "leechers": 40,
        "torrentUrl": "https://example.invalid/from-autobrr.torrent",
        "infoHash": "auto1",
    }
    response = client.post("/api/autobrr/intake", json=payload)
    assert response.status_code == 200
    candidate = db_session.query(ReleaseCandidate).one()
    decision = db_session.query(Decision).one()
    assert candidate.source == "autobrr"
    assert decision.utility_components["source_adjustment"] > 0


def test_settings_api_masks_secrets_and_persists(client, db_session):
    put_response = client.put(
        "/api/settings",
        headers=auth_header(),
        json={
            "qbit_base_url": "http://127.0.0.1:9000",
            "qbit_password": "topsecret",
            "rss_url": "https://example.invalid/rss?passkey=secret",
            "rss_enabled": True,
            "rss_poll_interval_minutes": 5,
        },
    )
    assert put_response.status_code == 200
    get_response = client.get("/api/settings", headers=auth_header())
    data = get_response.json()
    record = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    assert data["settings"]["qbit_password"] == "********"
    assert data["settings"]["rss_url"] == "********"
    assert record.payload["qbit_password"] == "topsecret"
    assert client.app.state.scheduler_manager.scheduler.get_job("rss-import") is not None


def test_manual_request_creation_uses_autoincrement_ids(client, db_session):
    first = client.post(
        "/api/agent/requests",
        headers=agent_header(),
        json={"media_type": "movie", "title": "The Thing", "year": 1982},
    )
    second = client.post(
        "/api/agent/requests",
        headers=agent_header(),
        json={"media_type": "movie", "title": "Alien", "year": 1979},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    first_id = first.json()["request"]["id"]
    second_id = second.json()["request"]["id"]
    assert first_id != second_id
    assert first_id > 0
    assert second_id > 0


def test_dashboard_and_status_include_settings_and_wanted(client, db_session):
    db_session.add(
        WantedItem(
            source="radarr",
            item_type="movie",
            title="Wanted Movie",
            normalized_title="wanted movie",
            reason="radarr_monitored",
        )
    )
    db_session.commit()
    html = client.get("/", headers=auth_header())
    status = client.get("/api/status", headers=auth_header())
    assert html.status_code == 200
    assert "Managed Scope" in html.text
    assert "Settings" in html.text
    assert status.status_code == 200
    assert status.json()["wanted_count"] == 1


def test_settings_test_endpoint_persists_integration_health(client, db_session, monkeypatch):
    class FakeConnectivity:
        def __init__(self, *_args, **_kwargs):
            pass

        def test_rss(self):
            from app.services.integrations import IntegrationResult

            return IntegrationResult(ok=True, message="ok", payload={"bytes": 12})

    monkeypatch.setattr("app.main.ConnectivityService", FakeConnectivity)
    monkeypatch.setattr("app.main.import_rss", lambda settings: [])
    response = client.post("/api/settings/test-rss", headers=auth_header())
    assert response.status_code == 200
    state = db_session.query(IntegrationState).filter(IntegrationState.name == "rss").one()
    assert state.last_success_at is not None
    assert state.last_error is None


def test_status_uses_live_snapshot_not_stale_persisted_one(client, db_session):
    db_session.add(
        SystemSnapshot(
            managed_usage_bytes=999,
            projected_usage_bytes=999,
            free_host_disk_bytes=999,
            unresolved_must_keep=99,
            hot_count=99,
            safe_anchor_count=99,
            emergency_mode=True,
            disk_pressure=1.0,
            unresolved_pressure=1.0,
            underperformance_penalty=1.0,
            final_threshold=9.0,
            reasons={"reasons": ["stale"]},
        )
    )
    db_session.add(
        ReleaseCandidate(
            title="Live Candidate",
            tracker="demo",
            category="movie",
            size_bytes=10,
            freeleech=True,
            source="rss",
            source_confidence=0.65,
            raw_payload={},
        )
    )
    db_session.commit()
    response = client.get("/api/status", headers=auth_header())
    assert response.status_code == 200
    assert response.json()["snapshot"]["hot_count"] == 0
    assert "protocol_usage_bytes" in response.json()["snapshot"]
    assert "manual_usage_bytes" in response.json()["snapshot"]
    assert "protocol" in response.json()["lane_status"]
    assert "manual" in response.json()["lane_status"]


def test_manual_preview_endpoint_returns_manual_lane_status(client, db_session):
    response = client.get("/api/status/manual-preview", headers=auth_header())
    assert response.status_code == 200
    data = response.json()
    assert "manual" in data
    assert "shared" in data
    assert data["manual"]["lane"] == "manual"


def test_agent_manual_preview_endpoint_returns_manual_lane_status(client, db_session):
    response = client.get(
        "/api/agent/manual-preview",
        headers=agent_header(),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["manual"]["lane"] == "manual"
    assert "host_disk_check_path" in data["shared"]


def test_backfill_release_years_updates_existing_candidates(db_session):
    db_session.add_all(
        [
            ReleaseCandidate(
                title="Movie Name 2024 1080p BluRay x264",
                tracker="demo",
                category="movie",
                size_bytes=10,
                freeleech=True,
                source="rss",
                source_confidence=0.65,
                raw_payload={},
            ),
            ReleaseCandidate(
                title="No Year Title WEB-DL",
                tracker="demo",
                category="movie",
                size_bytes=10,
                freeleech=True,
                source="rss",
                source_confidence=0.65,
                raw_payload={},
            ),
        ]
    )
    db_session.commit()

    updated = _backfill_release_years(db_session)
    db_session.commit()

    rows = db_session.query(ReleaseCandidate).order_by(ReleaseCandidate.id).all()
    assert updated == 1
    assert rows[0].release_year == 2024
    assert rows[1].release_year is None


def test_backfill_manual_learning_exclusions_updates_existing_rows(db_session):
    candidate = ReleaseCandidate(
        title="Manual Candidate",
        tracker="demo",
        category="movie",
        size_bytes=10,
        freeleech=True,
        source="manual_request",
        source_confidence=0.8,
        exclude_from_learning=False,
        raw_payload={},
    )
    request = ManualRequest(
        media_type="movie",
        title="Manual Candidate",
        exclude_from_learning=False,
        status="awaiting_execution",
    )
    db_session.add_all([candidate, request])
    db_session.flush()
    torrent = Torrent(
        candidate_id=candidate.id,
        title=candidate.title,
        info_hash="manual-backfill",
        managed=True,
        exclude_from_learning=False,
    )
    db_session.add(torrent)
    db_session.commit()

    updated = _backfill_manual_learning_exclusions(db_session)
    db_session.commit()
    db_session.refresh(candidate)
    db_session.refresh(request)
    db_session.refresh(torrent)

    assert updated == 3
    assert candidate.exclude_from_learning is True
    assert request.exclude_from_learning is True
    assert torrent.exclude_from_learning is True


def test_api_buckets_marks_zero_sample_rows_as_untrained(client, db_session):
    db_session.add(
        BucketStats(
            bucket_key="small|old|unknown|true|supply_heavy|movie",
            definition={"year_bucket": "unknown"},
            sample_count=0,
        )
    )
    db_session.commit()

    response = client.get("/api/buckets", headers=auth_header())
    assert response.status_code == 200
    assert response.json()[0]["training_state"] == "untrained"


def test_agent_buckets_exposes_training_state(client, db_session):
    db_session.add(
        BucketStats(
            bucket_key="tiny|fresh|current|true|balanced|tv",
            definition={"year_bucket": "current"},
            sample_count=0,
        )
    )
    db_session.commit()

    response = client.get("/api/agent/buckets", headers=agent_header())
    assert response.status_code == 200
    assert response.json()[0]["training_state"] == "untrained"


def test_agent_budget_exposes_split_pool_caps(client):
    response = client.get("/api/agent/budget", headers=agent_header())
    assert response.status_code == 200
    data = response.json()
    assert data["protocol"]["cap_gb"] == 250.0
    assert data["manual"]["cap_gb"] == 150.0
    assert "free_host_disk_bytes" in data["shared"]


def test_agent_metadata_exposes_cache_summary(client, db_session):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "metadata_enrichment_enabled": True,
        "tmdb_api_key": "secret-key",
    }
    db_session.add(runtime)
    db_session.add(
        MetadataCache(
            cache_key="tv_episode:solar opposites:6:6",
            media_type="tv_episode",
            provider="tvmaze",
            query_title="Solar Opposites",
            normalized_title="solar opposites",
            season=6,
            episode=6,
            release_year=2026,
            series_year=2020,
            confidence=0.95,
            status="resolved",
            raw_payload={},
        )
    )
    db_session.add(
        ReleaseCandidate(
            title="Still Missing",
            tracker="demo",
            category="tv",
            size_bytes=10,
            freeleech=True,
            source="rss",
            source_confidence=0.65,
            raw_payload={},
        )
    )
    db_session.commit()

    response = client.get("/api/agent/metadata", headers=agent_header())
    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["tmdb_configured"] is True
    assert data["cache_total"] == 1
    assert data["cache_resolved"] == 1
    assert data["provider_counts"]["tvmaze"] == 1
    assert data["remaining_release_year_null"] == 1


def test_radarr_webhook_delete_removes_wanted_item(client, db_session):
    db_session.add(
        WantedItem(
            source="radarr",
            item_type="movie",
            title="Delete Me",
            normalized_title="delete me",
            year=2025,
            external_id="123",
            reason="radarr_monitored",
        )
    )
    db_session.commit()

    response = client.post(
        "/api/radarr/webhook",
        json={
            "eventType": "MovieDelete",
            "movie": {"title": "Delete Me", "year": 2025, "tmdbId": 123},
        },
    )
    assert response.status_code == 200
    assert db_session.query(WantedItem).count() == 0


def test_sonarr_webhook_delete_removes_wanted_item(client, db_session):
    db_session.add(
        WantedItem(
            source="sonarr",
            item_type="series",
            title="Delete Show",
            normalized_title="delete show",
            year=2025,
            external_id="456",
            reason="sonarr_monitored",
        )
    )
    db_session.commit()

    response = client.post(
        "/api/sonarr/webhook",
        json={
            "eventType": "SeriesDelete",
            "series": {"title": "Delete Show", "year": 2025, "tvdbId": 456},
        },
    )
    assert response.status_code == 200
    assert db_session.query(WantedItem).count() == 0


class _FakeResponse:
    def __init__(self, status_code, payload=None, content_type="application/json"):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, headers=None, timeout=10):
        self.calls.append(url)
        if url.endswith("/api/healthz/liveness"):
            return _FakeResponse(200, {"status": "ok"})
        return _FakeResponse(404, {})


def test_autobrr_connectivity_uses_liveness_probe():
    session = _FakeSession()
    settings = Settings(autobrr_base_url="http://127.0.0.1:7474")
    result = ConnectivityService(settings, session=session).test_autobrr()
    assert result.ok is True
    assert session.calls == ["http://127.0.0.1:7474/api/healthz/liveness"]


def test_agent_overview_requires_bearer_token(client):
    response = client.get("/api/agent/overview")
    assert response.status_code == 401


def test_settings_masks_agent_token(client):
    response = client.get("/api/settings", headers=auth_header())
    assert response.status_code == 200
    assert response.json()["settings"]["agent_api_token"] == "********"


def test_agent_create_request_and_execute(monkeypatch, client, db_session):
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeSession:
        def request(self, method, url, headers=None, timeout=None, **kwargs):
            if url.endswith("/api/v3/movie"):
                return FakeResponse([{"id": 42, "title": "The Thing", "year": 1982}])
            if url.endswith("/api/v3/command"):
                return FakeResponse({"id": 99, "name": "MoviesSearch"})
            raise AssertionError(f"unexpected url {url}")

    db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).update(
        {
            "payload": {
                **db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one().payload,
                "radarr_enabled": True,
                "radarr_base_url": "http://radarr.local",
                "radarr_api_key": "key",
            }
        }
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.main.ArrRequestService",
        lambda db, settings: ArrRequestService(db, settings, session=FakeSession()),
    )

    create = client.post(
        "/api/agent/requests",
        headers=agent_header(),
        json={"media_type": "movie", "title": "The Thing", "year": 1982},
    )
    assert create.status_code == 200
    assert create.json()["request"]["exclude_from_learning"] is True
    request_id = create.json()["request"]["id"]

    execute = client.post(
        f"/api/agent/requests/{request_id}/execute?allow_arr_fallback=true",
        headers=agent_header(),
    )
    assert execute.status_code == 200
    row = db_session.query(ManualRequest).filter(ManualRequest.id == request_id).one()
    assert row.status == "submitted_to_arr"
    assert row.exclude_from_learning is True
    assert row.arr_item_id == 42
    assert row.arr_command_id == 99


def test_agent_create_request_defaults_add_to_plex_true(client, db_session):
    response = client.post(
        "/api/agent/requests",
        headers=agent_header(),
        json={"media_type": "movie", "title": "The Thing", "year": 1982},
    )
    assert response.status_code == 200
    row = (
        db_session.query(ManualRequest)
        .filter(ManualRequest.id == response.json()["request"]["id"])
        .one()
    )
    assert row.raw_payload["add_to_plex"] is True


def test_agent_create_request_allows_add_to_plex_false(client, db_session):
    response = client.post(
        "/api/agent/requests",
        headers=agent_header(),
        json={
            "media_type": "movie",
            "title": "The Thing",
            "year": 1982,
            "add_to_plex": False,
        },
    )
    assert response.status_code == 200
    row = (
        db_session.query(ManualRequest)
        .filter(ManualRequest.id == response.json()["request"]["id"])
        .one()
    )
    assert row.raw_payload["add_to_plex"] is False


def test_agent_plan_request_reports_missing_sonarr_requirements(client, db_session):
    create = client.post(
        "/api/agent/requests",
        headers=agent_header(),
        json={
            "media_type": "episode",
            "title": "Rick and Morty",
            "season": 4,
            "episode": 6,
            "quality_hint": "1080p",
        },
    )
    request_id = create.json()["request"]["id"]

    response = client.get(
        f"/api/agent/requests/{request_id}/plan",
        headers=agent_header(),
    )
    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["execution_path"] == "sonarr"
    assert plan["executable"] is False
    assert "sonarr_root_folder_path is required" in plan["requirements"]
    assert "sonarr_quality_profile_id is required" in plan["requirements"]
    assert "prowlarr candidate search is unavailable" in plan["warnings"]


def test_agent_request_candidates_ranks_and_filters_results(monkeypatch, client, db_session):
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeSession:
        def request(self, method, url, headers=None, timeout=None, **kwargs):
            if url.endswith("/api/v1/search"):
                return FakeResponse(
                    [
                        ]
                    if kwargs.get("params", {}).get("query") == "Rick and Morty S04E06"
                    else [
                        {
                            "title": "Rick and Morty S04E06 2160p WEB-DL HDR DV",
                            "indexer": "Provider",
                            "size": 12 * 1024**3,
                            "seeders": 5,
                            "leechers": 1,
                            "downloadUrl": "https://example.invalid/4k",
                        },
                        {
                            "title": "Rick and Morty S04E06 1080p WEB-DL English",
                            "indexer": "Provider",
                            "size": 2 * 1024**3,
                            "seeders": 80,
                            "leechers": 20,
                            "downloadUrl": "https://example.invalid/1080",
                            "freeleech": True,
                        },
                        {
                            "title": "Rick and Morty S04E06 2160p WEB-DL English",
                            "indexer": "Provider",
                            "size": 8 * 1024**3,
                            "seeders": 12,
                            "leechers": 8,
                            "downloadUrl": "https://example.invalid/clean-4k",
                        },
                    ]
                )
            raise AssertionError(f"unexpected url {url}")

    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "prowlarr_enabled": True,
        "prowlarr_base_url": "http://prowlarr.local",
        "prowlarr_api_key": "key",
    }
    db_session.add(runtime)
    db_session.commit()

    monkeypatch.setattr(
        "app.main.ArrRequestService",
        lambda db, settings: ArrRequestService(db, settings, session=FakeSession()),
    )

    create = client.post(
        "/api/agent/requests",
        headers=agent_header(),
        json={
            "media_type": "episode",
            "title": "Rick and Morty",
            "season": 4,
            "episode": 6,
            "quality_hint": "1080p",
        },
    )
    request_id = create.json()["request"]["id"]

    response = client.get(
        f"/api/agent/requests/{request_id}/candidates?limit=5",
        headers=agent_header(),
    )
    assert response.status_code == 200
    candidates = response.json()["candidates"]
    assert len(candidates) == 2
    assert candidates[0]["title"] == "Rick and Morty S04E06 1080p WEB-DL English"
    assert all("HDR" not in row["title"] for row in candidates)


def test_agent_execute_episode_request_uses_episode_search(monkeypatch, client, db_session):
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeSession:
        def request(self, method, url, headers=None, timeout=None, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("/api/v3/series"):
                return FakeResponse([{"id": 77, "title": "Rick and Morty", "year": 2013}])
            if url.endswith("/api/v3/episode"):
                return FakeResponse(
                    [
                        {"id": 7001, "seasonNumber": 4, "episodeNumber": 6},
                        {"id": 7002, "seasonNumber": 4, "episodeNumber": 7},
                    ]
                )
            if url.endswith("/api/v3/command"):
                return FakeResponse({"id": 501, "name": "EpisodeSearch"})
            raise AssertionError(f"unexpected url {url}")

    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "sonarr_enabled": True,
        "sonarr_base_url": "http://sonarr.local",
        "sonarr_api_key": "key",
    }
    db_session.add(runtime)
    db_session.commit()

    monkeypatch.setattr(
        "app.main.ArrRequestService",
        lambda db, settings: ArrRequestService(db, settings, session=FakeSession()),
    )

    create = client.post(
        "/api/agent/requests",
        headers=agent_header(),
        json={
            "media_type": "episode",
            "title": "Rick and Morty",
            "season": 4,
            "episode": 6,
        },
    )
    request_id = create.json()["request"]["id"]

    execute = client.post(
        f"/api/agent/requests/{request_id}/execute?allow_arr_fallback=true",
        headers=agent_header(),
    )
    assert execute.status_code == 200
    row = db_session.query(ManualRequest).filter(ManualRequest.id == request_id).one()
    assert row.status == "submitted_to_arr"
    command_call = next(kwargs for method, url, kwargs in calls if url.endswith("/api/v3/command"))
    assert command_call["json"]["name"] == "EpisodeSearch"
    assert command_call["json"]["episodeIds"] == [7001]


def test_agent_select_candidate_submits_exact_manual_candidate(monkeypatch, client, db_session):
    captured = {}
    candidate = ReleaseCandidate(
        title="Rick and Morty S04E06 1080p WEB-DL English",
        tracker="Provider",
        category="tv",
        size_bytes=2 * 1024**3,
        freeleech=True,
        source="manual_request",
        source_confidence=0.8,
        exclude_from_learning=True,
        raw_payload={},
    )
    decision = Decision(
        candidate=candidate,
        action="admit",
        score=3.5,
        threshold=1.1,
        utility_components={},
        bucket_key="manual",
        pressure_snapshot={},
    )
    torrent = Torrent(
        candidate=candidate,
        title=candidate.title,
        info_hash="manual-selected-hash",
        managed=True,
        exclude_from_learning=True,
    )

    class FakeController:
        def __init__(self, settings):
            self.settings = settings

        def intake_candidate(self, db, payload):
            captured["raw_payload"] = payload.raw_payload
            db.add(candidate)
            db.flush()
            decision.candidate_id = candidate.id
            db.add(decision)
            db.flush()
            torrent.candidate_id = candidate.id
            db.add(torrent)
            db.flush()
            return IntakeResponse(
                candidate_id=candidate.id,
                decision_id=decision.id,
                action="admit",
                reason=None,
                score=3.5,
                threshold=1.1,
            )

    monkeypatch.setattr("app.services.manual_requests.ControllerService", FakeController)

    create = client.post(
        "/api/agent/requests",
        headers=agent_header(),
        json={
            "media_type": "episode",
            "title": "Rick and Morty",
            "season": 4,
            "episode": 6,
            "quality_hint": "1080p",
        },
    )
    request_id = create.json()["request"]["id"]

    select = client.post(
        f"/api/agent/requests/{request_id}/select-candidate",
        headers=agent_header(),
        json={
            "title": "Rick and Morty S04E06 1080p WEB-DL English",
            "indexer": "Provider",
            "download_url": "https://provider.example/download.php/123/test.torrent",
            "size_bytes": 2 * 1024**3,
            "seeders": 80,
            "leechers": 20,
            "freeleech": True,
            "info_url": "https://provider.example/t/123",
            "resolution": "1080p",
            "language_match": "english",
            "ranking_score": 8.5,
            "rationale": ["preferred resolution 1080p"],
        },
    )
    assert select.status_code == 200
    row = db_session.query(ManualRequest).filter(ManualRequest.id == request_id).one()
    assert row.status == "admitted"
    assert row.execution_path == "transferops_exact_candidate"
    assert row.candidate_id == candidate.id
    assert row.decision_id == decision.id
    assert row.torrent_id == torrent.id
    assert row.chosen_payload["download_url"] == "https://provider.example/download.php/123/test.torrent"
    assert captured["raw_payload"]["save_path"] == r"C:\TransferOps\manual\collections"


def test_agent_select_candidate_forces_immediate_sync(
    monkeypatch, client, db_session
):
    candidate = ReleaseCandidate(
        title="The Thing 1982 1080p BluRay",
        tracker="Provider",
        category="movie",
        size_bytes=2 * 1024**3,
        freeleech=True,
        source="manual_request",
        source_confidence=0.8,
        exclude_from_learning=True,
        raw_payload={},
    )
    decision = Decision(
        candidate=candidate,
        action="admit",
        score=3.5,
        threshold=1.1,
        utility_components={},
        bucket_key="manual",
        pressure_snapshot={},
    )

    class FakeController:
        def __init__(self, settings, qb=None):
            self.settings = settings

        def intake_candidate(self, db, payload):
            db.add(candidate)
            db.flush()
            decision.candidate_id = candidate.id
            db.add(decision)
            db.flush()
            return IntakeResponse(
                candidate_id=candidate.id,
                decision_id=decision.id,
                action="admit",
                reason=None,
                score=3.5,
                threshold=1.1,
            )

        def sync_from_qb(self, db):
            torrent = Torrent(
                candidate_id=candidate.id,
                title=candidate.title,
                info_hash="sync-linked-hash",
                managed=True,
                exclude_from_learning=True,
            )
            db.add(torrent)
            db.flush()
            return 1

    monkeypatch.setattr("app.services.manual_requests.ControllerService", FakeController)

    create = client.post(
        "/api/agent/requests",
        headers=agent_header(),
        json={"media_type": "movie", "title": "The Thing", "year": 1982},
    )
    request_id = create.json()["request"]["id"]

    select = client.post(
        f"/api/agent/requests/{request_id}/select-candidate",
        headers=agent_header(),
        json={
            "title": "The Thing 1982 1080p BluRay",
            "indexer": "Provider",
            "download_url": "https://provider.example/download.php/123/test.torrent",
            "size_bytes": 2 * 1024**3,
            "freeleech": True,
        },
    )
    assert select.status_code == 200
    row = db_session.query(ManualRequest).filter(ManualRequest.id == request_id).one()
    assert row.torrent_id is not None


def test_agent_execute_request_requires_explicit_arr_confirmation(monkeypatch, client, db_session):
    class FakeSession:
        def request(self, method, url, headers=None, timeout=None, **kwargs):  # pragma: no cover
            raise AssertionError("ARR request should not run without explicit confirmation")

    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "radarr_enabled": True,
        "radarr_base_url": "http://radarr.local",
        "radarr_api_key": "key",
    }
    db_session.add(runtime)
    db_session.commit()

    monkeypatch.setattr(
        "app.main.ArrRequestService",
        lambda db, settings: ArrRequestService(db, settings, session=FakeSession()),
    )

    create = client.post(
        "/api/agent/requests",
        headers=agent_header(),
        json={"media_type": "movie", "title": "The Thing", "year": 1982},
    )
    request_id = create.json()["request"]["id"]

    execute = client.post(
        f"/api/agent/requests/{request_id}/execute",
        headers=agent_header(),
    )
    assert execute.status_code == 200
    row = db_session.query(ManualRequest).filter(ManualRequest.id == request_id).one()
    assert row.status == "awaiting_execution"
    assert row.last_error == "arr_fallback_requires_explicit_confirmation"


def test_agent_select_candidate_blocks_banned_terms(monkeypatch, client, db_session):
    class FakeController:
        def __init__(self, settings):
            self.settings = settings

        def intake_candidate(self, db, payload):  # pragma: no cover - should never run
            raise AssertionError("controller should not be called for banned selections")

    monkeypatch.setattr("app.services.manual_requests.ControllerService", FakeController)

    create = client.post(
        "/api/agent/requests",
        headers=agent_header(),
        json={"media_type": "movie", "title": "The Matrix", "year": 1999},
    )
    request_id = create.json()["request"]["id"]

    select = client.post(
        f"/api/agent/requests/{request_id}/select-candidate",
        headers=agent_header(),
        json={
            "title": "The Matrix 1999 UHD BluRay 2160p HDR DV REMUX",
            "indexer": "Provider",
            "download_url": "https://provider.example/download.php/456/test.torrent",
            "size_bytes": 50 * 1024**3,
        },
    )
    assert select.status_code == 200
    row = db_session.query(ManualRequest).filter(ManualRequest.id == request_id).one()
    assert row.status == "rejected"
    assert row.last_error == "blocked_by_banned_term:hdr"


def test_agent_select_candidate_canonicalizes_prowlarr_ipt_proxy_download_url(
    monkeypatch, client, db_session
):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "rss_url": "https://provider.example/t.rss?tp=abc123",
        "rss_default_tracker": "demo",
    }
    db_session.add(runtime)
    db_session.commit()

    captured: dict[str, object] = {}

    class FakeController:
        def __init__(self, settings, qb=None):
            self.settings = settings

        def intake_candidate(self, db, payload):
            captured["download_url"] = payload.download_url
            candidate = ReleaseCandidate(
                title=payload.title,
                tracker=payload.tracker,
                category=payload.category,
                size_bytes=payload.size_bytes,
                freeleech=payload.freeleech,
                source=payload.source,
                source_confidence=payload.source_confidence or 0.0,
                exclude_from_learning=payload.exclude_from_learning,
                download_url=payload.download_url,
                raw_payload=payload.raw_payload,
            )
            db.add(candidate)
            db.flush()
            decision = Decision(
                candidate_id=candidate.id,
                action="admit",
                score=1.0,
                threshold=0.0,
                utility_components={},
                bucket_key="manual",
                pressure_snapshot={},
            )
            db.add(decision)
            db.flush()
            torrent = Torrent(
                candidate_id=candidate.id,
                title=payload.title,
                info_hash="provider-manual-proxy-hash",
                managed=True,
                exclude_from_learning=True,
            )
            db.add(torrent)
            db.flush()
            return IntakeResponse(
                candidate_id=candidate.id,
                decision_id=decision.id,
                action="admit",
                reason=None,
                score=1.0,
                threshold=0.0,
            )

        def sync_from_qb(self, db):
            return 1

    monkeypatch.setattr("app.services.manual_requests.ControllerService", FakeController)

    create = client.post(
        "/api/agent/requests",
        headers=agent_header(),
        json={"media_type": "movie", "title": "Ted", "year": 2012},
    )
    request_id = create.json()["request"]["id"]

    select = client.post(
        f"/api/agent/requests/{request_id}/select-candidate",
        headers=agent_header(),
        json={
            "title": "Ted (2012) 2160p 4K BluRay 5 1-LAMA",
            "indexer": "Provider Manual",
            "download_url": "http://127.0.0.1:9696/2/download?apikey=key&link=encoded",
            "info_url": "https://provider.example/t/5588591",
            "size_bytes": 5410000000,
            "seeders": 20,
            "leechers": 5,
            "freeleech": False,
        },
    )
    assert select.status_code == 200
    assert (
        captured["download_url"]
        == "https://provider.example/download.php/5588591/Ted.2012.2160p.4K.BluRay.5.1.LAMA.torrent?torrent_pass=abc123"
    )


def test_agent_fulfill_media_request_selects_exact_episode(monkeypatch, client, db_session):
    captured = {}
    candidate = ReleaseCandidate(
        title="Rick and Morty S04E06 2160p WEB-DL English",
        tracker="demo",
        category="tv",
        size_bytes=10,
        freeleech=True,
        source="manual_request",
        source_confidence=0.8,
        exclude_from_learning=True,
        raw_payload={},
    )
    decision = Decision(
        action="admit",
        score=4.0,
        threshold=1.0,
        utility_components={},
        rejection_reason=None,
        bucket_key="manual",
        pressure_snapshot={},
    )
    torrent = Torrent(
        title=candidate.title,
        info_hash="fulfillhash1",
        state="hot",
        managed=True,
        exclude_from_learning=True,
    )

    class FakeController:
        def __init__(self, settings):
            self.settings = settings

        def intake_candidate(self, db, payload):
            captured["title"] = payload.title
            db.add(candidate)
            db.flush()
            decision.candidate_id = candidate.id
            db.add(decision)
            db.flush()
            torrent.candidate_id = candidate.id
            db.add(torrent)
            db.flush()
            return IntakeResponse(
                candidate_id=candidate.id,
                decision_id=decision.id,
                action="admit",
                reason=None,
                score=4.0,
                threshold=1.0,
            )

    def fake_plan(self, request):
        return ManualRequestPlan(
            request_id=request.id,
            executable=True,
            execution_path="sonarr",
            payload={},
        )

    def fake_preview(self, request, limit=None):
        return [
            ManualCandidatePreview(
                title="Rick and Morty S04 2160p BluRay Season Pack",
                indexer="Provider",
                size_bytes=30 * 1024**3,
                seeders=120,
                leechers=10,
                freeleech=True,
                download_url="https://provider.example/download.php/pack/test.torrent",
                info_url="https://provider.example/t/pack",
                resolution="2160p",
                language_match="english",
                ranking_score=11.0,
                rationale=["season pack"],
            ),
            ManualCandidatePreview(
                title="Rick and Morty S04E06 2160p WEB-DL English",
                indexer="Provider",
                size_bytes=4 * 1024**3,
                seeders=50,
                leechers=8,
                freeleech=True,
                download_url="https://provider.example/download.php/exact/test.torrent",
                info_url="https://provider.example/t/exact",
                resolution="2160p",
                language_match="english",
                ranking_score=8.0,
                rationale=["exact episode"],
            ),
        ]

    monkeypatch.setattr("app.services.manual_requests.ControllerService", FakeController)
    monkeypatch.setattr(ArrRequestService, "plan_request", fake_plan)
    monkeypatch.setattr(ArrRequestService, "candidate_preview", fake_preview)

    response = client.post(
        "/api/agent/fulfill",
        headers=agent_header(),
        json={
            "media_type": "episode",
            "title": "Rick and Morty",
            "season": 4,
            "episode": 6,
            "preferred_resolutions": ["2160p", "1080p"],
            "preferred_languages": ["english", "spanish"],
            "add_to_plex": True,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["selected_candidate"]["title"] == "Rick and Morty S04E06 2160p WEB-DL English"
    assert captured["title"] == "Rick and Morty S04E06 2160p WEB-DL English"
    row = db_session.query(ManualRequest).filter(ManualRequest.id == data["request"]["id"]).one()
    assert row.status == "admitted"
    assert row.raw_payload["add_to_plex"] is True


def test_agent_fulfill_media_request_returns_no_candidate_for_mismatch(
    monkeypatch,
    client,
    db_session,
):
    def fake_plan(self, request):
        return ManualRequestPlan(
            request_id=request.id,
            executable=True,
            execution_path="sonarr",
            payload={},
        )

    def fake_preview(self, request, limit=None):
        return [
            ManualCandidatePreview(
                title="Rick and Morty S04 1080p Season Pack",
                indexer="Provider",
                size_bytes=20 * 1024**3,
                seeders=80,
                leechers=5,
                freeleech=True,
                download_url="https://provider.example/download.php/season/test.torrent",
                info_url="https://provider.example/t/season",
                resolution="1080p",
                language_match="english",
                ranking_score=9.0,
                rationale=["season pack"],
            )
        ]

    monkeypatch.setattr(ArrRequestService, "plan_request", fake_plan)
    monkeypatch.setattr(ArrRequestService, "candidate_preview", fake_preview)

    response = client.post(
        "/api/agent/fulfill",
        headers=agent_header(),
        json={
            "media_type": "episode",
            "title": "Rick and Morty",
            "season": 4,
            "episode": 6,
            "preferred_resolutions": ["2160p", "1080p"],
            "candidate_limit": 5,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["selected_candidate"] is None
    assert data["message"] == "no candidate matched fulfill constraints"
    row = db_session.query(ManualRequest).filter(ManualRequest.id == data["request"]["id"]).one()
    assert row.status == "failed"
    assert row.last_error == "no_candidate_match"
    assert data["request"]["failure_category"] == "not_found_exact_match"


def test_manual_candidate_preview_uses_configured_prowlarr_manual_indexer_ids(db_session):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "prowlarr_enabled": True,
        "prowlarr_base_url": "http://prowlarr.local",
        "prowlarr_api_key": "key",
        "prowlarr_manual_indexer_ids": "2,5",
    }
    db_session.add(runtime)
    db_session.commit()

    captured = []

    class FakeResponse:
        def json(self):
            return []

    service = ArrRequestService(db_session, Settings.model_validate(runtime.payload))

    def fake_request(base_url, path, api_key, *, params=None, method="GET", json=None):
        captured.append(
            {
                "base_url": base_url,
                "path": path,
                "api_key": api_key,
                "params": params,
                "method": method,
                "json": json,
            }
        )
        return FakeResponse()

    service._request = fake_request  # type: ignore[method-assign]
    request = ManualRequest(
        media_type="episode",
        title="Rick and Morty",
        season=4,
        episode=6,
        freeleech_preferred=True,
        exclude_from_learning=True,
        status="awaiting_execution",
        request_source="test",
        raw_payload={},
    )

    service.candidate_preview(request, limit=5)

    assert captured
    assert all(call["params"]["indexerIds"] == "2,5" for call in captured)


def test_manual_candidate_preview_prefers_exact_episode_match(db_session):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "prowlarr_enabled": True,
        "prowlarr_base_url": "http://prowlarr.local",
        "prowlarr_api_key": "key",
    }
    db_session.add(runtime)
    db_session.commit()

    class FakeResponse:
        def json(self):
            return [
                {
                    "title": "Rick and Morty S04E05 1080p WEB-DL",
                    "indexer": "Provider",
                    "size": 700 * 1024**2,
                    "seeders": 20,
                    "leechers": 0,
                    "downloadUrl": "https://example.invalid/e05.torrent",
                    "guid": "https://example.invalid/e05",
                },
                {
                    "title": "Rick and Morty S04E06 1080p WEB-DL",
                    "indexer": "Provider",
                    "size": 750 * 1024**2,
                    "seeders": 11,
                    "leechers": 0,
                    "downloadUrl": "https://example.invalid/e06.torrent",
                    "guid": "https://example.invalid/e06",
                },
            ]

    service = ArrRequestService(db_session, Settings.model_validate(runtime.payload))
    service._request = lambda *args, **kwargs: FakeResponse()  # type: ignore[method-assign]
    request = ManualRequest(
        media_type="episode",
        title="Rick and Morty",
        season=4,
        episode=6,
        freeleech_preferred=True,
        exclude_from_learning=True,
        status="awaiting_execution",
        request_source="test",
        raw_payload={},
    )

    previews = service.candidate_preview(request, limit=5)

    assert previews[0].title == "Rick and Morty S04E06 1080p WEB-DL"
    assert "exact request match" in previews[0].rationale


def test_completed_manual_movie_queues_library_handoff(db_session, controller):
    candidate = ReleaseCandidate(
        title="Movie 2026 1080p BluRay",
        tracker="demo",
        category="movie",
        size_bytes=10,
        freeleech=True,
        source="manual_request",
        source_confidence=0.8,
        exclude_from_learning=True,
        raw_payload={},
    )
    db_session.add(candidate)
    db_session.flush()
    torrent = Torrent(
        candidate_id=candidate.id,
        title=candidate.title,
        info_hash="movhash1",
        managed=True,
        exclude_from_learning=True,
    )
    db_session.add(torrent)
    db_session.flush()
    request = ManualRequest(
        media_type="movie",
        title="Movie",
        year=2026,
        status="admitted",
        candidate_id=candidate.id,
        torrent_id=torrent.id,
        exclude_from_learning=True,
    )
    db_session.add(request)
    db_session.commit()

    controller.qb.torrents = [
        {
            "hash": "movhash1",
            "name": candidate.title,
            "save_path": r"C:\TransferOps\managed\movies",
            "content_path": r"C:\TransferOps\managed\movies\Movie 2026",
            "size": 10,
            "progress": 1.0,
            "ratio": 0.1,
            "uploaded": 100,
            "downloaded": 10,
            "seeding_time": 60,
            "dlspeed": 0,
            "upspeed": 0,
            "tags": "transferops.transferops",
            "category": "transferops.transferops",
            "state": "uploading",
        }
    ]

    controller.sync_from_qb(db_session)
    db_session.flush()
    handoff = db_session.query(LibraryHandoff).one()
    assert handoff.media_type == "movie"
    assert handoff.status == "waiting_config"
    assert handoff.source_path == r"C:\TransferOps\managed\movies\Movie 2026"


def test_process_library_requests_plex_refresh_for_movie(db_session, tmp_path):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "plex_enabled": True,
        "plex_token": "token",
        "plex_movies_section_id": 7,
        "host_disk_check_path": str(tmp_path),
    }
    db_session.add(runtime)
    handoff = LibraryHandoff(
        media_type="movie",
        target="plex",
        title="Movie",
        source_path=r"C:\TransferOps\managed\movies\Movie",
        status="pending",
    )
    db_session.add(handoff)
    db_session.commit()

    service = LibraryHandoffService(db_session, Settings.model_validate(runtime.payload))

    class FakePlex:
        def refresh_section(self, section_id, path=None):
            from app.services.integrations import IntegrationResult

            return IntegrationResult(True, "ok", {"section_id": section_id, "path": path})

        def find_in_section(self, section_id, title, **_kwargs):
            from app.services.integrations import IntegrationResult

            return IntegrationResult(True, "not found", {})

    service.plex = FakePlex()
    result = service.process_pending()
    db_session.commit()
    db_session.refresh(handoff)

    assert result.ok is True
    assert handoff.status == "scan_requested"
    assert handoff.section_id == 7
    assert handoff.scan_requested_at is not None


def test_process_library_confirms_plex_import_for_movie(db_session, tmp_path):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "plex_enabled": True,
        "plex_token": "token",
        "plex_movies_section_id": 7,
        "host_disk_check_path": str(tmp_path),
    }
    db_session.add(runtime)
    handoff = LibraryHandoff(
        media_type="movie",
        target="plex",
        title="Movie",
        source_path=r"C:\TransferOps\managed\movies\Movie",
        section_id=7,
        status="scan_requested",
    )
    db_session.add(handoff)
    db_session.commit()

    service = LibraryHandoffService(db_session, Settings.model_validate(runtime.payload))

    class FakePlex:
        def find_in_section(self, section_id, title, **kwargs):
            from app.services.integrations import IntegrationResult

            return IntegrationResult(
                True,
                "found",
                {
                    "rating_key": "42",
                    "title": title,
                    "season": kwargs.get("season"),
                    "episode": kwargs.get("episode"),
                },
            )

    service.plex = FakePlex()
    result = service.process_pending()
    db_session.commit()
    db_session.refresh(handoff)

    assert result.ok is True
    assert handoff.status == "completed"
    assert handoff.imported_at is not None
    assert handoff.payload["plex_item"]["rating_key"] == "42"


def test_retry_library_handoff_endpoint_requeues_failed_handoff(
    client, db_session, tmp_path, monkeypatch
):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "plex_enabled": True,
        "plex_token": "token",
        "plex_movies_section_id": 7,
        "host_disk_check_path": str(tmp_path),
    }
    db_session.add(runtime)
    handoff = LibraryHandoff(
        media_type="movie",
        target="plex",
        title="Red Dragon",
        source_path=r"C:\TransferOps\managed\movies\Red Dragon",
        status="failed",
        last_error="plex refresh failed",
    )
    db_session.add(handoff)
    db_session.commit()

    def fake_refresh_section(self, section_id, path=None):
        return IntegrationResult(True, "ok", {"section_id": section_id, "path": path})

    monkeypatch.setattr(PlexService, "refresh_section", fake_refresh_section)

    response = client.post(f"/api/agent/handoffs/{handoff.id}/retry", headers=agent_header())

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["payload"]["handoff_id"] == handoff.id
    assert payload["payload"]["status"] == "scan_requested"
    db_session.refresh(handoff)
    assert handoff.status == "scan_requested"
    assert handoff.last_error is None
    assert handoff.scan_requested_at is not None


def test_retry_library_handoff_endpoint_rejects_non_failed_status(client, db_session):
    handoff = LibraryHandoff(
        media_type="movie",
        target="plex",
        title="Red Dragon",
        status="scan_requested",
    )
    db_session.add(handoff)
    db_session.commit()

    response = client.post(f"/api/agent/handoffs/{handoff.id}/retry", headers=agent_header())

    assert response.status_code == 409
    assert response.json()["detail"] == "only failed library handoffs can be retried"


def test_retry_library_handoff_endpoint_returns_result_when_retry_fails(
    client, db_session, tmp_path, monkeypatch
):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "plex_enabled": True,
        "plex_token": "token",
        "plex_movies_section_id": 7,
        "host_disk_check_path": str(tmp_path),
    }
    db_session.add(runtime)
    handoff = LibraryHandoff(
        media_type="movie",
        target="plex",
        title="Red Dragon",
        source_path=r"C:\TransferOps\managed\movies\Red Dragon",
        status="failed",
        last_error="plex refresh failed",
    )
    db_session.add(handoff)
    db_session.commit()

    def fake_refresh_section(self, section_id, path=None):
        return IntegrationResult(False, "plex still down", {"section_id": section_id, "path": path})

    monkeypatch.setattr(PlexService, "refresh_section", fake_refresh_section)

    response = client.post(f"/api/agent/handoffs/{handoff.id}/retry", headers=agent_header())

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["payload"]["handoff_id"] == handoff.id
    assert payload["payload"]["status"] == "failed"
    assert payload["payload"]["last_error"] == "plex still down"


def test_plex_find_in_section_accepts_json_payload(db_session):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "plex_enabled": True,
        "plex_token": "token",
        "plex_movies_section_id": 7,
    }
    db_session.add(runtime)
    db_session.commit()

    class FakeResponse:
        status_code = 200
        text = (
            '{"MediaContainer":{"Metadata":['
            '{"ratingKey":"314","title":"Ted","year":2012,"type":"movie"}'
            "]}}"
        )
        headers = {"content-type": "application/json"}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "MediaContainer": {
                    "Metadata": [
                        {"ratingKey": "314", "title": "Ted", "year": 2012, "type": "movie"}
                    ]
                }
            }

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    plex = PlexService(Settings.model_validate(runtime.payload), session=FakeSession())
    result = plex.find_in_section(7, "Ted", year=2012)

    assert result.ok is True
    assert result.payload == {"rating_key": "314", "title": "Ted"}


def test_completed_manual_request_with_add_to_plex_false_does_not_queue_handoff(
    db_session,
    tmp_path,
):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "plex_enabled": True,
        "plex_token": "token",
        "plex_movies_section_id": 7,
        "host_disk_check_path": str(tmp_path),
    }
    db_session.add(runtime)
    candidate = ReleaseCandidate(
        title="Movie 2026 1080p BluRay",
        tracker="demo",
        category="movie",
        size_bytes=10,
        freeleech=True,
        source="manual_request",
        source_confidence=0.8,
        exclude_from_learning=True,
        raw_payload={},
    )
    db_session.add(candidate)
    db_session.flush()
    torrent = Torrent(
        candidate_id=candidate.id,
        title="Movie 2026 1080p BluRay",
        info_hash="movie-no-plex",
        save_path=r"C:\TransferOps\managed\movies\Movie 2026",
        progress=1.0,
        managed=True,
        exclude_from_learning=True,
    )
    db_session.add(torrent)
    db_session.flush()
    request = ManualRequest(
        media_type="movie",
        title="Movie",
        candidate_id=candidate.id,
        torrent_id=torrent.id,
        status="admitted",
        raw_payload={"add_to_plex": False},
    )
    db_session.add(request)
    db_session.commit()

    service = LibraryHandoffService(db_session, Settings.model_validate(runtime.payload))
    service.observe_completed_torrent(torrent)
    db_session.commit()

    assert db_session.query(LibraryHandoff).count() == 0


def test_process_library_confirm_for_episode_passes_episode_context(db_session, tmp_path):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "plex_enabled": True,
        "plex_token": "token",
        "plex_series_section_id": 2,
        "host_disk_check_path": str(tmp_path),
    }
    db_session.add(runtime)
    handoff = LibraryHandoff(
        media_type="series",
        target="plex",
        title="Rick and Morty",
        source_path=r"C:\TransferOps\managed\series\Rick and Morty",
        section_id=2,
        status="scan_requested",
        payload={"request_season": 4, "request_episode": 6},
    )
    db_session.add(handoff)
    db_session.commit()

    service = LibraryHandoffService(db_session, Settings.model_validate(runtime.payload))
    captured = {}

    class FakePlex:
        def find_in_section(self, section_id, title, **kwargs):
            from app.services.integrations import IntegrationResult

            captured["section_id"] = section_id
            captured["title"] = title
            captured.update(kwargs)
            return IntegrationResult(True, "found", {"rating_key": "ep-46", "title": title})

    service.plex = FakePlex()
    result = service.process_pending()
    db_session.commit()
    db_session.refresh(handoff)

    assert result.ok is True
    assert captured["season"] == 4
    assert captured["episode"] == 6
    assert handoff.status == "completed"


def test_tv_priority_frontier_rolls_forward(db_session, tmp_path):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    service = LibraryHandoffService(db_session, Settings.model_validate(runtime.payload))

    candidate = ReleaseCandidate(
        title="Show S01E01 1080p",
        tracker="demo",
        category="tv",
        size_bytes=10,
        freeleech=True,
        source="manual_request",
        source_confidence=0.8,
        exclude_from_learning=True,
        raw_payload={},
    )
    db_session.add(candidate)
    db_session.flush()
    torrent = Torrent(
        candidate_id=candidate.id,
        title=candidate.title,
        info_hash="showhash1",
        managed=True,
        exclude_from_learning=True,
        progress=1.0,
    )
    db_session.add(torrent)
    db_session.flush()
    request = ManualRequest(
        media_type="episode",
        title="Show",
        season=1,
        episode=1,
        candidate_id=candidate.id,
        torrent_id=torrent.id,
        exclude_from_learning=True,
        chosen_payload={"title": "Show S01E01 1080p"},
    )
    db_session.add(request)
    db_session.commit()

    service.observe_completed_torrent(torrent)
    db_session.flush()
    db_session.commit()
    rows = db_session.query(SeriesEpisodeProgress).all()
    assert len(rows) == 1
    frontier = service.tv_priority_frontier()
    assert frontier[0]["season"] == 1
    assert frontier[0]["episode"] == 2


def test_tv_season_pack_marks_whole_season(monkeypatch, db_session):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    service = LibraryHandoffService(db_session, Settings.model_validate(runtime.payload))
    monkeypatch.setattr(service.metadata, "season_episode_numbers", lambda title, season: [1, 2, 3])

    candidate = ReleaseCandidate(
        title="Pack Show S01 1080p",
        tracker="demo",
        category="tv",
        size_bytes=10,
        freeleech=True,
        source="manual_request",
        source_confidence=0.8,
        exclude_from_learning=True,
        raw_payload={},
    )
    db_session.add(candidate)
    db_session.flush()
    torrent = Torrent(
        candidate_id=candidate.id,
        title=candidate.title,
        info_hash="packhash1",
        managed=True,
        exclude_from_learning=True,
        progress=1.0,
    )
    db_session.add(torrent)
    db_session.flush()
    request = ManualRequest(
        media_type="series",
        title="Pack Show",
        season=1,
        candidate_id=candidate.id,
        torrent_id=torrent.id,
        exclude_from_learning=True,
        chosen_payload={"title": "Pack Show S01 1080p"},
    )
    db_session.add(request)
    db_session.commit()

    service.observe_completed_torrent(torrent)
    db_session.flush()
    db_session.commit()
    rows = (
        db_session.query(SeriesEpisodeProgress)
        .order_by(SeriesEpisodeProgress.episode.asc())
        .all()
    )
    assert [row.episode for row in rows] == [1, 2, 3]
    frontier = service.tv_priority_frontier()
    assert frontier[0]["season"] == 2
    assert frontier[0]["episode"] == 1


def test_agent_library_endpoints(client, db_session):
    db_session.add(
        LibraryHandoff(
            media_type="movie",
            target="plex",
            title="Movie",
            status="pending",
            priority_score=50,
        )
    )
    db_session.add(
        SeriesEpisodeProgress(
            series_title="Show",
            normalized_series_title="show",
            season=1,
            episode=1,
            status="downloaded",
        )
    )
    db_session.commit()

    handoffs = client.get("/api/agent/library-handoffs", headers=agent_header())
    priorities = client.get("/api/agent/tv-priorities", headers=agent_header())
    assert handoffs.status_code == 200
    assert len(handoffs.json()) == 1
    assert priorities.status_code == 200


def test_manual_workbench_preview(client, monkeypatch):
    def fake_preview(self, payload):
        plan = ManualRequestPlan(
            request_id=0,
            executable=True,
            execution_path="sonarr",
            payload={"lookup_term": "Show S01E01"},
        )
        candidates = [
            ManualCandidatePreview(
                title="Show S01E01 1080p WEB-DL English",
                indexer="Provider",
                size_bytes=2 * 1024**3,
                seeders=80,
                leechers=12,
                freeleech=True,
                download_url="https://provider.example/download.php/1/test.torrent",
                info_url="https://provider.example/t/1",
                resolution="1080p",
                language_match="english",
                ranking_score=8.3,
                rationale=["exact episode"],
            )
        ]
        return plan, candidates

    monkeypatch.setattr(ArrRequestService, "preview_request", fake_preview)
    response = client.post(
        "/api/manual/workbench/preview",
        headers=auth_header(),
        json={"media_type": "episode", "title": "Show", "season": 1, "episode": 1},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["plan"]["execution_path"] == "sonarr"
    assert data["candidates"][0]["title"] == "Show S01E01 1080p WEB-DL English"


def test_manual_candidate_preview_allows_dvdrip_titles_when_dv_is_banned(db_session, tmp_path):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {
        **runtime.payload,
        "prowlarr_enabled": True,
        "prowlarr_base_url": "http://prowlarr.local",
        "prowlarr_api_key": "key",
        "host_disk_check_path": str(tmp_path),
    }
    db_session.add(runtime)
    db_session.commit()

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "title": "Movie 2006 DVDRip x264",
                    "indexer": "Provider",
                    "size": 700 * 1024**2,
                    "seeders": 50,
                    "leechers": 5,
                    "downloadUrl": "https://provider.example/download.php/1/test.torrent",
                    "guid": "https://provider.example/t/1",
                }
            ]

    service = ArrRequestService(db_session, Settings.model_validate(runtime.payload))
    service.session.request = lambda *args, **kwargs: FakeResponse()
    request = ManualRequest(
        media_type="movie",
        title="Movie",
        year=2006,
        freeleech_preferred=True,
        exclude_from_learning=True,
    )

    previews = service.candidate_preview(request)

    assert len(previews) == 1
    assert previews[0].title == "Movie 2006 DVDRip x264"


def test_agent_manual_request_includes_timeline_and_handoff(client, db_session):
    request = ManualRequest(
        media_type="movie",
        title="Movie",
        status="failed",
        execution_path="transferops_exact_candidate",
        exclude_from_learning=True,
        last_error="blocked_by_banned_term:hdr",
        chosen_payload={"title": "Movie 2160p HDR"},
    )
    db_session.add(request)
    db_session.flush()
    torrent = Torrent(
        title="Movie 1080p BluRay",
        info_hash="timeline-hash",
        state="hot",
        progress=0.5,
        managed=True,
        exclude_from_learning=True,
    )
    db_session.add(torrent)
    db_session.flush()
    request.torrent_id = torrent.id
    db_session.add(request)
    handoff = LibraryHandoff(
        manual_request_id=request.id,
        media_type="movie",
        target="plex",
        title="Movie",
        source_path=r"C:\TransferOps\managed\movies\Movie",
        status="scan_requested",
    )
    db_session.add(handoff)
    db_session.commit()

    response = client.get("/api/agent/manual-requests", headers=agent_header())
    assert response.status_code == 200
    row = response.json()[0]
    assert row["failure_category"] == "blocked_by_preferences"
    assert row["linked_torrent"]["id"] == torrent.id
    assert row["library_handoff"]["status"] == "scan_requested"
    assert row["library_handoff"]["phase"] == "refresh_requested"
    assert any(step["step"] == "plex_handoff" for step in row["timeline"])
