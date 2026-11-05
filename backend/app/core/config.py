import base64
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AI_PROVIDER_NAMES = ("groq", "gemini", "nvidia")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "backend/.env"),
        extra="ignore"
    )

    database_url: str = Field(min_length=1)
    environment: Literal["development", "test", "production"] = "development"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    # Comma-separated list of exact origins; wildcards are rejected.
    cors_origins: str = "http://localhost:3000"
    max_resume_size_mb: int = Field(default=5, ge=1, le=20)
    # Marks the session cookie Secure (sent over HTTPS only). Turn on whenever the API is
    # served over HTTPS; plain-HTTP local development needs it off.
    auth_cookie_secure: bool = False
    cookie_secure: bool | None = None
    # Optional domain for cookies (e.g. '.yourdomain.com' for shared root domains).
    # Omit or leave empty for host-only cookies (e.g. separate Vercel and Render subdomains).
    cookie_domain: str | None = None
    # SameSite policy: 'lax' (standard/shared domain) or 'none' (cross-site Vercel -> Render).
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    # The application's own log lines (ids, enum codes, timings; never content), on stderr.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Supabase Auth Configuration
    supabase_url: str = "https://ccvnligpunynsxymrane.supabase.co"
    supabase_anon_key: str = ""
    supabase_service_role_key: SecretStr | None = None

    # Server-Side Token Encryption (AES-256-GCM)
    # Default 32-byte hex key for dev if unset
    session_encryption_key: SecretStr = Field(
        default=SecretStr("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    )
    session_key_id: str = "k1"
    session_encryption_key_fallback: SecretStr | None = None
    session_fallback_key_id: str = "k0"

    # BFF & Frontend Routing URLs
    fastapi_public_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:3000"

    # AI model gateway. Empty AI_PROVIDERS = AI disabled (resume processing still works).
    # Comma-separated, in order: the first is the primary, the rest are fallbacks.
    ai_providers: str = ""
    ai_timeout_seconds: float = Field(default=20, gt=0, le=120)  # per attempt
    ai_max_retries: int = Field(default=1, ge=0, le=5)  # per provider, transient errors only
    ai_max_output_tokens: int = Field(default=8192, ge=256, le=32768)
    # Model-backed requests admitted per UTC day, across all candidates (services/limits.py).
    # Size it from the provider plan; 0 = every AI request uses the deterministic fallback.
    ai_daily_request_limit: int = Field(default=2000, ge=0)
    groq_api_key: SecretStr | None = None
    groq_model: str | None = None
    gemini_api_key: SecretStr | None = None
    gemini_model: str | None = None
    nvidia_api_key: SecretStr | None = None
    nvidia_model: str | None = None

    @property
    def session_key_bytes(self) -> bytes:
        val = self.session_encryption_key.get_secret_value().strip()
        if len(val) == 64:
            try:
                return bytes.fromhex(val)
            except ValueError:
                pass
        try:
            decoded = base64.b64decode(val)
            if len(decoded) == 32:
                return decoded
        except Exception:
            pass
        return val.encode("utf-8").ljust(32, b"\0")[:32]

    @property
    def session_fallback_key_bytes(self) -> bytes | None:
        if not self.session_encryption_key_fallback:
            return None
        val = self.session_encryption_key_fallback.get_secret_value().strip()
        if len(val) == 64:
            try:
                return bytes.fromhex(val)
            except ValueError:
                pass
        try:
            decoded = base64.b64decode(val)
            if len(decoded) == 32:
                return decoded
        except Exception:
            pass
        return val.encode("utf-8").ljust(32, b"\0")[:32]

    @property
    def ai_provider_list(self) -> list[str]:
        return [p.strip().lower() for p in self.ai_providers.split(",") if p.strip()]

    @model_validator(mode="after")
    def resolve_cookie_settings(self) -> Self:
        if self.cookie_secure is not None:
            self.auth_cookie_secure = self.cookie_secure
        else:
            self.cookie_secure = self.auth_cookie_secure

        if self.cookie_domain is not None:
            cleaned = self.cookie_domain.strip()
            self.cookie_domain = cleaned if cleaned else None

        # Browsers reject SameSite=None unless Secure=True
        if self.cookie_samesite == "none":
            self.auth_cookie_secure = True
            self.cookie_secure = True
        return self

    @model_validator(mode="after")
    def check_ai_providers(self) -> Self:
        names = self.ai_provider_list
        if len(set(names)) != len(names):
            raise ValueError("AI_PROVIDERS lists a provider twice")
        for name in names:
            if name not in AI_PROVIDER_NAMES:
                raise ValueError(f"AI_PROVIDERS: unknown provider {name!r}")
            # Names only: never echo a key value in an error.
            if not getattr(self, f"{name}_api_key") or not getattr(self, f"{name}_model"):
                raise ValueError(f"AI provider {name!r} needs {name.upper()}_API_KEY and "
                                 f"{name.upper()}_MODEL")
        return self

    @property
    def max_resume_bytes(self) -> int:
        return self.max_resume_size_mb * 1024 * 1024

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        if "*" in origins:
            raise ValueError("CORS_ORIGINS must list explicit origins, not '*'")
        return origins


@lru_cache
def get_settings() -> Settings:
    return Settings()
