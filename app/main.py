"""药盒复原 API（纯后端，不接任何在线识别或医疗服务）。

一周药盒打翻后，根据建案时登记的服药计划与药品外观（刻印/颜色/形状）、
各格残留与散落药片的可见特征，枚举全部全局一致的归位方案：
只有所有方案都一致确定的药片才判定"可归位"，其余一律进入隔离清单。
本服务不提供任何服药建议。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from .db import init_db
from .errors import ApiError, api_error_handler, request_validation_handler
from .routers import cases


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="药盒复原 API",
    version="1.0.0",
    description=__doc__,
    lifespan=lifespan,
)
app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(RequestValidationError, request_validation_handler)
app.include_router(cases.router)


@app.get("/health", tags=["meta"], summary="健康检查")
def health():
    return {"status": "ok"}
