"""The optional pool may degrade without taking the gateway offline."""

import asyncio
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

with patch.dict(os.environ, LLM_GATEWAY_API_KEY=os.environ.get("LLM_GATEWAY_API_KEY", "test")):
    from app import config, gemini_store
    from app.main import app


def test_health_reports_unavailable_pool_without_failing_liveness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    not_a_directory = tmp_path / "blocked"
    not_a_directory.touch()
    monkeypatch.setattr(config, "SECRET_KEY", "test-pool-secret")
    monkeypatch.setattr(config, "DB_PATH", str(not_a_directory / "test.db"))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["gemini_pool"] == {"ready": False}
    assert str(not_a_directory) not in response.text


def test_cold_pool_under_write_lock_does_not_stall_unrelated_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "locked.db"
    monkeypatch.setattr(config, "SECRET_KEY", "test-pool-secret")
    monkeypatch.setattr(config, "DB_PATH", str(db))
    monkeypatch.setattr(config, "API_KEY", "test-caller")
    pool_counts = gemini_store.pool_counts
    probe_finished = Event()

    async def request_while_locked() -> None:
        loop = asyncio.get_running_loop()
        probe_started = asyncio.Event()

        def signal_pool_read() -> dict[str, int]:
            loop.call_soon_threadsafe(probe_started.set)
            try:
                return pool_counts()
            finally:
                probe_finished.set()

        monkeypatch.setattr(gemini_store, "pool_counts", signal_pool_read)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gateway.test",
        ) as client:
            started = loop.time()
            health = asyncio.create_task(client.get("/health"))
            try:
                await asyncio.wait_for(probe_started.wait(), timeout=2)
                embed = await client.post(
                    "/embed", json={"texts": []}, headers={"X-API-Key": "test-caller"},
                )
                response = await asyncio.wait_for(health, timeout=2)
                assert loop.time() - started < 2
                assert embed.status_code == 200
                assert embed.json()["embeddings"] == []
                assert response.status_code == 200
                assert response.json()["gemini_pool"] == {"ready": False}
            finally:
                lock.rollback()
                await asyncio.gather(health, return_exceptions=True)

    with closing(sqlite3.connect(db)) as lock:
        lock.execute("PRAGMA journal_mode=WAL").fetchone()
        lock.execute("CREATE TABLE existing (id INTEGER)")
        lock.execute("BEGIN IMMEDIATE")
        try:
            asyncio.run(request_while_locked())
        finally:
            assert probe_finished.wait(timeout=2)
