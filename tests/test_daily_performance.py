from datetime import datetime, timedelta

from core import IST
from pnl import PnlHistory
from test_terminal import engine
from test_pnl import fill


def store(engine, order, identifier):
    order['id'] = identifier
    order['qty'] = order['filled_qty']
    order['status'] = 'FILLED'
    engine.ledger.save(order)


def test_old_realized_excluded_without_deleting_positions(engine):
    store(engine, fill(day='2026-10-01'), 'old-entry')
    store(engine, fill('B', price=80, day='2026-10-01'), 'old-exit')
    assert engine.ledger.positions(engine.quotes, 'demo')[0]['realized'] == 400
    assert engine.ledger.positions(engine.quotes, 'demo', realized_day='2026-10-05')[0]['realized'] == 0
    assert len(engine.ledger.orders()) == 2


def test_daily_reset_archive_and_legacy_closed_day(engine):
    store(engine, fill(day='2026-10-01'), 'old-entry')
    store(engine, fill('B', price=80, day='2026-10-01'), 'old-exit')
    history = PnlHistory(engine.ledger)
    today = datetime(2026, 10, 5, 10, tzinfo=IST)
    assert history.view(engine, today, 'SENSEX')['current']['OVERALL']['total'] == 0
    rows = history.performance(engine, today)['rows']
    assert len(rows) == 1 and rows[0]['date'] == '2026-10-01'
    assert rows[0]['values']['OVERALL']['realized'] == 400 and rows[0]['status'] == 'Closed'
    restored = PnlHistory(engine.ledger)
    assert restored.performance(engine, today + timedelta(days=40))['rows'][0]['values']['OVERALL']['total'] == 400


def test_historical_open_day_not_falsely_zero(engine):
    store(engine, fill(day='2026-10-01'), 'old-entry')
    row = PnlHistory(engine.ledger).performance(engine, datetime(2026, 10, 5, 10, tzinfo=IST))['rows'][0]
    assert row['values']['OVERALL']['total'] is None
    assert row['status'] == 'Last observed / open'


def test_today_and_other_account_are_separate(engine):
    store(engine, fill(day='2026-10-05', account='mine'), 'today')
    store(engine, fill(day='2026-10-05', account='other'), 'other')
    engine.account_key = 'mine'
    rows = PnlHistory(engine.ledger).performance(engine, datetime(2026, 10, 5, 10, tzinfo=IST))['rows']
    assert len(rows) == 1 and rows[0]['status'] == 'Today'
    assert rows[0]['values']['OVERALL']['open'] == 1


def test_snapshot_and_performance_api_rollover(engine, monkeypatch, tmp_path):
    # Importing the app creates its ledger: never point tests at the running session.
    monkeypatch.setenv('TERMINAL_DATA_DIR', str(tmp_path / 'http'))
    import app
    from fastapi.testclient import TestClient
    store(engine, fill(day='2026-10-01'), 'old-entry')
    store(engine, fill('B', price=80, day='2026-10-01'), 'old-exit')
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 5, 10, tzinfo=IST)
    monkeypatch.setattr(app, 'datetime', Clock)
    monkeypatch.setattr(app, 'engine', engine)
    monkeypatch.setattr(app, 'ledger', engine.ledger)
    monkeypatch.setattr(app, 'pnl_history', PnlHistory(engine.ledger))
    with TestClient(app.app) as client:
        state = client.get('/api/bootstrap').json()['state']
        assert state['session_pnl']['realized'] == 0
        assert state['orders'] == [] and state['positions'] == []
        rows = client.get('/api/performance').json()['rows']
        assert rows[0]['values']['OVERALL']['total'] == 400
