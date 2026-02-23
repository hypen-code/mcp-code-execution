"""Pydantic settings for MFP configuration loaded from environment variables."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MFPConfig(BaseSettings):
    """Main MFP server configuration loaded from MFP_ prefixed environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="MFP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    debug: bool = False

    # Compiler
    compile_on_startup: bool = True
    compiled_output_dir: str = "./compiled"
    swagger_config_file: str = "./config/swaggers.yaml"
    llm_enhance: bool = False
    llm_api_key: str = ""
    llm_model: str = "claude-sonnet-4-20250514"

    # Executor
    docker_image: str = "mfp-sandbox:latest"
    execution_timeout_seconds: int = 30
    max_output_size_bytes: int = 1_048_576  # 1MB
    network_mode: str = "mfp_network"

    # Cache
    cache_enabled: bool = True
    cache_ttl_seconds: int = 3600
    cache_max_entries: int = 500
    cache_db_path: str = "./data/cache.db"

    # Security
    allowed_domains: list[str] = Field(default_factory=list)
    max_code_size_bytes: int = 65_536  # 64KB


def load_config() -> MFPConfig:
    """Load and return the MFP configuration.

    Returns:
        Populated MFPConfig instance.
    """
    return MFPConfig()
