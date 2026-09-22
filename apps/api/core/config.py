"""应用配置。

读取优先级：真实环境变量 > .env 文件 > 代码默认值。
字段名自动映射为大写环境变量（`llm_api_key` ← `LLM_API_KEY`）。

为什么用 pydantic-settings 而不是直接 os.getenv：
    配置项会在启动时一次性校验类型，缺失/写错能立刻发现，
    而不是等到某个请求打到一半才炸。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # .env 里有后续里程碑才用到的变量，未声明的直接忽略而不报错
        extra="ignore",
    )

    # ---------------- 应用 ----------------
    app_env: str = "dev"
    log_level: str = "INFO"
    cors_origins: str = "*"

    # ---------------- LLM（OpenAI 兼容，M0）----------------
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_s: float = 60.0

    # ---------------- 数据库与 Redis（M1 起启用）----------------
    # 三个角色分离，见设计文档 v1.1 ADR-9：
    #   app_api     受 RLS 约束
    #   app_worker  BYPASSRLS（跨租户扫描定时任务，M9 使用）
    #   app_owner   拥有表所有权，仅用于迁移
    database_url: str = ""
    database_url_worker: str = ""
    database_url_migrate: str = ""
    redis_url: str = "redis://redis:6379/0"

    # ---------------- 鉴权（M1）----------------
    # 生成方式：openssl rand -hex 32
    jwt_secret: str = ""
    access_ttl_min: int = 30
    refresh_ttl_days: int = 7

    # ---------------- 幂等与会话锁（M1，§7.4 / §7.6）----------------
    idempotency_ttl_s: int = 86400          # Idempotency-Key 结果缓存 24h
    conversation_lock_ttl_ms: int = 300_000  # 会话锁 TTL 5 分钟（长任务由持有者续期，M5 实现）

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的来源串拆成列表。"""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def llm_configured(self) -> bool:
        """LLM 是否已配置齐全。`/ready` 与 chat 路由都依赖它。"""
        return bool(self.llm_api_key and self.llm_model)

    @property
    def db_configured(self) -> bool:
        return bool(self.database_url)

    @property
    def jwt_configured(self) -> bool:
        return bool(self.jwt_secret)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例，避免每次请求都重新解析 .env。"""
    return Settings()


# 模块级单例：绝大多数场景直接 `from ... import settings`
settings = get_settings()
