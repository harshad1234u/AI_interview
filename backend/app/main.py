import copy
import logging
import re
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from urllib.parse import urlsplit

from app.api.routes import auth, candidates, health, interview_sessions, manual_facts, resumes
from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.services.auth import cleanup_expired_sessions


logger = logging.getLogger(__name__)

# Room for multipart boundaries and part headers around the file.
MULTIPART_OVERHEAD_BYTES = 64 * 1024
# Every other request is JSON; the largest real one (a 10,000-character answer) is ~40 KB.
JSON_MAX_BYTES = 256 * 1024
TOO_LARGE = "The request is too large."
UPLOAD_PATH = "/api/resumes"


class BodySizeLimit:
    """Stops reading a request body once it passes max_bytes.

    Starlette spools a whole upload to disk before the endpoint runs, so the endpoint's own size
    check alone would still accept unlimited bytes. Counting as bytes arrive also covers chunked
    requests that send no Content-Length.
    """

    def __init__(self, app: ASGIApp, max_bytes: int, detail: str, *,
                 upload_path: str | None = None, upload_max_bytes: int = 0,
                 upload_detail: str = "") -> None:
        self.app, self.max_bytes, self.detail = app, max_bytes, detail
        # The resume upload alone may be larger (and gets its own message).
        self.upload_path, self.upload_max_bytes = upload_path, upload_max_bytes
        self.upload_detail = upload_detail

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        received = 0
        upload = self.upload_path is not None and scope.get("path") == self.upload_path
        limit = self.upload_max_bytes if upload else self.max_bytes
        detail = self.upload_detail if upload else self.detail

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                # Raised inside body parsing, so FastAPI returns it as a normal 413 response.
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail)
            return message

        await self.app(scope, limited_receive, send)


class DuplicateSlashNormalizer:
    """Collapses consecutive slashes in request paths (e.g. '//api/health' -> '/api/health')
    to prevent unintended 404s caused by reverse-proxy rewrites or client URL concatenation.
    Also aliases '/health' to '/api/health' for platforms whose default health check is '/health'."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            raw_path = scope.get("path", "")
            if raw_path == "/health":
                scope["path"] = "/api/health"
            elif "//" in raw_path:
                scope["path"] = re.sub(r"/+", "/", raw_path)
        await self.app(scope, receive, send)


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class OriginCheck:
    """CSRF defence for cookie authentication (A5, with SameSite=Lax): a state-changing request
    must come from an allowed origin. A missing Origin is refused too: browsers send one on every
    POST, PUT, PATCH and DELETE, so only non-browser clients omit it, and they can add it."""

    def __init__(self, app: ASGIApp, allowed: list[str]) -> None:
        self.app, self.allowed = app, frozenset(allowed)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            if not origin or origin == "null":
                # Fallback for standard HTML form submissions where browsers send Referer instead of Origin
                referer = headers.get("referer")
                if referer:
                    r_parsed = urlsplit(referer)
                    if r_parsed.scheme and r_parsed.netloc:
                        ref_origin = f"{r_parsed.scheme}://{r_parsed.netloc}"
                        if ref_origin in self.allowed:
                            origin = ref_origin
                elif headers.get("sec-fetch-site") == "same-origin":
                    host = headers.get("host")
                    if host:
                        for allowed_orig in self.allowed:
                            parsed_allowed = urlsplit(allowed_orig)
                            if parsed_allowed.netloc and parsed_allowed.netloc.lower() == host.lower():
                                origin = allowed_orig
                                break
            if origin not in self.allowed:
                response = JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Request origin not allowed."},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI echoes the rejected input; on auth routes that can be a password. Drop it there.
    if not request.url.path.startswith("/api/auth/"):
        return await request_validation_exception_handler(request, exc)
    errors = [{k: v for k, v in e.items() if k != "input"} for e in exc.errors()]
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        content={"detail": jsonable_encoder(errors)})


async def database_unavailable(_: Request, exc: Exception) -> JSONResponse:
    # Log the driver error class only: its message can include host/user details.
    orig = getattr(exc, "orig", None)
    logger.error("Database unavailable (%s)", type(orig or exc).__name__)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Service temporarily unavailable. Please try again."},
    )


SENSITIVE_PARAM_PATTERN = re.compile(
    r"\b((?:token_hash|access_token|refresh_token|token|code|secret|password)(?:=|%3[dD]))(?!(?:%[sdifoxXeEgGcr]|%\([a-zA-Z0-9_]+\)[sdifoxXeEgGcr]))[^&\s\"'>]+",
    re.IGNORECASE,
)
BEARER_PATTERN = re.compile(r"\b(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
JSON_SENSITIVE_PATTERN = re.compile(
    r'("(?:token_hash|access_token|refresh_token|token|code|secret|password)"\s*:\s*")[^"]+(")',
    re.IGNORECASE,
)


def redact_string(val: str) -> str:
    """Redacts sensitive query parameters, bearer tokens, and JSON credentials."""
    if not isinstance(val, str):
        val = str(val)
    val = SENSITIVE_PARAM_PATTERN.sub(r"\g<1>[REDACTED]", val)
    val = BEARER_PATTERN.sub(r"\g<1>[REDACTED]", val)
    val = JSON_SENSITIVE_PATTERN.sub(r"\g<1>[REDACTED]\g<2>", val)
    return val


class SensitiveDataRedactionFilter(logging.Filter):
    """Redacts sensitive credentials and verification tokens from log messages, arguments, and tracebacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_string(record.msg)
        elif record.msg is not None:
            record.msg = redact_string(str(record.msg))

        if record.args:
            if isinstance(record.args, tuple):
                if len(record.args) == 1 and isinstance(record.args[0], dict):
                    record.args = (
                        {
                            k: redact_string(v) if not isinstance(v, (int, float, bool)) else v
                            for k, v in record.args[0].items()
                        },
                    )
                else:
                    record.args = tuple(
                        redact_string(arg) if not isinstance(arg, (int, float, bool)) else arg
                        for arg in record.args
                    )
            elif isinstance(record.args, dict):
                record.args = {
                    k: redact_string(v) if not isinstance(v, (int, float, bool)) else v
                    for k, v in record.args.items()
                }

        if record.exc_info:
            formatted_exc = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = redact_string(formatted_exc)
            record.exc_info = None

        if getattr(record, "stack_info", None):
            record.stack_info = redact_string(record.stack_info)

        return True


class RedactingFormatter(logging.Formatter):
    """Logging formatter that redacts credentials from final formatted log lines."""

    def format(self, record: logging.LogRecord) -> str:
        # Shallow-copy the record so mutations (like levelprefix fallback) do not affect
        # other handlers processing the same LogRecord instance.
        recordcopy = copy.copy(record)
        if not getattr(recordcopy, "levelprefix", None):
            levelname = getattr(recordcopy, "levelname", "INFO")
            separator = " " * max(1, 8 - len(levelname))
            recordcopy.levelprefix = f"{levelname}:{separator}"

        formatted = super().format(recordcopy)
        return redact_string(formatted)


def configure_logging(level: str) -> None:
    """Configures application and access loggers with sensitive credential redaction."""
    redaction_filter = SensitiveDataRedactionFilter()

    def _patch_handler(h: logging.Handler) -> None:
        h.addFilter(redaction_filter)
        if h.formatter is not None and not isinstance(h.formatter, RedactingFormatter):
            fmt_str = getattr(h.formatter, "_fmt", "%(asctime)s %(levelname)s %(name)s: %(message)s")
            date_fmt = getattr(h.formatter, "datefmt", None)
            try:
                h.setFormatter(RedactingFormatter(fmt_str, datefmt=date_fmt))
            except Exception:
                pass

    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.addFilter(redaction_filter)
        app_logger.addHandler(handler)
    else:
        for h in app_logger.handlers:
            _patch_handler(h)
    app_logger.addFilter(redaction_filter)
    app_logger.setLevel(level)

    for log_name in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "httpx2", "httpcore"):
        logger_instance = logging.getLogger(log_name)
        logger_instance.addFilter(redaction_filter)
        for h in logger_instance.handlers:
            _patch_handler(h)

    root_log = logging.getLogger()
    root_log.addFilter(redaction_filter)
    for h in root_log.handlers:
        _patch_handler(h)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure runtime handlers (e.g. Uvicorn worker handlers) have redaction filter applied
    try:
        configure_logging(get_settings().log_level)
    except Exception:
        pass
    # Startup: clean up expired authentication sessions (AUDIT-007)
    try:
        with SessionLocal() as db:
            cleaned = cleanup_expired_sessions(db)
            if cleaned > 0:
                logger.info("Cleaned up %d expired auth sessions on startup", cleaned)
            db.commit()
    except Exception as exc:
        logger.warning("Startup expired session cleanup skipped: %s", exc)
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    is_prod = settings.environment == "production"
    app = FastAPI(
        title="AI Interview Coach API",
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if is_prod else "/openapi.json",
    )
    # Added before CORS, so CORS wraps it: preflights never reach the Origin check.
    # Allow frontend CORS origins plus the API's own origin (for HTML confirmation forms)
    allowed_origins = list(settings.cors_origin_list)
    if settings.fastapi_public_url:
        parsed_api = urlsplit(settings.fastapi_public_url)
        if parsed_api.scheme and parsed_api.netloc:
            api_origin = f"{parsed_api.scheme}://{parsed_api.netloc}"
            if api_origin not in allowed_origins:
                allowed_origins.append(api_origin)

    app.add_middleware(OriginCheck, allowed=allowed_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,  # the session cookie
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )
    app.add_middleware(
        BodySizeLimit,
        max_bytes=JSON_MAX_BYTES,
        detail=TOO_LARGE,
        upload_path=UPLOAD_PATH,
        upload_max_bytes=settings.max_resume_bytes + MULTIPART_OVERHEAD_BYTES,
        upload_detail=resumes.too_large_detail(settings),
    )
    app.add_middleware(DuplicateSlashNormalizer)
    app.add_exception_handler(OperationalError, database_unavailable)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(candidates.router, prefix="/api")
    app.include_router(resumes.router, prefix="/api")
    app.include_router(manual_facts.router, prefix="/api")
    app.include_router(interview_sessions.router, prefix="/api")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.api_host, port=settings.api_port)
