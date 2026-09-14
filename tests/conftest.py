import os

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例一个独立的临时 SQLite 库。"""
    monkeypatch.setenv("PILLBOX_DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_case_payload(**overrides):
    """标准案例：A 每天早上 1 粒；B 每天晚 2 粒，第 5 天（含）后停服，
    第 2、3 天晚上为允许空格。"""
    payload = {
        "title": "张奶奶的一周药盒",
        "start_date": "2026-09-14",
        "medications": [
            {
                "med_id": "A",
                "imprint": "ABC",
                "color": "白色",
                "shape": "圆形",
                "dose_per_slot": 1,
                "cells": [{"day": d, "slot": "morning"} for d in range(1, 8)],
            },
            {
                "med_id": "B",
                "imprint": "XYZ",
                "color": "黄色",
                "shape": "椭圆",
                "dose_per_slot": 2,
                "cells": [{"day": d, "slot": "evening"} for d in range(1, 8)],
                "stop_date": "2026-09-18",
                "allowed_empty_cells": [
                    {"day": 2, "slot": "evening"},
                    {"day": 3, "slot": "evening"},
                ],
            },
        ],
    }
    payload.update(overrides)
    return payload
