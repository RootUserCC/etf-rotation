#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QMT 原生策略的离线安全测试；不连接账户，也不会提交委托。"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

import qmt_native_etf_rotation as qmt
from backtest import calc_signals


class SignalTests(unittest.TestCase):
    def test_signal_formula_matches_backtest(self):
        close = pd.Series(20 + np.sin(np.arange(160) / 7.0) + np.arange(160) * 0.003)
        actual = qmt.calc_signals(close)
        expected = calc_signals(close, sell_anywhere=True, fast=17, slow=34, sig_n=9)
        pd.testing.assert_series_equal(actual['dif'], expected['dif'], check_names=False)
        pd.testing.assert_series_equal(actual['dea'], expected['dea'], check_names=False)
        pd.testing.assert_series_equal(actual['buy_sig'], expected['buy_sig'], check_names=False)
        pd.testing.assert_series_equal(actual['sell_sig'], expected['sell_sig'], check_names=False)

    def test_parse_eastmoney_average_daily(self):
        payload = {'data': {'klines': [
            '2026-11-04,23.00,23.50,23.80,22.90,1,2',
            '2026-11-05,23.21,24.01,24.29,23.21,1,2',
        ]}}
        frame = qmt.parse_em_avg(payload)
        self.assertEqual(frame['_day'].tolist(), ['20261104', '20261105'])
        self.assertEqual(frame['close'].tolist(), [23.50, 24.01])

    def test_empty_average_daily_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '日线为空'):
            qmt.parse_em_avg({'data': {'klines': []}})

    def test_stale_average_cannot_create_signal(self):
        avg = pd.DataFrame({'close': np.linspace(20, 21, 50)},
                           index=pd.date_range('2026-08-01', periods=50))
        result = qmt.build_close_signal(avg, {}, '20261105')
        self.assertFalse(result['valid'])
        self.assertIn('未更新', result['reason'])

    def test_eastmoney_failure_uses_local_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'avg.csv')
            pd.DataFrame({
                'date': ['2026-11-04', '2026-11-05'], 'close': [23.5, 24.01],
            }).to_csv(path, index=False)
            with patch.object(qmt, 'AVG_CODE', ''), \
                    patch.object(qmt, 'AVG_HISTORY_FILE', path), \
                    patch.object(qmt, 'eastmoney_avg_daily', side_effect=OSError('offline')):
                frame, source = qmt.average_daily(None)
        self.assertEqual(frame['_day'].iloc[-1], '20261105')
        self.assertEqual(source, 'local_csv:after_eastmoney_error')

    def test_defense_selection_uses_20_day_momentum(self):
        index = pd.date_range('2026-10-01', periods=25)
        low_vol = pd.DataFrame({'close': np.linspace(1.0, 1.1, 25)}, index=index)
        cash_flow = pd.DataFrame({'close': np.linspace(1.0, 1.2, 25)}, index=index)
        day = index[-1].strftime('%Y%m%d')
        target, momentum = qmt.choose_defense(
            {qmt.LOW_VOL_CODE: low_vol, qmt.CASH_FLOW_CODE: cash_flow}, day)
        self.assertEqual(target, qmt.CASH_FLOW_CODE)
        self.assertGreater(momentum[qmt.CASH_FLOW_CODE], momentum[qmt.LOW_VOL_CODE])

    def test_new_cash_flow_history_falls_back_to_low_vol(self):
        index = pd.date_range('2026-10-01', periods=25)
        low_vol = pd.DataFrame({'close': np.linspace(1.0, 1.1, 25)}, index=index)
        cash_flow = pd.DataFrame({'close': [1.0, 1.1]}, index=index[-2:])
        target, _ = qmt.choose_defense(
            {qmt.LOW_VOL_CODE: low_vol, qmt.CASH_FLOW_CODE: cash_flow},
            index[-1].strftime('%Y%m%d'))
        self.assertEqual(target, qmt.LOW_VOL_CODE)


class ExecutionGuardTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_file_patch = patch.object(
            qmt, 'STATE_FILE', os.path.join(self.tempdir.name, 'state.json'))
        self.state_file_patch.start()
        qmt._state = qmt.empty_state()

    def tearDown(self):
        self.state_file_patch.stop()
        self.tempdir.cleanup()
        qmt._state = None

    def test_no_switch_inside_defense_pool(self):
        self.assertTrue(qmt.switch_not_needed({qmt.LOW_VOL_CODE: object()}, qmt.CASH_FLOW_CODE))
        self.assertTrue(qmt.switch_not_needed({qmt.CASH_FLOW_CODE: object()}, qmt.LOW_VOL_CODE))
        self.assertTrue(qmt.switch_not_needed({qmt.ATTACK_CODE: object()}, qmt.ATTACK_CODE))
        self.assertFalse(qmt.switch_not_needed({qmt.ATTACK_CODE: object()}, qmt.LOW_VOL_CODE))
        self.assertFalse(qmt.switch_not_needed({qmt.LOW_VOL_CODE: object()}, qmt.ATTACK_CODE))

    def test_dry_run_never_calls_passorder_and_rounds_buy_lot(self):
        qmt._state['pending'] = {'signal_id': 'C_TEST', 'phase': 'start'}
        order_api = Mock()
        with patch.object(qmt, 'ENABLE_TRADE', False), \
                patch.object(qmt, 'TEST_ENV_CONFIRMED', False), \
                patch.object(qmt, 'limit_price', return_value=1.234), \
                patch.object(qmt, 'passorder', order_api, create=True), \
                patch.object(qmt, 'log') as log:
            submitted = qmt.submit_order(object(), qmt.ATTACK_CODE, 'BUY', 1299)
        self.assertFalse(submitted)
        order_api.assert_not_called()
        self.assertIsNone(qmt._state['order'])
        self.assertEqual(qmt._state['reported'], ['C_TEST_BUY'])
        self.assertIn('1200股', log.call_args[0][0])

    def test_real_flag_alone_is_not_enough(self):
        qmt._state['pending'] = {'signal_id': 'C_TEST', 'phase': 'start'}
        order_api = Mock()
        with patch.object(qmt, 'ENABLE_TRADE', True), \
                patch.object(qmt, 'TEST_ENV_CONFIRMED', False), \
                patch.object(qmt, 'limit_price', return_value=1.234), \
                patch.object(qmt, 'passorder', order_api, create=True):
            qmt.submit_order(object(), qmt.ATTACK_CODE, 'BUY', 1000)
        order_api.assert_not_called()

    def test_partial_terminal_order_stops_signal(self):
        qmt._state['pending'] = {'signal_id': 'C_TEST', 'phase': 'sell'}
        qmt._state['order'] = {
            'signal_id': 'C_TEST', 'code': qmt.ATTACK_CODE, 'side': 'SELL',
            'volume': 1000, 'remark': 'C_TEST_SELL', 'submitted_at': '2026-11-05 09:31:00',
        }
        item = SimpleNamespace(m_nOrderStatus=53, m_nVolumeTraded=500, m_nVolumeTotal=500)
        with patch.object(qmt, 'matching_order', return_value=item):
            qmt.manage_order(object())
        self.assertIsNone(qmt._state['pending'])
        self.assertIsNone(qmt._state['order'])
        self.assertIn('未全部成交', qmt._state['last_error'])

    def test_complete_sell_unlocks_buy_phase(self):
        qmt._state['pending'] = {
            'signal_id': 'C_TEST', 'phase': 'sell', 'target_code': qmt.LOW_VOL_CODE,
        }
        qmt._state['order'] = {
            'signal_id': 'C_TEST', 'code': qmt.ATTACK_CODE, 'side': 'SELL',
            'volume': 1000, 'remark': 'C_TEST_SELL', 'submitted_at': '2026-11-05 09:31:00',
        }
        item = SimpleNamespace(m_nOrderStatus=56, m_nVolumeTraded=1000, m_nVolumeTotal=0)
        with patch.object(qmt, 'matching_order', return_value=item):
            qmt.manage_order(object())
        self.assertIsNone(qmt._state['order'])
        self.assertEqual(qmt._state['pending']['phase'], 'buy')

    def test_corrupt_state_is_fail_closed(self):
        with open(qmt.STATE_FILE, 'w', encoding='utf-8') as handle:
            handle.write('{broken json')
        state = qmt.load_state()
        self.assertIn('禁止委托', state['state_error'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
