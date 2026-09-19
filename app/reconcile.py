"""离线片段对账重建。

处理流程：

1. **按 event_id 去重**：汇总同一 ``chain_id`` 下多个离线片段中的全部事件。
   同一 ``event_id`` 的副本业务内容（type / timestamp / seal_id / custodian /
   prev_event_id）完全相同时合并为一份；业务字段存在差异时记入冲突明细。
2. **按 prev_event_id 重建唯一序列**：只依据显式前驱链接，**绝不按时间戳猜序**。
   明确诊断多头（multiple heads）、分叉（fork）、缺失前驱（missing predecessor）
   与环（cycle）四类结构问题；任一存在都无法重建唯一序列。
3. **仅在重建成功时**调用现有单链状态机校验器 ``validate_events``，并为每个事件
   附上其全部来源片段编号（fragment_index，按片段输入顺序从 0 开始）。

存在内容冲突时，冲突的标识不参与链接图分析（其链接身份无法确定），
此时不进行图诊断、不重建、不调用状态机校验。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .models import (
    ReconcileConflictDetail,
    ReconcileEvent,
    ReconcileGraphReport,
    ReconcileValidateRequest,
    ReconcileValidateResponse,
    ReconciledEventResult,
    Violation,
)
from .validator import validate_events

# 业务内容比较字段（event_id 是分组键，不计入内容），固定顺序
_CONTENT_FIELDS: Tuple[str, ...] = (
    "type",
    "timestamp",
    "seal_id",
    "custodian",
    "prev_event_id",
)


def _content_key(event: ReconcileEvent) -> tuple:
    """事件业务内容指纹：同 event_id 的副本指纹相同即视为完全一致。"""
    return tuple(getattr(event, name) for name in _CONTENT_FIELDS)


def _dedup(
    request: ReconcileValidateRequest,
) -> Tuple[
    Dict[str, ReconcileEvent],
    Dict[str, int],
    Dict[str, List[int]],
    List[ReconcileConflictDetail],
    int,
]:
    """按 event_id 汇总全部片段事件。

    返回：(无冲突事件 event_id -> 代表事件, 各 event_id 原始副本数,
    各 event_id 来源片段编号列表, 冲突明细, 原始副本总数)。
    遍历顺序为片段 / 事件的输入顺序，保证输出确定性。
    """
    unique: Dict[str, ReconcileEvent] = {}
    copies: Dict[str, int] = {}
    fragments_by_id: Dict[str, List[int]] = {}
    # event_id -> {内容指纹: (代表事件, 该变体的片段编号集合)}
    variants: Dict[str, Dict[tuple, Tuple[ReconcileEvent, set]]] = {}

    received = 0
    for fragment_index, fragment in enumerate(request.fragments):
        for event in fragment.events:
            received += 1
            copies[event.event_id] = copies.get(event.event_id, 0) + 1

            indexes = fragments_by_id.setdefault(event.event_id, [])
            if fragment_index not in indexes:
                indexes.append(fragment_index)

            key = _content_key(event)
            group = variants.setdefault(event.event_id, {})
            if key not in group:
                group[key] = (event, {fragment_index})
            else:
                group[key][1].add(fragment_index)

    conflicts: List[ReconcileConflictDetail] = []
    for event_id, group in variants.items():
        if len(group) > 1:
            # 按变体首次出现顺序（dict 插入序）给出冲突内容
            conflicts.append(
                ReconcileConflictDetail(
                    event_id=event_id,
                    variants=[representative.model_dump() for representative, _ in group.values()],
                    fragment_indexes=fragments_by_id[event_id],
                )
            )
        else:
            unique[event_id] = next(iter(group.values()))[0]

    return unique, copies, fragments_by_id, conflicts, received


def _build_graph(nodes: Dict[str, ReconcileEvent]) -> ReconcileGraphReport:
    """对无冲突事件按 prev_event_id 构图并诊断多头、分叉、缺失前驱与环。"""
    heads = sorted(eid for eid, event in nodes.items() if event.prev_event_id is None)

    # 后继表：parent -> children（children 按首次出现顺序插入，再统一排序保证稳定）
    children: Dict[str, List[str]] = {}
    missing: List[dict] = []
    for eid, event in nodes.items():
        parent = event.prev_event_id
        if parent is None:
            continue
        if parent not in nodes:
            missing.append({"event_id": eid, "missing_event_id": parent})
        else:
            children.setdefault(parent, []).append(eid)

    forks = [
        {"event_id": parent, "successors": sorted(successors)}
        for parent, successors in children.items()
        if len(successors) > 1
    ]
    forks.sort(key=lambda item: item["event_id"])
    missing.sort(key=lambda item: (item["missing_event_id"], item["event_id"]))

    # 环检测：沿 prev_event_id 方向做三色 DFS，0=未访问，1=在当前路径，2=已完成
    cycles: List[List[str]] = []
    color: Dict[str, int] = {eid: 0 for eid in nodes}
    for start in nodes:  # dict 插入序：跨片段首次出现顺序
        if color[start] != 0:
            continue
        path: List[str] = []
        position: Dict[str, int] = {}
        node = start
        while node is not None and color[node] == 0:
            color[node] = 1
            position[node] = len(path)
            path.append(node)
            parent = nodes[node].prev_event_id
            node = parent if parent in nodes else None
        if node is not None and color[node] == 1:
            cycle = path[position[node]:]
            # 整环旋转至最小 event_id 开头，得到规范表示
            pivot = min(range(len(cycle)), key=lambda i: cycle[i])
            cycle = cycle[pivot:] + cycle[:pivot]
            cycles.append(cycle)
        for visited in path:
            color[visited] = 2
    cycles.sort(key=lambda cycle: cycle[0])

    return ReconcileGraphReport(
        heads=heads,
        multiple_heads=len(heads) > 1,
        forks=forks,
        missing_predecessors=missing,
        cycles=cycles,
    )


def _rebuild_sequence(
    nodes: Dict[str, ReconcileEvent], graph: ReconcileGraphReport
) -> Optional[List[str]]:
    """结构无问题时，从唯一链头沿唯一后继线性还原事件顺序。

    任何结构问题（多头 / 分叉 / 缺失前驱 / 环）都返回 None。
    """
    if not nodes:
        # 空集合视为合法空链（与单链校验接口允许空 events 一致）
        return []
    if graph.multiple_heads or graph.forks or graph.missing_predecessors or graph.cycles:
        return None
    if len(graph.heads) != 1:
        # 非空节点集却没有链头：必然成环，已由 cycles 覆盖；防御性处理
        return None

    sequence: List[str] = []
    current: Optional[str] = graph.heads[0]
    while current is not None:
        sequence.append(current)
        # 结构检查已保证每个节点至多一个后继
        current = next(
            (child for child, child_event in nodes.items()
             if child_event.prev_event_id == current),
            None,
        )
    if len(sequence) != len(nodes):
        return None
    return sequence


def reconcile_validate(request: ReconcileValidateRequest) -> ReconcileValidateResponse:
    """对同一 chain_id 的多个离线片段去重、重建并校验。"""
    nodes, copies, fragment_indexes, conflicts, received = _dedup(request)

    # 无内容冲突的标识：其多份相同副本的合并数才计入 duplicates；
    # 冲突标识的副本一律计入 conflicts，不计入 duplicates。
    duplicate_count = sum(
        copies[eid] - 1 for eid in nodes
    )

    # 存在内容冲突：冲突标识不参与链接图分析，不重建、不校验
    if conflicts:
        return ReconcileValidateResponse(
            chain_id=request.chain_id,
            reconciled=False,
            valid=False,
            received_events=received,
            unique_events=len(nodes),
            duplicates=duplicate_count,
            conflicts=conflicts,
            graph=ReconcileGraphReport(),
            rebuilt=False,
        )

    graph = _build_graph(nodes)
    order = _rebuild_sequence(nodes, graph)

    if order is None:
        return ReconcileValidateResponse(
            chain_id=request.chain_id,
            reconciled=False,
            valid=False,
            received_events=received,
            unique_events=len(nodes),
            duplicates=duplicate_count,
            conflicts=[],
            graph=graph,
            rebuilt=False,
        )

    # 仅在成功重建出唯一序列时调用现有单链校验器；绝不按时间戳猜序
    ordered_events = [nodes[eid] for eid in order]
    single = validate_events(ordered_events)

    events = [
        ReconciledEventResult(
            index=index,
            type=event.type,
            status=result.status,
            reason=result.reason,
            cross_chain_conflict=False,
            event_id=event.event_id,
            fragment_indexes=fragment_indexes[event.event_id],
            duplicate_copies=copies[event.event_id],
        )
        for index, (event, result) in enumerate(zip(ordered_events, single.events))
    ]

    return ReconcileValidateResponse(
        chain_id=request.chain_id,
        reconciled=single.valid,
        valid=single.valid,
        received_events=received,
        unique_events=len(nodes),
        duplicates=duplicate_count,
        conflicts=[],
        graph=graph,
        rebuilt=True,
        events=events,
        sealed=single.sealed,
        final_custodian=single.final_custodian,
        current_seal=single.current_seal,
        violations=[Violation(index=v.index, reason=v.reason) for v in single.violations],
    )
