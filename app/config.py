import os
from typing import List


def get_env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


class Settings:
    def __init__(self) -> None:
        self.telegram_token: str = get_env("TELEGRAM_TOKEN", required=True)  # type: ignore[assignment]
        self.webhook_base: str | None = get_env("WEBHOOK_BASE")
        self.secret_token: str | None = get_env("SECRET_TOKEN")
        self.port: int = int(get_env("PORT", "8080"))
        # Tunables
        self.max_upload_mb: int = int(get_env("MAX_UPLOAD_MB", "48"))
        self.download_concurrency: int = int(get_env("DOWNLOAD_CONCURRENCY", "3"))
        self.force_direct_only: bool = (get_env("FORCE_DIRECT_ONLY", "false") or "").lower() in {"1", "true", "yes", "on"}
        self.always_fallback_domains: List[str] = [
            d.strip() for d in (
                get_env(
                    "ALWAYS_FALLBACK_DOMAINS",
                    "instagram.com,x.com,twitter.com,pinterest.com,linkedin.com,youtube.com,youtu.be",
                )
                or ""
            ).split(",") if d.strip()
        ]
        self.log_level: str = (get_env("LOG_LEVEL", "INFO") or "INFO").upper()
        # Optional cookies for specific domains
        self.linkedin_cookies_b64: str | None = get_env("LINKEDIN_COOKIES_B64")


settings = Settings()
