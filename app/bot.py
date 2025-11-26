from __future__ import annotations

import asyncio
import logging
from typing import List

from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import settings
from .downloader import extract_media_urls, find_urls


logger = logging.getLogger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    await update.effective_message.reply_text(
        "Привет! Пришлите ссылку на пост из соцсетей — я верну медиа."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Отправьте ссылку на Instagram, Facebook, Pinterest, TikTok и др.\n"
        "Я постараюсь вытащить фото/видео и прислать в ответ."
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("pong")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return

    text = update.effective_message.text or ""
    urls = find_urls(text)
    if not urls:
        await update.effective_message.reply_text("Не нашёл ссылок в сообщении.")
        return

    await update.effective_chat.send_action(ChatAction.TYPING)

    media_items = []
    for url in urls:
        try:
            items = extract_media_urls(url)
            media_items.extend(items)
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to extract media from %s: %s", url, e)

    if not media_items:
        await update.effective_message.reply_text(
            "Не удалось извлечь медиа по ссылке. Попробуйте другую."
        )
        return

    # Telegram ограничивает альбом до 10 элементов
    media_items = media_items[:10]

    photos: List[InputMediaPhoto] = []
    videos: List[InputMediaVideo] = []
    for item in media_items:
        if item.get("type") == "image":
            photos.append(InputMediaPhoto(media=item["url"]))
        elif item.get("type") == "video":
            videos.append(InputMediaVideo(media=item["url"]))

    # Отправка: если один элемент — в исходный чат, если несколько — альбом
    try:
        if len(media_items) == 1:
            one = media_items[0]
            if one.get("type") == "image":
                await update.effective_message.reply_photo(one["url"])  # type: ignore[index]
            else:
                await update.effective_message.reply_video(one["url"])  # type: ignore[index]
            return

        media_group = []
        media_group.extend(photos)
        media_group.extend(videos)
        if media_group:
            await update.effective_message.reply_media_group(media_group)
        else:
            await update.effective_message.reply_text(
                "Не удалось подготовить медиа к отправке."
            )
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to send media: %s", e)
        await update.effective_message.reply_text(
            "Получил ссылки, но не смог отправить медиа."
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error while handling update: %s", context.error)


def build_app() -> Application:
    app = Application.builder().token(settings.telegram_token).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ping", cmd_ping))

    # Text messages with links
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Errors
    app.add_error_handler(on_error)

    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    try:
        import uvloop  # type: ignore

        uvloop.install()
    except Exception:  # noqa: BLE001
        pass

    app = build_app()

    if settings.webhook_base:
        base = settings.webhook_base.rstrip("/")
        # Telegram secret_token: 1-256 chars, only [A-Za-z0-9_]
        secret = settings.secret_token
        try:
            import re as _re

            if secret and not _re.fullmatch(r"[A-Za-z0-9_]{1,256}", secret):
                logger.warning(
                    "SECRET_TOKEN contains unallowed characters; ignoring for webhook auth"
                )
                secret = None
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "Starting webhook on 0.0.0.0:%s path=/webhook base=%s",
            settings.port,
            base,
        )
        # NB: python-telegram-bot provides built-in webhook runner on aiohttp
        # url_path is the path component for receiving updates.
        app.run_webhook(
            listen="0.0.0.0",
            port=settings.port,
            url_path="webhook",
            webhook_url=f"{base}/webhook",
            secret_token=secret,
            drop_pending_updates=True,
        )
    else:
        logger.info("Starting polling mode")
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
