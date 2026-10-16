from app.config import get_settings
from app.database import SessionLocal
from app.models import AppSetting, utcnow

SETTING_KEYS = {
    "seedance_api_key",
    "seedance_base_url",
    "google_client_id",
    "google_client_secret",
}


async def get_value(key: str) -> str:
    if key not in SETTING_KEYS:
        raise KeyError(key)
    async with SessionLocal() as session:
        row = await session.get(AppSetting, key)
        if row and row.value.strip():
            return row.value.strip()
    fallback = getattr(get_settings(), key, "") or ""
    return fallback.strip()


async def set_value(key: str, value: str) -> None:
    if key not in SETTING_KEYS:
        raise KeyError(key)
    cleaned = value.strip()
    async with SessionLocal() as session:
        row = await session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=cleaned, updated_at=utcnow()))
        else:
            row.value = cleaned
            row.updated_at = utcnow()
        await session.commit()


async def clear_value(key: str) -> None:
    if key not in SETTING_KEYS:
        raise KeyError(key)
    async with SessionLocal() as session:
        row = await session.get(AppSetting, key)
        if row is not None:
            await session.delete(row)
            await session.commit()


async def public_settings() -> dict:
    base_url = await get_value("seedance_base_url")
    client_id = await get_value("google_client_id")
    api_key = await get_value("seedance_api_key")
    secret = await get_value("google_client_secret")
    settings = get_settings()
    return {
        "seedance_api_key_set": bool(api_key),
        "seedance_base_url": base_url or settings.seedance_base_url,
        "google_client_id": client_id,
        "google_client_secret_set": bool(secret),
        "redirect_uri": settings.redirect_uri,
        "frontend_url": settings.frontend_url,
    }


async def apply_settings_patch(payload: dict) -> None:
    if payload.get("clear_seedance_api_key"):
        await clear_value("seedance_api_key")
    elif payload.get("seedance_api_key"):
        await set_value("seedance_api_key", payload["seedance_api_key"])

    if payload.get("seedance_base_url"):
        url = payload["seedance_base_url"].strip().rstrip("/")
        if not url.startswith("https://"):
            raise ValueError("Base URL phải bắt đầu bằng https://")
        await set_value("seedance_base_url", url)

    if payload.get("clear_google_client_id"):
        await clear_value("google_client_id")
    elif payload.get("google_client_id"):
        await set_value("google_client_id", payload["google_client_id"])

    if payload.get("clear_google_client_secret"):
        await clear_value("google_client_secret")
    elif payload.get("google_client_secret"):
        await set_value("google_client_secret", payload["google_client_secret"])
