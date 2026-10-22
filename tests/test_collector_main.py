import threading
import time

import pytest

from vmdash.collector import main as cmain
from vmdash.config import CollectorSettings
from vmdash.db import connect_rw, start_run


@pytest.fixture
def settings(tmp_path):
    return CollectorSettings(vcenter_host="vc", vcenter_user="u", vcenter_password="p",
                             sqlite_path=tmp_path / "a.db", _env_file=None)


def test_run_forever_stops_promptly_while_waiting(settings):
    stop, calls = threading.Event(), []
    t = threading.Thread(target=cmain.run_forever,
                         args=(settings, None, stop), kwargs={"sync": lambda s, c: calls.append(1)})
    t.start()
    time.sleep(0.2)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
    assert calls == [1]


def test_run_forever_repeats_on_interval(settings):
    fast = settings.model_copy(update={"sync_interval_minutes": 0.001})  # 60 ms
    stop, calls = threading.Event(), []

    def sync(s, c):
        calls.append(1)
        if len(calls) == 3:
            stop.set()

    cmain.run_forever(fast, None, stop, sync=sync)
    assert len(calls) == 3


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("VCENTER_HOST=vc\nVCENTER_USER=u\nVCENTER_PASSWORD=p\n")
    return tmp_path


@pytest.mark.parametrize(("status", "code"), [("success", 0), ("partial", 0), ("failed", 1)])
def test_once_exit_codes(env, monkeypatch, status, code):
    monkeypatch.setattr(cmain, "run_sync", lambda s, c: status)
    assert cmain.main(["--once", "--db", str(env / "x.db")]) == code


def test_main_marks_stale_runs_failed(env, monkeypatch):
    conn = connect_rw(env / "x.db")
    start_run(conn)
    conn.close()
    monkeypatch.setattr(cmain, "run_sync", lambda s, c: "success")
    cmain.main(["--once", "--db", str(env / "x.db")])
    conn = connect_rw(env / "x.db")
    assert conn.execute("select status from sync_runs").fetchone()[0] == "failed"


def test_invalid_settings_exit_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("VCENTER_HOST", "VCENTER_USER", "VCENTER_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert cmain.main(["--once"]) == 2
