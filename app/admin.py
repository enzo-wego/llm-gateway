"""Internal admin page for the Gemini key pool. Server-rendered HTML, no JS.

Auth: LLM_GATEWAY_ADMIN_PASSWORD -> signed HttpOnly SameSite=Strict cookie (12 h).
Every POST must carry an Origin header matching the Host header. Failed logins
are throttled (5 per 15 min per client address).
"""

import base64
import hashlib
import hmac
import html
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from . import config, gemini_proxy, gemini_store as store

router = APIRouter()

COOKIE = "gw_admin"
SESSION_S = 12 * 3600
THROTTLE_MAX = 5
THROTTLE_WINDOW_S = 15 * 60
_fails: dict[str, list[float]] = {}


# ── session + guards ─────────────────────────────────────────────────────────
def _sign(payload: str) -> str:
    key = hashlib.sha256(b"llm-gateway/session/" + config.SECRET_KEY.encode()).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _make_session() -> str:
    exp = str(int(time.time()) + SESSION_S)
    return f"{exp}.{_sign(exp)}"


def _valid_session(request: Request) -> bool:
    raw = request.cookies.get(COOKIE, "")
    exp, _, sig = raw.partition(".")
    if not exp.isdigit() or not sig or not hmac.compare_digest(sig, _sign(exp)):
        return False
    return int(exp) > time.time()


def _disabled() -> Response | None:
    if not config.SECRET_KEY or not config.ADMIN_PASSWORD:
        return HTMLResponse("admin disabled", status_code=503)
    return None


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin", "")
    host = request.headers.get("host", "")
    return bool(origin) and bool(host) and urlparse(origin).netloc == host


async def _form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def _throttled(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < THROTTLE_WINDOW_S]
    _fails[ip] = recent
    return len(recent) >= THROTTLE_MAX


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _guard_post(request: Request) -> Response | None:
    if (r := _disabled()) is not None:
        return r
    if not _origin_ok(request):
        return HTMLResponse("bad origin", status_code=403)
    return None


def _guard_authed(request: Request) -> Response | None:
    if (r := _disabled()) is not None:
        return r
    if not _valid_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


# ── rendering ────────────────────────────────────────────────────────────────
_CSS = """
body{font:15px/1.4 system-ui,sans-serif;margin:0;padding:12px;max-width:1100px;margin-inline:auto}
h1{font-size:1.3em}h2{font-size:1.1em;margin-top:1.5em}
.cards{display:flex;flex-wrap:wrap;gap:8px}.card{border:1px solid #8884;border-radius:6px;padding:6px 10px}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #8883;padding:4px 6px;text-align:left;white-space:nowrap;font-size:.9em}
input[type=text],input[type=password]{padding:6px;width:100%;box-sizing:border-box;margin:3px 0}
button{padding:6px 10px;margin:2px}.err{color:#c00}.ok{color:#080}.warn{color:#b60}
form.inline{display:inline}
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (f'<!doctype html><html><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>{body}</body></html>")
    return HTMLResponse(doc, status_code=status,
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


def _e(v: object) -> str:
    return html.escape("" if v is None else str(v))


def _ts(v: float | None) -> str:
    if not v:
        return "-"
    return datetime.fromtimestamp(v, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _btn(key_id: int, action: str, label: str) -> str:
    return (f'<form class="inline" method="post" action="/admin/keys/{key_id}/{action}">'
            f'<button>{label}</button></form>')


def _login_page(msg: str = "", status: int = 200) -> HTMLResponse:
    note = f'<p class="err">{_e(msg)}</p>' if msg else ""
    return _page("llm-gateway admin", f"""<h1>llm-gateway admin</h1>{note}
<form method="post" action="/admin/login"><label>Password
<input type="password" name="password" autocomplete="current-password" autofocus></label>
<button>Log in</button></form>""", status)


def _dashboard(msg: str = "", status: int = 200) -> HTMLResponse:
    counts = store.pool_counts()
    hour, day = store.window_stats(3600), store.window_stats(86400)
    stats = store.key_stats_24h()
    now = time.time()
    rows = []
    for k in store.list_keys():
        cd = max(0, int(k["cooldown_until"] - now)) if k["status"] == "active" else 0
        s = stats.get(k["id"], {"requests": 0, "failures": 0})
        cls = {"active": "ok", "rejected": "err"}.get(k["status"], "warn")
        acts = _btn(k["id"], "test", "Test")
        acts += _btn(k["id"], "disable" if k["status"] == "active" else "enable",
                     "Disable" if k["status"] == "active" else "Enable")
        acts += f'<a href="/admin/keys/{k["id"]}/delete">Delete</a>'
        rows.append(
            f'<tr><td>{_e(k["label"])}</td><td>…{_e(k["key_tail"])}</td>'
            f'<td class="{cls}">{_e(k["status"])}</td><td>{cd}s</td><td>{_e(k["last_error"])}</td>'
            f'<td>{_ts(k["last_used_at"])}</td><td>{s["requests"]}</td><td>{s["failures"]}</td>'
            f"<td>{acts}</td></tr>")
    recent = "".join(
        f'<tr><td>{_ts(r["ts"])}</td><td>{_e(r["route"])}</td><td>{_e(r["model"])}</td>'
        f'<td>{_e(r["key_id"])}</td><td>{r["status"]}</td><td>{r["latency_ms"]}</td>'
        f'<td>{r["attempts"]}</td></tr>' for r in store.recent_requests(100))
    note = f'<p class="warn">{_e(msg)}</p>' if msg else ""
    cards = "".join(f'<div class="card">{n}: <b>{counts[n]}</b></div>'
                    for n in ("total", "healthy", "cooling", "rejected", "disabled"))
    cards += (f'<div class="card">last hour: <b>{hour["requests"]}</b> req / '
              f'<b>{hour["errors"]}</b> err</div>'
              f'<div class="card">last day: <b>{day["requests"]}</b> req / '
              f'<b>{day["errors"]}</b> err</div>')
    return _page("llm-gateway admin", f"""<h1>Gemini key pool</h1>{note}
<div class="cards">{cards}</div>
<h2>Keys</h2><div class="tw"><table id="keys"><tr><th>Label</th><th>Key</th><th>Status</th>
<th>Cooldown</th><th>Last error</th><th>Last used</th><th>Req 24h</th><th>Fail 24h</th><th></th></tr>
{''.join(rows)}</table></div>
<h2>Add key</h2><form method="post" action="/admin/keys">
<input type="text" name="label" placeholder="label" required>
<input type="password" name="key" placeholder="Gemini API key" autocomplete="off" required>
<button>Validate and add</button></form>
<h2>Recent requests</h2><div class="tw"><table><tr><th>Time</th><th>Route</th><th>Model</th>
<th>Key</th><th>Status</th><th>ms</th><th>Attempts</th></tr>{recent}</table></div>
<form method="post" action="/admin/logout"><button>Log out</button></form>""", status)


# ── upstream validation ──────────────────────────────────────────────────────
async def check_key(key: str) -> tuple[bool, str]:
    """One cheap upstream call. Returns (valid, reason)."""
    url = f"{config.GEMINI_UPSTREAM}/v1beta/models"
    try:
        async with gemini_proxy.client() as c:
            r = await c.get(url, params={"pageSize": 1}, headers={"x-goog-api-key": key})
    except httpx.HTTPError as e:
        return False, f"upstream unreachable ({type(e).__name__})"
    verdict = gemini_proxy.classify(r.status_code, r.content)
    if verdict == "ok":
        return True, "ok"
    reason = gemini_proxy._error_reason(r.content)
    return False, f"{r.status_code} {reason}".strip()


# ── routes ───────────────────────────────────────────────────────────────────
@router.get("/admin")
async def index(request: Request) -> Response:
    if (r := _guard_authed(request)) is not None:
        return r
    return _dashboard()


@router.get("/admin/login")
async def login_form() -> Response:
    if (r := _disabled()) is not None:
        return r
    return _login_page()


@router.post("/admin/login")
async def login(request: Request) -> Response:
    if (r := _guard_post(request)) is not None:
        return r
    ip = _ip(request)
    if _throttled(ip):
        return _login_page("Too many failed attempts. Try again later.", 429)
    given = (await _form(request)).get("password", "")
    if not hmac.compare_digest(given.encode(), config.ADMIN_PASSWORD.encode()):
        _fails.setdefault(ip, []).append(time.time())
        return _login_page("Wrong password.", 401)
    _fails.pop(ip, None)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(COOKIE, _make_session(), max_age=SESSION_S, httponly=True,
                    samesite="strict", path="/admin")
    return resp


@router.post("/admin/logout")
async def logout(request: Request) -> Response:
    if (r := _guard_post(request)) is not None:
        return r
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/admin")
    return resp


async def _authed_post(request: Request) -> Response | None:
    if (r := _guard_post(request)) is not None:
        return r
    if not _valid_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


@router.post("/admin/keys")
async def add_key(request: Request) -> Response:
    if (r := await _authed_post(request)) is not None:
        return r
    form = await _form(request)
    label, key = form.get("label", "").strip()[:60], form.get("key", "").strip()
    if not label or not key:
        return _dashboard("Label and key are required.", 400)
    if any(ch.isspace() for ch in key):
        return _dashboard("Key must not contain whitespace.", 400)
    ok, reason = await check_key(key)
    if not ok:
        return _dashboard(f"Key refused: {reason}", 400)
    if store.add_key(label, key) is None:
        return _dashboard("Key already in the pool (same fingerprint).", 409)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/keys/{key_id}/{action}")
async def key_action(key_id: int, action: str, request: Request) -> Response:
    if (r := await _authed_post(request)) is not None:
        return r
    row = store.get_key(key_id)
    if row is None:
        return _dashboard("No such key.", 404)
    if action == "enable":
        store.set_status(key_id, "active")
    elif action == "disable":
        store.set_status(key_id, "disabled")
    elif action == "test":
        secret = store.plaintext(row)
        ok, reason = await check_key(secret) if secret else (False, "decrypt failed")
        if ok:
            store.set_status(key_id, "active")
        elif reason.startswith(("401", "403")) or "API_KEY_INVALID" in reason:
            store.mark_rejected(key_id, reason)
        return _dashboard(f"Test of {row['label']}: {reason}")
    elif action == "delete":
        if (await _form(request)).get("confirm") != "yes":
            return _dashboard("Delete needs confirmation.", 400)
        store.delete_key(key_id)
    else:
        return _dashboard("Unknown action.", 404)
    return RedirectResponse("/admin", status_code=303)


@router.get("/admin/keys/{key_id}/delete")
async def delete_confirm(key_id: int, request: Request) -> Response:
    if (r := _guard_authed(request)) is not None:
        return r
    row = store.get_key(key_id)
    if row is None:
        return _dashboard("No such key.", 404)
    return _page("Delete key", f"""<h1>Delete key?</h1>
<p>{_e(row["label"])} (…{_e(row["key_tail"])}) will be removed from the pool.</p>
<form method="post" action="/admin/keys/{key_id}/delete">
<input type="hidden" name="confirm" value="yes"><button>Yes, delete</button></form>
<p><a href="/admin">Cancel</a></p>""")
