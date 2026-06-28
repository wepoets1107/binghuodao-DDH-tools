from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = ROOT_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
EVENTS_PATH = DATA_DIR / "events.jsonl"

DERIBIT_WS_HOSTS = {
    "testnet": "wss://test.deribit.com/ws/api/v2",
    "mainnet": "wss://www.deribit.com/ws/api/v2",
}


class ScheduleConfig(BaseModel):
    enabled: bool = True
    mode: Literal["hourly", "daily", "custom"] = "custom"
    timezone: str = "Asia/Shanghai"
    minute: int = Field(default=0, ge=0, le=59)
    times: list[str] = Field(default_factory=lambda: ["08:00", "16:00", "23:55"])
    force_rebalance: bool = True

    @field_validator("times")
    @classmethod
    def validate_times(cls, values: list[str]) -> list[str]:
        clean: list[str] = []
        for value in values:
            text = str(value).strip()
            parts = text.split(":")
            if len(parts) != 2:
                raise ValueError(f"Invalid time: {value}")
            hour = int(parts[0])
            minute = int(parts[1])
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                raise ValueError(f"Invalid time: {value}")
            clean.append(f"{hour:02d}:{minute:02d}")
        return clean


class AssetConfig(BaseModel):
    enabled: bool = True
    target_delta: float = 0.0
    positive_trigger_delta: float = Field(ge=0.0)
    negative_trigger_delta: float = Field(ge=0.0)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    hedge_ratio: float = Field(default=1.0, ge=0.0, le=5.0)
    hedge_instrument: str
    min_order_amount: float = Field(ge=0.0)
    max_order_amount: float = Field(gt=0.0)
    delta_to_order: Literal["index_price", "multiplier"] = "index_price"
    order_multiplier: float = Field(default=1.0, ge=0.0)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_threshold(cls, data):
        if isinstance(data, dict) and "trigger_delta" in data:
            old = data.get("trigger_delta")
            data.setdefault("positive_trigger_delta", old)
            data.setdefault("negative_trigger_delta", old)
            data.pop("trigger_delta", None)
        if isinstance(data, dict):
            data.pop("mock_net_delta", None)
            data.pop("amount_step", None)
        return data

    @field_validator("hedge_instrument")
    @classmethod
    def uppercase_instrument(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def validate_order_amounts(self):
        if self.min_order_amount > self.max_order_amount:
            raise ValueError("min_order_amount cannot be greater than max_order_amount.")
        return self


class AppConfig(BaseModel):
    mode: Literal["testnet", "mainnet"] = "testnet"
    dry_run: bool = False
    live_trading_armed: bool = True
    loop_interval_seconds: int = Field(default=15, ge=5, le=3600)
    order_type: Literal["limit", "market"] = "limit"
    slippage_bps: float = Field(default=5.0, ge=0.0, le=500.0)
    cooldown_seconds: int = Field(default=60, ge=0, le=86400)
    maker_wait_seconds: int = Field(default=60, ge=10, le=3600)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    assets: dict[str, AssetConfig] = Field(
        default_factory=lambda: {
            "BTC": AssetConfig(
                positive_trigger_delta=0.2,
                negative_trigger_delta=0.2,
                hedge_instrument="BTC-PERPETUAL",
                min_order_amount=0.001,
                max_order_amount=1.0,
            ),
            "ETH": AssetConfig(
                positive_trigger_delta=5.0,
                negative_trigger_delta=5.0,
                hedge_instrument="ETH-PERPETUAL",
                min_order_amount=0.001,
                max_order_amount=20.0,
            ),
        }
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_asset_schedules(cls, data):
        if isinstance(data, dict):
            legacy_schedule = data.get("schedule")
            assets = data.get("assets")
            if isinstance(legacy_schedule, dict) and isinstance(assets, dict):
                for asset in assets.values():
                    if isinstance(asset, dict):
                        asset.setdefault("schedule", legacy_schedule)
        return data

    @field_validator("assets")
    @classmethod
    def normalize_assets(cls, assets: dict[str, AssetConfig]) -> dict[str, AssetConfig]:
        return {key.upper(): value for key, value in assets.items()}


class EnvConfig(BaseModel):
    mode: Literal["testnet", "mainnet"] = "testnet"
    deribit_client_id: str = ""
    deribit_client_secret: str = ""


def load_dotenv_file(path: Path | None = None) -> None:
    env_path = path or ROOT_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def read_dotenv_values(path: Path | None = None) -> dict[str, str]:
    env_path = path or ROOT_DIR / ".env"
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def credential_keys(mode: str) -> tuple[str, str]:
    if mode == "mainnet":
        return "DERIBIT_MAINNET_CLIENT_ID", "DERIBIT_MAINNET_CLIENT_SECRET"
    return "DERIBIT_TESTNET_CLIENT_ID", "DERIBIT_TESTNET_CLIENT_SECRET"


def get_env_config(mode: str = "testnet") -> EnvConfig:
    load_dotenv_file()
    values = read_dotenv_values()
    id_key, secret_key = credential_keys(mode)
    legacy_id = values.get("DERIBIT_CLIENT_ID", "") if mode == "testnet" else ""
    legacy_secret = values.get("DERIBIT_CLIENT_SECRET", "") if mode == "testnet" else ""
    return EnvConfig(
        mode="mainnet" if mode == "mainnet" else "testnet",
        deribit_client_id=(values.get(id_key) or os.getenv(id_key, "") or legacy_id).strip(),
        deribit_client_secret=(values.get(secret_key) or os.getenv(secret_key, "") or legacy_secret).strip(),
    )


def write_env_config(mode: str, client_id: str, client_secret: str) -> EnvConfig:
    if "\n" in client_id or "\r" in client_id or "\n" in client_secret or "\r" in client_secret:
        raise ValueError("API credentials cannot contain line breaks.")
    mode = "mainnet" if mode == "mainnet" else "testnet"
    env_path = ROOT_DIR / ".env"
    values = read_dotenv_values(env_path)
    id_key, secret_key = credential_keys(mode)
    values[id_key] = client_id.strip()
    values[secret_key] = client_secret.strip()
    lines = [f"{key}={value}" for key, value in values.items()]
    env_path.write_text("\n".join(lines + [""]), encoding="utf-8")
    os.environ[id_key] = client_id.strip()
    os.environ[secret_key] = client_secret.strip()
    return get_env_config(mode)


def credentials_status() -> dict[str, dict[str, str | bool]]:
    return {
        mode: {
            "configured": bool((env := get_env_config(mode)).deribit_client_id and env.deribit_client_secret),
            "client_id_masked": mask_secret(env.deribit_client_id),
        }
        for mode in ("testnet", "mainnet")
    }


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return value[:2] + "****"
    return f"{value[:4]}****{value[-4:]}"
