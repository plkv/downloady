from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Literal, Optional, TypedDict

from yt_dlp import YoutubeDL


class MediaItem(TypedDict, total=False):
    type: Literal["image", "video"]
    url: str
    title: str
    ext: str
    filesize: Optional[int]
    headers: Dict[str, Any]


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
        # Some CDNs block IPv6 on servers
        "source_address": "0.0.0.0",
    }

    items: List[MediaItem] = []

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    for entry in _iter_entries(info):
        title = (entry.get("title") or "").strip()
        ext = (entry.get("ext") or "").lower()
        direct_url = entry.get("url")
        formats = entry.get("formats") or []

        # Image case (some extractors return direct image URL with ext)
        if (not formats) and direct_url and ext in {"jpg", "jpeg", "png", "webp", "gif"}:
            items.append({"type": "image", "url": direct_url, "title": title, "ext": ext})
            continue

        # Video case
        best = pick_best_video_format(formats)
        if best and best.get("url"):
            headers = best.get("http_headers") or {}
            items.append(
                {
                    "type": "video",
                    "url": best["url"],
                    "title": title,
                    "ext": (best.get("ext") or ext or "").lower(),
                    "filesize": best.get("filesize") or best.get("filesize_approx"),
                    "headers": headers,
                }
            )
            continue

        # Fallback: if extractor returned a direct url that looks like media
        if direct_url:
            if re.search(r"\.(mp4|m4v|mov|webm)(?:[?#].*)?$", direct_url, re.I):
                items.append({"type": "video", "url": direct_url, "title": title, "ext": ext or "mp4"})
            elif re.search(r"\.(jpe?g|png|webp|gif)(?:[?#].*)?$", direct_url, re.I):
                items.append({"type": "image", "url": direct_url, "title": title, "ext": ext or "jpg"})

    return items


def find_urls(text: str) -> List[str]:
    return [m.group(0) for m in _URL_RE.finditer(text)]
