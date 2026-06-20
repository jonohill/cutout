from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # S3-compatible object storage (R2, MinIO, AWS S3, …).
    s3_bucket: str = "cutout"
    s3_endpoint_url: str | None = None
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_region: str = "auto"

    # Public base URL of this server; used for feed self-links and the
    # /podcast/{feed_id} links written into rewritten feeds.
    public_service_url: str = "http://localhost:8080"
    # Public base URL that serves stored objects (rewritten feeds and audio),
    # e.g. an R2/CDN public bucket domain.
    public_storage_url: str = "http://localhost:8080"

    # Local working directory for a job's in-flight media
    work_dir: Path = Path("work")

    # HTTP server bind.
    host: str = "0.0.0.0"
    port: int = 8080

    # Logging verbosity for the application's loggers (DEBUG, INFO, WARNING, …).
    log_level: str = "INFO"

    # Expose the /opml endpoint
    # This allows listing and bulk uploading _all_ podcasts
    # so you probably only want to enable this behind an
    # authenticated reverse proxy
    enable_opml: bool = False

    # Max number of episodes per feed to process
    max_episodes: int = Field(default=12, ge=1)

    # Concurrency for fetching/parsing feeds
    feed_concurrency: int = Field(default=2, ge=1)

    # Concurrency for each stage of the episode pipeline
    download_concurrency: int = Field(default=2, ge=1)
    transcribe_concurrency: int = Field(default=1, ge=1)
    chapters_concurrency: int = Field(default=2, ge=1)
    encode_concurrency: int = Field(default=1, ge=1)
    upload_concurrency: int = Field(default=2, ge=1)

    # OpenAI-compatible transcription service. The transcribe stage uploads the
    # episode audio here and stores the returned transcript. Auth is optional —
    # leave the key unset for a local or unauthenticated endpoint.
    transcribe_url: str = "https://api.openai.com/v1/audio/transcriptions"
    transcribe_api_key: str | None = None
    transcribe_model: str = "whisper-1"
    transcribe_max_mb: int = Field(default=25, ge=1)

    # Chapter generation, via an OpenAI-compatible Chat Completions endpoint
    chapters_url: str = (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    chapters_api_key: str | None = None
    chapters_model: str = "gemini-3-flash-preview"
    # Language the chapter descriptions are written in.
    chapters_language: str = "NZ English"

    # Optional AAC bitrate (kbps) for the output.
    # Leave unset for a high-quality VBR default; set it (e.g. 64) to pin a
    # specific bitrate.
    encode_bitrate_kbps: int | None = Field(default=None, ge=8)

    @property
    def transcribe_max_bytes(self) -> int:
        # Decimal MB to match API maths
        return self.transcribe_max_mb * 1000 * 1000


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
