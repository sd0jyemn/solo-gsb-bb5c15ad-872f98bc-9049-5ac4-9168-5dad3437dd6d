# 封签链校验 API（Seal Chain Validation）

用于展品运输交接场景的封签链校验服务。客户端提交按时间排列的 **封装 / 交接 / 开封 / 重封** 事件序列，服务按状态机逐事件校验，返回最终保管人、当前封签、逐事件状态及全部违规事件的位置与原因；同时提供**批量封签占用检查**接口，对多条事件链做跨链 `seal_id` 占用冲突检测。

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
  models.py      # 请求 / 响应模型（单链与批量）
  validator.py   # 单链状态机校验核心
  batch.py       # 批量封签占用检查（跨链冲突）
tests/
  test_validator.py
  test_batch.py
Dockerfile
docker-compose.yml
requirements.txt
```
