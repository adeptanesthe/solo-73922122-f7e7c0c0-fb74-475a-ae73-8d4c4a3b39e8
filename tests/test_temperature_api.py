"""温控核查 API 的端到端测试（含非法输入精确定位）。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"


def _post(payload):
    return client.post("/api/v1/temperature-check", json=payload)


def test_demo_endpoints_and_docs_available():
    assert client.get("/docs").status_code == 200
    assert client.get("/api/v1/temperature-demo").status_code == 200
    assert client.get("/api/v1/temperature-demo/expected").status_code == 200


def test_main_demo_response_shape_and_singleton_intersection():
    payload = json.loads((DEMO_DIR / "temperature_request.json").read_text(encoding="utf-8"))
    resp = _post(payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["delta_lo"] == -20
    assert body["delta_hi"] == 40
    by_id = {r["sample_id"]: r for r in body["results"]}
    assert by_id["W-002"]["verdict"] == "all"
    assert by_id["W-003"]["verdict"] == "all"
    assert by_id["W-001"]["verdict"] == "partial"
    assert by_id["W-004"]["verdict"] == "partial"
    assert by_id["W-005"]["verdict"] == "partial"
    # 分数端点保持最简分数
    w1 = by_id["W-001"]["feasible_intervals"]
    assert w1 == [
        {"lower": "-20", "upper": "-9/2"},
        {"lower": "9/2", "upper": "40"},
    ]
    # 孤立可行点被保留
    assert by_id["W-004"]["feasible_intervals"] == [{"lower": "40", "upper": "40"}]
    # 全箱交集恰为孤立点
    assert body["box_verdict"] == "partial"
    assert body["intersection"] == [{"lower": "40", "upper": "40"}]


def test_live_expected_matches_demo_get_endpoint():
    payload = client.get("/api/v1/temperature-demo").json()
    posted = _post(payload).json()
    expected = client.get("/api/v1/temperature-demo/expected").json()
    assert posted == expected


def test_bundled_expected_responses_match_live_results():
    for request_name, expected_name in (
        ("temperature_request.json", "temperature_expected_response.json"),
        (
            "temperature_request_no_intersection.json",
            "temperature_expected_no_intersection.json",
        ),
    ):
        payload = json.loads((DEMO_DIR / request_name).read_text(encoding="utf-8"))
        bundled = json.loads((DEMO_DIR / expected_name).read_text(encoding="utf-8"))
        assert _post(payload).json() == bundled


def test_no_intersection_demo_yields_empty_and_none():
    payload = json.loads(
        (DEMO_DIR / "temperature_request_no_intersection.json").read_text(encoding="utf-8")
    )
    body = _post(payload).json()
    assert body["box_verdict"] == "none"
    assert body["intersection"] == []
    by_id = {r["sample_id"]: r for r in body["results"]}
    assert by_id["W-B"]["verdict"] == "none"
    assert by_id["W-B"]["feasible_intervals"] == []


def _assert_422(payload, pointer):
    resp = _post(payload)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    locations = [item["location"] for item in detail]
    assert pointer in locations, locations


def test_reject_missing_root_field():
    payload = {"delta_lo": -5, "delta_hi": 5, "log": []}  # 缺 samples
    _assert_422(payload, "/samples")


def test_reject_unknown_field():
    payload = {
        "delta_lo": -5,
        "delta_hi": 5,
        "log": [{"time": 0, "temp": "5"}, {"time": 1, "temp": "5"}],
        "samples": [
            {
                "id": "S1",
                "entry": 0,
                "exit": 1,
                "lower_temp": "0",
                "upper_temp": "10",
                "max_excursion_seconds": "0",
                "extra": 1,
            }
        ],
    }
    _assert_422(payload, "/samples/0/extra")


def test_reject_non_integer_time_including_boolean():
    base = {
        "delta_lo": -5,
        "delta_hi": 5,
        "log": [{"time": 0, "temp": "5"}, {"time": True, "temp": "5"}],
        "samples": [],
    }
    _assert_422(base, "/log/1/time")


def test_reject_wrong_sample_and_log_counts():
    payload = {
        "delta_lo": -5,
        "delta_hi": 5,
        "log": [{"time": 0, "temp": "5"}],  # 仅 1 点
        "samples": [],
    }
    _assert_422(payload, "/log")

    many_samples = [
        {
            "id": f"S{i}",
            "entry": 0,
            "exit": 1,
            "lower_temp": "0",
            "upper_temp": "10",
            "max_excursion_seconds": "0",
        }
        for i in range(13)
    ]
    payload = {
        "delta_lo": -5,
        "delta_hi": 5,
        "log": [{"time": 0, "temp": "5"}, {"time": 2, "temp": "5"}],
        "samples": many_samples,
    }
    _assert_422(payload, "/samples")


def test_reject_non_strictly_increasing_log_time():
    payload = {
        "delta_lo": -5,
        "delta_hi": 5,
        "log": [{"time": 5, "temp": "5"}, {"time": 5, "temp": "6"}],
        "samples": [
            {
                "id": "S1",
                "entry": 0,
                "exit": 1,
                "lower_temp": "0",
                "upper_temp": "10",
                "max_excursion_seconds": "0",
            }
        ],
    }
    _assert_422(payload, "/log/1/time")


def test_reject_entry_not_before_exit():
    payload = {
        "delta_lo": -5,
        "delta_hi": 5,
        "log": [{"time": 0, "temp": "5"}, {"time": 10, "temp": "5"}],
        "samples": [
            {
                "id": "S1",
                "entry": 5,
                "exit": 5,
                "lower_temp": "0",
                "upper_temp": "10",
                "max_excursion_seconds": "0",
            }
        ],
    }
    _assert_422(payload, "/samples/0/entry")


def test_reject_lower_above_upper():
    payload = {
        "delta_lo": -5,
        "delta_hi": 5,
        "log": [{"time": 0, "temp": "5"}, {"time": 10, "temp": "5"}],
        "samples": [
            {
                "id": "S1",
                "entry": 0,
                "exit": 5,
                "lower_temp": "10",
                "upper_temp": "5",
                "max_excursion_seconds": "0",
            }
        ],
    }
    _assert_422(payload, "/samples/0/lower_temp")


def test_reject_negative_limit_and_bad_decimal():
    payload = {
        "delta_lo": -5,
        "delta_hi": 5,
        "log": [{"time": 0, "temp": "5"}, {"time": 10, "temp": "5"}],
        "samples": [
            {
                "id": "S1",
                "entry": 0,
                "exit": 5,
                "lower_temp": "0",
                "upper_temp": "10",
                "max_excursion_seconds": "-0.1",
            }
        ],
    }
    _assert_422(payload, "/samples/0/max_excursion_seconds")

    payload["samples"][0]["max_excursion_seconds"] = "0"
    payload["log"][0]["temp"] = "1e3"
    _assert_422(payload, "/log/0/temp")


def test_reject_duplicate_sample_ids_and_empty_id():
    sample = {
        "entry": 0,
        "exit": 5,
        "lower_temp": "0",
        "upper_temp": "10",
        "max_excursion_seconds": "0",
    }
    payload = {
        "delta_lo": -5,
        "delta_hi": 5,
        "log": [{"time": 0, "temp": "5"}, {"time": 10, "temp": "5"}],
        "samples": [
            {"id": "S1", **sample},
            {"id": "S1", **sample},
        ],
    }
    _assert_422(payload, "/samples/1/id")

    payload["samples"][1]["id"] = ""
    _assert_422(payload, "/samples/1/id")

    payload["samples"][1]["id"] = "   "
    _assert_422(payload, "/samples/1/id")


def test_reject_inverted_delta_range():
    payload = {
        "delta_lo": 9,
        "delta_hi": 1,
        "log": [{"time": 0, "temp": "5"}, {"time": 10, "temp": "5"}],
        "samples": [
            {
                "id": "S1",
                "entry": 0,
                "exit": 5,
                "lower_temp": "0",
                "upper_temp": "10",
                "max_excursion_seconds": "0",
            }
        ],
    }
    _assert_422(payload, "/delta_lo")


def test_reject_malformed_json_and_nan_constant():
    resp = client.post(
        "/api/v1/temperature-check",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "malformed_json"

    resp = client.post(
        "/api/v1/temperature-check",
        content=b'{"delta_lo": NaN, "delta_hi": 1, "log": [], "samples": []}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422


def test_reject_empty_body_and_non_object_root():
    assert client.post("/api/v1/temperature-check", content=b"").status_code == 422
    resp = _post([1, 2, 3])
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["location"] == ""


def test_temperature_is_number_not_string_rejected():
    payload = {
        "delta_lo": -5,
        "delta_hi": 5,
        "log": [{"time": 0, "temp": 5}, {"time": 10, "temp": "5"}],
        "samples": [
            {
                "id": "S1",
                "entry": 0,
                "exit": 5,
                "lower_temp": "0",
                "upper_temp": "10",
                "max_excursion_seconds": "0",
            }
        ],
    }
    _assert_422(payload, "/log/0/temp")
