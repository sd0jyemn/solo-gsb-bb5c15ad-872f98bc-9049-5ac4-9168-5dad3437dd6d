"""封签链校验 API 入口。"""
from __future__ import annotations

from fastapi import FastAPI

from .batch import validate_batch
from .models import (
    BatchValidateRequest,
    BatchValidateResponse,
    ReconcileValidateRequest,
    ReconcileValidateResponse,
    ValidateRequest,
    ValidateResponse,
)
from .reconcile import reconcile_validate
from .validator import validate_events

app = FastAPI(
    title="封签链校验 API",
    description="用于展品运输交接场景的封签链状态机校验服务",
    version="1.2.0",
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


@app.post(
    "/api/v1/seal-chain/reconcile-validate",
    response_model=ReconcileValidateResponse,
    tags=["seal-chain"],
    summary="离线片段对账：按 event_id 去重并按 prev_event_id 重建后校验",
)
def reconcile(request: ReconcileValidateRequest) -> ReconcileValidateResponse:
    """对同一 chain_id 的多个离线片段去重，仅按 prev_event_id 链接重建唯一序列。

    同 event_id 内容完全相同则合并；业务字段不同则返回冲突明细。
    明确报告多头、分叉、缺失前驱与环，绝不按时间戳猜序；
    仅当唯一序列重建成功时才调用单链状态机校验器，
    并返回每个事件的来源片段编号列表。
    """
    return reconcile_validate(request)
