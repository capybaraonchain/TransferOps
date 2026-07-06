from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import DiskBudgetEvent, ExecutorState, Torrent, TorrentState
from app.units import BYTES_PER_GB


@dataclass(slots=True)
class DiskSnapshot:
    managed_usage_bytes: int
    projected_usage_bytes: int
    protocol_usage_bytes: int
    protocol_projected_usage_bytes: int
    manual_usage_bytes: int
    manual_projected_usage_bytes: int
    free_host_disk_bytes: int | None
    pressure: float
    reject_new_admits: bool
    reasons: list[str]


class DiskBudgetManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _managed_rows(self, db: Session) -> list[Torrent]:
        rows = (
            db.query(Torrent)
            .filter(
                Torrent.managed.is_(True),
                or_(
                    Torrent.executor_state == ExecutorState.confirmed.value,
                    Torrent.executor_state.is_(None),
                ),
                Torrent.state != TorrentState.retirable.value,
            )
            .all()
        )
        return rows

    def current_usage_bytes(self, db: Session) -> int:
        return sum(t.size_bytes for t in self._managed_rows(db))

    def current_usage_by_pool(self, db: Session) -> tuple[int, int]:
        protocol = 0
        manual = 0
        for row in self._managed_rows(db):
            if row.exclude_from_learning:
                manual += row.size_bytes
            else:
                protocol += row.size_bytes
        return protocol, manual

    def _host_free_bytes(self) -> int | None:
        path = Path(self.settings.host_disk_check_path)
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def snapshot(
        self,
        db: Session,
        candidate_size_bytes: int = 0,
        candidate_is_manual: bool = False,
    ) -> DiskSnapshot:
        protocol_usage, manual_usage = self.current_usage_by_pool(db)
        managed = protocol_usage + manual_usage
        protocol_projected = protocol_usage + (
            candidate_size_bytes if not candidate_is_manual else 0
        )
        manual_projected = manual_usage + (candidate_size_bytes if candidate_is_manual else 0)
        projected = protocol_projected + manual_projected
        protocol_cap = int(self.settings.managed_disk_cap_gb * BYTES_PER_GB)
        manual_cap = int(self.settings.manual_disk_cap_gb * BYTES_PER_GB)
        reserve = int(self.settings.disk_reserve_gb * BYTES_PER_GB)
        protocol_high_water = int(self.settings.admission_high_water_mark_gb * BYTES_PER_GB)
        manual_high_water = int(
            self.settings.manual_admission_high_water_mark_gb * BYTES_PER_GB
        )
        host_free = self._host_free_bytes()
        reasons: list[str] = []
        relevant_projected = manual_projected if candidate_is_manual else protocol_projected
        relevant_cap = manual_cap if candidate_is_manual else protocol_cap
        relevant_high_water = manual_high_water if candidate_is_manual else protocol_high_water
        pressure = max(0.0, relevant_projected / max(relevant_cap, 1))
        reject = False
        if relevant_projected >= relevant_high_water:
            pressure += 0.4
            reasons.append(
                "manual_high_water_mark"
                if candidate_is_manual
                else "managed_high_water_mark"
            )
        if relevant_projected + reserve > relevant_cap:
            reject = True
            pressure += 0.8
            reasons.append(
                "manual_cap_or_reserve_violation"
                if candidate_is_manual
                else "managed_cap_or_reserve_violation"
            )
        if not Path(self.settings.host_disk_check_path).exists():
            reject = True
            pressure += 0.8
            reasons.append("host_disk_path_missing")
        if host_free is not None and self.settings.minimum_free_host_disk_gb is not None:
            minimum_host = int(self.settings.minimum_free_host_disk_gb * BYTES_PER_GB)
            if host_free < minimum_host:
                reject = True
                pressure += 0.8
                reasons.append("host_disk_below_floor")
        return DiskSnapshot(
            managed_usage_bytes=managed,
            projected_usage_bytes=projected,
            protocol_usage_bytes=protocol_usage,
            protocol_projected_usage_bytes=protocol_projected,
            manual_usage_bytes=manual_usage,
            manual_projected_usage_bytes=manual_projected,
            free_host_disk_bytes=host_free,
            pressure=min(pressure, 2.0),
            reject_new_admits=reject,
            reasons=reasons,
        )

    def persist_event(
        self,
        db: Session,
        snapshot: DiskSnapshot,
        event_type: str,
        message: str,
    ) -> None:
        db.add(
            DiskBudgetEvent(
                event_type=event_type,
                managed_usage_bytes=snapshot.managed_usage_bytes,
                free_host_disk_bytes=snapshot.free_host_disk_bytes,
                projected_usage_bytes=snapshot.projected_usage_bytes,
                message=message,
            )
        )
