"""案件与复原相关路由。"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from .. import service
from ..db import get_db
from ..models import CaseCreate, ObservationIn

router = APIRouter(tags=["pillbox"])


@router.post("/cases", status_code=201, summary="建案：录入一周药格计划与药品外观")
def create_case(payload: CaseCreate, conn: sqlite3.Connection = Depends(get_db)):
    return service.create_case(conn, payload)


@router.get("/cases", summary="案件列表")
def list_cases(conn: sqlite3.Connection = Depends(get_db)):
    return {"cases": service.list_cases(conn)}


@router.get("/cases/{case_id}", summary="案件复查：登记信息、观察与快照列表")
def get_case(case_id: str, conn: sqlite3.Connection = Depends(get_db)):
    return service.get_case(conn, case_id)


@router.put("/cases/{case_id}/observation", summary="提交各格残留与散落药片的可见特征")
def put_observation(case_id: str, payload: ObservationIn,
                    conn: sqlite3.Connection = Depends(get_db)):
    return service.put_observation(conn, case_id, payload)


@router.post("/cases/{case_id}/solve", summary="复原求解并保存不可变快照")
def solve_case(case_id: str, conn: sqlite3.Connection = Depends(get_db)):
    return service.solve_case(conn, case_id)


@router.get("/cases/{case_id}/snapshots", summary="复原快照版本列表")
def list_snapshots(case_id: str, conn: sqlite3.Connection = Depends(get_db)):
    return {"case_id": case_id, "snapshots": service.list_snapshots(conn, case_id)}


@router.get("/cases/{case_id}/snapshots/{version}", summary="按版本复查复原快照")
def get_snapshot(case_id: str, version: int,
                 conn: sqlite3.Connection = Depends(get_db)):
    return service.get_snapshot(conn, case_id, version)


@router.get("/cases/{case_id}/export", summary="导出含输入哈希的 JSON")
def export_case(case_id: str,
                version: int | None = Query(default=None,
                                            description="缺省导出最新快照"),
                conn: sqlite3.Connection = Depends(get_db)):
    doc, filename = service.export_case(conn, case_id, version)
    return JSONResponse(
        content=doc,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
