"""Unit tests for RedactingFormatter and logging configurations."""

import io
import logging
import sys

from app.core.config import Settings
from app.main import RedactingFormatter


def test_ordinary_log_record_with_levelprefix_format():
    """Verify an ordinary LogRecord without levelprefix formats cleanly without mutating record."""
    fmt = RedactingFormatter("%(levelprefix)s%(message)s")
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Ordinary log message with param %s",
        args=("sample",),
        exc_info=None,
    )

    assert not hasattr(record, "levelprefix")
    output = fmt.format(record)

    assert output.startswith("INFO:   ")
    assert "Ordinary log message with param sample" in output
    # Original record must not be mutated
    assert not hasattr(record, "levelprefix")


def test_uvicorn_record_with_existing_levelprefix_preserved():
    """Verify that records with pre-existing levelprefix preserve that prefix."""
    fmt = RedactingFormatter("%(levelprefix)s%(message)s")
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.WARNING,
        pathname=__file__,
        lineno=20,
        msg="Server warning",
        args=(),
        exc_info=None,
    )
    custom_prefix = "\x1b[33mWARNING:\x1b[0m "
    record.levelprefix = custom_prefix

    output = fmt.format(record)
    assert output.startswith(custom_prefix)
    assert "Server warning" in output
    # Original record levelprefix should remain exactly what it was
    assert record.levelprefix == custom_prefix


def test_empty_or_none_levelprefix_triggers_fallback():
    """Verify that falsy levelprefix attributes fall back to standard level formatting."""
    fmt = RedactingFormatter("%(levelprefix)s%(message)s")

    for empty_val in ("", None):
        record = logging.LogRecord(
            name="app.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=30,
            msg="Error message",
            args=(),
            exc_info=None,
        )
        record.levelprefix = empty_val

        output = fmt.format(record)
        assert output.startswith("ERROR:  ")
        assert "Error message" in output
        # Original record attribute should remain whatever it was
        assert record.levelprefix == empty_val


def test_standard_format_strings_without_levelprefix():
    """Verify standard formatters without %(levelprefix)s continue to work normally."""
    fmt = RedactingFormatter("%(levelname)s [%(name)s] %(message)s")
    record = logging.LogRecord(
        name="app.core",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=40,
        msg="Debug detail",
        args=(),
        exc_info=None,
    )

    output = fmt.format(record)
    assert output == "DEBUG [app.core] Debug detail"
    assert not hasattr(record, "levelprefix")


def test_traceback_formatting_and_redaction():
    """Verify exception tracebacks format properly and have sensitive tokens redacted."""
    fmt = RedactingFormatter("%(levelprefix)s%(message)s")
    try:
        raise ValueError("Database connection failed for token=super_secret_token_12345")
    except Exception:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="app.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=50,
        msg="Caught error with password=my_db_password",
        args=(),
        exc_info=exc_info,
    )

    output = fmt.format(record)
    # Both the message and traceback must have secrets redacted
    assert "password=[REDACTED]" in output
    assert "my_db_password" not in output
    assert "token=[REDACTED]" in output
    assert "super_secret_token_12345" not in output
    assert "Traceback (most recent call last):" in output
    assert "ValueError: Database connection failed" in output

    # Original record exc_info must not be corrupted
    assert record.exc_info is exc_info


def test_credential_redaction_patterns():
    """Verify redaction of various sensitive patterns in log messages."""
    fmt = RedactingFormatter("%(message)s")

    messages = [
        ("User login with password=PlainSecret123", "User login with password=[REDACTED]"),
        ("Auth header Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz", "Auth header Bearer [REDACTED]"),
        ('Payload {"access_token": "sec_tok_999", "user_id": 42}', 'Payload {"access_token": "[REDACTED]", "user_id": 42}'),
        ("Reset URL: /auth/verify?token_hash=hashval789&code=123456", "Reset URL: /auth/verify?token_hash=[REDACTED]&code=[REDACTED]"),
    ]

    for raw, expected in messages:
        rec = logging.LogRecord("test", logging.INFO, __file__, 60, raw, (), None)
        assert fmt.format(rec) == expected


def test_multiple_handlers_no_mutation_or_cross_contamination():
    """Verify logging to multiple handlers where only one uses %(levelprefix)s."""
    stream1 = io.StringIO()
    stream2 = io.StringIO()

    handler1 = logging.StreamHandler(stream1)
    handler1.setFormatter(RedactingFormatter("%(levelprefix)s%(message)s"))

    handler2 = logging.StreamHandler(stream2)
    handler2.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    test_logger = logging.getLogger("test.multi_handler")
    test_logger.setLevel(logging.INFO)
    test_logger.handlers = [handler1, handler2]

    test_logger.info("Shared log message")

    out1 = stream1.getvalue()
    out2 = stream2.getvalue()

    assert "INFO:    Shared log message" in out1
    assert "INFO: Shared log message" in out2


def test_uvicorn_404_access_log_formatting():
    """Verify Uvicorn access log formatting on 404 responses with query parameters."""
    uvicorn_fmt = '%(levelprefix)s%(client_addr)s - "%(request_line)s" %(status_code)s'
    fmt = RedactingFormatter(uvicorn_fmt)

    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=80,
        msg="",
        args=(),
        exc_info=None,
    )
    record.client_addr = "127.0.0.1:54321"
    record.request_line = "GET /api/unknown-endpoint?token=secret_query_val HTTP/1.1"
    record.status_code = 404

    output = fmt.format(record)
    assert output.startswith("INFO:   ")
    assert '127.0.0.1:54321 - "GET /api/unknown-endpoint?token=[REDACTED] HTTP/1.1" 404' in output
    assert "secret_query_val" not in output
    # Original record must remain unmutated
    assert not hasattr(record, "levelprefix")


def test_uvicorn_access_log_with_client_info_in_args():
    """Verify Uvicorn access record with client information in positional args is extracted."""
    uvicorn_fmt = '%(levelprefix)s%(client_addr)s - "%(request_line)s" %(status_code)s'
    fmt = RedactingFormatter(uvicorn_fmt)

    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=90,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("192.168.1.100:54321", "GET", "/api/health", "1.1", 200),
        exc_info=None,
    )

    output = fmt.format(record)
    assert output.startswith("INFO:   ")
    assert '192.168.1.100:54321 - "GET /api/health HTTP/1.1" 200' in output
    # Original record must not be mutated
    assert not hasattr(record, "client_addr")
    assert not hasattr(record, "request_line")
    assert not hasattr(record, "status_code")
    assert not hasattr(record, "levelprefix")
    assert record.args == ("192.168.1.100:54321", "GET", "/api/health", "1.1", 200)


def test_uvicorn_access_log_without_client_addr_in_args_uses_neutral_unknown():
    """Verify Uvicorn access record with missing/empty client falls back to 'unknown'."""
    uvicorn_fmt = '%(levelprefix)s%(client_addr)s - "%(request_line)s" %(status_code)s'
    fmt = RedactingFormatter(uvicorn_fmt)

    for empty_client in ("", None):
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=95,
            msg='%s - "%s %s HTTP/%s" %d',
            args=(empty_client, "GET", "/api/auth/me", "1.1", 401),
            exc_info=None,
        )

        output = fmt.format(record)
        assert output.startswith("INFO:   ")
        # Must use neutral fallback 'unknown', never an invented IP address
        assert 'unknown - "GET /api/auth/me HTTP/1.1" 401' in output
        assert not hasattr(record, "client_addr")
        assert not hasattr(record, "levelprefix")


def test_uvicorn_access_log_with_tuple_client_info():
    """Verify Uvicorn access record with tuple client address is formatted properly."""
    uvicorn_fmt = '%(levelprefix)s%(client_addr)s - "%(request_line)s" %(status_code)s'
    fmt = RedactingFormatter(uvicorn_fmt)

    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=100,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(("10.0.0.1", 8080), "POST", "/api/auth/login", "1.1", 401),
        exc_info=None,
    )

    output = fmt.format(record)
    assert output.startswith("INFO:   ")
    assert '10.0.0.1:8080 - "POST /api/auth/login HTTP/1.1" 401' in output
    assert not hasattr(record, "client_addr")


def test_application_log_formatted_by_access_formatter_does_not_crash():
    """Verify an ordinary application log sent to an access formatter receives safe defaults."""
    uvicorn_fmt = '%(levelprefix)s%(client_addr)s - "%(request_line)s" %(status_code)s'
    fmt = RedactingFormatter(uvicorn_fmt)

    record = logging.LogRecord(
        name="app.main",
        level=logging.INFO,
        pathname=__file__,
        lineno=105,
        msg="Cleaned up %d expired auth sessions",
        args=(3,),
        exc_info=None,
    )

    output = fmt.format(record)
    assert output.startswith("INFO:   ")
    assert 'unknown - "Cleaned up 3 expired auth sessions" -' in output
    # Original record must not be mutated
    assert not hasattr(record, "client_addr")
    assert not hasattr(record, "request_line")
    assert not hasattr(record, "status_code")
    assert not hasattr(record, "levelprefix")


def test_preserves_existing_valid_structured_fields():
    """Verify that existing valid structured logging fields are preserved and not overwritten."""
    uvicorn_fmt = '%(levelprefix)s%(client_addr)s [%(request_id)s] - "%(request_line)s" %(status_code)s'
    fmt = RedactingFormatter(uvicorn_fmt)

    record = logging.LogRecord(
        name="app.api",
        level=logging.WARNING,
        pathname=__file__,
        lineno=110,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("1.2.3.4", "GET", "/api/test", "1.1", 200),
        exc_info=None,
    )
    # Explicitly set fields on record
    record.client_addr = "custom.proxy.domain:443"
    record.request_id = "req_trace_98765"
    record.status_code = 429
    record.request_line = "GET /api/test?custom=1 HTTP/1.1"

    output = fmt.format(record)
    assert 'custom.proxy.domain:443 [req_trace_98765] - "GET /api/test?custom=1 HTTP/1.1" 429' in output
    # Original values remain intact
    assert record.client_addr == "custom.proxy.domain:443"
    assert record.request_id == "req_trace_98765"
    assert record.status_code == 429


def test_missing_arbitrary_contextual_fields_receive_safe_defaults():
    """Verify format strings with arbitrary missing contextual fields format cleanly without exceptions."""
    custom_fmt = "%(levelname)s [%(request_id)s] [%(duration_ms)s] %(client_addr)s %(message)s"
    fmt = RedactingFormatter(custom_fmt)

    record = logging.LogRecord(
        name="app.metrics",
        level=logging.INFO,
        pathname=__file__,
        lineno=115,
        msg="Transaction complete for item %s",
        args=("A100",),
        exc_info=None,
    )

    output = fmt.format(record)
    assert output == "INFO [-] [-] unknown Transaction complete for item A100"
    assert not hasattr(record, "request_id")
    assert not hasattr(record, "duration_ms")
    assert not hasattr(record, "client_addr")


def test_brace_format_style_support():
    """Verify RedactingFormatter supports Python str.format style ('{') without KeyError/ValueError."""
    brace_fmt = "{levelprefix}{client_addr} - {message} [{status_code}]"
    fmt = RedactingFormatter(brace_fmt, style="{")

    record = logging.LogRecord(
        name="app.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=120,
        msg="Database timeout on port %d",
        args=(5432,),
        exc_info=None,
    )

    output = fmt.format(record)
    assert output.startswith("ERROR:  ")
    assert "unknown - Database timeout on port 5432 [-]" in output


def test_config_settings_lazy_resolution():
    """Verify from app.core.config import settings works dynamically."""
    from app.core.config import settings
    from app.db.session import engine, SessionLocal, get_db

    assert isinstance(settings, Settings)
    assert settings.database_url is not None
    assert engine is not None
    assert SessionLocal is not None
    assert callable(get_db)
