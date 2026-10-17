"""Apply the LM Analyzer trade-plan additions to api/analysis_submit.py.
Run from the repository root: python scripts/upgrade_analysis_submit.py
The script refuses to overwrite if expected source fragments are missing.
"""
from pathlib import Path

path = Path("api/analysis_submit.py")
if not path.exists():
    raise SystemExit("Run this script from the LM Analyzer repository root; api/analysis_submit.py was not found.")
s = path.read_text(encoding="utf-8")

replacements = [
    (
'''- Explain the evidence, entry readiness, and invalidation.
- Return only valid JSON without Markdown.

Return exactly this structure:''',
'''- Explain the evidence, entry readiness, and invalidation.
- setup_status must be ENTRY_AVAILABLE, SETUP_DEVELOPING, ENTRY_MISSED, or NO_CLEAR_SETUP.
- Use ENTRY_AVAILABLE only with BUY or SELL and a visible trigger plus defensible risk levels.
- For NO TRADE, use SETUP_DEVELOPING when a specific confirmation is still missing, ENTRY_MISSED when price is too extended, otherwise NO_CLEAR_SETUP.
- entry_trigger must describe the visible trigger or state what confirmation is still needed; never invent a trigger.
- invalidation_reason must explain what would invalidate the setup, or why a defensible level cannot be identified.
- risk_notes must mention practical risks such as opposing levels, chasing, timeframe conflict, unclear labels, or missing confirmation when actually relevant.
- lesson_topics must contain 1-3 exact titles from: Candlesticks and Price Action; Support and Resistance; Trend, Swing Highs and Swing Lows; BOS and CHoCH; Liquidity Pools and Sweeps; Order Blocks and Fair Value Gaps; Entries, Invalidation and Confirmation; Risk Per Trade and Position Sizing; Risk-to-Reward and Trade Selection; Combining 4H Context with 15M Execution; Fibonacci Retracements; Premium and Discount Zones; News, Fundamentals and Volatility; Backtesting, Expectancy and Strategy Review.
- Return only valid JSON without Markdown.

Return exactly this structure:'''),
    (
'''  "news_fundamental_risk": "",
  "warnings": []
}}
""".strip()''',
'''  "news_fundamental_risk": "",
  "warnings": [],
  "setup_status": "ENTRY_AVAILABLE | SETUP_DEVELOPING | ENTRY_MISSED | NO_CLEAR_SETUP",
  "entry_trigger": "",
  "invalidation_reason": "",
  "risk_notes": "",
  "lesson_topics": []
}}
""".strip()'''),
    (
'''        "news_fundamental_risk": {"type": "string"},
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
        },
    },''',
'''        "news_fundamental_risk": {"type": "string"},
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
        },
        "setup_status": {
            "type": "string",
            "enum": ["ENTRY_AVAILABLE", "SETUP_DEVELOPING", "ENTRY_MISSED", "NO_CLEAR_SETUP"],
        },
        "entry_trigger": {"type": "string"},
        "invalidation_reason": {"type": "string"},
        "risk_notes": {"type": "string"},
        "lesson_topics": {"type": "array", "items": {"type": "string"}},
    },'''),
    (
'''        "news_fundamental_risk",
        "warnings",
    ],
}''',
'''        "news_fundamental_risk",
        "warnings",
        "setup_status",
        "entry_trigger",
        "invalidation_reason",
        "risk_notes",
        "lesson_topics",
    ],
}'''),
    (
'''    output = {
        "signal": signal,''',
'''    setup_status = str(result.get("setup_status", "")).strip().upper()
    valid_setup_statuses = {
        "ENTRY_AVAILABLE", "SETUP_DEVELOPING", "ENTRY_MISSED", "NO_CLEAR_SETUP"
    }
    if setup_status not in valid_setup_statuses:
        setup_status = "ENTRY_AVAILABLE" if signal in {"BUY", "SELL"} else "NO_CLEAR_SETUP"
    if signal == "NO TRADE" and setup_status == "ENTRY_AVAILABLE":
        setup_status = "NO_CLEAR_SETUP"
    if signal in {"BUY", "SELL"}:
        setup_status = "ENTRY_AVAILABLE"

    output = {
        "signal": signal,'''),
    (
'''        "warnings": list_value("warnings"),
    }

    if signal == "NO TRADE":''',
'''        "warnings": list_value("warnings"),
        "setup_status": setup_status,
        "entry_trigger": text("entry_trigger"),
        "invalidation_reason": text("invalidation_reason"),
        "risk_notes": text("risk_notes"),
        "lesson_topics": list_value("lesson_topics")[:3],
    }

    if signal == "NO TRADE":'''),
]

for old, new in replacements:
    count = s.count(old)
    if count != 1:
        raise SystemExit("Refusing to edit: expected exactly one occurrence of a source block, found {} for: {}".format(count, old[:90].replace("\n", " ")))
    s = s.replace(old, new, 1)

path.write_text(s, encoding="utf-8")
print("Updated api/analysis_submit.py with entry-readiness, invalidation, risk notes, and Learning Hub topic fields.")
