"""In-process rate-limit guard on expensive/sensitive Backlot mutations.

Deterministic via an injected monotonic clock. Safe GETs are never limited; each
sensitive endpoint has its own per-client bucket; a tripped bucket returns a
sanitized 429 + Retry-After; stale buckets are evicted (memory bound). No real
keychain/CLI: credential 'status' just reports availability and unknown-engine
action calls 400 fast — we assert on HTTP status codes only.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backlot import server as server_mod


@pytest.fixture
def clock(monkeypatch):
    state = {"t": 1000.0}
    monkeypatch.setattr(server_mod, "_rate_now", lambda: state["t"])
    return state


@pytest.fixture
def client(monkeypatch):
    async def no_watch():
        return None
    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as c:
        yield c


def _post(client, url, body):
    token = client.get("/api/csrf").json()["csrf"]
    return client.post(url, json=body, headers={"X-OpenMontage-CSRF": token})


_CRED = {"engine": "zai", "action": "status"}       # -> 200 (reports availability)
_ACTION = {"engine": "nope", "action": "status"}    # -> 400 (unknown engine), still counted


def test_credential_allows_up_to_limit_then_429(client, clock):
    limit, _ = server_mod._RATE_LIMITS["credential"]
    for i in range(limit):
        r = _post(client, "/api/providers/credential", _CRED)
        assert r.status_code != 429, f"unexpected 429 at request #{i}"
    r = _post(client, "/api/providers/credential", _CRED)
    assert r.status_code == 429
    assert "please slow down" in r.json()["detail"].lower()
    assert int(r.headers["retry-after"]) >= 1


def test_window_resets_after_advancing_clock(client, clock):
    limit, window = server_mod._RATE_LIMITS["credential"]
    for _ in range(limit):
        _post(client, "/api/providers/credential", _CRED)
    assert _post(client, "/api/providers/credential", _CRED).status_code == 429
    clock["t"] += window + 0.1  # slide past the window
    assert _post(client, "/api/providers/credential", _CRED).status_code != 429


def test_get_and_csrf_never_rate_limited(client, clock):
    for _ in range(server_mod._RATE_LIMITS["credential"][0] * 3):
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/csrf").status_code == 200


def test_action_bucket_enforced_and_independent_of_credential(client, clock):
    limit, _ = server_mod._RATE_LIMITS["action"]
    for i in range(limit):
        r = _post(client, "/api/providers/action", _ACTION)
        assert r.status_code != 429, f"unexpected 429 at action #{i}"
        assert r.status_code == 400  # unknown engine, but the request WAS counted
    assert _post(client, "/api/providers/action", _ACTION).status_code == 429
    # credential bucket is separate and still open
    assert _post(client, "/api/providers/credential", _CRED).status_code != 429


def test_projects_bucket_enforced(client, clock):
    limit, _ = server_mod._RATE_LIMITS["projects"]
    bad = {"title": "", "brief": "x", "pipeline": "animation"}  # invalid -> fast 4xx
    for i in range(limit):
        r = _post(client, "/api/projects", bad)
        assert r.status_code != 429, f"unexpected 429 at project #{i}"
    assert _post(client, "/api/projects", bad).status_code == 429


def test_retry_after_is_sane(client, clock):
    limit, window = server_mod._RATE_LIMITS["credential"]
    for _ in range(limit):
        _post(client, "/api/providers/credential", _CRED)
    r = _post(client, "/api/providers/credential", _CRED)
    assert r.status_code == 429
    assert 1 <= int(r.headers["retry-after"]) <= int(window) + 1


def test_stale_buckets_are_evicted(client, clock):
    for _ in range(3):
        _post(client, "/api/providers/credential", _CRED)
    assert server_mod._rate_limiter._buckets  # something tracked
    clock["t"] += server_mod._RATE_MAX_WINDOW + 5  # everything now stale
    _post(client, "/api/providers/credential", _CRED)  # triggers evict_stale
    # only the just-touched bucket should remain tracked
    assert len(server_mod._rate_limiter._buckets) <= 1


def test_proxy_headers_do_not_split_client_bucket(client, clock):
    """X-Forwarded-For is NOT trusted: spoofing it must not grant a fresh budget."""
    limit, _ = server_mod._RATE_LIMITS["credential"]
    token = client.get("/api/csrf").json()["csrf"]
    for _ in range(limit):
        client.post("/api/providers/credential", json=_CRED,
                    headers={"X-OpenMontage-CSRF": token, "X-Forwarded-For": "9.9.9.9"})
    r = client.post("/api/providers/credential", json=_CRED,
                    headers={"X-OpenMontage-CSRF": token, "X-Forwarded-For": "8.8.8.8"})
    assert r.status_code == 429  # same real peer -> same bucket regardless of XFF
