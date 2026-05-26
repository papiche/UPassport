import logging
import sys
import os
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from core.config import settings

# Stocke le request ID courant pour corrélation dans les logs
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("-")
        return True


def setup_logging() -> None:
    log_dir = settings.ZEN_PATH / "tmp"
    os.makedirs(log_dir, exist_ok=True)
    log_file = log_dir / "54321.log"

    level_name = settings.LOG_LEVEL.upper()
    level = getattr(logging, level_name, logging.INFO)

    # Format court pour stdout (lisibilité terminal), complet pour le fichier
    stdout_fmt = "%(asctime)s [%(levelname)s] %(name)s [%(request_id)s] %(message)s"
    file_fmt   = "%(asctime)s [%(levelname)s] %(name)s [%(request_id)s] %(funcName)s:%(lineno)d %(message)s"
    datefmt    = "%Y-%m-%d %H:%M:%S"

    req_filter = RequestIdFilter()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter(stdout_fmt, datefmt=datefmt))
    stdout_handler.addFilter(req_filter)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(file_fmt, datefmt=datefmt))
    file_handler.addFilter(req_filter)

    root = logging.getLogger()
    root.setLevel(level)
    # Évite les doublons si setup_logging() est appelé plusieurs fois
    root.handlers.clear()
    root.addHandler(stdout_handler)
    root.addHandler(file_handler)

    # Réduire le bruit des bibliothèques tierces
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(
        logging.DEBUG if level == logging.DEBUG else logging.WARNING
    )

    logging.getLogger(__name__).info(
        "Logging initialisé — niveau=%s fichier=%s", level_name, log_file
    )
