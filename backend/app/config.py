from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_COMPARATOR_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    max_upload_mb: int = 50
    pdf_render_dpi: int = 200
    image_max_width: int = 2048
    text_chunk_max_chars: int = 1500

    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_raw_chunks: str = "raw_chunks"
    kafka_topic_prepare_word: str = "cmp.prepare.word.cmd.v1"
    kafka_topic_prepare_pdf: str = "cmp.prepare.pdf.cmd.v1"
    kafka_topic_ocr_cmd: str = "cmp.ocr.cmd.v1"
    kafka_topic_job_event: str = "cmp.job.event.v1"
    kafka_max_request_size_bytes: int = 10 * 1024 * 1024
    kafka_replication_factor: int = 3

    processing_service_url: str = "http://127.0.0.1:5001"
    processing_poll_interval_sec: float = 2.0
    processing_request_timeout_sec: float = 30.0
    processing_registration_attempts: int = 3
    processing_registration_retry_delay_sec: float = 1.0

    api_host: str = "0.0.0.0"
    api_port: int = 5000
    public_base_url: str = "http://localhost:5000"
    pipeline_version: str = "v2"
    worker_id: str = ""
    word_timeout_seconds: float = 300.0
    prepare_timeout_seconds: float = 600.0
    expected_schema_revision: str = "0002_page_text"

    database_url: str = (
        "postgresql://comparator:comparator@127.0.0.1:5432/comparator"
    )
    database_pool_min_size: int = 1
    database_pool_max_size: int = 5

    session_ttl_days: int = 14
    cookie_secure: bool = False
    session_cookie_name: str = "comparator_session"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = ""
    internal_api_token: str = ""

    object_store_backend: str = "s3"
    object_store_root: Path = _COMPARATOR_ROOT / "data" / "objects"
    s3_endpoint_url: str = "http://127.0.0.1:9000"
    s3_access_key: str = "comparator"
    s3_secret_key: str = "comparator-secret"
    s3_bucket: str = "comparator"
    s3_region: str = "us-east-1"
    outbox_poll_interval_sec: float = 1.0
    work_item_poll_interval_sec: float = 1.0
    lease_seconds: int = 900

    @field_validator("object_store_root", mode="before")
    @classmethod
    def _parse_object_root(cls, value: object) -> Path:
        if isinstance(value, Path):
            return value
        if isinstance(value, str) and value.strip():
            return Path(value)
        return _COMPARATOR_ROOT / "data" / "objects"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def processing_ws_base(self) -> str:
        base = self.processing_service_url.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :]
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :]
        return "ws://" + base

    def job_websocket_url(self, job_id: str) -> str:
        ws_base = self.public_base_url.rstrip("/")
        if ws_base.startswith("https://"):
            ws_base = "wss://" + ws_base[len("https://") :]
        elif ws_base.startswith("http://"):
            ws_base = "ws://" + ws_base[len("http://") :]
        else:
            ws_base = "ws://" + ws_base
        return f"{ws_base}/ws/jobs/{job_id}"


settings = Settings()
