import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.db.base import Base

# Determine if we're connecting to a local database
is_local_db = "localhost" in settings.DATABASE_URL or "127.0.0.1" in settings.DATABASE_URL

# Configure connection arguments based on environment
if is_local_db:
    # Local database - no SSL required
    connect_args = {}
else:
    # Azure or remote database - require SSL with keepalives
    connect_args = {
        "sslmode": "require",
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }

# Connection budget. Endpoints run in the threadpool, so many requests can hold a
# connection at once. These sizes assume PgBouncer sits in front of Postgres and
# multiplexes them onto far fewer real server connections — point DATABASE_URL at the
# PgBouncer port (6432 on Azure Flexible Server), not directly at Postgres on 5432.
# Without PgBouncer in the path, lower these: 2 API workers x 25 exceeds a B1ms's ~35
# max_connections. Override per deployment via env.
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "15"))
DB_POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "30"))

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,      # health-check connections before use
    pool_recycle=1800,       # recycle connections every 30 minutes to avoid stale SSL
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,  # queue for a free connection instead of failing fast
    connect_args=connect_args
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
