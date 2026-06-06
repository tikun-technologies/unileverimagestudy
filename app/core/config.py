from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, AliasChoices

class Settings(BaseSettings):
    # Pydantic Settings config (v2)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    SECRET_KEY: str
    
    # JWT Settings
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080  # 7 days in minutes
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Cloudinary Settings
    CLOUDINARY_CLOUD_NAME: str | None = None
    CLOUDINARY_API_KEY: str | None = None
    CLOUDINARY_API_SECRET: str | None = None
    CLOUDINARY_FOLDER: str = "mindsurve"
    
    # Azure Blob Storage Settings (support multiple env var names via validation aliases)
    AZURE_STORAGE_CONNECTION_STRING: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AZURE_STORAGE_CONNECTION_STRING",
            "azure_storage_connection_string",
        ),
    )
    AZURE_STORAGE_ACCOUNT_NAME: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AZURE_STORAGE_ACCOUNT_NAME",
            "azure_storage_account_name",
        ),
    )
    AZURE_STORAGE_ACCOUNT_KEY: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AZURE_STORAGE_ACCOUNT_KEY",
            "azure_storage_account_key",
        ),
    )
    AZURE_STORAGE_CONTAINER: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AZURE_STORAGE_CONTAINER",
            "AZURE_STORAGE_CONTAINER_NAME",
            "azure_storage_container_name",
            "azure_storage_container",
        ),
    )
    AZURE_STORAGE_PUBLIC_BASE_URL: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AZURE_STORAGE_PUBLIC_BASE_URL",
            "azure_storage_public_base_url",
            "AZURE_STORAGE_BASE_URL",
            "azure_storage_base_url",
        ),
    )  # Optional override for CDN/frontdoor
    AZURE_STORAGE_SAS_TOKEN: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AZURE_STORAGE_SAS_TOKEN",
            "azure_storage_sas_token",
            "AZURE_BLOB_SAS_TOKEN",
            "azure_blob_sas_token",
        ),
    )  # Optional, appended to URLs for private containers

    # Application Insights (optional; set in .env to send telemetry to Azure Monitor)
    APPLICATIONINSIGHTS_CONNECTION_STRING: str | None = None
    
    # Application Settings
    BASE_URL: str = "https://mindsurve.com"  # Base URL for share links in frontend
    RUN_API_BACKGROUND_SERVICES: bool = True
    
    # Task Generation Settings
    TASK_GENERATION_TIMEOUT: int = 12000  # 200 minutes timeout for task generation (Azure max is ~4-5 min for sync requests)
    MAX_RESPONDENTS_FOR_SYNC: int = 0  # Use async processing for studies with more than 4 respondents
    TASK_GENERATION_CHUNK_SIZE: int = 50  # Process respondents in smaller chunks for memory efficiency
    MAX_MEMORY_USAGE_MB: int = 1024  # 1GB memory limit for task generation (matches Azure P2 plan)
    
    # Redis Settings (for pub/sub and app cache only — not Celery broker)
    REDIS_URL: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "REDIS_URL",
            "redis_url",
        ),
    )

    # Celery: RabbitMQ (AMQP) broker (same URL for dev and prod is fine until you split instances)
    CELERY_BROKER_URL: str | None = None
    # Celery result backend: rpc:// uses RabbitMQ (no Redis). Or set e.g. redis://...
    CELERY_RESULT_BACKEND: str | None = Field(
        default=None,
        description="Default rpc:// when using RabbitMQ broker",
    )

    # Celery: when True, dispatch task generation and synthetic simulation to Celery workers
    USE_CELERY: bool = False

    def get_celery_broker_url(self) -> str:
        """RabbitMQ broker URL for Celery."""
        url = (self.CELERY_BROKER_URL or "").strip()
        if not url:
            raise ValueError("Set CELERY_BROKER_URL in .env for Celery.")
        return url

    def get_celery_result_backend(self) -> str:
        """Result backend: explicit CELERY_RESULT_BACKEND or rpc:// (RabbitMQ-friendly)."""
        if self.CELERY_RESULT_BACKEND and self.CELERY_RESULT_BACKEND.strip():
            return self.CELERY_RESULT_BACKEND.strip()
        return "rpc://"
    
    # SMTP Email Settings
    SMTP_SERVER: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    FROM_EMAIL: str 
    APP_NAME: str = "mindsurve"
    FRONTEND_URL: str = "https://mindsurve.com"
    MERGE_STUDY_A: str | None = None
    MERGE_STUDY_B: str | None = None

    # Stripe / Billing
    STRIPE_SECRET_KEY: str | None = None
    STRIPE_WEBHOOK_SECRET: str | None = None
    STRIPE_PRO_PRICE_ID: str | None = None
    STRIPE_ENTERPRISE_PRICE_ID: str | None = None
    BILLING_CURRENCY: str = "usd"
    PLATFORM_BASE_FEE_CENTS: int = 1000
    PRO_MONTHLY_FEE_CENTS: int = 9900
    PRO_WAIVE_PLATFORM_FEE: bool = False

    # Plan limits — grid/text/hybrid use categories + elements; layer uses layers + images per layer
    FREE_AI_RESPONDENT_LIMIT: int = 50
    FREE_MAX_CATEGORIES: int = 4
    FREE_MAX_ELEMENTS_PER_CATEGORY: int = 4
    FREE_MAX_LAYERS: int = 4
    FREE_MAX_IMAGES_PER_LAYER: int = 4
    PRO_AI_RESPONDENT_LIMIT: int = 999999
    PRO_MAX_CATEGORIES: int = 8
    PRO_MAX_ELEMENTS_PER_CATEGORY: int = 8
    PRO_MAX_LAYERS: int = 8
    PRO_MAX_IMAGES_PER_LAYER: int = 8

settings = Settings()
