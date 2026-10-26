"""Private full-body diagnostics for read-only Hero Cloud responses."""

import json
import logging
import os
import re
import threading
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_PATH = Path("/config/hero_api_logs/responses.jsonl")
_LOCK = threading.Lock()
_LOGGER = None
_REDACTED = "[REDACTED]"
_SECRET_WORDS = (
    "token",
    "password",
    "passwd",
    "secret",
    "credential",
    "cookie",
    "csrf",
)
_SECRET_KEYS = {
    "authorization",
    "proxyauthorization",
    "apikey",
    "session",
    "sessionid",
    "authorizationcode",
    "codeverifier",
}
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\"'<>]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_LABELED_SECRET = re.compile(
    r"(?i)(\b(?:[a-z0-9_]*(?:token|password|passwd|secret|credential|csrf)[a-z0-9_]*|api[_-]?key|session[_-]?id)\b"
    r"[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s&<>\"']+)"
)


def _scrub_text(value, secrets):
    for secret in secrets:
        if secret:
            value = value.replace(secret, _REDACTED)
    value = _BEARER.sub("Bearer " + _REDACTED, value)
    value = _JWT.sub(_REDACTED, value)
    return _LABELED_SECRET.sub(lambda match: match.group(1) + _REDACTED, value)


def redact_payload(value, secrets=()):
    """Preserve complete data while removing authentication material recursively."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            sensitive = normalized in _SECRET_KEYS or any(
                word in normalized for word in _SECRET_WORDS
            )
            result[_scrub_text(key, secrets)] = (
                _REDACTED if sensitive else redact_payload(item, secrets)
            )
        return result
    if isinstance(value, list):
        return [redact_payload(item, secrets) for item in value]
    if isinstance(value, str):
        return _scrub_text(value, secrets)
    return value


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.baseFilename, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, "a", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise

    def handleError(self, record):
        # logging's default stderr fallback would copy the sensitive record.
        logging.getLogger(__name__).warning(
            "Hero API private response log could not be written"
        )


def _get_logger():
    global _LOGGER
    with _LOCK:
        if _LOGGER is None:
            directory = LOG_PATH.parent
            if directory.is_symlink():
                raise OSError("Refusing a symlinked Hero API log directory")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
            handler = _PrivateRotatingFileHandler(
                LOG_PATH,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
                delay=True,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger = logging.getLogger(__name__ + ".private")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger.addHandler(handler)
            _LOGGER = logger
        return _LOGGER


def record_response(endpoint, http_status, text, access_token=""):
    """Called in a worker thread; never receive request headers or login responses."""
    if not (LOG_PATH.parent / ".enabled").is_file():
        return
    try:
        body = json.loads(text) if text else None
    except ValueError:
        body = "[Non-JSON body omitted to avoid logging embedded credentials]"
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "endpoint": endpoint,
        "http_status": http_status,
        "body": redact_payload(body, (access_token,)),
    }
    _get_logger().info(json.dumps(record, ensure_ascii=True, separators=(",", ":")))
