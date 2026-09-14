"""统一错误格式：整案拒绝时返回 422 + 字段定位。

响应形如：
{
  "detail": {
    "message": "…",
    "errors": [{"code": "missing_imprint", "loc": ["medications", 0, "imprint"], "message": "…"}]
  }
}
"""

from __future__ import annotations

from typing import Any

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def err(code: str, loc: list[Any], message: str) -> dict[str, Any]:
    return {"code": code, "loc": list(loc), "message": message}


class ApiError(Exception):
    def __init__(self, status_code: int, message: str, errors: list[dict[str, Any]]):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.errors = errors


async def api_error_handler(request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"message": exc.message, "errors": exc.errors}},
    )


async def request_validation_handler(request, exc: RequestValidationError) -> JSONResponse:
    """把 Pydantic/FastAPI 的参数校验错误也归一到统一格式。"""
    errors = [
        err("invalid_input", list(e.get("loc", ())), e.get("msg", "参数不合法"))
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": {"message": "请求参数校验未通过", "errors": errors}},
    )
