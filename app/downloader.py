from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Literal, Optional, TypedDict
from urllib.parse import urlparse
import base64
import tempfile
import os

from yt_dlp import YoutubeDL
from .config import settings


class MediaItem(TypedDict, total=False):
    type: Literal["image", "video"]
    url: str
    title: str
    ext: str
    filesize: Optional[int]
    headers: Dict[str, Any]
    source: str


_URL_RE = re.compile(r"https?://\S+")


def pick_best_video_format(formats: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not formats:
        return None
    candidates: List[Dict[str, Any]] = []
    for f in formats:
        vcodec = (f.get("vcodec") or "none").lower()
        acodec = (f.get("acodec") or "none").lower()
        ext = (f.get("ext") or "").lower()
        protocol = (f.get("protocol") or "").lower()
        if vcodec == "none":
            continue
        # Prefer progressive (contains audio) and non-HLS
        if acodec != "none" and ext in {"mp4", "mov", "m4v", "webm"} and "m3u8" not in protocol:
            candidates.append(f)
    if not candidates:
        # fallback: any non-HLS video
        for f in formats:
            protocol = (f.get("protocol") or "").lower()
            if (f.get("vcodec") or "none") != "none" and "m3u8" not in protocol:
                candidates.append(f)

    def score(f: Dict[str, Any]) -> tuple:
        height = f.get("height") or 0
        tbr = f.get("tbr") or 0
        filesize = f.get("filesize") or f.get("filesize_approx") or 0
        # Prefer larger resolution, then bitrate, then smaller file
        return (int(height), float(tbr), -int(filesize))

    # Prefer formats under the upload cap if possible
    try:
        cap = int(getattr(settings, "max_upload_mb", 48)) * 1024 * 1024
    except Exception:
        cap = 48 * 1024 * 1024

    under_cap = [f for f in candidates if (f.get("filesize") or f.get("filesize_approx") or 0) and int(f.get("filesize") or f.get("filesize_approx") or 0) <= cap]
    if under_cap:
        under_cap.sort(key=score, reverse=True)
        return under_cap[0]

    candidates.sort(key=score, reverse=True)
    return candidates[0] if candidates else None


def _iter_entries(info: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    if not info:
        return []
    if info.get("_type") == "playlist" and info.get("entries"):
        for e in info["entries"] or []:
            if e:
                yield e
    else:
        yield info


def _cookies_file_for(u: str) -> Optional[str]:
    try:
        host = urlparse(u).hostname or ""
    except Exception:
        host = ""
    # LinkedIn usually requires cookies to access media
    if host.endswith("linkedin.com") and settings.linkedin_cookies_b64:
        try:
            raw = base64.b64decode(settings.linkedin_cookies_b64)
            fd, path = tempfile.mkstemp(prefix="cookies-linkedin-", suffix=".txt")
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            return path
        except Exception:
            return None
    return None


def extract_media_urls(url: str) -> List[MediaItem]:
    ydl_opts: Dict[str, Any] = {
        "quiet": True,
        "noplaylist": True,
        "skip_download": True,
        "no_warnings": True,
        "extract_flat": False,
        "restrictfilenames": True,
        "nocheckcertificate": True,
        "ignoreerrors": True,
        "default_search": "auto",
        # Retries for flaky CDNs (X/Pinterest/LinkedIn often rate limit)
        "retries": 3,
        "fragment_retries": 3,
        # User-Agent helps some CDNs
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "*/*",
        },
        "geo_bypass": True,
        # Some CDNs block IPv6 on servers
        "source_address": "0.0.0.0",
    }

    cookiefile = _cookies_file_for(url)
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile

    items: List[MediaItem] = []

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    finally:
        # cleanup temp cookies file if created
        try:
            cookiefile = ydl_opts.get("cookiefile")
            if cookiefile:
                os.unlink(cookiefile)
        except Exception:
            pass

    for entry in _iter_entries(info):
        title = (entry.get("title") or "").strip()
        ext = (entry.get("ext") or "").lower()
        direct_url = entry.get("url")
        formats = entry.get("formats") or []

        # Image case (some extractors return direct image URL with ext)
        if (not formats) and direct_url and ext in {"jpg", "jpeg", "png", "webp", "gif"}:
            headers = (entry.get("http_headers") or {}).copy()
            items.append({
                "type": "image",
                "url": direct_url,
                "title": title,
                "ext": ext,
                "headers": headers,
                "source": url,
            })
            continue

        # Video case
        best = pick_best_video_format(formats)
        if best and best.get("url"):
            headers = (best.get("http_headers") or {}).copy()
            items.append(
                {
                    "type": "video",
                    "url": best["url"],
                    "title": title,
                    "ext": (best.get("ext") or ext or "").lower(),
                    "filesize": best.get("filesize") or best.get("filesize_approx"),
                    "headers": headers,
                    "source": url,
                }
            )
            continue

        # Fallback: if extractor returned a direct url that looks like media
        if direct_url:
            if re.search(r"\.(mp4|m4v|mov|webm)(?:[?#].*)?$", direct_url, re.I):
                headers = (entry.get("http_headers") or {}).copy()
                items.append({
                    "type": "video",
                    "url": direct_url,
                    "title": title,
                    "ext": ext or "mp4",
                    "headers": headers,
                    "source": url,
                })
            elif re.search(r"\.(jpe?g|png|webp|gif)(?:[?#].*)?$", direct_url, re.I):
                headers = (entry.get("http_headers") or {}).copy()
                items.append({
                    "type": "image",
                    "url": direct_url,
                    "title": title,
                    "ext": ext or "jpg",
                    "headers": headers,
                    "source": url,
                })

    return items


def find_urls(text: str) -> List[str]:
    return [m.group(0) for m in _URL_RE.finditer(text)]


def download_with_ytdlp(url: str) -> List[str]:
    """Download media to temporary files using yt-dlp (handles HLS).

    Returns list of file paths. Caller must delete them.
    """
    tmpdir = tempfile.mkdtemp(prefix="ytdlp-")
    cookiefile = _cookies_file_for(url)
    fmt = "bv*+ba/b[ext=mp4]/b"  # prefer merged mp4
    ydl_opts: Dict[str, Any] = {
        "quiet": True,
        "noplaylist": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "nocheckcertificate": True,
        "ignoreerrors": True,
        "retries": 3,
        "fragment_retries": 3,
        "merge_output_format": "mp4",
        "format": fmt,
        "outtmpl": os.path.join(tmpdir, "%(title).80s-%(id)s.%(ext)s"),
        "concurrent_fragment_downloads": 3,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "*/*",
            "Referer": url,
        },
        "source_address": "0.0.0.0",
    }
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile

    files: List[str] = []
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            for entry in _iter_entries(info):
                # Construct path by yt-dlp template or read from entry
                # Use expected file path in tmpdir with ext
                fn = entry.get("_filename") or None
                if not fn:
                    ext = (entry.get("ext") or "mp4").lower()
                    vid = (entry.get("id") or "media")
                    title = (entry.get("title") or "media")[:80]
                    fn = os.path.join(tmpdir, f"{title}-{vid}.{ext}")
                if os.path.exists(fn):
                    files.append(fn)
    finally:
        try:
            if cookiefile:
                os.unlink(cookiefile)
        except Exception:
            pass
    return files
