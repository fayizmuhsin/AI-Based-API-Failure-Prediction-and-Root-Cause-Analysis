import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env", override=False)
DEFAULT_PORTS = {
    "api-gateway": 8100,
    "user-service": 8101,
    "order-service": 8102,
    "database-service": 8103,
}


class ServiceSettings(BaseSettings):
    """The shared Phase 1 paths, independently validated for service use."""

    service_config_path: Path = PROJECT_ROOT / "config" / "services.json"
    application_database_path: Path = PROJECT_ROOT / "data" / "simulated" / "application.db"
    monitoring_database_path: Path = PROJECT_ROOT / "data" / "monitoring" / "monitoring.db"

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="APP_",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_database_locations(self) -> "ServiceSettings":
        root = PROJECT_ROOT.resolve()
        simulated_root = (root / "data" / "simulated").resolve()
        app_path = self._absolute_from_root(self.application_database_path)
        monitor_path = self._absolute_from_root(self.monitoring_database_path)
        config_path = self._absolute_from_root(self.service_config_path)

        if app_path == monitor_path:
            raise ValueError("Simulated application DB and platform monitoring DB paths must differ")
        if not app_path.is_relative_to(simulated_root):
            raise ValueError("Simulated application DB must be stored under data/simulated/")
        if not config_path.is_file():
            raise ValueError(f"Service topology configuration not found: {config_path}")

        object.__setattr__(self, "application_database_path", app_path)
        object.__setattr__(self, "monitoring_database_path", monitor_path)
        object.__setattr__(self, "service_config_path", config_path)
        return self

    @staticmethod
    def _absolute_from_root(path: Path) -> Path:
        expanded = path.expanduser()
        return (expanded if expanded.is_absolute() else PROJECT_ROOT / expanded).resolve()


settings = ServiceSettings()


def service_port(service_id: str) -> int:
    """Return the service-specific local port, supporting process overrides."""
    env_name = service_id.replace("-", "_").upper() + "_PORT"
    value = os.getenv(env_name)
    port = int(value) if value else DEFAULT_PORTS[service_id]
    if not 1 <= port <= 65535:
        raise ValueError(f"{env_name} must be between 1 and 65535")
    return port


def service_url(env_name: str, default_service_id: str) -> str:
    """Return a configured downstream URL or its local default."""
    default = f"http://127.0.0.1:{service_port(default_service_id)}"
    return os.getenv(env_name, default).rstrip("/")
