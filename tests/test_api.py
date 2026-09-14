"""API 端到端测试：建案 -> 观察 -> 复原 -> 快照/导出，以及各类整案拒绝。"""

import json
import os
import sqlite3

import pytest

from tests.conftest import make_case_payload


def create_case(client, **overrides):
    resp = client.post("/cases", json=make_case_payload(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()["case_id"]


def put_observation(client, case_id, observation):
    return client.put(f"/cases/{case_id}/observation", json=observation)


# ---------------------------------------------------------------- 正常流程

def test_full_flow_with_quarantine(client):
    case_id = create_case(client)
    # A：第 1 天早残留 1 粒，散落 6 粒（第 2~7 天早各 1 粒，全部可归位）
    # B：第 1/4/5 天晚必填各 2 粒，第 2/3 天晚为允许空格；
    #    散落 8 粒 -> 必填 6 粒可归位，剩 2 粒在第 2/3 天之间无法确定 -> 隔离
    obs = {
        "residual": [
            {"cell": {"day": 1, "slot": "morning"}, "imprint": "ABC",
             "color": "白色", "shape": "圆形", "count": 1}
        ],
        "scattered": [
            {"imprint": "ABC", "color": "白色", "shape": "圆形", "count": 6},
            {"imprint": "XYZ", "color": "黄色", "shape": "椭圆", "count": 8},
        ],
    }
    resp = put_observation(client, case_id, obs)
    assert resp.status_code == 200, resp.text

    resp = client.post(f"/cases/{case_id}/solve")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["status"] == "solved"
    assert result["version"] == 1
    assert result["solution_count"] == 2
    assert result["truncated"] is False
    assert result["input_hash"].startswith("sha256:")
    assert "不构成任何服药" in result["disclaimer"]

    placed_total = sum(p["count"] for p in result["placements"])
    quarantined_total = sum(q["count"] for q in result["quarantine"])
    assert placed_total + quarantined_total == 14  # 散落总数守恒
    assert quarantined_total == 2

    qua = result["quarantine"][0]
    assert qua["med_ids"] == ["B"]
    assert {c["day"] for c in qua["candidate_cells"]} == {2, 3}
    assert qua["possible_counts"]
    assert any("允许空格" in r for r in qua["reasons"])

    # 快照复查
    snaps = client.get(f"/cases/{case_id}/snapshots").json()["snapshots"]
    assert len(snaps) == 1 and snaps[0]["version"] == 1
    snap = client.get(f"/cases/{case_id}/snapshots/1").json()
    assert snap["input_hash"] == result["input_hash"]
    assert snap["input"]["observation"] == obs
    assert snap["result"]["placements"] == result["placements"]

    # 导出含输入哈希
    resp = client.get(f"/cases/{case_id}/export")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    exported = resp.json()
    assert exported["input_hash"] == result["input_hash"]
    assert exported["hash_algorithm"] == "sha256"

    # 再次求解 -> 版本递增，旧快照不变
    resp = client.post(f"/cases/{case_id}/solve")
    assert resp.json()["version"] == 2
    snaps = client.get(f"/cases/{case_id}/snapshots").json()["snapshots"]
    assert [s["version"] for s in snaps] == [1, 2]
    snap1 = client.get(f"/cases/{case_id}/snapshots/1").json()
    assert snap1["result"]["version"] == 1

    # 案件复查
    detail = client.get(f"/cases/{case_id}").json()
    assert detail["status"] == "solved"
    assert len(detail["medications"]) == 2
    assert len(detail["snapshots"]) == 2


def test_fully_determined_case_has_empty_quarantine(client):
    case_id = create_case(client)
    # B 散落 10 粒 -> 必填 6 + 两个允许空格各 2，全部唯一确定
    obs = {
        "residual": [
            {"cell": {"day": d, "slot": "morning"}, "imprint": "ABC",
             "color": "白色", "shape": "圆形", "count": 1}
            for d in range(1, 8)
        ],
        "scattered": [
            {"imprint": "XYZ", "color": "黄色", "shape": "椭圆", "count": 10}
        ],
    }
    assert put_observation(client, case_id, obs).status_code == 200
    result = client.post(f"/cases/{case_id}/solve").json()
    assert result["solution_count"] == 1
    assert result["quarantine"] == []
    assert sum(p["count"] for p in result["placements"]) == 10


# ---------------------------------------------------------------- 整案拒绝

def test_missing_imprint_rejected_at_create(client):
    payload = make_case_payload()
    payload["medications"][0]["imprint"] = "  "
    resp = client.post("/cases", json=payload)
    assert resp.status_code == 422
    errors = resp.json()["detail"]["errors"]
    assert errors[0]["code"] == "missing_imprint"
    assert errors[0]["loc"] == ["medications", 0, "imprint"]


def test_unknown_cell_rejected(client):
    case_id = create_case(client)
    obs = {
        "residual": [
            {"cell": {"day": 9, "slot": "morning"}, "imprint": "ABC",
             "color": "白色", "shape": "圆形", "count": 1}
        ],
        "scattered": [],
    }
    resp = put_observation(client, case_id, obs)
    assert resp.status_code == 422
    errors = resp.json()["detail"]["errors"]
    assert errors[0]["code"] == "unknown_cell"
    assert errors[0]["loc"] == ["observation", "residual", 0, "cell"]


def test_unknown_appearance_rejected(client):
    case_id = create_case(client)
    obs = {
        "residual": [],
        "scattered": [
            {"imprint": "NOPE", "color": "黑色", "shape": "三角形", "count": 3}
        ],
    }
    resp = put_observation(client, case_id, obs)
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"][0]["code"] == "unknown_appearance"


def test_residual_after_stop_date_is_constraint_conflict(client):
    case_id = create_case(client)
    obs = {
        "residual": [
            {"cell": {"day": 6, "slot": "evening"}, "imprint": "XYZ",
             "color": "黄色", "shape": "椭圆", "count": 1}
        ],
        "scattered": [],
    }
    resp = put_observation(client, case_id, obs)
    assert resp.status_code == 422
    errors = resp.json()["detail"]["errors"]
    assert errors[0]["code"] == "constraint_conflict"
    assert "停服" in errors[0]["message"]


def test_quantity_mismatch_rejected_and_recorded(client):
    case_id = create_case(client)
    # A 全周必填 7 粒，只找到 3 粒 -> 数量不闭合
    obs = {
        "residual": [],
        "scattered": [
            {"imprint": "ABC", "color": "白色", "shape": "圆形", "count": 3},
            {"imprint": "XYZ", "color": "黄色", "shape": "椭圆", "count": 6},
        ],
    }
    assert put_observation(client, case_id, obs).status_code == 200
    resp = client.post(f"/cases/{case_id}/solve")
    assert resp.status_code == 422
    errors = resp.json()["detail"]["errors"]
    assert any(e["code"] == "quantity_mismatch" for e in errors)
    detail = client.get(f"/cases/{case_id}").json()
    assert detail["status"] == "rejected"
    assert detail["last_error"]["errors"] == errors


def test_allowed_empty_outside_schedule_rejected(client):
    payload = make_case_payload()
    payload["medications"][1]["allowed_empty_cells"] = [
        {"day": 6, "slot": "evening"}  # 停服日（第 5 天）之后
    ]
    resp = client.post("/cases", json=payload)
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"][0]["code"] == "cell_not_in_schedule"


def test_duplicate_med_id_rejected(client):
    payload = make_case_payload()
    payload["medications"][1]["med_id"] = "A"
    resp = client.post("/cases", json=payload)
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"][0]["code"] == "duplicate_med_id"


def test_solve_without_observation_conflict(client):
    case_id = create_case(client)
    resp = client.post(f"/cases/{case_id}/solve")
    assert resp.status_code == 409
    assert resp.json()["detail"]["errors"][0]["code"] == "missing_observation"


def test_missing_case_404(client):
    assert client.get("/cases/nope").status_code == 404
    assert client.post("/cases/nope/solve").status_code == 404


# ---------------------------------------------------------------- 不可变性

def test_snapshot_immutable_at_db_level(client):
    case_id = create_case(client)
    obs = {
        "residual": [
            {"cell": {"day": d, "slot": "morning"}, "imprint": "ABC",
             "color": "白色", "shape": "圆形", "count": 1}
            for d in range(1, 8)
        ],
        "scattered": [
            {"imprint": "XYZ", "color": "黄色", "shape": "椭圆", "count": 10}
        ],
    }
    assert put_observation(client, case_id, obs).status_code == 200
    assert client.post(f"/cases/{case_id}/solve").status_code == 200

    conn = sqlite3.connect(os.environ["PILLBOX_DB_PATH"])
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE snapshots SET result_json = '{}' ")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM snapshots")
    finally:
        conn.close()
