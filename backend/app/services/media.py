import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

MAX_VIDEO_BYTES = 500 * 1024 * 1024


def image_extension(payload: bytes) -> str:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload.startswith(b"GIF87a") or payload.startswith(b"GIF89a"):
        return ".gif"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp"
    if payload.startswith(b"BM"):
        return ".bmp"
    return ".png"


class DownloadError(Exception):
    pass


async def download_video(url: str, destination: Path, headers: dict | None = None) -> None:
    if not url.startswith("https://"):
        raise DownloadError("URL video không hợp lệ.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    written = 0
    timeout = httpx.Timeout(300.0, connect=30.0)
    request_headers = dict(headers or {})
    current = url
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _ in range(6):
                async with client.stream("GET", current, headers=request_headers) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise DownloadError("Máy chủ video không trả địa chỉ tải.")
                        nxt = urljoin(str(response.url), location)
                        if urlparse(nxt).netloc != urlparse(current).netloc:
                            request_headers.pop("Authorization", None)
                        current = nxt
                        continue
                    if response.status_code >= 400:
                        raise DownloadError(f"Không tải được video (HTTP {response.status_code}).")
                    with temporary.open("wb") as handle:
                        async for chunk in response.aiter_bytes(256 * 1024):
                            written += len(chunk)
                            if written > MAX_VIDEO_BYTES:
                                raise DownloadError("Video lớn hơn 500MB.")
                            handle.write(chunk)
                    break
            else:
                raise DownloadError("Quá nhiều chuyển hướng khi tải video.")
        if written == 0:
            raise DownloadError("File video tải về rỗng.")
        temporary.replace(destination)
    except DownloadError:
        temporary.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise DownloadError(f"Lỗi tải video: {exc}") from exc


def safe_download_name(title: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip()
    cleaned = re.sub(r"[-\s]+", "-", cleaned)[:80].strip("-")
    return f"{cleaned or 'video'}.mp4"
