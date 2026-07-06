from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    Alert,
    Decision,
    ExecutorState,
    InboundEvent,
    ManualRequest,
    Observation,
    ReleaseCandidate,
    RuntimeSettings,
    SystemSnapshot,
    Torrent,
    TorrentState,
    WantedItem,
)
from app.services.disk import DiskBudgetManager
from app.services.integrations import WantedSyncService
from app.services.learning import BucketLearner
from app.services.lifecycle import LifecycleReconciler
from app.services.qbittorrent import QBittorrentClient
from app.services.rss import canonicalize_download_url, derive_guid, import_rss, parse_description
from app.services.settings import SettingsService
from app.units import BYTES_PER_GB


def make_candidate(**overrides):
    payload = {
        "title": "Example Release",
        "guid": "abc123",
        "tracker": "demo",
        "category": "movie",
        "size_bytes": 10 * BYTES_PER_GB,
        "freeleech": True,
        "published_at": datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10),
        "seeders": 10,
        "leechers": 40,
        "download_url": "https://example.invalid/file.torrent",
        "info_hash": "abcd",
        "source": "autobrr",
        "raw_payload": {"title": "Example Release"},
    }
    payload.update(overrides)
    return payload


def test_freeleech_candidate_admitted(db_session, controller):
    candidate = controller.normalize_candidate(make_candidate())
    response = controller.intake_candidate(db_session, candidate)
    db_session.commit()
    decision = db_session.query(Decision).one()
    torrent = db_session.query(Torrent).one()
    assert response.action == "admit"
    assert decision.action == "admit"
    assert torrent.save_path == controller.settings.qbit_save_path
    assert controller.settings.qbit_tag in torrent.tags


def test_manual_non_freeleech_candidate_can_be_admitted(db_session, controller):
    candidate = controller.normalize_candidate(
        make_candidate(
            title="Manual Movie 2026 1080p BluRay x264",
            guid="manual-nonfree",
            info_hash="manual-nonfree",
            freeleech=False,
            size_bytes=2 * BYTES_PER_GB,
            seeders=150,
            leechers=120,
            source="manual_request",
            exclude_from_learning=True,
        )
    )
    response = controller.intake_candidate(db_session, candidate)
    db_session.commit()

    decision = db_session.query(Decision).order_by(Decision.id.desc()).first()

    assert decision.rejection_reason != "automation_default_freeleech_only"
    assert response.reason != "automation_default_freeleech_only"


def test_manual_candidate_bypasses_profitability_threshold(db_session, controller):
    controller.settings.base_admission_threshold = 999.0
    candidate = controller.normalize_candidate(
        make_candidate(
            title="Manual Wanted 2012 2160p BluRay",
            guid="manual-threshold-bypass",
            info_hash="manual-threshold-bypass",
            freeleech=False,
            size_bytes=6 * BYTES_PER_GB,
            seeders=2,
            leechers=0,
            source="manual_request",
            exclude_from_learning=True,
        )
    )

    response = controller.intake_candidate(db_session, candidate)
    db_session.commit()

    decision = db_session.query(Decision).order_by(Decision.id.desc()).first()
    assert response.action == "admit"
    assert decision.action == "admit"
    assert decision.rejection_reason is None


def test_dry_run_candidate_does_not_create_torrent_row(db_session, controller):
    runtime = db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one()
    runtime.payload = {**runtime.payload, "dry_run": True}
    db_session.add(runtime)
    db_session.commit()

    controller.settings = SettingsService(db_session).resolve()
    candidate = controller.normalize_candidate(make_candidate(info_hash="dryrun-row"))
    response = controller.intake_candidate(db_session, candidate)
    db_session.commit()

    assert response.action == "dry_run"
    assert db_session.query(Torrent).count() == 0


def test_duplicate_candidate_is_deduplicated_and_upgraded_by_source(db_session, controller):
    rss_candidate = controller.normalize_candidate(
        make_candidate(
            source="rss",
            guid="provider-42",
            info_hash=None,
            freeleech=False,
            seeders=None,
            leechers=None,
            download_url="https://provider.example/t/42",
        )
    )
    autobrr_candidate = controller.normalize_candidate(
        make_candidate(
            source="autobrr",
            guid="provider-42",
            info_hash="dedupe42",
            seeders=25,
            leechers=80,
            download_url="https://example.invalid/42.torrent",
        )
    )

    first = controller.intake_candidate(db_session, rss_candidate)
    second = controller.intake_candidate(db_session, autobrr_candidate)
    db_session.commit()

    candidate = db_session.query(ReleaseCandidate).one()
    events = db_session.query(InboundEvent).order_by(InboundEvent.id).all()
    decisions = db_session.query(Decision).order_by(Decision.id).all()

    assert first.candidate_id == second.candidate_id
    assert db_session.query(ReleaseCandidate).count() == 1
    assert candidate.source == "autobrr"
    assert candidate.source_confidence == 1.0
    assert candidate.info_hash == "dedupe42"
    assert len(events) == 2
    assert events[0].status == "accepted"
    assert events[1].status == "updated"
    assert len(decisions) == 2
    assert decisions[0].action == "reject"
    assert decisions[1].action == "admit"


def test_release_candidate_dedupe_key_is_unique(db_session):
    first = ReleaseCandidate(
        title="Dup One",
        guid="dup1",
        tracker="demo",
        category="movie",
        size_bytes=1,
        freeleech=True,
        dedupe_key="provider:duplicate",
        raw_payload={},
    )
    second = ReleaseCandidate(
        title="Dup Two",
        guid="dup2",
        tracker="demo",
        category="movie",
        size_bytes=1,
        freeleech=True,
        dedupe_key="provider:duplicate",
        raw_payload={},
    )
    db_session.add(first)
    db_session.commit()

    db_session.add(second)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_normalize_candidate_extracts_release_year_from_title(controller):
    candidate = controller.normalize_candidate(
        make_candidate(
            title="Barren Land 2025 1080p BluRay x264-GeneMige",
            info_hash="year-parse",
        )
    )
    assert candidate.release_year == 2025


def test_sync_from_qb_ignores_out_of_scope_torrents(db_session, controller):
    controller.qb.torrents = [
        {
            "hash": "managed1",
            "name": "Managed",
            "save_path": controller.settings.qbit_save_path,
            "category": controller.settings.qbit_category,
            "tags": controller.settings.qbit_tag,
            "size": 10,
            "progress": 1.0,
            "ratio": 0.2,
            "uploaded": 5,
            "downloaded": 10,
            "seeding_time": 60,
            "dlspeed": 0,
            "upspeed": 0,
            "state": "uploading",
        },
        {
            "hash": "other1",
            "name": "Other",
            "save_path": r"D:\downloads",
            "category": "movies",
            "tags": "",
            "size": 500,
            "progress": 1.0,
            "ratio": 2.0,
            "uploaded": 1000,
            "downloaded": 500,
            "seeding_time": 600,
            "dlspeed": 0,
            "upspeed": 0,
            "state": "uploading",
        },
    ]
    synced = controller.sync_from_qb(db_session)
    db_session.commit()
    assert synced == 1
    assert db_session.query(Torrent).count() == 1


def test_sync_from_qb_requires_tag_or_category_when_configured(db_session, controller):
    controller.qb.torrents = [
        {
            "hash": "path-only",
            "name": "Path only",
            "save_path": controller.settings.qbit_save_path,
            "category": "",
            "tags": "",
            "size": 10,
            "progress": 1.0,
            "ratio": 0.2,
            "uploaded": 5,
            "downloaded": 10,
            "seeding_time": 60,
            "dlspeed": 0,
            "upspeed": 0,
            "state": "uploading",
        }
    ]

    synced = controller.sync_from_qb(db_session)
    db_session.commit()

    assert synced == 0
    assert db_session.query(Torrent).count() == 0


def test_sync_from_qb_heals_existing_managed_torrent_scope(db_session, controller):
    torrent = Torrent(
        title="Managed drift",
        info_hash="managed-drift",
        state=TorrentState.hot.value,
        size_bytes=10,
        progress=1.0,
        managed=True,
        tags=controller._managed_tags(TorrentState.hot.value),
        executor_state=ExecutorState.confirmed.value,
    )
    db_session.add(torrent)
    db_session.commit()

    controller.qb.torrents = [
        {
            "hash": "managed-drift",
            "name": "Managed drift",
            "save_path": controller.settings.qbit_save_path,
            "category": "Movie/Xvid",
            "tags": "some.other.tag",
            "size": 10,
            "progress": 1.0,
            "ratio": 0.2,
            "uploaded": 5,
            "downloaded": 10,
            "seeding_time": 60,
            "dlspeed": 0,
            "upspeed": 0,
            "state": "uploading",
        }
    ]

    synced = controller.sync_from_qb(db_session)
    db_session.commit()
    db_session.refresh(torrent)

    assert synced == 1
    assert controller.qb.category_updates == [("managed-drift", controller.settings.qbit_category)]
    assert controller.qb.tag_updates
    healed_hash, healed_tags = controller.qb.tag_updates[0]
    assert healed_hash == "managed-drift"
    assert controller.settings.qbit_tag in healed_tags
    assert torrent.category == controller.settings.qbit_category
    assert controller.settings.qbit_tag in torrent.tags


def test_qb_add_failure_does_not_create_phantom_torrent(db_session, controller):
    controller.qb.add_error = ValueError(
        "download_url does not resolve to a direct torrent payload"
    )
    candidate = controller.normalize_candidate(
        make_candidate(
            guid="provider-43",
            download_url="https://provider.example/t/43",
            info_hash="broken43",
        )
    )

    response = controller.intake_candidate(db_session, candidate)
    db_session.commit()

    decision = db_session.query(Decision).one()
    alert = db_session.query(Alert).one()

    assert response.action == "reject"
    assert decision.action == "reject"
    assert "executor_failure" in (decision.rejection_reason or "")
    assert db_session.query(Torrent).count() == 0
    assert alert.alert_type == "qb_add_failed"


def test_disk_accounting_only_counts_managed(db_session, controller):
    db_session.add(
        Torrent(
            title="managed",
            info_hash="managed",
            state=TorrentState.hot.value,
            size_bytes=10,
            progress=1.0,
            managed=True,
            executor_state=ExecutorState.confirmed.value,
        )
    )
    db_session.add(
        Torrent(
            title="unmanaged",
            info_hash="unmanaged",
            state=TorrentState.hot.value,
            size_bytes=999,
            progress=1.0,
            managed=False,
        )
    )
    db_session.add(
        Torrent(
            title="pending",
            info_hash="pending",
            state=TorrentState.candidate.value,
            size_bytes=999,
            progress=0.0,
            managed=True,
            executor_state=ExecutorState.pending.value,
        )
    )
    db_session.flush()
    assert DiskBudgetManager(controller.settings).current_usage_bytes(db_session) == 10


def test_disk_accounting_splits_protocol_and_manual_pools(db_session, controller):
    db_session.add(
        Torrent(
            title="protocol",
            info_hash="protocol",
            state=TorrentState.hot.value,
            size_bytes=11,
            progress=1.0,
            managed=True,
            exclude_from_learning=False,
            executor_state=ExecutorState.confirmed.value,
        )
    )
    db_session.add(
        Torrent(
            title="manual",
            info_hash="manual",
            state=TorrentState.hot.value,
            size_bytes=22,
            progress=1.0,
            managed=True,
            exclude_from_learning=True,
            executor_state=ExecutorState.confirmed.value,
        )
    )
    db_session.flush()

    protocol, manual = DiskBudgetManager(controller.settings).current_usage_by_pool(db_session)
    assert protocol == 11
    assert manual == 22


def test_disk_usage_uses_host_disk_path(db_session, controller, monkeypatch, tmp_path):
    called = {}

    def fake_disk_usage(path):
        called["path"] = str(path)

        class Usage:
            free = 100 * BYTES_PER_GB

        return Usage()

    controller.settings.host_disk_check_path = str(tmp_path)
    monkeypatch.setattr("app.services.disk.shutil.disk_usage", fake_disk_usage)
    snapshot = DiskBudgetManager(controller.settings).snapshot(db_session)
    assert called["path"] == str(tmp_path)
    assert snapshot.reject_new_admits is False


def test_protocol_and_manual_caps_are_enforced_separately(db_session, controller):
    controller.settings.managed_disk_cap_gb = 1.0
    controller.settings.admission_high_water_mark_gb = 0.9
    controller.settings.manual_disk_cap_gb = 10.0
    controller.settings.manual_admission_high_water_mark_gb = 9.0
    controller.settings.disk_reserve_gb = 0.0

    db_session.add(
        Torrent(
            title="protocol-cap",
            info_hash="protocol-cap",
            state=TorrentState.hot.value,
            size_bytes=int(0.95 * BYTES_PER_GB),
            progress=1.0,
            managed=True,
            exclude_from_learning=False,
            executor_state=ExecutorState.confirmed.value,
        )
    )
    db_session.commit()

    protocol_pressure = controller._current_pressure(
        db_session,
        candidate_size_bytes=int(0.1 * BYTES_PER_GB),
        candidate_is_manual=False,
    )
    manual_pressure = controller._current_pressure(
        db_session,
        candidate_size_bytes=int(0.1 * BYTES_PER_GB),
        candidate_is_manual=True,
    )

    assert protocol_pressure.reject_new_admits is True
    assert "managed_cap_or_reserve_violation" in protocol_pressure.reasons
    assert manual_pressure.reject_new_admits is False


def test_manual_cap_blocks_manual_requests_without_blocking_protocol_pool(db_session, controller):
    controller.settings.managed_disk_cap_gb = 10.0
    controller.settings.admission_high_water_mark_gb = 9.0
    controller.settings.manual_disk_cap_gb = 1.0
    controller.settings.manual_admission_high_water_mark_gb = 0.9
    controller.settings.disk_reserve_gb = 0.0

    db_session.add(
        Torrent(
            title="manual-cap",
            info_hash="manual-cap",
            state=TorrentState.hot.value,
            size_bytes=int(0.95 * BYTES_PER_GB),
            progress=1.0,
            managed=True,
            exclude_from_learning=True,
            executor_state=ExecutorState.confirmed.value,
        )
    )
    db_session.commit()

    manual_pressure = controller._current_pressure(
        db_session,
        candidate_size_bytes=int(0.1 * BYTES_PER_GB),
        candidate_is_manual=True,
    )
    protocol_pressure = controller._current_pressure(
        db_session,
        candidate_size_bytes=int(0.1 * BYTES_PER_GB),
        candidate_is_manual=False,
    )

    assert manual_pressure.reject_new_admits is True
    assert "manual_cap_or_reserve_violation" in manual_pressure.reasons
    assert protocol_pressure.reject_new_admits is False


def test_manual_pressure_uses_manual_obligations_not_protocol_backlog(db_session, controller):
    controller.settings.soft_unresolved_cap = 1
    controller.settings.hard_unresolved_cap = 2
    controller.settings.debt_budget = 1
    controller.settings.manual_soft_unresolved_cap = 8
    controller.settings.manual_hard_unresolved_cap = 12
    controller.settings.manual_debt_budget = 8

    for idx in range(2):
        db_session.add(
            Torrent(
                title=f"protocol-must-keep-{idx}",
                info_hash=f"protocol-must-keep-{idx}",
                state=TorrentState.must_keep.value,
                size_bytes=10,
                progress=1.0,
                managed=True,
                exclude_from_learning=False,
                executor_state=ExecutorState.confirmed.value,
            )
        )
    db_session.commit()

    protocol_pressure = controller._current_pressure(
        db_session,
        candidate_size_bytes=int(0.1 * BYTES_PER_GB),
        candidate_is_manual=False,
    )
    manual_pressure = controller._current_pressure(
        db_session,
        candidate_size_bytes=int(0.1 * BYTES_PER_GB),
        candidate_is_manual=True,
    )

    assert protocol_pressure.reject_new_admits is True
    assert "hard_unresolved_cap" in protocol_pressure.reasons
    assert manual_pressure.reject_new_admits is False
    assert "manual_hard_unresolved_cap" not in manual_pressure.reasons


def test_sync_marks_missing_unsafe_torrent_as_error(db_session, controller):
    torrent = Torrent(
        title="Missing Unsafe",
        info_hash="missing-unsafe",
        state=TorrentState.must_keep.value,
        size_bytes=10,
        progress=1.0,
        ratio=0.2,
        seed_time_seconds=6 * 3600,
        managed=True,
        executor_state=ExecutorState.confirmed.value,
        last_seen_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
    )
    db_session.add(torrent)
    db_session.commit()

    controller.qb.torrents = []
    controller.sync_from_qb(db_session)
    db_session.commit()
    db_session.refresh(torrent)

    alert = db_session.query(Alert).filter(Alert.alert_type == "managed_torrent_missing").one()
    assert torrent.state == TorrentState.error.value
    assert alert.severity == "critical"


def test_sync_removes_safe_missing_torrent_from_managed_scope(db_session, controller):
    torrent = Torrent(
        title="Missing Safe",
        info_hash="missing-safe",
        state=TorrentState.safe_anchor.value,
        size_bytes=10,
        progress=1.0,
        ratio=1.2,
        seed_time_seconds=400 * 3600,
        managed=True,
        executor_state=ExecutorState.confirmed.value,
        last_seen_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
        safely_seeded_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=3),
    )
    db_session.add(torrent)
    db_session.commit()

    controller.qb.torrents = []
    controller.sync_from_qb(db_session)
    db_session.commit()
    db_session.refresh(torrent)

    assert torrent.managed is False
    assert torrent.state == TorrentState.retirable.value


def test_rss_parser_handles_ipt_description_strings():
    size_bytes, category = parse_description("1.12 GB; Movie/HD/Bluray")
    assert size_bytes == int(1.12 * BYTES_PER_GB)
    assert category == "Movie/HD/Bluray"


def test_rss_guid_derivation_and_freeleech_assumption(monkeypatch, db_session):
    service = SettingsService(db_session)
    result = service.update(
        {
            "rss_url": "https://example.invalid/rss",
            "rss_assume_freeleech": True,
            "rss_parse_description": True,
            "rss_default_tracker": "demo",
        }
    )
    db_session.commit()

    class FakeFeed:
        feed = {"title": "Provider"}
        entries = [
            {
                "title": "Example",
                "link": "https://provider.example/download.php/12345/file.torrent",
                "description": "155 MB; TV/Web-DL",
                "published_parsed": (2026, 3, 9, 18, 30, 0, 0, 0, 0),
            }
        ]

    monkeypatch.setattr("app.services.rss.feedparser.parse", lambda url: FakeFeed())
    candidates = import_rss(result.settings)
    assert derive_guid(FakeFeed.entries[0]) == "provider-12345"
    assert candidates[0].freeleech is True
    assert candidates[0].size_bytes > 0
    assert candidates[0].category == "TV/Web-DL"
    assert candidates[0].release_year is None


def test_ipt_info_page_is_canonicalized_to_download_url(db_session):
    settings = (
        SettingsService(db_session)
        .update(
            {
                "rss_url": "https://provider.example/t.rss?tp=abc123",
                "rss_default_tracker": "demo",
            }
        )
        .settings
    )

    assert canonicalize_download_url(
        settings,
        "demo",
        "https://provider.example/t/7259652",
        "Example Release",
    ) == ("https://provider.example/download.php/7259652/Example.Release.torrent?torrent_pass=abc123")


def test_ipt_manual_tracker_name_still_canonicalizes_download_url(db_session):
    settings = (
        SettingsService(db_session)
        .update(
            {
                "rss_url": "https://provider.example/t.rss?tp=abc123",
                "rss_default_tracker": "demo",
            }
        )
        .settings
    )

    assert canonicalize_download_url(
        settings,
        "Provider Manual",
        "https://provider.example/t/7259652",
        "Example Release",
    ) == ("https://provider.example/download.php/7259652/Example.Release.torrent?torrent_pass=abc123")


def test_wanted_match_influences_scoring(db_session, controller):
    db_session.add(
        WantedItem(
            source="radarr",
            item_type="movie",
            title="Example Release",
            normalized_title="example release",
            reason="radarr_monitored",
        )
    )
    db_session.flush()
    candidate = controller.normalize_candidate(make_candidate(info_hash="wanted1", source="rss"))
    response = controller.intake_candidate(db_session, candidate)
    db_session.commit()
    release = db_session.query(ReleaseCandidate).one()
    decision = db_session.query(Decision).one()
    assert release.wanted is True
    assert "radarr" in (release.wanted_reason or "")
    assert decision.utility_components["wanted_adjustment"] > 0
    assert response.action in {"admit", "dry_run"}


def test_wanted_refresh_removes_stale_monitored_items(db_session, controller):
    db_session.add(
        WantedItem(
            source="radarr",
            item_type="movie",
            title="Old Movie",
            normalized_title="old movie",
            year=2024,
            external_id="old-1",
            reason="radarr_monitored",
        )
    )
    db_session.commit()

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"title": "New Movie", "year": 2025, "tmdbId": 2, "monitored": True}]

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    controller.settings.radarr_enabled = True
    controller.settings.radarr_base_url = "http://radarr"
    controller.settings.radarr_api_key = "key"
    result = WantedSyncService(db_session, controller.settings, session=FakeSession()).sync_radarr()
    db_session.commit()

    rows = db_session.query(WantedItem).order_by(WantedItem.id).all()
    assert result.ok is True
    assert [row.title for row in rows] == ["New Movie"]


def test_qbit_add_torrent_uses_savepath_category_and_tag(monkeypatch, controller):
    client = QBittorrentClient(controller.settings)
    called = {}

    def fake_request(method, path, **kwargs):
        called["data"] = kwargs["data"]

        class Response:
            pass

        return Response()

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr(client, "_ensure_auth", lambda: None)
    client.add_torrent(
        {
            "download_url": "https://example.invalid/file.torrent",
            "category": "Movie/Xvid",
        }
    )
    assert called["data"]["savepath"] == controller.settings.qbit_save_path
    assert called["data"]["category"] == controller.settings.qbit_category
    assert called["data"]["tags"] == controller.settings.qbit_tag


def test_lifecycle_never_retires_unsafe_torrent(db_session, controller):
    torrent = Torrent(
        title="unsafe",
        info_hash="unsafe",
        state=TorrentState.must_keep.value,
        size_bytes=10,
        progress=1.0,
        ratio=0.2,
        seed_time_seconds=10 * 3600,
        executor_state=ExecutorState.confirmed.value,
    )
    db_session.add(torrent)
    db_session.flush()
    result = LifecycleReconciler(controller.settings).reconcile(db_session)
    db_session.commit()
    db_session.refresh(torrent)
    assert torrent.state == TorrentState.must_keep.value
    assert result["emergency"] is False


def test_unsafe_paused_triggers_emergency(db_session, controller):
    torrent = Torrent(
        title="paused-unsafe",
        info_hash="paused",
        state=TorrentState.must_keep.value,
        size_bytes=10,
        progress=1.0,
        ratio=0.1,
        seed_time_seconds=1,
        paused=True,
        executor_state=ExecutorState.confirmed.value,
    )
    db_session.add(torrent)
    db_session.flush()
    result = LifecycleReconciler(controller.settings).reconcile(db_session)
    db_session.commit()
    alert = db_session.query(Alert).one()
    assert result["emergency"] is True
    assert alert.alert_type == "unsafe_paused"


def test_pending_placeholder_expiry_does_not_trigger_emergency(db_session, controller):
    torrent = Torrent(
        title="placeholder",
        info_hash="placeholder",
        state=TorrentState.candidate.value,
        size_bytes=12,
        progress=0.0,
        managed=True,
        executor_state=ExecutorState.pending.value,
        executor_deadline_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10),
    )
    db_session.add(torrent)
    db_session.commit()

    controller.qb.torrents = []
    controller.sync_from_qb(db_session)
    db_session.commit()
    db_session.refresh(torrent)

    snapshot = controller.snapshot_model(db_session)
    assert torrent.managed is False
    assert torrent.executor_state == ExecutorState.failed.value
    assert snapshot.emergency_mode is False
    assert db_session.query(Alert).count() == 0


def test_pending_placeholder_with_confirmed_twin_becomes_orphaned(db_session, controller):
    confirmed = Torrent(
        title="Same Title",
        info_hash="real-hash",
        state=TorrentState.hot.value,
        size_bytes=10,
        progress=1.0,
        managed=True,
        executor_state=ExecutorState.confirmed.value,
        last_seen_at=datetime.now(UTC).replace(tzinfo=None),
    )
    placeholder = Torrent(
        title="Same Title",
        info_hash=None,
        state=TorrentState.candidate.value,
        size_bytes=10,
        progress=0.0,
        managed=True,
        executor_state=ExecutorState.pending.value,
        executor_deadline_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10),
    )
    db_session.add_all([confirmed, placeholder])
    db_session.commit()

    controller.qb.torrents = [
        {
            "hash": "real-hash",
            "name": "Same Title",
            "save_path": controller.settings.qbit_save_path,
            "category": controller.settings.qbit_category,
            "tags": controller.settings.qbit_tag,
            "size": 10,
            "progress": 1.0,
            "ratio": 0.2,
            "uploaded": 5,
            "downloaded": 10,
            "seeding_time": 60,
            "dlspeed": 0,
            "upspeed": 0,
            "state": "uploading",
        }
    ]
    controller.sync_from_qb(db_session)
    db_session.commit()
    db_session.refresh(placeholder)

    assert placeholder.managed is False
    assert placeholder.executor_state == ExecutorState.orphaned.value


def test_stale_critical_alert_does_not_force_emergency_mode(db_session, controller):
    db_session.add(
        Alert(
            alert_type="managed_torrent_missing",
            severity="critical",
            message="stale",
            payload={"info_hash": "ghost"},
        )
    )
    db_session.commit()

    pressure = controller._current_pressure(db_session, 0)
    db_session.commit()

    alert = db_session.query(Alert).one()
    assert pressure.emergency_mode is False
    assert alert.active is False


def test_confirmed_error_torrent_still_triggers_emergency_mode(db_session, controller):
    torrent = Torrent(
        title="confirmed-error",
        info_hash="confirmed-error",
        state=TorrentState.error.value,
        size_bytes=10,
        progress=1.0,
        ratio=0.2,
        seed_time_seconds=2 * 3600,
        managed=True,
        executor_state=ExecutorState.confirmed.value,
    )
    db_session.add(torrent)
    db_session.commit()

    pressure = controller._current_pressure(db_session, 0)
    assert pressure.emergency_mode is True


def test_feedback_loop_updates_bucket_stats(db_session, controller):
    candidate = ReleaseCandidate(**make_candidate(info_hash="learn"))
    db_session.add(candidate)
    db_session.flush()
    torrent = Torrent(
        candidate_id=candidate.id,
        title=candidate.title,
        info_hash="learn",
        state=TorrentState.must_keep.value,
        size_bytes=candidate.size_bytes,
        progress=1.0,
        ratio=0.8,
        uploaded_bytes=8 * 1024**3,
        seed_time_seconds=18 * 3600,
    )
    db_session.add(torrent)
    db_session.flush()
    learner = BucketLearner(controller.settings)
    prediction_before = learner.get_prediction(db_session, candidate)
    obs = Observation(
        torrent_id=torrent.id,
        uploaded_bytes=torrent.uploaded_bytes,
        downloaded_bytes=2 * 1024**3,
        ratio=torrent.ratio,
        seed_time_seconds=torrent.seed_time_seconds,
        progress=1.0,
        state="uploading",
        up_speed=500_000,
        dl_speed=0,
        payload={},
    )
    db_session.add(obs)
    learner.update_from_outcome(db_session, torrent, obs)
    db_session.commit()
    prediction_after = learner.get_prediction(db_session, candidate)
    assert prediction_after.sample_count == 1
    assert prediction_after.upload_6h > prediction_before.upload_6h


def test_manual_request_candidates_are_excluded_from_learning(db_session, controller):
    candidate = controller.normalize_candidate(
        make_candidate(
            info_hash="manual-learn",
            guid="manual-learn",
            source="manual_request",
        )
    )
    response = controller.intake_candidate(db_session, candidate)
    db_session.commit()

    release = db_session.query(ReleaseCandidate).one()
    torrent = db_session.query(Torrent).one()
    learner = BucketLearner(controller.settings)
    prediction = learner.get_prediction(db_session, release)

    assert response.action == "admit"
    assert release.exclude_from_learning is True
    assert torrent.exclude_from_learning is True
    assert prediction.sample_count == 0


def test_excluded_torrents_do_not_update_bucket_stats(db_session, controller):
    candidate = ReleaseCandidate(
        **make_candidate(
            info_hash="excluded-learn",
            guid="excluded-learn",
            source="manual_request",
        ),
        exclude_from_learning=True,
    )
    db_session.add(candidate)
    db_session.flush()
    torrent = Torrent(
        candidate_id=candidate.id,
        title=candidate.title,
        info_hash="excluded-learn",
        state=TorrentState.must_keep.value,
        size_bytes=candidate.size_bytes,
        progress=1.0,
        ratio=0.8,
        uploaded_bytes=2 * 1024**3,
        seed_time_seconds=12 * 3600,
        exclude_from_learning=True,
    )
    db_session.add(torrent)
    db_session.flush()
    learner = BucketLearner(controller.settings)
    obs = Observation(
        torrent_id=torrent.id,
        uploaded_bytes=torrent.uploaded_bytes,
        downloaded_bytes=1 * 1024**3,
        ratio=torrent.ratio,
        seed_time_seconds=torrent.seed_time_seconds,
        progress=1.0,
        state="uploading",
        up_speed=100_000,
        dl_speed=0,
        payload={},
    )
    db_session.add(obs)
    stats = learner.update_from_outcome(db_session, torrent, obs)
    db_session.commit()

    assert stats is None
    assert db_session.query(Observation).count() == 1
    assert db_session.query(ReleaseCandidate).one().exclude_from_learning is True
    assert db_session.query(Torrent).one().exclude_from_learning is True


def test_sync_adopts_manual_placeholder_metadata_for_confirmed_qb_torrent(db_session, controller):
    candidate = ReleaseCandidate(
        title="Ted 2012 UNRATED 1080p BluRay x265",
        tracker="Provider Manual",
        category="movie",
        size_bytes=1868310784,
        freeleech=False,
        source="manual_request",
        source_confidence=0.8,
        exclude_from_learning=True,
        raw_payload={},
    )
    db_session.add(candidate)
    db_session.flush()
    placeholder = Torrent(
        candidate_id=candidate.id,
        title="Ted 2012 UNRATED 1080p BluRay x265",
        managed=True,
        exclude_from_learning=True,
        state=TorrentState.error.value,
        executor_state=ExecutorState.orphaned.value,
        size_bytes=1868310784,
    )
    db_session.add(placeholder)
    db_session.flush()
    confirmed = Torrent(
        title="Ted.2012.UNRATED.1080p.BluRay.x265",
        info_hash="confirmed-hash",
        managed=True,
        exclude_from_learning=False,
        state=TorrentState.must_keep.value,
        size_bytes=1868310784,
    )
    db_session.add(confirmed)
    db_session.flush()
    request = ManualRequest(
        media_type="movie",
        title="Ted",
        year=2012,
        exclude_from_learning=True,
        status="admitted",
        request_source="agent",
        raw_payload={"add_to_plex": True},
        candidate_id=candidate.id,
        torrent_id=placeholder.id,
    )
    db_session.add(request)
    db_session.commit()

    controller.qb.torrents = [
        {
            "hash": "confirmed-hash",
            "name": "Ted.2012.UNRATED.1080p.BluRay.x265",
            "save_path": controller.settings.manual_movies_save_path,
            "content_path": controller.settings.manual_movies_save_path,
            "size": 1868310784,
            "progress": 1.0,
            "ratio": 0.1,
            "uploaded": 123,
            "downloaded": 456,
            "seeding_time": 60,
            "dlspeed": 0,
            "upspeed": 0,
            "state": "uploading",
            "category": controller.settings.qbit_category,
            "tags": controller.settings.qbit_tag,
        }
    ]

    synced = controller.sync_from_qb(db_session)
    db_session.commit()

    confirmed = (
        db_session.query(Torrent)
        .filter(Torrent.info_hash == "confirmed-hash")
        .one()
    )
    placeholder = db_session.query(Torrent).filter(Torrent.id == placeholder.id).one()
    request = db_session.query(ManualRequest).filter(ManualRequest.id == request.id).one()

    assert synced == 1
    assert confirmed.exclude_from_learning is True
    assert confirmed.candidate_id == candidate.id
    assert request.torrent_id == confirmed.id
    assert placeholder.managed is False


def test_year_bucket_separates_catalog_and_current_titles(db_session, controller):
    learner = BucketLearner(controller.settings)
    current_candidate = ReleaseCandidate(
        **make_candidate(
            info_hash="current-year",
            title="New Thing 2026 1080p WEB-DL",
            release_year=2026,
        )
    )
    old_candidate = ReleaseCandidate(
        **make_candidate(
            info_hash="old-year",
            title="Old Thing 2010 1080p BluRay",
            release_year=2010,
        )
    )
    db_session.add_all([current_candidate, old_candidate])
    db_session.flush()

    current_bucket = learner.bucket_for_candidate(current_candidate)
    old_bucket = learner.bucket_for_candidate(old_candidate)

    assert current_bucket.size_bucket == old_bucket.size_bucket
    assert current_bucket.age_bucket == old_bucket.age_bucket
    assert current_bucket.swarm_bucket == old_bucket.swarm_bucket
    assert current_bucket.year_bucket == "current"
    assert old_bucket.year_bucket == "deep_catalog"
    assert learner.key_for_definition(current_bucket) != learner.key_for_definition(old_bucket)


def test_bucket_for_candidate_accepts_timezone_aware_published_at(db_session, controller):
    learner = BucketLearner(controller.settings)
    candidate = ReleaseCandidate(
        **make_candidate(
            info_hash="aware-published-at",
            title="Aware Thing 2026 1080p WEB-DL",
            published_at=datetime.now(UTC),
        )
    )
    db_session.add(candidate)
    db_session.flush()

    bucket = learner.bucket_for_candidate(candidate)

    assert bucket.age_bucket == "fresh"


def test_idle_observations_do_not_immediately_count_as_stalls(db_session, controller):
    candidate = ReleaseCandidate(**make_candidate(info_hash="idle-learning"))
    db_session.add(candidate)
    db_session.flush()
    torrent = Torrent(
        candidate_id=candidate.id,
        title=candidate.title,
        info_hash="idle-learning",
        state=TorrentState.must_keep.value,
        size_bytes=candidate.size_bytes,
        progress=1.0,
        ratio=0.4,
        uploaded_bytes=1 * 1024**3,
        seed_time_seconds=8 * 3600,
    )
    db_session.add(torrent)
    db_session.flush()
    old_obs = Observation(
        torrent_id=torrent.id,
        observed_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=4),
        uploaded_bytes=torrent.uploaded_bytes,
        downloaded_bytes=2 * 1024**3,
        ratio=torrent.ratio,
        seed_time_seconds=4 * 3600,
        progress=1.0,
        state="uploading",
        up_speed=0,
        dl_speed=0,
        payload={},
    )
    new_obs = Observation(
        torrent_id=torrent.id,
        observed_at=datetime.now(UTC).replace(tzinfo=None),
        uploaded_bytes=torrent.uploaded_bytes + 256 * 1024**2,
        downloaded_bytes=2 * 1024**3,
        ratio=torrent.ratio,
        seed_time_seconds=8 * 3600,
        progress=1.0,
        state="stalledUP",
        up_speed=0,
        dl_speed=0,
        payload={},
    )
    db_session.add_all([old_obs, new_obs])
    db_session.flush()

    penalty = controller._recent_underperformance_penalty(db_session)
    learner = BucketLearner(controller.settings)
    learner.update_from_outcome(db_session, torrent, new_obs)
    db_session.commit()
    prediction = learner.get_prediction(db_session, candidate)

    assert penalty == 0.0
    assert prediction.stall_probability < 0.5


def test_time_to_safe_updates_only_after_safe(db_session, controller):
    candidate = ReleaseCandidate(**make_candidate(info_hash="safe-learn"))
    db_session.add(candidate)
    db_session.flush()
    torrent = Torrent(
        candidate_id=candidate.id,
        title=candidate.title,
        info_hash="safe-learn",
        state=TorrentState.must_keep.value,
        size_bytes=candidate.size_bytes,
        progress=1.0,
        ratio=0.8,
        uploaded_bytes=4 * 1024**3,
        seed_time_seconds=24 * 3600,
    )
    db_session.add(torrent)
    db_session.flush()
    learner = BucketLearner(controller.settings)

    unsafe_obs = Observation(
        torrent_id=torrent.id,
        observed_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=5),
        uploaded_bytes=torrent.uploaded_bytes,
        downloaded_bytes=2 * 1024**3,
        ratio=torrent.ratio,
        seed_time_seconds=torrent.seed_time_seconds,
        progress=1.0,
        state="uploading",
        up_speed=0,
        dl_speed=0,
        payload={},
    )
    db_session.add(unsafe_obs)
    learner.update_from_outcome(db_session, torrent, unsafe_obs)
    db_session.flush()
    bucket_before_safe = (
        db_session.query(ReleaseCandidate)
        .filter(ReleaseCandidate.id == candidate.id)
        .one()
    )
    prediction_before_safe = learner.get_prediction(db_session, bucket_before_safe)

    torrent.safely_seeded_at = datetime.now(UTC).replace(tzinfo=None)
    safe_obs = Observation(
        torrent_id=torrent.id,
        observed_at=datetime.now(UTC).replace(tzinfo=None),
        uploaded_bytes=torrent.uploaded_bytes + 1,
        downloaded_bytes=2 * 1024**3,
        ratio=1.05,
        seed_time_seconds=48 * 3600,
        progress=1.0,
        state="uploading",
        up_speed=0,
        dl_speed=0,
        payload={},
    )
    db_session.add(safe_obs)
    learner.update_from_outcome(db_session, torrent, safe_obs)
    db_session.commit()
    prediction_after_safe = learner.get_prediction(db_session, candidate)

    assert prediction_before_safe.time_to_safe_hours == 336.0
    assert prediction_after_safe.time_to_safe_hours < prediction_before_safe.time_to_safe_hours


def test_snapshot_model_ignores_stale_persisted_snapshot(db_session, controller):
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
        Torrent(
            title="live",
            info_hash="live",
            state=TorrentState.hot.value,
            size_bytes=100,
            progress=0.5,
            managed=True,
        )
    )
    db_session.commit()

    snapshot = controller.snapshot_model(db_session)
    assert snapshot.hot_count == 1
    assert snapshot.managed_usage_bytes == 100


@pytest.mark.parametrize(
    ("description", "expected_category"),
    [
        ("155 MB; TV/Web-DL", "TV/Web-DL"),
        ("377 MB; Anime", "Anime"),
    ],
)
def test_multiple_ipt_rss_shapes(description, expected_category):
    _, category = parse_description(description)
    assert category == expected_category
