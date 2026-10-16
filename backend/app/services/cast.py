from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pathlib import Path

from app.models import Character, CharacterImage


async def pending_portrait_count(session: AsyncSession, project_id: str) -> int:
    characters = await session.scalar(
        select(func.count())
        .select_from(Character)
        .where(Character.project_id == project_id, Character.status.in_(("queued", "generating")))
    )
    images = await session.scalar(
        select(func.count())
        .select_from(CharacterImage)
        .where(
            CharacterImage.project_id == project_id,
            CharacterImage.status.in_(("queued", "generating")),
        )
    )
    return int(characters or 0) + int(images or 0)


async def reference_images(session: AsyncSession, project_id: str, limit: int = 3) -> list[tuple[str, str]]:
    characters = (
        await session.scalars(
            select(Character)
            .where(Character.project_id == project_id, Character.status == "ready")
            .order_by(Character.created_at.asc())
        )
    ).all()
    images = (
        await session.scalars(
            select(CharacterImage)
            .where(CharacterImage.project_id == project_id, CharacterImage.status.in_(("ready", "approved")))
            .order_by(CharacterImage.sort_order.asc(), CharacterImage.created_at.asc())
        )
    ).all()
    names = {item.id: item.name for item in characters}
    grouped: dict[str, list[CharacterImage]] = {}
    order: list[str] = []
    for image in images:
        if not image.image_path or image.character_id not in names:
            continue
        if image.character_id not in grouped:
            order.append(image.character_id)
            grouped[image.character_id] = []
        grouped[image.character_id].append(image)
    for character in characters:
        if character.id in grouped or not character.image_path:
            continue
        order.append(character.id)
        grouped[character.id] = []

    def ordered(character_id: str) -> list[tuple[str, str]]:
        rows = sorted(
            grouped.get(character_id) or [],
            key=lambda image: (
                0 if image.slot == "front" and image.status == "approved" else 1,
                0 if image.slot == "front" else 1,
                image.sort_order,
            ),
        )
        pairs = [
            (names[character_id], image.image_path)
            for image in rows
            if image.image_path and Path(image.image_path).is_file()
        ]
        character = next(item for item in characters if item.id == character_id)
        if not pairs and character.image_path and Path(character.image_path).is_file():
            pairs = [(character.name, character.image_path)]
        return pairs

    chosen: list[tuple[str, str]] = []
    primaries = {character_id: ordered(character_id) for character_id in order}
    for character_id in order:
        if len(chosen) >= limit or not primaries[character_id]:
            continue
        chosen.append(primaries[character_id][0])
    for character_id in order:
        for extra in primaries[character_id][1:]:
            if len(chosen) >= limit:
                break
            chosen.append(extra)
    return chosen[:limit]


async def approved_fronts(
    session: AsyncSession,
    project_id: str,
    keys: list[str],
) -> list[tuple[str, str, str]] | None:
    if not keys:
        return None
    characters = (
        await session.scalars(select(Character).where(Character.project_id == project_id))
    ).all()
    by_key = {item.bible_key: item for item in characters if item.bible_key}
    images = (
        await session.scalars(
            select(CharacterImage).where(
                CharacterImage.project_id == project_id,
                CharacterImage.slot == "front",
                CharacterImage.status == "approved",
            )
        )
    ).all()
    front_by_character = {
        image.character_id: image
        for image in images
        if image.image_path and Path(image.image_path).is_file()
    }
    resolved: list[tuple[str, str, str]] = []
    for key in keys:
        character = by_key.get(key)
        if character is None:
            return None
        front = front_by_character.get(character.id)
        if front is None:
            return None
        resolved.append((key, character.name, front.image_path))
    return resolved
