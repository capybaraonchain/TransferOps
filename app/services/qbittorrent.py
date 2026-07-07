from __future__ import annotations

from typing import Any

import requests

from app.config import Settings
from app.services.rss import canonicalize_download_url, looks_like_torrent_url


class QBittorrentClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base = settings.qbit_base_url.rstrip("/")
        self.session = requests.Session()
        self._authenticated = False

    def _ensure_auth(self) -> None:
        if self._authenticated:
            return
        response = self.session.post(
            f"{self.base}/api/v2/auth/login",
            data={"username": self.settings.qbit_username, "password": self.settings.qbit_password},
            timeout=self.settings.qbit_timeout_seconds,
        )
        response.raise_for_status()
        if response.text.strip() != "Ok.":
            raise RuntimeError("qBittorrent authentication failed")
        self._authenticated = True

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        self._ensure_auth()
        response = self.session.request(
            method,
            f"{self.base}{path}",
            timeout=self.settings.qbit_timeout_seconds,
            **kwargs,
        )
        if response.status_code == 403:
            self._authenticated = False
            self._ensure_auth()
            response = self.session.request(
                method,
                f"{self.base}{path}",
                timeout=self.settings.qbit_timeout_seconds,
                **kwargs,
            )
        response.raise_for_status()
        return response

    def add_torrent(self, candidate: dict[str, Any]) -> dict[str, Any]:
        download_url = candidate.get("download_url")
        if download_url:
            download_url = canonicalize_download_url(
                self.settings,
                str(candidate.get("tracker") or "unknown"),
                download_url,
                candidate.get("title"),
            )
            if not looks_like_torrent_url(download_url):
                raise ValueError(
                    "download_url does not resolve to a direct transfer payload. "
                    "Use a provider URL that the transfer backend can fetch without browser state."
                )
        payload: dict[str, Any] = {
            "category": self.settings.qbit_category,
            "savepath": candidate.get("save_path") or self.settings.qbit_save_path,
        }
        if download_url:
            payload["urls"] = download_url
        elif candidate.get("magnet_uri"):
            payload["urls"] = candidate["magnet_uri"]
        else:
            raise ValueError("candidate missing download_url or magnet_uri")
        if self.settings.qbit_tag:
            payload["tags"] = self.settings.qbit_tag
        response = self._request("POST", "/api/v2/torrents/add", data=payload)
        body = getattr(response, "text", "").strip().lower()
        if body and body not in {"ok.", "ok"}:
            raise RuntimeError(f"qBittorrent rejected add request: {body}")
        return {"status": "submitted"}

    def get_torrents(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/v2/torrents/info")
        return response.json()

    def set_tags(self, hashes: str, tags: list[str]) -> None:
        self._request(
            "POST",
            "/api/v2/torrents/addTags",
            data={"hashes": hashes, "tags": ",".join(tags)},
        )

    def set_category(self, hashes: str, category: str) -> None:
        self._request(
            "POST",
            "/api/v2/torrents/setCategory",
            data={"hashes": hashes, "category": category},
        )

    def remove_tags(self, hashes: str, tags: list[str]) -> None:
        self._request(
            "POST",
            "/api/v2/torrents/removeTags",
            data={"hashes": hashes, "tags": ",".join(tags)},
        )

    def delete_torrents(self, hashes: str, delete_files: bool = True) -> None:
        self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={"hashes": hashes, "deleteFiles": "true" if delete_files else "false"},
        )

    def pause(self, hashes: str) -> None:
        self._request("POST", "/api/v2/torrents/pause", data={"hashes": hashes})

    def resume(self, hashes: str) -> None:
        self._request("POST", "/api/v2/torrents/resume", data={"hashes": hashes})
