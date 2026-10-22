import argparse
from pathlib import Path

import pytest
from pydantic import ValidationError

from vmdash.config import (
    CollectorSettings,
    WebSettings,
    add_common_args,
    host_port,
    https_url,
    load_settings,
    vcenter_tls,
)

COLLECTOR_ENV = "VCENTER_HOST=vc\nVCENTER_USER=u\nVCENTER_PASSWORD=secret\n"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("VCENTER_HOST", "VCENTER_USER", "VCENTER_PASSWORD", "SQLITE_PATH", "WEB_USER",
                "WEB_PASSWORD", "SYNC_INTERVAL_MINUTES", "VCENTER_DATACENTER", "LOG_LEVEL"):
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


@pytest.mark.parametrize("value, expected", [
    ("vc", ("vc", 443)), ("vc:8443", ("vc", 8443)), ("10.0.0.5:9443", ("10.0.0.5", 9443)),
    ("2001:db8::5", ("2001:db8::5", 443)), ("[2001:db8::5]", ("2001:db8::5", 443)),
    ("[2001:db8::5]:8443", ("2001:db8::5", 8443)),
])
def test_host_port(value, expected):
    assert host_port(value) == expected


def test_https_url_brackets_ipv6():
    assert https_url("vc") == "https://vc:443"
    assert https_url("2001:db8::5") == "https://[2001:db8::5]:443"
    assert https_url("[2001:db8::5]:8443") == "https://[2001:db8::5]:8443"


def test_log_level_is_validated(tmp_path):
    env = tmp_path / "a.env"
    env.write_text(COLLECTOR_ENV + "LOG_LEVEL=debug\n")
    assert load_settings(CollectorSettings, parse(["--env-file", str(env)])).log_level == "DEBUG"
    env.write_text(COLLECTOR_ENV + "LOG_LEVEL=verbose\n")
    with pytest.raises(ValidationError, match="log_level"):
        load_settings(CollectorSettings, parse(["--env-file", str(env)]))


def test_example_web_password_is_rejected(tmp_path):
    env = tmp_path / "w.env"
    env.write_text("WEB_USER=admin\nWEB_PASSWORD=change-me\n")
    with pytest.raises(ValidationError, match="example value"):
        load_settings(WebSettings, parse(["--env-file", str(env)]))


def test_vcenter_tls_uses_ca_file():
    import ssl

    import certifi
    base = dict(vcenter_host="vc", vcenter_user="u", vcenter_password="p", _env_file=None)
    assert vcenter_tls(CollectorSettings(**base, vcenter_verify_ssl=False)) is None
    ctx = vcenter_tls(CollectorSettings(**base, vcenter_ca_file=Path(certifi.where())))
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert len(ctx.get_ca_certs()) > 10
    with pytest.raises(FileNotFoundError):
        vcenter_tls(CollectorSettings(**base, vcenter_ca_file=Path("/nonexistent/ca.pem")))
