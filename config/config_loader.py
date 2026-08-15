"""
config/config_loader.py

Loads and validates configuration from config/settings.yaml (non-secret,
committed) and .env (secret, gitignored). This is the single source of
truth for runtime configuration — every other module gets its config by
calling `load_settings()`, never by reading files or env vars directly.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

REQUIRED_ENV_VARS: list[str] = []  # nothing is unconditionally required — see FYERS_ENV_VARS below
FYERS_ENV_VARS = ["FYERS_CLIENT_ID", "FYERS_SECRET_KEY", "FYERS_REDIRECT_URI"]
REQUIRED_YAML_SECTIONS = [
    "app", "capital", "instruments", "timeframes",
    "session", "option_selection", "execution", "risk",
]
REQUIRED_INSTRUMENT_KEYS = ("name", "spot_key", "lot_size", "strike_interval")


class ConfigError(RuntimeError):
    """Raised when configuration is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class EnvConfig:
    """Secrets and environment-level settings, sourced from .env.

    client_id/secret_key/redirect_uri are Fyers-specific and OPTIONAL
    here — they're only required by the direct `python main.py`-via-.env
    path (see main.py's _get_broker_adapter(), which checks for them
    explicitly with a clear error). The multi-broker web UI / session
    manager path gets broker credentials from the encrypted vault per
    client instead and never needs these three at all — requiring them
    unconditionally here was a real bug: starting an AngelOne session
    failed with a Fyers-credentials error that had nothing to do with
    AngelOne.
    """
    client_id: Optional[str]
    secret_key: Optional[str]
    redirect_uri: Optional[str]
    paper_mode: bool
    auth_code: Optional[str]
    token_store_path: Path


@dataclass(frozen=True)
class Settings:
    """Everything the bot needs at runtime: non-secret YAML + secret env."""
    raw: dict[str, Any]
    env: EnvConfig

    @property
    def environment(self) -> str:
        return self.raw["app"]["environment"]

    @property
    def timezone(self) -> str:
        return self.raw["app"]["timezone"]

    @property
    def instruments(self) -> list[dict[str, Any]]:
        return self.raw["instruments"]

    @property
    def capital(self) -> dict[str, Any]:
        return self.raw["capital"]

    @property
    def session(self) -> dict[str, Any]:
        return self.raw["session"]

    @property
    def execution(self) -> dict[str, Any]:
        return self.raw["execution"]

    @property
    def risk(self) -> dict[str, Any]:
        return self.raw["risk"]

    def instrument(self, name: str) -> dict[str, Any]:
        for inst in self.instruments:
            if inst["name"].upper() == name.upper():
                return inst
        raise ConfigError(f"Instrument '{name}' not found in settings.yaml")


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Settings file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not data:
        raise ConfigError(f"Settings file is empty or invalid YAML: {path}")
    return data


def _str_to_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def _load_env(env_path: Path) -> EnvConfig:
    # override=False: a real environment variable exported by the process
    # manager (e.g. in production/CI) always wins over a local .env file,
    # which is what you want when .env is only a development convenience.
    load_dotenv(dotenv_path=env_path, override=False)

    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): " + ", ".join(missing) +
            f". Copy {PROJECT_ROOT / '.env.example'} to .env and fill these in."
        )

    paper_mode_val = _str_to_bool(os.getenv("PAPER_MODE", "true"))

    token_store_path = Path(os.getenv("TOKEN_STORE_PATH", "secrets/token_store.json"))
    if not token_store_path.is_absolute():
        token_store_path = PROJECT_ROOT / token_store_path

    return EnvConfig(
        client_id=os.getenv("FYERS_CLIENT_ID") or None,
        secret_key=os.getenv("FYERS_SECRET_KEY") or None,
        redirect_uri=os.getenv("FYERS_REDIRECT_URI") or None,
        paper_mode=paper_mode_val,
        auth_code=os.getenv("FYERS_AUTH_CODE") or None,
        token_store_path=token_store_path,
    )


def _validate_yaml(raw: dict[str, Any]) -> None:
    missing_sections = [s for s in REQUIRED_YAML_SECTIONS if s not in raw]
    if missing_sections:
        raise ConfigError(f"settings.yaml is missing section(s): {missing_sections}")

    if not raw["instruments"]:
        raise ConfigError("settings.yaml must define at least one instrument")

    for inst in raw["instruments"]:
        missing_keys = [k for k in REQUIRED_INSTRUMENT_KEYS if k not in inst]
        if missing_keys:
            raise ConfigError(f"Instrument entry {inst} missing key(s): {missing_keys}")

    env_name = raw["app"].get("environment")
    if env_name not in ("paper", "live"):
        raise ConfigError(
            f"app.environment must be 'paper' or 'live', got: {env_name!r}"
        )

    risk_pct = raw["capital"].get("risk_per_trade_pct")
    if not risk_pct or not (0 < risk_pct <= 100):
        raise ConfigError(f"capital.risk_per_trade_pct must be in (0, 100], got: {risk_pct}")


def load_settings(
    settings_path: Path | str = DEFAULT_SETTINGS_PATH,
    env_path: Path | str = DEFAULT_ENV_PATH,
) -> Settings:
    """Load and validate settings.yaml + .env into a single Settings object.

    Raises ConfigError with a specific, actionable message on any problem —
    callers should let this propagate at startup rather than catching it,
    since a misconfigured bot should not be allowed to run.
    """
    settings_path = Path(settings_path)
    env_path = Path(env_path)

    raw = _load_yaml(settings_path)
    _validate_yaml(raw)
    env = _load_env(env_path)

    logger.info(
        "Configuration loaded: environment=%s paper_mode=%s instruments=%s",
        raw["app"]["environment"],
        env.paper_mode,
        [i["name"] for i in raw["instruments"]],
    )
    return Settings(raw=raw, env=env)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    print(f"Environment : {settings.environment}")
    print(f"Paper mode  : {settings.env.paper_mode}")
    print(f"Instruments : {[i['name'] for i in settings.instruments]}")
    print(f"Token store : {settings.env.token_store_path}")
