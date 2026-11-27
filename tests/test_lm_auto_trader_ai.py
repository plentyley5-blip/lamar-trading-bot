import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from handlers import lm_auto_trader_ai


def sample_market(symbol="XAUUSD", news_blocked=False):
    candles_m1 = [
        {"time": 1795800000 + i * 60, "open": 2650.0 + i * 0.1,
         "high": 2650.2 + i * 0.1, "low": 2649.9 + i * 0.1,
         "close": 2650.1 + i * 0.1, "tick_volume": 100}
        for i in range(12)
    ]
    candles_m5 = [
        {"time": 1795800000 + i * 300, "open": 2649.0 + i * 0.2,
         "high": 2649.3 + i * 0.2, "low": 2648.8 + i * 0.2,
         "close": 2649.2 + i * 0.2, "tick_volume": 400}
        for i in range(8)
    ]
    return {
        "symbol": symbol,
        "bid": 2651.0,
        "ask": 2651.2,
        "point": 0.01,
        "digits": 2,
        "news_blocked": news_blocked,
        "m1": candles_m1,
        "m5": candles_m5,
    }


class IndependentAutoTraderAITests(unittest.TestCase):
    @patch("handlers.lm_auto_trader_ai._call_gemini")
    def test_buy_with_two_confirmations_is_retained(self, call_ai):
        call_ai.return_value = [{
            "symbol": "XAUUSD", "decision": "BUY", "confidence": 86,
            "entry_price": 2651.2, "stop_loss": 2649.0, "take_profit": 2654.0,
            "rationale": "Bullish short-term structure.",
            "confirmations": ["M1 bullish displacement", "M5 higher-low structure"],
            "invalidation": "Break below 2649.0",
        }]
        result = lm_auto_trader_ai.analyse([sample_market()])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["decision"], "BUY")
        self.assertEqual(result[0]["symbol"], "XAUUSD")
        self.assertEqual(len(result[0]["confirmations"]), 2)

    @patch("handlers.lm_auto_trader_ai._call_gemini")
    def test_model_decision_without_two_confirmations_becomes_wait(self, call_ai):
        call_ai.return_value = [{
            "symbol": "XAUUSD", "decision": "SELL", "confidence": 90,
            "entry_price": 2651.0, "stop_loss": 2653.0, "take_profit": 2648.0,
            "rationale": "Weak evidence.", "confirmations": ["M1 rejection"],
            "invalidation": "Above 2653.0",
        }]
        result = lm_auto_trader_ai.analyse([sample_market()])
        self.assertEqual(result[0]["decision"], "WAIT")

    @patch("handlers.lm_auto_trader_ai._call_gemini")
    def test_high_impact_news_window_forces_wait(self, call_ai):
        call_ai.return_value = [{
            "symbol": "XAUUSD", "decision": "BUY", "confidence": 92,
            "entry_price": 2651.2, "stop_loss": 2649.0, "take_profit": 2654.0,
            "rationale": "Potential setup.", "confirmations": ["M1 breakout", "M5 support"],
            "invalidation": "Below support",
        }]
        result = lm_auto_trader_ai.analyse([sample_market(news_blocked=True)])
        self.assertEqual(result[0]["decision"], "WAIT")
        self.assertIn("news", result[0]["rationale"].lower())

    @patch("handlers.lm_auto_trader_ai._call_gemini")
    def test_unreturned_symbol_is_explicitly_wait(self, call_ai):
        call_ai.return_value = [{
            "symbol": "XAUUSD", "decision": "BUY", "confidence": 86,
            "entry_price": 2651.2, "stop_loss": 2649.0, "take_profit": 2654.0,
            "rationale": "Setup valid.", "confirmations": ["M1 displacement", "M5 higher low"],
            "invalidation": "Below support",
        }]
        result = lm_auto_trader_ai.analyse([sample_market("XAUUSD"), sample_market("EURUSD")])
        by_symbol = {item["symbol"]: item for item in result}
        self.assertEqual(by_symbol["XAUUSD"]["decision"], "BUY")
        self.assertEqual(by_symbol["EURUSD"]["decision"], "WAIT")

    @patch("handlers.lm_auto_trader_ai._call_gemini")
    def test_model_cannot_inject_an_unrequested_symbol(self, call_ai):
        call_ai.return_value = [{
            "symbol": "GBPUSD", "decision": "BUY", "confidence": 99,
            "entry_price": 1.2, "stop_loss": 1.19, "take_profit": 1.22,
            "rationale": "Unrequested.", "confirmations": ["A", "B"],
            "invalidation": "Below",
        }]
        result = lm_auto_trader_ai.analyse([sample_market()])
        self.assertEqual(result[0]["symbol"], "XAUUSD")
        self.assertEqual(result[0]["decision"], "WAIT")

    def test_insufficient_broker_candle_history_is_rejected_before_ai(self):
        market = sample_market()
        market["m1"] = market["m1"][:5]
        with patch("handlers.lm_auto_trader_ai._call_gemini") as call_ai:
            with self.assertRaises(ValueError):
                lm_auto_trader_ai.analyse([market])
            call_ai.assert_not_called()


if __name__ == "__main__":
    unittest.main()
