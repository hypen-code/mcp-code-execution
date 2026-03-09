"""Pydantic settings for MCE configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env from CWD so it works both in dev and when installed via uvx/pip
_ENV_FILE = Path.cwd() / ".env"


class MCEConfig(BaseSettings):
    """Main MCE server configuration loaded from MCE_ prefixed environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="MCE_",
        env_file=str(_ENV_FILE),
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
    # LiteLLM model string — use provider/model format, e.g.:
    #   openai/gpt-4o  |  anthropic/claude-3-5-sonnet-20241022
    #   gemini/gemini-2.0-flash  |  openrouter/mistralai/mistral-7b-instruct
    llm_enhance: bool = False
    llm_api_key: str = ""
    llm_model: str = "gemini/gemini-2.0-flash"

    # Executor
    lint_enabled: bool = False  # Set MCE_LINT_ENABLED=true to enable ruff lint validation
    sandbox_requirements_path: str = "./sandbox/requirements.txt"
    docker_image: str = "mce-sandbox:latest"
    docker_host: str = ""  # e.g. unix:///home/user/.docker/desktop/docker.sock
    execution_timeout_seconds: int = 30
    max_output_size_bytes: int = 1_048_576  # 1MB
    network_mode: str = "mce_network"

    # Cache
    cache_enabled: bool = True
    cache_ttl_seconds: int = 3600
    cache_max_entries: int = 500
    cache_db_path: str = "./data/cache.db"

    # Security
    allowed_domains: list[str] = Field(default_factory=list)
    max_code_size_bytes: int = 65_536  # 64KB


def load_config() -> MCEConfig:
    """Load and return the MCE configuration.

    Returns:
        Populated MCEConfig instance.
    """
    return MCEConfig()
