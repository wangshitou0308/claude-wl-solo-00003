"""请求数据模型（Pydantic）。

设计约定：
- 药格固定为 7 天 x 3 个时段（早/中/晚），共 21 格。
- 刻印/颜色/形状在 Pydantic 层允许缺省，由 service 层做领域校验，
  以便按需求返回带业务错误码（如 missing_imprint）且定位字段的 422 响应。
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Slot(str, Enum):
    morning = "morning"  # 早
    noon = "noon"        # 中
    evening = "evening"  # 晚


class CellRef(BaseModel):
    """药格坐标：day 取 1..7（越界由领域校验报 unknown_cell）。"""

    day: int
    slot: Slot


class MedicationIn(BaseModel):
    """建案时登记的一种药品。"""

    med_id: str = Field(min_length=1, max_length=64, description="家属自定义药品编号，如 A、B")
    imprint: Optional[str] = Field(default=None, max_length=128, description="药片刻印，必填")
    color: Optional[str] = Field(default=None, max_length=64, description="颜色，必填")
    shape: Optional[str] = Field(default=None, max_length=64, description="形状，必填")
    dose_per_slot: int = Field(ge=1, le=100, description="每次（每格）粒数")
    cells: Optional[list[CellRef]] = Field(
        default=None, description="计划药格；缺省表示 7 天 x 早中晚全部 21 格"
    )
    stop_date: Optional[dt.date] = Field(
        default=None, description="停服日期（含当天为最后服药日），之后的药格不再计划该药"
    )
    allowed_empty_cells: list[CellRef] = Field(
        default_factory=list, description="允许空格：这些格允许该药缺失（如已提前服用）"
    )


class CaseCreate(BaseModel):
    title: str = Field(default="未命名药盒", max_length=200)
    start_date: Optional[dt.date] = Field(
        default=None, description="药盒第 1 天对应的日期，缺省为建案当天"
    )
    medications: list[MedicationIn] = Field(min_length=1, max_length=50)


class ResidualEntry(BaseModel):
    """某药格中残留的、外观一致的药片数量。"""

    cell: CellRef
    imprint: Optional[str] = Field(default=None, max_length=128)
    color: Optional[str] = Field(default=None, max_length=64)
    shape: Optional[str] = Field(default=None, max_length=64)
    count: int = Field(ge=1, le=100000)


class ScatteredEntry(BaseModel):
    """散落混在一起、外观一致的药片数量。"""

    imprint: Optional[str] = Field(default=None, max_length=128)
    color: Optional[str] = Field(default=None, max_length=64)
    shape: Optional[str] = Field(default=None, max_length=64)
    count: int = Field(ge=1, le=100000)


class ObservationIn(BaseModel):
    """家属清点后提交的可见特征观察。"""

    residual: list[ResidualEntry] = Field(default_factory=list, max_length=2000)
    scattered: list[ScatteredEntry] = Field(default_factory=list, max_length=2000)
