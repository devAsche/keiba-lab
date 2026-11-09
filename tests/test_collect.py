"""収集の流れのテスト（通信は偽物に差し替える）。"""

from keibalab import collect_history, config, db


def test_backfill_skips_a_failing_meeting(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "keiba.sqlite")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "MODEL_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "reports")
    conn = db.connect()
    for mid, day in (("23050101", "2023-01-07"), ("23050102", "2023-01-08")):
        conn.execute("INSERT INTO meetings (meeting_id, date, kai, track, nichi) VALUES (?,?,1,'東京',1)", (mid, day))
    conn.execute("INSERT INTO months (ym, fetched_at) VALUES ('2023-01', 'x')")
    conn.commit()
    conn.close()

    def fake_collect(conn, meeting):
        if meeting["meeting_id"] == "23050102":
            raise RuntimeError("HTTP Error 502: Bad Gateway")
        return True

    monkeypatch.setattr(collect_history, "collect_meeting", fake_collect)
    monkeypatch.setattr(collect_history, "refresh_schedule", lambda *a, **k: None)
    out = collect_history.backfill("2023-01", "2023-01")
    assert out == {"meetings_done": 1, "meetings_remaining": 1}
