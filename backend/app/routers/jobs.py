import re
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import CATEGORIES, MODEL_PRESETS, PRIVACY, RATIOS, RESOLUTIONS, STATUS_LABELS
from app.database import get_session
from app.models import Job, Project, YoutubeAccount, utcnow
from app.schemas import JobOut, MessageOut
from app.services.media import safe_download_name

router = APIRouter(tags=["jobs"])

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ACTIVE = {"submitting", "generating", "downloading", "uploading"}
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


def form_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def duration_bounds(model: str) -> tuple[int, int, bool]:
    for preset in MODEL_PRESETS:
        if preset["id"] == model:
            return preset["min_duration"], preset["max_duration"], preset["allow_auto_duration"]
    lowered = model.lower()
    if "2-5" in lowered or "2.5" in lowered:
        return 4, 30, True
    if "2-0" in lowered or "2.0" in lowered:
        return 4, 15, True
    if "1-5" in lowered or "1.5" in lowered:
        return 4, 12, True
    if "1-0" in lowered or "1.0" in lowered:
        return 2, 12, False
    if "heygen" in lowered:
        return 5, 15, False
    return 2, 30, True


def make_title(title: str, prompt: str) -> str:
    text = title.strip() or " ".join(prompt.split())
    text = text[:100].strip()
    return text or "Video Grok"


def to_job_out(job: Job) -> JobOut:
    has_video = bool(job.local_path and Path(job.local_path).is_file())
    video_id = job.youtube_video_id
    return JobOut(
        id=job.id,
        project_id=job.project_id,
        scene_id=job.scene_id,
        prompt=job.prompt,
        model=job.model,
        resolution=job.resolution,
        ratio=job.ratio,
        duration=job.duration,
        generate_audio=job.generate_audio,
        watermark=job.watermark,
        has_image=bool(job.image_path or job.image_url),
        seedance_task_id=job.seedance_task_id,
        seedance_status=job.seedance_status,
        has_video=has_video,
        youtube_title=job.youtube_title,
        youtube_description=job.youtube_description,
        youtube_tags=job.youtube_tags,
        youtube_privacy=job.youtube_privacy,
        youtube_category_id=job.youtube_category_id,
        auto_publish=job.auto_publish,
        youtube_video_id=video_id,
        youtube_url=f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
        status=job.status,
        status_label=STATUS_LABELS.get(job.status, job.status),
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


async def get_job_or_404(session: AsyncSession, job_id: str) -> Job:
    try:
        UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job.") from exc
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy job.")
    return job


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(session: AsyncSession = Depends(get_session)) -> list[JobOut]:
    rows = (
        await session.scalars(select(Job).order_by(Job.created_at.desc()).limit(100))
    ).all()
    return [to_job_out(job) for job in rows]


@router.get("/jobs/{job_id}", response_model=JobOut)
async def read_job(job_id: str, session: AsyncSession = Depends(get_session)) -> JobOut:
    job = await get_job_or_404(session, job_id)
    return to_job_out(job)


@router.post("/jobs", response_model=JobOut, status_code=201)
async def create_job(
    project_id: str = Form(...),
    prompt: str = Form(...),
    model: str = Form(...),
    custom_model: str = Form(""),
    resolution: str = Form("720p"),
    ratio: str = Form("16:9"),
    duration: int = Form(5),
    generate_audio: str = Form("true"),
    watermark: str = Form("false"),
    auto_publish: str = Form("true"),
    image_url: str = Form(""),
    youtube_title: str = Form(""),
    youtube_description: str = Form(""),
    youtube_tags: str = Form(""),
    youtube_privacy: str = Form("unlisted"),
    youtube_category_id: str = Form("22"),
    image: UploadFile | None = File(None),
    session: AsyncSession = Depends(get_session),
) -> JobOut:
    try:
        UUID(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy dự án.") from exc
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy dự án.")
    text = prompt.strip()
    if not text or len(text) > 4000:
        raise HTTPException(status_code=400, detail="Prompt phải từ 1 đến 4000 ký tự.")

    model_id = custom_model.strip() if model.strip() == "custom" else model.strip()
    if not MODEL_PATTERN.match(model_id):
        raise HTTPException(status_code=400, detail="Model ID không hợp lệ.")

    if resolution not in RESOLUTIONS:
        raise HTTPException(status_code=400, detail="Độ phân giải không được hỗ trợ.")
    preset = next((item for item in MODEL_PRESETS if item["id"] == model_id), None)
    if preset and resolution not in preset["resolutions"]:
        raise HTTPException(status_code=400, detail="Model này không hỗ trợ độ phân giải đã chọn.")
    allowed_ratios = preset.get("ratios") if preset else None
    if ratio not in (allowed_ratios or RATIOS):
        raise HTTPException(status_code=400, detail="Tỷ lệ khung hình không được hỗ trợ.")
    if youtube_privacy not in PRIVACY:
        raise HTTPException(status_code=400, detail="Chế độ riêng tư không hợp lệ.")
    if youtube_category_id not in {item["id"] for item in CATEGORIES}:
        raise HTTPException(status_code=400, detail="Danh mục YouTube không hợp lệ.")

    minimum, maximum, allow_auto = duration_bounds(model_id)
    if duration == -1:
        if not allow_auto:
            raise HTTPException(status_code=400, detail="Model này không hỗ trợ thời lượng tự động.")
    elif duration < minimum or duration > maximum:
        raise HTTPException(
            status_code=400,
            detail=f"Thời lượng phải từ {minimum} đến {maximum} giây.",
        )

    cleaned_image_url = image_url.strip()
    if cleaned_image_url and not cleaned_image_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL ảnh phải bắt đầu bằng http:// hoặc https://")
    if len(cleaned_image_url) > 2000:
        raise HTTPException(status_code=400, detail="URL ảnh quá dài.")

    job_id = str(uuid4())
    image_path = None
    if image is not None and image.filename:
        suffix = Path(image.filename).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise HTTPException(status_code=400, detail="Ảnh phải là jpg, png, webp, gif hoặc bmp.")
        payload = await image.read()
        if not payload:
            raise HTTPException(status_code=400, detail="File ảnh rỗng.")
        if len(payload) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="Ảnh phải nhỏ hơn 8MB.")
        upload_dir = Path(get_settings().data_dir) / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        image_path = upload_dir / f"{job_id}{suffix}"
        image_path.write_bytes(payload)
        cleaned_image_url = ""

    description = youtube_description.strip() or text
    job = Job(
        id=job_id,
        project_id=project.id,
        prompt=text,
        model=model_id,
        resolution=resolution,
        ratio=ratio,
        duration=duration,
        generate_audio=form_bool(generate_audio, True),
        watermark=form_bool(watermark, False),
        image_path=str(image_path) if image_path else None,
        image_url=cleaned_image_url or None,
        youtube_title=make_title(youtube_title, text),
        youtube_description=description[:5000],
        youtube_tags=youtube_tags.strip()[:500],
        youtube_privacy=youtube_privacy,
        youtube_category_id=youtube_category_id,
        auto_publish=form_bool(auto_publish, True),
        status="queued",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return to_job_out(job)


@router.post("/jobs/{job_id}/publish", response_model=JobOut)
async def publish_job(job_id: str, session: AsyncSession = Depends(get_session)) -> JobOut:
    job = await get_job_or_404(session, job_id)
    if job.status not in {"generated", "upload_failed"}:
        raise HTTPException(status_code=409, detail="Video chưa sẵn sàng để đăng.")
    if not job.local_path or not Path(job.local_path).is_file():
        raise HTTPException(status_code=409, detail="Không tìm thấy file video.")
    account = await session.get(YoutubeAccount, 1)
    if account is None:
        raise HTTPException(status_code=400, detail="Hãy kết nối YouTube trước khi đăng.")
    job.auto_publish = True
    job.status = "generated"
    job.error_message = None
    job.updated_at = utcnow()
    await session.commit()
    await session.refresh(job)
    return to_job_out(job)


@router.delete("/jobs/{job_id}", response_model=MessageOut)
async def delete_job(job_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    job = await get_job_or_404(session, job_id)
    if job.status in ACTIVE:
        raise HTTPException(status_code=409, detail="Không thể xóa job đang chạy.")
    for raw in (job.local_path, job.image_path):
        if raw:
            Path(raw).unlink(missing_ok=True)
    await session.delete(job)
    await session.commit()
    return {"message": "Đã xóa job."}


@router.get("/jobs/{job_id}/video")
async def stream_video(
    job_id: str,
    download: bool = False,
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    job = await get_job_or_404(session, job_id)
    if not job.local_path or not Path(job.local_path).is_file():
        raise HTTPException(status_code=404, detail="Video chưa có file.")
    path = Path(job.local_path)
    if download:
        return FileResponse(path, media_type="video/mp4", filename=safe_download_name(job.youtube_title))
    return FileResponse(path, media_type="video/mp4")
