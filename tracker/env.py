"""Load environment variables from .env when present (local dev)."""

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional in minimal test envs
    load_dotenv = None


def load_env() -> None:
    if load_dotenv is not None:
        load_dotenv()
