"""平台配置。

一套代码、三种部署形态（规范 9.1）：通过 ``REVIEWDESK_PROFILE`` 在
``local`` / ``server`` / ``cloud`` 之间切换。不同形态只影响各抽象接口的默认实现：

============  =====================  ==================  ==============  ==========
profile       关系库                  对象存储             任务执行         账号
============  =====================  ==================  ==============  ==========
local         SQLite                 本地文件夹           进程内 worker     单用户自动登录
server        PostgreSQL             MinIO / 本地卷       独立 worker      多用户（学生/导师/管理员）
cloud         托管 PostgreSQL         S3 兼容存储          独立 worker      多租户、SSO 预留
============  =====================  ==================  ==============  ==========
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Profile = Literal["local", "server", "cloud"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REVIEWDESK_", env_file=".env", extra="ignore")

    profile: Profile = "local"
    data_dir: Path = Path("./data")

    # 关系库：留空时 local 使用 data_dir 下的 SQLite
    database_url: str | None = None

    # 对象存储：local | s3
    storage_backend: Literal["local", "s3"] | None = None
    s3_endpoint: str | None = None
    s3_bucket: str = "reviewdesk"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"

    # 任务执行：inline（进程内线程）| external（独立 worker 进程轮询 jobs 表）
    worker_mode: Literal["inline", "external"] | None = None

    # 安全
    secret_key: str = "change-me-in-production-please-use-32+chars"
    token_ttl_hours: int = 24 * 7
    # local 形态下自动以内置单用户登录
    single_user: bool | None = None
    bootstrap_admin_email: str = "admin@local"
    bootstrap_admin_password: str = "admin"

    # 模型网关默认值（也可在“模型网关配置”页面按任务配置）
    default_provider: str = "rules"  # rules（离线规则引擎）| cloud | ollama | 已配置的提供方名称
    openai_base_url: str | None = None  # 例如 https://api.deepseek.com/v1
    openai_api_key: str | None = None
    openai_model: str = "deepseek-chat"
    ollama_base_url: str | None = None  # 例如 http://localhost:11434/v1
    ollama_model: str = "qwen2.5:14b"

    # 外部连接器
    ncbi_api_key: str | None = None
    ncbi_email: str | None = None
    crossref_mailto: str | None = None
    unpaywall_email: str | None = None
    openalex_api_key: str | None = None
    grobid_url: str | None = None
    zotero_api_base: str = "https://api.zotero.org"
    http_timeout: float = 30.0
    # 离线模式：禁止所有外部 HTTP（单机离线或纯内网部署）
    offline: bool = False

    cors_origins: list[str] = ["*"]
    static_dir: Path | None = None  # 前端构建产物目录

    # ---- 派生属性 ----
    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'reviewdesk.db').resolve()}"

    @property
    def resolved_storage_backend(self) -> str:
        if self.storage_backend:
            return self.storage_backend
        return "s3" if self.profile == "cloud" else "local"

    @property
    def resolved_worker_mode(self) -> str:
        if self.worker_mode:
            return self.worker_mode
        return "inline" if self.profile == "local" else "external"

    @property
    def resolved_single_user(self) -> bool:
        if self.single_user is not None:
            return self.single_user
        return self.profile == "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()
