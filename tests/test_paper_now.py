import io
import json
from datetime import datetime

import pytest

from scripts import paper_now as monitor


@pytest.fixture
def job(tmp_path, monkeypatch):
    path = tmp_path / "paper.json"
    path.write_text(json.dumps({"status": "OPEN", "exit_at": "2026-10-01T15:10+05:30", "legs": [
        {"key": "option", "symbol": "TESTCE", "qty": 20, "tick_size": .05,
         "trigger": 130, "limit": 132.6}
    ]}))

    class Clock(datetime):
        current = datetime(2026, 10, 1, 14, 50, tzinfo=monitor.IST)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(monitor, "datetime", Clock)
    # Fail promptly if a regression leaves the management loop waiting forever.
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 4:
            raise KeyboardInterrupt("loop did not finish")

    monkeypatch.setattr(monitor.time, "sleep", sleep)
    return path, Clock


def snapshot(price, mode="dry", stale=False):
    return {"token": "test-token", "state": {"mode": mode, "source": "broker",
        "orders": [], "contracts": [], "positions": [
            {"key": "option", "ltp": price, "stale": stale}], "time": "test-time"}}


def close_order(order_id):
    return {"id": order_id, "mode": "dry", "status": "FILLED", "filled_qty": 20,
            "fill_price": 131.1, "time": "test-time"}


def install_post(monkeypatch, path):
    posts = []

    def post(request, timeout):
        payload = json.loads(request.data)
        saved = json.loads(path.read_text())
        assert saved["legs"][0]["exit_id"] == payload["client_id"]
        assert payload["mode"] == "dry" and payload["side"] == "B"
        posts.append(payload)
        return io.BytesIO(json.dumps({"order": close_order(payload["client_id"])}).encode())

    monkeypatch.setattr(monitor.urllib.request, "urlopen", post)
    return posts


def test_gap_latches_then_fills_from_position_quote(job, monkeypatch):
    path, clock = job
    frames = iter([snapshot(140), snapshot(131)])
    monkeypatch.setattr(monitor, "read", lambda base: next(frames))
    posts = install_post(monkeypatch, path)
    monitor.manage(path)
    saved = json.loads(path.read_text())
    assert len(posts) == 1
    assert posts[0]["price"] == 132.6
    assert saved["status"] == "CLOSED"
    assert saved["legs"][0]["triggered"]
    assert saved["legs"][0]["reason"] == "30% stop"


def test_exit_at_1510_closes_beyond_stop_limit(job, monkeypatch):
    path, clock = job
    clock.current = clock.current.replace(hour=15, minute=10)
    monkeypatch.setattr(monitor, "read", lambda base: snapshot(150))
    posts = install_post(monkeypatch, path)
    monitor.manage(path)
    assert len(posts) == 1 and posts[0]["price"] >= 150.3
    assert json.loads(path.read_text())["legs"][0]["reason"] == "15:10 exit"


def test_lost_response_recovers_confirmed_order_without_duplicate(job, monkeypatch):
    path, clock = job
    posts = []

    def read(base):
        state = snapshot(131)
        if posts:
            state["state"]["orders"] = [close_order(posts[0]["client_id"])]
        return state

    def post(request, timeout):
        posts.append(json.loads(request.data))
        raise TimeoutError("response lost after paper fill")

    monkeypatch.setattr(monitor, "read", read)
    monkeypatch.setattr(monitor.urllib.request, "urlopen", post)
    monitor.manage(path)
    assert len(posts) == 1
    saved = json.loads(path.read_text())
    assert saved["status"] == "CLOSED" and saved["error"] == ""


@pytest.mark.parametrize("mode,stale", [("live", False), ("dry", True)])
def test_live_mode_and_stale_quotes_never_submit(job, monkeypatch, mode, stale):
    path, clock = job
    monkeypatch.setattr(monitor, "read", lambda base: snapshot(131, mode, stale))
    posts = install_post(monkeypatch, path)
    with pytest.raises(KeyboardInterrupt):
        monitor.manage(path)
    assert posts == []
    assert not json.loads(path.read_text())["legs"][0].get("closed")
