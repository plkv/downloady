import os


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


settings = Settings()

