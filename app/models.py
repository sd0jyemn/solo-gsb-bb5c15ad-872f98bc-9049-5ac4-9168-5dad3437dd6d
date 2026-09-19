"""请求 / 响应数据模型。"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class EventType(str, Enum):
    """封签链支持的四种事件类型。"""

    SEAL = "seal"          # 封装：首次（或开封后）以封签封装展品
    HANDOVER = "handover"  # 交接：保管人变更
    UNSEAL = "unseal"      # 开封：拆除当前封签
    RESEAL = "reseal"      # 重封：开封后使用新封签重新封装


class Event(BaseModel):
    """单个封签链事件。timestamp 使用 ISO 8601 格式。"""

    type: EventType
    timestamp: datetime
    seal_id: Optional[str] = Field(default=None, description="封签编号，seal/reseal 事件必填")
    custodian: Optional[str] = Field(default=None, description="保管人，seal/reseal/handover 事件必填")


class ValidateRequest(BaseModel):
    """校验请求：按时间排列的事件列表。"""

    events: List[Event] = Field(default_factory=list)


class EventResult(BaseModel):
    """逐事件判定结果。"""

    index: int
    type: EventType
    status: str = Field(description="ok 或 violation")
    reason: Optional[str] = Field(default=None, description="违规原因，status 为 violation 时给出")
    cross_chain_conflict: bool = Field(
        default=False,
        description="该有效 seal/reseal 事件是否与其他链占用同一 seal_id；单链校验接口恒为 false",
    )


class Violation(BaseModel):
    """违规事件的位置与原因。"""

    index: int
    reason: str


class ValidateResponse(BaseModel):
    """校验响应。"""

    valid: bool = Field(description="整条链是否无任何违规")
    sealed: bool = Field(description="处理完所有有效事件后是否处于封装状态")
    final_custodian: Optional[str] = Field(description="最终保管人")
    current_seal: Optional[str] = Field(description="当前有效封签编号，未封装时为 null")
    events: List[EventResult] = Field(description="逐事件状态")
    violations: List[Violation] = Field(description="所有违规事件的位置与原因")


# ---------------------------------------------------------------------------
# 批量封签占用检查（/api/v1/seal-chain/batch-validate）
# ---------------------------------------------------------------------------


class ChainEvents(BaseModel):
    """批次中的一条事件链。"""

    chain_id: str = Field(description="链标识，同一批次内不可重复")
    events: List[Event] = Field(default_factory=list)


class BatchValidateRequest(BaseModel):
    """批量校验请求：按输入顺序排列的多条带 chain_id 的事件链。"""

    chains: List[ChainEvents] = Field(default_factory=list)

    @model_validator(mode="after")
    def _chain_id_must_be_unique(self) -> "BatchValidateRequest":
        seen: set[str] = set()
        for chain in self.chains:
            if chain.chain_id in seen:
                raise ValueError(f"批次内 chain_id 不可重复：{chain.chain_id}")
            seen.add(chain.chain_id)
        return self


class CrossChainConflict(BaseModel):
    """跨链封签占用冲突项：对应一个被标为 cross_chain_conflict 的事件。"""

    seal_id: str = Field(description="被不同链重复占用的封签编号")
    chain_id: str = Field(description="冲突事件所属链")
    event_index: int = Field(description="冲突事件在该链中的下标（从 0 开始）")
    first_chain_id: str = Field(description="按批次输入顺序首次占用该 seal_id 的链")


class BatchChainResult(ValidateResponse):
    """单链结果：结构与单链校验接口完全一致，另附 chain_id。

    冲突仅会把对应有效事件的 cross_chain_conflict 置为 true，
    valid / sealed / violations 等各链自身字段不因跨链冲突改变。
    """

    chain_id: str = Field(description="链标识，回显请求中的 chain_id")


class BatchChainSummary(BaseModel):
    """批次汇总中的单链条目，按 chain_id 稳定排序。"""

    chain_id: str
    valid: bool = Field(description="该链自身校验是否通过（不受跨链冲突影响）")
    conflicts: int = Field(description="该链被标为 cross_chain_conflict 的事件数")


class BatchValidateResponse(BaseModel):
    """批量校验响应。"""

    valid: bool = Field(description="批次是否通过：各链均无违规且不存在任何跨链冲突")
    chains: List[BatchChainResult] = Field(description="各链原校验结果（按输入顺序）")
    conflicts: List[CrossChainConflict] = Field(
        default_factory=list,
        description="跨链封签占用冲突项，按首次占用链输入顺序排列",
    )
    summary: List[BatchChainSummary] = Field(
        default_factory=list,
        description="按 chain_id 稳定排序的批次汇总",
    )


# ---------------------------------------------------------------------------
# 离线片段对账重建（/api/v1/seal-chain/reconcile-validate）
# ---------------------------------------------------------------------------


class ReconcileEvent(Event):
    """离线片段中的封签链事件，在原有业务字段外增加链内链接标识。

    同一条链上的事件通过 event_id / prev_event_id 显式链接，
    对账时仅按链接关系重建顺序，绝不按 timestamp 猜测顺序。
    """

    event_id: str = Field(description="事件在该 chain_id 内的唯一标识，不可为空字符串")
    prev_event_id: Optional[str] = Field(
        default=None,
        description="前驱事件标识；链首事件为 null（或不传），不可为空字符串",
    )

    @field_validator("event_id", "prev_event_id")
    @classmethod
    def _id_must_not_be_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("event_id / prev_event_id 不可为空白字符串")
        return value


class ReconcileFragment(BaseModel):
    """同一 chain_id 的一个离线片段，fragment_index 由输入顺序隐式确定（从 0 开始）。"""

    fragment_id: Optional[str] = Field(
        default=None,
        description="可选的片段业务标识，仅用于阅读，来源编号以输入下标 fragment_index 为准",
    )
    events: List[ReconcileEvent] = Field(default_factory=list)


class ReconcileValidateRequest(BaseModel):
    """对账请求：同一 chain_id 的多个离线片段。"""

    chain_id: str = Field(description="本批次所有片段共同所属的链标识，不可为空字符串")
    fragments: List[ReconcileFragment] = Field(default_factory=list)

    @field_validator("chain_id")
    @classmethod
    def _chain_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("chain_id 不可为空白字符串")
        return value


class ReconcileConflictDetail(BaseModel):
    """同一 event_id 但业务内容不一致的冲突明细。"""

    event_id: str = Field(description="发生冲突的事件标识")
    variants: List[dict] = Field(
        description="去重后互不一致的事件内容（含 event_id 与 prev_event_id）"
    )
    fragment_indexes: List[int] = Field(
        description="该 event_id 全部副本所在的来源片段编号（按输入顺序，去重升序）"
    )


class ReconcileGraphReport(BaseModel):
    """前驱链接图（仅包含无内容冲突的事件）的结构诊断。"""

    heads: List[str] = Field(
        default_factory=list,
        description="所有链头事件（prev_event_id 为 null）的 event_id",
    )
    multiple_heads: bool = Field(default=False, description="链头数量是否大于 1")
    forks: List[dict] = Field(
        default_factory=list,
        description='分叉项：{"event_id": 被多个后继指向的事件, "successors": [后继 event_id]}'
    )
    missing_predecessors: List[dict] = Field(
        default_factory=list,
        description='缺失前驱项：{"event_id": 指向缺失前驱的事件, "missing_event_id": 缺失的前驱标识}'
    )
    cycles: List[List[str]] = Field(
        default_factory=list,
        description="环：每个元素为沿前驱方向排列的 event_id 环；整环旋转至最小标识开头，按最小标识排序"
    )


class ReconciledEventResult(EventResult):
    """重建序列中单个事件的校验结果，附去重与来源信息。"""

    event_id: str = Field(description="事件标识")
    fragment_indexes: List[int] = Field(
        description="该事件出现过的全部来源片段编号（输入顺序，去重升序）"
    )
    duplicate_copies: int = Field(
        description="该 event_id 在所有片段中收到的原始副本数（内容完全一致）"
    )


class ReconcileValidateResponse(BaseModel):
    """对账校验响应。"""

    chain_id: str = Field(description="回显请求中的 chain_id")
    reconciled: bool = Field(
        description="对账是否成功：无内容冲突、链接图结构完整且唯一，并通过单链状态机校验"
    )
    valid: bool = Field(
        description="重建序列的状态机校验结果；未成功重建时恒为 false"
    )
    received_events: int = Field(description="所有片段提交的原始事件副本总数")
    unique_events: int = Field(description="无内容冲突的不同 event_id 数量（参与链接图分析）")
    duplicates: int = Field(description="因内容完全一致而被合并掉的副本数")
    conflicts: List[ReconcileConflictDetail] = Field(
        default_factory=list, description="同 event_id 业务字段不一致的冲突明细"
    )
    graph: ReconcileGraphReport = Field(
        default_factory=ReconcileGraphReport,
        description="链接图结构诊断（仅在无内容冲突时生成；有冲突时各诊断列表为空）",
    )
    rebuilt: bool = Field(description="是否按 prev_event_id 成功重建出唯一序列")
    events: List[ReconciledEventResult] = Field(
        default_factory=list,
        description="重建成功时，按重建顺序排列的逐事件状态机校验结果；否则为空列表",
    )
    sealed: bool = Field(default=False, description="重建链处理完所有有效事件后的封装状态")
    final_custodian: Optional[str] = Field(default=None, description="重建链最终保管人")
    current_seal: Optional[str] = Field(default=None, description="重建链当前有效封签")
    violations: List[Violation] = Field(
        default_factory=list, description="重建链的状态机违规（index 基于重建序列，从 0 开始）"
    )
