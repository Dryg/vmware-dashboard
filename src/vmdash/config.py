"""Settings for both processes, read from environment and a .env file."""

import argparse
import ssl
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Annotated[Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], BeforeValidator(str.upper)]


def host_port(host: str) -> tuple[str, int]:
    """VCENTER_HOST as (host, port): "vc", "vc:8443", "10.0.0.5", "2001:db8::5", "[2001:db8::5]:8443"."""
    if host.startswith("["):
        name, _, rest = host[1:].partition("]")
        port = rest.removeprefix(":")
        return name, int(port) if port.isdigit() else 443
    name, sep, port = host.rpartition(":")
    if sep and port.isdigit() and ":" not in name:
        return name, int(port)
    return host, 443


def https_url(host: str) -> str:
    name, port = host_port(host)
    return f"https://[{name}]:{port}" if ":" in name else f"https://{name}:{port}"


EXAMPLE_PASSWORD = "change-me"  # the placeholder in .env.example and examples/vmdash.env


def settings_errors(e: ValidationError) -> str:
    """One line per invalid setting, by env variable name. Messages never contain the value itself."""
    return "; ".join(f"{'.'.join(map(str, err['loc'])).upper()}: {err['msg']}" for err in e.errors())


class BaseConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    sqlite_path: Path = Path("vmdash.db")
    log_level: LogLevel = "INFO"
    log_file: Path | None = None


class CollectorSettings(BaseConfig):
    vcenter_host: str
    vcenter_user: str
    vcenter_password: SecretStr
    vcenter_verify_ssl: bool = True
    vcenter_ca_file: Path | None = None  # PEM bundle for a private CA; default: the system trust store
    vcenter_datacenter: str = ""
    sync_interval_minutes: int = Field(15, ge=1)


def vcenter_tls(settings: CollectorSettings) -> ssl.SSLContext | None:
    """TLS context for both vCenter clients (pyVmomi and the tags REST client); None = do not verify."""
    if not settings.vcenter_verify_ssl:
        return None
    return ssl.create_default_context(cafile=settings.vcenter_ca_file)


class WebSettings(BaseConfig):
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    web_user: str
    web_password: SecretStr
    web_ssl_cert: Path | None = None
    web_ssl_key: Path | None = None

    @field_validator("web_password")
    @classmethod
    def not_the_example(cls, v: SecretStr) -> SecretStr:
        if v.get_secret_value() == EXAMPLE_PASSWORD:
            raise ValueError(f"still the example value '{EXAMPLE_PASSWORD}', set a real password")
        return v


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-file", type=Path, default=None, help="path to .env file (default: ./.env)")
    parser.add_argument("--db", type=Path, default=None, help="SQLite file (overrides SQLITE_PATH)")


def load_settings[T: BaseConfig](cls: type[T], args: argparse.Namespace) -> T:
    settings = cls(_env_file=args.env_file or ".env")
    if args.db is not None:
        settings = settings.model_copy(update={"sqlite_path": args.db})
    return settings
