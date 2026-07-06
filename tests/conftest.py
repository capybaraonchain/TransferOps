from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app, get_controller
from app.models import RuntimeSettings
from app.services.controller import ControllerService


class StubQB:
    def __init__(self) -> None:
        self.added: list[dict] = []
        self.torrents: list[dict] = []
        self.add_error: Exception | None = None
        self.tag_updates: list[tuple[str, list[str]]] = []
        self.category_updates: list[tuple[str, str]] = []

    def add_torrent(self, candidate: dict) -> dict:
        if self.add_error is not None:
            raise self.add_error
        self.added.append(candidate)
        return {"status": "submitted"}

    def get_torrents(self) -> list[dict]:
        return list(self.torrents)

    def set_tags(self, hashes: str, tags: list[str]) -> None:
        self.tag_updates.append((hashes, tags))

    def set_category(self, hashes: str, category: str) -> None:
        self.category_updates.append((hashes, category))


@pytest.fixture()
def db_session(tmp_path) -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    testing_session_local = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    session = testing_session_local()
    session.add(
        RuntimeSettings(
            id=1,
            payload={
                "dashboard_username": "admin",
                "dashboard_password": "secret",
                "agent_api_token": "agent-secret",
                "autobrr_shared_secret": None,
                "dry_run": False,
                "host_disk_check_path": str(tmp_path),
                "metadata_enrichment_enabled": False,
            },
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def controller(db_session: Session, tmp_path) -> ControllerService:
    qb = StubQB()
    settings = {
        "dashboard_username": "admin",
        "dashboard_password": "secret",
        "agent_api_token": "agent-secret",
        "autobrr_shared_secret": None,
        "dry_run": False,
        "host_disk_check_path": str(tmp_path),
        "metadata_enrichment_enabled": False,
        "qbit_save_path": r"C:\TransferOps\managed",
        "qbit_category": "transferops.transferops",
        "qbit_tag": "transferops.transferops",
    }
    db_session.query(RuntimeSettings).filter(RuntimeSettings.id == 1).update({"payload": settings})
    db_session.commit()
    from app.services.settings import SettingsService

    return ControllerService(SettingsService(db_session).resolve(), qb=qb)


@pytest.fixture()
def client(db_session: Session, controller: ControllerService) -> Generator[TestClient, None, None]:
    def override_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_controller] = lambda: controller
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
