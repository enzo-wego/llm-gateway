"""Internal admin page for the Gemini key pool. Server-rendered HTML, no JS.

Auth: LLM_GATEWAY_ADMIN_PASSWORD -> signed HttpOnly SameSite=Strict cookie (12 h).
Every POST must carry an Origin header matching the Host header. Failed logins
are throttled (5 per 15 min per client address).
"""

import hashlib
import hmac
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from functools import partial
from typing import ParamSpec, TypeVar
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import config, gemini_proxy, gemini_store as store
from .admin_pages import dashboard, delete_page, login_page

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


_P = ParamSpec("_P")
_T = TypeVar("_T")


async def _storage(fn: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Run SQLite and key crypto off-loop and report anticipated storage outages."""
    try:
        return await run_in_threadpool(partial(fn, *args, **kwargs))
    except (sqlite3.OperationalError, OSError) as exc:
        raise HTTPException(503, "Gemini pool storage unavailable") from exc


# ── upstream validation ──────────────────────────────────────────────────────
class _Verdict(Enum):
    OK = auto()
    REJECTED = auto()
    RATE_LIMITED = auto()
    UNVERIFIED = auto()


@dataclass(frozen=True, slots=True)
class _Probe:
    verdict: _Verdict
    reason: str


async def check_key(key: str) -> _Probe:
    """One cheap upstream call without displaying arbitrary upstream reasons."""
    url = f"{config.GEMINI_UPSTREAM}/v1beta/models"
    try:
        async with gemini_proxy.client() as c:
            r = await c.get(url, params={"pageSize": 1}, headers={"x-goog-api-key": key})
    except httpx.HTTPError:
        return _Probe(_Verdict.UNVERIFIED, "upstream unreachable")
    verdict = gemini_proxy.classify(r.status_code, r.content)
    if verdict == "ok":
        return _Probe(_Verdict.OK, "ok")
    if verdict == "rejected":
        reason = "400 API_KEY_INVALID" if r.status_code == 400 else str(r.status_code)
        return _Probe(_Verdict.REJECTED, reason)
    if r.status_code == 429:
        return _Probe(_Verdict.RATE_LIMITED, "429")
    return _Probe(_Verdict.UNVERIFIED, str(r.status_code))


# ── routes ───────────────────────────────────────────────────────────────────
@router.get("/admin")
async def index(request: Request) -> Response:
    if (r := _guard_authed(request)) is not None:
        return r
    return await _storage(dashboard)


@router.get("/admin/login")
async def login_form() -> Response:
    if (r := _disabled()) is not None:
        return r
    return login_page()


@router.post("/admin/login")
async def login(request: Request) -> Response:
    if (r := _guard_post(request)) is not None:
        return r
    ip = _ip(request)
    if _throttled(ip):
        return login_page("Too many failed attempts. Try again later.", 429)
    given = (await _form(request)).get("password", "")
    if not hmac.compare_digest(given.encode(), config.ADMIN_PASSWORD.encode()):
        _fails.setdefault(ip, []).append(time.time())
        return login_page("Wrong password.", 401)
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


def _authed_post(request: Request) -> Response | None:
    if (r := _guard_post(request)) is not None:
        return r
    return _guard_authed(request)


@router.post("/admin/keys")
async def add_key(request: Request) -> Response:
    if (r := _authed_post(request)) is not None:
        return r
    form = await _form(request)
    label, key = form.get("label", "").strip()[:60], form.get("key", "").strip()
    if not label or not key:
        return await _storage(dashboard, "Label and key are required.", 400)
    if any(ch.isspace() for ch in key):
        return await _storage(dashboard, "Key must not contain whitespace.", 400)
    probe = await check_key(key)
    if probe.verdict not in (_Verdict.OK, _Verdict.RATE_LIMITED):
        return await _storage(dashboard, f"Key refused: {probe.reason}", 400)
    if await _storage(store.add_key, label, key) is None:
        return await _storage(dashboard, "Key already in the pool (same fingerprint).", 409)
    if probe.verdict is _Verdict.RATE_LIMITED:
        return await _storage(dashboard, "Key added, but the probe was rate-limited (429). "
                              "Validity is unverified; use Test later to confirm.")
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/keys/{key_id}/{action}")
async def key_action(key_id: int, action: str, request: Request) -> Response:
    if (r := _authed_post(request)) is not None:
        return r
    row = await _storage(store.get_key, key_id)
    if row is None:
        return await _storage(dashboard, "No such key.", 404)
    if action == "enable":
        await _storage(store.set_status, key_id, "active")
    elif action == "disable":
        await _storage(store.set_status, key_id, "disabled")
    elif action == "test":
        secret = await _storage(store.plaintext, row)
        probe = await check_key(secret) if secret else _Probe(_Verdict.UNVERIFIED, "decrypt failed")
        if probe.verdict is _Verdict.OK and row["status"] == "rejected":
            await _storage(store.restore_rejected, key_id, row["operator_version"])
        elif probe.verdict is _Verdict.REJECTED and row["status"] == "active":
            await _storage(store.mark_rejected, key_id, probe.reason,
                           operator_version=row["operator_version"])
        return await _storage(dashboard, f"Test of {row['label']}: {probe.reason}")
    elif action == "delete":
        if (await _form(request)).get("confirm") != "yes":
            return await _storage(dashboard, "Delete needs confirmation.", 400)
        await _storage(store.delete_key, key_id)
    else:
        return await _storage(dashboard, "Unknown action.", 404)
    return RedirectResponse("/admin", status_code=303)


@router.get("/admin/keys/{key_id}/delete")
async def delete_confirm(key_id: int, request: Request) -> Response:
    if (r := _guard_authed(request)) is not None:
        return r
    row = await _storage(store.get_key, key_id)
    if row is None:
        return await _storage(dashboard, "No such key.", 404)
    return delete_page(row)
