from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .common.duration import parse_duration


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

    # Expose the /dashboard UI: view every stored podcast plus basic stats and
    # add/remove them individually or via OPML. Like /opml this exposes and
    # mutates _all_ podcasts, so only enable it behind an authenticated reverse
    # proxy.
    enable_dashboard: bool = False

    # Notify Overcast (https://overcast.fm/podcasterinfo) when a new episode
    # is published. If ``public_storage_url`` is a CDN, give
    # feed.xml a short cache TTL so Overcast's crawl sees the new episode.
    enable_overcast_ping: bool = False
    overcast_ping_url: str = "https://overcast.fm/ping"

    # Max number of episodes per feed to process
    max_episodes: int = Field(default=12, ge=1)

    # Periodic auto-refresh. Every ``auto_refresh_interval`` all feeds are
    # refreshed as if their feed had been requested. A feed not requested within
    # ``auto_refresh_ttl`` goes "stale" and is skipped by the sweep until it is
    # requested again. systemd-style durations (s/m/h/d/w, M=month, y=year, plus
    # long-form aliases like "min"/"hr"); "0" disables. interval="0" turns the
    # sweep off entirely; ttl="0" means feeds never go stale.
    auto_refresh_interval: str = "60m"
    auto_refresh_ttl: str = "90d"

    # Time-based cleanup of stored episode files, run after each feed refresh.
    # Audio for episodes older than ``cleanup_ttl`` (by their feed ``pubDate``)
    # is deleted, as is any stored file that no longer maps to an episode in the
    # feed. systemd-style duration; "0" (default) disables it.
    cleanup_ttl: str = "0"

    @field_validator("auto_refresh_interval", "auto_refresh_ttl", "cleanup_ttl")
    @classmethod
    def _validate_duration(cls, value: str) -> str:
        parse_duration(value)
        return value

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

    @property
    def auto_refresh_interval_secs(self) -> int:
        return parse_duration(self.auto_refresh_interval)

    @property
    def auto_refresh_ttl_secs(self) -> int:
        return parse_duration(self.auto_refresh_ttl)

    @property
    def cleanup_ttl_secs(self) -> int:
        return parse_duration(self.cleanup_ttl)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
