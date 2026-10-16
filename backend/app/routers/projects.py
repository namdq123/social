from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.models import Character, CharacterImage, Episode, Job, Project, Scene, utcnow
from app.schemas import CharacterImageOut, CharacterOut, MessageOut, ProjectIn, ProjectOut
from app.services.bible import remove_series_files, remove_sheet

router = APIRouter(tags=["projects"])

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}
ASPECTS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_CHARACTERS = 8
MAX_REFERENCES = 4
ACTIVE = {"submitting", "generating", "downloading", "uploading"}
STATUS_LABELS = {
    "queued": "Đang chờ",
    "generating": "Đang tạo",
    "ready": "Sẵn sàng",
    "approved": "Đã duyệt",
    "profile": "Chưa có ảnh",
    "failed": "Lỗi",
}


def _uuid_or_404(value: str, message: str) -> None:
    try:
        UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=message) from exc


def to_project_out(project: Project, character_count: int) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description or "",
        character_count=character_count,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def to_image_out(image: CharacterImage) -> CharacterImageOut:
    if image.slot == "front" and image.status == "ready":
        label = "Chờ duyệt"
    elif image.status == "approved":
        label = "Đã duyệt"
    else:
        label = STATUS_LABELS.get(image.status, image.status)
    return CharacterImageOut(
        id=image.id,
        character_id=image.character_id,
        label=image.label or "",
        slot=image.slot or "",
        prompt=image.prompt or "",
        source=image.source,
        status=image.status,
        status_label=label,
        error_message=image.error_message,
        has_image=bool(image.image_path and Path(image.image_path).is_file()),
    )


def to_character_out(character: Character, images: list[CharacterImage] | None = None) -> CharacterOut:
    rows = images or []
    ready = [item for item in rows if item.image_path and Path(item.image_path).is_file()]
    cover = bool(ready) or bool(character.image_path and Path(character.image_path).is_file())
    return CharacterOut(
        id=character.id,
        project_id=character.project_id,
        name=character.name,
        bible_key=character.bible_key,
        prompt=character.prompt or "",
        aspect_ratio=character.aspect_ratio or "1:1",
        source=character.source,
        status=character.status,
        status_label=STATUS_LABELS.get(character.status, character.status),
        error_message=character.error_message,
        has_image=cover,
        images=[to_image_out(item) for item in rows],
        created_at=character.created_at,
        updated_at=character.updated_at,
    )


async def get_project_or_404(session: AsyncSession, project_id: str) -> Project:
    _uuid_or_404(project_id, "Không tìm thấy dự án.")
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy dự án.")
    return project


def character_dir() -> Path:
    folder = Path(get_settings().data_dir) / "characters"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(session: AsyncSession = Depends(get_session)) -> list[ProjectOut]:
    projects = (await session.scalars(select(Project).order_by(Project.created_at.desc()))).all()
    counts = {
        project_id: count
        for project_id, count in (
            await session.execute(select(Character.project_id, func.count()).group_by(Character.project_id))
        ).all()
    }
    return [to_project_out(project, int(counts.get(project.id, 0))) for project in projects]


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(payload: ProjectIn, session: AsyncSession = Depends(get_session)) -> ProjectOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Tên dự án không được để trống.")
    now = utcnow()
    project = Project(
        id=str(uuid4()),
        name=name[:120],
        description=payload.description.strip()[:2000],
        created_at=now,
        updated_at=now,
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return to_project_out(project, 0)


@router.delete("/projects/{project_id}", response_model=MessageOut)
async def delete_project(project_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    project = await get_project_or_404(session, project_id)
    running = await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.project_id == project.id, Job.status.in_(ACTIVE))
    )
    if running:
        raise HTTPException(status_code=409, detail="Dự án còn video đang chạy.")
    jobs = (await session.scalars(select(Job).where(Job.project_id == project.id))).all()
    characters = (await session.scalars(select(Character).where(Character.project_id == project.id))).all()
    images = (await session.scalars(select(CharacterImage).where(CharacterImage.project_id == project.id))).all()
    episodes = (await session.scalars(select(Episode).where(Episode.project_id == project.id))).all()
    scenes = (await session.scalars(select(Scene).where(Scene.project_id == project.id))).all()
    files: set[str] = set()
    for job in jobs:
        for raw in (job.local_path, job.image_path):
            if raw:
                files.add(raw)
        await session.delete(job)
    for scene in scenes:
        if scene.image_path:
            files.add(scene.image_path)
        await session.delete(scene)
    for episode in episodes:
        await session.delete(episode)
    for image in images:
        if image.image_path:
            files.add(image.image_path)
        await session.delete(image)
    slugs = [character.sheet_dir for character in characters if character.sheet_dir]
    for character in characters:
        if character.image_path:
            files.add(character.image_path)
        await session.delete(character)
    remove_series_files(project.id, slugs)
    for raw in files:
        Path(raw).unlink(missing_ok=True)
    await session.delete(project)
    await session.commit()
    return {"message": "Đã xóa dự án."}


@router.get("/projects/{project_id}/characters", response_model=list[CharacterOut])
async def list_characters(project_id: str, session: AsyncSession = Depends(get_session)) -> list[CharacterOut]:
    project = await get_project_or_404(session, project_id)
    rows = (
        await session.scalars(
            select(Character).where(Character.project_id == project.id).order_by(Character.created_at.asc())
        )
    ).all()
    images = (
        await session.scalars(
            select(CharacterImage)
            .where(CharacterImage.project_id == project.id)
            .order_by(CharacterImage.sort_order.asc(), CharacterImage.created_at.asc())
        )
    ).all()
    grouped: dict[str, list[CharacterImage]] = {}
    for image in images:
        grouped.setdefault(image.character_id, []).append(image)
    changed = False
    for character in rows:
        if character.id in grouped or not character.image_path or not Path(character.image_path).is_file():
            continue
        image = CharacterImage(
            id=str(uuid4()),
            character_id=character.id,
            project_id=character.project_id,
            label="Ảnh chính",
            prompt=character.prompt or "",
            aspect_ratio=character.aspect_ratio or "1:1",
            source=character.source or "upload",
            image_path=character.image_path,
            status="ready",
            sort_order=0,
            created_at=character.created_at,
            updated_at=utcnow(),
        )
        session.add(image)
        grouped[character.id] = [image]
        changed = True
    if changed:
        await session.commit()
    return [to_character_out(row, grouped.get(row.id, [])) for row in rows]


@router.post("/projects/{project_id}/characters", response_model=CharacterOut, status_code=201)
async def create_character(
    project_id: str,
    name: str = Form(...),
    prompt: str = Form(""),
    aspect_ratio: str = Form("1:1"),
    image: UploadFile | None = File(None),
    session: AsyncSession = Depends(get_session),
) -> CharacterOut:
    project = await get_project_or_404(session, project_id)
    cleaned_name = " ".join(name.split())
    if not cleaned_name or len(cleaned_name) > 80:
        raise HTTPException(status_code=400, detail="Tên nhân vật phải từ 1 đến 80 ký tự.")
    cleaned_prompt = prompt.strip()
    if len(cleaned_prompt) > 2000:
        raise HTTPException(status_code=400, detail="Mô tả nhân vật tối đa 2000 ký tự.")
    if aspect_ratio not in ASPECTS:
        raise HTTPException(status_code=400, detail="Tỷ lệ ảnh không được hỗ trợ.")

    count = await session.scalar(
        select(func.count()).select_from(Character).where(Character.project_id == project.id)
    )
    if int(count or 0) >= MAX_CHARACTERS:
        raise HTTPException(status_code=400, detail="Mỗi dự án tối đa 8 nhân vật.")

    character_id = str(uuid4())
    image_path = None
    source = "openrouter"
    status = "queued"
    if image is not None and image.filename:
        suffix = Path(image.filename).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise HTTPException(status_code=400, detail="Ảnh phải là jpg, png, webp, gif hoặc bmp.")
        payload = await image.read()
        if not payload:
            raise HTTPException(status_code=400, detail="File ảnh rỗng.")
        if len(payload) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="Ảnh nhân vật phải nhỏ hơn 4MB.")
        image_path = character_dir() / f"{character_id}{suffix}"
        image_path.write_bytes(payload)
        source = "upload"
        status = "ready"
    elif not cleaned_prompt:
        raise HTTPException(status_code=400, detail="Hãy tải ảnh lên hoặc nhập mô tả để tạo ảnh.")

    now = utcnow()
    character = Character(
        id=character_id,
        project_id=project.id,
        name=cleaned_name,
        prompt=cleaned_prompt,
        aspect_ratio=aspect_ratio,
        source=source,
        image_path=str(image_path) if image_path else None,
        status=status,
        created_at=now,
        updated_at=now,
    )
    session.add(character)
    if image_path is not None:
        session.add(
            CharacterImage(
                id=str(uuid4()),
                character_id=character.id,
                project_id=project.id,
                label="Ảnh chính",
                prompt=cleaned_prompt,
                aspect_ratio=aspect_ratio,
                source="upload",
                image_path=str(image_path),
                status="ready",
                sort_order=0,
                created_at=now,
                updated_at=now,
            )
        )
    project.updated_at = now
    await session.commit()
    await session.refresh(character)
    images = (
        await session.scalars(select(CharacterImage).where(CharacterImage.character_id == character.id))
    ).all()
    return to_character_out(character, list(images))


@router.delete("/projects/{project_id}/characters/{character_id}", response_model=MessageOut)
async def delete_character(
    project_id: str,
    character_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    await get_project_or_404(session, project_id)
    _uuid_or_404(character_id, "Không tìm thấy nhân vật.")
    character = await session.get(Character, character_id)
    if character is None or character.project_id != project_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhân vật.")
    images = (
        await session.scalars(select(CharacterImage).where(CharacterImage.character_id == character.id))
    ).all()
    files = {character.image_path} if character.image_path else set()
    for image in images:
        if image.image_path:
            files.add(image.image_path)
        await session.delete(image)
    for raw in files:
        Path(raw).unlink(missing_ok=True)
    remove_sheet(character.sheet_dir)
    await session.delete(character)
    await session.commit()
    return {"message": "Đã xóa nhân vật."}


@router.get("/projects/{project_id}/characters/{character_id}/image")
async def character_image(
    project_id: str,
    character_id: str,
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    await get_project_or_404(session, project_id)
    _uuid_or_404(character_id, "Không tìm thấy nhân vật.")
    character = await session.get(Character, character_id)
    if character is None or character.project_id != project_id or not character.image_path:
        raise HTTPException(status_code=404, detail="Nhân vật chưa có ảnh.")
    path = Path(character.image_path).resolve()
    root = Path(get_settings().data_dir).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy file ảnh.")
    return FileResponse(path, media_type=IMAGE_TYPES.get(path.suffix.lower(), "image/png"))


@router.post("/projects/{project_id}/characters/{character_id}/images", response_model=CharacterOut, status_code=201)
async def add_character_image(
    project_id: str,
    character_id: str,
    prompt: str = Form(""),
    label: str = Form(""),
    aspect_ratio: str = Form("1:1"),
    image: UploadFile | None = File(None),
    session: AsyncSession = Depends(get_session),
) -> CharacterOut:
    await get_project_or_404(session, project_id)
    _uuid_or_404(character_id, "Không tìm thấy nhân vật.")
    character = await session.get(Character, character_id)
    if character is None or character.project_id != project_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhân vật.")
    if aspect_ratio not in ASPECTS:
        raise HTTPException(status_code=400, detail="Tỷ lệ ảnh không được hỗ trợ.")
    existing = (
        await session.scalars(select(CharacterImage).where(CharacterImage.character_id == character.id))
    ).all()
    if len(existing) >= MAX_REFERENCES:
        raise HTTPException(status_code=400, detail="Mỗi nhân vật tối đa 4 ảnh tham chiếu.")
    cleaned_prompt = prompt.strip()
    if len(cleaned_prompt) > 2000:
        raise HTTPException(status_code=400, detail="Mô tả ảnh tối đa 2000 ký tự.")
    image_id = str(uuid4())
    image_path = None
    source = "openrouter"
    status = "queued"
    if image is not None and image.filename:
        suffix = Path(image.filename).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise HTTPException(status_code=400, detail="Ảnh phải là jpg, png, webp, gif hoặc bmp.")
        payload = await image.read()
        if not payload:
            raise HTTPException(status_code=400, detail="File ảnh rỗng.")
        if len(payload) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="Ảnh nhân vật phải nhỏ hơn 4MB.")
        image_path = character_dir() / f"{image_id}{suffix}"
        image_path.write_bytes(payload)
        source = "upload"
        status = "ready"
    elif not cleaned_prompt and not (character.prompt or "").strip():
        raise HTTPException(status_code=400, detail="Hãy tải ảnh lên hoặc nhập mô tả góc cần vẽ.")
    now = utcnow()
    row = CharacterImage(
        id=image_id,
        character_id=character.id,
        project_id=character.project_id,
        label=(label.strip() or "Ảnh thêm")[:80],
        prompt=cleaned_prompt or character.prompt or "",
        aspect_ratio=aspect_ratio,
        source=source,
        image_path=str(image_path) if image_path else None,
        status=status,
        sort_order=len(existing),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    if image_path is not None and not character.image_path:
        character.image_path = str(image_path)
        character.status = "ready"
    await session.commit()
    await session.refresh(character)
    images = (
        await session.scalars(
            select(CharacterImage)
            .where(CharacterImage.character_id == character.id)
            .order_by(CharacterImage.sort_order.asc())
        )
    ).all()
    return to_character_out(character, list(images))


@router.delete("/projects/{project_id}/characters/{character_id}/images/{image_id}", response_model=MessageOut)
async def delete_character_image(
    project_id: str,
    character_id: str,
    image_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    await get_project_or_404(session, project_id)
    _uuid_or_404(character_id, "Không tìm thấy nhân vật.")
    _uuid_or_404(image_id, "Không tìm thấy ảnh.")
    image = await session.get(CharacterImage, image_id)
    if image is None or image.character_id != character_id or image.project_id != project_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy ảnh.")
    character = await session.get(Character, character_id)
    siblings = (
        await session.scalars(
            select(CharacterImage).where(
                CharacterImage.character_id == character_id,
                CharacterImage.id != image.id,
            )
        )
    ).all()
    shared = any(item.image_path == image.image_path for item in siblings)
    if image.image_path and not shared:
        Path(image.image_path).unlink(missing_ok=True)
    if character is not None and image.image_path and character.image_path == image.image_path:
        replacement = next((item for item in siblings if item.image_path and item.status == "ready"), None)
        character.image_path = replacement.image_path if replacement else None
    await session.delete(image)
    await session.commit()
    return {"message": "Đã xóa ảnh tham chiếu."}


@router.get("/projects/{project_id}/characters/{character_id}/images/{image_id}")
async def character_reference_image(
    project_id: str,
    character_id: str,
    image_id: str,
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    await get_project_or_404(session, project_id)
    image = await session.get(CharacterImage, image_id)
    if image is None or image.character_id != character_id or image.project_id != project_id or not image.image_path:
        raise HTTPException(status_code=404, detail="Không tìm thấy ảnh.")
    path = Path(image.image_path).resolve()
    root = Path(get_settings().data_dir).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy file ảnh.")
    return FileResponse(path, media_type=IMAGE_TYPES.get(path.suffix.lower(), "image/png"))
