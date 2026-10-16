import json
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Character, CharacterImage, Project, utcnow
from app.routers.projects import get_project_or_404, to_character_out
from app.schemas import BibleIn, BibleOut, CharacterOut, StoryIn, StoryOut
from app.services.bible import (
    BIBLE_PROMPT,
    SHEET_SLOTS,
    SLOT_ASPECT,
    SLOT_LABELS,
    TEXT_MODEL,
    BibleError,
    angle_prompt,
    approved_bible,
    card_prompt,
    find_person,
    front_prompt,
    normalize_bible,
    parse_json_text,
    remove_sheet,
    sheet_slug,
    write_bible_file,
    write_profile,
)
from app.services.seedance import SeedanceError, complete_json
from app.services.settings_store import get_value
from app.services.story import dump_state, empty_state, story_document

router = APIRouter(tags=["series"])


def _bible_out(project: Project) -> BibleOut:
    payload = None
    if project.bible_json:
        try:
            parsed = json.loads(project.bible_json)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    return BibleOut(
        status=project.bible_status or "",
        model=TEXT_MODEL,
        prompt=BIBLE_PROMPT,
        bible=payload,
    )


def _story_out(project: Project) -> StoryOut:
    state = story_document(project)
    return StoryOut(
        current_state=state["current_state"],
        used_topics=state["used_topics"],
        episodes=state["episodes"],
    )


async def _character_response(session: AsyncSession, character: Character) -> CharacterOut:
    images = (
        await session.scalars(
            select(CharacterImage)
            .where(CharacterImage.character_id == character.id)
            .order_by(CharacterImage.sort_order.asc(), CharacterImage.created_at.asc())
        )
    ).all()
    return to_character_out(character, list(images))


@router.get("/projects/{project_id}/bible", response_model=BibleOut)
async def get_bible(project_id: str, session: AsyncSession = Depends(get_session)) -> BibleOut:
    project = await get_project_or_404(session, project_id)
    return _bible_out(project)


@router.post("/projects/{project_id}/bible/generate", response_model=BibleOut)
async def generate_bible(project_id: str, session: AsyncSession = Depends(get_session)) -> BibleOut:
    project = await get_project_or_404(session, project_id)
    if project.bible_status == "approved":
        raise HTTPException(status_code=409, detail="Hồ sơ đã khóa. Mở khóa trước khi tạo lại.")
    api_key = await get_value("seedance_api_key")
    base_url = await get_value("seedance_base_url")
    if not api_key:
        raise HTTPException(status_code=400, detail="Chưa cấu hình OpenRouter API key.")
    try:
        raw = await complete_json(
            api_key,
            base_url,
            "Bạn chỉ trả về một JSON object hợp lệ. Không markdown, không giải thích.",
            BIBLE_PROMPT,
            TEXT_MODEL,
            temperature=0.8,
        )
        bible = normalize_bible(raw)
    except BibleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SeedanceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Model không trả về JSON hợp lệ.") from exc
    _store_bible(project, bible, "draft")
    await session.commit()
    await session.refresh(project)
    return _bible_out(project)


@router.put("/projects/{project_id}/bible", response_model=BibleOut)
async def save_bible(
    project_id: str,
    payload: BibleIn,
    session: AsyncSession = Depends(get_session),
) -> BibleOut:
    project = await get_project_or_404(session, project_id)
    if project.bible_status == "approved":
        raise HTTPException(status_code=409, detail="Hồ sơ đã khóa. Mở khóa trước khi sửa.")
    try:
        bible = normalize_bible(payload.bible)
    except BibleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _store_bible(project, bible, "draft")
    await session.commit()
    await session.refresh(project)
    return _bible_out(project)


@router.post("/projects/{project_id}/bible/approve", response_model=BibleOut)
async def approve_bible(project_id: str, session: AsyncSession = Depends(get_session)) -> BibleOut:
    project = await get_project_or_404(session, project_id)
    if not project.bible_json:
        raise HTTPException(status_code=400, detail="Chưa có hồ sơ để duyệt.")
    try:
        bible = normalize_bible(parse_json_text(project.bible_json))
    except (BibleError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Hồ sơ không hợp lệ.") from exc
    try:
        await _sync_characters(session, project, bible)
    except BibleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _store_bible(project, bible, "approved")
    if not project.story_state:
        project.story_state = dump_state(empty_state())
    await session.commit()
    await session.refresh(project)
    return _bible_out(project)


@router.post("/projects/{project_id}/bible/unlock", response_model=BibleOut)
async def unlock_bible(project_id: str, session: AsyncSession = Depends(get_session)) -> BibleOut:
    project = await get_project_or_404(session, project_id)
    if project.bible_json:
        project.bible_status = "draft"
        project.updated_at = utcnow()
        await session.commit()
        await session.refresh(project)
    return _bible_out(project)


@router.get("/projects/{project_id}/story", response_model=StoryOut)
async def get_story(project_id: str, session: AsyncSession = Depends(get_session)) -> StoryOut:
    project = await get_project_or_404(session, project_id)
    return _story_out(project)


@router.put("/projects/{project_id}/story", response_model=StoryOut)
async def save_story(
    project_id: str,
    payload: StoryIn,
    session: AsyncSession = Depends(get_session),
) -> StoryOut:
    project = await get_project_or_404(session, project_id)
    current = story_document(project)
    topics = []
    for item in payload.used_topics[:200]:
        text = " ".join(str(item).split())
        if text and text not in topics:
            topics.append(text[:160])
    current["current_state"] = payload.current_state.strip()[:4000] or empty_state()["current_state"]
    current["used_topics"] = topics
    project.story_state = dump_state(current)
    project.updated_at = utcnow()
    await session.commit()
    await session.refresh(project)
    return _story_out(project)


@router.post("/projects/{project_id}/characters/{character_id}/sheet/{slot}", response_model=CharacterOut)
async def queue_sheet(
    project_id: str,
    character_id: str,
    slot: str,
    session: AsyncSession = Depends(get_session),
) -> CharacterOut:
    if slot not in SHEET_SLOTS:
        raise HTTPException(status_code=404, detail="Không có góc ảnh này.")
    project = await get_project_or_404(session, project_id)
    character = await _bible_character(session, project, character_id)
    bible = approved_bible(project)
    person = find_person(bible, character.bible_key or "")
    if bible is None or person is None:
        raise HTTPException(status_code=400, detail="Hãy duyệt hồ sơ series trước khi vẽ ảnh chuẩn.")
    images = (
        await session.scalars(select(CharacterImage).where(CharacterImage.character_id == character.id))
    ).all()
    front = next((image for image in images if image.slot == "front"), None)
    row = front if slot == "front" else next((image for image in images if image.slot == slot), None)
    if row is not None and row.status == "generating":
        raise HTTPException(status_code=409, detail="Ảnh này đang được vẽ.")
    if slot != "front":
        if (
            front is None
            or front.status != "approved"
            or not front.image_path
            or not Path(front.image_path).is_file()
        ):
            raise HTTPException(
                status_code=400,
                detail="Hãy duyệt ảnh mặt trước trước. Góc khác phải dùng ảnh đó, không vẽ riêng từ văn bản.",
            )
        prompt = angle_prompt(slot, bible["visual_style"])
    else:
        prompt = front_prompt(person, bible["visual_style"])
        for image in list(images):
            if image.slot not in {"side", "back", "expressions"}:
                continue
            if image.image_path:
                Path(image.image_path).unlink(missing_ok=True)
            await session.delete(image)
        if front is not None and front.image_path:
            Path(front.image_path).unlink(missing_ok=True)
            if character.image_path == front.image_path:
                character.image_path = None
                character.status = "profile"
    now = utcnow()
    if row is None:
        row = CharacterImage(
            id=str(uuid4()),
            character_id=character.id,
            project_id=character.project_id,
            label=SLOT_LABELS[slot],
            slot=slot,
            prompt=prompt,
            aspect_ratio=SLOT_ASPECT[slot],
            source="openrouter",
            image_path=None,
            status="queued",
            sort_order=SHEET_SLOTS.index(slot),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.label = SLOT_LABELS[slot]
        row.slot = slot
        row.prompt = prompt
        row.aspect_ratio = SLOT_ASPECT[slot]
        row.source = "openrouter"
        row.image_path = None
        row.status = "queued"
        row.error_message = None
        row.sort_order = SHEET_SLOTS.index(slot)
        row.updated_at = now
    character.prompt = card_prompt(person)
    character.updated_at = now
    await session.commit()
    await session.refresh(character)
    return await _character_response(session, character)


@router.post(
    "/projects/{project_id}/characters/{character_id}/sheet/front/approve",
    response_model=CharacterOut,
)
async def approve_front(
    project_id: str,
    character_id: str,
    session: AsyncSession = Depends(get_session),
) -> CharacterOut:
    project = await get_project_or_404(session, project_id)
    character = await _bible_character(session, project, character_id)
    front = await session.scalar(
        select(CharacterImage).where(
            CharacterImage.character_id == character.id,
            CharacterImage.slot == "front",
        )
    )
    if front is None or front.status not in {"ready", "approved"} or not front.image_path:
        raise HTTPException(status_code=400, detail="Chưa có ảnh mặt trước để duyệt.")
    if not Path(front.image_path).is_file():
        raise HTTPException(status_code=400, detail="Không tìm thấy file ảnh mặt trước.")
    front.status = "approved"
    front.error_message = None
    front.updated_at = utcnow()
    character.image_path = front.image_path
    character.status = "ready"
    character.updated_at = utcnow()
    await session.commit()
    await session.refresh(character)
    return await _character_response(session, character)


def _store_bible(project: Project, bible: dict, status: str) -> None:
    project.bible_json = json.dumps(bible, ensure_ascii=False)
    project.bible_status = status
    project.updated_at = utcnow()
    write_bible_file(project.id, bible)


async def _bible_character(session: AsyncSession, project: Project, character_id: str) -> Character:
    character = await session.get(Character, character_id)
    if character is None or character.project_id != project.id:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhân vật.")
    if not character.bible_key:
        raise HTTPException(status_code=400, detail="Nhân vật này không thuộc hồ sơ đã khóa.")
    return character


async def _sync_characters(session: AsyncSession, project: Project, bible: dict) -> None:
    existing = (
        await session.scalars(select(Character).where(Character.project_id == project.id))
    ).all()
    by_key = {item.bible_key: item for item in existing if item.bible_key}
    by_name: dict[str, Character] = {}
    for item in existing:
        by_name.setdefault(item.name.casefold(), item)
    used_dirs = {item.sheet_dir for item in existing if item.sheet_dir}
    creating = 0
    for person in bible["characters"]:
        if by_key.get(person["id"]) is None and by_name.get(person["name"].casefold()) is None:
            creating += 1
    if len(existing) + creating > 8:
        raise BibleError("Mỗi dự án tối đa 8 nhân vật, không thêm được người từ hồ sơ.")
    now = utcnow()
    for person in bible["characters"]:
        character = by_key.get(person["id"]) or by_name.get(person["name"].casefold())
        slug = character.sheet_dir if character and character.sheet_dir else _unique_slug(person["id"], used_dirs)
        used_dirs.add(slug)
        if character is None:
            character = Character(
                id=str(uuid4()),
                project_id=project.id,
                name=person["name"],
                bible_key=person["id"],
                sheet_dir=slug,
                prompt=card_prompt(person),
                aspect_ratio="3:4",
                source="bible",
                image_path=None,
                status="profile",
                created_at=now,
                updated_at=now,
            )
            session.add(character)
        else:
            old_slug = character.sheet_dir
            character.name = person["name"]
            character.bible_key = person["id"]
            character.sheet_dir = slug
            character.prompt = card_prompt(person)
            character.updated_at = now
            if not character.status:
                character.status = "profile"
            if old_slug and old_slug != slug:
                remove_sheet(old_slug)
        write_profile(slug, person, bible["visual_style"])


def _unique_slug(character_id: str, used: set[str]) -> str:
    base = sheet_slug(character_id)
    slug = base
    number = 2
    while slug in used:
        slug = f"{base}-{number}"[:40]
        number += 1
    return slug
