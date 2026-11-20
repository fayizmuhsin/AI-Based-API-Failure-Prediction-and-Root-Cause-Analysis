import logging

from app.core.config import settings


def configure_logging() -> None:
    """Configure consistent console logging for local development."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
