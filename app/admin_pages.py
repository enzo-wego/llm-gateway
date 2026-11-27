"""Server-rendered views for the Gemini pool admin."""

import html
import time
from datetime import datetime, timezone
from sqlite3 import Row

from fastapi.responses import HTMLResponse

from . import gemini_store as store

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


def _e(v: str | int | None) -> str:
    return html.escape("" if v is None else str(v))


def _ts(v: float | None) -> str:
    if not v:
        return "-"
    return datetime.fromtimestamp(v, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _btn(key_id: int, action: str, label: str) -> str:
    return (f'<form class="inline" method="post" action="/admin/keys/{key_id}/{action}">'
            f'<button>{label}</button></form>')


def login_page(msg: str = "", status: int = 200) -> HTMLResponse:
    note = f'<p class="err">{_e(msg)}</p>' if msg else ""
    return _page("llm-gateway admin", f"""<h1>llm-gateway admin</h1>{note}
<form method="post" action="/admin/login"><label>Password
<input type="password" name="password" autocomplete="current-password" autofocus></label>
<button>Log in</button></form>""", status)


def dashboard(msg: str = "", status: int = 200) -> HTMLResponse:
    """Read pool data and build the dashboard in a worker thread."""
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


def delete_page(row: Row) -> HTMLResponse:
    return _page("Delete key", f"""<h1>Delete key?</h1>
<p>{_e(row["label"])} (…{_e(row["key_tail"])}) will be removed from the pool.</p>
<form method="post" action="/admin/keys/{row["id"]}/delete">
<input type="hidden" name="confirm" value="yes"><button>Yes, delete</button></form>
<p><a href="/admin">Cancel</a></p>""")
