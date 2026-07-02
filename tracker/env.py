"""Load environment variables from .env when present (local dev)."""

from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional in minimal test envs
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    if load_dotenv is not None:
        load_dotenv(PROJECT_ROOT / ".env")
