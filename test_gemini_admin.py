"""Gemini admin HTTP and dashboard contracts."""

from html.parser import HTMLParser

import httpx

from gemini_test_support import KEYS, add, client, config, env, post, store


class Page(HTMLParser):
    """Read rendered attributes, form fields, and the data cells of each row."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.forms: list[tuple[dict[str, str], list[dict[str, str]]]] = []
        self.rows: list[list[str]] = []
        self._in_form = False
        self._in_row = False
        self._cell: list[str] | None = None
        self.feed(text)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        self.elements.append((tag, attributes))
        if tag == "form":
            self.forms.append((attributes, []))
            self._in_form = True
        if tag == "input" and self._in_form:
            self.forms[-1][1].append(attributes)
        if tag == "tr":
            self.rows.append([])
            self._in_row = True
        if tag == "td" and self._in_row:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False
        if tag == "tr":
            self._in_row = False
        if tag == "td" and self._cell is not None:
            self.rows[-1].append("".join(self._cell).strip())
            self._cell = None


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
    page = client.get("/admin", follow_redirects=False)
    assert page.status_code == 200
    assert "…" + KEYS[0][-4:] in page.text and KEYS[0] not in page.text
    assert "secretplaintext" not in page.text
    [viewport] = [attrs for tag, attrs in Page(page.text).elements
                  if tag == "meta" and attrs.get("name") == "viewport"]
    settings = dict(item.strip().split("=", 1) for item in viewport["content"].split(","))
    assert settings["width"] == "device-width"
    assert float(settings["initial-scale"]) == 1


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
    assert login(client).status_code == 303
    r = client.post("/admin/keys", data={"label": "main", "key": KEYS[0]}, headers=ORIGIN,
                    follow_redirects=False)
    assert r.status_code == 303
    [key] = store.list_keys()
    assert key["label"] == "main"
    [(raw_url, probed_key)] = env.seen
    url = httpx.URL(raw_url)
    assert url.copy_with(query=None) == httpx.URL("http://upstream.test/v1beta/models")
    assert url.params["pageSize"] == "1"
    assert probed_key == KEYS[0]
    dup = client.post("/admin/keys", data={"label": "again", "key": KEYS[0]}, headers=ORIGIN)
    assert dup.status_code == 409 and len(store.list_keys()) == 1

    env.script[KEYS[1]] = [(400, {}, b'{"error":{"details":[{"reason":"API_KEY_INVALID"}]}}')]
    bad = client.post("/admin/keys", data={"label": "bad", "key": KEYS[1]}, headers=ORIGIN)
    assert bad.status_code == 400 and len(store.list_keys()) == 1

    kid = key["id"]
    env.seen.clear()
    assert client.post(f"/admin/keys/{kid}/test", headers=ORIGIN).status_code == 200
    assert env.keys_used == [KEYS[0]]
    assert client.post(f"/admin/keys/{kid}/disable", headers=ORIGIN).status_code == 200
    assert store.get_key(kid)["status"] == "disabled"
    assert post(client).json() == {"error": "no_gemini_keys"}
    assert client.post(f"/admin/keys/{kid}/enable", headers=ORIGIN).status_code == 200
    assert store.get_key(kid)["status"] == "active"
    assert post(client).status_code == 200

    delete_url = f"/admin/keys/{kid}/delete"
    confirm = client.get(delete_url)
    assert confirm.status_code == 200
    page = Page(confirm.text)
    [inputs] = [inputs for form, inputs in page.forms
                if form.get("method") == "post" and form.get("action") == delete_url]
    assert any(field.get("type") == "hidden" and field.get("name") == "confirm"
               and field.get("value") == "yes" for field in inputs)
    assert client.post(delete_url, headers=ORIGIN).status_code == 400
    assert store.get_key(kid) is not None
    assert client.post(delete_url, data={"confirm": "yes"}, headers=ORIGIN).status_code == 200
    assert store.get_key(kid) is None


def test_test_does_not_reenable_disabled_key_but_rejected_returns(client, env):
    assert login(client).status_code == 303
    (kid,) = add(1)
    probe = f"/admin/keys/{kid}/test"
    store.set_status(kid, "disabled")
    assert client.post(probe, headers=ORIGIN).status_code == 200
    assert env.keys_used == [KEYS[0]]
    assert store.get_key(kid)["status"] == "disabled"
    assert post(client).json() == {"error": "no_gemini_keys"}
    assert env.keys_used == [KEYS[0]]

    store.set_status(kid, "active")
    store.mark_rejected(kid, "403")
    assert store.get_key(kid)["status"] == "rejected"
    assert client.post(probe, headers=ORIGIN).status_code == 200
    assert env.keys_used == [KEYS[0], KEYS[0]]
    assert store.get_key(kid)["status"] == "active"
    assert post(client).status_code == 200


def test_admin_add_key_429_saved_with_note_but_invalid_refused(client, env):
    login(client)
    env.script[KEYS[0]] = [(429, {}, b'{"error":{"status":"RESOURCE_EXHAUSTED"}}')]
    r = client.post("/admin/keys", data={"label": "busy", "key": KEYS[0]}, headers=ORIGIN)
    assert r.status_code == 200
    assert any(tag == "p" and "warn" in attrs.get("class", "").split()
               for tag, attrs in Page(r.text).elements)
    [key] = store.list_keys()
    assert key["label"] == "busy" and key["status"] == "active"
    for i, st in ((1, 401), (2, 403)):
        env.script[KEYS[i]] = [(st, {}, b"{}")]
        assert client.post("/admin/keys", data={"label": f"x{i}", "key": KEYS[i]},
                           headers=ORIGIN).status_code == 400
    assert len(store.list_keys()) == 1


def test_admin_shows_recent_requests(client):
    assert login(client).status_code == 303
    assert post(client).status_code == 503  # empty pool
    page = client.get("/admin")
    assert page.status_code == 200
    assert any({"503", "generateContent"} <= set(cells) for cells in Page(page.text).rows)


def test_admin_disabled_without_password(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    assert client.get("/admin/login").status_code == 503
