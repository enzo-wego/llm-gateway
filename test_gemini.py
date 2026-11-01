"""Gemini key pool, proxy and admin page. No real key, no network: a fake upstream."""

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time

os.environ.setdefault("LLM_GATEWAY_API_KEY", "test")
os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import admin, config, gemini_proxy, gemini_store as store  # noqa: E402
from app.main import app  # noqa: E402

SECRET = "gw-secret"
H = {"x-goog-api-key": SECRET}
GEN = "/gemini/v1beta/models/gemini-x:generateContent"
KEYS = [f"AIzaKEY{i}-secretplaintext" for i in range(8)]


class Upstream:
    """Scripted fake of generativelanguage.googleapis.com. Records (url, key) per call."""

    def __init__(self):
        self.seen: list[tuple[str, str]] = []
        self.bodies: list[bytes] = []
        self.script: dict[str, list] = {}  # key -> responses (cycled last); default 200

    def __call__(self, req: httpx.Request) -> httpx.Response:
        key = req.headers.get("x-goog-api-key", "")
        self.seen.append((str(req.url), key))
        self.bodies.append(req.content)
        steps = self.script.get(key)
        if steps:
            step = steps.pop(0) if len(steps) > 1 else steps[0]
            if isinstance(step, Exception):
                raise step
            status, hdrs, body = step
            return httpx.Response(status, headers=hdrs, content=body)
        if req.url.path.endswith("/models") and req.method == "GET":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json={"ok": True, "usageMetadata":
                                         {"promptTokenCount": 3, "candidatesTokenCount": 4}})

    @property
    def keys_used(self) -> list[str]:
        return [k for _, k in self.seen]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "s3cret-for-fernet")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin-pw")
    monkeypatch.setattr(config, "API_KEY", SECRET)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "gw.db"))
    monkeypatch.setattr(config, "GEMINI_UPSTREAM", "http://upstream.test")
    up = Upstream()
    monkeypatch.setattr(gemini_proxy, "transport", httpx.MockTransport(up))
    monkeypatch.setattr(gemini_proxy, "_rr", 0)
    admin._fails.clear()
    return up


def add(n: int) -> list[int]:
    return [store.add_key(f"k{i}", KEYS[i]) for i in range(n)]


def post(client, path=GEN, headers=H, body=b'{"contents":[]}'):
    return client.post(path, headers=headers, content=body)


@pytest.fixture
def client(env):
    return TestClient(app)


def row(i):
    return store.get_key(i)


# ── startup without legacy backends ──────────────────────────────────────────
def test_startup_without_openrouter(tmp_path):
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("OPENROUTER", "ANTHROPIC", "LLM_GATEWAY"))}
    env = {**base, "LLM_GATEWAY_API_KEY": "k", "LLM_GATEWAY_SECRET_KEY": "s",
           "LLM_GATEWAY_ADMIN_PASSWORD": "p", "LLM_GATEWAY_DB_PATH": str(tmp_path / "x.db")}
    code = ("from app import main\n"
            "from fastapi.testclient import TestClient\n"
            "c = TestClient(main.app)\n"
            "r = c.post('/embed', headers={'X-API-Key': 'k'}, json={'texts': ['a']})\n"
            "print(r.status_code, r.json())\n"
            "print(c.get('/health').status_code)\n")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.abspath(__file__)))
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert lines[-2].startswith("502") and "OPENROUTER_API_KEY is not configured" in lines[-2]
    assert lines[-1] == "200"


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
    url = env.seen[0][0]
    assert "SENTINEL" not in url and "key=" not in url and "alt=sse" in url
    assert url.startswith("http://upstream.test/v1beta/models/gemini-x:generateContent")


def test_unknown_action_404_and_other_actions(client, env):
    add(1)
    assert post(client, "/gemini/v1beta/models/gemini-x:deleteEverything").status_code == 404
    for a in ("embedContent", "batchEmbedContents", "countTokens"):
        assert post(client, f"/gemini/v1beta/models/m:{a}", body=b'{"x":1}').status_code == 200
    assert [u.rsplit(":", 1)[1] for u, _ in env.seen] == ["embedContent", "batchEmbedContents", "countTokens"]


# ── pool behaviour ───────────────────────────────────────────────────────────
def test_round_robin(client, env):
    add(3)
    for _ in range(6):
        post(client)
    assert env.keys_used == [KEYS[0], KEYS[1], KEYS[2], KEYS[1], KEYS[2], KEYS[0]] or \
        env.keys_used[:3] == KEYS[:3]
    assert sorted(env.keys_used) == sorted(KEYS[:3] * 2)


def test_429_cooldown_then_next_key_and_retry_after(client, env):
    ids = add(2)
    env.script[KEYS[0]] = [(429, {"Retry-After": "7"}, b'{"error":{"status":"RESOURCE_EXHAUSTED"}}')]
    r = post(client)
    assert r.status_code == 200
    assert env.keys_used == [KEYS[0], KEYS[1]]
    k = row(ids[0])
    assert k["status"] == "active" and k["consecutive_failures"] == 1
    assert 5 < k["cooldown_until"] - time.time() <= 7
    env.seen.clear()
    post(client)
    assert env.keys_used == [KEYS[1]]  # cooling key skipped


def test_cooldown_doubles(client, env):
    ids = add(1)
    env.script[KEYS[0]] = [(503, {}, b"{}")]
    post(client)
    first = row(ids[0])["cooldown_until"] - time.time()
    post(client)  # fallback attempt on the cooling key
    second = row(ids[0])["cooldown_until"] - time.time()
    assert 55 < first <= 60 and 115 < second <= 120


@pytest.mark.parametrize("status,body", [
    (401, b"{}"), (403, b"{}"),
    (400, b'{"error":{"status":"INVALID_ARGUMENT","details":[{"reason":"API_KEY_INVALID"}]}}'),
])
def test_rejected(client, env, status, body):
    ids = add(2)
    env.script[KEYS[0]] = [(status, {}, body)]
    assert post(client).status_code == 200
    assert row(ids[0])["status"] == "rejected" and row(ids[1])["status"] == "active"
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
    k = row(ids[0])
    assert k["status"] == "active" and k["consecutive_failures"] == 0 and k["cooldown_until"] == 0


def test_one_key_429_forwarded_with_retry_after(client, env):
    ids = add(1)
    env.script[KEYS[0]] = [(429, {"Retry-After": "30"}, b'{"error":"slow down"}')]
    r = post(client)
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    assert r.json() == {"error": "slow down"}
    assert len(env.seen) == 1 and row(ids[0])["cooldown_until"] > time.time()


def test_six_keys_all_503_five_attempts(client, env):
    ids = add(6)
    for k in KEYS[:6]:
        env.script[k] = [(503, {}, b'{"error":"down"}')]
    r = post(client)
    assert r.status_code == 503 and r.json() == {"error": "down"}
    assert len(env.seen) == 5 and len(set(env.keys_used)) == 5
    untouched = [i for i in ids if row(i)["consecutive_failures"] == 0]
    assert len(untouched) == 1


def test_all_cooling_one_fallback_on_soonest(client, env):
    ids = add(3)
    now = time.time()
    for i, off in zip(ids, (300, 100, 200)):
        store._x("UPDATE gemini_keys SET cooldown_until=? WHERE id=?", (now + off, i))
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
    c = TestClient(app)
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


def test_db_has_no_plaintext(client, env):
    add(2)
    post(client)
    raw = open(config.DB_PATH, "rb").read()
    for p in (config.DB_PATH + "-wal",):
        if os.path.exists(p):
            raw += open(p, "rb").read()
    assert b"secretplaintext" not in raw and b"AIzaKEY" not in raw
    k = row(1)
    assert k["key_tail"] == KEYS[0][-4:] and k["fingerprint"] == store.fingerprint(KEYS[0])


# ── log redaction ────────────────────────────────────────────────────────────
def test_log_redaction(client, env, caplog):
    add(1)
    with caplog.at_level(logging.INFO):
        client.post(GEN + "?key=SENTINEL", content=b"{}")                      # 401
        client.post("/gemini/v1beta/models/m:nope?key=SENTINEL", headers=H, content=b"{}")  # 404
        client.post(GEN + "?key=SENTINEL&alt=sse", headers=H, content=b"{}")   # proxied
        client.get("/health?key=SENTINEL")
    assert any("/gemini" in r.getMessage() for r in caplog.records)  # middleware did log
    # The TestClient's own httpx log lines ("http://testserver/...") are the caller side,
    # not the gateway; everything the gateway process logs must be free of the sentinel.
    gateway = [r.getMessage() for r in caplog.records if "http://testserver" not in r.getMessage()]
    assert gateway and all("SENTINEL" not in m for m in gateway)
    assert all("SENTINEL" not in u for u, _ in env.seen)
    assert all(KEYS[0] not in m for m in gateway)


# ── streaming through real sockets ───────────────────────────────────────────
def _serve(asgi_app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(asgi_app, log_level="warning", access_log=False))
    t = threading.Thread(target=lambda: asyncio.run(server.serve([sock])), daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.02)
    return server, sock.getsockname()[1]


def _fake_stream_app(release: threading.Event, hits: list, crash: bool):
    async def fake(scope, receive, send):
        if scope["type"] != "http":
            return
        hits.append(scope["headers"])
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"data: chunk1\n\n", "more_body": True})
        if crash:
            raise RuntimeError("upstream died mid-stream")
        for _ in range(100):
            if release.is_set():
                break
            await asyncio.sleep(0.05)
        await send({"type": "http.response.body", "body": b"data: chunk2\n\n"})
    return fake


@pytest.fixture
def live(env, monkeypatch):
    monkeypatch.setattr(gemini_proxy, "transport", None)
    servers = []

    def start(crash=False):
        release, hits = threading.Event(), []
        up, up_port = _serve(_fake_stream_app(release, hits, crash))
        gw, gw_port = _serve(app)
        servers.extend([up, gw])
        monkeypatch.setattr(config, "GEMINI_UPSTREAM", f"http://127.0.0.1:{up_port}")
        return release, hits, gw_port

    yield start
    for s in servers:
        s.should_exit = True


STREAM = "/gemini/v1beta/models/gemini-x:streamGenerateContent?alt=sse"


def test_stream_is_incremental(live):
    add(1)
    release, hits, port = live()
    with httpx.Client(timeout=httpx.Timeout(3.0)) as c:
        with c.stream("POST", f"http://127.0.0.1:{port}{STREAM}", headers=H, content=b"{}") as r:
            assert r.status_code == 200
            it = r.iter_raw()
            first = next(it)
            assert b"chunk1" in first and not release.is_set()  # chunk2 is still held upstream
            release.set()
            rest = b"".join(it)
    assert b"chunk2" in rest
    assert len(hits) == 1


def test_no_failover_after_first_byte(live):
    add(2)
    _, hits, port = live(crash=True)
    got = b""
    with httpx.Client(timeout=httpx.Timeout(3.0)) as c:
        try:
            with c.stream("POST", f"http://127.0.0.1:{port}{STREAM}", headers=H, content=b"{}") as r:
                for chunk in r.iter_raw():
                    got += chunk
        except httpx.HTTPError:
            pass
    assert b"chunk1" in got
    time.sleep(0.3)
    assert len(hits) == 1  # second key never tried


def test_stream_fails_over_on_429_before_first_byte(client, env):
    add(2)
    env.script[KEYS[0]] = [(429, {}, b'{"error":{"status":"RESOURCE_EXHAUSTED"}}')]
    r = post(client, path=STREAM)
    assert r.status_code == 200
    assert env.keys_used == [KEYS[0], KEYS[1]]


def test_request_body_forwarded_byte_for_byte(client, env):
    add(1)
    body = '{"contents":[{"parts":[{"text":"héllo  \u2603 \n\t"}]}] }'.encode("utf-8") + b"\x00\xff"
    assert post(client, body=body).status_code == 200
    assert env.bodies == [body]


def test_client_disconnect_mid_stream_still_logs(live):
    add(1)
    release, _, port = live()
    with httpx.Client(timeout=httpx.Timeout(3.0)) as c:
        with c.stream("POST", f"http://127.0.0.1:{port}{STREAM}", headers=H, content=b"{}") as r:
            assert b"chunk1" in next(r.iter_raw())
        # leaving the block closes the connection before chunk2
    deadline = time.time() + 5
    while time.time() < deadline and not store.recent_requests(10):
        time.sleep(0.05)
    release.set()
    rows = store.recent_requests(10)
    assert len(rows) == 1 and rows[0]["route"].endswith("streamGenerateContent")


# ── admin ────────────────────────────────────────────────────────────────────
ORIGIN = {"Origin": "http://testserver"}


def login(client, pw="admin-pw", headers=ORIGIN):
    return client.post("/admin/login", data={"password": pw}, headers=headers, follow_redirects=False)


def test_admin_requires_login(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert client.post("/admin/keys", data={"label": "a", "key": "b"}, headers=ORIGIN,
                       follow_redirects=False).status_code == 303
    assert client.get("/admin/login").status_code == 200


def test_admin_login_cookie_and_masking(client, env):
    add(1)
    r = login(client)
    assert r.status_code == 303
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc
    page = client.get("/admin").text
    assert "…" + KEYS[0][-4:] in page and KEYS[0] not in page and "secretplaintext" not in page
    assert "viewport" in page


def test_admin_origin_check(client):
    assert login(client, headers={}).status_code == 403
    assert login(client, headers={"Origin": "http://evil.example"}).status_code == 403
    assert login(client).status_code == 303
    r = client.post("/admin/keys", data={"label": "a", "key": "b"},
                    headers={"Origin": "http://evil.example"}, follow_redirects=False)
    assert r.status_code == 403


def test_admin_login_throttle(client):
    for _ in range(5):
        assert login(client, pw="wrong").status_code == 401
    assert login(client, pw="wrong").status_code == 429
    assert login(client).status_code == 429  # even the right password, while throttled


def test_admin_add_test_disable_delete(client, env):
    login(client)
    r = client.post("/admin/keys", data={"label": "main", "key": KEYS[0]}, headers=ORIGIN,
                    follow_redirects=False)
    assert r.status_code == 303 and store.list_keys()[0]["label"] == "main"
    assert env.seen[0][0].startswith("http://upstream.test/v1beta/models?pageSize=1")
    dup = client.post("/admin/keys", data={"label": "again", "key": KEYS[0]}, headers=ORIGIN)
    assert dup.status_code == 409 and len(store.list_keys()) == 1

    env.script[KEYS[1]] = [(400, {}, b'{"error":{"details":[{"reason":"API_KEY_INVALID"}]}}')]
    bad = client.post("/admin/keys", data={"label": "bad", "key": KEYS[1]}, headers=ORIGIN)
    assert bad.status_code == 400 and "API_KEY_INVALID" in bad.text and len(store.list_keys()) == 1

    kid = store.list_keys()[0]["id"]
    assert "Test of main: ok" in client.post(f"/admin/keys/{kid}/test", headers=ORIGIN).text
    client.post(f"/admin/keys/{kid}/disable", headers=ORIGIN)
    assert row(kid)["status"] == "disabled"
    client.post(f"/admin/keys/{kid}/enable", headers=ORIGIN)
    assert row(kid)["status"] == "active"

    assert "Yes, delete" in client.get(f"/admin/keys/{kid}/delete").text
    assert client.post(f"/admin/keys/{kid}/delete", headers=ORIGIN).status_code == 400  # no confirm
    assert row(kid) is not None
    client.post(f"/admin/keys/{kid}/delete", data={"confirm": "yes"}, headers=ORIGIN)
    assert row(kid) is None


def test_test_does_not_reenable_disabled_key_but_rejected_returns(client, env):
    login(client)
    (kid,) = add(1)
    store.set_status(kid, "disabled")
    assert "Test of k0: ok" in client.post(f"/admin/keys/{kid}/test", headers=ORIGIN).text
    assert row(kid)["status"] == "disabled"
    store.set_status(kid, "active")
    store.mark_rejected(kid, "403")
    assert row(kid)["status"] == "rejected"
    client.post(f"/admin/keys/{kid}/test", headers=ORIGIN)
    assert row(kid)["status"] == "active"


def test_admin_add_key_429_saved_with_note_but_invalid_refused(client, env):
    login(client)
    env.script[KEYS[0]] = [(429, {}, b'{"error":{"status":"RESOURCE_EXHAUSTED"}}')]
    r = client.post("/admin/keys", data={"label": "busy", "key": KEYS[0]}, headers=ORIGIN)
    assert r.status_code == 200 and "rate-limited" in r.text
    assert [k["label"] for k in store.list_keys()] == ["busy"]
    for i, st in ((1, 401), (2, 403)):
        env.script[KEYS[i]] = [(st, {}, b"{}")]
        assert client.post("/admin/keys", data={"label": f"x{i}", "key": KEYS[i]},
                           headers=ORIGIN).status_code == 400
    assert len(store.list_keys()) == 1


def test_admin_shows_recent_requests(client, env):
    login(client)
    post(client)  # empty pool -> 503 row
    assert "503" in client.get("/admin").text and "generateContent" in client.get("/admin").text


def test_admin_disabled_without_password(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    assert client.get("/admin/login").status_code == 503
