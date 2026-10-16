from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.constants import (
    CATEGORIES,
    CONTINUITY_RULES,
    MODEL_PRESETS,
    RATIOS,
    RESOLUTIONS,
    STATUS_LABELS,
    TEXT_MODEL_PRESETS,
)
from app.schemas import MetaOut, SeedanceTestOut, SettingsIn, SettingsOut
from app.services.seedance import SeedanceError, probe_api_key
from app.services.settings_store import apply_settings_patch, get_value, public_settings

router = APIRouter(tags=["settings"])


@router.get("/meta", response_model=MetaOut)
async def meta() -> dict:
    settings = get_settings()
    return {
        "models": MODEL_PRESETS,
        "text_models": TEXT_MODEL_PRESETS,
        "continuity_rules": CONTINUITY_RULES,
        "ratios": RATIOS,
        "resolutions": RESOLUTIONS,
        "categories": CATEGORIES,
        "privacy": [
            {"id": "unlisted", "label": "Không công khai"},
            {"id": "private", "label": "Riêng tư"},
            {"id": "public", "label": "Công khai"},
        ],
        "redirect_uri": settings.redirect_uri,
        "status_labels": STATUS_LABELS,
    }


@router.get("/settings", response_model=SettingsOut)
async def read_settings() -> dict:
    return await public_settings()


@router.put("/settings", response_model=SettingsOut)
async def update_settings(payload: SettingsIn) -> dict:
    try:
        await apply_settings_patch(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await public_settings()


@router.post("/settings/test-seedance", response_model=SeedanceTestOut)
async def test_seedance() -> dict:
    api_key = await get_value("seedance_api_key")
    base_url = await get_value("seedance_base_url")
    if not api_key:
        raise HTTPException(status_code=400, detail="Chưa có OpenRouter API key.")
    try:
        message = await probe_api_key(api_key, base_url)
    except SeedanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": message}
