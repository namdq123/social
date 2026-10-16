import secrets
import time
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.database import SessionLocal
from app.models import YoutubeAccount, utcnow
from app.schemas import MessageOut, YoutubeStatusOut
from app.services.settings_store import get_value
from app.services.youtube import (
    AUTH_URL,
    YoutubeError,
    exchange_code,
    expiry_from_token_response,
    fetch_channel,
    scope_param,
)

router = APIRouter(prefix="/youtube", tags=["youtube"])

_states: dict[str, float] = {}


def _purge_states() -> None:
    now = time.time()
    expired = [key for key, expiry in _states.items() if expiry < now]
    for key in expired:
        _states.pop(key, None)


def _new_state() -> str:
    _purge_states()
    state = secrets.token_urlsafe(24)
    _states[state] = time.time() + 600
    return state


def _consume_state(state: str) -> bool:
    _purge_states()
    expiry = _states.pop(state, None)
    return expiry is not None and expiry >= time.time()


def _frontend_redirect(query: str) -> RedirectResponse:
    frontend = get_settings().frontend_url.rstrip("/")
    return RedirectResponse(f"{frontend}/?{query}")


@router.get("/status", response_model=YoutubeStatusOut)
async def youtube_status() -> dict:
    async with SessionLocal() as session:
        account = await session.get(YoutubeAccount, 1)
        if account is None:
            return {"connected": False}
        return {
            "connected": True,
            "channel_id": account.channel_id,
            "channel_title": account.channel_title,
        }


@router.get("/connect")
async def youtube_connect() -> RedirectResponse:
    client_id = await get_value("google_client_id")
    client_secret = await get_value("google_client_secret")
    if not client_id or not client_secret:
        return _frontend_redirect(
            f"youtube=error&message={quote('Chưa cấu hình Google OAuth client trong Cài đặt.')}"
        )
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": get_settings().redirect_uri,
            "response_type": "code",
            "scope": scope_param(),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": _new_state(),
        }
    )
    return RedirectResponse(f"{AUTH_URL}?{query}")


@router.get("/callback")
async def youtube_callback(request: Request) -> RedirectResponse:
    error = request.query_params.get("error")
    if error:
        return _frontend_redirect(f"youtube=error&message={quote(error)}")
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state or not _consume_state(state):
        return _frontend_redirect(
            f"youtube=error&message={quote('Phiên đăng nhập không hợp lệ hoặc đã hết hạn.')}"
        )

    client_id = await get_value("google_client_id")
    client_secret = await get_value("google_client_secret")
    try:
        tokens = await exchange_code(
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=get_settings().redirect_uri,
        )
        channel_id, channel_title = await fetch_channel(tokens["access_token"])
    except YoutubeError as exc:
        return _frontend_redirect(f"youtube=error&message={quote(str(exc))}")

    async with SessionLocal() as session:
        account = await session.get(YoutubeAccount, 1)
        if account is None:
            account = YoutubeAccount(id=1)
            session.add(account)
        account.channel_id = channel_id
        account.channel_title = channel_title
        account.access_token = tokens["access_token"]
        account.refresh_token = tokens["refresh_token"]
        account.expires_at = expiry_from_token_response(tokens)
        account.updated_at = utcnow()
        await session.commit()
    return _frontend_redirect("youtube=connected")


@router.delete("/account", response_model=MessageOut)
async def youtube_disconnect() -> dict:
    async with SessionLocal() as session:
        account = await session.get(YoutubeAccount, 1)
        if account is not None:
            await session.delete(account)
            await session.commit()
    return {"message": "Đã ngắt kết nối YouTube."}
