"""Load configuration from YAML and environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass
class Settings:
    target_count: int = 6_000_000
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    database_url: str = ""
    config_path: Path = field(default_factory=lambda: ROOT / "config" / "config.yaml")
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, config_path: Path | None = None) -> "Settings":
        path = config_path or Path(os.getenv("CONFIG_PATH", ROOT / "config" / "config.yaml"))
        if not path.is_absolute():
            path = ROOT / path
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        db_url = os.getenv("DATABASE_URL", "").strip()
        if not db_url:
            db_url = f"sqlite:///{ROOT / 'data' / 'pipeline.db'}"

        data_dir = Path(os.getenv("DATA_DIR", raw.get("data_dir", "data")))
        if not data_dir.is_absolute():
            data_dir = ROOT / data_dir

        target = int(os.getenv("TARGET_COUNT", raw.get("target_count", 6_000_000)))

        return cls(
            target_count=target,
            data_dir=data_dir,
            database_url=db_url,
            config_path=path,
            raw=raw,
        )

    def get(self, *keys, default=None):
        node = self.raw
        for key in keys:
            if not isinstance(node, dict):
                return default
            node = node.get(key)
            if node is None:
                return default
        return node

    def ensure_dirs(self) -> None:
        for name in ("audit", "manifests", "reports"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)

    def resolve_path(self, rel: str) -> Path:
        path = Path(rel)
        if path.is_absolute():
            return path
        return self.config_path.parent.parent / path
