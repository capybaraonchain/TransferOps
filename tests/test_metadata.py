from app.config import Settings
from app.main import _backfill_release_years
from app.models import MetadataCache, ReleaseCandidate, RuntimeSettings
from app.services.metadata import MetadataResolver


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        for matcher, payload in self.routes:
            if matcher(url, params):
                return _FakeResponse(payload)
        return _FakeResponse({}, status_code=404)


def test_tv_episode_metadata_uses_episode_air_year_and_cache(db_session):
    session = _FakeSession(
        [
            (
                lambda url, params: url.endswith("/search/shows"),
                [
                    {
                        "score": 10,
                        "show": {
                            "id": 5,
                            "name": "Solar Opposites",
                            "premiered": "2020-05-08",
                        },
                    }
                ],
            ),
            (
                lambda url, params: url.endswith("/shows/5/episodebynumber"),
                {"id": 50, "name": "Episode", "airdate": "2026-03-10"},
            ),
        ]
    )
    settings = Settings(metadata_enrichment_enabled=True, metadata_lookup_timeout_seconds=1)
    resolver = MetadataResolver(settings, session=session)
    candidate = ReleaseCandidate(
        title="Solar Opposites S06E06 MULTI XviD-AFG",
        tracker="demo",
        category="tv",
        size_bytes=1,
        freeleech=True,
        source="rss",
        source_confidence=0.65,
        raw_payload={},
    )
    db_session.add(candidate)
    db_session.commit()

    changed = resolver.enrich_release_candidate(db_session, candidate)
    db_session.commit()

    assert changed is True
    assert candidate.release_year == 2026
    assert db_session.query(MetadataCache).one().provider == "tvmaze"

    session.calls.clear()
    changed_again = resolver.enrich_release_candidate(db_session, candidate)
    assert changed_again is False
    assert session.calls == []


def test_tv_season_metadata_uses_latest_episode_year(db_session):
    session = _FakeSession(
        [
            (
                lambda url, params: url.endswith("/search/shows"),
                [
                    {
                        "score": 10,
                        "show": {"id": 8, "name": "Scarpetta", "premiered": "2025-01-01"},
                    }
                ],
            ),
            (
                lambda url, params: url.endswith("/shows/8/episodes"),
                [
                    {"season": 1, "airdate": "2025-11-01"},
                    {"season": 1, "airdate": "2026-01-15"},
                    {"season": 2, "airdate": "2027-01-01"},
                ],
            ),
        ]
    )
    settings = Settings(metadata_enrichment_enabled=True, metadata_lookup_timeout_seconds=1)
    resolver = MetadataResolver(settings, session=session)

    result = resolver.lookup_release_year(db_session, "Scarpetta S01 2160p AMZN WEB-DL")

    assert result is not None
    assert result.release_year == 2026
    assert result.media_type == "tv_season"


def test_movie_metadata_uses_tmdb_when_year_missing(db_session):
    session = _FakeSession(
        [
            (
                lambda url, params: "search/movie" in url,
                {
                    "results": [
                        {
                            "id": 77,
                            "title": "The Thing",
                            "release_date": "1982-06-25",
                        }
                    ]
                },
            )
        ]
    )
    settings = Settings(
        metadata_enrichment_enabled=True,
        metadata_lookup_timeout_seconds=1,
        tmdb_api_key="key",
    )
    resolver = MetadataResolver(settings, session=session)

    result = resolver.lookup_release_year(db_session, "The Thing BluRay x264")

    assert result is not None
    assert result.release_year == 1982
    assert result.provider == "tmdb"


def test_backfill_release_years_uses_metadata_for_tv_titles(monkeypatch, db_session):
    db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).update(
        {
            "payload": {
                **db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).one().payload,
                "metadata_enrichment_enabled": True,
            }
        }
    )
    db_session.add(
        ReleaseCandidate(
            title="Solar Opposites S06E06 MULTI XviD-AFG",
            tracker="demo",
            category="tv",
            size_bytes=1,
            freeleech=True,
            source="rss",
            source_confidence=0.65,
            raw_payload={},
        )
    )
    db_session.commit()

    class FakeResolver:
        def __init__(self, *_args, **_kwargs):
            pass

        def enrich_release_candidate(self, db, candidate):
            candidate.release_year = 2026
            db.add(candidate)
            return True

    monkeypatch.setattr("app.main.MetadataResolver", FakeResolver)
    updated = _backfill_release_years(db_session)
    db_session.commit()

    row = db_session.query(ReleaseCandidate).one()
    assert updated == 1
    assert row.release_year == 2026
