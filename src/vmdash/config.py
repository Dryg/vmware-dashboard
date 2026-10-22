"""Settings for both processes, read from environment and a .env file."""

import argparse
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    sqlite_path: Path = Path("vmdash.db")
    log_level: str = "INFO"
    log_file: Path | None = None


class CollectorSettings(BaseConfig):
    vcenter_host: str
    vcenter_user: str
    vcenter_password: SecretStr
    vcenter_verify_ssl: bool = True
    vcenter_datacenter: str = ""
    sync_interval_minutes: int = Field(15, ge=1)


class WebSettings(BaseConfig):
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    web_user: str
    web_password: SecretStr
    web_ssl_cert: Path | None = None
    web_ssl_key: Path | None = None


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-file", type=Path, default=None, help="path to .env file (default: ./.env)")
    parser.add_argument("--db", type=Path, default=None, help="SQLite file (overrides SQLITE_PATH)")


def load_settings[T: BaseConfig](cls: type[T], args: argparse.Namespace) -> T:
    settings = cls(_env_file=args.env_file or ".env")
    if args.db is not None:
        settings = settings.model_copy(update={"sqlite_path": args.db})
    return settings
