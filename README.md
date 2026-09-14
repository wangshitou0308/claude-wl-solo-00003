# 药盒复原 API（pillbox-api）

纯后端的一周药盒复原服务：药盒打翻、部分药片混在一起后，根据**建案时登记的服药计划**与
**各格残留 / 散落药片的可见特征**，枚举全部全局一致的归位方案，帮助家属隔着电话也能
稳妥地指导老人把药片放回原格。

- 技术栈：Python 3.12 · FastAPI · Pydantic · SQLite（标准库 `sqlite3`）
- 本机运行：`docker compose up --build`，不接任何在线识别或医疗服务
- **本服务不提供任何服药建议**；无法确定归属的药片一律进入隔离清单

## 快速开始

```bash
docker compose up --build        # 服务监听 http://localhost:8000
curl http://localhost:8000/health
# 交互式接口文档：http://localhost:8000/docs
```

不使用 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt
pytest
```

## 使用流程

1. **建案** `POST /cases`：录入 7 天 × 早中晚共 21 格的计划。每种药登记：
   `med_id`（编号）、`imprint`（刻印）、`color`（颜色）、`shape`（形状）、
   `dose_per_slot`（每次粒数）、`cells`（计划药格，缺省为全部 21 格）、
   `stop_date`（停服日期，含当天为最后服药日）、`allowed_empty_cells`（允许空格）。
2. **提交观察** `PUT /cases/{id}/observation`：家属电话指导老人清点，
   录入各格 `residual`（残留）与 `scattered`（散落）药片的可见特征与数量。
3. **复原求解** `POST /cases/{id}/solve`：返回可归位清单与隔离清单，
   同时保存一份**不可变快照**。
4. **复查 / 导出**：`GET /cases/{id}`、`GET /cases/{id}/snapshots[/{version}]`、
   `GET /cases/{id}/export`（导出含输入哈希的 JSON 文件）。

### 最小示例

```bash
# 1. 建案：A 每天早 1 粒；B 每天晚 2 粒，9-18（含）后停服，第 2、3 天晚允许空格
curl -X POST localhost:8000/cases -H 'Content-Type: application/json' -d '{
  "title": "张奶奶的一周药盒", "start_date": "2026-09-14",
  "medications": [
    {"med_id":"A","imprint":"ABC","color":"白色","shape":"圆形","dose_per_slot":1,
     "cells":[{"day":1,"slot":"morning"},{"day":2,"slot":"morning"},{"day":3,"slot":"morning"},
              {"day":4,"slot":"morning"},{"day":5,"slot":"morning"},{"day":6,"slot":"morning"},
              {"day":7,"slot":"morning"}]},
    {"med_id":"B","imprint":"XYZ","color":"黄色","shape":"椭圆","dose_per_slot":2,
     "cells":[{"day":1,"slot":"evening"},{"day":2,"slot":"evening"},{"day":3,"slot":"evening"},
              {"day":4,"slot":"evening"},{"day":5,"slot":"evening"},{"day":6,"slot":"evening"},
              {"day":7,"slot":"evening"}],
     "stop_date":"2026-09-18",
     "allowed_empty_cells":[{"day":2,"slot":"evening"},{"day":3,"slot":"evening"}]}
  ]}'

# 2. 提交观察：第 1 天早残留 A×1；散落 A×6、B×8
curl -X PUT localhost:8000/cases/<case_id>/observation -H 'Content-Type: application/json' -d '{
  "residual": [{"cell":{"day":1,"slot":"morning"},"imprint":"ABC","color":"白色","shape":"圆形","count":1}],
  "scattered": [{"imprint":"ABC","color":"白色","shape":"圆形","count":6},
                {"imprint":"XYZ","color":"黄色","shape":"椭圆","count":8}]}'

# 3. 求解
curl -X POST localhost:8000/cases/<case_id>/solve
```

## 判定规则

- 外观（刻印+颜色+形状）相同的药片视为**不可区分项**，按外观组独立求解。
- 一个方案须同时满足：每格最终内容符合该格计划（必填格足量、允许空格可为空）、
  原格残留保持不动、散落药片全部归位、停服日期之后不出现该药。
- **可归位**：某格某外观的入格粒数在*所有*全局一致方案中的最小值（每粒不可区分，
  最小值粒数放入该格与任何方案都不冲突）。
- **隔离清单**：其余散落药片一律隔离，并给出造成歧义的药品（`med_ids`）、
  候选药格（`candidate_cells`）、各候选格可能的粒数（`possible_counts`）及原因。
- 方案数超过枚举上限（默认 20000，可用 `PILLBOX_MAX_SOLUTIONS` 调整）时，
  该外观的散落药片**保守地全部隔离**。

## 整案拒绝（422，定位字段）

响应统一为 `{"detail": {"message": …, "errors": [{"code", "loc", "message"}]}}`：

| code | 含义 |
| --- | --- |
| `missing_imprint` | 药品或观察记录缺少刻印 |
| `missing_attribute` | 缺少颜色/形状 |
| `duplicate_med_id` / `duplicate_cell` / `duplicate_entry` | 重复登记/录入 |
| `unknown_cell` | 药格不存在（第 1~7 天 × 早中晚之外） |
| `cell_not_in_schedule` | 允许空格不在该药计划内（如晚于停服日期） |
| `unknown_appearance` | 观察到的外观与任何已登记药品都不一致 |
| `constraint_conflict` | 残留落在无计划的格 / 残留超过该格容量 |
| `quantity_mismatch` | 全周数量不闭合（过多、不足或无法闭合分配） |
| `missing_observation`（409） | 尚未提交观察就请求求解 |

## 快照与导出

- 每次求解保存一份不可变快照（SQLite 触发器禁止 UPDATE/DELETE），版本号递增。
- 快照内含完整输入与结果，`input_hash` 为输入规范化 JSON 的 SHA-256。
- `GET /cases/{id}/export[?version=N]` 下载含输入哈希的 JSON 文件，便于存档核对。

## 目录结构

```
app/
  main.py       FastAPI 入口（lifespan 建库、异常归一）
  models.py     请求模型（Pydantic）
  solver.py     纯函数求解器：按外观组回溯枚举全部一致方案
  service.py    三阶段校验、结果组装、快照持久化
  db.py         SQLite 连接与建表（含快照不可变触发器）
  routers/      案件路由
tests/          求解器单测 + API 端到端测试
Dockerfile / docker-compose.yml
```

## 免责说明

本服务仅依据登记计划与可见特征做药盒物理复原核对，不构成任何服药、停药或药品处置
建议；隔离清单中的药品请勿自行处理，请咨询医生或药师。
