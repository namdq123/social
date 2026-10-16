import base64
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.constants import IMAGE_MODEL

logger = logging.getLogger(__name__)

MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

DEFAULT_MODEL = "heygen/heygen-video-1"


class SeedanceError(Exception):
    pass


class SeedanceThrottle(SeedanceError):
    pass


@dataclass
class ReferenceImage:
    name: str
    path: str


@dataclass
class GenerationRequest:
    prompt: str
    model: str
    resolution: str
    ratio: str
    duration: int
    generate_audio: bool
    watermark: bool
    image_path: str | None
    image_url: str | None
    references: list[ReferenceImage] | None = None
    frame_only: bool = False


def _error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        text = response.text.strip()
        return text[:500] or f"OpenRouter lỗi HTTP {response.status_code}"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        message = str(err.get("message") or err.get("code") or "").strip()
        if "insufficient credits" in message.lower():
            return "OpenRouter hết credit. Nạp thêm rồi thử lại."
        return message or f"OpenRouter lỗi HTTP {response.status_code}"
    if isinstance(err, str) and err.strip():
        return err.strip()
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])[:500]
    return f"OpenRouter lỗi HTTP {response.status_code}"


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 429:
        raise SeedanceThrottle("OpenRouter đang giới hạn tần suất. Hệ thống sẽ thử lại.")
    if response.status_code >= 400:
        raise SeedanceError(_error_message(response))


def _image_data_url(path: str) -> str:
    file_path = Path(path)
    mime = MIME_BY_SUFFIX.get(file_path.suffix.lower(), "image/png")
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _reference_entry(url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


def _finish_payload(body: dict) -> dict:
    model = str(body.get("model") or "")
    if model.startswith("heygen/"):
        body.pop("generate_audio", None)
    elif "grok-imagine" in model and body.get("input_references") and body.get("resolution") == "1080p":
        body["resolution"] = "720p"
    return body


def build_payload(request: GenerationRequest) -> dict:
    body: dict = {
        "model": request.model or DEFAULT_MODEL,
        "prompt": request.prompt,
        "resolution": request.resolution,
        "aspect_ratio": request.ratio,
        "generate_audio": request.generate_audio,
    }
    if request.duration > 0:
        body["duration"] = request.duration

    extra_url = None
    if request.image_path and Path(request.image_path).is_file():
        extra_url = _image_data_url(request.image_path)
    elif request.image_url:
        extra_url = request.image_url
    if request.frame_only and extra_url:
        body["frame_images"] = [
            {
                "type": "image_url",
                "image_url": {"url": extra_url},
                "frame_type": "first_frame",
            }
        ]
        return _finish_payload(body)

    references = []
    for item in request.references or []:
        if item.path and Path(item.path).is_file():
            references.append((item.name, _image_data_url(item.path)))
    if references:
        if extra_url:
            references.append(("cảnh", extra_url))
        names = ", ".join(name for name, _url in references if name and name != "cảnh")
        if names:
            body["prompt"] = (
                "Giữ đúng khuôn mặt, trang phục và đặc điểm của các nhân vật trong ảnh tham chiếu "
                f"({names}). {request.prompt}"
            )
        body["input_references"] = [_reference_entry(url) for _name, url in references[:8]]
        return _finish_payload(body)

    if extra_url:
        body["frame_images"] = [
            {
                "type": "image_url",
                "image_url": {"url": extra_url},
                "frame_type": "first_frame",
            }
        ]
    return _finish_payload(body)


def _image_bytes_from_payload(data: dict) -> tuple[bytes, str] | None:
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        items = data.get("images") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    item = items[0]
    encoded = item.get("b64_json") or item.get("b64")
    if isinstance(encoded, str) and encoded:
        return base64.b64decode(encoded), ".png"
    url = item.get("url")
    nested = item.get("image_url")
    if isinstance(nested, dict):
        url = nested.get("url") or url
    if isinstance(url, str) and url.startswith("https://"):
        return url.encode("utf-8"), "url"
    return None


async def generate_character_image(
    api_key: str,
    base_url: str,
    prompt: str,
    aspect_ratio: str,
    references: list[tuple[str, str]] | None = None,
) -> bytes:
    text = prompt.strip()
    named = [name for name, path in (references or []) if name and path and Path(path).is_file()]
    if named:
        text = (
            "Giữ đúng khuôn mặt, tóc, trang phục và đặc điểm trong ảnh tham chiếu "
            f"({', '.join(named)}). {text}"
        )
    payload = {
        "model": IMAGE_MODEL,
        "prompt": text,
        "aspect_ratio": aspect_ratio,
        "resolution": "1K",
        "n": 1,
    }
    ref_items = []
    for _name, path in (references or [])[:8]:
        if path and Path(path).is_file():
            ref_items.append(_reference_entry(_image_data_url(path)))
    if ref_items:
        payload["input_references"] = ref_items
    url = f"{base_url.rstrip('/')}/images"
    async with _client(api_key, timeout=120) as client:
        response = await client.post(url, json=payload)
        if response.status_code >= 400:
            _raise_for_status(response)
        parsed = _image_bytes_from_payload(response.json())
        if parsed is None:
            raise SeedanceError("OpenRouter không trả về ảnh nhân vật.")
        raw, kind = parsed
        if kind != "url":
            return raw
        image_url = raw.decode("utf-8")
    headers = {}
    if needs_auth_header(image_url):
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=20.0), follow_redirects=True) as client:
        downloaded = await client.get(image_url, headers=headers)
    if downloaded.status_code >= 400:
        raise SeedanceError(f"Không tải được ảnh nhân vật (HTTP {downloaded.status_code}).")
    if not downloaded.content:
        raise SeedanceError("Ảnh nhân vật tải về rỗng.")
    return downloaded.content


def _client(api_key: str, timeout: float = 60.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=20.0),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )


async def create_task(api_key: str, base_url: str, request: GenerationRequest) -> str:
    url = f"{base_url.rstrip('/')}/videos"
    async with _client(api_key, timeout=180) as client:
        response = await client.post(url, json=build_payload(request))
    if response.status_code not in {200, 201, 202}:
        _raise_for_status(response)
    data = response.json()
    task_id = data.get("id") if isinstance(data, dict) else None
    if not task_id:
        raise SeedanceError("OpenRouter không trả về mã tác vụ video.")
    logger.info("Created OpenRouter video job %s model=%s", task_id, request.model)
    return str(task_id)


async def get_task(api_key: str, base_url: str, task_id: str) -> dict:
    url = f"{base_url.rstrip('/')}/videos/{task_id}"
    async with _client(api_key) as client:
        response = await client.get(url)
    if response.status_code == 404:
        raise SeedanceError("Tác vụ video không còn tồn tại.")
    _raise_for_status(response)
    data = response.json()
    if not isinstance(data, dict):
        raise SeedanceError("Phản hồi OpenRouter không hợp lệ.")
    return data


def content_download_url(base_url: str, task_id: str) -> str:
    return f"{base_url.rstrip('/')}/videos/{task_id}/content"


def video_url_from_task(data: dict, base_url: str) -> str | None:
    urls = data.get("urls")
    if isinstance(urls, list):
        for item in urls:
            if isinstance(item, str) and item.startswith("https://"):
                return item
            if isinstance(item, dict):
                url = item.get("url")
                if isinstance(url, str) and url.startswith("https://"):
                    return url
    unsigned = data.get("unsigned_urls")
    if isinstance(unsigned, list):
        for item in unsigned:
            if isinstance(item, str) and item.startswith("https://"):
                return item
    task_id = data.get("id")
    if isinstance(task_id, str) and task_id:
        return content_download_url(base_url, task_id)
    return None


def needs_auth_header(url: str) -> bool:
    return "openrouter.ai" in url


def failure_message(data: dict) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        message = str(err.get("message") or err.get("code") or "").strip()
        if message:
            return message
    if isinstance(err, str) and err.strip():
        return err.strip()
    status = data.get("status") or "failed"
    return f"OpenRouter kết thúc với trạng thái {status}"


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(parts)
    return ""


def _json_from_text(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise SeedanceError("Model không trả về JSON object.")
    return data


async def complete_json(
    api_key: str,
    base_url: str,
    system: str,
    user: str,
    model: str,
    temperature: float = 0.7,
) -> dict:
    url = f"{base_url.rstrip('/')}/chat/completions"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    async with _client(api_key, timeout=120) as client:
        response = await client.post(url, json=body)
        if response.status_code == 400:
            body.pop("response_format", None)
            response = await client.post(url, json=body)
    _raise_for_status(response)
    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SeedanceError("OpenRouter không trả về nội dung.") from exc
    try:
        return _json_from_text(_message_text(content))
    except (json.JSONDecodeError, SeedanceError) as exc:
        raise SeedanceError("Model không trả về JSON hợp lệ.") from exc


async def probe_api_key(api_key: str, base_url: str) -> str:
    url = f"{base_url.rstrip('/')}/key"
    async with _client(api_key, timeout=30) as client:
        response = await client.get(url)
    if response.status_code in {401, 403}:
        raise SeedanceError("OpenRouter từ chối API key.")
    _raise_for_status(response)
    return "API key OpenRouter hợp lệ."
