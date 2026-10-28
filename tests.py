# -*- coding: utf-8 -*-
"""单元测试：python3 tests.py （零外部依赖，全部使用合成数据，不联网）"""
import math
import os
import random
import tempfile
import unittest

import factors
import risk
import store
import llm_analyst
from config import RiskConfig, LLMConfig


def make_rows(n=40, base=100.0, drift=0.001, seed=7, noise=0.01):
    """合成升序K线：缓慢上涨 + 小噪声，用于验证因子符号与数值。"""
    random.seed(seed)
    rows, price = [], base
    for i in range(n):
        o = price
        c = price * (1 + drift + random.uniform(-noise, noise))
        h = max(o, c) * 1.005
        low = min(o, c) * 0.995
        rows.append({"date": f"2026-01-{i+1:02d}" if i < 28 else f"2026-02-{i-27:02d}",
                     "open": o, "close": c, "high": h, "low": low,
                     "volume": 1000 + 50 * i, "amount": c * 1000})
        price = c
    return rows


class TestFactorBasics(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(factors.sma([1, 2, 3], 3), 2.0)
        self.assertIsNone(factors.sma([1, 2], 3))

    def test_pct_rank(self):
        self.assertEqual(factors.pct_rank(5, [1, 2, 3, 4]), 1.0)
        self.assertEqual(factors.pct_rank(0, [1, 2, 3, 4]), 0.0)
        self.assertEqual(factors.pct_rank(None, [1]), None)
        self.assertEqual(factors.pct_rank(5, []), 0.5)

    def test_rsi14_trend(self):
        up = [100 + i for i in range(20)]       # 单边上涨
        self.assertEqual(factors.rsi14(up, 19), 100.0)
        down = [100 - i for i in range(20)]     # 单边下跌
        self.assertEqual(factors.rsi14(down, 19), 0.0)

    def test_series_shapes(self):
        # 严格递增的确定性序列（无噪声），便于断言符号
        rows = [{"date": f"2026-{1 if i < 28 else 2}-{(i % 28) + 1:02d}",
                 "open": 100 + i - 0.5, "close": 100 + i,
                 "high": 100 + i + 0.5, "low": 100 + i - 0.5,
                 "volume": 1000.0, "amount": (100 + i) * 1000.0}
                for i in range(40)]
        out = factors.compute_factor_series(rows)
        self.assertEqual(len(out), 40)
        self.assertIsNone(out[10]["vol20"])          # 前21根不足
        self.assertIsNotNone(out[-1]["vol20"])
        self.assertGreater(out[-1]["mom5"], 0)       # 上涨序列动量为正
        self.assertEqual(out[-1]["rsi14"], 100.0)    # 单边上涨 RSI=100
        self.assertAlmostEqual(out[-1]["close_pos"], 0.975)  # 收盘=high-0.5，区间宽20 -> 19.5/20

    def test_min_bars(self):
        self.assertEqual(factors.min_bars_needed(), 21)


class TestFusion(unittest.TestCase):
    def setUp(self):
        self.rows = make_rows(40)
        self.series = [f for f in factors.compute_factor_series(self.rows)
                       if all(f.get(c) is not None for c in factors.FACTOR_COLS)]
        self.hist = self.series[-29:]

    def test_snapshot_uses_realtime_price(self):
        rt = {"price": self.rows[-1]["close"] * 1.10, "prev_close": self.rows[-1]["close"],
              "high_today": self.rows[-1]["close"] * 1.12, "low_today": self.rows[-1]["close"] * 1.01,
              "volume_today": 1500, "ts": "2026-02-10 14:00"}
        snap = factors.snapshot_from_realtime(self.rows, rt)
        # mom5 = 今日实时价 / 5日前收盘 - 1（closes[-5] 即 rows[-5]）
        self.assertAlmostEqual(snap["mom5"],
                               rt["price"] / self.rows[-5]["close"] - 1, places=8)
        self.assertIn("vol_ratio", snap)

    def test_fuse_high_low(self):
        base = {"mom5": 0.05, "mom10": 0.08, "vol20": 0.02, "vol_ratio": 1.0,
                "amplitude": 0.02, "rsi14": 55, "close_pos": 0.5, "ma_bias": 0.01}
        hist = [dict(base) for _ in range(29)]
        top = {**base, "mom5": 0.10, "mom10": 0.15, "vol20": 0.01, "close_pos": 0.9}
        fused_top = factors.fuse_score(top, hist)
        self.assertGreater(fused_top["score"], 0.6)
        bot = {**base, "mom5": 0.0, "mom10": 0.0, "vol20": 0.05, "close_pos": 0.1}
        fused_bot = factors.fuse_score(bot, hist)
        self.assertLess(fused_bot["score"], 0.4)

    def test_fuse_missing_renormalizes(self):
        snap = dict(self.hist[-1]); snap["vol_ratio"] = None
        fused = factors.fuse_score(snap, self.hist[:-1])
        self.assertIn("vol_ratio", fused["missing"])
        self.assertTrue(0 <= fused["score"] <= 1)


class TestLLMFallback(unittest.TestCase):
    def test_rule_bullish(self):
        snap = {c: 0.9 for c in factors.FACTOR_COLS}   # 全因子处于高分位
        snap["vol20"] = 0.01                            # 低波动
        fused = factors.fuse_score(snap, [{"mom5": 0.1, "mom10": 0.1, "vol20": 0.1,
                                            "vol_ratio": 1.0, "amplitude": 0.02,
                                            "rsi14": 50, "close_pos": 0.5, "ma_bias": 0.01}] * 5)
        out = llm_analyst._rule_analyst("测试", {"price": 100, "change_pct": 1}, snap, fused)
        self.assertEqual(out["signal"], "buy")
        self.assertEqual(out["engine"], "rule")

    def test_extract_json_with_fence(self):
        text = '好的，结果如下：```json\n{"signal": "hold", "confidence": 0.5}\n```'
        self.assertEqual(llm_analyst._extract_json(text)["signal"], "hold")

    def test_invalid_llm_output_falls_back(self):
        class FakeCfg:  # base_url 指向不存在的端口 -> 触发降级
            llm = LLMConfig(base_url="http://127.0.0.1:9", api_key="x", model="m", timeout=1)
        snap = {c: 0.5 for c in factors.FACTOR_COLS}
        fused = factors.fuse_score(snap, [])
        out = llm_analyst.analyze("测试", "000001",
                                  {"price": 10, "change_pct": 0, "ts": ""}, snap, fused, FakeCfg())
        self.assertEqual(out["engine"], "rule")
        self.assertIn("llm_error", out)


class TestRisk(unittest.TestCase):
    def setUp(self):
        self.cfg = type("C", (), {"risk": RiskConfig()})()

    def test_amplitude_breaker(self):
        llm = {"signal": "buy", "confidence": 0.9, "rationale": ""}
        d = risk.gate(0.9, llm, high20=110, price=100, amplitude=0.09, cfg=self.cfg)
        self.assertEqual(d.action, "AVOID")

    def test_drawdown_breaker(self):
        llm = {"signal": "buy", "confidence": 0.9, "rationale": ""}
        d = risk.gate(0.9, llm, high20=120, price=100, amplitude=0.02, cfg=self.cfg)
        self.assertEqual(d.action, "AVOID")

    def test_low_confidence_downgrade(self):
        llm = {"signal": "buy", "confidence": 0.3, "rationale": ""}
        d = risk.gate(0.9, llm, high20=101, price=100, amplitude=0.02, cfg=self.cfg)
        self.assertEqual(d.action, "WAIT")

    def test_weight_capped(self):
        llm = {"signal": "buy", "confidence": 0.9, "rationale": ""}
        d = risk.gate(0.95, llm, high20=101, price=100, amplitude=0.02, cfg=self.cfg)
        self.assertEqual(d.action, "BUY")
        self.assertLessEqual(d.weight, self.cfg.risk.max_position_pct)


class TestStore(unittest.TestCase):
    def test_upsert_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            conn = store.connect(os.path.join(td, "t.db"))
            rows = make_rows(30)
            store.upsert_klines(conn, "X", rows)
            store.upsert_klines(conn, "X", rows)      # 重复写
            self.assertEqual(len(store.get_klines(conn, "X")), 30)
            frs = [f for f in factors.compute_factor_series(rows)
                   if all(f.get(c) is not None for c in factors.FACTOR_COLS)]
            store.upsert_factors(conn, "X", frs)
            store.upsert_factors(conn, "X", frs)
            self.assertEqual(len(store.get_factor_history(conn, "X", 29)), len(frs))
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
