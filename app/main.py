"""封签链校验 API 入口。"""
from __future__ import annotations

from fastapi import FastAPI

from .batch import validate_batch
from .models import (
    BatchValidateRequest,
    BatchValidateResponse,
    ValidateRequest,
    ValidateResponse,
)
from .validator import validate_events

app = FastAPI(
    title="封签链校验 API",
    description="用于展品运输交接场景的封签链状态机校验服务",
    version="1.1.0",
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    """健康检查。"""
    return {"status": "ok"}


@app.post(
    "/api/v1/seal-chain/validate",
    response_model=ValidateResponse,
    tags=["seal-chain"],
    summary="校验封签链事件序列",
)
def validate(request: ValidateRequest) -> ValidateResponse:
    """按状态机逐事件校验，返回最终保管人、当前封签、逐事件状态与全部违规。"""
    return validate_events(request.events)


@app.post(
    "/api/v1/seal-chain/batch-validate",
    response_model=BatchValidateResponse,
    tags=["seal-chain"],
    summary="批量封签占用检查（跨链冲突）",
)
def batch_validate(request: BatchValidateRequest) -> BatchValidateResponse:
    """先分别校验每条链，再按输入顺序建立全批次封签占用表，标记跨链占用冲突。"""
    return validate_batch(request)
