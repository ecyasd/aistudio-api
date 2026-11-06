import json

from aistudio_api.infrastructure.utils import telemetry_store


def test_metadata_survives_memory_reset_and_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("AISTUDIO_TELEMETRY_PERSIST", "1")
    monkeypatch.setenv("AISTUDIO_TELEMETRY_DB", str(tmp_path / "observations.sqlite3"))
    monkeypatch.setattr(telemetry_store, "MAX_REQUESTS", 2)
    monkeypatch.setattr(telemetry_store.time, "time", lambda: 104)
    for index in range(3):
        telemetry_store.save(
            "request", str(index), 100 + index, {"id": str(index), "state": "completed"}
        )
    rows = telemetry_store.recent("request", 10)
    assert [row["id"] for row in rows] == ["2", "1"]
    assert "prompt" not in json.dumps(rows)
