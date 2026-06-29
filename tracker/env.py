"""Load environment variables from .env when present (local dev)."""

from dotenv import load_dotenv


def load_env() -> None:
    load_dotenv()
