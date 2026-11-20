from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Environment-backed application settings."""

    app_name: str = "AI-Based API Failure Prediction"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"
    api_prefix: str = "/api"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    service_config_path: Path = PROJECT_ROOT / "config" / "services.json"
    application_database_path: Path = PROJECT_ROOT / "data" / "simulated" / "application.db"
    monitoring_database_path: Path = PROJECT_ROOT / "data" / "monitoring" / "monitoring.db"
    anomaly_contamination: float = Field(default=0.05, gt=0, le=0.5)
    anomaly_random_state: int = Field(default=42, ge=0)
    anomaly_n_estimators: int = Field(default=100, ge=1, le=2000)
    anomaly_max_samples: int = Field(default=256, ge=2, le=10000)
    anomaly_min_training_samples: int = Field(default=30, ge=2)
    anomaly_min_training_requests: int = Field(default=5, ge=1)
    anomaly_detection_batch_size: int = Field(default=100, ge=1, le=1000)
    anomaly_window_lateness_seconds: int = Field(default=10, ge=0, le=300)
    anomaly_rule_min_requests: int = Field(default=5, ge=1)
    anomaly_error_rate_threshold: float = Field(default=0.25, ge=0, le=1)
    anomaly_p95_latency_threshold_ms: float = Field(default=1000.0, gt=0)
    anomaly_timeout_count_threshold: int = Field(default=2, ge=1)
    anomaly_latency_spike_ratio: float = Field(default=2.5, gt=1)
    anomaly_latency_spike_min_history: int = Field(default=3, ge=1)
    anomaly_dependency_failure_rate_threshold: float = Field(default=0.3, ge=0, le=1)
    anomaly_request_change_ratio: float = Field(default=3.0, gt=0)
    rca_correlation_window_seconds: int = Field(default=180, ge=1, le=3600)
    rca_processing_batch_size: int = Field(default=100, ge=1, le=1000)
    rca_candidate_min_score: float = Field(default=30.0, ge=0, le=100)

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="APP_",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("APP_LOG_LEVEL must be a standard Python logging level")
        return normalized

    @field_validator("api_prefix")
    @classmethod
    def normalize_api_prefix(cls, value: str) -> str:
        return "/" + value.strip("/")

    @model_validator(mode="after")
    def database_paths_must_be_distinct(self) -> "Settings":
        app_path = self.application_database_path.expanduser().resolve()
        monitoring_path = self.monitoring_database_path.expanduser().resolve()
        if app_path == monitoring_path:
            raise ValueError("Application and monitoring database paths must be different")
        return self


settings = Settings()
