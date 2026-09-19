"""离线片段对账重建的单元测试与 API 集成测试。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.models import ReconcileEvent, ReconcileFragment, ReconcileValidateRequest
from app.reconcile import reconcile_validate

client = TestClient(app)

T = "2026-09-18T%02d:00:00Z"


def rev(eid: str, type_: str, i: int, prev: str | None = None, **kw) -> ReconcileEvent:
    return ReconcileEvent(
        event_id=eid, prev_event_id=prev, type=type_, timestamp=T % i, **kw
    )


def frag(*events: ReconcileEvent, fragment_id: str | None = None) -> ReconcileFragment:
    return ReconcileFragment(fragment_id=fragment_id, events=list(events))


def request(chain_id: str, *fragments: ReconcileFragment) -> ReconcileValidateRequest:
    return ReconcileValidateRequest(chain_id=chain_id, fragments=list(fragments))


# ---------------------------------------------------------------------------
# 去重与来源
# ---------------------------------------------------------------------------


def test_identical_duplicates_merged_with_provenance():
    # 片段 0、1 内容完全一致；片段 2 只含尾部事件并再次携带 e2 的相同副本
    f0 = frag(
        rev("e1", "seal", 1, seal_id="S001", custodian="Alice"),
        rev("e2", "handover", 2, prev="e1", custodian="Bob"),
    )
    f1 = frag(
        rev("e1", "seal", 1, seal_id="S001", custodian="Alice"),
        rev("e2", "handover", 2, prev="e1", custodian="Bob"),
        rev("e3", "unseal", 3, prev="e2"),
    )
    f2 = frag(
        rev("e2", "handover", 2, prev="e1", custodian="Bob"),
        rev("e4", "reseal", 4, prev="e3", seal_id="S002", custodian="Bob"),
    )
    resp = reconcile_validate(request("chain-x", f0, f1, f2))

    assert resp.reconciled is True
    assert resp.valid is True
    assert resp.rebuilt is True
    assert resp.received_events == 7
    assert resp.unique_events == 4
    assert resp.duplicates == 3  # e1/e2 在 f1 重复，e2 在 f2 重复
    assert resp.conflicts == []
    assert [e.event_id for e in resp.events] == ["e1", "e2", "e3", "e4"]

    by_id = {e.event_id: e for e in resp.events}
    assert by_id["e1"].fragment_indexes == [0, 1]
    assert by_id["e2"].fragment_indexes == [0, 1, 2]
    assert by_id["e3"].fragment_indexes == [1]
    assert by_id["e4"].fragment_indexes == [2]
    assert by_id["e2"].duplicate_copies == 3
    assert by_id["e3"].duplicate_copies == 1
    # 单链状态机结果透传
    assert resp.final_custodian == "Bob"
    assert resp.current_seal == "S002"
    assert resp.sealed is True
    assert all(e.cross_chain_conflict is False for e in resp.events)


def test_graph_heads_reported_for_clean_chain():
    f0 = frag(rev("a", "seal", 1, seal_id="S1", custodian="A"))
    resp = reconcile_validate(request("c", f0))
    assert resp.graph.heads == ["a"]
    assert resp.graph.multiple_heads is False


# ---------------------------------------------------------------------------
# 内容冲突
# ---------------------------------------------------------------------------


def test_same_event_id_different_business_field_is_conflict():
    f0 = frag(rev("e1", "seal", 1, seal_id="S001", custodian="Alice"))
    f1 = frag(rev("e1", "seal", 1, seal_id="S999", custodian="Alice"))  # seal_id 不同
    resp = reconcile_validate(request("c", f0, f1))

    assert resp.reconciled is False
    assert resp.valid is False
    assert resp.rebuilt is False
    assert len(resp.conflicts) == 1
    conflict = resp.conflicts[0]
    assert conflict.event_id == "e1"
    assert conflict.fragment_indexes == [0, 1]
    assert len(conflict.variants) == 2
    assert {v["seal_id"] for v in conflict.variants} == {"S001", "S999"}
    # 有冲突时不做图诊断、不校验
    assert resp.graph.heads == []
    assert resp.events == []
    assert resp.received_events == 2
    assert resp.unique_events == 0
    assert resp.duplicates == 0


def test_conflict_on_prev_event_id_is_business_conflict():
    f0 = frag(
        rev("e1", "seal", 1, seal_id="S1", custodian="A"),
        rev("e2", "handover", 2, prev="e1", custodian="B"),
    )
    # e2 业务内容里的 prev_event_id 被改成另一个标识
    f1 = frag(rev("e2", "handover", 2, prev="e9", custodian="B"))
    resp = reconcile_validate(request("c", f0, f1))
    assert [c.event_id for c in resp.conflicts] == ["e2"]
    assert {v["prev_event_id"] for v in resp.conflicts[0].variants} == {"e1", "e9"}
    assert resp.rebuilt is False


def test_identical_duplicate_copies_not_counted_as_conflict():
    f0 = frag(rev("e1", "seal", 1, seal_id="S1", custodian="A"))
    f1 = frag(rev("e1", "seal", 1, seal_id="S1", custodian="A"))
    resp = reconcile_validate(request("c", f0, f1))
    assert resp.conflicts == []
    assert resp.duplicates == 1
    assert resp.reconciled is True


# ---------------------------------------------------------------------------
# 图结构诊断
# ---------------------------------------------------------------------------


def test_multiple_heads_reported():
    f0 = frag(
        rev("a", "seal", 1, seal_id="S1", custodian="A"),
        rev("b", "seal", 2, seal_id="S2", custodian="B"),  # 另一个链头
    )
    resp = reconcile_validate(request("c", f0))
    assert resp.rebuilt is False
    assert resp.reconciled is False
    assert resp.graph.heads == ["a", "b"]
    assert resp.graph.multiple_heads is True
    assert resp.events == []


def test_fork_reported():
    # x 同时被 b 和 c 指认为前驱
    f0 = frag(
        rev("a", "seal", 1, seal_id="S1", custodian="A"),
        rev("b", "handover", 2, prev="a", custodian="B"),
        rev("c", "unseal", 3, prev="a"),
    )
    resp = reconcile_validate(request("c", f0))
    assert resp.rebuilt is False
    assert resp.graph.forks == [{"event_id": "a", "successors": ["b", "c"]}]
    assert resp.graph.heads == ["a"]


def test_missing_predecessor_reported():
    f0 = frag(
        rev("a", "seal", 1, seal_id="S1", custodian="A"),
        rev("b", "handover", 2, prev="ghost", custodian="B"),
    )
    resp = reconcile_validate(request("c", f0))
    assert resp.rebuilt is False
    assert resp.graph.missing_predecessors == [
        {"event_id": "b", "missing_event_id": "ghost"}
    ]
    # 缺失前驱的链头 a 依然列出，但无法重建
    assert resp.graph.heads == ["a"]


def test_cycle_reported():
    f0 = frag(
        rev("a", "unseal", 1, prev="c"),
        rev("b", "handover", 2, prev="a", custodian="X"),
        rev("c", "unseal", 3, prev="b"),
    )
    resp = reconcile_validate(request("c", f0))
    assert resp.rebuilt is False
    assert resp.graph.heads == []
    # 环按最小标识旋转到开头
    assert resp.graph.cycles == [["a", "c", "b"]]


def test_self_loop_reported():
    f0 = frag(rev("a", "seal", 1, prev="a", seal_id="S1", custodian="A"))
    resp = reconcile_validate(request("c", f0))
    assert resp.rebuilt is False
    assert resp.graph.cycles == [["a"]]


def test_cycle_with_tail_branch_reported_once():
    # t -> a -> b -> a（尾部 t 指向环）
    f0 = frag(
        rev("t", "handover", 1, prev="a", custodian="X"),
        rev("a", "unseal", 2, prev="b"),
        rev("b", "unseal", 3, prev="a"),
    )
    resp = reconcile_validate(request("c", f0))
    assert resp.graph.cycles == [["a", "b"]]


# ---------------------------------------------------------------------------
# 重建与状态机
# ---------------------------------------------------------------------------


def test_rebuild_uses_links_not_timestamps():
    # 片段内部时间戳乱序且跨片段重叠；只能按 prev 链接得到 e1 -> e2 -> e3
    f0 = frag(
        rev("e3", "unseal", 9, prev="e2"),
        rev("e1", "seal", 3, seal_id="S1", custodian="A"),
    )
    f1 = frag(rev("e2", "handover", 6, prev="e1", custodian="B"))
    resp = reconcile_validate(request("c", f0, f1))
    assert resp.rebuilt is True
    assert [e.event_id for e in resp.events] == ["e1", "e2", "e3"]
    assert resp.events[0].index == 0
    assert [e.status for e in resp.events] == ["ok", "ok", "ok"]


def test_validator_runs_only_on_rebuilt_order_and_flags_violations():
    # 片段输入顺序与链接序、时间序都不同：f0 只含时间更早的 e2，f1 含 e1
    # 重建后为 e1(10:05 seal) -> e2(10:02 handover)，时间戳倒退必须判违规，
    # 证明校验器消费的是按链接重建的序列而不是任何时间排序结果
    f0 = frag(rev("e2", "handover", 2, prev="e1", custodian="B"))
    f1 = frag(rev("e1", "seal", 5, seal_id="S1", custodian="A"))
    resp = reconcile_validate(request("c", f0, f1))
    assert resp.rebuilt is True
    assert [e.event_id for e in resp.events] == ["e1", "e2"]
    assert resp.reconciled is False
    assert resp.valid is False
    assert [v.index for v in resp.violations] == [1]
    assert "严格递增" in resp.violations[0].reason
    assert resp.events[0].status == "ok"
    assert resp.events[1].status == "violation"


def test_empty_fragments_reconciles_as_empty_chain():
    resp = reconcile_validate(request("c", frag(), frag()))
    assert resp.reconciled is True
    assert resp.valid is True
    assert resp.rebuilt is True
    assert resp.received_events == 0
    assert resp.unique_events == 0
    assert resp.events == []
    assert resp.graph.heads == []
    assert resp.final_custodian is None


# ---------------------------------------------------------------------------
# API 集成
# ---------------------------------------------------------------------------


def test_api_reconcile_happy_path():
    payload = {
        "chain_id": "chain-1",
        "fragments": [
            {"fragment_id": "offline-a", "events": [
                {"event_id": "e1", "prev_event_id": None, "type": "seal",
                 "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001",
                 "custodian": "Alice"},
                {"event_id": "e2", "prev_event_id": "e1", "type": "handover",
                 "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"},
            ]},
            {"events": [
                {"event_id": "e2", "prev_event_id": "e1", "type": "handover",
                 "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"},
                {"event_id": "e3", "prev_event_id": "e2", "type": "unseal",
                 "timestamp": "2026-09-18T12:00:00Z"},
            ]},
        ],
    }
    r = client.post("/api/v1/seal-chain/reconcile-validate", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["chain_id"] == "chain-1"
    assert body["reconciled"] is True
    assert body["rebuilt"] is True
    assert body["duplicates"] == 1
    assert [e["event_id"] for e in body["events"]] == ["e1", "e2", "e3"]
    assert body["events"][1]["fragment_indexes"] == [0, 1]
    assert body["events"][1]["duplicate_copies"] == 2
    assert body["graph"] == {
        "heads": ["e1"], "multiple_heads": False, "forks": [],
        "missing_predecessors": [], "cycles": [],
    }


def test_api_reconcile_conflict_response():
    payload = {
        "chain_id": "c",
        "fragments": [
            {"events": [{"event_id": "e1", "type": "seal",
                         "timestamp": "2026-09-18T10:00:00Z",
                         "seal_id": "S1", "custodian": "A"}]},
            {"events": [{"event_id": "e1", "type": "seal",
                         "timestamp": "2026-09-18T10:00:00Z",
                         "seal_id": "S2", "custodian": "A"}]},
        ],
    }
    r = client.post("/api/v1/seal-chain/reconcile-validate", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["reconciled"] is False
    assert body["rebuilt"] is False
    assert body["conflicts"][0]["event_id"] == "e1"
    assert body["conflicts"][0]["fragment_indexes"] == [0, 1]


def test_api_reconcile_missing_predecessor_response():
    payload = {
        "chain_id": "c",
        "fragments": [{"events": [
            {"event_id": "e2", "prev_event_id": "e1", "type": "handover",
             "timestamp": "2026-09-18T11:00:00Z", "custodian": "B"},
        ]}],
    }
    r = client.post("/api/v1/seal-chain/reconcile-validate", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["rebuilt"] is False
    assert body["graph"]["missing_predecessors"] == [
        {"event_id": "e2", "missing_event_id": "e1"}
    ]
    assert body["events"] == []


def test_api_reconcile_blank_event_id_rejected_with_422():
    payload = {
        "chain_id": "c",
        "fragments": [{"events": [
            {"event_id": " ", "type": "seal",
             "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S1", "custodian": "A"},
        ]}],
    }
    assert client.post(
        "/api/v1/seal-chain/reconcile-validate", json=payload
    ).status_code == 422


def test_api_reconcile_blank_prev_event_id_rejected_with_422():
    payload = {
        "chain_id": "c",
        "fragments": [{"events": [
            {"event_id": "e1", "prev_event_id": "", "type": "seal",
             "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S1", "custodian": "A"},
        ]}],
    }
    assert client.post(
        "/api/v1/seal-chain/reconcile-validate", json=payload
    ).status_code == 422


def test_api_reconcile_blank_chain_id_rejected_with_422():
    r = client.post(
        "/api/v1/seal-chain/reconcile-validate", json={"chain_id": "", "fragments": []}
    )
    assert r.status_code == 422
