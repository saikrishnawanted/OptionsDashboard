import asyncio
from types import SimpleNamespace

import pytest

from core import Ledger
from terminal import BrokerTerminal, funds_data, holding_data, position_data


@pytest.fixture
def terminal(tmp_path):
    return BrokerTerminal(Ledger(tmp_path / 'terminal.sqlite'))


def item(token='1333'):
    return {'exchange': 'nse_cm', 'token': token, 'symbol': 'RELIANCE'}


def broker_engine():
    client = SimpleNamespace(
        limits=lambda: {'stat': 'Ok', 'Net': '10000.25', 'MarginUsed': '2000', 'CollateralValue': '500'},
        holdings=lambda: {'data': [{'displaySymbol': 'RELIANCE', 'exchangeSegment': 'nse_cm',
                                   'exchangeIdentifier': '1333', 'instrumentToken': '999', 'quantity': 2,
                                   'averagePrice': 100, 'holdingCost': 200, 'mktValue': 230, 'sellableQuantity': 2}]},
        quotes=lambda **kw: {'data': [{'instrument': {'neoSymbol': 'nse_cm|1333'}, 'quote': {'ltp': '115'}}]},
        search_scrip=lambda **kw: [{'pSymbol': '1333', 'pTrdSymbol': 'RELIANCE-EQ'}],
    )
    return SimpleNamespace(account_key='a', broker=SimpleNamespace(client=client, authenticated=True,
        positions=[{'trdSym': 'NIFTYCE', 'flBuyQty': '65', 'flSellQty': '65', 'buyAmt': '5000', 'sellAmt': '5400'}],
        reconciled_at=100, reconciliation_error=''))


def test_account_watchlists_persist_and_are_isolated(terminal):
    saved = terminal.save_watchlist('a', {'name': 'Stocks', 'items': [item(), item()]})
    assert len(saved['items']) == 1
    assert terminal.watchlists('b') == []
    restored = BrokerTerminal(SimpleNamespace(db=terminal.db))
    assert restored.watchlists('a')[0]['name'] == 'Stocks'
    with pytest.raises(ValueError, match='account'):
        terminal.save_watchlist('b', {'id': saved['id'], 'name': 'Wrong account'})
    terminal.delete_watchlist('b', saved['id'])
    assert len(terminal.watchlists('a')) == 1
    terminal.delete_watchlist('a', saved['id'])
    assert terminal.watchlists('a') == []


@pytest.mark.parametrize('data', [{'name': ''}, {'name': 'x'*41}, {'name': 'Stocks', 'items': [item('bad')]},
                                {'name': 'Stocks', 'items': [item(str(i)) for i in range(51)]}])
def test_watchlist_validation(terminal, data):
    with pytest.raises(ValueError):
        terminal.save_watchlist('a', data)


def test_refresh_funds_holdings_quotes_and_disconnect(terminal):
    e = broker_engine()
    terminal.save_watchlist('a', {'name': 'Stocks', 'items': [item()]})
    asyncio.run(terminal.refresh(e))
    view = terminal.view(e)
    assert view['funds']['data']['available'] == 10000.25
    assert view['portfolio'] == {'cost': 200, 'value': 230, 'pnl': 30, 'open': 0}
    assert view['positions'][0]['pnl'] == 400
    assert view['holdings']['data'][0]['token'] == '1333'  # Exchange identifier, not holding-record token.
    assert view['watchlists'][0]['items'][0]['quote']['ltp'] == 115
    assert 'account_key' not in str(view) and 'actId' not in str(view)
    e.broker.authenticated = False
    view = terminal.view(e)
    assert view['funds'] == {} and view['holdings'] == {} and view['watchlists'] == []
    e.broker.authenticated = True
    e.account_key = 'b'
    assert terminal.view(e)['funds'] == {}


def test_failure_retains_last_good_data_and_reports_error(terminal):
    e = broker_engine()
    asyncio.run(terminal.refresh(e))
    e.broker.client.limits = lambda: {'stat': 'Not_Ok', 'errMsg': 'Session expired'}
    e.broker.client.holdings = lambda: {'stCode': 5203, 'errMsg': 'No Data', 'stat': 'Not_Ok'}
    asyncio.run(terminal.refresh(e, force=True))
    view = terminal.view(e)
    assert view['funds']['error']
    assert view['funds']['data']['available'] == 10000.25
    assert view['holdings']['data'] == [] and view['portfolio']['value'] == 0


def test_missing_values_remain_unavailable():
    assert holding_data({'quantity': 2})['pnl'] is None
    assert position_data({'flSellQty': '65', 'sellAmt': '5000'})['pnl'] is None
    assert funds_data({'Net': 'nan'})['available'] is None
    with pytest.raises(ValueError):
        funds_data({'stat': 'Ok'})


def test_search_requires_session_and_returns_safe_metadata(terminal):
    e = broker_engine()
    assert asyncio.run(terminal.search(e.broker, 'nse_cm', 'RELIANCE')) == [
        {'exchange': 'nse_cm', 'token': '1333', 'symbol': 'RELIANCE-EQ', 'key': 'nse_cm|1333'}]
    e.broker.authenticated = False
    with pytest.raises(ValueError, match='Connect'):
        asyncio.run(terminal.search(e.broker, 'nse_cm', 'RELIANCE'))


def test_refresh_throttles_repeated_requests(terminal):
    e = broker_engine()
    calls = []
    e.broker.client.limits = lambda: calls.append(1) or {'Net': '10'}
    asyncio.run(terminal.refresh(e))
    asyncio.run(terminal.refresh(e))
    assert calls == [1]
    asyncio.run(terminal.refresh(e, force=True))
    assert calls == [1, 1]


def test_quotes_accept_production_flat_response(terminal):
    e = broker_engine()
    terminal.save_watchlist('a', {'name': 'Stocks', 'items': [item()]})
    e.broker.client.quotes = lambda **kw: [{'exchange': 'nse_cm', 'exchange_token': '1333', 'ltp': '115.50', 'per_change': '1.2'}]
    asyncio.run(terminal.refresh(e))
    quote = terminal.view(e)['watchlists'][0]['items'][0]['quote']
    assert quote['ltp'] == 115.5 and quote['change'] == 1.2


def test_account_endpoints_enforce_authentication_and_persist(terminal, monkeypatch):
    import app
    from fastapi.testclient import TestClient
    e = broker_engine()
    monkeypatch.setattr(app, 'engine', e)
    monkeypatch.setattr(app, 'broker_terminal', terminal)
    monkeypatch.setattr(app, 'broker', e.broker)
    client = TestClient(app.app)
    # Obtain the process token without invoking snapshot on the minimal engine.
    headers = {'origin': 'http://testserver', 'x-terminal-token': app.session_token}
    response = client.post('/api/watchlist-save', json={'name': 'Stocks', 'items': [item()]}, headers=headers)
    assert response.status_code == 200
    assert response.json()['terminal']['watchlists'][0]['name'] == 'Stocks'
    assert client.post('/api/watchlist-save', json={'name': 'Denied'}).status_code == 403
    view = client.get('/api/broker-terminal').json()
    assert view['funds']['data']['available'] == 10000.25
    e.broker.authenticated = False
    assert client.post('/api/watchlist-save', json={'name': 'Denied'}, headers=headers).status_code == 400
    assert client.get('/api/broker-terminal').json()['watchlists'] == []
