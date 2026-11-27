import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from handlers.mt5_trader import (
    RequestError,
    _dashboard_for_user,
    _record_account_snapshot,
    _snapshot_positions,
)


class MT5DashboardTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.device = {
            "id": "device-1",
            "user_id": "user-1",
            "account_login": 123456,
            "broker_server": "Exness-MT5Trial10",
            "account_mode": "demo",
        }

    def test_snapshot_keeps_only_lm_auto_trader_positions(self):
        raw = [
            {
                "ticket": 100, "position_id": 200, "symbol": "XAUUSDm",
                "direction": "BUY", "volume": 0.01, "price_open": 2300.0,
                "stop_loss": 2295.0, "take_profit": 2310.0, "profit": 2.5,
                "swap": 0, "magic_number": 26112701, "opened_at_epoch": 1790000000,
            },
            {
                "ticket": 101, "position_id": 201, "symbol": "EURUSD",
                "direction": "SELL", "volume": 0.1, "price_open": 1.1,
                "magic_number": 12345,
            },
        ]
        result = _snapshot_positions(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ticket"], 100)
        self.assertEqual(result[0]["symbol"], "XAUUSDm")

    def test_snapshot_rejects_more_than_50_positions(self):
        with self.assertRaises(RequestError):
            _snapshot_positions([{}] * 51)

    def test_snapshot_rejects_invalid_direction_for_owned_trade(self):
        raw = [{
            "ticket": 100, "position_id": 200, "symbol": "XAUUSDm",
            "direction": "UNKNOWN", "volume": 0.01, "price_open": 2300.0,
            "magic_number": 26112701,
        }]
        with self.assertRaises(RequestError):
            _snapshot_positions(raw)

    @patch("handlers.mt5_trader._db_request")
    def test_account_snapshot_is_upserted_for_linked_device(self, db_request):
        body = {
            "account_login": 123456,
            "broker_server": "Exness-MT5Trial10",
            "balance": 100.0,
            "equity": 102.5,
            "floating_profit": 2.5,
            "credit": 0,
            "margin": 5,
            "free_margin": 97.5,
            "margin_level": 2050,
            "currency": "USD",
            "leverage": 1000,
            "terminal_connected": True,
            "terminal_build": 5000,
            "positions": [],
        }
        result = _record_account_snapshot(self.device, body)
        self.assertEqual(result, {
            "saved": True, "terminal_connected": True, "open_positions": 0,
        })
        args = db_request.call_args
        self.assertEqual(args.args[0], "POST")
        self.assertIn("on_conflict=device_id", args.args[1])
        self.assertEqual(args.args[2]["device_id"], "device-1")
        self.assertEqual(args.args[2]["equity"], 102.5)

    @patch("handlers.mt5_trader._db_request")
    def test_dashboard_returns_latest_snapshot_and_real_positions(self, db_request):
        db_request.side_effect = [
            [{
                "id": "device-1", "account_login": 123456,
                "broker_server": "Exness-MT5Trial10", "account_mode": "demo",
                "auto_trade_enabled": False, "last_seen_at": "2026-11-27T10:00:00Z",
                "created_at": "2026-11-27T09:00:00Z",
            }],
            [{
                "balance": 100.0, "equity": 102.5, "floating_profit": 2.5,
                "currency": "USD", "terminal_connected": True,
                "positions": [{"ticket": 100, "symbol": "XAUUSDm"}],
                "updated_at": "2026-11-27T10:00:00Z",
            }],
            [],
            [],
        ]
        result = _dashboard_for_user({"id": "user-1"})
        self.assertTrue(result["connected"])
        self.assertEqual(result["telemetry_status"], "stale")
        self.assertEqual(result["account"]["equity"], 102.5)
        self.assertEqual(result["open_positions"][0]["ticket"], 100)

    @patch("handlers.mt5_trader._db_request")
    def test_dashboard_without_device_is_not_connected(self, db_request):
        db_request.return_value = []
        result = _dashboard_for_user({"id": "user-1"})
        self.assertFalse(result["connected"])
        self.assertEqual(result["telemetry_status"], "not_connected")
        self.assertEqual(db_request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
