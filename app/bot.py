from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Dict, Any

from telegram import InputMediaPhoto, InputMediaVideo, Update, FSInputFile
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest, TimedOut, NetworkError

import aiohttp
import tempfile
import os

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

    # Отправка: пробуем по URL. Если не удалось — качаем и заливаем.
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
            return
    except (BadRequest, TimedOut, NetworkError) as e:
        logger.warning("Direct send by URL failed, will fallback to upload: %s", e)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to send media by URL: %s", e)

    # Fallback: если элементов > 1 — пробуем отправить одним media group через локальные файлы
    if len(media_items) > 1:
        try:
            media_group, tmp_paths = await _prepare_downloaded_group(media_items)
            if media_group:
                await update.effective_message.reply_media_group(media_group)
                _cleanup_tmp(tmp_paths)
                return
            _cleanup_tmp(tmp_paths)
        except Exception as e:  # noqa: BLE001
            logger.exception("Group upload fallback failed: %s", e)

    # Иначе — по одному элементу
    for item in media_items:
        try:
            await _download_and_send_item(update, item)
        except Exception as e:  # noqa: BLE001
            logger.exception("Fallback upload failed: %s", e)


MAX_UPLOAD = 48 * 1024 * 1024  # 48 MB safety headroom


async def _download_and_send_item(update: Update, item: Dict[str, Any]) -> None:
    url = item.get("url")
    if not url:
        return
    headers: Dict[str, str] = {}
    src_headers = item.get("headers") or {}
    for k, v in src_headers.items():
        if isinstance(k, str) and isinstance(v, str):
            headers[k] = v
    # Reasonable defaults to reduce blocks
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    )
    headers.setdefault("Accept", "*/*")
    suffix = "." + (item.get("ext") or ("jpg" if item.get("type") == "image" else "mp4"))

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"download failed: HTTP {resp.status}")
            size = int(resp.headers.get("Content-Length") or 0)
            if size and size > MAX_UPLOAD:
                await update.effective_message.reply_text(url)
                return
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
                tmp_path = f.name
                written = 0
                async for chunk in resp.content.iter_chunked(512 * 1024):
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_UPLOAD:
                        f.close()
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass
                        await update.effective_message.reply_text(url)
                        return
                    f.write(chunk)

    try:
        if item.get("type") == "image":
            with open(tmp_path, "rb") as fp:
                await update.effective_message.reply_photo(fp)
        else:
            with open(tmp_path, "rb") as fp:
                await update.effective_message.reply_video(fp)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


async def _prepare_downloaded_group(items: List[Dict[str, Any]]):
    tmp_paths: List[str] = []
    media: List[InputMediaPhoto | InputMediaVideo] = []
    for item in items:
        path = await _download_to_tmp(item)
        if not path:
            continue
        tmp_paths.append(path)
        if item.get("type") == "image":
            media.append(InputMediaPhoto(media=FSInputFile(path)))
        else:
            media.append(InputMediaVideo(media=FSInputFile(path)))
    # Telegram ограничивает 2–10 в группе
    if len(media) < 2:
        return [], tmp_paths
    return media[:10], tmp_paths


async def _download_to_tmp(item: Dict[str, Any]) -> Optional[str]:
    url = item.get("url")
    if not url:
        return None
    headers: Dict[str, str] = {}
    src_headers = item.get("headers") or {}
    for k, v in src_headers.items():
        if isinstance(k, str) and isinstance(v, str):
            headers[k] = v
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    )
    headers.setdefault("Accept", "*/*")
    suffix = "." + (item.get("ext") or ("jpg" if item.get("type") == "image" else "mp4"))

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                return None
            size = int(resp.headers.get("Content-Length") or 0)
            if size and size > MAX_UPLOAD:
                return None
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
                tmp_path = f.name
                written = 0
                async for chunk in resp.content.iter_chunked(512 * 1024):
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_UPLOAD:
                        f.close()
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass
                        return None
                    f.write(chunk)
    return tmp_path


def _cleanup_tmp(paths: List[str]) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except Exception:
            pass


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
