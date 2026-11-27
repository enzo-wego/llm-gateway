"""Enrich FastAPI's schemas for raw-body handlers without changing request validation."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse

from . import config
from .codex_gateway import _CodexInput


def obj(**fields):
    return {"type": "object", "properties": fields}


S, I, N, B = ({"type": t} for t in ("string", "integer", "number", "boolean"))
O = {"type": "object"}


def arr(item):
    return {"type": "array", "items": item}


def media(schema, **examples):
    result = {"schema": schema}
    if examples:
        result["examples"] = {name: {"value": value} for name, value in examples.items()}
    return {"application/json": result}


def config_fields():
    fields = {}
    for key in config.EDITABLE_ENV_KEYS:
        if key.startswith("BACKEND_"):
            spec = {"enum": sorted(config._BACKENDS)}
        elif key.startswith("EFFORT_"):
            spec = {"enum": sorted(config._EFFORTS)}
        elif key.startswith("THINKING_"):
            spec = {"enum": sorted(config._THINKING)}
        elif key.startswith("OR_MAX_TOKENS"):
            spec = {**I, "minimum": 1}
        elif key == "CLAUDE_TIMEOUT_S":
            spec = {**I, "minimum": 1, "maximum": 199}
        elif key == "MAX_BUDGET_USD":
            spec = {**N, "exclusiveMinimum": 0}
        elif key == "FALLBACK_ON_QUOTA":
            spec = B
        else:
            spec = {**S, "pattern": r"^\S+$"}
        fields[key] = spec
    return {**obj(**fields), "additionalProperties": False}


def install_reference(app: FastAPI):
    # Serve browser code locally: the user may authorize a real gateway credential.
    static = Path(__file__).resolve().parent.parent / "swagger"

    @app.get("/docs", include_in_schema=False)
    def docs():
        return get_swagger_ui_html(
            openapi_url="/openapi.json", title="LLM Gateway API",
            swagger_js_url="/docs/swagger-ui-bundle.js", swagger_css_url="/docs/swagger-ui.css",
            swagger_favicon_url="data:,", swagger_ui_parameters={"validatorUrl": None,
                "persistAuthorization": False, "filter": True, "defaultModelsExpandDepth": -1})

    @app.get("/docs/swagger-ui-bundle.js", include_in_schema=False)
    def js():
        return FileResponse(static / "swagger-ui-bundle.js", media_type="application/javascript")

    @app.get("/docs/swagger-ui.css", include_in_schema=False)
    def css():
        return FileResponse(static / "swagger-ui.css", media_type="text/css")

    original = app.openapi

    def openapi():
        if app.openapi_schema:
            return app.openapi_schema
        spec = original()
        describe_contract(spec)
        return spec

    app.openapi = openapi


def describe_contract(spec):
    paths = spec["paths"]
    for path in list(paths):
        if path.startswith("/admin"):
            del paths[path]  # HTML dashboard, not a consumer JSON API.
    spec["info"]["description"] = (
        "Internal caller API. Authorize with the credential for the operation. Executing an "
        "operation is real: model calls can incur charges and PUT /config changes live settings.")
    schemes = spec["components"]["securitySchemes"] = {}
    for name, header, desc in [
        ("GatewayKey", "X-API-Key", "Shared gateway key"),
        ("GeminiGatewayKey", "x-goog-api-key", "Same shared gateway key; not a Google key"),
        ("CodexProjectKey", "X-API-Key", "Dedicated p-agent or agent-mem key; not the shared key"),
    ]:
        schemes[name] = {"type": "apiKey", "in": "header", "name": header, "description": desc}
    gem = "/gemini/v1beta/models/{spec}"
    for path, methods in paths.items():
        auth = ([{"GeminiGatewayKey": []}, {"GatewayKey": []}] if path == gem else
                [{"CodexProjectKey": []}] if path == "/codex" else
                [] if path == "/health" else [{"GatewayKey": []}])
        for op in methods.values():
            op["security"] = auth
            op["parameters"] = [p for p in op.get("parameters", []) if p["name"] != "x-api-key"]
            for param in op["parameters"]:
                if param["name"] == "x-llm-caller":
                    param["description"] = "Optional accounting label, not authentication; malformed labels become unknown."
            if auth:
                op["responses"]["401"] = {"description": "Missing or incorrect key"}
    for route, prompt in [("/generate", {"user": "Return JSON with a greeting."}),
                          ("/describe", {"prompt": "Extract the total.", "mime": "image/png",
                                         "data_b64": "<base64 without a data URI prefix>"})]:
        op = paths[route]["post"]
        op["description"] = ("Omit backend/model/effort for configured routing. Without backend, "
            "model or effort pins Claude (422 if OpenRouter is primary). Explicit backend=openrouter "
            "accepts vendor/model and an optional OpenRouter effort; no Claude fallback. "
            "Claude efforts: low/medium/high/xhigh/max. OpenRouter: none/minimal/low/medium/high/xhigh.")
        if route == "/describe":
            op["description"] += " Image or PDF. The OpenRouter description adapter sends prompt and attachment, not system."
        body = op["requestBody"]["content"]["application/json"]
        body.update(media(body["schema"], configured=prompt, Claude={**prompt, "model": "haiku", "effort": "low"},
            OpenRouter={**prompt, "backend": "openrouter", "model": "anthropic/claude-sonnet-4.5", "effort": "low"},
            structured={**prompt, "schema": {"type": "object", "properties": {"answer": S},
                                              "required": ["answer"], "additionalProperties": False}})["application/json"])
    cx = paths["/codex"]["post"]
    cs = _CodexInput.model_json_schema(ref_template="#/components/schemas/{model}")
    spec["components"]["schemas"].update(cs.pop("$defs", {}))
    cs["not"] = {"required": ["tier"]}
    cx["requestBody"] = {"required": True, "content": media(cs, defaults={"user": "Reply OK."},
        explicit={"user": "Reply OK.", "model": "gpt-6.2-sol", "effort": "low"})}
    cx["description"] = "Nonblank user; no tier. Dedicated project key. 1 MiB request/response limit, 180 s worker deadline."
    for route in ("/generate", "/describe", "/codex"):
        op = paths[route]["post"]
        result = {"oneOf": [{**obj(backend=S, text=S, meta=O), "required": ["backend", "text", "meta"]},
                            {**obj(backend=S, output={}, meta=O), "required": ["backend", "output", "meta"]}]}
        provider = "codex" if route == "/codex" else "claude"
        meta = {"model": "gpt-6.2-sol", "effort": "medium"} if route == "/codex" else {"model": "haiku"}
        op["responses"]["200"]["content"] = media(result, text={"backend": provider, "text": "OK", "meta": meta},
            structured={"backend": provider, "output": {"answer": "OK"}, "meta": meta})
        for code in ([400, 408, 413, 422, 429, 502, 503, 504] if route == "/codex" else [422, 502, 503]):
            op["responses"].setdefault(str(code), {"description": "Validation, provider or availability error; see detail."})
    gemini_contract(paths[gem]["post"])
    ep = paths["/embed"]["post"]
    ep["responses"]["200"]["content"] = media(obj(embeddings=arr(arr(N)), model=S, dims=I),
        empty={"embeddings": [], "model": "google/gemini-embedding-001"})
    ep["responses"]["502"] = {"description": "Embedding provider failed"}
    cfg = config_fields()
    paths["/config"]["put"]["requestBody"]["content"] = media(cfg, partial={"EFFORT_CHEAP": "low"})
    for op in paths["/config"].values():
        op["responses"]["200"]["content"] = media({**cfg, "required": list(cfg["properties"])})
    paths["/config"]["put"]["responses"].update({"400": {"description": "Unknown key or invalid value"},
                                                 "500": {"description": "Cannot persist update"}})
    caller = obj(calls=I, errors=I, notional_cost_usd=N)
    paths["/usage"]["get"]["responses"]["200"]["content"] = media(obj(
        callers={"type": "object", "additionalProperties": caller},
        seat=obj(available=B, calls=I, last_rate_limit={}, notional_cost_usd=N, openrouter_fallbacks=I),
        openrouter=obj(limit={}, usage={}, limit_remaining={}, usage_daily={}, usage_weekly={}, usage_monthly={}, error=S)))
    tiers = obj(cheap=S, summary=S, describe=S, embed=S)
    paths["/health"]["get"]["responses"]["200"]["content"] = media(obj(ok=B, backends=tiers, models=tiers,
        gemini_pool=obj(total=I, healthy=I, cooling=I, disabled=I, rejected=I, ready=B),
        seat=obj(available=B, blocked_until={}, last_rate_limit={}),
        counters=obj(claude_calls=I, notional_cost_usd=N, openrouter_fallbacks=I),
        fallback_on_quota=B, embed_fallback={}, alerts_enabled=B))
    for path, ops in paths.items():
        for op in ops.values():
            for code, response in op["responses"].items():
                if int(code) < 400 or path == gem:
                    continue
                detail = obj(detail={"oneOf": [S, arr(O)]} if code == "422" else S)
                if code == "422" or "content" not in response:
                    response["content"] = media(detail, error={"detail": response["description"]})


def gemini_contract(op):
    content = {"parts": [{"text": "Hello"}]}
    examples = {"generateContent": {"contents": [content]}, "streamGenerateContent": {"contents": [content]},
        "countTokens": {"contents": [content]}, "embedContent": {"model": "models/gemini-embedding-001", "content": content},
        "batchEmbedContents": {"requests": [{"model": "models/gemini-embedding-001", "content": content}]}}
    op["summary"] = "Call a native Gemini model action"
    op["description"] = ("spec is model:action. Select the matching body example. The URL chooses the model; "
        "retry changes keys, not models. Native Google fields are passed through, not restricted to this list. "
        "effort is for supported generation models only; omit it when supplying native thinkingBudget/thinkingLevel.")
    op["parameters"][0]["example"] = "gemini-2.5-flash:generateContent"
    op["parameters"].extend({"name": k, "in": "query", "required": False, "schema": {"enum": vs},
                             "description": desc} for k, vs, desc in (
        ("alt", ["sse"], "Use with streamGenerateContent for server-sent events."),
        ("effort", ["low", "medium", "high"], "Model-specific shortcut on generateContent and streamGenerateContent.")))
    schema = obj(contents=arr(O), systemInstruction=O, generationConfig=O, safetySettings=arr(O),
                 tools=arr(O), toolConfig=O, cachedContent=S, content=O, model=S, requests=arr(O),
                 taskType=S, outputDimensionality=I, generateContentRequest=O)
    op["requestBody"] = {"required": True, "content": media(schema, **examples)}
    op["responses"]["200"]["content"] = {**media(O, generateContent={"candidates":[{"content":content}]},
        countTokens={"totalTokens":1}, embedContent={"embedding":{"values":[0.1]}},
        batchEmbedContents={"embeddings":[{"values":[0.1]}]}),
        "text/event-stream": {"schema": S, "example": 'data: {"candidates":[]}\n\n'}}
    for status, reason in {400:"Invalid effort or upstream request", 401:"Unauthorized", 403:"Upstream key rejected",
            404:"Unknown action/model", 429:"Upstream rate limit", 502:"Upstream unreachable",
            503:"Pool disabled, unavailable, empty or upstream unavailable"}.items():
        op["responses"][str(status)] = {"description": reason, "content":media(obj(error={}))}
