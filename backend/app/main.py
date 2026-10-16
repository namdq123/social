import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import get_settings
from app.database import Base, engine
from app.models import AppSetting, Character, CharacterImage, Episode, Job, Project, Scene, YoutubeAccount  # noqa: F401
from app.routers import episodes, jobs, projects, series, settings_api, youtube_auth
from app.services.worker import recover_stuck_jobs, worker_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def init_db() -> None:
    last_error: Exception | None = None
    for attempt in range(30):
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await ensure_job_project_column(connection)
                await ensure_job_scene_column(connection)
                await ensure_series_columns(connection)
            return
        except Exception as exc:
            last_error = exc
            logger.warning("Waiting for MySQL (%s/30): %s", attempt + 1, exc)
            await asyncio.sleep(2)
    raise RuntimeError("MySQL is not ready") from last_error


async def ensure_job_project_column(connection) -> None:
    rows = await connection.execute(text("SHOW COLUMNS FROM jobs LIKE 'project_id'"))
    if rows.first() is not None:
        return
    await connection.execute(text("ALTER TABLE jobs ADD COLUMN project_id VARCHAR(36) NULL"))
    await connection.execute(text("CREATE INDEX ix_jobs_project_id ON jobs (project_id)"))


async def ensure_series_columns(connection) -> None:
    statements = [
        ("projects", "bible_json", "bible_json MEDIUMTEXT NULL"),
        ("projects", "bible_status", "bible_status VARCHAR(16) NOT NULL DEFAULT ''"),
        ("projects", "story_state", "story_state MEDIUMTEXT NULL"),
        ("characters", "bible_key", "bible_key VARCHAR(64) NULL"),
        ("characters", "sheet_dir", "sheet_dir VARCHAR(80) NULL"),
        ("character_images", "slot", "slot VARCHAR(16) NOT NULL DEFAULT ''"),
        ("episodes", "summary", "summary TEXT NULL"),
        ("episodes", "script_json", "script_json MEDIUMTEXT NULL"),
        ("episodes", "status", "status VARCHAR(16) NOT NULL DEFAULT 'draft'"),
        ("scenes", "duration_seconds", "duration_seconds INT NOT NULL DEFAULT 0"),
        ("scenes", "script_json", "script_json MEDIUMTEXT NULL"),
    ]
    for table, column, ddl in statements:
        rows = await connection.execute(text(f"SHOW COLUMNS FROM {table} LIKE '{column}'"))
        if rows.first() is None:
            await connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


async def ensure_job_scene_column(connection) -> None:
    rows = await connection.execute(text("SHOW COLUMNS FROM jobs LIKE 'scene_id'"))
    if rows.first() is not None:
        return
    await connection.execute(text("ALTER TABLE jobs ADD COLUMN scene_id VARCHAR(36) NULL"))
    await connection.execute(text("CREATE INDEX ix_jobs_scene_id ON jobs (scene_id)"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    Path(settings.data_dir, "videos").mkdir(parents=True, exist_ok=True)
    Path(settings.data_dir, "uploads").mkdir(parents=True, exist_ok=True)
    Path(settings.data_dir, "characters").mkdir(parents=True, exist_ok=True)
    Path(settings.data_dir, "scenes").mkdir(parents=True, exist_ok=True)
    Path(settings.data_dir, "bibles").mkdir(parents=True, exist_ok=True)
    await init_db()
    await recover_stuck_jobs()
    task = asyncio.create_task(worker_loop())
    logger.info("Seedance Studio is ready")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await engine.dispose()


settings = get_settings()
app = FastAPI(title="Seedance Studio", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_url.rstrip("/"),
        "http://localhost:8080",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(settings_api.router, prefix="/api")
app.include_router(youtube_auth.router, prefix="/api")
app.include_router(projects.router, prefix="/api")
app.include_router(series.router, prefix="/api")
app.include_router(episodes.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}
