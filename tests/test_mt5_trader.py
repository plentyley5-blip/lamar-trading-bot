import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from handlers.mt5_trader import _eligible_signal, _number, _single_price, RequestError


class MT5SignalValidationTests(unittest.TestCase):
    def test_single_exact_price_is_accepted(self):
        self.assertEqual(_single_price("1.09450"), 1.0945)

    def test_price_zone_is_rejected_not_guessed(self):
        self.assertIsNone(_single_price("1.0940 - 1.0945"))

    def test_prose_price_is_rejected(self):
        self.assertIsNone(_single_price("Entry near 1.09450"))

    def test_ready_market_matching_buy_is_eligible(self):
        result = {
            "signal": "BUY",
            "setup_status": "READY",
            "order_type": "MARKET",
            "chart_instrument_status": "MATCH",
            "instrument": "EURUSD",
            "confidence": 87,
            "entry": "1.09450",
            "stop_loss": "1.09250",
            "take_profit_1": "1.09850",
        }
        parsed = _eligible_signal(result, "EURUSD", 75)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["direction"], "BUY")

    def test_waiting_signal_is_not_eligible(self):
        result = {
            "signal": "BUY", "setup_status": "WAITING", "order_type": "WAIT",
            "chart_instrument_status": "MATCH", "instrument": "EURUSD",
            "confidence": 90, "entry": "1.09450", "stop_loss": "1.09250",
            "take_profit_1": "1.09850",
        }
        self.assertIsNone(_eligible_signal(result, "EURUSD", 75))

    def test_chart_mismatch_is_not_eligible(self):
        result = {
            "signal": "BUY", "setup_status": "READY", "order_type": "MARKET",
            "chart_instrument_status": "MISMATCH", "instrument": "EURUSD",
            "confidence": 90, "entry": "1.09450", "stop_loss": "1.09250",
            "take_profit_1": "1.09850",
        }
        self.assertIsNone(_eligible_signal(result, "EURUSD", 75))

    def test_wrong_instrument_is_not_eligible(self):
        result = {
            "signal": "BUY", "setup_status": "READY", "order_type": "MARKET",
            "chart_instrument_status": "MATCH", "instrument": "GBPUSD",
            "confidence": 90, "entry": "1.09450", "stop_loss": "1.09250",
            "take_profit_1": "1.09850",
        }
        self.assertIsNone(_eligible_signal(result, "EURUSD", 75))

    def test_invalid_price_order_is_not_eligible(self):
        result = {
            "signal": "BUY", "setup_status": "READY", "order_type": "MARKET",
            "chart_instrument_status": "MATCH", "instrument": "EURUSD",
            "confidence": 90, "entry": "1.09450", "stop_loss": "1.09550",
            "take_profit_1": "1.09850",
        }
        self.assertIsNone(_eligible_signal(result, "EURUSD", 75))

    def test_integer_settings_reject_fractional_values(self):
        with self.assertRaises(RequestError):
            _number(1.5, "max_open_positions", 1, 3, integer=True)


if __name__ == "__main__":
    unittest.main()
