import asyncio
from types import SimpleNamespace
import pytest

from broker import Broker, chain_data, report_rows, valid_response


def test_explicit_no_data_is_an_empty_report():
    response={"stCode":5203,"errMsg":"No Data","desc":"data not found","stat":"Not_Ok"}
    assert report_rows(response,"order book")==[]
    assert report_rows(response,"positions")==[]


@pytest.mark.parametrize("response",[
    {"stCode":403,"errMsg":"Session expired","stat":"Not_Ok"},
    {"stCode":5203,"errMsg":"No Data","error":[{"code":403}]},
    {"stat":"Not_Ok","emsg":"Invalid credentials"},
    {"stat":"Ok","unexpected":[]},
])
def test_invalid_reports_never_become_successful_reconciliation(response):
    with pytest.raises(ValueError):report_rows(response,"positions")


def test_empty_error_field_does_not_reject_success():
    response={"stat":"Ok","errMsg":"","data":[]}
    assert valid_response(response)==response
    assert report_rows(response,"positions")==[]


@pytest.mark.parametrize("wrapped",[True,False])
@pytest.mark.parametrize("short_fields",[True,False])
def test_option_chain_accepts_actual_and_documented_payloads(wrapped,short_fields):
    inst={"neoSymbol":"nse_fo|40659","symbol":"NIFTY26O0621800CE"}
    inst.update({"optType":"CE","strkPrc":"21800"} if short_fields else {"optionType":"CE","strikePrice":"21800"})
    row={"inst" if short_fields else "instrument":inst,"quote":{"ltp":"166.75","pc":"150","vol":"2000"},"oi":{"cur":"1200"}}
    data={"common_data":{"mktLot":"65"},"call":[row],"put":[]}
    response={"data":data} if wrapped else data
    assert chain_data(response)==data
    quotes=[]
    broker=Broker(lambda key,q:quotes.append((key,q)),lambda _:None,lambda *args:None)
    broker.market=SimpleNamespace(is_connected=True)
    broker.client=SimpleNamespace(
        option_chain=lambda **kwargs:response,
        search_scrip=lambda **kwargs:[{"pTrdSymbol":"NIFTY26O0621800CE","lLotSize":65,"dTickSize":5}],
    )
    contracts=asyncio.run(broker.chain("NIFTY","2026-10-06"))
    contract=contracts["nse_fo|40659"]
    assert contract["strike"]==21800 and contract["option_type"]=="CE"
    assert contract["lot_size"]==65 and contract["tick_size"]==.05
    assert quotes[0][1]["ltp"]==166.75
    assert quotes[0][1]["received"]==0 # REST snapshot must not qualify as a fresh streaming tick.


def test_empty_account_reconciliation_becomes_ready_without_fake_positions():
    broker=Broker(lambda *args:None,lambda _:None,lambda *args:None)
    empty={"stCode":5203,"errMsg":"No Data","stat":"Not_Ok"}
    broker.client=SimpleNamespace(order_report=lambda:empty,positions=lambda:empty)
    broker.authenticated=True
    asyncio.run(broker.reconcile())
    assert broker.management_ready and broker.positions==[]
