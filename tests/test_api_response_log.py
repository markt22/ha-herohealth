"""Opt-in private response logging never exports authentication material."""

import importlib
import json
import logging
import stat
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


@pytest.fixture
def log_module(tmp_path, monkeypatch):
    module = importlib.import_module(
        "custom_components.hero_health.api.api_response_log"
    )
    path = tmp_path / "private" / "responses.jsonl"
    path.parent.mkdir(mode=0o700)
    (path.parent / ".enabled").touch()
    monkeypatch.setattr(module, "LOG_PATH", path)
    monkeypatch.setattr(module, "_LOGGER", None)
    yield module
    if module._LOGGER is not None:
        for handler in list(module._LOGGER.handlers):
            handler.close()
            module._LOGGER.removeHandler(handler)


def test_complete_values_and_arrays_preserved(log_module):
    payload = {
        "schedules": [{"time": f"{n}:00", "count": n} for n in range(300)],
        **{f"field_{n}": n for n in range(300)},
        "active": True,
        "optional": None,
    }
    log_module.record_response("/frontend/test/", 200, json.dumps(payload))
    record = json.loads(log_module.LOG_PATH.read_text())
    assert record["body"] == payload
    assert record["endpoint"] == "/frontend/test/"
    assert record["http_status"] == 200


def test_disabled_by_default(log_module):
    (log_module.LOG_PATH.parent / ".enabled").unlink()
    log_module.record_response("/frontend/test/", 200, '{"data":1}')
    assert not log_module.LOG_PATH.exists()


def test_nested_credentials_and_embedded_tokens_redacted(log_module):
    payload = {
        "data": [
            {
                "access_token": "PRIVATE_ACCESS",
                "RefreshToken": "PRIVATE_REFRESH",
                "Password": "PRIVATE_PASS",
                "api-key": "PRIVATE_KEY",
                "csrfmiddlewaretoken": "PRIVATE_CSRF",
                "authorization": "PRIVATE_AUTH",
                "session_id": "PRIVATE_SESSION",
                "client_secret": "PRIVATE_SECRET",
            }
        ],
        "message": "Bearer PRIVATE_BEARER",
        "url": "https://example.invalid/?access_token=PRIVATE_QUERY&ok=1",
        "nested_json": '{"password":"PRIVATE_PASSWORD"}',
        "unexpected": "opaque_PRIVATE_KNOWN",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiMTIzIn0.signature",
    }
    log_module.record_response(
        "/frontend/test/", 200, json.dumps(payload), "opaque_PRIVATE_KNOWN"
    )
    encoded = log_module.LOG_PATH.read_text()
    assert "PRIVATE" not in encoded
    assert "eyJ" not in encoded
    assert "ok=1" in encoded


def test_non_json_and_empty_bodies(log_module):
    log_module.record_response("/frontend/test/", 502, '<input value="PRIVATE_CSRF">')
    log_module.record_response("/frontend/test/", 204, "")
    records = [
        json.loads(line) for line in log_module.LOG_PATH.read_text().splitlines()
    ]
    assert "PRIVATE" not in str(records)
    assert records[1]["body"] is None


def test_file_permissions_rotation_and_no_general_log_propagation(log_module, caplog):
    log_module.record_response("/frontend/test/", 200, '{"medication":"Example"}')
    logger = log_module._get_logger()
    handler = logger.handlers[0]
    handler.maxBytes = 100
    log_module.record_response("/frontend/test/", 200, '{"schedule":[1,2,3]}')
    assert log_module.LOG_PATH.with_suffix(".jsonl.1").exists()
    for path in log_module.LOG_PATH.parent.glob("responses.jsonl*"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(log_module.LOG_PATH.parent.stat().st_mode) == 0o700
    assert "medication" not in caplog.text


def test_symlinked_log_directory_rejected(log_module, tmp_path, monkeypatch):
    link = tmp_path / "link"
    link.symlink_to(log_module.LOG_PATH.parent, target_is_directory=True)
    monkeypatch.setattr(log_module, "LOG_PATH", link / "responses.jsonl")
    with pytest.raises(OSError, match="symlinked"):
        log_module.record_response("/frontend/test/", 200, "{}")


def test_handler_failure_does_not_dump_sensitive_record(log_module, caplog, capsys):
    handler = log_module._PrivateRotatingFileHandler("/unused", delay=True)
    record = logging.LogRecord(
        "test", logging.INFO, "test", 1, "PRIVATE_DATA", (), None
    )
    with patch.object(handler, "_open", side_effect=OSError("PRIVATE_ERROR")):
        handler.emit(record)
    assert "could not be written" in caplog.text
    assert "PRIVATE" not in caplog.text + capsys.readouterr().err


def test_open_failure_closes_file_descriptor(log_module, monkeypatch):
    handler = log_module._PrivateRotatingFileHandler(log_module.LOG_PATH, delay=True)
    with (
        patch.object(log_module.os, "fdopen", side_effect=OSError()),
        patch.object(log_module.os, "close") as close,
    ):
        with pytest.raises(OSError):
            handler._open()
        close.assert_called_once()
    # Close the real descriptor after verifying the error path.
    import os

    os.close(close.call_args.args[0])


@pytest.mark.parametrize("status", [200, 401, 429])
async def test_client_logs_body_and_preserves_result_or_error(status, monkeypatch):
    module = importlib.import_module("custom_components.hero_health.api.client")
    record = Mock()
    monkeypatch.setattr(module, "record_response", record)

    class Response:
        headers = {"Retry-After": "60"}

        async def text(self):
            return '{"schedules":[{"time":"08:00","count":2}]}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    response = Response()
    response.status = status
    session = SimpleNamespace(get=lambda *args, **kwargs: response)
    client = module.HeroCloudClient(session, "fake-access", "fake-account")
    if status == 200:
        assert await client.pills_by_schedules() == {
            "schedules": [{"time": "08:00", "count": 2}]
        }
    else:
        error = (
            module.HeroAuthenticationError
            if status == 401
            else module.HeroRateLimitError
        )
        with pytest.raises(error):
            await client.pills_by_schedules()
    assert record.call_args.args[:2] == ("/frontend/pills-by-schedules/", status)


async def test_client_logging_failure_does_not_break_updates(monkeypatch, caplog):
    module = importlib.import_module("custom_components.hero_health.api.client")
    monkeypatch.setattr(
        module, "record_response", Mock(side_effect=OSError("PRIVATE_ERROR"))
    )

    class Response:
        status = 200

        async def text(self):
            return "{}"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    client = module.HeroCloudClient(
        SimpleNamespace(get=lambda *args, **kwargs: Response()), "fake-access"
    )
    assert await client.user_status() == {}
    assert "logging failed" in caplog.text
    assert "PRIVATE_ERROR" not in caplog.text
