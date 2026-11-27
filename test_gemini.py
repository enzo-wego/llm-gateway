"""Gemini proxy, key-pool and request logging contracts."""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from gemini_test_support import GEN, H, KEYS, SECRET, add, app, client, config, env, post, store


def test_startup_without_openrouter(tmp_path):
    child_env = {k: v for k, v in os.environ.items()
                 if not k.startswith(("OPENROUTER", "ANTHROPIC", "LLM_GATEWAY"))}
    child_env.update(LLM_GATEWAY_API_KEY="k", LLM_GATEWAY_SECRET_KEY="s",
                     LLM_GATEWAY_ADMIN_PASSWORD="p", LLM_GATEWAY_DB_PATH=str(tmp_path / "x.db"))
    code = """
import json
from app.main import app
from fastapi.testclient import TestClient

with TestClient(app) as client:
    embed = client.post('/embed', headers={'X-API-Key': 'k'}, json={'texts': ['a']})
    health = client.get('/health')
print(json.dumps({'embed_status': embed.status_code, 'embed': embed.json(),
                  'health_status': health.status_code}))
"""
    out = subprocess.run([sys.executable, "-c", code], env=child_env, capture_output=True,
                         text=True, cwd=Path(__file__).resolve().parent, timeout=10, check=True)
    result = json.loads(out.stdout)
    assert result["embed_status"] == 502
    assert "OPENROUTER_API_KEY is not configured" in result["embed"]["detail"]
    assert result["health_status"] == 200


# ── caller auth ──────────────────────────────────────────────────────────────
def test_caller_auth(client, env):
    add(1)
    assert post(client, headers={}).status_code == 401
    assert post(client, headers={"x-goog-api-key": "nope"}).status_code == 401
    assert post(client, headers={"X-API-Key": SECRET}).status_code == 200
    assert post(client, headers=H).status_code == 200
    assert env.keys_used == [KEYS[0], KEYS[0]]


def test_key_param_dropped_alt_kept(client, env):
    add(1)
    r = client.post(GEN + "?key=SENTINEL&alt=sse", headers=H, content=b"{}")
    assert r.status_code == 200
    [(raw_url, _)] = env.seen
    url = httpx.URL(raw_url)
    assert url.copy_with(query=None) == httpx.URL(
        "http://upstream.test/v1beta/models/gemini-x:generateContent")
    assert url.params.multi_items() == [("alt", "sse")]


def test_unknown_action_404_and_other_actions(client, env):
    add(1)
    assert post(client, "/gemini/v1beta/models/gemini-x:deleteEverything").status_code == 404
    for a in ("embedContent", "batchEmbedContents", "countTokens"):
        assert post(client, f"/gemini/v1beta/models/m:{a}", body=b'{"x":1}').status_code == 200
    assert [u.rsplit(":", 1)[1] for u, _ in env.seen] == ["embedContent", "batchEmbedContents", "countTokens"]


# ── pool behaviour ───────────────────────────────────────────────────────────
@pytest.fixture
def store_clock(monkeypatch):
    clock = SimpleNamespace(now=1_700_000_000.0)
    # Replace only the store's clock reference, not the process-wide time module.
    monkeypatch.setattr(store, "time", SimpleNamespace(time=lambda: clock.now))
    return clock


def test_round_robin(client, env):
    add(3)
    for _ in range(6):
        assert post(client).status_code == 200
    first_cycle = env.keys_used[:3]
    assert set(first_cycle) == set(KEYS[:3])
    assert env.keys_used[3:] == first_cycle


def test_429_cooldown_then_next_key_and_retry_after(client, env, store_clock):
    first_id, second_id = add(2)
    env.script[KEYS[0]] = [(429, {"Retry-After": "7"}, b'{"error":{"status":"RESOURCE_EXHAUSTED"}}')]
    assert post(client).status_code == 200
    assert env.keys_used == [KEYS[0], KEYS[1]]
    k = store.get_key(first_id)
    assert k["status"] == "active" and k["consecutive_failures"] == 1

    store_clock.now += 6
    assert [k["id"] for k in store.usable_keys()] == [second_id]
    env.seen.clear()
    assert post(client).status_code == 200
    assert env.keys_used == [KEYS[1]]

    store_clock.now += 1
    assert {k["id"] for k in store.usable_keys()} == {first_id, second_id}


def test_cooldown_doubles(client, env, store_clock):
    (key_id,) = add(1)
    env.script[KEYS[0]] = [(503, {}, b"{}")]
    assert post(client).status_code == 503
    assert store.get_key(key_id)["cooldown_until"] == store_clock.now + 60

    store_clock.now += 59
    assert store.usable_keys() == []
    assert post(client).status_code == 503  # fallback on the only cooling key
    assert env.keys_used == [KEYS[0], KEYS[0]]
    store_clock.now += 119
    assert store.usable_keys() == []
    store_clock.now += 1
    assert [k["id"] for k in store.usable_keys()] == [key_id]


@pytest.mark.parametrize("status,body", [
    (401, b"{}"), (403, b"{}"),
    (400, b'{"error":{"status":"INVALID_ARGUMENT","details":[{"reason":"API_KEY_INVALID"}]}}'),
])
def test_rejected(client, env, status, body):
    ids = add(2)
    env.script[KEYS[0]] = [(status, {}, body)]
    assert post(client).status_code == 200
    assert store.get_key(ids[0])["status"] == "rejected" and store.get_key(ids[1])["status"] == "active"
    env.seen.clear()
    for _ in range(3):
        post(client)
    assert KEYS[0] not in env.keys_used


def test_other_4xx_returned_without_penalty(client, env):
    ids = add(2)
    env.script[KEYS[0]] = [(400, {}, b'{"error":{"status":"INVALID_ARGUMENT"}}')]
    r = post(client)
    assert r.status_code == 400 and r.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert env.keys_used == [KEYS[0]]
    k = store.get_key(ids[0])
    assert k["status"] == "active" and k["consecutive_failures"] == 0 and k["cooldown_until"] == 0


def test_one_key_429_forwarded_with_retry_after(client, env):
    ids = add(1)
    env.script[KEYS[0]] = [(429, {"Retry-After": "30"}, b'{"error":"slow down"}')]
    r = post(client)
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    assert r.json() == {"error": "slow down"}
    assert len(env.seen) == 1 and store.get_key(ids[0])["cooldown_until"] > time.time()


def test_six_keys_all_503_five_attempts(client, env):
    ids = add(6)
    for k in KEYS[:6]:
        env.script[k] = [(503, {}, b'{"error":"down"}')]
    r = post(client)
    assert r.status_code == 503 and r.json() == {"error": "down"}
    assert len(env.seen) == 5 and len(set(env.keys_used)) == 5
    untouched = [i for i in ids if store.get_key(i)["consecutive_failures"] == 0]
    assert len(untouched) == 1


def test_all_cooling_one_fallback_on_soonest(client, env, store_clock):
    ids = add(3)
    for key_id, delay in zip(ids, (300, 100, 200), strict=True):
        store.mark_cooldown(key_id, "503", delay)
    env.script[KEYS[1]] = [(503, {}, b'{"error":"still down"}')]
    r = post(client)
    assert r.status_code == 503 and r.json() == {"error": "still down"}
    assert env.keys_used == [KEYS[1]]


def test_all_transport_errors_502(client, env):
    add(3)
    for k in KEYS[:3]:
        env.script[k] = [httpx.ConnectError("boom")]
    r = post(client)
    assert r.status_code == 502 and r.json() == {"error": "gemini_upstream_unreachable"}
    assert len(env.seen) == 3
    assert all(k["cooldown_until"] > time.time() for k in store.list_keys())


def test_timeout_then_next_key(client, env):
    add(2)
    env.script[KEYS[0]] = [httpx.ReadTimeout("slow")]
    assert post(client).status_code == 200
    assert env.keys_used == [KEYS[0], KEYS[1]]


def test_empty_or_unusable_pool_503_and_logged(client, env):
    r = post(client)
    assert r.status_code == 503 and r.json() == {"error": "no_gemini_keys"}
    rows = store.recent_requests()
    assert rows[0]["status"] == 503 and rows[0]["attempts"] == 0
    ids = add(2)
    store.set_status(ids[0], "disabled")
    store.mark_rejected(ids[1], "401")
    assert post(client).json() == {"error": "no_gemini_keys"}
    assert env.seen == []


def test_request_log_rows_for_401_404_and_success(client, env):
    add(1)
    post(client, headers={})
    post(client, "/gemini/v1beta/models/m:nope")
    post(client)
    rows = store.recent_requests()
    assert [r["status"] for r in rows] == [200, 404, 401]
    assert rows[0]["prompt_tokens"] == 3 and rows[0]["output_tokens"] == 4 and rows[0]["attempts"] == 1


def test_disabled_without_secret_key(env, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "")
    with TestClient(app) as c:
        assert post(c).status_code == 503
        assert post(c, headers={}).status_code == 401
        assert c.get("/admin").status_code == 503
        assert "gemini_pool" not in c.get("/health").json()


def test_health_pool(client):
    ids = add(3)
    store.set_status(ids[0], "disabled")
    store.mark_rejected(ids[1], "x")
    assert client.get("/health").json()["gemini_pool"] == {
        "total": 3, "healthy": 1, "cooling": 0, "rejected": 1, "disabled": 1}


def test_db_has_no_plaintext(client):
    key_id, _ = add(2)
    assert post(client).status_code == 200
    db = Path(config.DB_PATH)
    wal = db.with_name(db.name + "-wal")
    raw = db.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
    assert b"secretplaintext" not in raw and b"AIzaKEY" not in raw
    key = store.get_key(key_id)
    assert key["key_tail"] == KEYS[0][-4:]
    # Known SHA-256 prefix of the fixed test key, derived independently of the store.
    assert key["fingerprint"] == "9c24300624d4d7b1"


# ── log redaction ────────────────────────────────────────────────────────────
def test_log_redaction(client, env, caplog):
    add(1)
    unknown = "/gemini/v1beta/models/m:nope"
    with caplog.at_level(logging.INFO):
        client.post(GEN + "?key=SENTINEL", content=b"{}")
        client.post(unknown + "?key=SENTINEL", headers=H, content=b"{}")
        client.post(GEN + "?key=SENTINEL&alt=sse", headers=H, content=b"{}")
        client.get("/health?key=SENTINEL")
    access = [r.getMessage().split()[:3] for r in caplog.records if r.name == "gw-access"]
    assert access == [["POST", GEN, "401"], ["POST", unknown, "404"],
                      ["POST", GEN, "200"], ["GET", "/health", "200"]]
    gateway = [r.getMessage() for r in caplog.records
               if r.name in {"gw-access", "llm-gateway"} or r.name.startswith("llm-gateway.")]
    assert all("SENTINEL" not in m and KEYS[0] not in m for m in gateway)
    assert all("SENTINEL" not in u for u, _ in env.seen)




def test_request_body_forwarded_byte_for_byte(client, env):
    add(1)
    body = '{"contents":[{"parts":[{"text":"héllo  \u2603 \n\t"}]}] }'.encode("utf-8") + b"\x00\xff"
    assert post(client, body=body).status_code == 200
    assert env.bodies == [body]
