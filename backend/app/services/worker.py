import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path

from uuid import uuid4

from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Character, CharacterImage, Job, Project, Scene, YoutubeAccount, utcnow
from app.services.bible import (
    approved_bible,
    find_person,
    scene_image_prompt,
    slot_file,
)
from app.services.cast import approved_fronts, pending_portrait_count, reference_images
from app.services.media import DownloadError, download_video, image_extension
from app.services.seedance import (
    GenerationRequest,
    ReferenceImage,
    SeedanceError,
    SeedanceThrottle,
    create_task,
    failure_message,
    generate_character_image,
    get_task,
    needs_auth_header,
    video_url_from_task,
)
from app.services.settings_store import get_value
from app.services.youtube import YoutubeError, refresh_access_token, upload_video

logger = logging.getLogger(__name__)

POLL_STATUSES = ("queued", "submitting", "generating", "downloading", "generated")


async def recover_stuck_jobs() -> None:
    async with SessionLocal() as session:
        rows = (
            await session.scalars(
                select(Job).where(Job.status.in_(["submitting", "uploading", "downloading"]))
            )
        ).all()
        changed = 0
        for job in rows:
            if job.status == "submitting" and not job.seedance_task_id:
                job.status = "queued"
                changed += 1
            elif job.status == "uploading" and not job.youtube_video_id:
                job.status = "generated"
                job.error_message = "Upload bị gián đoạn. Sẽ thử đăng lại."
                changed += 1
            elif job.status == "downloading":
                job.status = "generating" if not job.remote_video_url else "downloading"
                changed += 1
            job.updated_at = utcnow()
        characters = (
            await session.scalars(select(Character).where(Character.status == "generating"))
        ).all()
        for character in characters:
            character.status = "queued"
            character.updated_at = utcnow()
            changed += 1
        portraits = (
            await session.scalars(select(CharacterImage).where(CharacterImage.status == "generating"))
        ).all()
        for portrait in portraits:
            portrait.status = "queued"
            portrait.updated_at = utcnow()
            changed += 1
        scenes = (await session.scalars(select(Scene).where(Scene.status == "generating"))).all()
        for scene in scenes:
            scene.status = "queued"
            scene.updated_at = utcnow()
            changed += 1
        if changed:
            await session.commit()
            logger.info("Recovered %s stuck jobs", changed)


async def worker_loop() -> None:
    interval = get_settings().worker_interval_seconds
    while True:
        try:
            await process_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker loop failed")
        await asyncio.sleep(interval)


async def process_once() -> None:
    async with SessionLocal() as session:
        busy = await session.scalar(
            select(Character.id).where(Character.status == "generating")
        )
        if busy is None:
            busy = await session.scalar(
                select(CharacterImage.id).where(CharacterImage.status == "generating")
            )
        if busy is None:
            busy = await session.scalar(select(Scene.id).where(Scene.status == "generating"))
    if busy is None:
        if not await process_next_character():
            if not await process_next_portrait():
                await process_next_scene()
    async with SessionLocal() as session:
        jobs = (
            await session.scalars(
                select(Job)
                .where(Job.status.in_(POLL_STATUSES))
                .order_by(Job.created_at.asc())
            )
        ).all()
        ids = [(job.id, job.status, job.next_run_at) for job in jobs]

    now = utcnow()
    for job_id, status, next_run_at in ids:
        if next_run_at and next_run_at > now and status == "queued":
            continue
        try:
            if status == "queued":
                await process_queued(job_id)
            elif status == "submitting":
                await process_submitting(job_id)
            elif status == "generating":
                await process_generating(job_id)
            elif status == "downloading":
                await process_downloading(job_id)
            elif status == "generated":
                await process_generated(job_id)
        except Exception:
            logger.exception("Job %s failed in worker", job_id)


async def _load(job_id: str) -> Job | None:
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if job is not None:
            session.expunge(job)
        return job


async def process_submitting(job_id: str) -> None:
    job = await _load(job_id)
    if job is None or job.status != "submitting":
        return
    if utcnow() - job.updated_at < timedelta(seconds=120):
        return
    if job.seedance_task_id:
        await _update(job_id, status="generating")
        return
    await _update(
        job_id,
        status="queued",
        error_message="Gửi yêu cầu bị gián đoạn, sẽ thử lại.",
    )


async def process_next_character() -> bool:
    async with SessionLocal() as session:
        character = await session.scalar(
            select(Character).where(Character.status == "queued").order_by(Character.created_at.asc())
        )
        if character is None:
            return False
        character_id = character.id
        project_id = character.project_id
        prompt = character.prompt
        aspect = character.aspect_ratio or "1:1"
        source = character.source or "openrouter"
        api_key = await get_value("seedance_api_key")
        base_url = await get_value("seedance_base_url")
        if not api_key:
            character.status = "failed"
            character.error_message = "Chưa cấu hình OpenRouter API key."
            character.updated_at = utcnow()
            await session.commit()
            return True
        character.status = "generating"
        character.error_message = None
        character.updated_at = utcnow()
        await session.commit()

    try:
        payload = await generate_character_image(api_key, base_url, prompt, aspect)
    except SeedanceThrottle as exc:
        await _update_character(character_id, status="queued", error_message=str(exc))
        return True
    except Exception as exc:
        logger.warning("Character image failed for %s: %s", character_id, exc)
        await _update_character(character_id, status="failed", error_message=str(exc)[:1000])
        return True

    folder = Path(get_settings().data_dir) / "characters"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{character_id}{image_extension(payload)}"
    path.write_bytes(payload)
    async with SessionLocal() as session:
        character = await session.get(Character, character_id)
        if character is None:
            path.unlink(missing_ok=True)
            return True
        character.image_path = str(path)
        character.status = "ready"
        character.error_message = None
        character.updated_at = utcnow()
        session.add(
            CharacterImage(
                id=str(uuid4()),
                character_id=character_id,
                project_id=project_id,
                label="Ảnh chính",
                prompt=prompt or "",
                aspect_ratio=aspect,
                source=source,
                image_path=str(path),
                status="ready",
                sort_order=0,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        await session.commit()
    return True


async def process_next_portrait() -> bool:
    async with SessionLocal() as session:
        portrait = await session.scalar(
            select(CharacterImage).where(CharacterImage.status == "queued").order_by(CharacterImage.created_at.asc())
        )
        if portrait is None:
            return False
        portrait_id = portrait.id
        slot = portrait.slot or ""
        prompt = portrait.prompt
        aspect = portrait.aspect_ratio or "1:1"
        character = await session.get(Character, portrait.character_id)
        sheet_dir = character.sheet_dir if character else ""
        character_name = character.name if character else ""
        if slot == "front":
            refs = []
        elif slot in {"side", "back", "expressions"}:
            front = await session.scalar(
                select(CharacterImage).where(
                    CharacterImage.character_id == portrait.character_id,
                    CharacterImage.slot == "front",
                    CharacterImage.status == "approved",
                )
            )
            if front is None or not front.image_path or not Path(front.image_path).is_file():
                portrait.status = "failed"
                portrait.error_message = "Hãy duyệt ảnh mặt trước trước khi vẽ góc khác."
                portrait.updated_at = utcnow()
                await session.commit()
                return True
            refs = [(character_name, front.image_path)]
        else:
            refs = await reference_images(session, portrait.project_id, limit=3)
        api_key = await get_value("seedance_api_key")
        base_url = await get_value("seedance_base_url")
        if not api_key:
            portrait.status = "failed"
            portrait.error_message = "Chưa cấu hình OpenRouter API key."
            portrait.updated_at = utcnow()
            await session.commit()
            return True
        portrait.status = "generating"
        portrait.error_message = None
        portrait.updated_at = utcnow()
        await session.commit()

    try:
        payload = await generate_character_image(api_key, base_url, prompt, aspect, references=refs)
    except SeedanceThrottle as exc:
        await _update_portrait(portrait_id, status="queued", error_message=str(exc))
        return True
    except Exception as exc:
        logger.warning("Reference image failed for %s: %s", portrait_id, exc)
        await _update_portrait(portrait_id, status="failed", error_message=str(exc)[:1000])
        return True

    ext = image_extension(payload)
    if sheet_dir and slot:
        path = slot_file(sheet_dir, slot, ext)
    else:
        folder = Path(get_settings().data_dir) / "characters"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{portrait_id}{ext}"
    path.write_bytes(payload)
    await _update_portrait(portrait_id, status="ready", image_path=str(path), error_message=None)
    if slot == "front":
        async with SessionLocal() as session:
            portrait = await session.get(CharacterImage, portrait_id)
            character = await session.get(Character, portrait.character_id) if portrait else None
            if character is not None:
                character.image_path = str(path)
                character.status = "ready"
                character.updated_at = utcnow()
                await session.commit()
    return True


async def process_next_scene() -> bool:
    async with SessionLocal() as session:
        scene = await session.scalar(
            select(Scene).where(Scene.status == "queued").order_by(Scene.created_at.asc())
        )
        if scene is None:
            return False
        scene_id = scene.id
        prompt = scene.prompt
        aspect = scene.aspect_ratio or "16:9"
        project_id = scene.project_id
        if await pending_portrait_count(session, project_id):
            scene.error_message = "Đang hoàn thiện ảnh tham chiếu. Ảnh cảnh sẽ vẽ khi ảnh nhân vật xong."
            scene.updated_at = utcnow()
            await session.commit()
            return True
        project = await session.get(Project, project_id)
        script_obj = _load_json(scene.script_json)
        bible = approved_bible(project)
        if script_obj:
            if bible is None:
                scene.status = "failed"
                scene.error_message = "Hãy duyệt hồ sơ series trước khi vẽ cảnh."
                scene.updated_at = utcnow()
                await session.commit()
                return True
            keys = script_obj.get("required_reference_ids") or script_obj.get("character_ids") or []
            resolved = await approved_fronts(session, project_id, [str(item) for item in keys])
            if not resolved:
                scene.status = "failed"
                scene.error_message = "Mỗi nhân vật trong cảnh cần ảnh mặt trước đã duyệt. Ảnh đó được gửi kèm khi vẽ."
                scene.updated_at = utcnow()
                await session.commit()
                return True
            people = []
            refs = []
            for key, name, path in resolved:
                person = find_person(bible, key)
                if person is None:
                    scene.status = "failed"
                    scene.error_message = f"Hồ sơ không có nhân vật {key}."
                    scene.updated_at = utcnow()
                    await session.commit()
                    return True
                people.append(person)
                refs.append((name, path))
            scene_prompt = scene_image_prompt(bible, script_obj, people)
        else:
            refs = await reference_images(session, project_id, limit=3)
            if not refs:
                scene.status = "failed"
                scene.error_message = "Hãy lưu ảnh tham chiếu của nhân vật trước. Ảnh cảnh cần những ảnh này để giữ ngoại hình."
                scene.updated_at = utcnow()
                await session.commit()
                return True
            scene_prompt = _manual_scene_prompt(bible, prompt)
        api_key = await get_value("seedance_api_key")
        base_url = await get_value("seedance_base_url")
        if not api_key:
            scene.status = "failed"
            scene.error_message = "Chưa cấu hình OpenRouter API key."
            scene.updated_at = utcnow()
            await session.commit()
            return True
        scene.status = "generating"
        scene.error_message = None
        scene.updated_at = utcnow()
        await session.commit()

    try:
        payload = await generate_character_image(api_key, base_url, scene_prompt, aspect, references=refs)
    except SeedanceThrottle as exc:
        await _update_scene(scene_id, status="queued", error_message=str(exc))
        return True
    except Exception as exc:
        logger.warning("Scene image failed for %s: %s", scene_id, exc)
        await _update_scene(scene_id, status="failed", error_message=str(exc)[:1000])
        return True

    folder = Path(get_settings().data_dir) / "scenes"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{scene_id}{image_extension(payload)}"
    path.write_bytes(payload)
    await _update_scene(scene_id, status="ready", image_path=str(path), error_message=None)
    return True


def _load_json(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _manual_scene_prompt(bible: dict | None, action: str) -> str:
    if not bible:
        return f"Một khung hình có đủ các nhân vật trong cùng một cảnh. {action}"
    lines = [
        f"Visual style: {bible.get('visual_style', '')}.",
        "Preserve each character's identity, facial proportions, hairstyle, body proportions and exact default outfit.",
    ]
    for person in bible.get("characters") or []:
        lines.append(f"{person.get('name')} ({person.get('id')}): outfit {person.get('default_outfit')}.")
    lines.append(f"Scene: {action}")
    lines.append("No subtitles, no speech bubbles, no readable text.")
    return "\n".join(lines)


async def _update_portrait(portrait_id: str, **values) -> None:
    async with SessionLocal() as session:
        portrait = await session.get(CharacterImage, portrait_id)
        if portrait is None:
            return
        for key, value in values.items():
            setattr(portrait, key, value)
        portrait.updated_at = utcnow()
        await session.commit()


async def _update_scene(scene_id: str, **values) -> None:
    async with SessionLocal() as session:
        scene = await session.get(Scene, scene_id)
        if scene is None:
            return
        for key, value in values.items():
            setattr(scene, key, value)
        scene.updated_at = utcnow()
        await session.commit()


async def _update_character(character_id: str, **values) -> None:
    async with SessionLocal() as session:
        character = await session.get(Character, character_id)
        if character is None:
            return
        for key, value in values.items():
            setattr(character, key, value)
        character.updated_at = utcnow()
        await session.commit()


async def process_queued(job_id: str) -> None:
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if job is None or job.status != "queued":
            return
        if job.next_run_at and job.next_run_at > utcnow():
            return
        api_key = await get_value("seedance_api_key")
        base_url = await get_value("seedance_base_url")
        if not api_key:
            job.status = "failed"
            job.error_message = "Chưa cấu hình OpenRouter API key."
            job.updated_at = utcnow()
            await session.commit()
            return
        references: list[ReferenceImage] = []
        frame_only = bool(job.scene_id and job.image_path)
        if frame_only:
            if not job.image_path or not Path(job.image_path).is_file():
                job.status = "failed"
                job.error_message = "Ảnh cảnh không còn trên máy."
                job.updated_at = utcnow()
                await session.commit()
                return
        elif job.project_id:
            if await pending_portrait_count(session, job.project_id):
                job.error_message = "Đang tạo ảnh nhân vật. Video sẽ chạy khi ảnh xong."
                job.next_run_at = utcnow() + timedelta(seconds=15)
                job.updated_at = utcnow()
                await session.commit()
                return
            references = [
                ReferenceImage(name=name, path=path)
                for name, path in await reference_images(session, job.project_id, limit=6)
            ]
        request = GenerationRequest(
            prompt=job.prompt,
            model=job.model,
            resolution=job.resolution,
            ratio=job.ratio,
            duration=job.duration,
            generate_audio=job.generate_audio,
            watermark=job.watermark,
            image_path=job.image_path,
            image_url=job.image_url,
            references=references,
            frame_only=frame_only,
        )
        job.status = "submitting"
        job.updated_at = utcnow()
        await session.commit()

    try:
        task_id = await create_task(api_key, base_url, request)
    except SeedanceThrottle as exc:
        await _update(
            job_id,
            status="queued",
            error_message=str(exc),
            next_run_at=utcnow() + timedelta(seconds=45),
        )
        return
    except Exception as exc:
        message = str(exc).strip() or "OpenRouter không phản hồi khi tạo video. Bấm tạo lại."
        logger.warning("OpenRouter create failed for %s: %s", job_id, message)
        await _update(job_id, status="failed", error_message=message)
        return

    await _update(
        job_id,
        status="generating",
        seedance_task_id=task_id,
        seedance_status="queued",
        error_message=None,
        next_run_at=None,
    )


async def process_generating(job_id: str) -> None:
    job = await _load(job_id)
    if job is None or job.status != "generating" or not job.seedance_task_id:
        return
    if job.next_run_at and job.next_run_at > utcnow():
        return
    api_key = await get_value("seedance_api_key")
    base_url = await get_value("seedance_base_url")
    if not api_key:
        await _update(job_id, status="failed", error_message="Chưa cấu hình OpenRouter API key.")
        return
    try:
        data = await get_task(api_key, base_url, job.seedance_task_id)
    except SeedanceThrottle as exc:
        await _update(job_id, error_message=str(exc), next_run_at=utcnow() + timedelta(seconds=30))
        return
    except SeedanceError as exc:
        await _update(job_id, status="failed", error_message=str(exc))
        return

    status = str(data.get("status") or "")
    if status in {"queued", "running", "pending", "in_progress"}:
        await _update(job_id, seedance_status=status, error_message=None)
        return
    if status in {"succeeded", "completed"}:
        url = video_url_from_task(data, base_url)
        if not url:
            await _update(
                job_id,
                status="failed",
                seedance_status=status,
                error_message="OpenRouter báo xong nhưng không có URL video.",
            )
            return
        await _update(
            job_id,
            status="downloading",
            seedance_status=status,
            remote_video_url=url,
            error_message=None,
        )
        await process_downloading(job_id)
        return
    if status in {"failed", "expired", "cancelled"}:
        await _update(
            job_id,
            status="failed",
            seedance_status=status,
            error_message=failure_message(data),
        )
        return
    await _update(job_id, seedance_status=status or None)


async def process_downloading(job_id: str) -> None:
    job = await _load(job_id)
    if job is None or job.status != "downloading" or not job.remote_video_url:
        return
    if job.next_run_at and job.next_run_at > utcnow():
        return
    settings = get_settings()
    destination = Path(settings.data_dir) / "videos" / f"{job.id}.mp4"
    headers = None
    if needs_auth_header(job.remote_video_url):
        api_key = await get_value("seedance_api_key")
        if api_key:
            headers = {"Authorization": f"Bearer {api_key}"}
    try:
        await download_video(job.remote_video_url, destination, headers=headers)
    except DownloadError as exc:
        attempts = job.attempt_count + 1
        failed = attempts >= 5
        await _update(
            job_id,
            status="failed" if failed else "downloading",
            error_message=str(exc),
            attempt_count=attempts,
            next_run_at=None if failed else utcnow() + timedelta(seconds=20),
        )
        return
    await _update(
        job_id,
        status="generated",
        local_path=str(destination),
        error_message=None,
        attempt_count=0,
        next_run_at=None,
    )
    await process_generated(job_id)


async def process_generated(job_id: str) -> None:
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if job is None or job.status != "generated" or not job.auto_publish:
            return
        if job.youtube_video_id:
            job.status = "published"
            job.updated_at = utcnow()
            await session.commit()
            return
        account = await session.get(YoutubeAccount, 1)
        if account is None:
            if job.error_message != "Chưa kết nối YouTube. Video đã lưu, sẽ đăng khi bạn kết nối.":
                job.error_message = "Chưa kết nối YouTube. Video đã lưu, sẽ đăng khi bạn kết nối."
                job.updated_at = utcnow()
                await session.commit()
            return
        if not job.local_path or not Path(job.local_path).is_file():
            job.status = "failed"
            job.error_message = "Không tìm thấy file video trên máy."
            job.updated_at = utcnow()
            await session.commit()
            return
        client_id = await get_value("google_client_id")
        client_secret = await get_value("google_client_secret")
        if not client_id or not client_secret:
            job.error_message = "Thiếu Google OAuth client. Mở Cài đặt để nhập."
            job.updated_at = utcnow()
            await session.commit()
            return
        try:
            token = await refresh_access_token(account, client_id, client_secret)
        except YoutubeError as exc:
            job.status = "upload_failed"
            job.error_message = str(exc)
            job.updated_at = utcnow()
            await session.commit()
            return
        snapshot = {
            "path": job.local_path,
            "title": job.youtube_title,
            "description": job.youtube_description,
            "tags": job.youtube_tags,
            "privacy": job.youtube_privacy,
            "category": job.youtube_category_id,
        }
        job.status = "uploading"
        job.error_message = None
        job.updated_at = utcnow()
        await session.commit()

    try:
        video_id = await upload_video(
            access_token=token,
            file_path=snapshot["path"],
            title=snapshot["title"],
            description=snapshot["description"],
            tags=snapshot["tags"],
            privacy=snapshot["privacy"],
            category_id=snapshot["category"],
        )
    except Exception as exc:
        logger.warning("YouTube upload failed for %s: %s", job_id, exc)
        await _update(job_id, status="upload_failed", error_message=str(exc))
        return
    await _update(job_id, status="published", youtube_video_id=video_id, error_message=None)


async def _update(job_id: str, **fields) -> None:
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)
        job.updated_at = utcnow()
        await session.commit()
