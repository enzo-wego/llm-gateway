import os

from fastapi.testclient import TestClient

os.environ.setdefault("LLM_GATEWAY_API_KEY", "test")
from app import config
from app.main import app


def test_reference_discovery_and_credentials():
    client = TestClient(app)
    html = client.get("/docs").text
    assert "/docs/swagger-ui-bundle.js" in html and "cdn.jsdelivr.net" not in html
    spec = client.get("/openapi.json").json()
    p = spec["paths"]
    assert not any(k.startswith("/admin") for k in p)
    assert p["/health"]["get"]["security"] == []
    assert p["/generate"]["post"]["security"] == [{"GatewayKey": []}]
    cx = p["/codex"]["post"]
    assert cx["security"] == [{"CodexProjectKey": []}]
    schema = cx["requestBody"]["content"]["application/json"]["schema"]
    assert schema["properties"]["model"]["default"] == "gpt-6.2-sol"
    assert schema["properties"]["effort"]["default"] == "medium"
    assert schema["not"] == {"required": ["tier"]}
    assert set(p["/config"]["put"]["requestBody"]["content"]["application/json"]["schema"]["properties"]) == set(config.editable_config())
    gem = p["/gemini/v1beta/models/{spec}"]["post"]
    assert {"effort", "alt", "spec"} == {q["name"] for q in gem["parameters"]}
    assert "text/event-stream" in gem["responses"]["200"]["content"]
    assert len(gem["requestBody"]["content"]["application/json"]["examples"]) == 5

    def resolve_refs(node):
        if isinstance(node, list):
            for v in node:
                resolve_refs(v)
        elif isinstance(node, dict):
            if "$ref" in node:
                target = spec
                for component in node["$ref"].removeprefix("#/").split("/"):
                    target = target[component]
            for v in node.values():
                resolve_refs(v)
    resolve_refs(spec)
