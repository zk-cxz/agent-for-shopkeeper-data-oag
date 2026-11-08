"""HTTP 接口：SSE 流式问数 + 本体概览 + 极简调试页"""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import app_config
from app.log import logger
from app.pipeline.run import get_pipeline

app = FastAPI(title="Shopkeeper Agent · OAG", version="1.0.0")


class QueryIn(BaseModel):
    query: str


@app.get("/health")
async def health() -> dict:
    onto = get_pipeline().onto
    return {
        "status": "ok",
        "mode": "oag",
        "ontology_version": onto.version,
        "objects": {
            "tables": len(onto.tables),
            "columns": len(onto.columns),
            "metrics": len(onto.metrics),
            "relations": len(onto.foreign_keys),
            "values": sum(len(c.values) for c in onto.columns.values()),
        },
        "resolve_mode": str(app_config.ontology.resolve_mode),
    }


@app.get("/api/ontology")
async def ontology() -> dict:
    """以只读形式暴露本体，便于演示知识底座长什么样"""
    onto = get_pipeline().onto
    return {
        "version": onto.version,
        "domain": onto.domain,
        "data_range": onto.data_range,
        "tables": {
            k: {"kind": v.kind, "grain": v.grain, "description": v.description}
            for k, v in onto.tables.items()
        },
        "metrics": {
            k: {"display": v.display, "formula": v.formula, "depends_on": v.depends_on}
            for k, v in onto.metrics.items()
        },
        "relations": [f"{f.from_column} = {f.to_column}" for f in onto.foreign_keys],
        "enum_domains": {
            c.id: c.values for c in onto.columns.values() if c.values
        },
    }


@app.post("/api/query")
async def query(body: QueryIn) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue()

    async def worker() -> None:
        def emit(event: dict) -> None:
            queue.put_nowait(event)

        try:
            result = await get_pipeline().run(body.query, emit=emit)
        except Exception as exc:  # noqa: BLE001
            logger.exception("查询失败")
            queue.put_nowait({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            await queue.put(None)
            return

        if result.refused or (result.message and not result.rows):
            queue.put_nowait(
                {
                    "type": "error",
                    "message": result.message,
                    "refused": result.refused,
                    "unresolved": result.resolution.unresolved,
                }
            )
        else:
            queue.put_nowait(
                {
                    "type": "result",
                    "data": result.rows,
                    "columns": result.columns,
                    "sql": result.sql,
                    "latency_ms": result.latency_ms,
                    "llm_calls": result.llm_calls,
                }
            )
        await queue.put(None)

    async def stream():
        task = asyncio.create_task(worker())
        while True:
            item = await queue.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        await task

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/")
async def root() -> dict:
    """后端只提供 API；界面是同构的 React 前端（Vite dev server）"""
    return {
        "service": "shopkeeper-agent-oag",
        "ui": "http://127.0.0.1:5174/",
        "docs": "/docs",
        "ontology": "/api/ontology",
        "health": "/health",
    }
