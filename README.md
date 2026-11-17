# 园区公共用电逐小时净电量 API

给分析库交付"每小时公共用电"：总表相邻整点差值减去**直接下级表**差值之和，
避免总表与租户分表重复计量。孙表及更深的表**不再扣减**（其电量已含在上级分表内）。

计数器**回绕**（如量程 M=1000 时读数 999→3）不再被误判为倒退：采集员按表号和
UTC 小时登记回绕确认（附计数周期 M），确认后该小时差值按 `M - 起值 + 终值` 折算。

技术栈：FastAPI + SQLite（WAL 模式），Python 3.11。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 `8000` 端口，交互式文档在 **http://localhost:8000/docs**。
SQLite 数据文件落在名为 `energy-data` 的卷里（容器内路径 `/data/energy.db`，
可用环境变量 `ENERGY_DB_PATH` 覆盖）。

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest                 # 运行测试
```

## 在 /docs 上演示

1. **上传整版层级**：`POST /api/hierarchy`，选择 `examples/hierarchy.csv`
   （内容为 `M1 -> T1, T2` 且 `T1 -> S1`）。整版语义：每次上传**全量替换**旧层级。
2. **补传读数**：`POST /api/readings`，选择 `examples/readings.csv`
   （M1/T1/T2/S1 在 2026-01-15 全天 25 个整点的累计读数）。
3. **查询净电量**：`GET /api/meters/{meter_id}/daily-net`，
   填 `meter_id=M1`、`date=2026-01-15`。
   预期每小时 `net_kwh = 100 - 30 - 20 = 50`（孙表 S1 的 10 不再扣）。
4. **演示拒收**：用 `examples/readings_bad_conflict.csv`（批内同键异值）或
   `examples/hierarchy_cycle.csv`（层级成环）上传，返回 422 且已有数据不变；
   `GET /api/batches` 可看到被拒批次的留痕。
5. **演示回绕确认**：上传 `examples/readings_wrap.csv`（W1 在 02:00=999 → 03:00=3
   回绕），先查 `GET /api/meters/W1/daily-net?date=2026-01-15`，
   可见 02:00 小时 `pending_review`（读数倒退）；再调
   `POST /api/meters/W1/wrap-confirmations`，Body 填
   `{"hour_start": "2026-01-15T02:00:00Z", "modulus": 1000}`；
   重查日净电量，02:00 小时转为 `ok`，差值 = 1000 − 999 + 3 = 4，
   `wrap_adjusted_meters` 标出被折算的表。`GET /api/wrap-confirmations`
   可查看当前生效的全部确认。

命令行等价演示：

```bash
curl -F "file=@examples/hierarchy.csv" http://localhost:8000/api/hierarchy
curl -F "file=@examples/readings.csv"  http://localhost:8000/api/readings
curl "http://localhost:8000/api/meters/M1/daily-net?date=2026-01-15"

# 回绕确认演示
curl -F "file=@examples/readings_wrap.csv" http://localhost:8000/api/readings
curl -X POST http://localhost:8000/api/meters/W1/wrap-confirmations \
  -H "Content-Type: application/json" \
  -d '{"hour_start": "2026-01-15T02:00:00Z", "modulus": 1000}'
curl "http://localhost:8000/api/meters/W1/daily-net?date=2026-01-15"
curl "http://localhost:8000/api/wrap-confirmations?meter_id=W1"
```

## CSV 格式

### 整版层级（`POST /api/hierarchy`）

```csv
parent_meter,child_meter
M1,T1
M1,T2
T1,S1
```

- 表头别名：`parent/上级表`、`child/下级表` 也可；UTF-8（可带 BOM）。
- 每对 `(上级表, 下级表)` 一条边；一只表至多一个上级。
- **整版替换**：上传成功即全量替换旧层级，历史日期的查询结果随之按新关系重算。

### 一批读数（`POST /api/readings`）

```csv
meter_id,ts_utc,kwh
M1,2026-01-15T00:00:00Z,10000.0
M1,2026-01-15T01:00:00Z,10100.0
```

- 表头别名：`meter/表号`、`timestamp/UTC整点`、`读数` 也可。
- 时间必须是 **UTC 整点**（`Z` 或 `+00:00`，分秒为 0），其它时区/非整点整批拒收。
- 键 = `(表号, UTC整点)`。

## 业务规则

**逐小时净电量**（`GET /api/meters/{meter_id}/daily-net?date=YYYY-MM-DD`，UTC 日）：

- 小时 `h` 需要 `h` 与 `h+1` 两个整点读数（全天共 25 个点，含次日 00:00）。
- `净电量 = 总表差值 - Σ 直接下级表差值`；孙表不再扣。
- 以下情况该小时 `status=pending_review`，只给待核原因、不给数值：
  - 参与表（总表 + 全部直接下级）缺任一整点读数；
  - 任一参与表读数倒退（差值为负）且该（表, 小时）**没有**回绕确认；
  - 净电量为负。

**回绕确认**（`POST /api/meters/{meter_id}/wrap-confirmations`）：

- 请求体：`{"hour_start": "<UTC整点>", "modulus": <计数周期 M>}`，
  小时指 `hour_start` 到 `hour_start+1h`（可跨日，如 23:00→次日 00:00）。
- 受理条件（任一不满足即 422，不产生任何数据）：
  - 该小时起、止两个整点读数齐全；
  - 起值、终值都在 `[0, M)` 内（M 须为正有限数）；
  - 终值 < 起值（确实发生回绕）。
- 生效后：该表该小时差值按 `M - 起值 + 终值` 折算，再沿用直属分表扣减规则；
  未确认的倒退、折算后仍为负的净值仍按原规则待核。
- **失效**：读数批次"真正更正"任一整点（键已存在且值不同）时，依赖该点的
  所有确认（无论该点是小时起点还是终点）在同一事务内被删除——之后即使读数
  改回原值，旧确认也**不能复活**，须按当前读数重新登记；
  同值重传不算更正，不影响确认。
- 同一（表, 小时）重复登记 = 按当前读数更新确认。
- `GET /api/wrap-confirmations`（可选 `?meter_id=`）查看当前生效的确认。

**拒收规则**（整次拒收，HTTP 422，已有数据不受影响）：

- 层级：成环、自环、一表多上级；
- 读数：一批内同键异值、非 UTC 整点、非数值读数。

**更正与幂等**：

- 历史同键异值 = 更正，覆盖旧值，旧日期结果随之更新；
- 同值重传 = 幂等，结果与数据库 `updated_at` 都不变；
- 层级整版替换后，历史日期按新层级重算（结果实时计算，不物化缓存）。

**一致性**：

- 每个请求一个事务；上传先做完所有校验再 `BEGIN IMMEDIATE` 落库，
  任一步失败即回滚——失败后旧层级、旧读数、旧确认、旧查询结果全部可用；
- 登记确认在同一个 `BEGIN IMMEDIATE` 事务内读取并校验起止读数再写入，
  确认所依据的读数与确认本身同源同提交；
- 查询在单个读事务（WAL 快照）内取层级、读数与回绕确认，与交错进行的上传/
  登记互不影响：一次查询看到的必是同一已提交状态，不会混杂新旧数据。

## 项目结构

```
app/
  main.py     # FastAPI 路由与事务边界（含回绕确认登记/查询）
  db.py       # SQLite 连接、schema、WAL
  parsing.py  # CSV 解析与纯函数校验（拒收规则）
  core.py     # 逐小时净电量计算（待核规则 + 回绕折算）
tests/test_api.py    # 层级/读数/一致性场景（17 个）
tests/test_wrap.py   # 回绕确认场景（15 个）
examples/            # 演示 CSV（含会被拒收的反面示例与回绕示例）
Dockerfile / docker-compose.yml
```

## 测试

```bash
pytest
```

覆盖：嵌套扣减（孙表不扣）、多直接下级、缺整点（含次日 00:00 边界）、
读数倒退、净值为负、批内同键异值整批拒收、成环/自环/多上级拒收、
历史更正更新旧结果、同值重传幂等、层级替换更新历史日、
失败上传后旧结果可用、上传与查询并发交错的快照一致性；
回绕确认：未确认倒退待核、确认后按 M−起值+终值 折算、登记校验
（读数不齐/越出 [0,M)/终值不小于起值/非法 M/非法整点）、
重复登记按当前读数更新、真正更正使确认失效且改回不复活、
同值重传与无关点更正在确认保留、跨日小时（23:00→次日 00:00）确认与失效、
子表回绕参与扣减、折算后负净值仍待核。
