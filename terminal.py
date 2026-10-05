"""Broker account overview and account-scoped, durable watchlists."""
import asyncio
import json
import math
import time
import uuid

from broker import report_rows, valid_response

EXCHANGES = {'nse_cm', 'bse_cm', 'nse_fo', 'bse_fo', 'cde_fo', 'mcx_fo'}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def funds_data(response):
    result = valid_response(response)
    row = result.get('data', result)
    if isinstance(row, list) and len(row) == 1:
        row = row[0]
    if not isinstance(row, dict) or 'Net' not in row:
        raise ValueError('Broker funds format is unrecognized.')
    return {name: number(row.get(field)) for name, field in {
        'available': 'Net', 'margin_used': 'MarginUsed', 'collateral': 'CollateralValue',
        'pay_in': 'RmsPayInAmt', 'pay_out': 'RmsPayOutAmt',
        'realized': 'RealizedMtomPrsnt', 'unrealized': 'UnrealizedMtomPrsnt',
    }.items()}


def holding_data(row):
    quantity, average = number(row.get('quantity')), number(row.get('averagePrice'))
    cost = number(row.get('holdingCost'))
    if cost is None and quantity is not None and average is not None:
        cost = quantity * average
    value = number(row.get('mktValue'))
    return {'symbol': str(row.get('displaySymbol') or row.get('symbol') or ''),
            'exchange': str(row.get('exchangeSegment') or ''),
            'token': str(row.get('exchangeIdentifier') or ''),
            'qty': quantity, 'sellable': number(row.get('sellableQuantity')),
            'average': average, 'cost': cost, 'value': value,
            'pnl': value - cost if value is not None and cost is not None else None}


def position_data(row):
    qtys = [number(row.get(k, 0)) for k in ('flBuyQty', 'cfBuyQty', 'flSellQty', 'cfSellQty')]
    qty = None if any(q is None for q in qtys) else qtys[0] + qtys[1] - qtys[2] - qtys[3]
    pnl = number(row.get('mtmPnl'))
    # This SDK returns raw quantities. Only flat intraday rows have a
    # defensible realized result without additional cost/price calculations.
    if pnl is None and qty == 0 and not qtys[1] and not qtys[3]:
        buy, sell = number(row.get('buyAmt')), number(row.get('sellAmt'))
        if buy is not None and sell is not None:
            pnl = sell - buy
    return {'symbol': str(row.get('trdSym') or row.get('sym') or ''),
            'exchange': str(row.get('exSeg') or ''), 'product': str(row.get('prod') or ''),
            'qty': qty, 'ltp': number(row.get('ltp')), 'pnl': pnl,
            'status': 'Unknown' if qty is None else 'Closed' if qty == 0 else 'Long' if qty > 0 else 'Short'}


def sum_known(rows, field):
    values = [r[field] for r in rows]
    return None if any(v is None for v in values) else sum(values)


class BrokerTerminal:
    def __init__(self, ledger):
        self.db = ledger.db
        self.db.execute('CREATE TABLE IF NOT EXISTS watchlists (account TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY(account,id))')
        self.db.commit()
        self.cache = {}
        self.refresh_lock = asyncio.Lock()

    def watchlists(self, account):
        if not account:
            return []
        return [json.loads(row[0]) for row in self.db.execute(
            'SELECT payload FROM watchlists WHERE account=? ORDER BY rowid', (account,))]

    def save_watchlist(self, account, data):
        if not account:
            raise ValueError('Connect the broker before editing watchlists.')
        lists = self.watchlists(account)
        list_id = data.get('id') or str(uuid.uuid4())
        previous = next((w for w in lists if w['id'] == list_id), None)
        if data.get('id') and previous is None:
            raise ValueError('Watchlist does not exist in this account.')
        if previous is None and len(lists) >= 10:
            raise ValueError('You can save up to 10 watchlists.')
        name = str(data.get('name', previous['name'] if previous else '')).strip()
        if not 1 <= len(name) <= 40:
            raise ValueError('Enter a watchlist name of 1–40 characters.')
        items = data.get('items', previous['items'] if previous else [])
        if not isinstance(items, list) or len(items) > 50:
            raise ValueError('A watchlist can contain up to 50 instruments.')
        normalized = []
        for item in items:
            exchange, token, symbol = item.get('exchange'), str(item.get('token', '')), str(item.get('symbol', '')).strip()
            if exchange not in EXCHANGES or not token.isdigit() or not 1 <= len(symbol) <= 100:
                raise ValueError('Invalid watchlist instrument.')
            value = {'exchange': exchange, 'token': token, 'symbol': symbol, 'key': f'{exchange}|{token}'}
            if value['key'] not in {i['key'] for i in normalized}:
                normalized.append(value)
        payload = {'id': list_id, 'name': name, 'items': normalized}
        self.db.execute('INSERT OR REPLACE INTO watchlists VALUES (?,?,?)', (account, list_id, json.dumps(payload)))
        self.db.commit()
        return payload

    def delete_watchlist(self, account, list_id):
        self.db.execute('DELETE FROM watchlists WHERE account=? AND id=?', (account, list_id))
        self.db.commit()

    async def search(self, broker, exchange, query):
        if not broker.authenticated:
            raise ValueError('Connect the broker to search instruments.')
        query = str(query).strip()
        if exchange not in EXCHANGES or not 2 <= len(query) <= 60:
            raise ValueError('Select an exchange and enter at least two search characters.')
        response = await asyncio.to_thread(broker.client.search_scrip, exchange_segment=exchange,
                                         symbol=query.upper(), ignore_50multiple=False)
        rows = report_rows(response, 'instrument search')
        results = []
        for row in rows[:30]:
            token = str(row.get('pSymbol') or row.get('instrumentToken') or '')
            symbol = str(row.get('pTrdSymbol') or row.get('tradingSymbol') or '')
            if token.isdigit() and symbol:
                results.append({'exchange': exchange, 'token': token, 'symbol': symbol, 'key': f'{exchange}|{token}'})
        return results

    async def refresh(self, engine, force=False):
        broker, account = engine.broker, engine.account_key
        if not account or not broker.authenticated:
            return
        async with self.refresh_lock:
            cached = self.cache.setdefault(account, {'quotes': {}})
            if not force and time.time() - cached.get('attempted_at', 0) < 30:
                return
            cached['attempted_at'] = time.time()
            client = broker.client
            responses = await asyncio.gather(asyncio.to_thread(client.limits), asyncio.to_thread(client.holdings), return_exceptions=True)
            if engine.account_key != account or broker.client is not client or not broker.authenticated:
                return
            for section, result in zip(('funds', 'holdings'), responses):
                try:
                    if isinstance(result, Exception):
                        raise ValueError('Broker request failed.')
                    values = funds_data(result) if section == 'funds' else [holding_data(r) for r in report_rows(result, 'holdings')]
                    cached[section] = {'data': values, 'updated_at': time.time(), 'error': ''}
                except ValueError as exc:
                    cached.setdefault(section, {'data': None, 'updated_at': None})['error'] = str(exc)
            instruments = {i['key']: i for w in self.watchlists(account) for i in w['items']}
            cached['quotes'] = {k: v for k, v in cached['quotes'].items() if k in instruments}
            try:
                items = list(instruments.values())
                for start in range(0, len(items), 50):
                    batch = items[start:start + 50]
                    response = await asyncio.to_thread(client.quotes, instrument_tokens=[
                        {'exchange_segment': i['exchange'], 'instrument_token': i['token']} for i in batch])
                    if engine.account_key != account or broker.client is not client or not broker.authenticated:
                        return
                    rows = report_rows(response, 'quotes')
                    for row in rows:
                        inst = row.get('instrument', row.get('inst', {}))
                        key = row.get('neoSymbol') or inst.get('neoSymbol')
                        if not key:
                            exchange = row.get('exchange') or row.get('exchange_segment') or row.get('exchangeSegment')
                            token = row.get('exchange_token') or row.get('instrument_token') or row.get('instrumentToken')
                            key = f'{exchange}|{token}'
                        if key in instruments:
                            quote = row.get('quote', row)
                            price = number(quote.get('ltp', quote.get('last_traded_price')))
                            if price is not None:
                                cached['quotes'][key] = {'ltp': price, 'updated_at': time.time(),
                                    'change': number(quote.get('per_change', quote.get('net_change_percentage')))}
                cached['quote_error'] = ''
            except Exception:
                cached['quote_error'] = 'Watchlist quotes could not be refreshed. Last prices may be stale.'

    def view(self, engine):
        account, broker = engine.account_key, engine.broker
        connected = bool(account and broker.authenticated)
        cached = self.cache.get(account, {}) if connected else {}
        holdings = cached.get('holdings', {}).get('data')
        positions = [position_data(r) for r in broker.positions] if connected else []
        lists = self.watchlists(account) if connected else []
        for watchlist in lists:
            for item in watchlist['items']:
                item['quote'] = cached.get('quotes', {}).get(item['key'])
        return {'connected': connected, 'funds': cached.get('funds', {}), 'holdings': cached.get('holdings', {}),
                'positions': positions, 'positions_updated_at': broker.reconciled_at if connected else None,
                'positions_error': broker.reconciliation_error if connected else '',
                'portfolio': {'cost': None if holdings is None else sum_known(holdings, 'cost'),
                              'value': None if holdings is None else sum_known(holdings, 'value'),
                              'pnl': None if holdings is None else sum_known(holdings, 'pnl'),
                              'open': sum(p['qty'] is not None and p['qty'] != 0 for p in positions)},
                'watchlists': lists, 'quote_error': cached.get('quote_error', '')}
