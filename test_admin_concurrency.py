"""Admin concurrency contracts using an in-process app and fake Gemini."""

import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Barrier, Event
from time import monotonic
from unittest.mock import patch

import anyio
import httpx
import pytest
from fastapi import FastAPI

with patch.dict("os.environ", LLM_GATEWAY_API_KEY="test", OPENROUTER_API_KEY="sk-or-test"):
    from app import admin, config, gemini_proxy, gemini_store as store

ORIGIN = {"Origin": "http://admin.test"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def admin_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "pool.sqlite"))
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "test-password")
    monkeypatch.setattr(config, "GEMINI_UPSTREAM", "http://gemini.test")
    app = FastAPI()
    app.include_router(admin.router)
    return app


async def login(client: httpx.AsyncClient) -> None:
    response = await client.post("/admin/login", data={"password": "test-password"}, headers=ORIGIN)
    assert response.status_code == 303


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("initial", "probe_status", "action", "expected"),
    [
        ("rejected", 200, "disable", "disabled"),
        ("active", 403, "enable", "active"),
    ],
)
async def test_operator_action_wins_over_pending_probe(
    admin_app: FastAPI, monkeypatch: pytest.MonkeyPatch, initial: str,
    probe_status: int, action: str, expected: str,
) -> None:
    started, release = anyio.Event(), anyio.Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1beta/models"
        started.set()
        await release.wait()
        return httpx.Response(probe_status, json={})

    monkeypatch.setattr(gemini_proxy, "transport", httpx.MockTransport(upstream))
    key_id = store.add_key("fake", "local-fake-key")
    assert key_id is not None
    if initial == "rejected":
        store.mark_rejected(key_id, "403")
    endpoint = f"/admin/keys/{key_id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin.test",
    ) as client:
        await login(client)

        async def probe() -> None:
            response = await client.post(f"{endpoint}/test", headers=ORIGIN)
            assert response.status_code == 200

        with anyio.fail_after(3):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(probe)
                await started.wait()
                response = await client.post(f"{endpoint}/{action}", headers=ORIGIN)
                assert response.status_code == 303
                release.set()

    row = store.get_key(key_id)
    assert row is not None and row["status"] == expected
    assert bool(store.usable_keys()) == (expected == "active")


def test_disabled_key_ignores_late_health_updates(admin_app: FastAPI) -> None:
    key_id = store.add_key("fake", "local-fake-key")
    assert key_id is not None
    store.mark_cooldown(key_id, "429", 100)
    store.set_status(key_id, "disabled")
    disabled = store.get_key(key_id)
    assert disabled is not None
    store.mark_ok(key_id)
    store.mark_cooldown(key_id, "503", 200)
    store.mark_rejected(key_id, "403")
    assert store.get_key(key_id) == disabled


@pytest.mark.anyio
async def test_admin_write_returns_503_promptly_under_sqlite_contention(admin_app: FastAPI) -> None:
    key_id = store.add_key("fake", "local-fake-key")
    assert key_id is not None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app, raise_app_exceptions=False),
        base_url="http://admin.test",
    ) as client:
        await login(client)
        with closing(sqlite3.connect(config.DB_PATH)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            start = monotonic()
            response = await client.post(f"/admin/keys/{key_id}/disable", headers=ORIGIN)
            assert monotonic() - start < 2
            assert response.status_code == 503
        response = await client.post(f"/admin/keys/{key_id}/disable", headers=ORIGIN)
        assert response.status_code == 303


@pytest.mark.anyio
async def test_slow_dashboard_storage_does_not_delay_login_form(
    admin_app: FastAPI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = Event(), Event()
    real_counts = store.pool_counts

    def held_counts() -> dict[str, int]:
        entered.set()
        assert release.wait(timeout=3)
        return real_counts()

    monkeypatch.setattr(store, "pool_counts", held_counts)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://admin.test",
    ) as client:
        await login(client)

        async def dashboard() -> None:
            assert (await client.get("/admin")).status_code == 200

        with anyio.fail_after(2):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(dashboard)
                try:
                    assert await anyio.to_thread.run_sync(entered.wait, 1)
                    assert (await client.get("/admin/login")).status_code == 200
                finally:
                    release.set()


def test_versioned_health_writes_cannot_undo_reenable(admin_app: FastAPI) -> None:
    key_id = store.add_key("fake", "local-fake-key")
    assert key_id is not None
    (attempt,) = store.usable_keys()
    store.set_status(key_id, "disabled")
    store.set_status(key_id, "active")
    enabled = store.get_key(key_id)
    assert enabled is not None
    version = attempt["operator_version"]
    store.mark_ok(key_id, operator_version=version)
    store.mark_cooldown(key_id, "503", 200, operator_version=version)
    store.mark_rejected(key_id, "403", operator_version=version)
    assert store.get_key(key_id) == enabled


@pytest.mark.anyio
async def test_concurrent_first_use_upgrades_legacy_pool_without_losing_keys(admin_app: FastAPI) -> None:
    with closing(sqlite3.connect(config.DB_PATH)) as db, db:
        db.executescript("""
            CREATE TABLE gemini_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL,
                ciphertext BLOB NOT NULL, key_tail TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'active', cooldown_until REAL NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                last_used_at REAL, created_at REAL NOT NULL
            );
            INSERT INTO gemini_keys (label, ciphertext, key_tail, fingerprint, status, created_at)
            VALUES ('legacy', X'00', 'fake', 'legacy', 'disabled', 1);
        """)
    ready = Barrier(2, timeout=2)

    def add(label: str) -> None:
        ready.wait()
        assert store.add_key(label, f"{label}-fake-key") is not None

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(anyio.to_thread.run_sync, add, "first")
        tasks.start_soon(anyio.to_thread.run_sync, add, "second")

    assert {row["label"]: row["status"] for row in store.list_keys()} == {
        "legacy": "disabled", "first": "active", "second": "active",
    }
    assert store.restore_rejected(1, 0) is False
