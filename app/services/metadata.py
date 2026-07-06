from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import MetadataCache, ReleaseCandidate
from app.services.integrations import extract_year, normalize_title

TV_EPISODE_RE = re.compile(
    r"^(?P<title>.+?)\s+S(?P<season>\d{1,2})E(?P<episode>\d{1,3})(?:\b|[ ._-])",
    re.I,
)
TV_SEASON_RE = re.compile(r"^(?P<title>.+?)\s+S(?P<season>\d{1,2})(?:\b|[ ._-])(?!E\d)", re.I)


@dataclass(slots=True)
class ParsedMetadataQuery:
    media_type: str
    title: str
    normalized_title: str
    season: int | None = None
    episode: int | None = None

    @property
    def cache_key(self) -> str:
        if self.media_type == "tv_episode":
            return f"tv_episode:{self.normalized_title}:{self.season or 0}:{self.episode or 0}"
        if self.media_type == "tv_season":
            return f"tv_season:{self.normalized_title}:{self.season or 0}"
        return f"movie:{self.normalized_title}"


@dataclass(slots=True)
class MetadataResult:
    cache_key: str
    media_type: str
    provider: str
    query_title: str
    normalized_title: str
    release_year: int | None
    series_year: int | None
    confidence: float
    status: str
    resolved_title: str | None = None
    season: int | None = None
    episode: int | None = None
    raw_payload: dict[str, Any] | None = None


class MetadataResolver:
    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()

    def enrich_release_candidate(self, db: Session, candidate: ReleaseCandidate) -> bool:
        if not self.settings.metadata_enrichment_enabled or candidate.release_year is not None:
            return False
        result = self.lookup_release_year(db, candidate.title)
        if result is None or result.release_year is None:
            return False
        candidate.release_year = result.release_year
        db.add(candidate)
        return True

    def lookup_release_year(self, db: Session, title: str) -> MetadataResult | None:
        query = self.parse_title(title)
        if query is None:
            return None
        cached = (
            db.query(MetadataCache)
            .filter(MetadataCache.cache_key == query.cache_key)
            .one_or_none()
        )
        if cached is not None:
            return MetadataResult(
                cache_key=cached.cache_key,
                media_type=cached.media_type,
                provider=cached.provider,
                query_title=cached.query_title,
                normalized_title=cached.normalized_title,
                release_year=cached.release_year,
                series_year=cached.series_year,
                confidence=cached.confidence,
                status=cached.status,
                resolved_title=cached.resolved_title,
                season=cached.season,
                episode=cached.episode,
                raw_payload=cached.raw_payload or {},
            )
        result = self._resolve_query(query)
        payload = result.raw_payload or {}
        row = MetadataCache(
            cache_key=result.cache_key,
            media_type=result.media_type,
            provider=result.provider,
            query_title=result.query_title,
            normalized_title=result.normalized_title,
            season=result.season,
            episode=result.episode,
            resolved_title=result.resolved_title,
            release_year=result.release_year,
            series_year=result.series_year,
            confidence=result.confidence,
            status=result.status,
            raw_payload=payload,
        )
        db.add(row)
        db.flush()
        return result

    def parse_title(self, title: str) -> ParsedMetadataQuery | None:
        episode_match = TV_EPISODE_RE.search(title)
        if episode_match:
            show_title = episode_match.group("title").strip()
            return ParsedMetadataQuery(
                media_type="tv_episode",
                title=show_title,
                normalized_title=normalize_title(show_title),
                season=int(episode_match.group("season")),
                episode=int(episode_match.group("episode")),
            )
        season_match = TV_SEASON_RE.search(title)
        if season_match:
            show_title = season_match.group("title").strip()
            return ParsedMetadataQuery(
                media_type="tv_season",
                title=show_title,
                normalized_title=normalize_title(show_title),
                season=int(season_match.group("season")),
            )
        normalized = normalize_title(title)
        if not normalized:
            return None
        return ParsedMetadataQuery(media_type="movie", title=title, normalized_title=normalized)

    def _resolve_query(self, query: ParsedMetadataQuery) -> MetadataResult:
        if query.media_type == "tv_episode":
            return self._resolve_tv_episode(query)
        if query.media_type == "tv_season":
            return self._resolve_tv_season(query)
        return self._resolve_movie(query)

    def _resolve_tv_episode(self, query: ParsedMetadataQuery) -> MetadataResult:
        show = self._tvmaze_show(query.normalized_title)
        if show is None:
            return self._miss(query, provider="tvmaze")
        episode = self._tvmaze_episode_by_number(show["id"], query.season or 0, query.episode or 0)
        if episode is None:
            return self._miss(query, provider="tvmaze", payload={"show": show})
        year = self._date_year(episode.get("airdate") or episode.get("airstamp"))
        return MetadataResult(
            cache_key=query.cache_key,
            media_type=query.media_type,
            provider="tvmaze",
            query_title=query.title,
            normalized_title=query.normalized_title,
            release_year=year,
            series_year=self._date_year(show.get("premiered")),
            confidence=0.95 if year is not None else 0.0,
            status="resolved" if year is not None else "miss",
            resolved_title=show.get("name"),
            season=query.season,
            episode=query.episode,
            raw_payload={"show": show, "episode": episode},
        )

    def _resolve_tv_season(self, query: ParsedMetadataQuery) -> MetadataResult:
        show = self._tvmaze_show(query.normalized_title)
        if show is None:
            return self._miss(query, provider="tvmaze")
        episodes = self._tvmaze_episodes(show["id"])
        season_years = [
            self._date_year(item.get("airdate") or item.get("airstamp"))
            for item in episodes
            if item.get("season") == query.season
        ]
        season_years = [year for year in season_years if year is not None]
        latest_year = max(season_years) if season_years else None
        return MetadataResult(
            cache_key=query.cache_key,
            media_type=query.media_type,
            provider="tvmaze",
            query_title=query.title,
            normalized_title=query.normalized_title,
            release_year=latest_year,
            series_year=self._date_year(show.get("premiered")),
            confidence=0.8 if latest_year is not None else 0.0,
            status="resolved" if latest_year is not None else "miss",
            resolved_title=show.get("name"),
            season=query.season,
            raw_payload={"show": show, "episodes_considered": len(season_years)},
        )

    def season_episode_numbers(self, title: str, season: int) -> list[int]:
        query = self.parse_title(f"{title} S{season:02d}")
        if query is None:
            return []
        show = self._tvmaze_show(query.normalized_title)
        if show is None:
            return []
        episodes = self._tvmaze_episodes(show["id"])
        numbers = sorted(
            {
                int(item.get("number"))
                for item in episodes
                if item.get("season") == season and item.get("number") is not None
            }
        )
        return numbers

    def _resolve_movie(self, query: ParsedMetadataQuery) -> MetadataResult:
        if not self.settings.tmdb_api_key:
            return self._miss(query, provider="tmdb")
        response = self.session.get(
            "https://api.themoviedb.org/3/search/movie",
            params={"api_key": self.settings.tmdb_api_key, "query": query.title},
            timeout=self.settings.metadata_lookup_timeout_seconds,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        match = self._select_movie_result(query.normalized_title, results)
        if match is None:
            return self._miss(query, provider="tmdb", payload={"results": results[:5]})
        year = self._date_year(match.get("release_date"))
        return MetadataResult(
            cache_key=query.cache_key,
            media_type=query.media_type,
            provider="tmdb",
            query_title=query.title,
            normalized_title=query.normalized_title,
            release_year=year,
            series_year=year,
            confidence=0.85 if year is not None else 0.0,
            status="resolved" if year is not None else "miss",
            resolved_title=match.get("title"),
            raw_payload={"movie": match},
        )

    def _tvmaze_show(self, normalized_title: str) -> dict[str, Any] | None:
        response = self.session.get(
            "https://api.tvmaze.com/search/shows",
            params={"q": normalized_title},
            timeout=self.settings.metadata_lookup_timeout_seconds,
        )
        response.raise_for_status()
        results = response.json()
        if not results:
            return None
        exact = [
            row["show"]
            for row in results
            if normalize_title((row.get("show") or {}).get("name", "")) == normalized_title
        ]
        if exact:
            return exact[0]
        scored = sorted(results, key=lambda row: row.get("score", 0), reverse=True)
        return (scored[0].get("show") if scored else None) or None

    def _tvmaze_episode_by_number(
        self, show_id: int, season: int, episode: int
    ) -> dict[str, Any] | None:
        response = self.session.get(
            f"https://api.tvmaze.com/shows/{show_id}/episodebynumber",
            params={"season": season, "number": episode},
            timeout=self.settings.metadata_lookup_timeout_seconds,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def _tvmaze_episodes(self, show_id: int) -> list[dict[str, Any]]:
        response = self.session.get(
            f"https://api.tvmaze.com/shows/{show_id}/episodes",
            params={"specials": 1},
            timeout=self.settings.metadata_lookup_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def _select_movie_result(
        self, normalized_title: str, results: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        if not results:
            return None
        exact = [
            row
            for row in results
            if normalize_title(row.get("title") or "") == normalized_title
        ]
        if exact:
            return exact[0]
        return results[0]

    def _miss(
        self,
        query: ParsedMetadataQuery,
        provider: str,
        payload: dict[str, Any] | None = None,
    ) -> MetadataResult:
        return MetadataResult(
            cache_key=query.cache_key,
            media_type=query.media_type,
            provider=provider,
            query_title=query.title,
            normalized_title=query.normalized_title,
            release_year=None,
            series_year=extract_year(query.title),
            confidence=0.0,
            status="miss",
            season=query.season,
            episode=query.episode,
            raw_payload=payload or {},
        )

    def _date_year(self, value: str | None) -> int | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).year
        except ValueError:
            match = re.match(r"^(?P<year>\d{4})", value)
            if match:
                return int(match.group("year"))
        return None
