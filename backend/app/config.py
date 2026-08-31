from pydantic_settings import BaseSettings, SettingsConfigDict


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
    kafka_max_request_size_bytes: int = 10 * 1024 * 1024  # 10 MB, как KAFKA_MESSAGE_MAX_BYTES брокера

    processing_service_url: str = "http://127.0.0.1:5001"
    processing_poll_interval_sec: float = 2.0
    processing_request_timeout_sec: float = 30.0
    processing_registration_attempts: int = 3
    processing_registration_retry_delay_sec: float = 1.0

    api_host: str = "0.0.0.0"
    api_port: int = 5000
    public_base_url: str = "http://localhost:5000"

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
