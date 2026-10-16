import asyncio
from pathlib import Path


class AssembleError(Exception):
    pass


async def _run(command: list[str]) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AssembleError("Máy chưa có ffmpeg để ghép video.") from exc
    stdout, stderr = await process.communicate()
    text = (stderr or stdout or b"").decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        raise AssembleError(text[-500:] or "ffmpeg thất bại khi ghép video.")
    return text


async def _video_size(path: Path) -> tuple[int, int]:
    text = await _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(path),
        ]
    )
    width_text, _, height_text = text.partition("x")
    try:
        width = int(width_text)
        height = int(height_text)
    except ValueError as exc:
        raise AssembleError("Không đọc được kích thước video.") from exc
    if width < 2 or height < 2:
        raise AssembleError("Video nguồn không hợp lệ.")
    return width + (width % 2), height + (height % 2)


async def _duration(path: Path) -> float:
    text = await _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ]
    )
    try:
        duration = float(text.splitlines()[0])
    except (ValueError, IndexError) as exc:
        raise AssembleError("Không đọc được độ dài video.") from exc
    if duration < 0.5:
        raise AssembleError("Video quá ngắn để ghép nhạc.")
    return duration


async def _has_audio(path: Path) -> bool:
    text = await _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ]
    )
    return "audio" in text


async def _mix_music(video: Path, music: Path, volume: float) -> None:
    if not await _has_audio(music):
        raise AssembleError("File nhạc không có tiếng.")
    duration = await _duration(video)
    fade = min(1.2, max(0.2, duration / 8))
    fade_out = max(0.0, duration - fade)
    mixed = video.with_name(f"{video.stem}-music.mp4")
    filter_complex = (
        f"[1:a:0]aformat=sample_rates=48000:channel_layouts=stereo,"
        f"volume={volume:.3f},"
        f"afade=t=in:st=0:d={fade:.3f},"
        f"afade=t=out:st={fade_out:.3f}:d={fade:.3f},"
        f"atrim=0:{duration:.3f}[m];"
        f"[0:a:0][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
    )
    try:
        await _run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video),
                "-stream_loop",
                "-1",
                "-i",
                str(music),
                "-filter_complex",
                filter_complex,
                "-map",
                "0:v:0",
                "-map",
                "[a]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                str(mixed),
            ]
        )
        if not mixed.is_file() or mixed.stat().st_size < 1000:
            raise AssembleError("File ghép nhạc ra rỗng.")
        mixed.replace(video)
    except Exception:
        mixed.unlink(missing_ok=True)
        raise


async def concat_videos(
    paths: list[Path],
    destination: Path,
    music: Path | None = None,
    music_volume: float = 0.15,
) -> None:
    if len(paths) < 2:
        raise AssembleError("Cần ít nhất hai video để ghép.")
    for path in paths:
        if not path.is_file():
            raise AssembleError(f"Thiếu file {path.name}.")
    width, height = await _video_size(paths[0])
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-y"]
    for path in paths:
        command.extend(["-i", str(path)])
    filters = []
    for index in range(len(paths)):
        filters.append(
            f"[{index}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p[v{index}]"
        )
        filters.append(
            f"[{index}:a:0]aformat=sample_rates=48000:channel_layouts=stereo[a{index}]"
        )
    joined = "".join(f"[v{index}][a{index}]" for index in range(len(paths)))
    filters.append(f"{joined}concat=n={len(paths)}:v=1:a=1[v][a]")
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(destination),
        ]
    )
    await _run(command)
    if not destination.is_file() or destination.stat().st_size < 1000:
        raise AssembleError("File ghép ra rỗng.")
    if music is not None:
        await _mix_music(destination, music, music_volume)
