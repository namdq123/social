import json
import re
import shutil
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import CATEGORIES, MODEL_PRESETS, PRIVACY, RATIOS, RESOLUTIONS
from app.database import get_session
from app.models import Episode, Job, Scene, utcnow
from app.routers.jobs import MODEL_PATTERN, duration_bounds, form_bool, make_title, to_job_out
from app.routers.projects import ASPECTS, IMAGE_SUFFIXES, IMAGE_TYPES, get_project_or_404
from app.schemas import EpisodeIn, EpisodeOut, EpisodePlanIn, EpisodeScriptIn, JobOut, MessageOut, SceneOut
from app.services.assemble import AssembleError, concat_videos
from app.services.bible import SCENE_MODEL, TEXT_MODEL, approved_bible, video_prompt
from app.services.cast import approved_fronts
from app.services.seedance import DEFAULT_MODEL, SeedanceError, complete_json
from app.services.settings_store import get_value
from app.services.story import (
    StoryError,
    approve_story,
    episode_user_prompt,
    plan_user_prompt,
    prepare_plan,
    recent_history,
    replace_episode_script,
    save_scripted_episode,
    story_document,
)
router = APIRouter(tags=["episodes"])

MAX_SCENES = 12
IMAGE_BYTES = 4 * 1024 * 1024
SCENE_LABELS = {
    "draft": "Chưa có ảnh",
    "queued": "Đang chờ",
    "generating": "Đang vẽ",
    "ready": "Đã có ảnh",
    "failed": "Lỗi",
}
ACTIVE = {"submitting", "generating", "downloading", "uploading"}


def scene_dir() -> Path:
    folder = Path(get_settings().data_dir) / "scenes"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def to_episode_out(episode: Episode, scene_count: int) -> EpisodeOut:
    return EpisodeOut(
        id=episode.id,
        project_id=episode.project_id,
        number=episode.number,
        title=episode.title,
        script=episode.script or "",
        summary=episode.summary or "",
        status=episode.status or "draft",
        scene_count=scene_count,
        created_at=episode.created_at,
        updated_at=episode.updated_at,
    )


def to_scene_out(scene: Scene) -> SceneOut:
    return SceneOut(
        id=scene.id,
        episode_id=scene.episode_id,
        project_id=scene.project_id,
        sort_order=scene.sort_order,
        duration_seconds=scene.duration_seconds or 0,
        prompt=scene.prompt,
        aspect_ratio=scene.aspect_ratio,
        status=scene.status,
        status_label=SCENE_LABELS.get(scene.status, scene.status),
        join=str((_scene_script(scene) or {}).get("join") or ""),
        cut_note=str((_scene_script(scene) or {}).get("cut_note") or ""),
        error_message=scene.error_message,
        has_image=bool(scene.image_path and Path(scene.image_path).is_file()),
        created_at=scene.created_at,
        updated_at=scene.updated_at,
    )


async def get_episode_or_404(session: AsyncSession, project_id: str, episode_id: str) -> Episode:
    try:
        UUID(episode_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy tập.") from exc
    episode = await session.get(Episode, episode_id)
    if episode is None or episode.project_id != project_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy tập.")
    return episode


def split_script(script: str) -> list[str]:
    chunks = [part.strip() for part in re.split(r"\n\s*\n", script) if part.strip()]
    if len(chunks) <= 1:
        chunks = [line.strip() for line in script.splitlines() if line.strip()]
    return chunks[:MAX_SCENES]


@router.get("/projects/{project_id}/episodes", response_model=list[EpisodeOut])
async def list_episodes(project_id: str, session: AsyncSession = Depends(get_session)) -> list[EpisodeOut]:
    project = await get_project_or_404(session, project_id)
    episodes = (
        await session.scalars(
            select(Episode).where(Episode.project_id == project.id).order_by(Episode.number.asc(), Episode.created_at.asc())
        )
    ).all()
    counts = {
        episode_id: count
        for episode_id, count in (
            await session.execute(select(Scene.episode_id, func.count()).group_by(Scene.episode_id))
        ).all()
    }
    return [to_episode_out(episode, int(counts.get(episode.id, 0))) for episode in episodes]


@router.post("/projects/{project_id}/episodes", response_model=EpisodeOut, status_code=201)
async def create_episode(
    project_id: str,
    payload: EpisodeIn,
    session: AsyncSession = Depends(get_session),
) -> EpisodeOut:
    project = await get_project_or_404(session, project_id)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Tập phim cần có tiêu đề.")
    now = utcnow()
    episode = Episode(
        id=str(uuid4()),
        project_id=project.id,
        number=payload.number,
        title=title[:160],
        script=payload.script.strip()[:20000],
        created_at=now,
        updated_at=now,
    )
    session.add(episode)
    await session.commit()
    await session.refresh(episode)
    return to_episode_out(episode, 0)


@router.post("/projects/{project_id}/episodes/write", response_model=EpisodeOut, status_code=201)
async def write_episode(project_id: str, session: AsyncSession = Depends(get_session)) -> EpisodeOut:
    project = await get_project_or_404(session, project_id)
    bible = approved_bible(project)
    if bible is None:
        raise HTTPException(status_code=400, detail="Hãy duyệt hồ sơ series trước khi viết tập.")
    api_key = await get_value("seedance_api_key")
    base_url = await get_value("seedance_base_url")
    if not api_key:
        raise HTTPException(status_code=400, detail="Chưa cấu hình OpenRouter API key.")
    history = await recent_history(session, project.id)
    try:
        raw = await complete_json(
            api_key,
            base_url,
            "Bạn viết kịch bản tập phim. Chỉ trả về một JSON object. Không thiết kế lại nhân vật. Không markdown.",
            episode_user_prompt(bible, story_document(project), history),
            TEXT_MODEL,
            temperature=0.4,
        )
        episode = await save_scripted_episode(session, project, raw)
    except StoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SeedanceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return to_episode_out(episode, len(json.loads(episode.script_json)["scenes"]))


@router.post("/projects/{project_id}/episodes/plan", response_model=EpisodeOut, status_code=201)
async def plan_episode(
    project_id: str,
    payload: EpisodePlanIn,
    session: AsyncSession = Depends(get_session),
) -> EpisodeOut:
    project = await get_project_or_404(session, project_id)
    bible = approved_bible(project)
    if bible is None:
        raise HTTPException(status_code=400, detail="Hãy duyệt hồ sơ series trước khi tạo tập.")
    api_key = await get_value("seedance_api_key")
    base_url = await get_value("seedance_base_url")
    if not api_key:
        raise HTTPException(status_code=400, detail="Chưa cấu hình OpenRouter API key.")
    history = await recent_history(session, project.id)
    system = (
        "Bạn tách một prompt chung thành prompt video cho từng cảnh. "
        "Chỉ trả về một JSON object. Không thiết kế lại nhân vật. Không markdown."
    )
    user = plan_user_prompt(
        bible,
        story_document(project),
        history,
        payload.prompt,
        payload.scene_count,
        payload.continuity,
    )
    chosen = payload.model.strip()
    if chosen == "custom":
        model_id = payload.custom_model.strip()
    else:
        model_id = chosen or SCENE_MODEL
    if not MODEL_PATTERN.match(model_id):
        raise HTTPException(status_code=400, detail="Model ID không hợp lệ.")
    try:
        raw = await complete_json(api_key, base_url, system, user, model_id, temperature=0.4)
        try:
            prepared = prepare_plan(raw, payload.scene_count, payload.prompt)
        except StoryError as exc:
            raw = await complete_json(
                api_key,
                base_url,
                system,
                user + f"\n\nSửa lỗi: {exc} Trả về đúng {payload.scene_count} cảnh.",
                model_id,
                temperature=0.2,
            )
            prepared = prepare_plan(raw, payload.scene_count, payload.prompt)
        episode = await save_scripted_episode(session, project, prepared)
    except StoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SeedanceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return to_episode_out(episode, len(json.loads(episode.script_json)["scenes"]))


@router.post("/projects/{project_id}/episodes/import", response_model=EpisodeOut, status_code=201)
async def import_episode(
    project_id: str,
    payload: EpisodeScriptIn,
    session: AsyncSession = Depends(get_session),
) -> EpisodeOut:
    project = await get_project_or_404(session, project_id)
    try:
        episode = await save_scripted_episode(session, project, payload.episode)
    except StoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    count = len(json.loads(episode.script_json)["scenes"])
    return to_episode_out(episode, count)


@router.put("/projects/{project_id}/episodes/{episode_id}/script", response_model=EpisodeOut)
async def update_episode_script(
    project_id: str,
    episode_id: str,
    payload: EpisodeScriptIn,
    session: AsyncSession = Depends(get_session),
) -> EpisodeOut:
    project = await get_project_or_404(session, project_id)
    episode = await get_episode_or_404(session, project_id, episode_id)
    try:
        episode = await replace_episode_script(session, project, episode, payload.episode)
    except StoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    count = len(json.loads(episode.script_json)["scenes"])
    return to_episode_out(episode, count)


@router.post("/projects/{project_id}/episodes/{episode_id}/approve", response_model=EpisodeOut)
async def approve_episode(
    project_id: str,
    episode_id: str,
    session: AsyncSession = Depends(get_session),
) -> EpisodeOut:
    project = await get_project_or_404(session, project_id)
    episode = await get_episode_or_404(session, project_id, episode_id)
    try:
        approve_story(project, episode)
    except StoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    await session.refresh(episode)
    count = await session.scalar(select(func.count()).select_from(Scene).where(Scene.episode_id == episode.id))
    return to_episode_out(episode, int(count or 0))


@router.post("/projects/{project_id}/episodes/{episode_id}/assemble", response_model=JobOut, status_code=201)
async def assemble_episode(
    project_id: str,
    episode_id: str,
    session: AsyncSession = Depends(get_session),
) -> JobOut:
    await get_project_or_404(session, project_id)
    episode = await get_episode_or_404(session, project_id, episode_id)
    scenes = (
        await session.scalars(
            select(Scene).where(Scene.episode_id == episode.id).order_by(Scene.sort_order.asc(), Scene.created_at.asc())
        )
    ).all()
    if len(scenes) < 2:
        raise HTTPException(status_code=400, detail="Cần ít nhất hai cảnh đã có video để ghép.")
    scene_ids = [scene.id for scene in scenes]
    running = await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.scene_id.in_(scene_ids), Job.status.in_(ACTIVE | {"queued"}))
    )
    if running:
        raise HTTPException(status_code=409, detail="Còn video cảnh đang tạo. Đợi xong rồi ghép.")
    ratios = {scene.aspect_ratio for scene in scenes}
    if len(ratios) != 1:
        raise HTTPException(status_code=400, detail="Các cảnh phải cùng tỷ lệ thì mới ghép được.")
    clips: list[Path] = []
    total = 0
    model = DEFAULT_MODEL
    resolution = "768p"
    for scene in scenes:
        job = await session.scalar(
            select(Job)
            .where(
                Job.scene_id == scene.id,
                Job.status.in_(("generated", "published", "upload_failed")),
            )
            .order_by(Job.created_at.desc())
        )
        path = Path(job.local_path) if job and job.local_path else None
        if path is None or not path.is_file():
            raise HTTPException(status_code=400, detail=f"Cảnh {scene.sort_order} chưa có video.")
        clips.append(path)
        total += int(scene.duration_seconds or job.duration or 0)
        model = job.model
        resolution = job.resolution
    job_id = str(uuid4())
    destination = Path(get_settings().data_dir) / "videos" / f"{job_id}.mp4"
    try:
        await concat_videos(clips, destination)
    except AssembleError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    now = utcnow()
    merged = Job(
        id=job_id,
        project_id=project_id,
        prompt=f"Tập ghép: {episode.title}. {len(clips)} cảnh nối theo thứ tự.",
        model=model,
        resolution=resolution,
        ratio=scenes[0].aspect_ratio,
        duration=total,
        generate_audio=True,
        watermark=False,
        local_path=str(destination),
        youtube_title=episode.title[:100],
        youtube_description=(episode.summary or episode.title)[:5000],
        youtube_privacy="unlisted",
        youtube_category_id="22",
        auto_publish=False,
        status="generated",
        created_at=now,
        updated_at=now,
    )
    session.add(merged)
    await session.commit()
    await session.refresh(merged)
    return to_job_out(merged)


@router.delete("/projects/{project_id}/episodes/{episode_id}", response_model=MessageOut)
async def delete_episode(
    project_id: str,
    episode_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    episode = await get_episode_or_404(session, project_id, episode_id)
    scenes = (await session.scalars(select(Scene).where(Scene.episode_id == episode.id))).all()
    scene_ids = [scene.id for scene in scenes]
    if scene_ids:
        running = await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.scene_id.in_(scene_ids), Job.status.in_(ACTIVE))
        )
        if running:
            raise HTTPException(status_code=409, detail="Tập này còn video đang chạy.")
    for scene in scenes:
        if scene.image_path:
            Path(scene.image_path).unlink(missing_ok=True)
        await session.delete(scene)
    await session.delete(episode)
    await session.commit()
    return {"message": "Đã xóa tập."}


@router.get("/projects/{project_id}/episodes/{episode_id}/scenes", response_model=list[SceneOut])
async def list_scenes(
    project_id: str,
    episode_id: str,
    session: AsyncSession = Depends(get_session),
) -> list[SceneOut]:
    episode = await get_episode_or_404(session, project_id, episode_id)
    rows = (
        await session.scalars(
            select(Scene).where(Scene.episode_id == episode.id).order_by(Scene.sort_order.asc(), Scene.created_at.asc())
        )
    ).all()
    return [to_scene_out(row) for row in rows]


@router.post("/projects/{project_id}/episodes/{episode_id}/scenes", response_model=SceneOut, status_code=201)
async def create_scene(
    project_id: str,
    episode_id: str,
    prompt: str = Form(...),
    aspect_ratio: str = Form("16:9"),
    generate: str = Form("false"),
    image: UploadFile | None = File(None),
    session: AsyncSession = Depends(get_session),
) -> SceneOut:
    episode = await get_episode_or_404(session, project_id, episode_id)
    text = prompt.strip()
    if not text or len(text) > 2000:
        raise HTTPException(status_code=400, detail="Mô tả cảnh phải từ 1 đến 2000 ký tự.")
    if aspect_ratio not in ASPECTS:
        raise HTTPException(status_code=400, detail="Tỷ lệ ảnh không được hỗ trợ.")
    count = await session.scalar(select(func.count()).select_from(Scene).where(Scene.episode_id == episode.id))
    if int(count or 0) >= MAX_SCENES:
        raise HTTPException(status_code=400, detail="Mỗi tập tối đa 12 cảnh.")
    sort_order = int(count or 0) + 1
    scene_id = str(uuid4())
    image_path = None
    status = "draft"
    if image is not None and image.filename:
        suffix = Path(image.filename).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise HTTPException(status_code=400, detail="Ảnh phải là jpg, png, webp, gif hoặc bmp.")
        payload = await image.read()
        if not payload:
            raise HTTPException(status_code=400, detail="File ảnh rỗng.")
        if len(payload) > IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="Ảnh cảnh phải nhỏ hơn 4MB.")
        image_path = scene_dir() / f"{scene_id}{suffix}"
        image_path.write_bytes(payload)
        status = "ready"
    elif form_bool(generate, False):
        status = "queued"
    now = utcnow()
    scene = Scene(
        id=scene_id,
        episode_id=episode.id,
        project_id=episode.project_id,
        sort_order=sort_order,
        prompt=text,
        aspect_ratio=aspect_ratio,
        image_path=str(image_path) if image_path else None,
        status=status,
        created_at=now,
        updated_at=now,
    )
    session.add(scene)
    await session.commit()
    await session.refresh(scene)
    return to_scene_out(scene)


@router.post("/projects/{project_id}/episodes/{episode_id}/split", response_model=list[SceneOut])
async def split_episode(
    project_id: str,
    episode_id: str,
    session: AsyncSession = Depends(get_session),
) -> list[SceneOut]:
    episode = await get_episode_or_404(session, project_id, episode_id)
    parts = split_script(episode.script or "")
    if not parts:
        raise HTTPException(status_code=400, detail="Kịch bản đang trống.")
    existing = int(
        await session.scalar(select(func.count()).select_from(Scene).where(Scene.episode_id == episode.id)) or 0
    )
    room = MAX_SCENES - existing
    if room <= 0:
        raise HTTPException(status_code=400, detail="Tập này đã đủ 12 cảnh.")
    now = utcnow()
    created = []
    for offset, text in enumerate(parts[:room], start=1):
        scene = Scene(
            id=str(uuid4()),
            episode_id=episode.id,
            project_id=episode.project_id,
            sort_order=existing + offset,
            prompt=text[:2000],
            aspect_ratio="16:9",
            status="draft",
            created_at=now,
            updated_at=now,
        )
        session.add(scene)
        created.append(scene)
    await session.commit()
    for scene in created:
        await session.refresh(scene)
    return [to_scene_out(scene) for scene in created]


@router.post("/projects/{project_id}/scenes/{scene_id}/generate", response_model=SceneOut)
async def queue_scene_image(
    project_id: str,
    scene_id: str,
    session: AsyncSession = Depends(get_session),
) -> SceneOut:
    await get_project_or_404(session, project_id)
    try:
        UUID(scene_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy cảnh.") from exc
    scene = await session.get(Scene, scene_id)
    if scene is None or scene.project_id != project_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy cảnh.")
    if scene.script_json:
        await _require_scene_references(session, project_id, scene.script_json)
    if scene.status in {"queued", "generating"}:
        return to_scene_out(scene)
    scene.status = "queued"
    scene.error_message = None
    scene.updated_at = utcnow()
    await session.commit()
    await session.refresh(scene)
    return to_scene_out(scene)


@router.delete("/projects/{project_id}/scenes/{scene_id}", response_model=MessageOut)
async def delete_scene(project_id: str, scene_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    await get_project_or_404(session, project_id)
    try:
        UUID(scene_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy cảnh.") from exc
    scene = await session.get(Scene, scene_id)
    if scene is None or scene.project_id != project_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy cảnh.")
    running = await session.scalar(
        select(func.count()).select_from(Job).where(Job.scene_id == scene.id, Job.status.in_(ACTIVE))
    )
    if running:
        raise HTTPException(status_code=409, detail="Cảnh này còn video đang chạy.")
    if scene.image_path:
        Path(scene.image_path).unlink(missing_ok=True)
    await session.delete(scene)
    await session.commit()
    return {"message": "Đã xóa cảnh."}


@router.get("/projects/{project_id}/scenes/{scene_id}/image")
async def scene_image(project_id: str, scene_id: str, session: AsyncSession = Depends(get_session)) -> FileResponse:
    await get_project_or_404(session, project_id)
    scene = await session.get(Scene, scene_id)
    if scene is None or scene.project_id != project_id or not scene.image_path:
        raise HTTPException(status_code=404, detail="Cảnh chưa có ảnh.")
    path = Path(scene.image_path).resolve()
    root = Path(get_settings().data_dir).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy file ảnh.")
    return FileResponse(path, media_type=IMAGE_TYPES.get(path.suffix.lower(), "image/png"))


@router.post("/projects/{project_id}/scenes/{scene_id}/videos", response_model=JobOut, status_code=201)
async def create_scene_video(
    project_id: str,
    scene_id: str,
    model: str = Form(...),
    custom_model: str = Form(""),
    resolution: str = Form("720p"),
    duration: int = Form(5),
    generate_audio: str = Form("true"),
    watermark: str = Form("false"),
    auto_publish: str = Form("true"),
    youtube_title: str = Form(""),
    youtube_description: str = Form(""),
    youtube_tags: str = Form(""),
    youtube_privacy: str = Form("unlisted"),
    youtube_category_id: str = Form("22"),
    session: AsyncSession = Depends(get_session),
) -> JobOut:
    project = await get_project_or_404(session, project_id)
    scene = await session.get(Scene, scene_id)
    if scene is None or scene.project_id != project_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy cảnh.")
    if scene.status != "ready" or not scene.image_path or not Path(scene.image_path).is_file():
        raise HTTPException(status_code=400, detail="Hãy tạo ảnh cảnh có đủ nhân vật trước khi dựng video.")

    model_id = custom_model.strip() if model.strip() == "custom" else model.strip() or DEFAULT_MODEL
    if not MODEL_PATTERN.match(model_id):
        raise HTTPException(status_code=400, detail="Model ID không hợp lệ.")
    if resolution not in RESOLUTIONS:
        raise HTTPException(status_code=400, detail="Độ phân giải không được hỗ trợ.")
    preset = next((item for item in MODEL_PRESETS if item["id"] == model_id), None)
    if preset and resolution not in preset["resolutions"]:
        raise HTTPException(status_code=400, detail="Model này không hỗ trợ độ phân giải đã chọn.")
    allowed = preset["ratios"] if preset and preset.get("ratios") else RATIOS
    if scene.aspect_ratio not in allowed:
        raise HTTPException(status_code=400, detail="Tỷ lệ của ảnh cảnh không khớp model video.")
    if youtube_privacy not in PRIVACY:
        raise HTTPException(status_code=400, detail="Chế độ riêng tư không hợp lệ.")
    if youtube_category_id not in {item["id"] for item in CATEGORIES}:
        raise HTTPException(status_code=400, detail="Danh mục YouTube không hợp lệ.")
    scripted = _scene_script(scene)
    if scripted and scripted.get("duration_seconds"):
        duration = int(scripted["duration_seconds"])
    minimum, maximum, allow_auto = duration_bounds(model_id)
    if duration == -1:
        if not allow_auto:
            raise HTTPException(status_code=400, detail="Model này không hỗ trợ thời lượng tự động.")
    elif duration < minimum or duration > maximum:
        raise HTTPException(status_code=400, detail=f"Thời lượng phải từ {minimum} đến {maximum} giây.")

    source = Path(scene.image_path)
    job_id = str(uuid4())
    upload_dir = Path(get_settings().data_dir) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / f"{job_id}{source.suffix.lower() or '.png'}"
    shutil.copyfile(source, target)
    if scripted:
        prompt = video_prompt(scripted)
    else:
        prompt = (
            "Giữ nguyên khuôn mặt, trang phục và bố cục của ảnh khung đầu. "
            f"Chỉ thêm chuyển động tự nhiên. {scene.prompt}"
        )
        if project.bible_status == "approved":
            prompt += " Không đổi ngoại hình, trang phục và vật đặc trưng trong hồ sơ đã khóa."
    now = utcnow()
    job = Job(
        id=job_id,
        project_id=scene.project_id,
        scene_id=scene.id,
        prompt=prompt[:4000],
        model=model_id,
        resolution=resolution,
        ratio=scene.aspect_ratio,
        duration=duration,
        generate_audio=form_bool(generate_audio, True),
        watermark=form_bool(watermark, False),
        image_path=str(target),
        youtube_title=make_title(youtube_title, scene.prompt),
        youtube_description=(youtube_description.strip() or scene.prompt)[:5000],
        youtube_tags=youtube_tags.strip()[:500],
        youtube_privacy=youtube_privacy,
        youtube_category_id=youtube_category_id,
        auto_publish=form_bool(auto_publish, True),
        status="queued",
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return to_job_out(job)


def _scene_script(scene: Scene) -> dict | None:
    if not scene.script_json:
        return None
    try:
        data = json.loads(scene.script_json)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _require_scene_references(session: AsyncSession, project_id: str, script_json: str) -> None:
    project = await get_project_or_404(session, project_id)
    if approved_bible(project) is None:
        raise HTTPException(status_code=400, detail="Hãy duyệt hồ sơ series trước khi vẽ cảnh.")
    try:
        data = json.loads(script_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Cảnh không có kịch bản hợp lệ.") from exc
    keys = data.get("required_reference_ids") or data.get("character_ids") or []
    if not isinstance(keys, list):
        keys = []
    resolved = await approved_fronts(session, project_id, [str(item) for item in keys])
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail="Mỗi nhân vật trong cảnh cần ảnh mặt trước đã duyệt. Ảnh đó được gửi kèm khi vẽ.",
        )
