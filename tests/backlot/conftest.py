"""Shared Backlot test fixtures.

Reset the in-process rate limiter around every test so ordinary API tests never
inherit a neighbor's request budget (and the dedicated rate-limit tests start
from a clean slate)."""

from __future__ import annotations

import pytest

from backlot import server as _server


@pytest.fixture(autouse=True)
def _reset_backlot_rate_limits():
    _server.reset_rate_limits()
    yield
    _server.reset_rate_limits()
