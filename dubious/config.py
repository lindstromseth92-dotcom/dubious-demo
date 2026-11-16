"""Runtime settings, read once from the environment (and .env in the project root)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    model: str = os.environ.get("DUBIOUS_MODEL", "gemini-3.8-flash")
    port: int = _int("DUBIOUS_PORT", 8787)
    threshold: int = _int("DUBIOUS_THRESHOLD", 40)
    max_claims: int = _int("DUBIOUS_MAX_CLAIMS", 30)
    concurrency: int = _int("DUBIOUS_CONCURRENCY", 6)
    db_path: Path = ROOT / os.environ.get("DUBIOUS_DB", ".cache/dubious.sqlite")

    @property
    def api_key(self) -> str:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set. Put it in .env in the project root.")
        return key


settings = Settings()
