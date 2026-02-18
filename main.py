import html
import json
import logging
import re
from typing import Iterable, Optional, Tuple
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
from telegram import InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import yt_dlp

BOT_TOKEN = "8582785227:AAFh4uRwbKPOUuNHx1_mKecVDWUX9osoeLQ"
TIKTOK_COOKIES_PATH = "tiktok_cookies.txt"
TIKTOK_COOKIES_FALLBACK = "cookies.txt"
TIKTOK_USE_COOKIES = False
APIFY_TOKEN = "apify_api_DliZ0AEL2IZcf37WCulB7pQFdHXmhq09HHuM"
APIFY_ACTOR_ID = "scrapearchitect~tiktok-video-audio-mp3-photo-slideshows-downloader"
APIFY_INSTAGRAM_ACTOR_ID = "alpha-scraper~instagram-video-scraper-downloader"
INSTAGRAM_USE_APIFY_FALLBACK = True
RAPIDAPI_KEY = "210df55a93msh16d391db925aa7ap1feafajsn1c42322395a2"
RAPIDAPI_HOST = "instagram-downloader-download-instagram-videos-stories1.p.rapidapi.com"
RAPIDAPI_BASE_URL = f"https://{RAPIDAPI_HOST}/"
RAPIDAPI_REELS_HOST = "instagram-reels-downloader-api.p.rapidapi.com"
RAPIDAPI_REELS_BASE_URL = f"https://{RAPIDAPI_REELS_HOST}/download"
LOGGER = logging.getLogger("video-note-bot")

REPLY_NOT_VIDEO = "Пожалуйста, отправь видео."
REPLY_ERROR = "Не удалось обработать видео. Попробуй другое."
REPLY_DONE = "Готово."
REPLY_LINK_NOT_VIDEO = "Пришли ссылку на видео."
REPLY_COOKIES_SAVED = "Готово."
REPLY_NEED_COOKIES = "Нужны cookies TikTok: пришли файл tiktok_cookies.txt."
REPLY_INSTAGRAM_UNSUPPORTED = "Инстаграм не поддерживается."
START_MESSAGE = "👋 <b>Привет, {name}!</b>\nОтправь мне видео и я преобразую его в кружок!"
URL_RE = re.compile(r"(https?://\S+)")


def process_video(input_path: Path, output_path: Path) -> None:
    # Crop to centered square, keep max 640x640, trim to 60s, H.264/AAC MP4.
    vf = (
        "crop='floor(min(iw,ih)/2)*2':'floor(min(iw,ih)/2)*2':"
        "(iw-ow)/2:(ih-oh)/2,"
        "scale=640:640,"
        "setsar=1"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-t",
        "60",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "30",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg failed")


def extract_audio_from_video(input_path: Path, output_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg audio extract failed")


def extract_url(text: str) -> Optional[str]:
    match = URL_RE.search(text or "")
    if not match:
        return None
    url = match.group(1).strip()
    return url.strip("()[]<>.,!\"'")


def is_tiktok_photo(url: str) -> bool:
    lower = (url or "").lower()
    if "tiktok.com" in lower and "/photo/" in lower:
        return True
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    return "tiktok.com" in host and "/photo/" in path


def is_tiktok_url(url: str) -> bool:
    lower = (url or "").lower()
    if "tiktok.com" in lower or "vt.tiktok.com" in lower:
        return True
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    return "tiktok.com" in host or "vt.tiktok.com" in host


def extract_tiktok_item_id(url: str) -> Optional[str]:
    match = re.search(r"/(?:photo|video)/(\d+)", url)
    if match:
        return match.group(1)
    match = re.search(r"[?&]item_id=(\d+)", url)
    if match:
        return match.group(1)
    return None


def find_tiktok_cookies_path() -> Optional[Path]:
    primary = Path(TIKTOK_COOKIES_PATH)
    if primary.exists():
        return primary
    fallback = Path(TIKTOK_COOKIES_FALLBACK)
    if fallback.exists():
        return fallback
    return None


def load_tiktok_cookies() -> Optional[httpx.Cookies]:
    if not TIKTOK_USE_COOKIES:
        return None
    path = find_tiktok_cookies_path()
    if not path:
        return None
    cookies = httpx.Cookies()
    count = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _flag, cpath, _secure, _expires, name, value = parts[:7]
            cookies.set(name, value, domain=domain, path=cpath)
            count += 1
    except Exception:
        return None
    if count:
        LOGGER.info("Loaded %s TikTok cookies from %s", count, path.name)
    return cookies


def has_tiktok_cookies() -> bool:
    if not TIKTOK_USE_COOKIES:
        return False
    path = find_tiktok_cookies_path()
    return bool(path and path.stat().st_size > 0)


def resolve_tiktok_url(url: str) -> str:
    lower = (url or "").lower()
    if "tiktok.com" not in lower and "vt.tiktok.com" not in lower:
        return url
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        cookies = load_tiktok_cookies()
        client_kwargs = {"follow_redirects": True, "timeout": 15.0, "headers": headers}
        if cookies:
            client_kwargs["cookies"] = cookies
        with httpx.Client(
            **client_kwargs
        ) as client:
            resp = client.get(url)
            return str(resp.url)
    except Exception:
        return url


def extract_json_from_html(page_html: str) -> Optional[dict]:
    patterns = [
        r'<script[^>]+id="SIGI_STATE"[^>]*>(?P<data>.*?)</script>',
        r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(?P<data>.*?)</script>',
        r'window\.\s*\[?"SIGI_STATE"\]?\s*=\s*(?P<data>\{.*?\})\s*;'
    ]
    for pattern in patterns:
        match = re.search(pattern, page_html, re.DOTALL | re.IGNORECASE)
        if not match:
            continue
        raw = match.group("data").strip()
        raw = html.unescape(raw)
        if raw.startswith("window") and "=" in raw:
            raw = raw.split("=", 1)[1].strip().rstrip(";")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            try:
                # Some pages embed JSON as a string literal
                decoded = json.loads(raw.strip().strip(";"))
                if isinstance(decoded, str):
                    return json.loads(decoded)
            except Exception:
                continue
    return None


def is_image_url(url: str) -> bool:
    lower = url.lower()
    return lower.startswith("http") and (
        lower.endswith(".jpg")
        or lower.endswith(".jpeg")
        or lower.endswith(".png")
        or ".jpg?" in lower
        or ".jpeg?" in lower
        or ".png?" in lower
    )


def extract_urls_from_value(value: object) -> Iterable[str]:
    if isinstance(value, dict):
        for key in ("urlList", "url_list", "urls", "url"):
            if key in value:
                inner = value.get(key)
                if isinstance(inner, list) and inner:
                    return [inner[-1]]
                if isinstance(inner, str) and is_image_url(inner):
                    return [inner]
    if isinstance(value, str) and is_image_url(value):
        return [value]
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        if is_image_url(value[-1]):
            return [value[-1]]
    return []


def is_audio_url(url: str) -> bool:
    lower = url.lower()
    return (
        lower.startswith("http")
        and (
            ".mp3" in lower
            or ".m4a" in lower
            or "mime_type=audio" in lower
            or ("audio" in lower and "tiktokcdn" in lower)
            or ("music" in lower and "tiktokcdn" in lower)
        )
    )


def extract_audio_urls_from_value(value: object) -> Iterable[str]:
    if isinstance(value, dict):
        for key in ("urlList", "url_list", "url"):
            if key in value:
                inner = value.get(key)
                if isinstance(inner, list) and inner:
                    return [inner[-1]]
                if isinstance(inner, str) and inner.startswith("http"):
                    return [inner]
    if isinstance(value, str) and value.startswith("http"):
        return [value]
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        for v in value:
            if v.startswith("http") or is_audio_url(v):
                return [v]
    return []


def extract_tiktok_audio_url(data: object) -> Optional[str]:
    candidates: list[str] = []

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            if "music" in obj and isinstance(obj["music"], dict):
                music = obj["music"]
                for key in (
                    "playUrl",
                    "playUrlV2",
                    "playUrlV3",
                    "playUrlList",
                    "play_url",
                    "play_url_list",
                ):
                    candidates.extend(extract_audio_urls_from_value(music.get(key)))
            for key, value in obj.items():
                if key.lower() in {
                    "playurl",
                    "playurlv2",
                    "playurlv3",
                    "playurllist",
                    "play_url",
                    "play_url_list",
                }:
                    candidates.extend(extract_audio_urls_from_value(value))
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
        elif isinstance(obj, str):
            if is_audio_url(obj):
                candidates.append(obj)

    walk(data)
    for url in candidates:
        return url
    return None


def find_item_struct_by_id(data: object, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    if isinstance(data, dict):
        if "ItemModule" in data and isinstance(data["ItemModule"], dict):
            if item_id in data["ItemModule"]:
                return data["ItemModule"][item_id]
        if "itemStruct" in data and isinstance(data["itemStruct"], dict):
            item = data["itemStruct"]
            if str(item.get("id") or item.get("itemId")) == item_id:
                return item
        if str(data.get("id") or data.get("itemId")) == item_id and (
            "imagePost" in data or "music" in data or "video" in data
        ):
            return data
        for value in data.values():
            found = find_item_struct_by_id(value, item_id)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_item_struct_by_id(value, item_id)
            if found:
                return found
    return None


def find_first_imagepost_item(data: object) -> Optional[dict]:
    if isinstance(data, dict):
        if "imagePost" in data or "imagePostInfo" in data:
            return data
        for value in data.values():
            found = find_first_imagepost_item(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_first_imagepost_item(value)
            if found:
                return found
    return None


def extract_photo_urls_from_item(item: dict) -> list[str]:
    urls: list[str] = []

    images = []
    if "imagePost" in item and isinstance(item["imagePost"], dict):
        images = item["imagePost"].get("images") or []
    elif "imagePostInfo" in item and isinstance(item["imagePostInfo"], dict):
        images = item["imagePostInfo"].get("images") or []
    elif "images" in item and isinstance(item["images"], list):
        images = item["images"]

    for image in images:
        if isinstance(image, dict):
            for key in (
                "displayImage",
                "imageURL",
                "imageUrl",
                "image_url",
                "urlList",
                "url_list",
            ):
                urls.extend(extract_urls_from_value(image.get(key)))
        else:
            urls.extend(extract_urls_from_value(image))

    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def extract_audio_url_from_item(item: dict) -> Optional[str]:
    music = item.get("music") if isinstance(item.get("music"), dict) else None
    if music:
        for key in (
            "playUrl",
            "playUrlV2",
            "playUrlV3",
            "playUrlList",
            "play_url",
            "play_url_list",
        ):
            urls = extract_audio_urls_from_value(music.get(key))
            for url in urls:
                return url
    for key in ("playUrl", "playUrlV2", "playUrlV3"):
        urls = extract_audio_urls_from_value(item.get(key))
        for url in urls:
            return url
    return None


def fetch_tiktok_item_detail(item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    cookies = load_tiktok_cookies()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.tiktok.com/@tiktok/photo/{item_id}",
    }
    try:
        client_kwargs = {
            "follow_redirects": True,
            "timeout": 20.0,
            "headers": headers,
        }
        if cookies:
            client_kwargs["cookies"] = cookies
        with httpx.Client(**client_kwargs) as client:
            resp = client.get(
                "https://www.tiktok.com/api/item/detail/",
                params={"itemId": item_id, "aid": "1988"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception:
        return None

    if isinstance(data, dict):
        status = data.get("statusCode") or data.get("status_code")
        if status not in (0, None):
            LOGGER.info("TikTok item detail status=%s", status)

    item_info = data.get("itemInfo") if isinstance(data, dict) else None
    if isinstance(item_info, dict):
        item_struct = item_info.get("itemStruct")
        if isinstance(item_struct, dict):
            return item_struct
    return None


def fetch_tiktok_oembed(url: str) -> Optional[dict]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        with httpx.Client(
            follow_redirects=True, timeout=15.0, headers=headers
        ) as client:
            resp = client.get("https://www.tiktok.com/oembed", params={"url": url})
            if resp.status_code != 200:
                return None
            data = resp.json()
            if isinstance(data, dict):
                return data
    except Exception:
        return None
    return None


def extract_oembed_canonical_url(oembed: dict) -> Optional[str]:
    if not oembed:
        return None
    html_block = oembed.get("html") if isinstance(oembed.get("html"), str) else ""
    if html_block:
        match = re.search(r'cite="(https?://[^"]+)"', html_block)
        if match:
            return match.group(1)
        match = re.search(r'href="(https?://[^"]+)"', html_block)
        if match:
            return match.group(1)
        match = re.search(r'data-video-id="(\d+)"', html_block)
        if match:
            video_id = match.group(1)
            author_url = oembed.get("author_url") if isinstance(oembed.get("author_url"), str) else ""
            author = author_url.rstrip("/").split("/")[-1] if author_url else "tiktok"
            return f"https://www.tiktok.com/@{author}/video/{video_id}"
    return None


def download_audio_from_url(url: str, output_dir: Path) -> Optional[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_tmpl = str(output_dir / "audio.%(ext)s")
    ydl_opts = {
        "outtmpl": output_tmpl,
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "128"}
        ],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception:
        return None

    candidates = sorted(
        output_dir.glob("audio.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    return candidates[0]


def tiktok_audio_candidates(url: str) -> list[str]:
    candidates = [url]
    if "/photo/" in url:
        candidates.append(url.replace("/photo/", "/video/"))
    oembed = fetch_tiktok_oembed(url)
    canonical = extract_oembed_canonical_url(oembed or {})
    if canonical:
        candidates.append(canonical)
        if "/photo/" in canonical:
            candidates.append(canonical.replace("/photo/", "/video/"))
    unique: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def extract_ytdlp_info(url: str) -> Optional[dict]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        return None


def is_instagram_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    return "instagram.com" in host or "instagr.am" in host


def extract_instagram_shortcode(url: str) -> Optional[str]:
    match = re.search(r"/(p|reel|tv)/([A-Za-z0-9_-]+)/?", url)
    if match:
        return match.group(2)
    return None


def extract_instagram_username(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    path = (parsed.path or "").strip("/")
    if not path:
        return None
    first = path.split("/", 1)[0]
    if first in {"p", "reel", "tv", "stories"}:
        return None
    return first


def instagram_profile_url(username: Optional[str]) -> Optional[str]:
    if not username:
        return None
    return f"https://www.instagram.com/{username}/"


def extract_instagram_media_type(url: str) -> Optional[str]:
    match = re.search(r"/(p|reel|tv|stories)/", url)
    if match:
        return match.group(1)
    return None


def normalize_instagram_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or "www.instagram.com"
    path = parsed.path or ""
    # Normalize short domains
    if netloc in {"instagr.am", "instagram.com"}:
        netloc = "www.instagram.com"
    # Ensure trailing slash for media paths
    match = re.search(r"^/(p|reel|tv|stories)/[^/]+/?", path)
    if match and not path.endswith("/"):
        path = path + "/"
    if not path.startswith("/"):
        path = "/" + path
    return f"{scheme}://{netloc}{path}"


def is_probable_video_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    lower = url.lower()
    if not lower.startswith("http"):
        return False
    if any(ext in lower for ext in (".mp4", ".mov", ".webm", ".mkv")):
        return True
    if "mime_type=video" in lower or "video" in lower:
        if not any(ext in lower for ext in (".jpg", ".jpeg", ".png", ".webp")):
            return True
    return False


def extract_video_urls_from_response(data: object) -> list[str]:
    urls: list[str] = []

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str) and is_probable_video_url(value):
                    urls.append(value)
                elif isinstance(value, (dict, list)):
                    walk(value)
                elif isinstance(value, str) and key.lower() in {
                    "video_url",
                    "videourl",
                    "download_url",
                    "downloadurl",
                    "media_url",
                    "mediaurl",
                    "url",
                    "download",
                    "file",
                }:
                    if is_probable_video_url(value):
                        urls.append(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
        elif isinstance(obj, str) and is_probable_video_url(obj):
            urls.append(obj)

    walk(data)
    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def rapidapi_fetch_instagram_video_urls(url: str) -> list[str]:
    if not RAPIDAPI_KEY or "PASTE_RAPIDAPI_KEY_HERE" in RAPIDAPI_KEY:
        raise RuntimeError("RAPIDAPI_KEY is not set in code")
    clean_url = normalize_instagram_url(url)
    username = extract_instagram_username(clean_url)
    profile_url = instagram_profile_url(username)
    media_type = extract_instagram_media_type(clean_url)
    headers = {
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": RAPIDAPI_KEY,
    }
    param_keys = [
        "url",
        "Url",
        "URL",
        "link",
        "Link",
        "instagram_url",
        "instagramUrl",
        "media_url",
        "mediaUrl",
        "reel_url",
        "reelUrl",
        "video_url",
        "videoUrl",
        "post_url",
        "postUrl",
        "media",
        "download",
        "Userinfo",
        "username",
    ]
    param_values = [clean_url, url]
    if profile_url:
        param_values.append(profile_url)
    if username:
        param_values.append(username)
    params_variants = []
    for key in param_keys:
        for value in param_values:
            params_variants.append({key: value})
    if media_type:
        for key in param_keys:
            for value in param_values:
                params_variants.append({key: value, "type": media_type})
                params_variants.append({key: value, "media_type": media_type})
                params_variants.append({key: value, "mediaType": media_type})
    last_error: Optional[Exception] = None
    for params in params_variants:
        try:
            with httpx.Client(timeout=30.0, headers=headers) as client:
                resp = client.get(RAPIDAPI_BASE_URL, params=params)
                if resp.status_code == 400:
                    LOGGER.info("RapidAPI IG 400 response: %s", resp.text[:200])
                resp.raise_for_status()
                data = resp.json()
                urls = extract_video_urls_from_response(data)
                if urls:
                    return urls
        except Exception as exc:
            last_error = exc
            continue

    # Try POST with JSON and form data in case the API expects body params
    post_variants = []
    for key in param_keys:
        for value in param_values:
            post_variants.append({key: value})
            if media_type:
                post_variants.append({key: value, "type": media_type})
                post_variants.append({key: value, "media_type": media_type})
                post_variants.append({key: value, "mediaType": media_type})
    for payload in post_variants:
        try:
            with httpx.Client(timeout=30.0, headers=headers) as client:
                resp = client.post(RAPIDAPI_BASE_URL, json=payload)
                if resp.status_code == 400:
                    LOGGER.info("RapidAPI IG 400 response: %s", resp.text[:200])
                resp.raise_for_status()
                data = resp.json()
                urls = extract_video_urls_from_response(data)
                if urls:
                    return urls
        except Exception as exc:
            last_error = exc
            continue
        try:
            with httpx.Client(timeout=30.0, headers=headers) as client:
                resp = client.post(RAPIDAPI_BASE_URL, data=payload)
                if resp.status_code == 400:
                    LOGGER.info("RapidAPI IG 400 response: %s", resp.text[:200])
                resp.raise_for_status()
                data = resp.json()
                urls = extract_video_urls_from_response(data)
                if urls:
                    return urls
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return []


def rapidapi_fetch_instagram_reel_video_urls(url: str) -> list[str]:
    if not RAPIDAPI_KEY or "PASTE_RAPIDAPI_KEY_HERE" in RAPIDAPI_KEY:
        raise RuntimeError("RAPIDAPI_KEY is not set in code")
    headers = {
        "x-rapidapi-host": RAPIDAPI_REELS_HOST,
        "x-rapidapi-key": RAPIDAPI_KEY,
    }
    params = {"url": normalize_instagram_url(url)}
    with httpx.Client(timeout=30.0, headers=headers) as client:
        resp = client.get(RAPIDAPI_REELS_BASE_URL, params=params)
        if resp.status_code == 400:
            LOGGER.info("RapidAPI Reels 400 response: %s", resp.text[:200])
        resp.raise_for_status()
        data = resp.json()
    urls = extract_video_urls_from_response(data)
    return urls


def fetch_instagram_json(shortcode: str, kind: str) -> Optional[dict]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/json,text/plain,*/*",
        "X-IG-App-ID": "936619743392459",
    }
    url = f"https://www.instagram.com/{kind}/{shortcode}/"
    params = {"__a": "1", "__d": "dis"}
    try:
        with httpx.Client(follow_redirects=True, timeout=20.0, headers=headers) as client:
            resp = client.get(url, params=params)
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:
        return None


def extract_instagram_video_url(data: dict) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    graphql = data.get("graphql")
    if isinstance(graphql, dict):
        media = graphql.get("shortcode_media")
        if isinstance(media, dict):
            if isinstance(media.get("video_url"), str):
                return media["video_url"]
            if isinstance(media.get("video_versions"), list):
                for v in media["video_versions"]:
                    if isinstance(v, dict) and isinstance(v.get("url"), str):
                        return v["url"]
            if isinstance(media.get("carousel_media"), list):
                for item in media["carousel_media"]:
                    if isinstance(item, dict) and isinstance(item.get("video_url"), str):
                        return item["video_url"]
                    if isinstance(item, dict) and isinstance(item.get("video_versions"), list):
                        for v in item["video_versions"]:
                            if isinstance(v, dict) and isinstance(v.get("url"), str):
                                return v["url"]
    items = data.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                if isinstance(item.get("video_versions"), list):
                    for v in item["video_versions"]:
                        if isinstance(v, dict) and isinstance(v.get("url"), str):
                            return v["url"]
                if isinstance(item.get("carousel_media"), list):
                    for media in item["carousel_media"]:
                        if isinstance(media, dict) and isinstance(media.get("video_versions"), list):
                            for v in media["video_versions"]:
                                if isinstance(v, dict) and isinstance(v.get("url"), str):
                                    return v["url"]
    return None


def download_instagram_video(url: str, output_dir: Path) -> Optional[Path]:
    shortcode = extract_instagram_shortcode(url)
    if not shortcode:
        return None
    for kind in ("reel", "p", "tv"):
        data = fetch_instagram_json(shortcode, kind)
        video_url = extract_instagram_video_url(data or {})
        if video_url:
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / "ig_video.mp4"
            download_file(video_url, path)
            return path
    return None


def probe_duration_seconds(path: Path) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return float(result.stdout.strip())
    except Exception:
        return None


def best_thumbnail_url(info: dict) -> Optional[str]:
    thumbs = info.get("thumbnails") if isinstance(info, dict) else None
    if not isinstance(thumbs, list) or not thumbs:
        thumb = info.get("thumbnail") if isinstance(info, dict) else None
        return thumb if isinstance(thumb, str) else None
    # Prefer highest resolution
    thumbs_sorted = sorted(
        [t for t in thumbs if isinstance(t, dict) and t.get("url")],
        key=lambda t: (t.get("width") or 0, t.get("height") or 0),
        reverse=True,
    )
    if not thumbs_sorted:
        return None
    return thumbs_sorted[0]["url"]


def extract_photo_urls_from_info(info: Optional[dict]) -> list[str]:
    if not info:
        return []
    urls: list[str] = []

    entries = info.get("entries") if isinstance(info, dict) else None
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            candidate = entry.get("url") or entry.get("thumbnail")
            if isinstance(candidate, str) and is_image_url(candidate):
                urls.append(candidate)
                continue
            thumb = best_thumbnail_url(entry)
            if thumb:
                urls.append(thumb)
    else:
        candidate = info.get("url") if isinstance(info, dict) else None
        if isinstance(candidate, str) and is_image_url(candidate):
            urls.append(candidate)
        thumb = best_thumbnail_url(info)
        if thumb:
            urls.append(thumb)

    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def apify_fetch_tiktok_media(url: str) -> Tuple[list[str], Optional[str]]:
    if not APIFY_TOKEN or "PASTE_APIFY_TOKEN_HERE" in APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN is not set in code")
    endpoint = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
    headers = {"Content-Type": "application/json"}
    payload = {
        "desired_resolution": "576p",
        "include_watermark": False,
        "saveToKeyValueStore": True,
        "video_urls": [{"url": url, "method": "GET"}],
    }
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            endpoint,
            headers=headers,
            params={"token": APIFY_TOKEN},
            json=payload,
        )
        resp.raise_for_status()
        items = resp.json()
    if not isinstance(items, list) or not items:
        raise RuntimeError("Apify returned no items")
    item = items[0] if isinstance(items[0], dict) else {}

    def normalize_url_list(value: object) -> list[str]:
        urls: list[str] = []
        if isinstance(value, str):
            urls.append(value)
        elif isinstance(value, list):
            for v in value:
                if isinstance(v, str):
                    urls.append(v)
                elif isinstance(v, dict):
                    for key in ("url", "downloadUrl", "download_url", "file", "fileUrl"):
                        u = v.get(key)
                        if isinstance(u, str):
                            urls.append(u)
        elif isinstance(value, dict):
            for key in ("url", "downloadUrl", "download_url", "file", "fileUrl"):
                u = value.get(key)
                if isinstance(u, str):
                    urls.append(u)
        return urls

    photo_urls = normalize_url_list(
        item.get("photo_downloads")
        or item.get("photoDownloads")
        or item.get("photos")
        or item.get("photo_urls")
        or item.get("photoUrls")
    )

    audio_url = (
        item.get("Download audio")
        or item.get("download_audio")
        or item.get("audio_download")
        or item.get("audio")
        or item.get("audio_url")
        or item.get("audioUrl")
        or item.get("music_url")
        or item.get("musicUrl")
        or item.get("music")
    )
    if isinstance(audio_url, (list, dict)):
        candidates = normalize_url_list(audio_url)
        audio_url = candidates[0] if candidates else None
    if audio_url and not isinstance(audio_url, str):
        audio_url = None

    if not audio_url:
        def find_audio(obj: object) -> Optional[str]:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, str) and is_audio_url(v):
                        return v
                    found = find_audio(v)
                    if found:
                        return found
            elif isinstance(obj, list):
                for v in obj:
                    found = find_audio(v)
                    if found:
                        return found
            elif isinstance(obj, str) and is_audio_url(obj):
                return obj
            return None
        audio_url = find_audio(item)

    return photo_urls, audio_url


def apify_fetch_instagram_video_urls(url: str) -> list[str]:
    if not APIFY_TOKEN or "PASTE_APIFY_TOKEN_HERE" in APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN is not set in code")
    if not APIFY_INSTAGRAM_ACTOR_ID or "PASTE_APIFY_ACTOR_ID_HERE" in APIFY_INSTAGRAM_ACTOR_ID:
        raise RuntimeError("APIFY_INSTAGRAM_ACTOR_ID is not set in code")
    endpoint = (
        f"https://api.apify.com/v2/acts/{APIFY_INSTAGRAM_ACTOR_ID}/run-sync-get-dataset-items"
    )
    headers = {"Content-Type": "application/json"}
    payload = {
        "startUrls": [{"url": url}],
        "resolution": "1080p",
    }
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            endpoint,
            headers=headers,
            params={"token": APIFY_TOKEN},
            json=payload,
        )
        resp.raise_for_status()
        items = resp.json()
    if not isinstance(items, list) or not items:
        raise RuntimeError("Apify returned no items")
    urls = extract_video_urls_from_response(items)
    if not urls:
        raise RuntimeError("no instagram video urls found via Apify")
    return urls


def extract_tiktok_photo_urls(data: object) -> list[str]:
    urls: list[str] = []

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            if "imagePost" in obj:
                image_post = obj.get("imagePost") or {}
                images = image_post.get("images") or []
                for image in images:
                    if isinstance(image, dict):
                        for key in (
                            "displayImage",
                            "imageURL",
                            "imageUrl",
                            "image_url",
                            "urlList",
                            "url_list",
                        ):
                            urls.extend(extract_urls_from_value(image.get(key)))
                    else:
                        urls.extend(extract_urls_from_value(image))
            for key, value in obj.items():
                if key in {
                    "displayImage",
                    "imageURL",
                    "imageUrl",
                    "image_url",
                    "urlList",
                    "url_list",
                }:
                    urls.extend(extract_urls_from_value(value))
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
        elif isinstance(obj, str):
            if is_image_url(obj) and "tiktokcdn" in obj:
                urls.append(obj)

    walk(data)

    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def download_file(url: str, path: Path) -> None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0 Safari/537.36"
        )
    }
    cookies = load_tiktok_cookies()
    client_kwargs = {"follow_redirects": True, "timeout": 30.0, "headers": headers}
    if cookies:
        client_kwargs["cookies"] = cookies
    with httpx.Client(**client_kwargs) as client:
        response = client.get(url)
        response.raise_for_status()
        path.write_bytes(response.content)


def download_tiktok_media(url: str, output_dir: Path) -> Tuple[list[Path], Optional[Path]]:
    item_id = extract_tiktok_item_id(url)
    item_struct = fetch_tiktok_item_detail(item_id)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": url,
    }
    cookies = load_tiktok_cookies()
    client_kwargs = {"follow_redirects": True, "timeout": 30.0, "headers": headers}
    if cookies:
        client_kwargs["cookies"] = cookies
    data = None
    if not item_struct:
        with httpx.Client(**client_kwargs) as client:
            response = client.get(url)
            response.raise_for_status()
            html = response.text

        data = extract_json_from_html(html)
        if data:
            item_struct = find_item_struct_by_id(data, item_id) or find_first_imagepost_item(
                data
            )

    photo_urls: list[str] = []
    audio_url: Optional[str] = None
    if item_struct:
        photo_urls = extract_photo_urls_from_item(item_struct)
        audio_url = extract_audio_url_from_item(item_struct)
    oembed = None
    if not photo_urls:
        oembed = fetch_tiktok_oembed(url)
        if oembed and oembed.get("thumbnail_url"):
            photo_urls = [oembed["thumbnail_url"]]
    if data and len(photo_urls) < 2:
        more_photos = extract_tiktok_photo_urls(data)
        if more_photos:
            for p in more_photos:
                if p not in photo_urls:
                    photo_urls.append(p)
    audio_file_path: Optional[Path] = None
    if not audio_url:
        if oembed is None:
            oembed = fetch_tiktok_oembed(url)
        canonical = extract_oembed_canonical_url(oembed or {})
        if canonical:
            audio_path = download_audio_from_url(canonical, output_dir)
            if audio_path:
                audio_file_path = audio_path
    if not photo_urls and not audio_url and not audio_file_path:
        info = extract_ytdlp_info(url)
        ytdlp_photos = extract_photo_urls_from_info(info)
        if ytdlp_photos:
            photo_urls = ytdlp_photos
        audio_path = download_audio_from_url(url, output_dir)
        if audio_path:
            audio_file_path = audio_path

    if not photo_urls and not audio_url and not audio_file_path:
        alt_url = url.replace("/photo/", "/video/") if "/photo/" in url else url
        info = extract_ytdlp_info(alt_url)
        ytdlp_photos = extract_photo_urls_from_info(info)
        if ytdlp_photos:
            photo_urls = ytdlp_photos
        audio_path = download_audio_from_url(alt_url, output_dir)
        if audio_path:
            audio_file_path = audio_path
    LOGGER.info(
        "TikTok media parsed: item_id=%s, item_struct=%s, photos=%s, audio=%s",
        item_id,
        bool(item_struct),
        len(photo_urls),
        bool(audio_url),
    )

    if not photo_urls and not audio_url and not audio_file_path:
        raise RuntimeError("no tiktok media urls found")

    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for idx, photo_url in enumerate(photo_urls, start=1):
        path = output_dir / f"photo_{idx}.jpg"
        download_file(photo_url, path)
        paths.append(path)

    audio_path: Optional[Path] = None
    if audio_url:
        audio_path = output_dir / "audio.m4a"
        download_file(audio_url, audio_path)
    elif audio_file_path:
        audio_path = audio_file_path

    return paths, audio_path


def download_from_url(url: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_tmpl = str(output_dir / "source.%(ext)s")
    ydl_opts = {
        "outtmpl": output_tmpl,
        "format": "bestvideo[vcodec!=none]+bestaudio[acodec!=none]/best[vcodec!=none]/best",
        "merge_output_format": "mp4",
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
        "postprocessor_args": ["-movflags", "+faststart"],
        "force_keyframes_at_cuts": True,
        "retries": 2,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0 Safari/537.36"
            )
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

        def select_video_entry(data: dict) -> dict:
            entries = data.get("entries") if isinstance(data, dict) else None
            if not entries:
                return data
            for entry in entries:
                if not entry:
                    continue
                entry_info = entry
                if entry.get("_type") in {"url", "url_transparent"} and entry.get("url"):
                    try:
                        entry_info = ydl.extract_info(entry["url"], download=False)
                    except Exception:
                        continue
                formats = entry_info.get("formats") if isinstance(entry_info, dict) else None
                if formats and any(f.get("vcodec") not in (None, "none") for f in formats):
                    return entry_info
            return data

        info = select_video_entry(info if isinstance(info, dict) else {})
        target_url = info.get("webpage_url") if isinstance(info, dict) else None
        if target_url and target_url != url:
            ydl.download([target_url])
        else:
            ydl.download([url])

    def pick_video_file() -> Optional[Path]:
        candidates = sorted(
            output_dir.glob("source.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return None
        for cand in candidates:
            if cand.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
                return cand
        return None

    video_file = pick_video_file()
    if not video_file:
        raise RuntimeError("download failed")

    if is_instagram_url(url):
        duration = probe_duration_seconds(video_file)
        file_size = video_file.stat().st_size
        if duration is not None and duration < 2 and file_size < 300_000:
            alt = download_instagram_video(url, output_dir)
            if alt:
                return alt
            raise RuntimeError("instagram video not accessible without login")

    return video_file


async def process_and_send(
    update: Update, context: ContextTypes.DEFAULT_TYPE, input_path: Path
) -> None:
    output_path = input_path.parent / "output.mp4"
    process_video(input_path, output_path)
    with output_path.open("rb") as f:
        await context.bot.send_video_note(
            chat_id=update.message.chat_id,
            video_note=f,
        )
    await update.message.reply_text(REPLY_DONE)


async def send_full_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    input_path: Path,
    send_done: bool = True,
) -> None:
    with input_path.open("rb") as f:
        await context.bot.send_video(
            chat_id=update.message.chat_id,
            video=f,
            supports_streaming=True,
        )
    if send_done:
        await update.message.reply_text(REPLY_DONE)


async def send_photos(
    update: Update, context: ContextTypes.DEFAULT_TYPE, photo_paths: list[Path]
) -> None:
    if not photo_paths:
        await update.message.reply_text(REPLY_ERROR)
        return

    batch: list[InputMediaPhoto] = []
    for path in photo_paths:
        batch.append(InputMediaPhoto(path.read_bytes()))
        if len(batch) == 10:
            await context.bot.send_media_group(
                chat_id=update.message.chat_id, media=batch
            )
            batch = []

    if batch:
        await context.bot.send_media_group(chat_id=update.message.chat_id, media=batch)

async def send_audio(
    update: Update, context: ContextTypes.DEFAULT_TYPE, audio_path: Path
) -> None:
    with audio_path.open("rb") as f:
        await context.bot.send_audio(
            chat_id=update.message.chat_id,
            audio=f,
            caption="звук из тикток",
        )


async def send_video_audio(
    update: Update, context: ContextTypes.DEFAULT_TYPE, audio_path: Path
) -> None:
    with audio_path.open("rb") as f:
        await context.bot.send_audio(
            chat_id=update.message.chat_id,
            audio=f,
            caption="звук из видео",
        )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    video = update.message.video or update.message.document
    if video is None:
        await update.message.reply_text(REPLY_NOT_VIDEO)
        return

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            tg_file = await context.bot.get_file(video.file_id)
            suffix = Path(tg_file.file_path).suffix or ".mp4"
            input_path = tmpdir_path / f"input{suffix}"
            await tg_file.download_to_drive(custom_path=str(input_path))

            await process_and_send(update, context, input_path)
    except Exception:
        LOGGER.exception("Failed to process video")
        await update.message.reply_text(REPLY_ERROR)


async def handle_not_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(REPLY_NOT_VIDEO)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    first_name = (update.effective_user.first_name or "друг").strip()
    await update.message.reply_text(
        START_MESSAGE.format(name=first_name),
        parse_mode=ParseMode.HTML,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    raw_text = update.message.text or ""
    lower_text = raw_text.lower()
    url = extract_url(raw_text)
    if not url:
        await update.message.reply_text(REPLY_NOT_VIDEO)
        return
    if is_instagram_url(url):
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir_path = Path(tmpdir)
                video_urls: list[str] = []
                media_type = extract_instagram_media_type(url)
                if media_type == "reel":
                    try:
                        video_urls = rapidapi_fetch_instagram_reel_video_urls(url)
                    except Exception as exc:
                        LOGGER.warning("RapidAPI Reels fetch failed: %s", exc)
                if not video_urls:
                    try:
                        video_urls = rapidapi_fetch_instagram_video_urls(url)
                    except Exception as exc:
                        LOGGER.warning("RapidAPI Instagram fetch failed: %s", exc)

                if not video_urls and INSTAGRAM_USE_APIFY_FALLBACK:
                    try:
                        video_urls = apify_fetch_instagram_video_urls(url)
                    except Exception as exc:
                        LOGGER.warning("Apify Instagram fetch failed: %s", exc)

                if video_urls:
                    input_path = tmpdir_path / "ig_video.mp4"
                    download_file(video_urls[0], input_path)
                    await send_full_video(update, context, input_path)
                    return

                alt_path = download_instagram_video(url, tmpdir_path)
                if alt_path:
                    await send_full_video(update, context, alt_path)
                    return
                raise RuntimeError("no instagram video urls")
            return
        except Exception:
            LOGGER.exception("Failed to process instagram url")
            await update.message.reply_text(REPLY_ERROR)
            return
    resolved_url = resolve_tiktok_url(url)
    is_photo_hint = "/photo/" in lower_text or "/photo/" in resolved_url.lower()
    is_photo = is_tiktok_photo(resolved_url) or is_photo_hint
    LOGGER.info(
        "Text URL detected: %r, resolved=%r, is_tiktok_photo=%s, is_photo_hint=%s",
        url,
        resolved_url,
        is_photo,
        is_photo_hint,
    )
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            if is_photo:
                photo_urls, audio_url = apify_fetch_tiktok_media(resolved_url)
                photo_paths: list[Path] = []
                for idx, photo_url in enumerate(photo_urls, start=1):
                    path = tmpdir_path / f"photo_{idx}.jpg"
                    download_file(photo_url, path)
                    photo_paths.append(path)
                audio_path: Optional[Path] = None
                if audio_url:
                    audio_path = tmpdir_path / "audio.m4a"
                    download_file(audio_url, audio_path)
                if not audio_path:
                    for candidate in tiktok_audio_candidates(resolved_url):
                        audio_path = download_audio_from_url(candidate, tmpdir_path)
                        if audio_path:
                            break
                if photo_paths:
                    await send_photos(update, context, photo_paths)
                if audio_path:
                    await send_audio(update, context, audio_path)
                await update.message.reply_text(REPLY_DONE)
                return

            input_path = download_from_url(resolved_url, tmpdir_path)
            if is_tiktok_url(resolved_url):
                await send_full_video(update, context, input_path, send_done=False)
                audio_path = tmpdir_path / "audio.m4a"
                try:
                    extract_audio_from_video(input_path, audio_path)
                    await send_audio(update, context, audio_path)
                except Exception:
                    LOGGER.exception("Failed to extract audio from TikTok video")
                await update.message.reply_text(REPLY_DONE)
            else:
                await send_full_video(update, context, input_path)
    except Exception:
        LOGGER.exception("Failed to process url")
        await update.message.reply_text(REPLY_ERROR)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.message.document is None:
        return
    doc = update.message.document
    file_name = (doc.file_name or "").lower()
    is_cookies_name = (
        file_name == TIKTOK_COOKIES_PATH
        or "cookie" in file_name
        or "cookies" in file_name
        or file_name.endswith(".txt")
    )
    if not is_cookies_name:
        await update.message.reply_text(REPLY_NOT_VIDEO)
        return
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(custom_path=TIKTOK_COOKIES_PATH)
        await update.message.reply_text(REPLY_COOKIES_SAVED)
    except Exception:
        LOGGER.exception("Failed to save cookies")
        await update.message.reply_text(REPLY_ERROR)


def main() -> None:
    token = BOT_TOKEN
    if not token or token == "PASTE_NEW_BOT_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN is not set in code")

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", handle_start), group=0)
    application.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video),
        group=1,
    )
    application.add_handler(
        MessageHandler(filters.Document.ALL & ~filters.Document.VIDEO, handle_document),
        group=2,
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        ),
        group=3,
    )
    application.add_handler(
        MessageHandler(
            filters.ALL
            & ~filters.VIDEO
            & ~filters.Document.VIDEO
            & ~filters.TEXT
            & ~filters.COMMAND,
            handle_not_video,
        ),
        group=4,
    )

    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
