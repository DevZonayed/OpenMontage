"""Hermes / Mochlet connection layer — status, guided connect (no live I/O)."""

from __future__ import annotations

import pytest

from lib.production_brain import connection as C
from lib.production_brain.orchestrator import ORCHESTRATOR_TOKEN_ACCOUNT


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _transport(status_code=200, body=None, exc=None):
    calls = []

    def call(url, *, timeout, headers=None):
        calls.append({"url": url, "timeout": timeout, "headers": headers})
        if exc is not None:
            raise exc
        return _Resp(status_code, body)

    call.calls = calls
    return call


class _MemSecrets:
    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def getter(self, account):
        return self.store.get(account)

    def setter(self, account, value):
        self.store[account] = value

    def deleter(self, account):
        return self.store.pop(account, None) is not None


# --------------------------------------------------------------------------- #
# Health probe
# --------------------------------------------------------------------------- #
def test_probe_healthy_service():
    t = _transport(200, {"service": "mochlet", "status": "ok"})
    h = C.probe_health(C.DEFAULT_MOCHLET_URL, transport=t)
    assert h["reachable"] and h["healthy"]
    assert h["service"] == "mochlet"
    assert t.calls[0]["url"].endswith("/health")


def test_probe_forwards_bearer_token():
    t = _transport(200, {"service": "mochlet"})
    C.probe_health(C.DEFAULT_MOCHLET_URL, transport=t, token="tok-abc")
    assert t.calls[0]["headers"] == {"Authorization": "Bearer tok-abc"}


def test_connect_verify_sends_stored_token(tmp_path):
    # A /health that requires auth (401 without token, 200 with it) must succeed
    # because connect verifies WITH the credential it just stored.
    secrets = _MemSecrets()
    seen = {}

    def call(url, *, timeout, headers=None):
        seen["headers"] = headers
        # authorized only when the bearer header is present
        return _Resp(200 if headers and "Authorization" in headers else 401,
                     {"service": "mochlet"})

    s = C.connect(url=C.DEFAULT_MOCHLET_URL, token="tok-xyz", base_dir=tmp_path,
                  transport=call, secret_setter=secrets.setter, secret_getter=secrets.getter,
                  env={})
    assert s["status"] == "connected" and s["available"] is True
    assert seen["headers"] == {"Authorization": "Bearer tok-xyz"}


def test_probe_connection_refused_is_unreachable():
    t = _transport(exc=ConnectionError("refused"))
    h = C.probe_health(C.DEFAULT_MOCHLET_URL, transport=t)
    assert h["reachable"] is False and h["healthy"] is False


def test_probe_rejects_redirect():
    t = _transport(302)
    h = C.probe_health(C.DEFAULT_MOCHLET_URL, transport=t)
    assert h["reachable"] is True and h["healthy"] is False


def test_probe_invalid_endpoint():
    h = C.probe_health("ftp://nope", transport=_transport(200))
    assert h["reachable"] is False


def test_probe_zero_status_is_not_connected():
    # A response object lacking a usable status_code must NOT be reported healthy.
    h = C.probe_health(C.DEFAULT_MOCHLET_URL, transport=_transport(0))
    assert h["reachable"] is False and h["healthy"] is False


def test_probe_1xx_is_not_healthy():
    h = C.probe_health(C.DEFAULT_MOCHLET_URL, transport=_transport(100))
    assert h["healthy"] is False


# --------------------------------------------------------------------------- #
# Connection status
# --------------------------------------------------------------------------- #
def test_status_needs_setup_when_nothing_configured(tmp_path):
    t = _transport(exc=ConnectionError("refused"))  # mochlet not running
    s = C.connection_status(env={}, base_dir=tmp_path, transport=t,
                            secret_getter=_MemSecrets().getter)
    assert s["status"] == "needs_setup"
    assert s["available"] is False
    assert any(a["id"] == "connect_hermes" for a in s["actions"])


def test_status_detected_when_mochlet_running_but_unconfigured(tmp_path):
    t = _transport(200, {"service": "mochlet"})
    s = C.connection_status(env={}, base_dir=tmp_path, transport=t,
                            secret_getter=_MemSecrets().getter)
    assert s["status"] == "detected"
    assert s["available"] is False  # not connected yet — must go through Connect
    assert s["suggested_endpoint"] == C.DEFAULT_MOCHLET_URL


def test_status_connected_when_endpoint_persisted_and_healthy(tmp_path):
    C._persist_endpoint(C.DEFAULT_MOCHLET_URL, base_dir=tmp_path)
    t = _transport(200, {"service": "mochlet"})
    s = C.connection_status(env={}, base_dir=tmp_path, transport=t,
                            secret_getter=_MemSecrets().getter)
    assert s["status"] == "connected"
    assert s["available"] is True


def test_status_unreachable_when_configured_but_down(tmp_path):
    C._persist_endpoint(C.DEFAULT_MOCHLET_URL, base_dir=tmp_path)
    t = _transport(exc=TimeoutError("down"))
    s = C.connection_status(env={}, base_dir=tmp_path, transport=t,
                            secret_getter=_MemSecrets().getter)
    assert s["status"] == "unreachable"
    assert s["available"] is False


def test_status_needs_token_on_401(tmp_path):
    C._persist_endpoint(C.DEFAULT_MOCHLET_URL, base_dir=tmp_path)
    t = _transport(401)
    s = C.connection_status(env={}, base_dir=tmp_path, transport=t,
                            secret_getter=_MemSecrets().getter)
    assert s["status"] == "needs_token"


def test_status_never_returns_token(tmp_path):
    secrets = _MemSecrets({ORCHESTRATOR_TOKEN_ACCOUNT: "super-secret-value"})
    C._persist_endpoint(C.DEFAULT_MOCHLET_URL, base_dir=tmp_path)
    s = C.connection_status(env={}, base_dir=tmp_path, transport=_transport(200, {}),
                            secret_getter=secrets.getter)
    assert s["token_configured"] is True
    assert "super-secret-value" not in str(s)


# --------------------------------------------------------------------------- #
# Guided connect
# --------------------------------------------------------------------------- #
def test_connect_success_persists_and_enables(tmp_path):
    secrets = _MemSecrets()
    t = _transport(200, {"service": "mochlet"})
    s = C.connect(url=C.DEFAULT_MOCHLET_URL, token="tok-123", base_dir=tmp_path,
                  transport=t, secret_setter=secrets.setter, secret_getter=secrets.getter,
                  env={})
    assert s["status"] == "connected"
    assert s["available"] is True
    # token stored in the (fake) keyring, endpoint persisted
    assert secrets.store[ORCHESTRATOR_TOKEN_ACCOUNT] == "tok-123"
    assert C.stored_endpoint(tmp_path) == C.DEFAULT_MOCHLET_URL
    # the live client the adapter builds now points at the persisted endpoint
    client = C.build_live_client(env={}, base_dir=tmp_path)
    assert client.available() is True


def test_connect_unreachable_does_not_persist(tmp_path):
    secrets = _MemSecrets()
    t = _transport(exc=ConnectionError("refused"))
    s = C.connect(url=C.DEFAULT_MOCHLET_URL, token="tok", base_dir=tmp_path,
                  transport=t, secret_setter=secrets.setter, secret_getter=secrets.getter,
                  env={})
    assert s["status"] == "unreachable"
    assert s["available"] is False
    assert C.stored_endpoint(tmp_path) is None  # fail-closed: nothing persisted


def test_connect_rejects_bad_endpoint(tmp_path):
    with pytest.raises(C.ConnectionError):
        C.connect(url="http://example.com", base_dir=tmp_path,  # non-loopback plain http
                  transport=_transport(200), secret_setter=_MemSecrets().setter, env={})


def test_connect_401_reports_needs_token_no_persist(tmp_path):
    secrets = _MemSecrets()
    t = _transport(401)
    s = C.connect(url=C.DEFAULT_MOCHLET_URL, token="bad", base_dir=tmp_path,
                  transport=t, secret_setter=secrets.setter, secret_getter=secrets.getter,
                  env={})
    assert s["status"] == "needs_token"
    assert C.stored_endpoint(tmp_path) is None


def test_connect_empty_token_rejected(tmp_path):
    with pytest.raises(C.ConnectionError):
        C.connect(url=C.DEFAULT_MOCHLET_URL, token="   ", base_dir=tmp_path,
                  transport=_transport(200), secret_setter=_MemSecrets().setter, env={})


# --------------------------------------------------------------------------- #
# Endpoint resolution precedence + fail-closed default
# --------------------------------------------------------------------------- #
def test_env_var_takes_precedence(tmp_path):
    C._persist_endpoint("http://127.0.0.1:9235", base_dir=tmp_path)
    ep = C.configured_endpoint(env={"OPENMONTAGE_HERMES_ORCHESTRATOR_URL": "https://hermes.example"},
                               base_dir=tmp_path)
    assert ep == "https://hermes.example"


def test_unconfigured_endpoint_is_none_not_mochlet(tmp_path):
    # The Mochlet loopback default is only a *suggestion* — never auto-adopted.
    assert C.configured_endpoint(env={}, base_dir=tmp_path) is None
    client = C.build_live_client(env={}, base_dir=tmp_path)
    assert client.available() is False


def test_disconnect_forgets_endpoint(tmp_path):
    secrets = _MemSecrets({ORCHESTRATOR_TOKEN_ACCOUNT: "x"})
    C._persist_endpoint(C.DEFAULT_MOCHLET_URL, base_dir=tmp_path)
    C.disconnect(base_dir=tmp_path, secret_deleter=secrets.deleter, wipe_token=True)
    assert C.stored_endpoint(tmp_path) is None
    assert secrets.getter(ORCHESTRATOR_TOKEN_ACCOUNT) is None
