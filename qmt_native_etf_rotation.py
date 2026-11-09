#coding:utf-8
import json
import math
import os
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

import numpy as np
import pandas as pd

ACCOUNT_ID = ''
ACCOUNT_TYPE = 'STOCK'
STRATEGY_CAPITAL = 100000.0
ENABLE_TRADE = False
TEST_ENV_CONFIRMED = False
# QMT 不提供通达信 880003 / 东财 800005；留空时使用东财平均股价日线。
# 若券商定制版 QMT 提供等价指数，可填实际代码以优先使用 QMT 数据。
AVG_CODE = ''
AVG_HISTORY_FILE = r'C:\Users\ccf\etf_rotation\data\avg_880003.csv'
EM_AVG_SECID = '47.800005'
EM_UT = '7eea3edcaed734bea9cbfc24409ed989'

ATTACK_CODE = '159552.SZ'
LOW_VOL_CODE = '512890.SH'
CASH_FLOW_CODE = '159201.SZ'
ETF_CODES = [ATTACK_CODE, LOW_VOL_CODE, CASH_FLOW_CODE]
STRATEGY_NAME = 'ETF_ROTATION_C_NATIVE'
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'qmt_native_etf_rotation_state.json')
ORDER_TIMEOUT_SECONDS = 120
MAX_SLIPPAGE = 0.003
CASH_BUFFER = 0.001
FINAL_ORDER_STATUS = [0, 52, 53, 54, 56, 57]
_state = None


def log(message):
    print('[%s] %s' % (STRATEGY_NAME, message))


def day_of(value):
    if hasattr(value, 'strftime'):
        try:
            return value.strftime('%Y%m%d')
        except Exception:
            pass
    if isinstance(value, (int, float, np.integer, np.floating)):
        numeric = float(value)
        if numeric > 100000000000:
            return pd.Timestamp(numeric, unit='ms').strftime('%Y%m%d')
        if numeric > 1000000000:
            return pd.Timestamp(numeric, unit='s').strftime('%Y%m%d')
    text = str(value)
    digits = ''.join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8 and digits[:4].isdigit() and 2000 <= int(digits[:4]) <= 2099:
        return digits[:8]
    stamp = pd.Timestamp(value)
    return stamp.strftime('%Y%m%d')


def calc_signals(close):
    series = pd.Series(close, dtype='float64')
    fast = series.ewm(alpha=2.0 / 18.0, adjust=False).mean()
    slow = series.ewm(alpha=2.0 / 35.0, adjust=False).mean()
    dif = fast - slow
    dea = dif.ewm(alpha=2.0 / 10.0, adjust=False).mean()
    previous1 = dif.shift(1)
    previous2 = dif.shift(2)
    return pd.DataFrame({
        'dif': dif,
        'dea': dea,
        'buy_sig': (previous1 < previous2) & (dif > previous1) & (dif < 0),
        'sell_sig': (previous1 > previous2) & (dif < previous1),
    }, index=series.index)


def normalize_daily(frame):
    if frame is None or frame.empty or 'close' not in frame:
        return pd.DataFrame(columns=['close', '_day'])
    out = frame[['close']].copy()
    out['close'] = pd.to_numeric(out['close'], errors='coerce')
    out = out.dropna(subset=['close'])
    out['_day'] = [day_of(value) for value in out.index]
    out = out.drop_duplicates('_day', keep='last').sort_values('_day')
    return out


def build_close_signal(avg, defense_frames, signal_day):
    avg = normalize_daily(avg)
    result = {'signal_date': signal_day, 'valid': False, 'action': 'hold'}
    if avg.empty or avg['_day'].iloc[-1] != signal_day:
        result['reason'] = '平均股价日线缺失或未更新到信号日'
        return result
    signals = calc_signals(avg['close'].values)
    latest = signals.iloc[-1]
    result.update({'dif': float(latest['dif']), 'dea': float(latest['dea'])})
    if bool(latest['buy_sig']):
        action = 'switch_to_atk'
        target = ATTACK_CODE
    elif bool(latest['sell_sig']):
        action = 'switch_to_defense'
        try:
            target, momentum = choose_defense(defense_frames, signal_day)
        except ValueError as exc:
            result['reason'] = str(exc)
            return result
        result['defense_momentum'] = momentum
    else:
        result['valid'] = True
        result['reason'] = '无买卖拐点'
        return result
    result.update({
        'valid': True,
        'action': action,
        'target_code': target,
        'signal_id': 'C_%s_%s' % (signal_day, action),
        'reason': '',
    })
    return result


def choose_defense(defense_frames, signal_day):
    momentum = {}
    for code in [LOW_VOL_CODE, CASH_FLOW_CODE]:
        frame = normalize_daily(defense_frames.get(code))
        frame = frame[frame['_day'] <= signal_day]
        if code == CASH_FLOW_CODE and (frame.empty or len(frame) < 21):
            continue
        if frame.empty or frame['_day'].iloc[-1] != signal_day:
            raise ValueError('%s 日线未更新到信号日' % code)
        if len(frame) < 21:
            raise ValueError('%s 不足20日动量数据' % code)
        momentum[code] = float(frame['close'].iloc[-1] / frame['close'].iloc[-21] - 1)
    if LOW_VOL_CODE not in momentum:
        raise ValueError('红利低波ETF无有效20日动量')
    target = LOW_VOL_CODE
    if CASH_FLOW_CODE in momentum and momentum[CASH_FLOW_CODE] > momentum[LOW_VOL_CODE]:
        target = CASH_FLOW_CODE
    return target, momentum


def empty_state():
    return {'pending': None, 'order': None, 'executed': [], 'reported': [],
            'last_signal': None, 'last_error': '', 'last_block': '', 'state_error': ''}


def load_state():
    if not os.path.exists(STATE_FILE):
        return empty_state()
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as handle:
            loaded = json.load(handle)
        state = empty_state()
        state.update(loaded)
        return state
    except Exception as exc:
        state = empty_state()
        state['state_error'] = '状态文件读取失败，禁止委托: %r' % exc
        return state


def save_state():
    if _state is None:
        return
    temporary = STATE_FILE + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(_state, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, STATE_FILE)


def native_daily(ContextInfo, code, dividend_type):
    data = ContextInfo.get_market_data_ex(
        ['close'], [code], period='1d', start_time='', end_time='', count=-1,
        dividend_type=dividend_type, fill_data=False, subscribe=True)
    return normalize_daily((data or {}).get(code))


def parse_em_avg(payload):
    """解析东财 800005 日线；与本地监控使用的平均股价口径一致。"""
    data = payload.get('data') or {}
    rows = []
    for item in data.get('klines') or []:
        fields = item.split(',')
        if len(fields) >= 3:
            rows.append((fields[0], float(fields[2])))
    if not rows:
        raise ValueError('东财平均股价日线为空')
    frame = pd.DataFrame(rows, columns=['date', 'close']).set_index('date')
    return normalize_daily(frame)


def eastmoney_avg_daily():
    params = urlencode({
        'ut': EM_UT, 'secid': EM_AVG_SECID,
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
    # 与项目 requests 通道一致，绕过系统代理；500 根日线足够 EMA 预热。
    response = build_opener(ProxyHandler({})).open(request, timeout=15)
    try:
        payload = json.loads(response.read().decode('utf-8'))
    finally:
        response.close()
    return parse_em_avg(payload)


def average_daily(ContextInfo):
    if AVG_CODE:
        return native_daily(ContextInfo, AVG_CODE, 'none'), 'qmt:' + AVG_CODE
    try:
        return eastmoney_avg_daily(), 'eastmoney:800005'
    except Exception as exc:
        log('东财平均股价读取失败，尝试本地缓存: %r' % exc)
        if not os.path.exists(AVG_HISTORY_FILE):
            raise
        frame = pd.read_csv(AVG_HISTORY_FILE).set_index('date')
        return normalize_daily(frame), 'local_csv:after_eastmoney_error'


def generate_close_signal(ContextInfo):
    now = datetime.now()
    if now.strftime('%H%M') < '1500':
        return
    signal_day = now.strftime('%Y%m%d')
    last_signal = _state.get('last_signal') or {}
    if last_signal.get('signal_date') == signal_day and last_signal.get('valid'):
        return
    try:
        avg, avg_source = average_daily(ContextInfo)
        defenses = dict((code, native_daily(ContextInfo, code, 'back'))
                        for code in [LOW_VOL_CODE, CASH_FLOW_CODE])
        signal = build_close_signal(avg, defenses, signal_day)
        signal['avg_source'] = avg_source
    except Exception as exc:
        log('收盘信号计算失败: %r' % exc)
        return
    if not signal['valid']:
        log('收盘信号无效: %s' % signal.get('reason', ''))
        return
    if _state.get('order') is not None:
        log('上一委托尚未完结，不覆盖当前指令')
        return
    _state['last_signal'] = signal
    _state['last_error'] = ''
    _state['last_block'] = ''
    if signal['action'] == 'hold':
        _state['pending'] = None
        log('%s 无换仓信号 DIF=%.6f DEA=%.6f' % (signal_day, signal['dif'], signal['dea']))
    else:
        _state['pending'] = dict((key, signal[key]) for key in
                                 ['signal_id', 'signal_date', 'action', 'target_code'])
        _state['pending']['phase'] = 'start'
        log('收盘指令 %s target=%s DIF=%.6f DEA=%.6f' % (
            signal['signal_id'], signal['target_code'], signal['dif'], signal['dea']))
    save_state()
    print_account(ContextInfo)


def code_of(item):
    instrument = getattr(item, 'm_strInstrumentID', '')
    exchange = getattr(item, 'm_strExchangeID', '')
    if not instrument:
        return ''
    return instrument + '.' + exchange if exchange else instrument


def positions(ContextInfo):
    result = {}
    items = get_trade_detail_data(ACCOUNT_ID, ACCOUNT_TYPE, 'POSITION') or []
    for item in items:
        code = code_of(item)
        if code in ETF_CODES and int(getattr(item, 'm_nVolume', 0)) > 0:
            result[code] = item
    return result


def print_account(ContextInfo):
    if not ACCOUNT_ID:
        log('未配置 ACCOUNT_ID，跳过账户查询')
        return
    try:
        accounts = get_trade_detail_data(ACCOUNT_ID, ACCOUNT_TYPE, 'ACCOUNT') or []
        for account in accounts:
            log('账户状态=%s 可用资金=%.2f' % (
                getattr(account, 'm_strStatus', ''), getattr(account, 'm_dAvailable', 0.0)))
        held = positions(ContextInfo)
        for code in ETF_CODES:
            if code in held:
                item = held[code]
                log('持仓 %s 总量=%d 可用=%d' % (
                    code, int(item.m_nVolume), int(getattr(item, 'm_nCanUseVolume', 0))))
    except Exception as exc:
        log('只读账户查询失败: %r' % exc)


def order_status(item):
    return int(getattr(item, 'm_nOrderStatus', 255))


def is_active_order(item):
    return order_status(item) not in FINAL_ORDER_STATUS


def all_orders(ContextInfo, own_only=False):
    if own_only:
        return get_trade_detail_data(
            ACCOUNT_ID, ACCOUNT_TYPE, 'ORDER', STRATEGY_NAME) or []
    return get_trade_detail_data(ACCOUNT_ID, ACCOUNT_TYPE, 'ORDER') or []


def matching_order(ContextInfo):
    current = _state.get('order')
    if not current:
        return None
    matches = []
    for item in all_orders(ContextInfo, own_only=True):
        if code_of(item) != current['code']:
            continue
        if getattr(item, 'm_strRemark', '') != current['remark']:
            continue
        if int(getattr(item, 'm_nDirection', -1)) != current['direction']:
            continue
        matches.append(item)
    return matches[-1] if matches else None


def has_active_conflict(ContextInfo, code):
    for item in all_orders(ContextInfo):
        if code_of(item) == code and is_active_order(item):
            return True
    return False


def next_trading_day_ok(ContextInfo, signal_day, today):
    dates = ContextInfo.get_trading_dates(ATTACK_CODE, signal_day, today, -1, '1d') or []
    later = sorted(set(day_of(value) for value in dates if day_of(value) > signal_day))
    return bool(later) and later[0] == today


def limit_price(ContextInfo, code, side):
    tick = (ContextInfo.get_full_tick([code]) or {}).get(code) or {}
    last = float(tick.get('lastPrice') or 0)
    bids = tick.get('bidPrice') or []
    asks = tick.get('askPrice') or []
    bid = float(bids[0]) if len(bids) else 0.0
    ask = float(asks[0]) if len(asks) else 0.0
    if not math.isfinite(last) or last <= 0:
        raise ValueError('%s 实时行情无效' % code)
    tag = tick.get('timetag')
    if tag is not None and day_of(tag) != datetime.now().strftime('%Y%m%d'):
        raise ValueError('%s 实时行情已过期' % code)
    if side == 'BUY':
        price = min(ask if ask > 0 else last, last * (1 + MAX_SLIPPAGE))
    else:
        price = max(bid if bid > 0 else last, last * (1 - MAX_SLIPPAGE))
    if not math.isfinite(price) or price <= 0:
        raise ValueError('%s 盘口价格无效' % code)
    return round(price, 3)


def blocked(message):
    if _state.get('last_block') != message:
        log('暂停执行: ' + message)
        _state['last_block'] = message
        save_state()


def finish_signal():
    signal_id = _state['pending']['signal_id']
    if signal_id not in _state['executed']:
        _state['executed'].append(signal_id)
        _state['executed'] = _state['executed'][-50:]
    _state['pending'] = None
    _state['order'] = None
    _state['last_error'] = ''
    _state['last_block'] = ''
    log('指令完成 ' + signal_id)
    save_state()


def fail_signal(message):
    signal_id = (_state.get('pending') or {}).get('signal_id', '')
    _state['pending'] = None
    _state['order'] = None
    _state['last_error'] = message
    _state['last_block'] = ''
    log('指令终止 %s %s' % (signal_id, message))
    save_state()


def submit_order(ContextInfo, code, side, volume):
    pending = _state['pending']
    signal_id = pending['signal_id']
    if side == 'BUY':
        volume = int(volume // 100 * 100)
    if volume <= 0:
        blocked('%s 委托数量为零' % code)
        return False
    direction = 48 if side == 'BUY' else 49
    remark = signal_id + '_' + side
    if not ENABLE_TRADE or not TEST_ENV_CONFIRMED:
        if remark not in _state['reported']:
            try:
                price = limit_price(ContextInfo, code, side)
                log('拟委托 %s %s %d股 限价=%.3f；真实委托开关关闭' % (
                    side, code, volume, price))
            except Exception as exc:
                log('拟委托 %s %s %d股；无法取得限价: %s' % (side, code, volume, exc))
            _state['reported'].append(remark)
            _state['reported'] = _state['reported'][-50:]
            save_state()
        return False
    if not ACCOUNT_ID or _state.get('state_error'):
        blocked('账户未配置或状态文件异常')
        return False
    if has_active_conflict(ContextInfo, code):
        blocked('%s 已有活动委托，可能包含手工挂单' % code)
        return False
    try:
        price = limit_price(ContextInfo, code, side)
        operation = 23 if side == 'BUY' else 24
        passorder(operation, 1101, ACCOUNT_ID, code, 11, price, int(volume),
                  STRATEGY_NAME, 1, remark, ContextInfo)
        order_id = get_last_order_id(ACCOUNT_ID, ACCOUNT_TYPE, 'ORDER', STRATEGY_NAME)
    except Exception as exc:
        blocked('委托调用失败: %r' % exc)
        return False
    _state['order'] = {
        'signal_id': signal_id, 'code': code, 'side': side, 'direction': direction,
        'volume': int(volume), 'price': price, 'remark': remark,
        'order_id': str(order_id), 'submitted_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'cancel_requested': False,
    }
    pending['phase'] = 'buy' if side == 'BUY' else 'sell'
    _state['last_block'] = ''
    save_state()
    log('已提交 %s %s %d股 限价=%.3f order_id=%s' % (
        side, code, volume, price, order_id))
    return True


def available_cash():
    accounts = get_trade_detail_data(ACCOUNT_ID, ACCOUNT_TYPE, 'ACCOUNT') or []
    if not accounts:
        raise ValueError('未查询到账户资金')
    return float(getattr(accounts[0], 'm_dAvailable', 0.0))


def switch_not_needed(held, target):
    """已在目标仓，或已处于任一防守仓时，不因新的卖出信号在防守池内换仓。"""
    if set(held) == {target}:
        return True
    defenses = {LOW_VOL_CODE, CASH_FLOW_CODE}
    return len(held) == 1 and next(iter(held)) in defenses and target in defenses


def execute_pending(ContextInfo):
    pending = _state.get('pending')
    if not pending or _state.get('order'):
        return
    now = datetime.now()
    clock = now.strftime('%H%M')
    if clock < '0931' or (clock > '1000' and pending.get('phase') != 'buy') or clock > '1450':
        return
    today = now.strftime('%Y%m%d')
    try:
        if not next_trading_day_ok(ContextInfo, pending['signal_date'], today):
            fail_signal('不是信号后的首个交易日，禁止补做旧指令')
            return
        if pending['signal_id'] in _state['executed']:
            finish_signal()
            return
        held = positions(ContextInfo)
    except Exception as exc:
        blocked('交易日或持仓查询失败: %r' % exc)
        return
    target = pending['target_code']
    if switch_not_needed(held, target):
        finish_signal()
        return
    if len(held) > 1:
        blocked('策略白名单内同时持有多个标的: ' + ','.join(sorted(held)))
        return
    source = next(iter(held), None)
    try:
        if source and source != target:
            if pending.get('phase') == 'buy':
                blocked('%s 卖单已成交但持仓尚未清零，禁止重复卖出或提前买入' % source)
                return
            item = held[source]
            volume = int(getattr(item, 'm_nCanUseVolume', 0))
            submit_order(ContextInfo, source, 'SELL', volume)
            return
        if pending.get('phase') != 'buy':
            pending['phase'] = 'buy'
            save_state()
        price = limit_price(ContextInfo, target, 'BUY')
        cash = min(STRATEGY_CAPITAL, available_cash()) * (1 - CASH_BUFFER)
        submit_order(ContextInfo, target, 'BUY', int(cash / price))
    except Exception as exc:
        blocked('委托准备失败: %r' % exc)


def manage_order(ContextInfo):
    current = _state.get('order')
    if not current:
        return
    try:
        item = matching_order(ContextInfo)
    except Exception as exc:
        blocked('委托查询失败: %r' % exc)
        return
    if item is None:
        blocked('已提交委托但尚未查询到回报，禁止重复下单')
        return
    status = order_status(item)
    traded = int(getattr(item, 'm_nVolumeTraded', 0))
    remaining = int(getattr(item, 'm_nVolumeTotal', current['volume'] - traded))
    complete = status == 56 or (status in FINAL_ORDER_STATUS and remaining == 0 and
                                traded >= current['volume'])
    if status not in FINAL_ORDER_STATUS:
        submitted = datetime.strptime(current['submitted_at'], '%Y-%m-%d %H:%M:%S')
        elapsed = (datetime.now() - submitted).total_seconds()
        if elapsed > ORDER_TIMEOUT_SECONDS and not current.get('cancel_requested'):
            if not ENABLE_TRADE or not TEST_ENV_CONFIRMED:
                return
            order_id = current.get('order_id')
            if not order_id or order_id == '-1':
                order_id = getattr(item, 'm_strOrderSysID', '')
            if not order_id or order_id == '-1':
                blocked('活动委托缺少订单号，未自动撤单')
                return
            try:
                cancel(order_id, ACCOUNT_ID, ACCOUNT_TYPE, ContextInfo)
                current['cancel_requested'] = True
                save_state()
                log('委托超时，已请求撤单 ' + current['remark'])
            except Exception as exc:
                blocked('撤单调用失败: %r' % exc)
        return
    if not complete:
        fail_signal('委托未全部成交 status=%d 已成交=%d 剩余=%d；后续委托已停止' % (
            status, traded, remaining))
        return
    if current['side'] == 'SELL':
        _state['order'] = None
        _state['pending']['phase'] = 'buy'
        _state['last_block'] = ''
        save_state()
        log('卖出已全部成交，等待持仓回报后买入 ' + _state['pending']['target_code'])
    else:
        finish_signal()


def monitor(ContextInfo):
    if _state.get('order'):
        manage_order(ContextInfo)
    else:
        execute_pending(ContextInfo)


def order_callback(ContextInfo, orderInfo):
    log('委托回报 %s status=%d traded=%d remaining=%d remark=%s' % (
        code_of(orderInfo), order_status(orderInfo),
        int(getattr(orderInfo, 'm_nVolumeTraded', 0)),
        int(getattr(orderInfo, 'm_nVolumeTotal', 0)),
        getattr(orderInfo, 'm_strRemark', '')))


def deal_callback(ContextInfo, dealInfo):
    log('成交回报 %s volume=%d price=%.3f remark=%s' % (
        code_of(dealInfo), int(getattr(dealInfo, 'm_nVolume', 0)),
        float(getattr(dealInfo, 'm_dPrice', 0.0)), getattr(dealInfo, 'm_strRemark', '')))


def account_callback(ContextInfo, accountInfo):
    log('账户回报 status=%s' % getattr(accountInfo, 'm_strStatus', ''))


def init(ContextInfo):
    global _state
    _state = load_state()
    ContextInfo.set_universe(ETF_CODES + ([AVG_CODE] if AVG_CODE else []))
    if ACCOUNT_ID:
        ContextInfo.set_account(ACCOUNT_ID, ACCOUNT_TYPE)
    for code in ETF_CODES + ([AVG_CODE] if AVG_CODE else []):
        down_history_data(code, '1d', '', '')
    ContextInfo.run_time('generate_close_signal', '1nDay', '2020-01-01 15:02:00', 'SH')
    ContextInfo.run_time('generate_close_signal', '1nDay', '2020-01-01 15:05:00', 'SH')
    ContextInfo.run_time('generate_close_signal', '1nDay', '2020-01-01 15:08:00', 'SH')
    ContextInfo.run_time('execute_pending', '1nDay', '2020-01-01 09:31:00', 'SH')
    ContextInfo.run_time('monitor', '5nSecond', '2020-01-01 09:31:00', 'SH')
    log('原生策略已初始化；ENABLE_TRADE=%s TEST_ENV_CONFIRMED=%s' % (
        ENABLE_TRADE, TEST_ENV_CONFIRMED))
    if _state.get('state_error'):
        log(_state['state_error'])


def handlebar(ContextInfo):
    pass
