"""Login page: no caching, and every CSRF failure lands on the login page with a friendly message."""
from sentinel.config import load_config

MESSAGE = "Your session expired. Please sign in again."


def test_login_page_is_no_store(app):
    r = app.test_client().get("/login")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"


def test_login_post_responses_are_no_store(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf"] = "tok"
    wrong = c.post("/login", data={"password": "bad", "csrf": "tok"})
    assert wrong.status_code == 200 and wrong.headers["Cache-Control"] == "no-store"
    ok = c.post("/login", data={"password": "pw", "csrf": "tok"})
    assert ok.status_code == 302 and ok.headers["Cache-Control"] == "no-store"


def test_missing_token_redirects_to_login_with_message(app):
    c = app.test_client()
    r = c.post("/login", data={"password": "pw"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    page = c.get("/login")
    assert MESSAGE in page.data.decode() and b"Bad Request" not in page.data


def test_expired_session_token_redirects_to_login_with_message(app):
    c = app.test_client()
    c.get("/login")  # page rendered, token issued ...
    c.delete_cookie("session")  # ... then the session is gone (expired / key changed)
    r = c.post("/login", data={"password": "pw", "csrf": "stale"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert MESSAGE in c.get("/login").data.decode()


def test_wrong_token_on_dashboard_post_redirects_and_does_nothing(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["auth"], s["csrf"] = True, "tok"
    r = c.post("/leads/delete", data={"csrf": "wrong", "ids": "1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert MESSAGE in c.get("/login").data.decode()


def test_valid_login_still_works(app):
    c = app.test_client()
    html = c.get("/login").data.decode()
    token = html.split('name="csrf" value="')[1].split('"')[0]
    r = c.post("/login", data={"password": "pw", "csrf": token})
    assert r.status_code == 302 and "/login" not in r.headers["Location"]
    assert c.get("/leads").status_code == 200


def test_session_cookie_is_persistent_with_a_lifetime(app):
    r = app.test_client().get("/login")
    cookie = next(h for h in r.headers.getlist("Set-Cookie") if h.startswith("session="))
    assert "Expires=" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_secret_key_is_stable_across_restarts_when_unset(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    assert load_config()["SECRET_KEY"] == load_config()["SECRET_KEY"]
    monkeypatch.setenv("SECRET_KEY", "explicit")
    assert load_config()["SECRET_KEY"] == "explicit"
