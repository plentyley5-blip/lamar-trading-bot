"""Authenticated LM Analyzer Learning Hub, journal, and feedback API."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    verify_access_token,
)

MAX_BODY = 24_000

LESSONS = [
    {"id":"basics-01","level":"BEGINNER","category":"Foundations","title":"How the Forex Market Works","minutes":7,"summary":"Understand currency pairs, bid/ask, spreads, sessions, and why prices move.","content":"A currency pair compares a base currency with a quote currency. EURUSD at 1.0800 means one euro is priced at 1.0800 US dollars. Bid is the price at which the market buys from you; ask is the price at which it sells to you. The spread is the difference. Liquidity and volatility change across sessions and around economic releases. Learn your broker's contract specifications before trading.","question":"In EURUSD, which currency is the base currency?","options":["USD","EUR","Both are base currencies","Neither"],"answer":1,"explanation":"EUR is the first currency in EURUSD, so it is the base currency."},
    {"id":"basics-02","level":"BEGINNER","category":"Foundations","title":"Candlesticks and Price Action","minutes":9,"summary":"Read candle bodies, wicks, momentum, rejection, and context.","content":"A candle body shows the open-to-close range; wicks show the high and low. A long wick can suggest rejection, but it is not a signal by itself. Read candles at meaningful levels and with surrounding structure. A bullish candle at resistance may fail; a bearish candle at support may reverse. Avoid interpreting one candle without context.","question":"What should you consider before treating a long wick as a trade signal?","options":["Nothing; every long wick is a signal","Only the candle colour","Its location and surrounding market structure","Only the current spread"],"answer":2,"explanation":"A candle's meaning depends on location, structure, and follow-through."},
    {"id":"basics-03","level":"BEGINNER","category":"Foundations","title":"Support and Resistance","minutes":8,"summary":"Mark reaction zones and avoid treating levels as perfectly exact lines.","content":"Support and resistance are areas where price previously reacted or where orders may cluster. Mark zones around repeated reactions, swing highs/lows, and consolidation boundaries. A level can break, so plan for both rejection and breakout scenarios. Confirmation and invalidation matter more than drawing many lines.","question":"How should a useful support or resistance level usually be treated?","options":["As an exact line that can never break","As a zone that may react or break","As a guaranteed reversal","As a replacement for a stop-loss"],"answer":1,"explanation":"Levels are zones of interest, not guarantees."},
    {"id":"structure-01","level":"INTERMEDIATE","category":"Market Structure","title":"Trend, Swing Highs and Swing Lows","minutes":10,"summary":"Identify higher highs/higher lows and lower highs/lower lows.","content":"An uptrend generally forms higher highs and higher lows; a downtrend forms lower highs and lower lows. A range contains overlapping swings without clear directional progression. Structure can differ by timeframe. Use the 4H chart for broader context and the 15M chart for entry timing, while explaining conflicts rather than hiding them.","question":"Which sequence most clearly describes an uptrend?","options":["Lower highs and lower lows","Higher highs and higher lows","Equal highs only","Random candles without swing progression"],"answer":1,"explanation":"Higher highs and higher lows are the classic uptrend sequence."},
    {"id":"structure-02","level":"INTERMEDIATE","category":"Market Structure","title":"BOS and CHoCH","minutes":10,"summary":"Distinguish continuation breaks from possible character changes.","content":"A Break of Structure (BOS) is commonly used to describe a break in the direction of established structure. A Change of Character (CHoCH) describes an early break against the prior swing sequence and may signal a transition. Definitions vary among trading educators. A wick through a level without acceptance may be a sweep rather than a meaningful break. Seek close, follow-through, and context.","question":"A break against the previous swing trend is often called what in SMC terminology?","options":["CHoCH","Spread","Swap","Take profit"],"answer":0,"explanation":"CHoCH commonly refers to a potential change in market character."},
    {"id":"liquidity-01","level":"INTERMEDIATE","category":"Liquidity & SMC","title":"Liquidity Pools and Sweeps","minutes":11,"summary":"Study equal highs/lows, stop clusters, sweeps, and reactions.","content":"Liquidity is often discussed around visible swing highs/lows and equal highs/lows where stops may cluster. A sweep occurs when price trades beyond a level and then rejects or returns through it. Not every breakout is a sweep, and screenshots cannot reveal actual order-book liquidity. Look for the reaction after the sweep and identify a clear invalidation level.","question":"What additional evidence helps distinguish a potential sweep from a genuine breakout?","options":["A reaction and return through the level","The instrument name alone","A random indicator colour","Nothing; they are always identical"],"answer":0,"explanation":"The post-break reaction and acceptance/rejection help interpret the move."},
    {"id":"smc-01","level":"INTERMEDIATE","category":"Liquidity & SMC","title":"Order Blocks and Fair Value Gaps","minutes":12,"summary":"Understand common SMC definitions and their limitations.","content":"An order block is often marked as a candle or zone before a strong displacement move. A Fair Value Gap (FVG) is commonly described as a three-candle imbalance where the first and third candles do not overlap across a range. These are interpretation frameworks, not proof of institutional orders. Focus on clear displacement, structure, location, mitigation, and whether price has already moved too far.","question":"Does a visible order block prove that institutional orders are waiting there?","options":["Yes, always","No; it is a chart interpretation, not proof of hidden orders","Only on gold","Only on 15M"],"answer":1,"explanation":"A chart cannot prove hidden order placement from an order-block pattern alone."},
    {"id":"entry-01","level":"INTERMEDIATE","category":"Trade Planning","title":"Entries, Invalidation and Confirmation","minutes":10,"summary":"Separate directional bias from a tradeable entry.","content":"A directional bias is not an entry. Define the trigger, entry area, invalidation point, and target before entering. If confirmation has not occurred, classify the setup as developing. If price has already extended too far, avoid chasing. If the invalidation level or target is unclear, waiting is a valid decision.","question":"What is the purpose of an invalidation level?","options":["To guarantee a winning trade","To define where the trade idea is no longer valid","To increase leverage","To replace analysis"],"answer":1,"explanation":"Invalidation defines when the original trade thesis has failed."},
    {"id":"risk-01","level":"BEGINNER","category":"Risk Management","title":"Risk Per Trade and Position Sizing","minutes":10,"summary":"Calculate a cash-risk budget before choosing a position size.","content":"Risk amount = account equity × risk percentage. For example, a $500 account risking 1% has a $5 planned risk before costs and slippage. Position size must then be calculated using the instrument's contract size, tick/pip value, stop distance, and account currency. Gold, crypto, and cross-currency pairs can differ significantly. Never copy a generic lot size without checking your broker's contract specifications.","question":"What is 1% planned risk on a $500 account?","options":["$0.50","$5","$50","$100"],"answer":1,"explanation":"500 × 0.01 = 5."},
    {"id":"risk-02","level":"INTERMEDIATE","category":"Risk Management","title":"Risk-to-Reward and Trade Selection","minutes":8,"summary":"Compare stop distance with target distance and respect nearby barriers.","content":"Risk-to-reward compares the potential reward to the planned risk. If entry-to-stop distance is 20 points and entry-to-target distance is 40 points, the gross ratio is 1:2. A high ratio alone does not make a trade good: the target must be realistic and fees, spread, slippage, and probability matter. Do not widen a stop simply to force a preferred ratio.","question":"If planned risk is 20 points and target distance is 40 points, what is the reward-to-risk ratio?","options":["1:1","1:2","2:1 risk to reward","1:4"],"answer":1,"explanation":"The target distance is twice the stop distance, giving 1:2 risk-to-reward."},
    {"id":"mindset-01","level":"BEGINNER","category":"Discipline","title":"Trading Psychology and Journaling","minutes":7,"summary":"Record the plan and result to improve process, not chase outcomes.","content":"A trading journal records the setup, planned risk, execution, result, and lessons. Review a meaningful sample rather than judging a strategy from one trade. Avoid revenge trading, moving stops without a plan, and increasing risk after losses. A winning trade can still be poorly executed; a losing trade can follow a good process.","question":"What is a useful way to evaluate trading performance?","options":["Judge every trade only by profit or loss","Review a sample of trades and the quality of the process","Delete losing trades","Increase risk after a loss"],"answer":1,"explanation":"Process and a meaningful sample give better feedback than one outcome."},
    {"id":"advanced-01","level":"ADVANCED","category":"Multi-Timeframe Analysis","title":"Combining 4H Context with 15M Execution","minutes":11,"summary":"Use the higher timeframe for context and lower timeframe for timing.","content":"The 4H chart can reveal broad structure and important levels, while the 15M chart can help refine a trigger. Alignment can strengthen a setup but is not mandatory for every scalp. Countertrend trades generally require careful justification and can carry added risk. Describe the disagreement clearly, define invalidation, and do not force a signal when evidence is insufficient.","question":"What is a sensible role for the 4H chart in a 4H + 15M workflow?","options":["It guarantees the next 15M candle","It provides broader context and important levels","It replaces entry planning","It means countertrend trades are impossible"],"answer":1,"explanation":"The higher timeframe provides context; the lower timeframe helps time execution."},
    {"id":"fib-01","level":"INTERMEDIATE","category":"Technical Tools","title":"Fibonacci Retracements","minutes":8,"summary":"Use retracement levels as confluence, not standalone signals.","content":"Fibonacci retracements are drawn across a meaningful swing to mark possible pullback zones. Commonly watched levels include 38.2%, 50%, and 61.8%, but no level guarantees a reaction. Choose the swing consistently, consider market structure and nearby support/resistance, and wait for price action confirmation rather than entering because price touched a number.","question":"How should Fibonacci retracement levels be used?","options":["As guaranteed reversal points","As possible confluence zones with structure and confirmation","As a substitute for stop-losses","Only after a losing trade"],"answer":1,"explanation":"Fibonacci levels are reference zones; confirmation and context still matter."},
    {"id":"premium-01","level":"ADVANCED","category":"Liquidity & SMC","title":"Premium and Discount Zones","minutes":8,"summary":"Use a clearly defined dealing range to contextualize price location.","content":"Premium and discount are often defined relative to the midpoint of a selected dealing range. Price above the midpoint is called premium and below it discount. The usefulness depends on selecting a defensible swing range and considering trend, structure, and liquidity. The midpoint alone is not a buy or sell signal.","question":"What must be identified before classifying price as premium or discount?","options":["A defensible dealing range","The user's account password","Only the candle colour","A guaranteed news forecast"],"answer":0,"explanation":"Premium and discount depend on the chosen dealing range."},
    {"id":"news-01","level":"INTERMEDIATE","category":"Fundamentals","title":"News, Fundamentals and Volatility","minutes":9,"summary":"Understand event risk and avoid assuming a chart screenshot contains live news.","content":"Central-bank decisions, inflation, employment data, and unexpected headlines can increase volatility, spreads, and slippage. A chart screenshot does not provide a live news feed. Check a reliable economic calendar separately, know the broker's trading conditions around events, and avoid assuming that technical levels will hold during major releases.","question":"Can an uploaded chart screenshot alone confirm the latest economic news?","options":["Yes, always","No; check a reliable current news/calendar source separately","Only if it is a 4H chart","Only for EURUSD"],"answer":1,"explanation":"A chart screenshot is not a live news feed."},
    {"id":"review-01","level":"ADVANCED","category":"Strategy Testing","title":"Backtesting, Expectancy and Strategy Review","minutes":10,"summary":"Evaluate a repeatable process over a meaningful sample.","content":"Backtesting applies clear rules to historical data; forward testing observes the rules in current conditions with controlled risk. Expectancy estimates average outcome per trade using win rate and average wins/losses. Include spread, commission, slippage, and market-regime changes. Avoid curve-fitting rules to a tiny sample and track whether trades followed the plan.","question":"Why should a strategy be evaluated across a meaningful sample?","options":["One winning trade proves the strategy","A sample helps estimate consistency and expectancy","It removes all risk","It guarantees future results"],"answer":1,"explanation":"A larger, well-recorded sample gives a more useful view of the strategy's behaviour."},
]


def json_response(handler, code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except Exception:
        pass


def read_json(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise ValueError("Invalid request length.") from exc
    if length <= 0 or length > MAX_BODY:
        raise ValueError("Request is empty or too large.")
    try:
        data = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid request JSON.") from exc
    if not isinstance(data, dict):
        raise ValueError("Invalid request.")
    return data


def authenticated_user(handler):
    user = verify_access_token(extract_bearer_token(handler))
    user_id = str(user.get("id", "")).strip()
    if not user_id:
        raise PermissionError("Your session could not be verified. Please sign in again.")
    return user_id


def db_request(method, table, query=None, payload=None, prefer=None):
    base = get_supabase_url().strip().rstrip("/")
    key = get_supabase_service_key().strip()
    if not base or not key:
        raise RuntimeError("Learning Hub storage is not configured.")
    url = base + "/rest/v1/" + table
    if query:
        url += "?" + urllib.parse.urlencode(query, safe="(),.*:")
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"apikey": key, "Authorization": "Bearer " + key, "Accept": "application/json", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=18) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # Avoid exposing database details to the client.
        raise RuntimeError("Learning Hub storage request failed (HTTP {}). Check the trader-tools SQL schema is installed.".format(exc.code)) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Learning Hub storage is temporarily unavailable.") from exc
    if not raw.strip():
        return []
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid response from Learning Hub storage.") from exc
    return result


def user_rows(table, user_id, select, order=None, limit="100"):
    query = {"user_id": "eq." + user_id, "select": select, "limit": limit}
    if order:
        query["order"] = order
    result = db_request("GET", table, query=query)
    return result if isinstance(result, list) else []


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        try:
            user_id = authenticated_user(self)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            action = (query.get("action", ["lessons"])[0] or "lessons").strip().lower()
            if action == "lessons":
                progress = user_rows("learning_progress", user_id, "lesson_id,completed,quiz_score,updated_at", "updated_at.desc", "200")
                json_response(self, 200, {"lessons": LESSONS, "progress": progress})
            elif action == "journal":
                entries = user_rows("trade_journal", user_id, "id,instrument,direction,entry_price,stop_loss,take_profit,account_balance,risk_percent,outcome,notes,created_at", "created_at.desc", "100")
                json_response(self, 200, {"entries": entries})
            else:
                json_response(self, 400, {"error": "Unsupported action."})
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 503, {"error": str(exc)})
        except Exception:
            json_response(self, 500, {"error": "Unable to load trader tools."})

    def do_POST(self):
        try:
            user_id = authenticated_user(self)
            data = read_json(self)
            action = str(data.get("action", "")).strip().lower()
            if action == "progress":
                lesson_id = str(data.get("lesson_id", "")).strip()
                if lesson_id not in {lesson["id"] for lesson in LESSONS}:
                    raise ValueError("Unknown lesson.")
                score = int(data.get("quiz_score", -1))
                if score < 0 or score > 100:
                    raise ValueError("Quiz score must be between 0 and 100.")
                payload = {"user_id": user_id, "lesson_id": lesson_id, "completed": bool(data.get("completed", False)), "quiz_score": score}
                rows = db_request("POST", "learning_progress", query={"on_conflict": "user_id,lesson_id"}, payload=payload, prefer="resolution=merge-duplicates,return=representation")
                json_response(self, 200, {"saved": True, "progress": rows[0] if isinstance(rows, list) and rows else payload})
            elif action == "journal":
                instrument = str(data.get("instrument", "")).strip().upper()
                direction = str(data.get("direction", "")).strip().upper()
                if len(instrument) < 3 or len(instrument) > 12:
                    raise ValueError("Enter a valid instrument.")
                if direction not in {"BUY", "SELL"}:
                    raise ValueError("Direction must be BUY or SELL.")
                def number(name, required=True):
                    value = data.get(name)
                    if value in (None, "") and not required:
                        return None
                    try:
                        n = float(value)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("Enter a valid number for " + name.replace("_", " ") + ".") from exc
                    if not (n == n and abs(n) != float("inf")) or n <= 0:
                        raise ValueError("Values must be positive finite numbers.")
                    return n
                balance = number("account_balance")
                risk_percent = number("risk_percent")
                if risk_percent > 10:
                    raise ValueError("Risk percentage must be 10% or less.")
                entry = number("entry_price", False)
                stop = number("stop_loss", False)
                target = number("take_profit", False)
                outcome = str(data.get("outcome", "PLANNED")).strip().upper()
                if outcome not in {"PLANNED", "WIN", "LOSS", "BREAKEVEN", "CANCELLED"}:
                    raise ValueError("Invalid journal outcome.")
                notes = str(data.get("notes", "")).strip()[:3000]
                payload = {"user_id": user_id, "instrument": instrument, "direction": direction, "entry_price": entry, "stop_loss": stop, "take_profit": target, "account_balance": balance, "risk_percent": risk_percent, "outcome": outcome, "notes": notes}
                rows = db_request("POST", "trade_journal", payload=payload, prefer="return=representation")
                json_response(self, 201, {"saved": True, "entry": rows[0] if isinstance(rows, list) and rows else payload})
            elif action == "feedback":
                category = str(data.get("category", "")).strip().upper()
                message = str(data.get("message", "")).strip()
                instrument = str(data.get("instrument", "")).strip().upper()[:12]
                signal = str(data.get("signal", "")).strip().upper()[:20]
                if category not in {"INACCURATE_ANALYSIS", "UNCLEAR_EXPLANATION", "BUG_OR_ERROR", "FEATURE_REQUEST", "OTHER"}:
                    raise ValueError("Choose a valid feedback category.")
                if len(message) < 5 or len(message) > 3000:
                    raise ValueError("Feedback must be between 5 and 3000 characters.")
                payload = {"user_id": user_id, "category": category, "message": message, "instrument": instrument or None, "signal": signal or None}
                rows = db_request("POST", "analysis_feedback", payload=payload, prefer="return=representation")
                json_response(self, 201, {"saved": True, "feedback": rows[0] if isinstance(rows, list) and rows else payload})
            else:
                json_response(self, 400, {"error": "Unsupported action."})
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 503, {"error": str(exc)})
        except Exception:
            json_response(self, 500, {"error": "Unable to save trader tools data."})
