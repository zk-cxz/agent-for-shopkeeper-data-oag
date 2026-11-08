"""数仓访问：异步 MySQL（SQLAlchemy 2.0）"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import app_config


class DW:
    def __init__(self, dsn: str | None = None):
        self._dsn = dsn or str(app_config.dw.dsn)
        self._engine: AsyncEngine | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(self._dsn, pool_pre_ping=True)
        return self._engine

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def validate(self, sql: str) -> None:
        """用 EXPLAIN 做语法/语义校验，不执行真实查询"""
        async with self.engine.connect() as conn:
            await conn.execute(text(f"EXPLAIN {sql}"))

    async def fetch(self, sql: str) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(text(sql))).mappings().fetchall()
        return [{k: _jsonable(v) for k, v in row.items()} for row in rows]

    async def info(self) -> dict[str, str]:
        async with self.engine.connect() as conn:
            version = (await conn.execute(text("select version()"))).scalar()
            dialect = conn.engine.dialect.name
        return {"dialect": dialect, "version": str(version)}


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


dw = DW()
