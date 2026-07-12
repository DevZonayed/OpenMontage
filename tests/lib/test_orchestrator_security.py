"""Transport hardening for the orchestration port (token exfil / URL / IDs).

  * endpoint policy is fail-closed: HTTPS only (loopback-HTTP exception); no
    userinfo / fragment / non-http / control chars / ambiguous host;
  * redirects are disabled and any 3xx is rejected — the bearer token is never
    replayed to a redirect target, and no second request is made;
  * external ids are validated against a strict bounded allowlist before they are
    persisted or interpolated into a URL path (which is percent-encoded);
  * the token never appears in an error/return value.
"""

from __future__ import annotations

import pytest

from lib.production_brain.orchestrator import (
    ConfiguredHermesOrchestratorClient,
    OrchestratorHandle,
    OrchestratorUnavailable,
    is_canonical_id,
    validate_endpoint,
)


class _FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class _Transport:
    """Records every outbound call; NEVER follows redirects (like allow_redirects=False)."""

    def __init__(self, response):
        self._response = response
        self.calls = []

    def __call__(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": dict(headers or {}), "timeout": timeout})
        return self._response


# --------------------------------------------------------------------------- #
# URL policy
# --------------------------------------------------------------------------- #
class TestEndpointPolicy:
    @pytest.mark.parametrize("url", [
        "https://hermes.example.com",
        "https://hermes.example.com/api",
        "http://127.0.0.1:8900",
        "http://localhost:8900/hermes",
        "http://[::1]:8900",
    ])
    def test_allowed(self, url):
        assert validate_endpoint(url) == url
        assert ConfiguredHermesOrchestratorClient(url=url).available() is True

    @pytest.mark.parametrize("url", [
        "http://hermes.example.com",          # remote plain HTTP
        "http://evil.example.com:80/hermes",  # remote plain HTTP
        "ftp://hermes.example.com",           # non-http scheme
        "file:///etc/passwd",                 # non-http scheme
        "https://user:pass@hermes.example.com",  # embedded credentials
        "https://hermes.example.com#frag",    # fragment
        "https://",                            # no host
        "https://hermes.example.com\n",       # control char
        "https://her mes.example.com",        # whitespace
        "javascript:alert(1)",                 # bogus scheme
        "https://example.com:badport",         # non-numeric port
        "https://example.com:99999",           # out-of-range port
        "https://example.com:-1",              # negative port
        "https://[::1",                        # malformed bracketed host
        "https://[not:an:ip]",                 # malformed bracketed host
        "",                                    # empty
        None,                                  # missing
    ])
    def test_rejected(self, url):
        # Fail-closed: every malformed input raises OrchestratorUnavailable (never
        # a bare ValueError) and reports unavailable.
        with pytest.raises(OrchestratorUnavailable):
            validate_endpoint(url)
        assert ConfiguredHermesOrchestratorClient(url=url).available() is False


# --------------------------------------------------------------------------- #
# Redirect / token safety
# --------------------------------------------------------------------------- #
class TestRedirectAndToken:
    def _client(self, transport, monkeypatch, token="super-secret-token"):
        from lib import secret_store
        from lib.production_brain.orchestrator import ORCHESTRATOR_TOKEN_ACCOUNT

        secret_store.set_secret(ORCHESTRATOR_TOKEN_ACCOUNT, token)
        return ConfiguredHermesOrchestratorClient(url="https://hermes.example.com", transport=transport)

    def test_redirect_is_rejected_and_no_second_request(self, monkeypatch):
        tr = _Transport(_FakeResp(status_code=302, body={}))
        c = self._client(tr, monkeypatch)
        with pytest.raises(OrchestratorUnavailable) as ei:
            c.create_job(project_id="p", run_id="run_1", requested_duration_seconds=60,
                         idempotency_key="p:run_1")
        # Exactly ONE request was made — the redirect was NOT followed.
        assert len(tr.calls) == 1
        assert tr.calls[0]["url"] == "https://hermes.example.com/jobs"
        # The token is never leaked into the error.
        assert "super-secret-token" not in str(ei.value)

    def test_token_only_sent_to_validated_endpoint(self, monkeypatch):
        tr = _Transport(_FakeResp(200, {"session_id": "sess-1", "job_id": "job-1"}))
        c = self._client(tr, monkeypatch, token="tok-123")
        c.create_job(project_id="p", run_id="run_1", requested_duration_seconds=60,
                     idempotency_key="p:run_1")
        assert len(tr.calls) == 1
        call = tr.calls[0]
        assert call["url"].startswith("https://hermes.example.com")
        assert call["headers"].get("Authorization") == "Bearer tok-123"

    def test_4xx_rejected(self, monkeypatch):
        tr = _Transport(_FakeResp(403, {}))
        c = self._client(tr, monkeypatch)
        with pytest.raises(OrchestratorUnavailable):
            c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")


# --------------------------------------------------------------------------- #
# Canonical id validation + encoding
# --------------------------------------------------------------------------- #
class TestCanonicalIds:
    @pytest.mark.parametrize("val", ["job-1", "sess_ABC.9", "run:123", "a" * 128])
    def test_valid(self, val):
        assert is_canonical_id(val) is True

    @pytest.mark.parametrize("val", [
        "job/1",          # slash
        "job\\1",         # backslash
        "..",             # traversal
        "a/../b",         # traversal
        "job..1",         # dot-dot
        "job%2f1",        # encoded slash literal
        "job\n1",         # newline
        "job 1",          # whitespace
        "a" * 129,        # too long
        "",               # empty
        123,              # non-string
        None,             # non-string
        {"x": 1},         # non-string object (never str()-coerced)
    ])
    def test_invalid(self, val):
        assert is_canonical_id(val) is False

    def test_handle_is_valid_checks_both(self):
        assert OrchestratorHandle("sess-1", "job-1").is_valid() is True
        assert OrchestratorHandle("sess/1", "job-1").is_valid() is False
        assert OrchestratorHandle("sess-1", "job/1").is_valid() is False

    def test_create_job_rejects_non_string_ids(self, monkeypatch):
        from lib import secret_store
        from lib.production_brain.orchestrator import ORCHESTRATOR_TOKEN_ACCOUNT

        secret_store.set_secret(ORCHESTRATOR_TOKEN_ACCOUNT, "tok")
        tr = _Transport(_FakeResp(200, {"session_id": 123, "job_id": {"a": 1}}))
        c = ConfiguredHermesOrchestratorClient(url="https://h.example.com", transport=tr)
        with pytest.raises(OrchestratorUnavailable):
            c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")

    def test_create_job_rejects_traversal_ids(self):
        tr = _Transport(_FakeResp(200, {"session_id": "ok-1", "job_id": "../escape"}))
        c = ConfiguredHermesOrchestratorClient(url="https://h.example.com", transport=tr)
        with pytest.raises(OrchestratorUnavailable):
            c.create_job(project_id="p", run_id="r", requested_duration_seconds=60, idempotency_key="k")

    def test_control_job_percent_encodes_path_but_rejects_bad_id(self):
        tr = _Transport(_FakeResp(200, {}))
        c = ConfiguredHermesOrchestratorClient(url="https://h.example.com", transport=tr)
        # a valid id is used verbatim (already allowlist-safe)
        c.control_job(job_id="job-1", action="retry", idempotency_key="k")
        assert tr.calls[0]["url"] == "https://h.example.com/jobs/job-1/retry"
        # a non-canonical id is refused BEFORE any request
        with pytest.raises(OrchestratorUnavailable):
            c.control_job(job_id="job/../1", action="cancel", idempotency_key="k")
        assert len(tr.calls) == 1  # no request for the bad id
