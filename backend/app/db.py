from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
# Dashboard + inbox fire many parallel requests; default pool_size=5 starved under load
# and left clients hanging until the 15s browser timeout.
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=15,
    max_overflow=25,
    pool_timeout=10,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
