import logging
from datetime import datetime, timedelta

import httpx

from app.constants import YOUTUBE_SCOPES
from app.models import YoutubeAccount, utcnow

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
UPLOAD_INIT_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


class YoutubeError(Exception):
    pass


def _google_error(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return response.text.strip()[:500] or f"YouTube lỗi HTTP {response.status_code}"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        message = err.get("message") or err.get("status")
        errors = err.get("errors")
        if isinstance(errors, list) and errors:
            reason = errors[0].get("reason") or errors[0].get("message")
            if reason and message:
                return f"{message} ({reason})"
            return str(reason or message)
        if message:
            return str(message)
    if isinstance(err, str):
        description = data.get("error_description")
        return f"{err}: {description}" if description else err
    return f"YouTube lỗi HTTP {response.status_code}"


async def exchange_code(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if response.status_code >= 400:
        raise YoutubeError(_google_error(response))
    data = response.json()
    if not data.get("access_token") or not data.get("refresh_token"):
        raise YoutubeError(
            "Google không trả refresh token. Hãy hủy quyền ứng dụng trong tài khoản Google rồi kết nối lại."
        )
    return data


async def refresh_access_token(
    account: YoutubeAccount,
    client_id: str,
    client_secret: str,
) -> str:
    if account.expires_at and account.expires_at > utcnow() + timedelta(seconds=90):
        return account.access_token
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": account.refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if response.status_code >= 400:
        raise YoutubeError(_google_error(response))
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise YoutubeError("Không làm mới được access token YouTube.")
    account.access_token = token
    expires_in = int(data.get("expires_in") or 3600)
    account.expires_at = utcnow() + timedelta(seconds=expires_in)
    account.updated_at = utcnow()
    return token


async def fetch_channel(access_token: str) -> tuple[str | None, str | None]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            CHANNELS_URL,
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code >= 400:
        raise YoutubeError(_google_error(response))
    items = response.json().get("items") or []
    if not items:
        return None, None
    snippet = items[0].get("snippet") or {}
    return items[0].get("id"), snippet.get("title")


def parse_tags(raw: str) -> list[str]:
    tags: list[str] = []
    for part in raw.split(","):
        tag = part.strip()
        if not tag:
            continue
        tags.append(tag[:30])
        if len(tags) >= 15:
            break
    return tags


async def upload_video(
    *,
    access_token: str,
    file_path: str,
    title: str,
    description: str,
    tags: str,
    privacy: str,
    category_id: str,
) -> str:
    path_size = __import__("pathlib").Path(file_path).stat().st_size
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    parsed_tags = parse_tags(tags)
    if parsed_tags:
        body["snippet"]["tags"] = parsed_tags

    timeout = httpx.Timeout(600.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        init = await client.post(
            UPLOAD_INIT_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(path_size),
                "X-Upload-Content-Type": "video/mp4",
            },
            json=body,
        )
        if init.status_code >= 400:
            raise YoutubeError(_google_error(init))
        upload_url = init.headers.get("Location")
        if not upload_url:
            raise YoutubeError("YouTube không trả URL phiên upload.")

        with open(file_path, "rb") as handle:
            payload = handle.read()
        last = path_size - 1 if path_size else 0
        uploaded = await client.put(
            upload_url,
            content=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "video/mp4",
                "Content-Length": str(path_size),
                "Content-Range": f"bytes 0-{last}/{path_size}",
            },
        )
    if uploaded.status_code not in {200, 201}:
        raise YoutubeError(_google_error(uploaded))
    video_id = uploaded.json().get("id")
    if not video_id:
        raise YoutubeError("Upload xong nhưng YouTube không trả video id.")
    logger.info("Uploaded YouTube video %s (%s bytes)", video_id, path_size)
    return str(video_id)


def expiry_from_token_response(data: dict) -> datetime:
    expires_in = int(data.get("expires_in") or 3600)
    return utcnow() + timedelta(seconds=expires_in)


def scope_param() -> str:
    return " ".join(YOUTUBE_SCOPES)
