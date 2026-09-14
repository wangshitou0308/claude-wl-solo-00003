"""业务编排：建案、观察录入、复原求解、快照与导出。

校验分三个阶段，任一阶段发现错误即整案拒绝（422）并定位字段：
  1. 药品登记校验（刻印缺失、编号重复、未知药格、空格不在计划内等）
  2. 观察数据校验（未知外观、重复录入、残留落在无计划格、残留超容量）
  3. 全局闭合求解（数量不闭合 / 约束矛盾 -> 无可行方案）
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import uuid
from typing import Any

from . import solver
from .errors import ApiError, err
from .models import CaseCreate, MedicationIn, ObservationIn
from .solver import Appearance, CellDemand, GroupInput

DISCLAIMER = (
    "本结果仅用于药盒物理复原核对，不构成任何服药、停药或药品处置建议；"
    "隔离清单中的药品请勿自行处理，请咨询医生或药师。"
)

MAX_SOLUTIONS = int(os.environ.get("PILLBOX_MAX_SOLUTIONS", solver.DEFAULT_MAX_SOLUTIONS))
MAX_NODES = int(os.environ.get("PILLBOX_MAX_NODES", solver.DEFAULT_MAX_NODES))


# ---------------------------------------------------------------- 工具

def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _input_hash(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def _norm(s: str | None) -> str:
    return (s or "").strip().casefold()


def _appearance_of(m: MedicationIn) -> Appearance:
    return Appearance(_norm(m.imprint), _norm(m.color), _norm(m.shape))


def _cell_label(day: int, slot: str) -> str:
    return f"第{day}天{solver.SLOT_LABELS.get(slot, slot)}格"


def _appearance_label(a: Appearance) -> str:
    return f"刻印[{a.imprint}]/颜色[{a.color}]/形状[{a.shape}]"


# ---------------------------------------------------------------- 校验

def _validate_medications(meds: list[MedicationIn]) -> list[dict]:
    errors: list[dict] = []
    seen_ids: set[str] = set()
    for i, m in enumerate(meds):
        base = ["medications", i]
        if not _norm(m.imprint):
            errors.append(err("missing_imprint", base + ["imprint"],
                              f"药品 {m.med_id} 缺少刻印，无法建立外观标识"))
        for field_name, label in (("color", "颜色"), ("shape", "形状")):
            if not _norm(getattr(m, field_name)):
                errors.append(err("missing_attribute", base + [field_name],
                                  f"药品 {m.med_id} 缺少{label}"))
        if m.med_id in seen_ids:
            errors.append(err("duplicate_med_id", base + ["med_id"],
                              f"药品编号 {m.med_id} 重复登记"))
        seen_ids.add(m.med_id)
        if m.cells is not None:
            seen_cells: set[tuple[int, str]] = set()
            for j, c in enumerate(m.cells):
                if not 1 <= c.day <= 7:
                    errors.append(err("unknown_cell", base + ["cells", j],
                                      f"药格第 {c.day} 天不存在（一周药盒为第 1~7 天）"))
                    continue
                key = (c.day, c.slot.value)
                if key in seen_cells:
                    errors.append(err("duplicate_cell", base + ["cells", j],
                                      f"药格 {_cell_label(*key)} 在药品 {m.med_id} 的计划中重复"))
                seen_cells.add(key)
        seen_empty: set[tuple[int, str]] = set()
        for j, c in enumerate(m.allowed_empty_cells):
            if not 1 <= c.day <= 7:
                errors.append(err("unknown_cell", base + ["allowed_empty_cells", j],
                                  f"允许空格第 {c.day} 天不存在（一周药盒为第 1~7 天）"))
                continue
            key = (c.day, c.slot.value)
            if key in seen_empty:
                errors.append(err("duplicate_cell", base + ["allowed_empty_cells", j],
                                  f"允许空格 {_cell_label(*key)} 重复登记"))
            seen_empty.add(key)
    return errors


def _effective_schedule(
    start: dt.date, m: MedicationIn
) -> tuple[set[tuple[int, str]], set[tuple[int, str]]]:
    """返回 (必填格, 允许空格)。stop_date 为最后服药日（含当天）。"""
    if m.cells is None:
        cells = {(d, s) for d in range(1, 8) for s in solver.SLOTS}
    else:
        cells = {(c.day, c.slot.value) for c in m.cells}
    if m.stop_date is not None:
        cells = {c for c in cells if start + dt.timedelta(days=c[0] - 1) <= m.stop_date}
    allowed = {(c.day, c.slot.value) for c in m.allowed_empty_cells}
    return cells - allowed, cells & allowed


def _validate_allowed_empty(
    start: dt.date, meds: list[MedicationIn]
) -> list[dict]:
    errors: list[dict] = []
    for i, m in enumerate(meds):
        required, optional = _effective_schedule(start, m)
        scheduled = required | optional
        for j, c in enumerate(m.allowed_empty_cells):
            key = (c.day, c.slot.value)
            if (key not in scheduled) and 1 <= c.day <= 7:
                errors.append(err(
                    "cell_not_in_schedule",
                    ["medications", i, "allowed_empty_cells", j],
                    f"药品 {m.med_id} 的允许空格 {_cell_label(*key)} 不在其服药计划内"
                    "（可能晚于停服日期）",
                ))
    return errors


def _validate_observation(
    appearance_known: dict[Appearance, dict], obs: ObservationIn
) -> list[dict]:
    errors: list[dict] = []

    def check_fields(base: list, e: Any) -> Appearance | None:
        ok = True
        if not _norm(e.imprint):
            errors.append(err("missing_imprint", base + ["imprint"], "观察记录缺少刻印"))
            ok = False
        for field_name, label in (("color", "颜色"), ("shape", "形状")):
            if not _norm(getattr(e, field_name)):
                errors.append(err("missing_attribute", base + [field_name],
                                  f"观察记录缺少{label}"))
                ok = False
        return Appearance(_norm(e.imprint), _norm(e.color), _norm(e.shape)) if ok else None

    seen_residual: set[tuple[int, str, Appearance]] = set()
    for i, e in enumerate(obs.residual):
        base = ["observation", "residual", i]
        if not 1 <= e.cell.day <= 7:
            errors.append(err("unknown_cell", base + ["cell"],
                              f"残留记录指向第 {e.cell.day} 天，一周药盒只有第 1~7 天"))
            continue
        key = check_fields(base, e)
        if key is None:
            continue
        if key not in appearance_known:
            errors.append(err("unknown_appearance", base,
                              f"残留药片外观（{_appearance_label(key)}）"
                              "与本案任何已登记药品都不一致"))
            continue
        dup = (e.cell.day, e.cell.slot.value, key)
        if dup in seen_residual:
            errors.append(err("duplicate_entry", base,
                              f"{_cell_label(e.cell.day, e.cell.slot.value)} 同一外观的"
                              "残留被重复录入，请合并为一条"))
        seen_residual.add(dup)

    seen_scattered: set[Appearance] = set()
    for i, e in enumerate(obs.scattered):
        base = ["observation", "scattered", i]
        key = check_fields(base, e)
        if key is None:
            continue
        if key not in appearance_known:
            errors.append(err("unknown_appearance", base,
                              f"散落药片外观（{_appearance_label(key)}）"
                              "与本案任何已登记药品都不一致"))
            continue
        if key in seen_scattered:
            errors.append(err("duplicate_entry", base,
                              "同一外观的散落药片被重复录入，请合并为一条"))
        seen_scattered.add(key)
    return errors


# ---------------------------------------------------------------- 分组构建

class _Plan:
    """建案数据的求解视图。"""

    def __init__(self, start: dt.date, meds: list[MedicationIn]):
        self.groups: dict[Appearance, GroupInput] = {}
        self.display: dict[Appearance, dict] = {}          # 外观 -> 展示用原始写法
        self.cell_meds: dict[Appearance, dict[int, list[str]]] = {}
        self.planned_by_cell: dict[int, list[dict]] = {}   # 格 -> 计划明细（结果展示用）
        for m in meds:
            key = _appearance_of(m)
            self.display.setdefault(key, {
                "imprint": (m.imprint or "").strip(),
                "color": (m.color or "").strip(),
                "shape": (m.shape or "").strip(),
            })
            group = self.groups.get(key)
            if group is None:
                group = self.groups[key] = GroupInput(
                    appearance=key, med_ids=[], demands={}, residual={},
                    scattered=0, has_optional=False,
                )
                self.cell_meds[key] = {}
            group.med_ids.append(m.med_id)
            required, optional = _effective_schedule(start, m)
            for kind, cells in (("required", required), ("optional", optional)):
                for (d, s) in cells:
                    idx = solver.cell_index(d, s)
                    demand = group.demands.setdefault(idx, CellDemand())
                    if kind == "required":
                        demand.required += m.dose_per_slot
                    else:
                        demand.optional.append(m.dose_per_slot)
                        group.has_optional = True
                    cm = self.cell_meds[key].setdefault(idx, [])
                    if m.med_id not in cm:
                        cm.append(m.med_id)
                    self.planned_by_cell.setdefault(idx, []).append(
                        {"med_id": m.med_id, "dose": m.dose_per_slot, "kind": kind}
                    )


def _apply_observation(
    plan: _Plan, obs: ObservationIn
) -> tuple[list[dict], dict[int, list[dict]]]:
    """把观察填入各外观组；返回 (格级约束错误, 残留展示视图)。"""
    errors: list[dict] = []
    residual_view: dict[int, list[dict]] = {}
    for i, e in enumerate(obs.residual):
        key = Appearance(_norm(e.imprint), _norm(e.color), _norm(e.shape))
        group = plan.groups.get(key)
        if group is None:
            continue  # unknown_appearance 已在上一阶段报告
        idx = solver.cell_index(e.cell.day, e.cell.slot.value)
        demand = group.demands.get(idx)
        if demand is None:
            errors.append(err(
                "constraint_conflict",
                ["observation", "residual", i, "cell"],
                f"{_cell_label(e.cell.day, e.cell.slot.value)}没有任何该外观药品的服药计划"
                f"（可能已过停服日期），却登记残留 {e.count} 粒，约束矛盾",
            ))
            continue
        capacity = max(demand.achievable())
        if e.count > capacity:
            errors.append(err(
                "constraint_conflict",
                ["observation", "residual", i, "count"],
                f"{_cell_label(e.cell.day, e.cell.slot.value)}该外观最多容纳 {capacity} 粒，"
                f"却登记残留 {e.count} 粒，约束矛盾",
            ))
            continue
        group.residual[idx] = group.residual.get(idx, 0) + e.count
        residual_view.setdefault(idx, []).append(
            {"appearance": plan.display[key], "count": e.count}
        )
    for e in obs.scattered:
        key = Appearance(_norm(e.imprint), _norm(e.color), _norm(e.shape))
        group = plan.groups.get(key)
        if group is None:
            continue
        group.scattered += e.count
    return errors, residual_view


# ---------------------------------------------------------------- 结果组装

def _assemble(
    plan: _Plan,
    outcomes: dict[Appearance, solver.GroupOutcome],
    residual_view: dict[int, list[dict]],
) -> dict:
    placements: list[dict] = []
    quarantine: list[dict] = []
    placed_by_cell: dict[int, list[dict]] = {}
    final_range: dict[int, dict[Appearance, list[int]]] = {}
    truncated_any = False
    total_solutions: int | None = 1

    for key, group in plan.groups.items():
        outcome = outcomes[key]
        disp = plan.display[key]
        if outcome.truncated:
            truncated_any = True
            total_solutions = None
            if group.scattered > 0:
                quarantine.append({
                    "appearance": disp,
                    "count": group.scattered,
                    "med_ids": sorted(group.med_ids),
                    "candidate_cells": [solver.cell_ref(c) for c in sorted(group.demands)],
                    "possible_counts": None,
                    "reasons": [
                        f"全局一致方案数量超过枚举上限（{MAX_SOLUTIONS}），"
                        "为确保安全，该外观散落药片全部按无法确定处理"
                    ],
                })
            continue

        assert outcome.solution_count is not None
        if total_solutions is not None:
            total_solutions *= outcome.solution_count

        determined = 0
        for idx, xs in sorted(outcome.x_values.items()):
            certain = xs[0]  # min 规则：所有方案都至少有 xs[0] 粒进入该格
            if certain > 0:
                determined += certain
                entry = {
                    "cell": solver.cell_ref(idx),
                    "appearance": disp,
                    "count": certain,
                    "med_ids": sorted(plan.cell_meds[key].get(idx, [])),
                }
                placements.append(entry)
                placed_by_cell.setdefault(idx, []).append(
                    {"appearance": disp, "count": certain}
                )
            final_range.setdefault(idx, {})[key] = [
                group.residual.get(idx, 0) + xs[0],
                group.residual.get(idx, 0) + xs[-1],
            ]
            if len(xs) > 1:
                pass  # 歧义格，见隔离清单 possible_counts

        left = group.scattered - determined
        if left > 0:
            reasons = []
            if group.has_optional:
                reasons.append("该外观药品的计划含允许空格，空格是否已服药无法从残留判断")
            if len(group.med_ids) > 1:
                reasons.append(
                    "药品 " + "、".join(sorted(group.med_ids)) +
                    " 的刻印/颜色/形状完全相同，散落药片归属存在多种全局一致方案"
                )
            if not reasons:
                reasons.append("散落药片在药格间的分配存在多种全局一致方案")
            quarantine.append({
                "appearance": disp,
                "count": left,
                "med_ids": sorted(group.med_ids),
                "candidate_cells": [
                    solver.cell_ref(idx)
                    for idx, xs in sorted(outcome.x_values.items()) if len(xs) > 1
                ],
                "possible_counts": [
                    {"cell": solver.cell_ref(idx), "counts": xs}
                    for idx, xs in sorted(outcome.x_values.items()) if len(xs) > 1
                ],
                "reasons": reasons,
            })

    placements.sort(key=lambda p: (p["cell"]["day"],
                                   solver.SLOTS.index(p["cell"]["slot"]),
                                   p["appearance"]["imprint"]))
    quarantine.sort(key=lambda q: (q["appearance"]["imprint"],
                                   q["appearance"]["color"],
                                   q["appearance"]["shape"]))

    cells_view = []
    for idx in sorted(set(plan.planned_by_cell) | set(residual_view)):
        entry: dict[str, Any] = {
            "cell": solver.cell_ref(idx),
            "planned": sorted(plan.planned_by_cell.get(idx, []),
                              key=lambda p: p["med_id"]),
            "residual": residual_view.get(idx, []),
            "placed": placed_by_cell.get(idx, []),
        }
        ranges = final_range.get(idx)
        if ranges:
            entry["expected_total"] = [
                {"appearance": plan.display[k], "min": v[0], "max": v[1]}
                for k, v in sorted(ranges.items(), key=lambda kv: kv[0].imprint)
            ]
        cells_view.append(entry)

    return {
        "solution_count": total_solutions,
        "truncated": truncated_any,
        "placements": placements,
        "quarantine": quarantine,
        "cells": cells_view,
    }


# ---------------------------------------------------------------- 持久化用例

def _load_case_row(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if row is None:
        raise ApiError(404, "案件不存在",
                       [err("not_found", ["case_id"], f"案件 {case_id} 不存在")])
    return row


def _load_medications(conn: sqlite3.Connection, case_id: str) -> list[MedicationIn]:
    rows = conn.execute(
        "SELECT payload FROM medications WHERE case_id = ? ORDER BY seq", (case_id,)
    ).fetchall()
    return [MedicationIn.model_validate(json.loads(r["payload"])) for r in rows]


def _load_observation(conn: sqlite3.Connection, case_id: str) -> ObservationIn | None:
    row = conn.execute(
        "SELECT payload FROM observations WHERE case_id = ?", (case_id,)
    ).fetchone()
    return ObservationIn.model_validate(json.loads(row["payload"])) if row else None


def _reject(conn: sqlite3.Connection, case_id: str, message: str,
            errors: list[dict]) -> ApiError:
    """记录整案拒绝并返回 422 异常。"""
    now = _now()
    conn.execute(
        "UPDATE cases SET status = 'rejected', last_error = ?, updated_at = ? WHERE id = ?",
        (_canonical({"message": message, "errors": errors}), now, case_id),
    )
    conn.commit()
    return ApiError(422, message, errors)


def create_case(conn: sqlite3.Connection, payload: CaseCreate) -> dict:
    start = payload.start_date or dt.date.today()
    errors = _validate_medications(payload.medications)
    if not errors:
        errors = _validate_allowed_empty(start, payload.medications)
    if errors:
        raise ApiError(422, "药品登记信息校验未通过，案件未建立", errors)

    case_id = uuid.uuid4().hex
    now = _now()
    conn.execute(
        "INSERT INTO cases (id, title, start_date, status, created_at, updated_at)"
        " VALUES (?, ?, ?, 'draft', ?, ?)",
        (case_id, payload.title, start.isoformat(), now, now),
    )
    for seq, m in enumerate(payload.medications):
        conn.execute(
            "INSERT INTO medications (case_id, seq, med_id, payload) VALUES (?, ?, ?, ?)",
            (case_id, seq, m.med_id, _canonical(m.model_dump(mode="json"))),
        )
    conn.commit()
    return get_case(conn, case_id)


def get_case(conn: sqlite3.Connection, case_id: str) -> dict:
    row = _load_case_row(conn, case_id)
    meds = _load_medications(conn, case_id)
    obs = _load_observation(conn, case_id)
    snapshots = conn.execute(
        "SELECT version, input_hash, created_at FROM snapshots"
        " WHERE case_id = ? ORDER BY version",
        (case_id,),
    ).fetchall()
    return {
        "case_id": row["id"],
        "title": row["title"],
        "start_date": row["start_date"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "medications": [m.model_dump(mode="json") for m in meds],
        "observation": obs.model_dump(mode="json") if obs else None,
        "snapshots": [dict(s) for s in snapshots],
        "last_error": json.loads(row["last_error"]) if row["last_error"] else None,
    }


def list_cases(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT c.id, c.title, c.start_date, c.status, c.created_at,"
        "       (SELECT COUNT(*) FROM medications m WHERE m.case_id = c.id) AS med_count,"
        "       (SELECT COUNT(*) FROM snapshots s WHERE s.case_id = c.id) AS snapshot_count"
        " FROM cases c ORDER BY c.created_at DESC"
    ).fetchall()
    return [
        {"case_id": r["id"], "title": r["title"], "start_date": r["start_date"],
         "status": r["status"], "created_at": r["created_at"],
         "medication_count": r["med_count"], "snapshot_count": r["snapshot_count"]}
        for r in rows
    ]


def put_observation(conn: sqlite3.Connection, case_id: str,
                    payload: ObservationIn) -> dict:
    row = _load_case_row(conn, case_id)
    meds = _load_medications(conn, case_id)
    start = dt.date.fromisoformat(row["start_date"])

    errors = _validate_medications(meds) or _validate_allowed_empty(start, meds)
    if not errors:
        appearance_known = {k: v for k, v in _Plan(start, meds).display.items()}
        errors = _validate_observation(appearance_known, payload)
    if not errors:
        plan = _Plan(start, meds)
        errors, _ = _apply_observation(plan, payload)
    if errors:
        raise _reject(conn, case_id, "观察数据校验未通过，整案拒绝", errors)

    now = _now()
    conn.execute(
        "INSERT INTO observations (case_id, payload, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(case_id) DO UPDATE SET payload = excluded.payload,"
        " updated_at = excluded.updated_at",
        (case_id, _canonical(payload.model_dump(mode="json")), now),
    )
    conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (now, case_id))
    conn.commit()
    return {"case_id": case_id, "observation": payload.model_dump(mode="json"),
            "updated_at": now}


def solve_case(conn: sqlite3.Connection, case_id: str) -> dict:
    row = _load_case_row(conn, case_id)
    meds = _load_medications(conn, case_id)
    obs = _load_observation(conn, case_id)
    if obs is None:
        raise ApiError(409, "尚未提交残留/散落观察，无法复原",
                       [err("missing_observation", ["observation"],
                            "请先通过 PUT /cases/{id}/observation 提交观察数据")])
    start = dt.date.fromisoformat(row["start_date"])

    # 阶段 1：药品登记
    errors = _validate_medications(meds)
    if not errors:
        errors = _validate_allowed_empty(start, meds)
    if errors:
        raise _reject(conn, case_id, "药品登记信息校验未通过，整案拒绝", errors)

    # 阶段 2：观察数据 + 格级约束
    plan = _Plan(start, meds)
    errors = _validate_observation(plan.display, obs)
    residual_view: dict[int, list[dict]] = {}
    if not errors:
        errors, residual_view = _apply_observation(plan, obs)
    if errors:
        raise _reject(conn, case_id, "观察数据与服药计划存在矛盾，整案拒绝", errors)

    # 阶段 3：全局闭合求解
    outcomes: dict[Appearance, solver.GroupOutcome] = {}
    for key, group in plan.groups.items():
        outcome = solver.solve_group(group, MAX_SOLUTIONS, MAX_NODES)
        outcomes[key] = outcome
        if not outcome.feasible:
            observed = sum(group.residual.values()) + group.scattered
            disp = plan.display[key]
            label = (f"刻印[{disp['imprint']}]/颜色[{disp['color']}]"
                     f"/形状[{disp['shape']}]")
            if observed > outcome.max_total:
                why = (f"实际残留+散落共 {observed} 粒，超出全周计划最多可容纳的"
                       f" {outcome.max_total} 粒")
            elif observed < outcome.min_total:
                why = (f"实际残留+散落共 {observed} 粒，不足以填满必填药格所需的"
                       f" {outcome.min_total} 粒")
            else:
                why = (f"实际残留+散落共 {observed} 粒，"
                       "无法在满足各格剂量与停服约束下完成闭合分配")
            errors.append(err(
                "quantity_mismatch", ["observation"],
                f"外观（{label}）涉及药品 {sorted(group.med_ids)}：{why}，数量不闭合",
            ))
    if errors:
        raise _reject(conn, case_id, "药片数量与服药计划不闭合，整案拒绝", errors)

    result = _assemble(plan, outcomes, residual_view)

    # 不可变快照
    input_payload = {
        "title": row["title"],
        "start_date": row["start_date"],
        "medications": [m.model_dump(mode="json") for m in meds],
        "observation": obs.model_dump(mode="json"),
    }
    input_hash = _input_hash(input_payload)
    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM snapshots WHERE case_id = ?",
        (case_id,),
    ).fetchone()["v"]
    now = _now()
    snapshot_id = uuid.uuid4().hex
    result_doc = {
        "case_id": case_id,
        "snapshot_id": snapshot_id,
        "version": version,
        "status": "solved",
        "generated_at": now,
        "input_hash": input_hash,
        **result,
        "disclaimer": DISCLAIMER,
    }
    conn.execute(
        "INSERT INTO snapshots (id, case_id, version, input_hash, input_json,"
        " result_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (snapshot_id, case_id, version, input_hash,
         _canonical(input_payload), _canonical(result_doc), now),
    )
    conn.execute(
        "UPDATE cases SET status = 'solved', last_error = NULL, updated_at = ?"
        " WHERE id = ?",
        (now, case_id),
    )
    conn.commit()
    return result_doc


def _load_snapshot(conn: sqlite3.Connection, case_id: str,
                   version: int | None) -> sqlite3.Row:
    _load_case_row(conn, case_id)
    if version is None:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE case_id = ? ORDER BY version DESC LIMIT 1",
            (case_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE case_id = ? AND version = ?",
            (case_id, version),
        ).fetchone()
    if row is None:
        raise ApiError(404, "快照不存在",
                       [err("not_found", ["snapshots", version],
                            f"案件 {case_id} 没有该版本的复原快照")])
    return row


def list_snapshots(conn: sqlite3.Connection, case_id: str) -> list[dict]:
    _load_case_row(conn, case_id)
    rows = conn.execute(
        "SELECT version, input_hash, created_at FROM snapshots"
        " WHERE case_id = ? ORDER BY version",
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_snapshot(conn: sqlite3.Connection, case_id: str, version: int) -> dict:
    row = _load_snapshot(conn, case_id, version)
    return {
        "case_id": case_id,
        "version": row["version"],
        "created_at": row["created_at"],
        "input_hash": row["input_hash"],
        "input": json.loads(row["input_json"]),
        "result": json.loads(row["result_json"]),
    }


def export_case(conn: sqlite3.Connection, case_id: str,
                version: int | None) -> tuple[dict, str]:
    row = _load_snapshot(conn, case_id, version)
    doc = {
        "case_id": case_id,
        "version": row["version"],
        "exported_at": _now(),
        "hash_algorithm": "sha256",
        "input_hash": row["input_hash"],
        "input": json.loads(row["input_json"]),
        "result": json.loads(row["result_json"]),
        "disclaimer": DISCLAIMER,
    }
    filename = f"pillbox_{case_id}_v{row['version']}.json"
    return doc, filename
