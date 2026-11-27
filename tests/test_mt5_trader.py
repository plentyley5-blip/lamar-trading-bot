import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from handlers.mt5_trader import (
    RequestError,
    _eligible_signal,
    _number,
    _record_deal,
    _single_price,
)


class MT5SignalValidationTests(unittest.TestCase):
    def test_single_exact_price_is_accepted(self):
        self.assertEqual(_single_price("1.09450"), 1.0945)

    def test_price_zone_is_rejected_not_guessed(self):
        self.assertIsNone(_single_price("1.0940 - 1.0945"))

    def test_prose_price_is_rejected(self):
        self.assertIsNone(_single_price("Entry near 1.09450"))

    def test_ready_market_matching_buy_is_eligible(self):
        result = {
            "signal": "BUY", "setup_status": "READY", "order_type": "MARKET",
            "chart_instrument_status": "MATCH", "instrument": "EURUSD",
            "confidence": 87, "entry": "1.09450", "stop_loss": "1.09250",
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

    @patch("handlers.mt5_trader._db_request")
    def test_deal_without_executed_signal_position_is_rejected(self, db_request):
        db_request.return_value = []
        device = {
            "id": "device-1", "user_id": "user-1",
            "account_login": 123456, "broker_server": "Demo-Server",
        }
        body = {
            "magic_number": 26112701, "deal_ticket": 100, "position_id": 200,
            "deal_time_epoch": 1790000000, "symbol": "EURUSD", "volume": 0.01,
            "price": 1.1, "profit": 10, "commission": -0.5, "swap": 0,
        }
        with self.assertRaises(RequestError):
            _record_deal(device, body)
        self.assertEqual(db_request.call_count, 2)

    @patch("handlers.mt5_trader._db_request")
    def test_deal_is_saved_after_executed_signal_position_match(self, db_request):
        db_request.side_effect = [
            [{"id": "execution-1", "signal_id": "signal-1", "symbol": "EURUSD", "status": "executed"}],
            None,
        ]
        device = {
            "id": "device-1", "user_id": "user-1",
            "account_login": 123456, "broker_server": "Demo-Server",
        }
        body = {
            "magic_number": 26112701, "deal_ticket": 100, "position_id": 200,
            "deal_time_epoch": 1790000000, "symbol": "EURUSDm", "volume": 0.01,
            "price": 1.1, "profit": 10, "commission": -0.5, "swap": 0,
        }
        result = _record_deal(device, body)
        self.assertEqual(result, {"saved": True, "duplicate": False})
        payload = db_request.call_args_list[1].args[2]
        self.assertEqual(payload["position_id"], 200)
        self.assertEqual(payload["user_id"], "user-1")


if __name__ == "__main__":
    unittest.main()
