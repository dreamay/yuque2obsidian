"""Configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator


class YuqueConfig(BaseModel):
    token: str
    base_url: str = "https://www.yuque.com/api/v2"


class ExportConfig(BaseModel):
    output_dir: str = "./obsidian_vault"
    assets_dir: str = "assets"
    include_personal: bool = True
    include_groups: bool = True
    incremental: bool = True
    full_sync: bool = False
    concurrency: int = 5
    rate_limit: float = 1.3

    @field_validator("concurrency")
    @classmethod
    def concurrency_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("concurrency must be >= 1")
        return v

    @field_validator("rate_limit")
    @classmethod
    def rate_limit_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("rate_limit must be > 0")
        return v


class FrontmatterConfig(BaseModel):
    include_source_url: bool = True
    include_created_at: bool = True
    include_updated_at: bool = True
    include_tags: bool = True


class UiConfig(BaseModel):
    enabled: bool = False
    port: int = 7860


class Config(BaseModel):
    yuque: YuqueConfig
    export: ExportConfig = Field(default_factory=ExportConfig)
    frontmatter: FrontmatterConfig = Field(default_factory=FrontmatterConfig)
    ui: UiConfig = Field(default_factory=UiConfig)

    @property
    def output_path(self) -> Path:
        return Path(self.export.output_dir).expanduser().resolve()

    @property
    def db_path(self) -> Path:
        return self.output_path / ".yuque2obsidian.db"


def load_config(path: Optional[Path | str] = None) -> Config:
    """Load configuration from YAML file."""
    if path is None:
        path = "config.yaml"
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. "
            f"Please copy config.example.yaml to config.yaml and fill in your token."
        )
    with path.open("r", encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        msgs = [f"  - {'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ValueError(
            f"Invalid configuration in {path}:\n" + "\n".join(msgs)
        ) from exc
