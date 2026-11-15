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


def test_fallback_key_is_hmac_of_the_seed_never_the_raw_value(monkeypatch, caplog):
    import hashlib
    import hmac
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2-password")
    with caplog.at_level("DEBUG"):
        key = load_config()["SECRET_KEY"]
    expected = hmac.new(b"hunter2-password", b"meganet-sentinel-session-key", hashlib.sha256).hexdigest()
    assert key == expected and key != "hunter2-password" and "hunter2-password" not in key
    assert "SECRET_KEY not set" in caplog.text
    assert key not in caplog.text and "hunter2-password" not in caplog.text  # log never leaks key or source


def test_fallback_key_from_database_url_is_not_the_url_and_differs_per_seed(monkeypatch, caplog):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://user:s3cretpw@host/db")
    with caplog.at_level("DEBUG"):
        a = load_config()["SECRET_KEY"]
    assert "s3cretpw" not in a and "postgres://" not in a and a not in caplog.text
    assert "s3cretpw" not in caplog.text and "postgres://" not in caplog.text
    monkeypatch.setenv("DATABASE_URL", "postgres://user:other@host/db")
    assert load_config()["SECRET_KEY"] != a


def test_production_cookie_flags_and_lifetime(monkeypatch):
    from datetime import timedelta
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("SESSION_LIFETIME_HOURS", raising=False)
    cfg = load_config()
    assert cfg["SESSION_COOKIE_SECURE"] is True  # Secure unless explicitly turned off for local http
    assert cfg["SESSION_COOKIE_HTTPONLY"] is True and cfg["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert cfg["PERMANENT_SESSION_LIFETIME"] == timedelta(hours=12)


def test_login_cookie_carries_secure_httponly_samesite_and_12h_expiry(app):
    from datetime import datetime, timedelta, timezone
    from email.utils import parsedate_to_datetime
    app.config["SESSION_COOKIE_SECURE"] = True
    r = app.test_client().get("/login", base_url="https://sentinel.test")
    cookie = next(h for h in r.headers.getlist("Set-Cookie") if h.startswith("session="))
    for flag in ("Secure", "HttpOnly", "SameSite=Lax"):
        assert flag in cookie
    expires = parsedate_to_datetime(cookie.split("Expires=")[1].split(";")[0])
    left = expires - datetime.now(timezone.utc)
    assert timedelta(hours=11, minutes=55) < left <= timedelta(hours=12)
