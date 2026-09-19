# 封签链校验 API（Seal Chain Validation）

用于展品运输交接场景的封签链校验服务。客户端提交按时间排列的 **封装 / 交接 / 开封 / 重封** 事件序列，服务按状态机逐事件校验，返回最终保管人、当前封签、逐事件状态及全部违规事件的位置与原因；同时提供**批量封签占用检查**接口，对多条事件链做跨链 `seal_id` 占用冲突检测；还提供**离线片段对账**接口，对同一条链的多个离线片段按 `event_id` 去重、按 `prev_event_id` 链接重建唯一序列后再做状态机校验。

## 校验规则（状态机）

| # | 规则 |
|---|------|
| 1 | 未封装状态下不得交接（handover）或开封（unseal） |
| 2 | 已封装状态下不得重复封装 / 重封，须先开封 |
| 3 | 开封后必须使用**新的、未使用过的**封签重封（reseal） |
| 4 | 已使用过的封签不得复用 |
| 5 | 事件时间戳必须严格递增 |

**违规事件不改变状态，也不阻止后续事件的判定**（时间递增性始终与最近一个*有效*事件比较）。

## 快速启动

单条命令（依赖随镜像构建完成，启动即就绪）：

```bash
docker compose up --build
```

- API 地址：`http://localhost:8000`
- 交互式文档（Swagger UI）：`http://localhost:8000/docs`
- 健康检查：`GET http://localhost:8000/health`

### 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `HOST_PORT` | `8000` | 宿主机映射端口，如 `HOST_PORT=9000 docker compose up --build` |

容器内服务固定监听 `0.0.0.0:8000`。

## API 说明

### `POST /api/v1/seal-chain/validate`

**请求体**

```json
{
  "events": [
    {"type": "seal",     "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
    {"type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"},
    {"type": "unseal",   "timestamp": "2026-09-18T12:00:00Z"},
    {"type": "reseal",   "timestamp": "2026-09-18T13:00:00Z", "seal_id": "S002", "custodian": "Bob"}
  ]
}
```

- `type`：`seal`（封装）/ `handover`（交接）/ `unseal`（开封）/ `reseal`（重封）
- `timestamp`：ISO 8601 时间，需严格递增
- `seal_id`：`seal` / `reseal` 必填，且不得与历史封签重复
- `custodian`：`seal` / `reseal` / `handover` 必填（交接时为接收方）

**响应示例**（上例中若在第 2、3 步之间插入一次未封装状态下的交接）：

```json
{
  "valid": false,
  "sealed": true,
  "final_custodian": "Bob",
  "current_seal": "S002",
  "events": [
    {"index": 0, "type": "seal", "status": "ok", "reason": null},
    {"index": 1, "type": "handover", "status": "violation", "reason": "未封装状态下不得交接"}
  ],
  "violations": [
    {"index": 1, "reason": "未封装状态下不得交接"}
  ]
}
```

| 字段 | 说明 |
|------|------|
| `valid` | 整条链是否无违规 |
| `sealed` | 处理完所有有效事件后是否处于封装状态 |
| `final_custodian` | 最终保管人 |
| `current_seal` | 当前有效封签，未封装时为 `null` |
| `events[]` | 逐事件状态（`ok` / `violation` + 原因）；`cross_chain_conflict` 在单链接口恒为 `false` |
| `violations[]` | 所有违规事件的位置（`index`，从 0 开始）与原因 |

> 事件结构不合法（如未知 `type`、缺少 `timestamp`）返回 `422`；语义违规（状态机层面）则在响应的 `violations` 中逐条列出。

### 示例调用

```bash
curl -s http://localhost:8000/api/v1/seal-chain/validate \
  -H 'Content-Type: application/json' \
  -d '{"events": [
        {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
        {"type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"}
      ]}'
```

### `POST /api/v1/seal-chain/batch-validate`

批量封签占用检查。请求包含多条带 `chain_id` 的事件链，服务处理分两步：

1. **分别按上述状态机规则校验每条链**，得到各链原校验结果；
2. **按输入顺序建立全批次封签占用表**：同一 `seal_id` 被**不同链**的**有效** seal/reseal 事件（单链校验中 `status = ok`）使用时，两条链对应的事件均标记 `cross_chain_conflict: true`。

冲突不改变各链自身状态（链的 `valid` / `sealed` / `violations` 等与单链接口一致），但只要存在冲突，批次的 `valid` 必为 `false`。同一条链内复用封签属于状态机违规（规则 3/4），不计入跨链冲突；单链校验中不合法的事件不构成封签占用。

**请求体**

```json
{
  "chains": [
    {
      "chain_id": "chain-a",
      "events": [
        {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"}
      ]
    },
    {
      "chain_id": "chain-b",
      "events": [
        {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Carol"},
        {"type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Dave"}
      ]
    }
  ]
}
```

- `chains[]`：按批次输入顺序排列；`chain_id` 在批次内不可重复（重复返回 `422`），允许空批次。

**响应示例**（对应上例）

```json
{
  "valid": false,
  "chains": [
    {
      "chain_id": "chain-a",
      "valid": true,
      "sealed": true,
      "final_custodian": "Alice",
      "current_seal": "S001",
      "events": [
        {"index": 0, "type": "seal", "status": "ok", "reason": null, "cross_chain_conflict": true}
      ],
      "violations": []
    },
    {
      "chain_id": "chain-b",
      "valid": true,
      "sealed": true,
      "final_custodian": "Dave",
      "current_seal": "S001",
      "events": [
        {"index": 0, "type": "seal", "status": "ok", "reason": null, "cross_chain_conflict": true},
        {"index": 1, "type": "handover", "status": "ok", "reason": null, "cross_chain_conflict": false}
      ],
      "violations": []
    }
  ],
  "conflicts": [
    {"seal_id": "S001", "chain_id": "chain-a", "event_index": 0, "first_chain_id": "chain-a"},
    {"seal_id": "S001", "chain_id": "chain-b", "event_index": 0, "first_chain_id": "chain-a"}
  ],
  "summary": [
    {"chain_id": "chain-a", "valid": true, "conflicts": 1},
    {"chain_id": "chain-b", "valid": true, "conflicts": 1}
  ]
}
```

| 字段 | 说明 |
|------|------|
| `valid` | 批次是否通过：各链自身均无违规**且**不存在任何跨链冲突 |
| `chains[]` | 每链的原校验结果（结构同单链接口，附 `chain_id`），按**输入顺序**返回；冲突事件的 `events[].cross_chain_conflict` 为 `true` |
| `conflicts[]` | 冲突项，每个被标记的事件一条：`seal_id`、所属 `chain_id`、链内 `event_index`（从 0 开始）、按输入顺序首次占用该封签的 `first_chain_id`；首次占用事件自身的锚点项 `chain_id` 与 `first_chain_id` 相同 |
| `summary[]` | 按 `chain_id` 字典序稳定排序的汇总：每链自身 `valid` 与冲突事件数 `conflicts` |

### 批量接口示例调用

```bash
curl -s http://localhost:8000/api/v1/seal-chain/batch-validate \
  -H 'Content-Type: application/json' \
  -d '{"chains": [
        {"chain_id": "chain-a", "events": [
          {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"}
        ]},
        {"chain_id": "chain-b", "events": [
          {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Carol"}
        ]}
      ]}'
```

### `POST /api/v1/seal-chain/reconcile-validate`

离线片段对账：接收**同一 `chain_id`** 的多个离线片段（可能由不同设备在断网期间各自记录，彼此重叠或断尾）。每个事件在原有业务字段外增加两个链接字段：

- `event_id`：事件在该链内的唯一标识（非空字符串）
- `prev_event_id`：前驱事件标识；链首事件为 `null`（或不传）

处理分三步：

1. **按 `event_id` 去重**：同一 `event_id` 的副本，若业务内容（`type` / `timestamp` / `seal_id` / `custodian` / `prev_event_id`）完全相同则合并为一份；若业务字段存在差异，则在 `conflicts` 中返回冲突明细（各变体内容及全部来源片段编号）。存在冲突的标识不参与后续链接分析。
2. **仅按 `prev_event_id` 重建唯一序列，绝不按时间戳猜序**。结构问题在 `graph` 中明确报告：
   - **多头（multiple heads）**：`prev_event_id` 为 `null` 的链头事件多于一个；
   - **分叉（fork）**：同一事件被多个不同后继指认为前驱；
   - **缺失前驱（missing predecessors）**：`prev_event_id` 指向的 `event_id` 在所有片段中均不存在；
   - **环（cycles）**：沿前驱方向形成闭合环（含自环），环按最小 `event_id` 旋转到开头并排序。
3. **仅当无内容冲突且唯一序列重建成功时**，才调用现有单链状态机校验器对重建序列做校验，并在每个事件上返回 `fragment_indexes`（该事件出现过的全部来源片段编号，即片段在 `fragments[]` 中的下标，从 0 开始、按输入顺序去重升序）与 `duplicate_copies`（原始副本数）。

> 存在内容冲突或任一结构问题时 `rebuilt` 为 `false`，不调用状态机校验，`events` 为空，`reconciled` / `valid` 为 `false`。空片段列表视为合法空链（与单链接口一致）。`fragment_indexes` 始终以**片段输入下标**为准（从 0 开始），可选的 `fragment_id` 仅作业务标注。

**请求体**

```json
{
  "chain_id": "chain-1",
  "fragments": [
    {
      "fragment_id": "device-a",
      "events": [
        {"event_id": "e1", "prev_event_id": null, "type": "seal",
         "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
        {"event_id": "e2", "prev_event_id": "e1", "type": "handover",
         "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"}
      ]
    },
    {
      "fragment_id": "device-b",
      "events": [
        {"event_id": "e2", "prev_event_id": "e1", "type": "handover",
         "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"},
        {"event_id": "e3", "prev_event_id": "e2", "type": "unseal",
         "timestamp": "2026-09-18T12:00:00Z"}
      ]
    }
  ]
}
```

**成功响应示例**（对应上例，`e2` 在两个片段中内容完全一致，被合并）

```json
{
  "chain_id": "chain-1",
  "reconciled": true,
  "valid": true,
  "received_events": 4,
  "unique_events": 3,
  "duplicates": 1,
  "conflicts": [],
  "graph": {
    "heads": ["e1"],
    "multiple_heads": false,
    "forks": [],
    "missing_predecessors": [],
    "cycles": []
  },
  "rebuilt": true,
  "events": [
    {"index": 0, "event_id": "e1", "type": "seal", "status": "ok", "reason": null,
     "cross_chain_conflict": false, "fragment_indexes": [0], "duplicate_copies": 1},
    {"index": 1, "event_id": "e2", "type": "handover", "status": "ok", "reason": null,
     "cross_chain_conflict": false, "fragment_indexes": [0, 1], "duplicate_copies": 2},
    {"index": 2, "event_id": "e3", "type": "unseal", "status": "ok", "reason": null,
     "cross_chain_conflict": false, "fragment_indexes": [1], "duplicate_copies": 1}
  ],
  "sealed": false,
  "final_custodian": "Bob",
  "current_seal": null,
  "violations": []
}
```

**结构问题响应示例**（多头 `a` 与游离链头 `d`；`a` 又被 `b`、`c` 同时指向；`d` 的前驱 `x` 缺失）

```json
{
  "chain_id": "c",
  "reconciled": false,
  "valid": false,
  "received_events": 4,
  "unique_events": 4,
  "duplicates": 0,
  "conflicts": [],
  "graph": {
    "heads": ["a", "d"],
    "multiple_heads": true,
    "forks": [{"event_id": "a", "successors": ["b", "c"]}],
    "missing_predecessors": [{"event_id": "d", "missing_event_id": "x"}],
    "cycles": []
  },
  "rebuilt": false,
  "events": [],
  "sealed": false,
  "final_custodian": null,
  "current_seal": null,
  "violations": []
}
```

**内容冲突响应示例**（`e1` 在两个片段中 `seal_id` 不一致）

```json
{
  "chain_id": "c",
  "reconciled": false,
  "valid": false,
  "received_events": 2,
  "unique_events": 0,
  "duplicates": 0,
  "conflicts": [
    {
      "event_id": "e1",
      "variants": [
        {"event_id": "e1", "prev_event_id": null, "type": "seal",
         "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S1", "custodian": "A"},
        {"event_id": "e1", "prev_event_id": null, "type": "seal",
         "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S2", "custodian": "A"}
      ],
      "fragment_indexes": [0, 1]
    }
  ],
  "graph": {"heads": [], "multiple_heads": false, "forks": [],
            "missing_predecessors": [], "cycles": []},
  "rebuilt": false,
  "events": [],
  "sealed": false,
  "final_custodian": null,
  "current_seal": null,
  "violations": []
}
```

| 字段 | 说明 |
|------|------|
| `reconciled` | 对账是否成功：无内容冲突、链接图结构完整唯一且状态机校验通过 |
| `valid` | 重建序列的状态机校验结果；未成功重建时恒为 `false` |
| `received_events` | 所有片段提交的原始事件副本总数 |
| `unique_events` | 无内容冲突的不同 `event_id` 数量（参与链接图分析） |
| `duplicates` | 因内容完全一致而被合并掉的副本数（冲突标识的副本不计入） |
| `conflicts[]` | 同 `event_id` 业务字段不一致的冲突明细：`event_id`、互异 `variants`（完整事件内容）、全部来源 `fragment_indexes` |
| `graph.heads` | 所有链头事件（`prev_event_id` 为 `null`）的 `event_id` |
| `graph.multiple_heads` | 链头是否多于一个 |
| `graph.forks[]` | 分叉项：`event_id`（被多后继指向）与 `successors` |
| `graph.missing_predecessors[]` | 缺失前驱项：`event_id` 与其缺失的 `missing_event_id` |
| `graph.cycles[]` | 环：沿前驱方向排列的 `event_id` 环，整环旋转到最小标识开头，按最小标识排序；自环为单元素列表 |
| `rebuilt` | 是否成功重建唯一序列；仅为 `true` 时 `events` 非空（空片段列表也为 `true`） |
| `events[]` | 重建序列按重建顺序的逐事件状态机结果（`index` 基于重建序列），附 `event_id`、`fragment_indexes`、`duplicate_copies`；`cross_chain_conflict` 恒为 `false` |
| `sealed` / `final_custodian` / `current_seal` / `violations[]` | 重建链的状态机结论，含义同单链接口 |

> `chain_id` 为空、`event_id` / `prev_event_id` 为空白字符串、事件结构不合法（未知 `type` 等）返回 `422`。

### 对账接口示例调用

```bash
curl -s http://localhost:8000/api/v1/seal-chain/reconcile-validate \
  -H 'Content-Type: application/json' \
  -d '{"chain_id": "chain-1", "fragments": [
        {"events": [
          {"event_id": "e1", "prev_event_id": null, "type": "seal",
           "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"}
        ]},
        {"events": [
          {"event_id": "e2", "prev_event_id": "e1", "type": "handover",
           "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"}
        ]}
      ]}'
```

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest -v
uvicorn app.main:app --reload --port 8000
```

## 项目结构

```
app/
  main.py        # FastAPI 入口与路由
  models.py      # 请求 / 响应模型（单链、批量、离线对账）
  validator.py   # 单链状态机校验核心
  batch.py       # 批量封签占用检查（跨链冲突）
  reconcile.py   # 离线片段对账：event_id 去重 + prev_event_id 重建
tests/
  test_validator.py
  test_batch.py
  test_reconcile.py
Dockerfile
docker-compose.yml
requirements.txt
```
