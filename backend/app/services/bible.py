import json
import re
import shutil
from pathlib import Path

from app.config import get_settings
from app.constants import TEXT_MODEL_PRESETS

TEXT_MODEL = "x-ai/grok-4.7"
SCENE_MODEL = TEXT_MODEL_PRESETS[0]["id"]
SHEET_SLOTS = ("front", "side", "back", "expressions")
SLOT_LABELS = {
    "front": "Mặt trước",
    "side": "Nghiêng",
    "back": "Phía sau",
    "expressions": "Biểu cảm",
}
SLOT_ASPECT = {
    "front": "3:4",
    "side": "3:4",
    "back": "3:4",
    "expressions": "16:9",
}

BIBLE_PROMPT = """Phong cách: hoạt hình 3D, màu sắc tươi sáng, biểu cảm rõ ràng.
Mỗi tập dài khoảng 45–60 giây.
Chủ đề: trường học, ôn bài, thi cử, tình bạn và những tình huống hài hước.

Hãy tạo hai nhân vật chính có ngoại hình khác biệt, dễ nhận ra, dễ tái sử dụng trong hàng trăm tập.

Trả về JSON gồm:
- series_id
- series_name
- visual_style
- characters, đúng hai người, mỗi người có:
  - id dạng CHAR_TEN, ví dụ CHAR_MINH
  - name
  - age
  - appearance: object với face, hair, eyes, body
  - default_outfit: mô tả chính xác màu và từng món trang phục
  - signature_item
  - personality
  - strengths
  - weaknesses
  - speaking_style

Ngoại hình và trang phục phải đủ cụ thể để vẽ lại đúng ở các tập sau.
Chỉ trả về một JSON object, không markdown."""


class BibleError(ValueError):
    pass


def as_text(value) -> str:
    if isinstance(value, dict):
        parts = [f"{key}: {as_text(item)}" for key, item in value.items() if as_text(item)]
        return ". ".join(parts)
    if isinstance(value, list):
        return ", ".join(as_text(item) for item in value if as_text(item))
    if value is None:
        return ""
    return str(value).strip()


def parse_json_text(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise BibleError("Hồ sơ phải là một JSON object.")
    return data


def _required(value, label: str) -> str:
    text = as_text(value)
    if not text:
        raise BibleError(f"Hồ sơ thiếu {label}.")
    return text


def _character_key(raw_id: str, name: str) -> str:
    token = (raw_id or name).upper().replace(" ", "_")
    token = re.sub(r"[^A-Z0-9_]", "", token)
    if not token.startswith("CHAR_"):
        token = "CHAR_" + token
    token = re.sub(r"_+", "_", token).strip("_")
    if token == "CHAR":
        raise BibleError(f"Không tạo được id cho {name}.")
    return token[:64]


def normalize_bible(data: dict) -> dict:
    if not isinstance(data, dict):
        raise BibleError("Hồ sơ phải là một JSON object.")
    characters = data.get("characters")
    if not isinstance(characters, list) or len(characters) != 2:
        raise BibleError("Hồ sơ cần đúng hai nhân vật chính.")
    people = []
    seen: set[str] = set()
    for index, raw in enumerate(characters, start=1):
        if not isinstance(raw, dict):
            raise BibleError(f"Nhân vật {index} không hợp lệ.")
        name = _required(raw.get("name"), f"tên nhân vật {index}")[:80]
        key = _character_key(as_text(raw.get("id")), name)
        if key in seen:
            raise BibleError("Hai nhân vật không được trùng id.")
        seen.add(key)
        appearance = raw.get("appearance")
        if isinstance(appearance, dict):
            appearance = {str(key_name): as_text(item) for key_name, item in appearance.items() if as_text(item)}
            if not appearance:
                raise BibleError(f"{name} thiếu mô tả ngoại hình.")
        else:
            appearance = _required(appearance, f"ngoại hình của {name}")
        people.append(
            {
                "id": key,
                "name": name,
                "age": as_text(raw.get("age")) or "không rõ",
                "appearance": appearance,
                "default_outfit": _required(raw.get("default_outfit"), f"trang phục của {name}"),
                "signature_item": as_text(raw.get("signature_item")),
                "personality": _required(raw.get("personality"), f"tính cách của {name}"),
                "strengths": as_text(raw.get("strengths")),
                "weaknesses": as_text(raw.get("weaknesses")),
                "speaking_style": _required(raw.get("speaking_style"), f"cách nói của {name}"),
            }
        )
    return {
        "series_id": (as_text(data.get("series_id")) or "series")[:64],
        "series_name": _required(data.get("series_name"), "tên series")[:120],
        "visual_style": _required(data.get("visual_style"), "phong cách hình"),
        "characters": people,
    }


def approved_bible(project) -> dict | None:
    if project is None or project.bible_status != "approved" or not project.bible_json:
        return None
    try:
        data = json.loads(project.bible_json)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def find_person(bible: dict | None, token: str) -> dict | None:
    if not bible:
        return None
    wanted = as_text(token)
    if not wanted:
        return None
    folded = wanted.casefold()
    upper = wanted.upper().replace(" ", "_")
    for person in bible.get("characters") or []:
        if person.get("id") == upper or person.get("id") == wanted:
            return person
        if as_text(person.get("name")).casefold() == folded:
            return person
    if not upper.startswith("CHAR_"):
        return find_person(bible, "CHAR_" + upper)
    return None


def sheet_slug(character_id: str) -> str:
    token = character_id.upper()
    if token.startswith("CHAR_"):
        token = token[5:]
    slug = re.sub(r"[^A-Z0-9]+", "-", token).strip("-").lower()
    return (slug or "character")[:40]


def bible_file(project_id: str) -> Path:
    folder = Path(get_settings().data_dir) / "bibles" / project_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "series_bible.json"


def write_bible_file(project_id: str, bible: dict) -> None:
    bible_file(project_id).write_text(json.dumps(bible, ensure_ascii=False, indent=2), encoding="utf-8")


def sheet_folder(slug: str) -> Path:
    if not slug or slug in {".", ".."} or "/" in slug or "\\" in slug:
        raise BibleError("Thư mục nhân vật không hợp lệ.")
    folder = Path(get_settings().data_dir) / "characters" / slug
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_profile(slug: str, person: dict, visual_style: str) -> None:
    payload = {
        "id": person["id"],
        "name": person["name"],
        "age": person["age"],
        "appearance": person["appearance"],
        "default_outfit": person["default_outfit"],
        "signature_item": person["signature_item"],
        "personality": person["personality"],
        "strengths": person["strengths"],
        "weaknesses": person["weaknesses"],
        "speaking_style": person["speaking_style"],
        "visual_style": visual_style,
    }
    path = sheet_folder(slug) / "profile.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def slot_file(slug: str, slot: str, ext: str) -> Path:
    if slot not in SHEET_SLOTS:
        raise BibleError("Góc ảnh không hợp lệ.")
    suffix = ext if ext.startswith(".") else f".{ext}"
    return sheet_folder(slug) / f"{slot}{suffix}"


def remove_sheet(slug: str | None) -> None:
    if not slug or slug in {".", ".."} or "/" in slug or "\\" in slug:
        return
    folder = Path(get_settings().data_dir) / "characters" / slug
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def remove_series_files(project_id: str, slugs: list[str]) -> None:
    folder = Path(get_settings().data_dir) / "bibles" / project_id
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
    for slug in slugs:
        remove_sheet(slug)


def card_prompt(person: dict) -> str:
    appearance = as_text(person["appearance"])
    text = f"{appearance}\nTrang phục cố định: {person['default_outfit']}"
    if person.get("signature_item"):
        text += f"\nVật đặc trưng: {person['signature_item']}"
    return text[:4000]


def front_prompt(person: dict, visual_style: str) -> str:
    outfit = person["default_outfit"]
    if person.get("signature_item"):
        outfit = f"{outfit} Signature item: {person['signature_item']}."
    return (
        f"{as_text(person['appearance'])}\n\n"
        "Default outfit:\n"
        f"{outfit}\n"
        "No logos, no writing, no patterns.\n\n"
        "Expression and pose:\n"
        "Friendly neutral expression, standing upright, "
        "arms slightly away from the body, both hands visible.\n\n"
        "Visual style:\n"
        f"{visual_style}\n\n"
        "Composition:\n"
        "One character only, full body entirely visible, "
        "plain light-gray background, eye-level camera.\n\n"
        "Keep the design simple and reproducible."
    )


def angle_prompt(slot: str, visual_style: str) -> str:
    views = {
        "side": (
            "Side view of the same character, full body entirely visible, standing upright, "
            "plain light-gray background, eye-level camera."
        ),
        "back": (
            "Back view of the same character, full body entirely visible, standing upright, "
            "plain light-gray background, eye-level camera."
        ),
        "expressions": (
            "The same character on one sheet, several clear expressions: friendly smile, surprised, "
            "worried, laughing. Same outfit and proportions in every expression. Plain light-gray background."
        ),
    }
    return (
        "Same character as the reference image. Exact same face, hair, body proportions, "
        "outfit colors and signature item. Do not redesign.\n\n"
        f"{views[slot]}\n\n"
        "Visual style:\n"
        f"{visual_style}\n\n"
        "Keep the design simple and reproducible. No logos, no writing, no patterns."
    )


def scene_image_prompt(bible: dict, scene: dict, people: list[dict]) -> str:
    lines = ["Reference mapping:"]
    for index, person in enumerate(people, start=1):
        outfit = as_text(person.get("default_outfit"))
        lines.append(f"Image {index} = {person['id']}, {person['name']}, {outfit}.")
    lines.extend(
        [
            "",
            "Preserve each character's identity, facial proportions, hairstyle, body proportions and exact default outfit.",
            "",
            "Scene:",
            _scene_body(scene),
            "",
            "The image is the first frame, already in the opening pose, hand, prop and motion. It is not a neutral portrait.",
            "",
            "Framing:",
            as_text(scene.get("camera")) or "Medium shot. Both faces and hands visible.",
            "",
            "No subtitles, no speech bubbles, no readable text.",
        ]
    )
    return "\n".join(lines)


def video_prompt(scene: dict) -> str:
    duration = int(scene.get("duration_seconds") or 5)
    lines = [
        f"Animate the supplied image into a {duration}-second shot.",
        "",
        "Keep the same characters, outfits, layout, objects and lighting as the input image.",
        "",
        "Edit buffer: a short settle at the very start and a short settle at the very end, so the edit can trim. Finish the action and the spoken line before the end buffer.",
    ]
    start = as_text(scene.get("start_state"))
    end = as_text(scene.get("end_state"))
    join = as_text(scene.get("join"))
    if start:
        lines.extend(["", "Opening pose, active hand, prop and motion direction:", start])
    if join == "continue":
        lines.extend(
            [
                "",
                "This shot continues the previous action. Begin already in that pose, hand, prop and motion direction.",
            ]
        )
    elif join == "new_angle":
        note = as_text(scene.get("cut_note"))
        lines.extend(["", "This is a new angle or location.", note])
    lines.extend(["", "Action:", as_text(scene.get("main_action"))])
    dialogue = scene.get("dialogue") or []
    spoken = []
    for line in dialogue:
        if not isinstance(line, dict):
            continue
        text = as_text(line.get("text"))
        if text:
            spoken.append(f"{as_text(line.get('character_id'))}: {text}")
    if spoken:
        lines.extend(["", "Spoken dialogue only, no on-screen text. Finish the line before the end buffer:", *spoken])
    if end:
        lines.extend(["", "End pose before the final buffer:", end])
    camera = as_text(scene.get("camera")) or "Locked shot"
    lines.extend(
        [
            "",
            "Keep the motion going into the end buffer. Do not stop to pose for the camera.",
            "Camera:",
            f"{camera}. No cuts.",
            "No subtitles, no readable text.",
        ]
    )
    return "\n".join(lines)


def aspect_from_camera(camera: str) -> str:
    text = camera.lower()
    if "9:16" in text or "vertical" in text or "dọc" in text:
        return "9:16"
    if "1:1" in text:
        return "1:1"
    return "16:9"


def _scene_body(scene: dict) -> str:
    parts = []
    location = as_text(scene.get("location_id")).replace("_", " ")
    if location:
        parts.append(f"Location: {location}.")
    start = as_text(scene.get("start_state"))
    if start:
        parts.append(start)
    action = as_text(scene.get("main_action"))
    if action:
        parts.append(action)
    return " ".join(parts)
