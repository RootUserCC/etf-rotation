#coding:utf-8
import json
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

CANDIDATES = ['880003.SH', '880003.SZ', '800005.SH', '800005.SZ']
ETFS = ['159552.SZ', '512890.SH', '159201.SZ']
PROBE_DONE = False


def init(ContextInfo):
    ContextInfo.set_universe(ETFS)
    ContextInfo.run_time('probe', '1nSecond', '2020-01-01 09:00:00', 'SH')


def probe(ContextInfo):
    global PROBE_DONE
    if PROBE_DONE:
        return
    PROBE_DONE = True
    print('NATIVE_PROBE_TIME=%s' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    for code in CANDIDATES + ETFS:
        try:
            detail = ContextInfo.get_instrument_detail(code)
        except Exception as exc:
            print('DETAIL %s ERROR %r' % (code, exc))
            continue
        name = detail.get('InstrumentName') if detail else None
        print('DETAIL %s EXISTS=%s NAME=%s TICK=%s' % (
            code, bool(detail), name, detail.get('PriceTick') if detail else None))
        try:
            data = ContextInfo.get_market_data_ex(
                ['close'], [code], period='1d', start_time='', end_time='', count=3,
                dividend_type='none', fill_data=False, subscribe=True)
            frame = (data or {}).get(code)
            if frame is None or frame.empty:
                print('DAILY %s EMPTY' % code)
            else:
                print('DAILY %s INDEX=%s CLOSE=%s' % (
                    code, [str(x) for x in frame.index], frame['close'].tolist()))
        except Exception as exc:
            print('DAILY %s ERROR %r' % (code, exc))
    try:
        params = urlencode({
            'ut': '7eea3edcaed734bea9cbfc24409ed989',
            'secid': '47.800005',
            'fields1': 'f1,f2,f3,f4,f5,f6',
            'fields2': 'f51,f52,f53,f54,f55,f56,f57',
            'klt': '101', 'fqt': '0', 'lmt': '500', 'end': '20500101',
        })
        url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get?' + params
        request = Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept': 'application/json,text/plain,*/*',
            'Referer': 'https://quote.eastmoney.com/',
        })
        response = build_opener(ProxyHandler({})).open(request, timeout=10)
        payload = json.loads(response.read().decode('utf-8'))
        response.close()
        data = payload.get('data') or {}
        klines = data.get('klines') or []
        print('EM_AVG NAME=%s CODE=%s COUNT=%s' % (
            data.get('name'), data.get('code'), len(klines)))
        print('EM_AVG FIRST=%s LAST=%s' % (
            klines[0] if klines else None, klines[-1] if klines else None))
    except Exception as exc:
        print('EM_AVG ERROR %r' % exc)
    try:
        ticks = ContextInfo.get_full_tick(ETFS)
        for code in ETFS:
            tick = (ticks or {}).get(code) or {}
            print('TICK %s LAST=%s BID=%s ASK=%s TIMETAG=%s' % (
                code, tick.get('lastPrice'), tick.get('bidPrice'),
                tick.get('askPrice'), tick.get('timetag')))
    except Exception as exc:
        print('TICK ERROR %r' % exc)


def handlebar(ContextInfo):
    pass
