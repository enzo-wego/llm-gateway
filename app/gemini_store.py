"""SQLite storage for the Gemini key pool: encrypted keys, health state, request log.

Plaintext keys exist only in memory. Nothing here stores prompt or response
content; ``last_error`` carries the upstream status and error reason only.
"""

import base64
import hashlib
import os
import sqlite3
import time
from contextlib import closing
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import config

COOLDOWN_BASE_S = 60
COOLDOWN_MAX_S = 900
LOG_RETENTION_S = 7 * 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gemini_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    ciphertext BLOB NOT NULL,
    key_tail TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','rejected')),
    cooldown_until REAL NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_used_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS request_log (
    ts REAL NOT NULL,
    route TEXT NOT NULL,
    model TEXT,
    key_id INTEGER,
    status INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    prompt_tokens INTEGER,
    output_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS request_log_ts ON request_log (ts);
"""

_inited: set[str] = set()
_last_prune = 0.0


def enabled() -> bool:
    return bool(config.SECRET_KEY)


def _fernet() -> Fernet:
    digest = hashlib.sha256(b"llm-gateway/fernet/" + config.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _conn() -> sqlite3.Connection:
    path = config.DB_PATH
    if path not in _inited:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with closing(sqlite3.connect(path, timeout=10)) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)
            c.execute("DELETE FROM request_log WHERE ts < ?", (time.time() - LOG_RETENTION_S,))
            c.commit()
        _inited.add(path)
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _q(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with closing(_conn()) as c:
        rows = c.execute(sql, args).fetchall()
        c.commit()
        return rows


def _x(sql: str, args: tuple = ()) -> int:
    with closing(_conn()) as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.rowcount


# ── keys ─────────────────────────────────────────────────────────────────────
def add_key(label: str, key: str) -> int | None:
    """Insert an encrypted key. Returns the new id, or None for a duplicate."""
    token = _fernet().encrypt(key.encode())
    try:
        with closing(_conn()) as c:
            cur = c.execute(
                "INSERT INTO gemini_keys (label, ciphertext, key_tail, fingerprint, created_at)"
                " VALUES (?,?,?,?,?)",
                (label, token, key[-4:], fingerprint(key), time.time()),
            )
            c.commit()
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def plaintext(row: sqlite3.Row) -> str | None:
    try:
        return _fernet().decrypt(bytes(row["ciphertext"])).decode()
    except InvalidToken:
        return None


def get_key(key_id: int) -> sqlite3.Row | None:
    rows = _q("SELECT * FROM gemini_keys WHERE id=?", (key_id,))
    return rows[0] if rows else None


def list_keys() -> list[sqlite3.Row]:
    return _q("SELECT * FROM gemini_keys ORDER BY id")


def delete_key(key_id: int) -> None:
    _x("DELETE FROM gemini_keys WHERE id=?", (key_id,))


def set_status(key_id: int, status: str) -> None:
    """Admin enable/disable/reject. Enabling also clears cooldown and failures."""
    if status == "active":
        _x("UPDATE gemini_keys SET status='active', cooldown_until=0, consecutive_failures=0,"
           " last_error=NULL WHERE id=?", (key_id,))
    else:
        _x("UPDATE gemini_keys SET status=? WHERE id=?", (status, key_id))


def mark_rejected(key_id: int, error: str) -> None:
    _x("UPDATE gemini_keys SET status='rejected', last_error=? WHERE id=? AND status='active'",
       (error[:200], key_id))


def mark_ok(key_id: int) -> None:
    _x("UPDATE gemini_keys SET consecutive_failures=0, cooldown_until=0, last_used_at=? WHERE id=?",
       (time.time(), key_id))


def mark_cooldown(key_id: int, error: str, retry_after: float | None) -> float:
    """Start or extend a cooldown; returns its length in seconds."""
    rows = _q("SELECT consecutive_failures FROM gemini_keys WHERE id=?", (key_id,))
    failures = (rows[0]["consecutive_failures"] if rows else 0) + 1
    if retry_after is not None and retry_after > 0:
        secs = min(retry_after, COOLDOWN_MAX_S)
    else:
        secs = min(COOLDOWN_BASE_S * 2 ** (failures - 1), COOLDOWN_MAX_S)
    _x("UPDATE gemini_keys SET consecutive_failures=?, cooldown_until=?, last_error=?,"
       " last_used_at=? WHERE id=?",
       (failures, time.time() + secs, error[:200], time.time(), key_id))
    return secs


def usable_keys() -> list[sqlite3.Row]:
    return _q("SELECT * FROM gemini_keys WHERE status='active' AND cooldown_until <= ? ORDER BY id",
              (time.time(),))


def soonest_active() -> sqlite3.Row | None:
    rows = _q("SELECT * FROM gemini_keys WHERE status='active' ORDER BY cooldown_until, id LIMIT 1")
    return rows[0] if rows else None


def pool_counts() -> dict[str, int]:
    now = time.time()
    out = {"total": 0, "healthy": 0, "cooling": 0, "rejected": 0, "disabled": 0}
    for r in list_keys():
        out["total"] += 1
        if r["status"] == "active":
            out["cooling" if r["cooldown_until"] > now else "healthy"] += 1
        else:
            out[r["status"]] += 1
    return out


# ── request log ──────────────────────────────────────────────────────────────
def log_request(route: str, model: str | None, key_id: int | None, status: int,
                latency_ms: int, attempts: int,
                prompt_tokens: int | None = None, output_tokens: int | None = None) -> None:
    global _last_prune
    now = time.time()
    _x("INSERT INTO request_log VALUES (?,?,?,?,?,?,?,?,?)",
       (now, route, model, key_id, status, latency_ms, attempts, prompt_tokens, output_tokens))
    if now - _last_prune > 3600:
        _last_prune = now
        _x("DELETE FROM request_log WHERE ts < ?", (now - LOG_RETENTION_S,))


def recent_requests(limit: int = 100) -> list[sqlite3.Row]:
    return _q("SELECT * FROM request_log ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,))


def window_stats(seconds: int) -> dict[str, int]:
    rows = _q("SELECT COUNT(*) n, COALESCE(SUM(status>=400),0) e FROM request_log WHERE ts>=?",
              (time.time() - seconds,))
    return {"requests": rows[0]["n"], "errors": rows[0]["e"]}


def key_stats_24h() -> dict[int, dict[str, int]]:
    rows = _q("SELECT key_id, COUNT(*) n, SUM(status>=400) e FROM request_log"
              " WHERE ts>=? AND key_id IS NOT NULL GROUP BY key_id", (time.time() - 86400,))
    return {r["key_id"]: {"requests": r["n"], "failures": r["e"] or 0} for r in rows}


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys() if k != "ciphertext"}
