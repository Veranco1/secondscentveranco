"""
Fase 1 verification — run with:  python3 -m pytest tests/test_auth.py -v
(or, since pytest may not be present everywhere, this file also runs
standalone via `python3 tests/test_auth.py`.)

Covers, end-to-end, against the real Flask app + a real (temp) SQLite db:
  - registration works and returns a session
  - duplicate email is rejected
  - weak password is rejected
  - CSRF-protected endpoints reject requests without a valid token
  - login works with correct credentials, fails with wrong ones
  - login is rate-limited after repeated failures
  - /auth/me requires a session
  - logout actually clears the session
  - become-seller flips the flag only for the logged-in user
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DEV_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

from app import create_app  # noqa: E402

PASSED = 0
FAILED = 0


def check(label, condition):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok   - {label}")
    else:
        FAILED += 1
        print(f"  FAIL - {label}")


def get_csrf(client):
    resp = client.get("/auth/csrf-token")
    return resp.get_json()["csrf_token"]


def main():
    app = create_app()
    client = app.test_client()

    print("== register ==")
    csrf = get_csrf(client)
    resp = client.post("/auth/register", json={
        "email": "koper@example.nl", "password": "hunter22", "display_name": "Koper Een",
    }, headers={"X-CSRF-Token": csrf})
    check("register succeeds (201)", resp.status_code == 201)
    check("returns public user with is_buyer=True, is_seller=False",
          resp.get_json()["user"]["is_buyer"] is True and resp.get_json()["user"]["is_seller"] is False)
    check("password_hash never returned to client", "password_hash" not in resp.get_json()["user"])

    print("== duplicate email rejected ==")
    csrf2 = get_csrf(client)
    resp = client.post("/auth/register", json={
        "email": "koper@example.nl", "password": "anotherpass1", "display_name": "Dubbel",
    }, headers={"X-CSRF-Token": csrf2})
    check("duplicate email rejected (409)", resp.status_code == 409)

    print("== weak password rejected ==")
    csrf3 = get_csrf(client)
    resp = client.post("/auth/register", json={
        "email": "nieuw@example.nl", "password": "short", "display_name": "Kort",
    }, headers={"X-CSRF-Token": csrf3})
    check("weak password rejected (400)", resp.status_code == 400)

    print("== CSRF enforcement ==")
    resp = client.post("/auth/register", json={
        "email": "nocsrf@example.nl", "password": "hunter22", "display_name": "Geen CSRF",
    })  # no header at all
    check("missing CSRF token rejected (400)", resp.status_code == 400)

    print("== /auth/me requires session ==")
    fresh = app.test_client()
    resp = fresh.get("/auth/me")
    check("me() without session -> 401", resp.status_code == 401)

    print("== login ==")
    login_client = app.test_client()
    csrf4 = get_csrf(login_client)
    resp = login_client.post("/auth/login", json={
        "email": "koper@example.nl", "password": "wrongpassword",
    }, headers={"X-CSRF-Token": csrf4})
    check("wrong password -> 401", resp.status_code == 401)

    csrf5 = get_csrf(login_client)
    resp = login_client.post("/auth/login", json={
        "email": "koper@example.nl", "password": "hunter22",
    }, headers={"X-CSRF-Token": csrf5})
    check("correct password -> 200", resp.status_code == 200)

    resp = login_client.get("/auth/me")
    check("me() works after login", resp.status_code == 200 and resp.get_json()["user"]["email"] == "koper@example.nl")

    print("== rate limiting ==")
    rl_client = app.test_client()
    for i in range(5):
        c = get_csrf(rl_client)
        rl_client.post("/auth/login", json={"email": "koper@example.nl", "password": "nope"},
                        headers={"X-CSRF-Token": c})
    c = get_csrf(rl_client)
    resp = rl_client.post("/auth/login", json={"email": "koper@example.nl", "password": "hunter22"},
                            headers={"X-CSRF-Token": c})
    check("6th attempt locked out even with correct password (429)", resp.status_code == 429)

    print("== logout clears session ==")
    csrf6 = get_csrf(login_client)
    resp = login_client.post("/auth/logout", headers={"X-CSRF-Token": csrf6})
    check("logout -> 200", resp.status_code == 200)
    resp = login_client.get("/auth/me")
    check("me() after logout -> 401", resp.status_code == 401)

    print("== become-seller ==")
    seller_client = app.test_client()
    csrf7 = get_csrf(seller_client)
    seller_client.post("/auth/register", json={
        "email": "verkoper@example.nl", "password": "hunter22", "display_name": "Verkoper Een",
    }, headers={"X-CSRF-Token": csrf7})
    csrf8 = get_csrf(seller_client)
    resp = seller_client.post("/auth/become-seller", headers={"X-CSRF-Token": csrf8})
    check("become-seller -> 200", resp.status_code == 200)
    check("is_seller now True", resp.get_json()["user"]["is_seller"] is True)

    other_client = app.test_client()
    csrf9 = get_csrf(other_client)
    other_client.post("/auth/register", json={
        "email": "anders@example.nl", "password": "hunter22", "display_name": "Anders",
    }, headers={"X-CSRF-Token": csrf9})
    resp = other_client.get("/auth/me")
    check("a different, unrelated account is NOT affected (still is_seller=False)",
          resp.get_json()["user"]["is_seller"] is False)

    print(f"\n{PASSED} passed, {FAILED} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
