import argparse
from pathlib import Path

import pytest
from pydantic import ValidationError

from vmdash.config import CollectorSettings, WebSettings, add_common_args, load_settings

COLLECTOR_ENV = "VCENTER_HOST=vc\nVCENTER_USER=u\nVCENTER_PASSWORD=secret\n"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("VCENTER_HOST", "VCENTER_USER", "VCENTER_PASSWORD", "SQLITE_PATH", "WEB_USER",
                "WEB_PASSWORD", "SYNC_INTERVAL_MINUTES", "VCENTER_DATACENTER"):
        monkeypatch.delenv(key, raising=False)


def parse(argv):
    p = argparse.ArgumentParser()
    add_common_args(p)
    return p.parse_args(argv)


def test_collector_settings_from_env_file(tmp_path):
    env = tmp_path / "x.env"
    env.write_text(COLLECTOR_ENV + "SQLITE_PATH=/data/a.db\nVCENTER_DATACENTER=\n")
    s = load_settings(CollectorSettings, parse(["--env-file", str(env)]))
    assert s.vcenter_host == "vc"
    assert s.sqlite_path == Path("/data/a.db")
    assert s.vcenter_datacenter == ""
    assert s.sync_interval_minutes == 15
    assert s.vcenter_verify_ssl is True


def test_default_env_file_is_dot_env(tmp_path):
    (tmp_path / ".env").write_text(COLLECTOR_ENV)
    s = load_settings(CollectorSettings, parse([]))
    assert s.vcenter_user == "u"
    assert s.sqlite_path == Path("vmdash.db")


def test_db_flag_overrides_env(tmp_path):
    env = tmp_path / "x.env"
    env.write_text(COLLECTOR_ENV + "SQLITE_PATH=/data/a.db\n")
    s = load_settings(CollectorSettings, parse(["--env-file", str(env), "--db", "/tmp/b.db"]))
    assert s.sqlite_path == Path("/tmp/b.db")


def test_missing_required_raises():
    with pytest.raises(ValidationError):
        load_settings(WebSettings, parse([]))


def test_password_not_in_repr(tmp_path):
    (tmp_path / ".env").write_text(COLLECTOR_ENV)
    s = load_settings(CollectorSettings, parse([]))
    assert "secret" not in repr(s)
    assert s.vcenter_password.get_secret_value() == "secret"


def test_interval_must_be_positive(tmp_path):
    (tmp_path / ".env").write_text(COLLECTOR_ENV + "SYNC_INTERVAL_MINUTES=0\n")
    with pytest.raises(ValidationError):
        load_settings(CollectorSettings, parse([]))
