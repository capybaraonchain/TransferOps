from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Alert, ControllerEvent, ExecutorState, Torrent, TorrentState

TAG_BY_STATE = {
    TorrentState.hot.value: "transferops.hot",
    TorrentState.must_keep.value: "transferops.mustkeep",
    TorrentState.safe_anchor.value: "transferops.safe",
    TorrentState.retirable.value: "transferops.retirable",
    TorrentState.error.value: "transferops.error",
}


class LifecycleReconciler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _managed_tags(self, state: str, existing_tags: str = "") -> str:
        tags: list[str] = []
        for tag in existing_tags.split(","):
            normalized = tag.strip()
            if (
                not normalized
                or normalized in TAG_BY_STATE.values()
                or normalized == "transferops.anchor"
            ):
                continue
            if normalized not in tags:
                tags.append(normalized)
        if self.settings.qbit_tag and self.settings.qbit_tag not in tags:
            tags.append(self.settings.qbit_tag)
        state_tags = TAG_BY_STATE.get(state, "")
        for tag in state_tags.split(","):
            normalized = tag.strip()
            if normalized and normalized not in tags:
                tags.append(normalized)
        if state == TorrentState.safe_anchor.value and "transferops.anchor" not in tags:
            tags.append("transferops.anchor")
        return ",".join(tags)

    def is_safe(self, torrent: Torrent) -> bool:
        ratio_target = self.settings.tracker_safe_ratio - self.settings.tracker_safe_grace_ratio
        hour_target = self.settings.tracker_safe_hours - self.settings.tracker_safe_grace_hours
        return torrent.ratio >= ratio_target or torrent.seed_time_seconds / 3600 >= hour_target

    def reconcile(self, db: Session) -> dict[str, int | bool]:
        torrents = (
            db.query(Torrent)
            .filter(
                Torrent.managed.is_(True),
                or_(
                    Torrent.executor_state == ExecutorState.confirmed.value,
                    Torrent.executor_state.is_(None),
                ),
            )
            .all()
        )
        safe_candidates: list[Torrent] = []
        emergency = False

        for torrent in torrents:
            was_safe = self.is_safe(torrent)
            if was_safe and torrent.safely_seeded_at is None:
                torrent.safely_seeded_at = datetime.now(UTC).replace(tzinfo=None)

            if torrent.paused and not was_safe:
                emergency = True
                torrent.state = TorrentState.error.value
                db.add(
                    Alert(
                        alert_type="unsafe_paused",
                        severity="critical",
                        message=f"Unsafe torrent paused unexpectedly: {torrent.title}",
                        payload={"info_hash": torrent.info_hash},
                    )
                )
                continue

            if was_safe:
                safe_candidates.append(torrent)
            elif torrent.progress < 1.0:
                torrent.state = TorrentState.hot.value
            else:
                torrent.state = TorrentState.must_keep.value
            torrent.tags = self._managed_tags(torrent.state, torrent.tags)

        safe_candidates.sort(key=lambda t: (t.size_bytes, t.up_speed, t.seed_time_seconds))
        anchors = safe_candidates[: self.settings.anchor_count]
        anchor_ids = {torrent.id for torrent in anchors}
        for torrent in safe_candidates:
            if torrent.id in anchor_ids:
                torrent.state = TorrentState.safe_anchor.value
                torrent.tags = self._managed_tags(TorrentState.safe_anchor.value, torrent.tags)
            else:
                torrent.state = TorrentState.retirable.value
                torrent.retirable_at = datetime.now(UTC).replace(tzinfo=None)
                torrent.tags = self._managed_tags(TorrentState.retirable.value, torrent.tags)
        db.add(
            ControllerEvent(
                event_type="lifecycle_reconcile",
                severity="warning" if emergency else "info",
                message="Lifecycle reconciliation completed",
                payload={"emergency": emergency, "anchors": len(anchors)},
            )
        )
        return {
            "emergency": emergency,
            "anchors": len(anchors),
            "safe": len(safe_candidates),
        }
