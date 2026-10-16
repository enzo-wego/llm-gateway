"""Deploy probe: prove an alert mute survives a container recreate.

Run inside the container with LLM_GATEWAY_ALERT_STATE_FILE pointing at a
self-test file, never the live one. Prints the number of Slack post attempts.
Usage (from the host, script piped on stdin; cwd in the container is /app):
  docker compose exec -T -e LLM_GATEWAY_ALERT_STATE_FILE=... llm-gateway \
    python - <key> <until_unix_ts> < deploy/alert_selftest.py
"""
import asyncio
import sys

from app import alerts, config

assert config.ALERT_STATE_FILE.endswith("alert-state-selftest.json"), config.ALERT_STATE_FILE

attempts = 0


class _Resp:
    def json(self):
        return {"ok": True}


class _Client:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        global attempts
        attempts += 1
        return _Resp()


alerts.httpx.AsyncClient = _Client
config.ALERTS_ENABLED = True
asyncio.run(alerts.alert(sys.argv[1], "selftest", critical=False, until=float(sys.argv[2])))
print(attempts)
