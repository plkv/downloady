from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Dict, Any
from urllib.parse import urlparse

from telegram import InputMediaPhoto, InputMediaVideo, Update, InputFile
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
from aiohttp import web
import tempfile
import os
import json

from .config import settings
from .downloader import extract_media_urls, find_urls, download_with_ytdlp


# Configure logging BEFORE importing settings to catch early errors
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
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

    # Best-effort typing; don't fail on network hiccups
    try:
        await update.effective_chat.send_action(ChatAction.TYPING)
    except (BadRequest, TimedOut, NetworkError) as e:
        logger.warning("send_action failed, continue: %s", e)
    except Exception:
        pass

    # Extract with threads + timeout per URL
    async def _extract(u: str) -> List[Dict[str, Any]]:
        try:
            return await asyncio.wait_for(asyncio.to_thread(extract_media_urls, u), timeout=35)
        except Exception as e:  # noqa: BLE001
            logger.warning("extract failed for %s: %s", u, e)
            return []

    results = await asyncio.gather(*[_extract(u) for u in urls])
    media_items: List[Dict[str, Any]] = [it for sub in results for it in sub]

    if not media_items:
        # Try heavy fallback with yt-dlp download (handles HLS-only sources like LinkedIn/Pinterest/Shorts)
        files = await asyncio.gather(*[asyncio.to_thread(download_with_ytdlp, u) for u in urls])
        flat_files = [p for sub in files for p in sub]
        if flat_files:
            await _send_files_group(update, flat_files)
            return
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

    # Отправка: пробуем по URL (если не принудительный локальный fallback). Если не удалось — качаем и заливаем.
    # Force fallback by domain list unless FORCE_DIRECT_ONLY=true
    def _host(u: str) -> str:
        try:
            return urlparse(u).hostname or ""
        except Exception:
            return ""

    force_fallback = False
    if not settings.force_direct_only:
        for u in urls:
            h = _host(u)
            if any(h.endswith(d) for d in settings.always_fallback_domains if d):
                force_fallback = True
                break

    # Always use heavy yt-dlp download for YouTube/Shorts to avoid Telegram fetch issues
    if any(_host(u).endswith(d) for u in urls for d in ("youtube.com", "youtu.be")):
        logger.info("YouTube/Shorts detected; invoking heavy yt-dlp download path")
        files = await asyncio.gather(*[asyncio.to_thread(download_with_ytdlp, u) for u in urls])
        flat_files = [p for sub in files for p in sub]
        if flat_files:
            await _send_files_group(update, flat_files)
            return

    try:
        if len(media_items) == 1:
            one = media_items[0]
            if one.get("type") == "image":
                await update.effective_message.reply_photo(one["url"])  # type: ignore[index]
            else:
                await update.effective_message.reply_video(one["url"])  # type: ignore[index]
            return

        if not force_fallback:
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
            media_group, tmp_paths, handles = await _prepare_downloaded_group(media_items)
            if media_group:
                await update.effective_message.reply_media_group(media_group)
                _cleanup_tmp(tmp_paths, handles)
                return
            _cleanup_tmp(tmp_paths, handles)
        except Exception as e:  # noqa: BLE001
            logger.exception("Group upload fallback failed: %s", e)

    # Иначе — по одному элементу
    for item in media_items:
        try:
            await _download_and_send_item(update, item)
        except Exception as e:  # noqa: BLE001
            logger.exception("Fallback upload failed: %s", e)


async def _send_files_group(update: Update, paths: List[str]) -> None:
    # Compose media group from local files
    media: List[InputMediaPhoto | InputMediaVideo] = []
    handles: List[Any] = []
    for p in paths[:10]:
        ext = os.path.splitext(p)[1].lower()
        fp = open(p, "rb")
        handles.append(fp)
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            media.append(InputMediaPhoto(media=InputFile(fp)))
        else:
            media.append(InputMediaVideo(media=InputFile(fp)))
    try:
        if len(media) == 1:
            if isinstance(media[0], InputMediaPhoto):
                await update.effective_message.reply_photo(media[0].media)
            else:
                await update.effective_message.reply_video(media[0].media)
        elif media:
            await update.effective_message.reply_media_group(media)
    finally:
        for h in handles:
            try:
                h.close()
            except Exception:
                pass
        for p in paths:
            try:
                os.unlink(p)
            except Exception:
                pass


MAX_UPLOAD = max(1, int(getattr(settings, "max_upload_mb", 48))) * 1024 * 1024


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
    # Preserve referer to the source page when available
    ref = item.get("source") or url
    if isinstance(ref, str) and ref:
        headers.setdefault("Referer", ref)
    suffix = "." + (item.get("ext") or ("jpg" if item.get("type") == "image" else "mp4"))

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=90)) as resp:
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
    handles: List[Any] = []

    sem = asyncio.Semaphore(max(1, int(getattr(settings, "download_concurrency", 3))))

    async def _task(item: Dict[str, Any]) -> Optional[str]:
        async with sem:
            return await _download_to_tmp(item)

    paths = await asyncio.gather(*[_task(it) for it in items])
    for item, path in zip(items, paths):
        if not path:
            continue
        tmp_paths.append(path)
        if item.get("type") == "image":
            fp = open(path, "rb")
            handles.append(fp)
            media.append(InputMediaPhoto(media=InputFile(fp)))
        else:
            fp = open(path, "rb")
            handles.append(fp)
            media.append(InputMediaVideo(media=InputFile(fp)))
    # Telegram ограничивает 2–10 в группе
    if len(media) < 2:
        return [], tmp_paths, handles
    return media[:10], tmp_paths, handles


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
    ref = item.get("source") or url
    if isinstance(ref, str) and ref:
        headers.setdefault("Referer", ref)
    suffix = "." + (item.get("ext") or ("jpg" if item.get("type") == "image" else "mp4"))

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=90)) as resp:
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


def _cleanup_tmp(paths: List[str], handles: Optional[List[Any]] = None) -> None:
    if handles:
        for h in handles:
            try:
                h.close()
            except Exception:
                pass
    for p in paths:
        try:
            os.unlink(p)
        except Exception:
            pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error while handling update: %s", context.error)


async def health_check_handler(request: web.Request) -> web.Response:
    """Health check endpoint for Railway and monitoring."""
    return web.Response(
        text=json.dumps({"status": "ok", "service": "downloady-bot"}),
        content_type="application/json"
    )


async def webhook_handler(request: web.Request) -> web.Response:
    """Handle incoming webhook updates from Telegram."""
    # Verify secret token if configured
    secret = getattr(settings, 'secret_token', None)
    if secret:
        header_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if header_token != secret:
            logger.warning("Webhook request with invalid secret token")
            return web.Response(status=403)

    # Get the telegram Application from app state
    telegram_app: Application = request.app['telegram_app']

    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        if update:
            await telegram_app.update_queue.put(update)
    except Exception as e:
        logger.exception("Failed to process webhook update: %s", e)
        return web.Response(status=500)

    return web.Response(status=200)


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


async def run_webhook_server(telegram_app: Application, base_url: str, secret: Optional[str]) -> None:
    """Run aiohttp web server with health check and webhook endpoints."""
    # Create aiohttp web application
    webapp = web.Application()

    # Store telegram app in webapp state for webhook handler
    webapp['telegram_app'] = telegram_app

    # Add routes
    webapp.router.add_get('/', health_check_handler)
    webapp.router.add_post('/webhook', webhook_handler)

    # Set webhook URL
    webhook_url = f"{base_url}/webhook"
    logger.info("Setting webhook URL: %s", webhook_url)

    await telegram_app.bot.set_webhook(
        url=webhook_url,
        secret_token=secret,
        drop_pending_updates=True,
    )

    # Start telegram application (without webhook runner)
    await telegram_app.initialize()
    await telegram_app.start()

    # Run aiohttp web server
    runner = web.AppRunner(webapp)
    await runner.setup()

    site = web.TCPSite(runner, '0.0.0.0', settings.port)
    logger.info("Starting webhook server on 0.0.0.0:%s", settings.port)
    await site.start()

    # Keep running until interrupted
    try:
        await asyncio.Event().wait()
    finally:
        logger.info("Shutting down webhook server...")
        await telegram_app.stop()
        await telegram_app.shutdown()
        await runner.cleanup()


def main() -> None:
    # Update log level from settings
    log_level = getattr(logging, getattr(settings, "log_level", "INFO"), logging.INFO)
    logging.getLogger().setLevel(log_level)

    logger.info("Starting Downloady Telegram Bot...")

    # Validate environment variables
    try:
        if not settings.telegram_token:
            raise RuntimeError("TELEGRAM_TOKEN is required")

        if settings.webhook_base:
            if not settings.secret_token:
                logger.warning(
                    "WEBHOOK_BASE is set but SECRET_TOKEN is not. "
                    "It's highly recommended to set SECRET_TOKEN for webhook security."
                )
            logger.info("Mode: webhook")
        else:
            logger.info("Mode: polling")

    except Exception as e:
        logger.exception("Failed to validate environment variables: %s", e)
        raise

    try:
        import uvloop  # type: ignore

        uvloop.install()
        logger.info("uvloop installed")
    except Exception:  # noqa: BLE001
        logger.info("uvloop not available, using default event loop")

    telegram_app = build_app()

    # Log cookies env presence at startup
    try:
        logger.info(
            "env cookies: youtube=%s linkedin=%s",
            bool(getattr(settings, "youtube_cookies_b64", None)),
            bool(getattr(settings, "linkedin_cookies_b64", None)),
        )
    except Exception:
        pass

    if settings.webhook_base:
        base = settings.webhook_base.rstrip("/")
        # Telegram secret_token: 1-256 chars, only [A-Za-z0-9_]
        secret = settings.secret_token
        try:
            import re as _re

            if secret and not _re.fullmatch(r"[A-Za-z0-9_]{1,256}", secret):
                logger.error(
                    "SECRET_TOKEN contains unallowed characters (must match [A-Za-z0-9_]{1,256})"
                )
                secret = None
        except Exception:  # noqa: BLE001
            pass

        # Run webhook server with aiohttp
        try:
            asyncio.run(run_webhook_server(telegram_app, base, secret))
        except KeyboardInterrupt:
            logger.info("Received interrupt signal, shutting down...")
        except Exception as e:
            logger.exception("Webhook server failed: %s", e)
            raise
    else:
        logger.info("Starting polling mode")
        telegram_app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
