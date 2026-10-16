from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "mysql+aiomysql://seedance:seedance@mysql:3306/seedance?charset=utf8mb4"
    )
    public_backend_url: str = "http://localhost:8080"
    frontend_url: str = "http://localhost:8080"
    data_dir: str = "/data"
    seedance_api_key: str = ""
    seedance_base_url: str = "https://openrouter.ai/api/v1"
    google_client_id: str = ""
    google_client_secret: str = ""
    worker_interval_seconds: float = 8

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_backend_url.rstrip('/')}/api/youtube/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()
