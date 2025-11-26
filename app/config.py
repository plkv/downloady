import os
from typing import List, Optional


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
        # Optional cookies for specific domains (single var or chunked *_1,*_2,...)
        self.linkedin_cookies_b64: Optional[str] = self._read_chunked("LINKEDIN_COOKIES_B64")
        self.youtube_cookies_b64: Optional[str] = self._read_chunked("YOUTUBE_COOKIES_B64")

    def _read_chunked(self, base: str) -> Optional[str]:
        direct = get_env(base)
        if direct:
            return direct
        # Join numeric chunks in order: VAR_1, VAR_2, ... until missing
        parts: List[str] = []
        idx = 1
        while True:
            val = get_env(f"{base}_{idx}")
            if not val:
                break
            parts.append(val)
            idx += 1
        return "".join(parts) if parts else None


settings = Settings()
