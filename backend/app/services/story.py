import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import CONTINUITY_RULES
from app.models import Episode, Job, Scene, utcnow
from app.services.bible import BibleError, approved_bible, as_text, find_person

ACTIVE = {"submitting", "generating", "downloading", "uploading"}


class StoryError(ValueError):
    pass


EPISODE_RULES = """Mỗi tập dài khoảng 45–60 giây. Tổng duration_seconds của các cảnh nằm trong khoảng đó. Mỗi cảnh từ 5 đến 15 giây.

Không chèn chữ vào hình ảnh; chữ và phụ đề được thêm ở hậu kỳ.

Chỉ viết tình huống, hành động và lời thoại mới.
Không mô tả lại và không thay đổi ngoại hình, trang phục, tuổi, tính cách hay cách nói. Backend tự gắn các đặc điểm đã khóa khi tạo ảnh.

Không lặp chủ đề đã dùng. Cảnh đầu tiếp nối trạng thái câu chuyện hiện tại. Cảnh sau bắt đầu từ end_state của cảnh trước.
character_ids và required_reference_ids chỉ được dùng id có trong hồ sơ.

Trả về JSON:
{
  "episode_id": "...",
  "title": "...",
  "summary": "...",
  "scenes": [
    {
      "scene_id": "...",
      "duration_seconds": 7,
      "location_id": "...",
      "character_ids": ["CHAR_MINH", "CHAR_AN"],
      "start_state": "...",
      "main_action": "...",
      "dialogue": [{"character_id": "...", "text": "..."}],
      "camera": "...",
      "end_state": "...",
      "required_reference_ids": ["CHAR_MINH", "CHAR_AN"]
    }
  ]
}"""


def empty_state() -> dict:
    return {
        "current_state": "Chưa có tập nào. Câu chuyện bắt đầu từ đây.",
        "used_topics": [],
        "episodes": [],
    }


def story_document(project) -> dict:
    if project is None or not project.story_state:
        return empty_state()
    try:
        data = json.loads(project.story_state)
    except json.JSONDecodeError:
        return empty_state()
    if not isinstance(data, dict):
        return empty_state()
    topics = data.get("used_topics") if isinstance(data.get("used_topics"), list) else []
    episodes = data.get("episodes") if isinstance(data.get("episodes"), list) else []
    return {
        "current_state": as_text(data.get("current_state")) or empty_state()["current_state"],
        "used_topics": [as_text(item) for item in topics if as_text(item)][:200],
        "episodes": [item for item in episodes if isinstance(item, dict)][:300],
    }


def dump_state(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False)


def episode_user_prompt(bible: dict, state: dict, history: list[dict]) -> str:
    cards = ["HỒ SƠ ĐÃ KHÓA (chỉ để bám tính cách và cách nói, không được viết lại):"]
    for person in bible["characters"]:
        cards.append(
            "\n".join(
                [
                    f"- {person['id']} / {person['name']}, tuổi {person['age']}",
                    f"  Tính cách: {person['personality']}",
                    f"  Điểm mạnh: {person['strengths'] or 'không nêu'}",
                    f"  Điểm yếu: {person['weaknesses'] or 'không nêu'}",
                    f"  Cách nói: {person['speaking_style']}",
                ]
            )
        )
    topics = state.get("used_topics") or []
    topic_text = "\n".join(f"- {item}" for item in topics) or "- Chưa có"
    related = []
    for item in history[-12:]:
        related.append(
            f"- Tập {item.get('number')}: {item.get('title')} ({item.get('status')}). {item.get('summary')}"
        )
    related_text = "\n".join(related) or "- Chưa có tập trước."
    return "\n\n".join(
        [
            "\n".join(cards),
            "TRẠNG THÁI CÂU CHUYỆN HIỆN TẠI:\n" + state["current_state"],
            "CHỦ ĐỀ ĐÃ DÙNG:\n" + topic_text,
            "TÓM TẮT CÁC TẬP LIÊN QUAN:\n" + related_text,
            EPISODE_RULES,
        ]
    )


def plan_user_prompt(
    bible: dict,
    state: dict,
    history: list[dict],
    idea: str,
    scene_count: int,
    continuity: str = "",
) -> str:
    ids = ", ".join(f"{person['id']} ({person['name']})" for person in bible["characters"])
    wide = any(token in idea.lower() for token in ("16:9", "ngang", "landscape"))
    framing = "16:9 wide shot" if wide else "Vertical 9:16, medium two-shot"
    cards = ["HỒ SƠ ĐÃ KHÓA. Chỉ bám tính cách và cách nói. Không viết lại ngoại hình hay trang phục:"]
    for person in bible["characters"]:
        cards.append(
            "\n".join(
                [
                    f"- {person['id']} / {person['name']}",
                    f"  Tính cách: {person['personality']}",
                    f"  Cách nói: {person['speaking_style']}",
                ]
            )
        )
    topics = state.get("used_topics") or []
    topic_text = "\n".join(f"- {item}" for item in topics) or "- Chưa có"
    related = [
        f"- Tập {item.get('number')}: {item.get('title')}. {item.get('summary')}"
        for item in history[-12:]
    ]
    related_text = "\n".join(related) or "- Chưa có tập trước."
    edit_rules = continuity.strip() or CONTINUITY_RULES
    rules = f"""Tách prompt chung thành đúng {scene_count} cảnh. Không hơn, không kém.
Mỗi cảnh là một shot video riêng, duration_seconds từ 5 đến 15.
Cảnh đầu tiếp nối trạng thái câu chuyện hiện tại.
main_action mô tả đoạn đệm đầu, hành động chính, rồi đoạn đệm cuối. Đủ cụ thể để vẽ ảnh rồi tạo video.
dialogue là lời nói ngắn bằng tiếng Việt, nói trọn trong phần hành động chính.
camera luôn bắt đầu bằng "{framing}".
character_ids và required_reference_ids dùng các id: {ids}. Mặc định có đủ các nhân vật trong hồ sơ.
Không mô tả lại mặt, tóc, trang phục. Không chữ trên hình.
join chỉ được là continue hoặc new_angle. Cảnh new_angle bắt buộc có cut_note.

QUY TẮC DỰNG NỐI:
{edit_rules}

title là một câu tóm tắt đúng nội dung prompt chung, không đặt tên khác.

Trả về JSON:
{{
  "episode_id": "...",
  "title": "một câu tóm tắt đúng nội dung prompt chung",
  "summary": "tóm tắt diễn biến các cảnh",
  "scenes": [
    {{
      "scene_id": "s1",
      "duration_seconds": 15,
      "location_id": "...",
      "character_ids": [],
      "join": "continue",
      "start_state": "tư thế, tay đang dùng, đạo cụ, hướng chuyển động",
      "main_action": "...",
      "dialogue": [{{"character_id": "...", "text": "..."}}],
      "camera": "{framing}",
      "end_state": "tư thế, tay đang dùng, đạo cụ, hướng chuyển động",
      "cut_note": "",
      "required_reference_ids": []
    }}
  ]
}}"""
    return "\n\n".join(
        [
            "\n".join(cards),
            "TRẠNG THÁI CÂU CHUYỆN HIỆN TẠI:\n" + state["current_state"],
            "CHỦ ĐỀ ĐÃ DÙNG:\n" + topic_text,
            "TÓM TẮT CÁC TẬP LIÊN QUAN:\n" + related_text,
            "PROMPT CHUNG:\n" + idea.strip(),
            rules,
        ]
    )


def _join_value(raw) -> str:
    token = as_text(raw).casefold().replace("_", " ")
    if token in {"continue", "continuation", "tiếp tục", "tiep tuc"}:
        return "continue"
    if token in {"new angle", "newangle", "angle", "đổi góc", "doi goc", "cut"}:
        return "new_angle"
    return ""


def title_from_prompt(idea: str) -> str:
    text = " ".join(as_text(idea).split())
    if not text:
        raise StoryError("Prompt chung đang trống.")
    sentence = text
    for mark in (".", "!", "?", "。"):
        index = text.find(mark)
        if index > 0:
            sentence = text[:index]
            break
    sentence = sentence.strip(" \"'“”.,")
    if len(sentence) > 120:
        cut = sentence[:120]
        space = cut.rfind(" ")
        sentence = (cut[:space] if space >= 40 else cut).rstrip(" \"'“”.,")
    return sentence[:160]


def prepare_plan(raw: dict, scene_count: int, idea: str) -> dict:
    scenes = raw.get("scenes") if isinstance(raw, dict) else None
    if not isinstance(scenes, list):
        raise StoryError("Model không trả về danh sách cảnh.")
    if len(scenes) != scene_count:
        raise StoryError(f"Model trả về {len(scenes)} cảnh, cần đúng {scene_count}.")
    wide = any(token in idea.lower() for token in ("16:9", "ngang", "landscape"))
    framing = "16:9 wide shot" if wide else "Vertical 9:16, medium two-shot"
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            raise StoryError(f"Cảnh {index} không hợp lệ.")
        try:
            seconds = int(scene.get("duration_seconds") or 15)
        except (TypeError, ValueError):
            seconds = 15
        scene["duration_seconds"] = min(15, max(5, seconds))
        camera = as_text(scene.get("camera"))
        if not any(token in camera.lower() for token in ("9:16", "16:9", "1:1", "4:3", "3:4", "21:9")):
            scene["camera"] = f"{framing}. {camera}".strip()
        join = _join_value(scene.get("join"))
        if not join:
            raise StoryError("Mỗi cảnh cần join là continue hoặc new_angle.")
        scene["join"] = join
        if not as_text(scene.get("start_state")) or not as_text(scene.get("end_state")):
            raise StoryError("Mỗi cảnh cần start_state và end_state.")
        if join == "new_angle" and not as_text(scene.get("cut_note")):
            raise StoryError("Cảnh đổi góc cần cut_note nêu điểm cắt và chủ thể cảnh sau.")
    raw["title"] = title_from_prompt(idea)
    return raw


def _resolve_key(token, bible: dict) -> str:
    person = find_person(bible, as_text(token))
    if person is None:
        raise StoryError(f"Không có nhân vật {as_text(token)} trong hồ sơ đã khóa.")
    return person["id"]


def _duration(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise StoryError("Mỗi cảnh cần duration_seconds là số nguyên.") from exc
    if number < 1 or number > 15:
        raise StoryError("Mỗi cảnh dài từ 1 đến 15 giây.")
    return number


def normalize_episode(data: dict, bible: dict) -> dict:
    if not isinstance(data, dict):
        raise StoryError("Kịch bản phải là một JSON object.")
    title = as_text(data.get("title"))
    summary = as_text(data.get("summary"))
    episode_id = as_text(data.get("episode_id")) or "episode"
    if not title:
        raise StoryError("Tập phim thiếu tiêu đề.")
    if not summary:
        raise StoryError("Tập phim thiếu tóm tắt.")
    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise StoryError("Tập phim cần ít nhất một cảnh.")
    if len(scenes) > 12:
        raise StoryError("Mỗi tập tối đa 12 cảnh.")
    known = {person["id"] for person in bible["characters"]}
    cleaned = []
    for index, raw in enumerate(scenes, start=1):
        if not isinstance(raw, dict):
            raise StoryError(f"Cảnh {index} không hợp lệ.")
        keys = raw.get("character_ids")
        if not isinstance(keys, list) or not keys:
            raise StoryError(f"Cảnh {index} chưa chỉ định nhân vật.")
        character_ids = []
        for token in keys:
            key = _resolve_key(token, bible)
            if key not in character_ids:
                character_ids.append(key)
        if any(key not in known for key in character_ids):
            raise StoryError(f"Cảnh {index} dùng nhân vật ngoài hồ sơ.")
        action = as_text(raw.get("main_action"))
        if not action:
            raise StoryError(f"Cảnh {index} thiếu hành động chính.")
        dialogue = []
        for line in raw.get("dialogue") or []:
            if not isinstance(line, dict):
                continue
            text = as_text(line.get("text"))
            if not text:
                continue
            speaker = _resolve_key(line.get("character_id"), bible)
            dialogue.append({"character_id": speaker, "text": text[:400]})
        references = raw.get("required_reference_ids") or character_ids
        if not isinstance(references, list):
            references = character_ids
        reference_ids = []
        for token in references:
            key = _resolve_key(token, bible)
            if key not in reference_ids:
                reference_ids.append(key)
        cleaned.append(
            {
                "scene_id": (as_text(raw.get("scene_id")) or f"scene-{index}")[:64],
                "duration_seconds": _duration(raw.get("duration_seconds")),
                "location_id": (as_text(raw.get("location_id")) or "location")[:64],
                "character_ids": character_ids,
                "start_state": as_text(raw.get("start_state"))[:1000],
                "main_action": action[:1500],
                "dialogue": dialogue[:8],
                "camera": as_text(raw.get("camera"))[:500],
                "end_state": as_text(raw.get("end_state"))[:1000],
                "join": _join_value(raw.get("join")),
                "cut_note": as_text(raw.get("cut_note"))[:500],
                "required_reference_ids": reference_ids or character_ids,
            }
        )
    return {
        "episode_id": episode_id[:64],
        "title": title[:160],
        "summary": summary[:2000],
        "scenes": cleaned,
    }


def _speaker_name(token: str, bible: dict | None) -> str:
    key = as_text(token)
    if bible:
        for person in bible.get("characters") or []:
            if person.get("id") == key or person.get("name") == key:
                return as_text(person.get("name")) or key
    bare = key.removeprefix("CHAR_").replace("_", " ").strip()
    if not bare:
        return key
    return bare[:1].upper() + bare[1:].lower()


def youtube_listing(title: str, summary: str, scenes: list | None, bible: dict | None = None) -> tuple[str, str]:
    story = " ".join(as_text(summary).split())
    fallback = " ".join(as_text(title).split())
    heading = story or fallback or "Video"
    if len(heading) > 100:
        cut = heading[:100]
        space = cut.rfind(" ")
        heading = (cut[:space] if space >= 40 else cut).rstrip(" .,")
    lines = [story] if story else []
    for scene in scenes or []:
        if not isinstance(scene, dict):
            continue
        action = " ".join(as_text(scene.get("main_action")).split())
        spoken = []
        for line in scene.get("dialogue") or []:
            if not isinstance(line, dict):
                continue
            text = as_text(line.get("text"))
            if not text:
                continue
            spoken.append(f"{_speaker_name(line.get('character_id'), bible)}: {text}")
        parts = []
        if action and action != story:
            parts.append(action)
        parts.extend(spoken)
        block = "\n".join(parts).strip()
        if block and block not in lines:
            lines.append(block)
    description = "\n\n".join(line for line in lines if line).strip()[:5000]
    return heading[:100], description or heading[:5000]


def render_script(episode_obj: dict) -> str:
    lines = [episode_obj["summary"], ""]
    for index, scene in enumerate(episode_obj["scenes"], start=1):
        names = ", ".join(scene["character_ids"])
        kind = {"continue": "tiếp tục hành động", "new_angle": "đổi góc"}.get(scene.get("join") or "", "")
        label = f"Cảnh {index} ({scene['duration_seconds']}s) — {scene['location_id']} — {names}"
        if kind:
            label += f" — {kind}"
        lines.append(label)
        if scene.get("cut_note"):
            lines.append(f"Điểm cắt: {scene['cut_note']}")
        if scene["start_state"]:
            lines.append(f"Đầu cảnh: {scene['start_state']}")
        lines.append(scene["main_action"])
        for line in scene["dialogue"]:
            lines.append(f"{line['character_id']}: {line['text']}")
        if scene["end_state"]:
            lines.append(f"Cuối cảnh: {scene['end_state']}")
        lines.append("")
    return "\n".join(lines)[:20000]


def advance_state(state: dict, episode_obj: dict, number: int) -> dict:
    episodes = [
        item
        for item in state.get("episodes") or []
        if item.get("episode_id") != episode_obj["episode_id"]
    ]
    episodes.append(
        {
            "episode_id": episode_obj["episode_id"],
            "number": number,
            "title": episode_obj["title"],
            "summary": episode_obj["summary"],
            "end_state": episode_obj["scenes"][-1]["end_state"],
        }
    )
    topics = list(state.get("used_topics") or [])
    if episode_obj["title"] not in topics:
        topics.append(episode_obj["title"])
    current = episode_obj["scenes"][-1]["end_state"] or state.get("current_state") or ""
    return {
        "current_state": current,
        "used_topics": topics[-200:],
        "episodes": episodes[-300:],
    }


async def recent_history(session: AsyncSession, project_id: str) -> list[dict]:
    rows = (
        await session.scalars(
            select(Episode).where(Episode.project_id == project_id).order_by(Episode.number.asc())
        )
    ).all()
    history = []
    for episode in rows[-12:]:
        history.append(
            {
                "number": episode.number,
                "title": episode.title,
                "summary": episode.summary or "",
                "status": "đã duyệt" if episode.status == "approved" else "bản nháp",
            }
        )
    return history


async def save_scripted_episode(session: AsyncSession, project, raw: dict) -> Episode:
    bible = approved_bible(project)
    if bible is None:
        raise StoryError("Hãy duyệt hồ sơ series trước khi lưu tập.")
    try:
        episode_obj = normalize_episode(raw, bible)
    except BibleError as exc:
        raise StoryError(str(exc)) from exc
    number = await session.scalar(select(func.max(Episode.number)).where(Episode.project_id == project.id))
    now = utcnow()
    episode = Episode(
        id=str(uuid4()),
        project_id=project.id,
        number=int(number or 0) + 1,
        title=episode_obj["title"],
        script=render_script(episode_obj),
        summary=episode_obj["summary"],
        script_json=json.dumps(episode_obj, ensure_ascii=False),
        status="draft",
        created_at=now,
        updated_at=now,
    )
    session.add(episode)
    await session.flush()
    _add_scenes(session, episode, episode_obj)
    project.updated_at = now
    await session.commit()
    await session.refresh(episode)
    return episode


def _add_scenes(session: AsyncSession, episode: Episode, episode_obj: dict) -> None:
    from app.services.bible import aspect_from_camera

    now = utcnow()
    for index, scene in enumerate(episode_obj["scenes"], start=1):
        session.add(
            Scene(
                id=str(uuid4()),
                episode_id=episode.id,
                project_id=episode.project_id,
                sort_order=index,
                duration_seconds=scene["duration_seconds"],
                prompt=scene["main_action"],
                script_json=json.dumps(scene, ensure_ascii=False),
                aspect_ratio=aspect_from_camera(scene.get("camera") or ""),
                status="draft",
                created_at=now,
                updated_at=now,
            )
        )


async def replace_episode_script(session: AsyncSession, project, episode: Episode, raw: dict) -> Episode:
    bible = approved_bible(project)
    if bible is None:
        raise StoryError("Hồ sơ series chưa được duyệt.")
    episode_obj = normalize_episode(raw, bible)
    scenes = (await session.scalars(select(Scene).where(Scene.episode_id == episode.id))).all()
    scene_ids = [scene.id for scene in scenes]
    if scene_ids:
        running = await session.scalar(
            select(func.count()).select_from(Job).where(Job.scene_id.in_(scene_ids), Job.status.in_(ACTIVE))
        )
        if running:
            raise StoryError("Tập này còn video đang chạy.")
    for scene in scenes:
        if scene.image_path:
            Path(scene.image_path).unlink(missing_ok=True)
        await session.delete(scene)
    now = utcnow()
    episode.title = episode_obj["title"]
    episode.summary = episode_obj["summary"]
    episode.script = render_script(episode_obj)
    episode.script_json = json.dumps(episode_obj, ensure_ascii=False)
    episode.status = "draft"
    episode.updated_at = now
    _add_scenes(session, episode, episode_obj)
    await session.commit()
    await session.refresh(episode)
    return episode


def approve_story(project, episode: Episode) -> dict:
    if not episode.script_json:
        raise StoryError("Tập này chưa có kịch bản có cấu trúc.")
    try:
        episode_obj = json.loads(episode.script_json)
    except json.JSONDecodeError as exc:
        raise StoryError("Kịch bản tập không đọc được.") from exc
    state = advance_state(story_document(project), episode_obj, episode.number)
    project.story_state = dump_state(state)
    episode.status = "approved"
    episode.updated_at = utcnow()
    project.updated_at = utcnow()
    return state
