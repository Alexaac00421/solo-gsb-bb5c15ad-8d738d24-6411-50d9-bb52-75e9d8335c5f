# 公交换乘票价清分服务

Python 3 / FastAPI + PostgreSQL 实现的公交换乘票价清分（fare clearing）服务。
支持乘车记录幂等提交、04:00 运营日界、90 分钟链式换乘归组、组内取最高票价、
单账户单日实收封顶、迟到补录与跨日更正的自动重算、版本化更正/撤销，
以及按提交顺序递增、仅追加、游标翻页的原子流水。

运营方可发布**按运营日生效的换乘窗口与单日封顶规则**（版本化、乐观并发）：
任一运营日采用不晚于它的最新规则，回溯发布在同一事务内重算覆盖区间内
已有乘次的账户运营日，规则与账单原子可见。

## 一条命令启动

前置：已安装 Docker 与 Docker Compose v2。

```bash
docker compose up --build
```

该命令会：

1. 启动 PostgreSQL 16（账号 `fare` / 密码 `farepass` / 库 `fareclearing`），数据持久化到命名卷 `pgdata`；
2. 等待数据库健康检查通过；
3. 构建并启动 API 容器，自动完成表结构初始化（`app/schema.sql`，幂等）。

启动后：

| 用途 | 地址 |
| --- | --- |
| HTTP API 基地址 | <http://localhost:8080> |
| 健康检查 | <http://localhost:8080/healthz> |
| 交互式接口文档（Swagger UI） | <http://localhost:8080/docs> |
| OpenAPI 描述 | <http://localhost:8080/openapi.json> |
| PostgreSQL | `localhost:5432`（库 `fareclearing`） |

停止：`Ctrl+C` 后 `docker compose down`（保留数据）；连同数据卷一起清除：`docker compose down -v`。

运行调用示例：

```bash
bash scripts/examples.sh            # 默认 http://localhost:8080
```

## 配置项

通过环境变量配置（见 `docker-compose.yml` 与 `.env.example`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://fare:farepass@localhost:5432/fareclearing` | PostgreSQL DSN |
| `APP_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `APP_PORT` | `8080` | HTTP 监听端口（宿主映射也可用同名环境变量覆盖） |
| `DAY_BOUNDARY_HOUR` | `4` | 运营日界（北京时间整点）；`[当日04:00, 次日04:00)` 为当日运营日 |
| `TRANSFER_WINDOW_MINUTES` | `90` | 相邻两程归为同一换乘组的最大间隔（分钟，含端点）；**未发布规则时的兜底默认值** |
| `DAILY_CAP_CENTS` | `2000` | 单账户单运营日实收封顶（分）；**未发布规则时的兜底默认值** |

## 业务规则

- **运营日界**：上车时刻按北京时间（UTC+8）折算；04:00 之前归入前一运营日。
  时间一律以带时区偏移的 ISO 8601 提交（如 `2026-10-06T08:00:00+08:00`），库内统一存 UTC。
- **换乘归组**：同一账户、同一运营日内，先按上车时刻、再按乘次号排序；
  **相邻**两程间隔 ≤ 窗口分钟（默认 90）即归入同一换乘组（链式：只比较相邻两程，不与首程比较）；
  跨运营日绝不合组。
- **组费**：每组取组内最高基础票价。
- **日实收**：当日各组组费之和，封顶（默认 `2000` 分，`min(组费合计, 封顶值)`）；封顶不跨日。
- **运营规则**：运营方可发布按运营日生效的（窗口分钟, 封顶分）规则；任一运营日采用
  **不晚于它的最新规则**，覆盖至下一生效日前；无规则记录时沿用上述默认配置。
  规则版本全局从 1 递增，发布须携带预期版本（从未发布为 `0`）；同一生效日仅允许一条规则。
  回溯发布在**同一事务**内重算覆盖区间 `[生效日, 下一生效日)` 内已有乘次的账户运营日
  （区间内每日按其各自适用规则），并更新换乘组与日结。
- **幂等提交**：乘次号全局唯一；同号同内容重传返回当前状态且 `idempotent=true`、不产生新流水；
  同号异内容返回 `409 IDEMPOTENCY_CONFLICT`。
- **更正 / 撤销**：更正须携带乘次当前 `version`，成功后版本 +1；撤销不推进版本，同版本重复撤销幂等。
  未知乘次返回 `404 UNKNOWN_TRIP`，版本不匹配返回 `409 VERSION_CONFLICT`。
- **重算**：迟到记录或更正（含跨日更正、账户变更）会重算所有受影响的（账户 × 运营日）。
  当日实收发生变化时，流水成对追加 `day_reversal`（冲销旧日金额）与 `day_charge`（记入新金额）；
  实收不变不产生金额流水。
- **原子性**：乘次状态、换乘组、日结、流水、规则发布在单个数据库事务内提交；
  所有写事务经同一事务级 advisory 锁串行化，查询不会见到规则与账单不一致的中间态。
- **流水**：`seq` 按提交顺序全局严格递增且无空洞（回滚的冲突请求不耗号）；
  表级触发器禁止 UPDATE/DELETE（仅追加，已返回流水不可改写）；
  以 `seq > cursor` 的 keyset 分页，翻页不重不漏。

## HTTP API

所有请求/响应均为 JSON。金额单位均为人民币分（非负整数）。

### `POST /trips` —— 提交乘车记录

```bash
curl -X POST http://localhost:8080/trips \
  -H 'Content-Type: application/json' \
  -d '{"trip_id":"T1","account_id":"A001",
       "boarding_at":"2026-10-06T08:00:00+08:00",
       "base_fare_cents":200}'
```

响应 `201`：

```json
{
  "idempotent": false,
  "trip": {
    "trip_id": "T1", "account_id": "A001",
    "boarding_at": "2026-10-06T08:00:00+08:00",
    "base_fare_cents": 200, "status": "active",
    "op_day": "2026-10-06", "version": 1
  }
}
```

### `POST /trips/{trip_id}/correct` —— 更正（凭乘次号 + 当前版本）

```bash
curl -X POST http://localhost:8080/trips/T1/correct \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"A001","boarding_at":"2026-10-06T08:00:00+08:00",
       "base_fare_cents":800,"expected_version":1}'
```

成功 `200`，`version` 递增；内容与当前一致时为幂等重放（`idempotent=true`，版本不变）。
跨运营日（或跨账户）更正会自动重算新旧双方运营日。

### `POST /trips/{trip_id}/cancel?expected_version=1` —— 撤销

成功 `200`，乘次置为 `cancelled` 并从换乘组与日结中剔除、重算当日；
同版本重复撤销幂等。

### `GET /trips/{trip_id}` —— 查询乘次状态（含版本）

### `GET /accounts/{account_id}/days/{op_day}` —— 查询运营日

`op_day` 格式 `YYYY-MM-DD`（北京日期）。返回换乘组（含组内乘次明细）、
封顶前组费合计 `sum_group_fare_cents`、封顶值 `daily_cap_cents` 与实收 `charged_cents`。
归组窗口与 `daily_cap_cents` 均按该运营日**实际适用的规则**（见 `GET /rules/applicable`）计算。

```bash
curl http://localhost:8080/accounts/A001/days/2026-10-06
```

### `GET /accounts/{account_id}/ledger` —— 游标后流水

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `after_seq` | `0` | 只返回 `seq` 严格大于该值的条目 |
| `limit` | `50` | 每页条数（1–200） |
| `op_day` | 无 | 可选，按运营日过滤 |

```bash
curl "http://localhost:8080/accounts/A001/ledger?after_seq=0&limit=50"
```

响应含 `next_cursor`：为 `null` 表示已取完；否则将其作为下一页的 `after_seq`，
即可保证不重不漏。流水事件类型：

| `event_type` | 含义 |
| --- | --- |
| `trip_posted` / `trip_corrected` / `trip_cancelled` | 乘次生命周期（`delta_cents=0`） |
| `day_reversal` | 重算前冲销旧日实收（`delta_cents` 为负，`charged_after_cents=0`） |
| `day_charge` | 重算后记入新实收（`delta_cents` 为正，`charged_after_cents` 为新实收） |
| `rule_published` | 规则发布审计（挂在 `operator` 账户下，`delta_cents=0`） |

### `POST /rules` —— 发布运营规则（按运营日生效）

```bash
curl -X POST http://localhost:8080/rules \
  -H 'Content-Type: application/json' \
  -d '{"effective_day":"2026-10-06","transfer_window_minutes":60,
       "daily_cap_cents":1500,"expected_version":0}'
```

| 字段 | 说明 |
| --- | --- |
| `effective_day` | 生效运营日 `YYYY-MM-DD`（北京日期）；同一生效日仅允许一条规则 |
| `transfer_window_minutes` | 正整数，相邻两程归组最大间隔（分钟） |
| `daily_cap_cents` | 非负整数，单账户单运营日实收封顶（分） |
| `expected_version` | 当前规则版本（从未发布为 `0`）；不符返回 `409 VERSION_CONFLICT` 且不改动任何数据 |

响应 `201`：

```json
{
  "version": 1,
  "effective_day": "2026-10-06",
  "transfer_window_minutes": 60,
  "daily_cap_cents": 1500,
  "coverage_start": "2026-10-06",
  "coverage_end": null,
  "recalculated_account_days": 3
}
```

- 覆盖区间为 `[coverage_start, coverage_end]`；`coverage_end=null` 表示覆盖至未来
  所有运营日（直至下一条规则发布，届时前一条的覆盖上界随之确定）。
- 回溯发布在同一事务内重算覆盖区间内已有乘次的账户运营日（换乘组、日结一并更新），
  `recalculated_account_days` 为本次重算的账户运营日数；实收变化沿用
  `day_reversal` / `day_charge` 成对记入流水，金额不变则不写金额流水。

### `GET /rules` —— 规则列表与当前版本

```json
{"current_version": 1,
 "rules": [{"version": 1, "effective_day": "2026-10-06",
            "transfer_window_minutes": 60, "daily_cap_cents": 1500}]}
```

### `GET /rules/applicable?op_day=YYYY-MM-DD` —— 查询指定运营日适用规则

```bash
curl "http://localhost:8080/rules/applicable?op_day=2026-10-06"
```

```json
{"op_day": "2026-10-06", "source": "rule", "version": 1,
 "effective_day": "2026-10-06",
 "coverage_start": "2026-10-06", "coverage_end": null,
 "transfer_window_minutes": 60, "daily_cap_cents": 1500}
```

无规则记录时 `source=default`、`version=0`，返回环境变量默认配置
（`coverage_start=null`，`coverage_end` 为首条规则生效日前一天，无规则则为 `null`）。

### `GET /healthz` —— 健康检查

## 错误响应

统一格式：

```json
{"error": {"code": "VERSION_CONFLICT", "message": "expected version 2, got 1"}}
```

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 404 | `UNKNOWN_TRIP` | 更正/撤销/查询未知乘次 |
| 409 | `IDEMPOTENCY_CONFLICT` | 同号异内容重复提交 |
| 409 | `VERSION_CONFLICT` | 携带的 `expected_version` 不是当前版本（乘次或规则） |
| 409 | `TRIP_NOT_ACTIVE` | 更正已撤销的乘次 |
| 409 | `RULE_DAY_CONFLICT` | 该生效日已发布过规则 |
| 422 | `INVALID_TIME` | 时间缺失时区偏移、格式非法、年份越界 |
| 422 | `INVALID_FARE` | 票价不是非负整数 |
| 422 | `INVALID_DATE` | 生效日/运营日格式非法或年份越界 |
| 422 | `INVALID_WINDOW` | 换乘窗口不是正整数 |
| 422 | `INVALID_CAP` | 封顶分值不是非负整数 |
| 422 | `INVALID_TRIP_ID` / `INVALID_ACCOUNT_ID` / `INVALID_VERSION` | 对应字段非法 |
| 422 | `INVALID_REQUEST` | 其它请求体校验错误（多余字段等） |

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql://fare:farepass@localhost:5432/fareclearing
uvicorn app.main:app --reload --port 8080   # 首次启动自动建表
```

运行测试（使用便携 PostgreSQL，无需本机安装 PG）：

```bash
pip install -r requirements.txt pytest httpx pgserver
pytest
```

## 项目结构

```
app/
  main.py        FastAPI 路由与异常映射
  service.py     归组/重算/幂等/版本控制/规则发布/流水（事务核心）
  models.py      请求响应模型与输入校验
  db.py          连接池与幂等建表
  schema.sql     表、索引、append-only 触发器、既有库约束迁移
  timeutil.py    北京时间与 04:00 运营日界
  config.py      环境变量配置
scripts/examples.sh   端到端调用示例
docker-compose.yml / Dockerfile / docker-entrypoint.sh
tests/test_clearing.py 业务规则与并发测试
tests/test_rules.py    规则发布/回溯重算/适用查询测试
```
