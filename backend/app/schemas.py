from datetime import datetime

from pydantic import BaseModel, Field, field_serializer


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class ProjectOut(BaseModel):
    id: str
    name: str
    description: str
    character_count: int
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class CharacterImageOut(BaseModel):
    id: str
    character_id: str
    label: str
    slot: str = ""
    prompt: str
    source: str
    status: str
    status_label: str
    error_message: str | None
    has_image: bool


class CharacterOut(BaseModel):
    id: str
    project_id: str
    name: str
    bible_key: str | None = None
    prompt: str
    aspect_ratio: str
    source: str
    status: str
    status_label: str
    error_message: str | None
    has_image: bool
    images: list[CharacterImageOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class EpisodeIn(BaseModel):
    number: int = Field(default=1, ge=1, le=999)
    title: str = Field(min_length=1, max_length=160)
    script: str = Field(default="", max_length=20000)


class EpisodeOut(BaseModel):
    id: str
    project_id: str
    number: int
    title: str
    script: str
    summary: str = ""
    status: str = "draft"
    scene_count: int
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class SceneOut(BaseModel):
    id: str
    episode_id: str
    project_id: str
    sort_order: int
    duration_seconds: int = 0
    prompt: str
    aspect_ratio: str
    status: str
    status_label: str
    join: str = ""
    cut_note: str = ""
    error_message: str | None
    has_image: bool
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class JobOut(BaseModel):
    id: str
    project_id: str | None = None
    scene_id: str | None = None
    prompt: str
    model: str
    resolution: str
    ratio: str
    duration: int
    generate_audio: bool
    watermark: bool
    has_image: bool
    seedance_task_id: str | None
    seedance_status: str | None
    has_video: bool
    youtube_title: str
    youtube_description: str
    youtube_tags: str
    youtube_privacy: str
    youtube_category_id: str
    auto_publish: bool
    youtube_video_id: str | None
    youtube_url: str | None
    status: str
    status_label: str
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class SettingsOut(BaseModel):
    seedance_api_key_set: bool
    seedance_base_url: str
    google_client_id: str
    google_client_secret_set: bool
    redirect_uri: str
    frontend_url: str


class SettingsIn(BaseModel):
    seedance_api_key: str | None = None
    clear_seedance_api_key: bool = False
    seedance_base_url: str | None = None
    google_client_id: str | None = None
    clear_google_client_id: bool = False
    google_client_secret: str | None = None
    clear_google_client_secret: bool = False


class YoutubeStatusOut(BaseModel):
    connected: bool
    channel_id: str | None = None
    channel_title: str | None = None


class MetaOut(BaseModel):
    models: list[dict]
    text_models: list[dict] = Field(default_factory=list)
    continuity_rules: str = ""
    ratios: list[str]
    resolutions: list[str]
    categories: list[dict]
    privacy: list[dict]
    redirect_uri: str
    status_labels: dict[str, str]


class SeedanceTestOut(BaseModel):
    ok: bool
    message: str


class MessageOut(BaseModel):
    message: str = Field(default="ok")


class BibleOut(BaseModel):
    status: str
    model: str
    prompt: str
    bible: dict | None = None


class BibleIn(BaseModel):
    bible: dict


class StoryOut(BaseModel):
    current_state: str
    used_topics: list[str]
    episodes: list[dict]


class StoryIn(BaseModel):
    current_state: str = Field(default="", max_length=4000)
    used_topics: list[str] = Field(default_factory=list)


class EpisodeScriptIn(BaseModel):
    episode: dict


class EpisodePlanIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    scene_count: int = Field(ge=1, le=12)
    model: str = ""
    custom_model: str = Field(default="", max_length=128)
    continuity: str = Field(default="", max_length=8000)
