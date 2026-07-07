from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

TRANSFEROPS_BASE_URL = os.getenv("TRANSFEROPS_BASE_URL", "http://127.0.0.1:8000")
TRANSFEROPS_AGENT_TOKEN = os.getenv("TRANSFEROPS_AGENT_TOKEN", "transferops-local-agent")
TRANSFEROPS_MCP_TRANSPORT = os.getenv("TRANSFEROPS_MCP_TRANSPORT", "stdio")
TRANSFEROPS_MCP_MOUNT_PATH = os.getenv("TRANSFEROPS_MCP_MOUNT_PATH")
TRANSFEROPS_MCP_HOST = os.getenv("TRANSFEROPS_MCP_HOST", "127.0.0.1")
TRANSFEROPS_MCP_PORT = int(os.getenv("TRANSFEROPS_MCP_PORT", "8765"))
TRANSFEROPS_MCP_STREAMABLE_HTTP_PATH = os.getenv("TRANSFEROPS_MCP_STREAMABLE_HTTP_PATH", "/mcp")


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


TRANSFEROPS_MCP_STATELESS_HTTP = _env_flag(
    "TRANSFEROPS_MCP_STATELESS_HTTP",
    TRANSFEROPS_MCP_TRANSPORT == "streamable-http",
)

mcp = FastMCP(
    "transferops",
    host=TRANSFEROPS_MCP_HOST,
    port=TRANSFEROPS_MCP_PORT,
    streamable_http_path=TRANSFEROPS_MCP_STREAMABLE_HTTP_PATH,
    stateless_http=TRANSFEROPS_MCP_STATELESS_HTTP,
)


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=TRANSFEROPS_BASE_URL.rstrip("/"),
        headers={"Authorization": f"Bearer {TRANSFEROPS_AGENT_TOKEN}"},
        timeout=20.0,
    )


def _get(path: str) -> dict[str, Any] | list[dict[str, Any]]:
    with _client() as client:
        response = client.get(path)
        response.raise_for_status()
        return response.json()


def _post(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    with _client() as client:
        response = client.post(path, json=payload or {})
        response.raise_for_status()
        return response.json()


@mcp.tool()
def transferops_overview() -> dict[str, Any]:
    return _get("/api/agent/overview")


@mcp.tool()
def transferops_integrations() -> dict[str, Any]:
    return _get("/api/agent/integrations")


@mcp.tool()
def transferops_budget() -> dict[str, Any]:
    result = _get("/api/agent/budget")
    assert isinstance(result, dict)
    return result


@mcp.tool()
def transferops_manual_preview() -> dict[str, Any]:
    result = _get("/api/agent/manual-preview")
    assert isinstance(result, dict)
    return result


@mcp.tool()
def transferops_recommendations() -> list[dict[str, Any]]:
    result = _get("/api/agent/recommendations")
    assert isinstance(result, list)
    return result


@mcp.tool()
def transferops_buckets() -> list[dict[str, Any]]:
    result = _get("/api/agent/buckets")
    assert isinstance(result, list)
    return result


@mcp.tool()
def transferops_metadata() -> dict[str, Any]:
    result = _get("/api/agent/metadata")
    assert isinstance(result, dict)
    return result


@mcp.tool()
def transferops_library_handoffs() -> list[dict[str, Any]]:
    result = _get("/api/agent/library-handoffs")
    assert isinstance(result, list)
    return result


@mcp.tool()
def transferops_tv_priorities() -> list[dict[str, Any]]:
    result = _get("/api/agent/tv-priorities")
    assert isinstance(result, list)
    return result


@mcp.tool()
def transferops_manual_requests() -> list[dict[str, Any]]:
    result = _get("/api/agent/manual-requests")
    assert isinstance(result, list)
    return result


@mcp.tool()
def transferops_run_sync() -> dict[str, Any]:
    return _post("/api/agent/actions/sync")


@mcp.tool()
def transferops_run_reconcile() -> dict[str, Any]:
    return _post("/api/agent/actions/reconcile")


@mcp.tool()
def transferops_prune_retirable(delete_files: bool = True) -> dict[str, Any]:
    suffix = "?delete_files=false" if not delete_files else ""
    return _post(f"/api/agent/actions/prune-retirable{suffix}")


@mcp.tool()
def transferops_refresh_wanted() -> dict[str, Any]:
    return _post("/api/agent/actions/refresh-wanted")


@mcp.tool()
def transferops_test_integrations() -> dict[str, Any]:
    return _post("/api/agent/actions/test-integrations")


@mcp.tool()
def transferops_process_library() -> dict[str, Any]:
    return _post("/api/agent/actions/process-library")


@mcp.tool()
def transferops_retry_handoff(handoff_id: int) -> dict[str, Any]:
    return _post(f"/api/agent/handoffs/{handoff_id}/retry")


@mcp.tool()
def transferops_create_request(
    media_type: str,
    title: str,
    year: int | None = None,
    season: int | None = None,
    episode: int | None = None,
    quality_hint: str | None = None,
    language_hint: str | None = None,
    preferred_source: bool = True,
    queue_library_handoff: bool = True,
    notes: str | None = None,
) -> dict[str, Any]:
    return _post(
        "/api/agent/requests",
        {
            "media_type": media_type,
            "title": title,
            "year": year,
            "season": season,
            "episode": episode,
            "quality_hint": quality_hint,
            "language_hint": language_hint,
            "freeleech_preferred": preferred_source,
            "add_to_plex": queue_library_handoff,
            "notes": notes,
        },
    )


@mcp.tool()
def transferops_fulfill_media_request(
    media_type: str,
    title: str,
    year: int | None = None,
    season: int | None = None,
    episode: int | None = None,
    quality_hint: str | None = None,
    language_hint: str | None = None,
    preferred_source: bool = True,
    notes: str | None = None,
    preferred_resolutions: list[str] | None = None,
    preferred_languages: list[str] | None = None,
    queue_library_handoff: bool = True,
    exact_match_required: bool | None = None,
    candidate_limit: int = 5,
) -> dict[str, Any]:
    return _post(
        "/api/agent/fulfill",
        {
            "media_type": media_type,
            "title": title,
            "year": year,
            "season": season,
            "episode": episode,
            "quality_hint": quality_hint,
            "language_hint": language_hint,
            "freeleech_preferred": preferred_source,
            "notes": notes,
            "preferred_resolutions": preferred_resolutions or [],
            "preferred_languages": preferred_languages or [],
            "add_to_plex": queue_library_handoff,
            "exact_match_required": exact_match_required,
            "candidate_limit": candidate_limit,
        },
    )


@mcp.tool()
def transferops_get_request(request_id: int) -> dict[str, Any]:
    return _get(f"/api/agent/requests/{request_id}")


@mcp.tool()
def transferops_plan_request(request_id: int) -> dict[str, Any]:
    return _get(f"/api/agent/requests/{request_id}/plan")


@mcp.tool()
def transferops_request_candidates(request_id: int, limit: int = 5) -> dict[str, Any]:
    return _get(f"/api/agent/requests/{request_id}/candidates?limit={limit}")


@mcp.tool()
def transferops_select_request_candidate(
    request_id: int,
    title: str,
    indexer: str,
    download_url: str,
    size_bytes: int = 0,
    seeders: int | None = None,
    leechers: int | None = None,
    preferred_source: bool = False,
    info_url: str | None = None,
    resolution: str | None = None,
    language_match: str | None = None,
    ranking_score: float | None = None,
    rationale: list[str] | None = None,
) -> dict[str, Any]:
    return _post(
        f"/api/agent/requests/{request_id}/select-candidate",
        {
            "title": title,
            "indexer": indexer,
            "download_url": download_url,
            "size_bytes": size_bytes,
            "seeders": seeders,
            "leechers": leechers,
            "freeleech": preferred_source,
            "info_url": info_url,
            "resolution": resolution,
            "language_match": language_match,
            "ranking_score": ranking_score,
            "rationale": rationale or [],
        },
    )


@mcp.tool()
def transferops_execute_request(request_id: int, allow_arr_fallback: bool) -> dict[str, Any]:
    suffix = "?allow_arr_fallback=true" if allow_arr_fallback else ""
    return _post(f"/api/agent/requests/{request_id}/execute{suffix}")


if __name__ == "__main__":
    mcp.run(
        transport=TRANSFEROPS_MCP_TRANSPORT,
        mount_path=TRANSFEROPS_MCP_MOUNT_PATH,
    )
