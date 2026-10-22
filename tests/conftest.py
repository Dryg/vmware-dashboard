import time

import pytest


@pytest.fixture(autouse=True)
def utc_local_time(monkeypatch):
    """Local-time formatting is tested in UTC so expectations are fixed."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()
