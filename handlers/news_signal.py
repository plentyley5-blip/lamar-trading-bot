"""Authenticated, news-only AI outlook and post-release setup endpoint."""
import json
import math
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

from analysis_common import json_response
from api.user_security import extract_bearer_token, verify_access_token, is_owner_user
from handlers.subscription_access import has_active_subscription

TZ = ZoneInfo("Africa/Kampala")
MODEL = "gemini-3.5-flash-lite"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/" + MODEL + ":generateContent"
RELATED = {
    "USD": {"EURUSD","GBPUSD","USDJPY","USDCHF","USDCAD","AUDUSD","NZDUSD","XAUUSD","XAGUSD"},
    "EUR": {"EURUSD","EURGBP","EURJPY","EURCHF","EURAUD","EURCAD","EURNZD"},
    "GBP": {"GBPUSD","EURGBP","GBPJPY","GBPCHF","GBPAUD","GBPCAD","GBPNZD"},
    "JPY": {"USDJPY","EURJPY","GBPJPY","AUDJPY","CADJPY","CHFJPY","NZDJPY"},
    "CHF": {"USDCHF","EURCHF","GBPCHF","AUDCHF","CADCHF","NZDCHF","CHFJPY"},
    "CAD": {"USDCAD","EURCAD","GBPCAD","AUDCAD","CADJPY","CADCHF","NZDCAD"},
    "AUD": {"AUDUSD","EURAUD","GBPAUD","AUDJPY","AUDCAD","AUDCHF","AUDNZD"},
    "NZD": {"NZDUSD","EURNZD","GBPNZD","AUDNZD","NZDJPY","NZDCAD","NZDCHF"},
}

# Flexible confluence model: not every market filter must agree. At least two
# independent factors must support direction, including structure or momentum.
# Mechanical data, entry, stop and reward/risk checks remain mandatory below.
CONFLUENCE_WEIGHTS = {
    "PRICE_STRUCTURE": 3,
    "MOMENTUM": 2,
    "EVENT_REACTION": 2,
    "TREND_ALIGNMENT": 1,
    "VOLATILITY_QUALITY": 1,
    "SUPPORT_RESISTANCE": 1,
}
MIN_SIGNAL_CONFIDENCE = 55
MIN_CONFLUENCE_SCORE = 4
MIN_CONFIRMED_FACTORS = 2

def qualify_signal(assessment):
    confirmed = sorted({
        str(x).upper() for x in assessment.get("confirmed_factors", [])
        if str(x).upper() in CONFLUENCE_WEIGHTS
    })
    score = sum(CONFLUENCE_WEIGHTS[x] for x in confirmed)
    anchor = bool({"PRICE_STRUCTURE", "MOMENTUM"}.intersection(confirmed))
    severe_conflict = bool(assessment.get("severe_conflict", False))
    qualified = (
        assessment.get("signal") in ("BUY", "SELL")
        and int(assessment.get("confidence", 0)) >= MIN_SIGNAL_CONFIDENCE
        and len(confirmed) >= MIN_CONFIRMED_FACTORS
        and anchor
        and score >= MIN_CONFLUENCE_SCORE
        and not severe_conflict
    )
    return {
        "confirmed_factors": confirmed,
        "qualification_score": score,
        "setup_qualified": qualified,
    }

def parse_event_dt(event):
    try:
        return datetime.strptime(str(event.get("date","")) + " " + str(event.get("time","")), "%a %d %b %Y %H:%M").replace(tzinfo=TZ)
    except ValueError as exc:
        raise ValueError("The event date or time is invalid. Refresh the economic calendar.") from exc

def verify_event(supplied):
    if not isinstance(supplied, dict):
        raise ValueError("Select a high-impact news event first.")
    keys = ("currency","title","date","time")
    if any(not str(supplied.get(k,"")).strip() for k in keys):
        raise ValueError("The selected news event is incomplete.")
    if str(supplied.get("impact","")).strip().lower() != "high":
        raise ValueError("This feature only analyses verified high-impact events.")
    # The app obtains this event from the existing live economic-calendar endpoint.
    # Do not make a second calendar-provider request here; that could exceed the
    # serverless execution window during upstream fallback.
    parse_event_dt(supplied)
    return {k: str(supplied.get(k,"")).strip() for k in
            ("currency","title","date","time","actual","forecast","previous","impact")}

def parse_candle_time(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z","+00:00"))
    except ValueError:
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S","%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(raw,fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                pass
    if dt is None:
        return None
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(TZ)

def validate_candles(raw, updated_at, stale=False):
    if not isinstance(raw,list) or not 30 <= len(raw) <= 120:
        raise ValueError("At least 30 recent one-minute candles are required.")
    if stale:
        raise ValueError("Market candles are delayed. No live news signal can be issued.")
    try:
        stamp = datetime.fromisoformat(str(updated_at or "").replace("Z","+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Market candle freshness could not be verified.")
        age = max(0,int((datetime.now(TZ)-stamp.astimezone(TZ)).total_seconds()))
    except (TypeError,ValueError) as exc:
        raise ValueError("Market candle freshness could not be verified.") from exc
    if age > 180:
        raise ValueError("Market candles are delayed. No live news signal can be issued.")
    rows = []
    for item in raw:
        if not isinstance(item,dict):
            continue
        candle_time = parse_candle_time(item.get("time"))
        try:
            o,h,l,c = [float(item[k]) for k in ("open","high","low","close")]
        except (KeyError,TypeError,ValueError):
            continue
        if candle_time is None or not all(math.isfinite(x) and x > 0 for x in (o,h,l,c)) or h < max(o,c,l) or l > min(o,c,h):
            continue
        rows.append({"time":candle_time,"open":o,"high":h,"low":l,"close":c})
    rows.sort(key=lambda x:x["time"])
    if len(rows) < 30:
        raise ValueError("The provider returned insufficient valid candles.")
    candle_age = (datetime.now(TZ)-rows[-1]["time"]).total_seconds()
    if candle_age < -180 or candle_age > 180:
        raise ValueError("The latest market candle is not current. No live news signal can be issued.")
    return rows,age

def atr(rows):
    tr=[]
    for i in range(1,len(rows)):
        a,b=rows[i],rows[i-1]
        tr.append(max(a["high"]-a["low"],abs(a["high"]-b["close"]),abs(a["low"]-b["close"])))
    sample=tr[-14:]
    return sum(sample)/len(sample) if sample else 0.0

def fmt(value):
    return f"{value:.8f}".rstrip("0").rstrip(".")

def ai_assess(event,symbol,stage,rows):
    key=os.environ.get("GEMINI_API_KEY","").strip()
    if not key:
        raise RuntimeError("The AI news-analysis service is not configured.")
    prompt = (
      "You are LM Analyzer's high-impact Forex news analyst. Analyse only the verified event and supplied real 1-minute OHLC candles. "
      "Do not assume textbook currency reaction determines direction. Assess actual price response, momentum, volatility and swing structure. "
      "Return JSON only: signal (BUY, SELL, WAIT), confidence (integer 0-100 evidence-strength score, NOT win probability), "
      "trend_structure, event_bias, confirmed_factors, conflicting_factors, severe_conflict, severe_conflict_reason, "
      "reasons (2-4 short strings), invalidation. "
      "FLEXIBLE CONFLUENCE RULE: Do NOT require every indicator or market filter to agree. Judge the current chart context and select the strongest relevant confirmations. "
      "A setup may qualify when at least TWO independent factors genuinely support the same direction, including PRICE_STRUCTURE or MOMENTUM, "
      "and the evidence is coherent. Other factors may be NEUTRAL, unavailable, or mildly conflicting; do not reject a sound setup only because every optional factor is not aligned. "
      "Possible confirmed_factors are PRICE_STRUCTURE, MOMENTUM, EVENT_REACTION, TREND_ALIGNMENT, VOLATILITY_QUALITY, SUPPORT_RESISTANCE. "
      "Only list a factor as confirmed when the supplied candles actually show it. Do not count the same price movement twice under different names. "
      "For PRE_NEWS, EVENT_REACTION is unavailable because the release has not occurred; never claim it is confirmed. "
      "For POST_NEWS, prioritize actual post-release price reaction, structure and momentum. "
      "Set severe_conflict true only for a material contradiction that invalidates the proposed direction or makes the setup unsafe; a merely neutral/missing optional filter is NOT severe. "
      "Do not invent confirmations, prices or levels. Confidence reflects overall evidence strength, never win probability. "
      "HARD SAFETY: no signal if price data is contradictory/unstable, a safe structural stop cannot be placed, or a sensible reward/risk setup cannot be calculated. "
      "The application independently validates candle freshness and entry/stop/target geometry; do not try to bypass those checks. "
      "DISPLAY RULES: event_bias must explain the directional bias plainly; WAIT means there is no reliable directional edge. "
      "trend_structure must describe observed structure, not predict the event result. "
      "invalidation must state an observable price/structure condition and, when candles support it, the exact price boundary and direction. "
      "For a ranging market, identify both range boundaries if they are clear. Do not use the vague word 'Invalid' as the whole condition. "
      "For PRE_NEWS, separate directional bias from a conditional setup; a BUY/SELL bias is not permission to enter before release confirmation. "
      "LIVE_REACTION must be conservative. POST_NEWS judges observed reaction rather than forecast."
    )
    data={"event":{k:event.get(k,"") for k in ("currency","title","date","time","actual","forecast","previous","impact")},
          "instrument":symbol,"stage":stage,"latest_price":rows[-1]["close"],"atr_14_1m":atr(rows),
          "candles_1m":[{"time":x["time"].isoformat(),"open":x["open"],"high":x["high"],"low":x["low"],"close":x["close"]} for x in rows[-60:]]}
    factor_enum=["PRICE_STRUCTURE","MOMENTUM","EVENT_REACTION","TREND_ALIGNMENT","VOLATILITY_QUALITY","SUPPORT_RESISTANCE"]
    schema={"type":"object","properties":{
      "signal":{"type":"string","enum":["BUY","SELL","WAIT"]},
      "confidence":{"type":"integer","minimum":0,"maximum":100},
      "trend_structure":{"type":"string"},"event_bias":{"type":"string"},
      "confirmed_factors":{"type":"array","items":{"type":"string","enum":factor_enum}},
      "conflicting_factors":{"type":"array","items":{"type":"string","enum":factor_enum}},
      "severe_conflict":{"type":"boolean"},"severe_conflict_reason":{"type":"string"},
      "reasons":{"type":"array","items":{"type":"string"}},"invalidation":{"type":"string"}},
      "required":["signal","confidence","trend_structure","event_bias","confirmed_factors","conflicting_factors","severe_conflict","severe_conflict_reason","reasons","invalidation"]}
    body=json.dumps({"contents":[{"role":"user","parts":[{"text":prompt+"\n\nINPUT:\n"+json.dumps(data,separators=(",",":"))}]}],
      "generationConfig":{"responseMimeType":"application/json","responseJsonSchema":schema,"maxOutputTokens":900,"thinkingConfig":{"thinkingLevel":"low"}}},separators=(",",":")).encode()
    req=urllib.request.Request(GEMINI_URL,data=body,headers={"x-goog-api-key":key,"Content-Type":"application/json"},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=25) as response:
            raw=json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code==429:
            raise RuntimeError("The AI news-analysis rate limit has been reached. Retry shortly.") from exc
        raise RuntimeError("The AI news-analysis service could not complete the assessment.") from exc
    except (urllib.error.URLError,TimeoutError,json.JSONDecodeError) as exc:
        raise RuntimeError("The AI news-analysis service is temporarily unavailable.") from exc
    try:
        result=json.loads(raw["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError,IndexError,TypeError,json.JSONDecodeError) as exc:
        raise RuntimeError("The AI returned an incomplete news assessment.") from exc
    signal=str(result.get("signal","WAIT")).upper()
    if signal not in ("BUY","SELL","WAIT"):
        signal="WAIT"
    try:
        confidence=max(0,min(100,int(result.get("confidence",0))))
    except (ValueError,TypeError):
        confidence=0
    reasons=result.get("reasons") if isinstance(result.get("reasons"),list) else []
    reasons=[str(x).strip()[:180] for x in reasons if str(x).strip()]
    bias_text=str(result.get("event_bias","The event reaction is not yet clear.")).strip()[:200]
    if not bias_text.lower().startswith("directional bias"):
        bias_text=("Directional bias — " + signal + ": " + bias_text)[:240]
    invalidation_text=str(result.get("invalidation","")).strip()
    # Normalize ambiguous model wording such as "Invalid, ..." into an explicit
    # condition label, and require a candle-close trigger rather than a wick.
    for prefix in ("invalidation condition:", "invalidation:", "invalid,",
                   "invalid:", "invalid -", "invalid —"):
        if invalidation_text.lower().startswith(prefix):
            invalidation_text=invalidation_text[len(prefix):].strip(" :-—,")
            break
    if not any(ch.isdigit() for ch in invalidation_text):
        invalidation_text=("No reliable numeric invalidation level is visible in the supplied candles. "
                           "Reassess with fresh 1-minute candles; do not guess a boundary.")
    elif "1-minute" not in invalidation_text.lower() and "1 min" not in invalidation_text.lower():
        invalidation_text += (" Treat a break as confirmed only after a completed 1-minute candle close; "
                              "a wick alone does not count.")
    invalidation_text=("Invalidation condition: " + invalidation_text)[:320]
    if stage == "PRE_NEWS":
        # Always make the post-release confirmation rule visible, even for WAIT
        # outlooks; never promote a neutral bias into a forced trade.
        reasons=reasons[:3]
        reasons.append(
            "Post-release confirmation: Wait until the release and the initial five-minute reaction have passed. "
            "Then reassess fresh 1-minute candles. If conditional levels are shown, require a completed 1-minute "
            "close through the entry level and a hold/retest; a wick or first spike is not confirmation. "
            "If the outlook is WAIT or levels are absent, remain WAIT."
        )
    assessment={
      "signal":signal,"confidence":confidence,
      "trend_structure":str(result.get("trend_structure","Insufficient structure evidence."))[:240],
      "event_bias":bias_text,
      "confirmed_factors":result.get("confirmed_factors") if isinstance(result.get("confirmed_factors"),list) else [],
      "conflicting_factors":result.get("conflicting_factors") if isinstance(result.get("conflicting_factors"),list) else [],
      "severe_conflict":bool(result.get("severe_conflict",False)),
      "severe_conflict_reason":str(result.get("severe_conflict_reason","")).strip()[:200],
      "reasons":reasons[:4],
      "invalidation":invalidation_text}
    assessment.update(qualify_signal(assessment))
    if assessment["setup_qualified"]:
        labels=", ".join(x.replace("_"," ").lower() for x in assessment["confirmed_factors"])
        note=("Setup qualified by relevant confirmations: "+labels+
              ". Neutral or unavailable optional filters were not treated as mandatory.")
    elif signal in ("BUY","SELL"):
        if assessment["severe_conflict"]:
            note=("No trade: material conflicting evidence was identified. "+
                  assessment["severe_conflict_reason"])
        else:
            note=("No trade yet: the setup did not meet the minimum combination of independent confirmations.")
    else:
        note="No reliable directional setup is present; WAIT remains the correct result."
    assessment["reasons"]=(assessment["reasons"][:3]+[note[:220]])
    return assessment

def trade_levels(rows,signal):
    volatility=atr(rows)
    if volatility<=0:
        return None
    prior=rows[-6:-1]
    swing=rows[-11:-1]
    price=rows[-1]["close"]
    if signal=="BUY":
        entry=max(x["high"] for x in prior)+volatility*.10
        stop=min(x["low"] for x in swing)-volatility*.20
        risk=entry-stop
        reached=price>=entry
        tp1,tp2=entry+risk*1.5,entry+risk*2.5
        late = price>=tp1 or price-entry>volatility*.75
    else:
        entry=min(x["low"] for x in prior)-volatility*.10
        stop=max(x["high"] for x in swing)+volatility*.20
        risk=stop-entry
        reached=price<=entry
        tp1,tp2=entry-risk*1.5,entry-risk*2.5
        late = price<=tp1 or entry-price>volatility*.75
    if risk<=0 or risk>volatility*8:
        return None
    return {"entry":fmt(entry),"stop_loss":fmt(stop),"take_profit_1":fmt(tp1),"take_profit_2":fmt(tp2),
            "entry_reached":reached,"late_entry":late,"current_price":fmt(price)}

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        json_response(self,204,{})
    def do_POST(self):
        try:
            user = verify_access_token(extract_bearer_token(self))
        except Exception as exc:
            json_response(self,401,{"error":str(exc) or "Sign in to request a news assessment."})
            return
        user_id = str(user.get("id", "")).strip() if isinstance(user, dict) else ""
        if not user_id:
            json_response(self,401,{"error":"A valid signed-in session is required."})
            return
        # Owner identity is verified server-side against OWNER_EMAIL.
        # Every non-owner still needs an active, non-expired subscription.
        if not is_owner_user(user) and not has_active_subscription(user_id):
            json_response(self,403,{"error":"An active LM Analyzer subscription is required to access High Impact News AI outlooks and signals.","premium_feature":"high_impact_news"})
            return
        try:
            length=int(self.headers.get("Content-Length","0"))
            if length<=0 or length>180000:
                raise ValueError("The news assessment request is empty or too large.")
            payload=json.loads(self.rfile.read(length).decode("utf-8"))
            event=verify_event(payload.get("event"))
            symbol=str(payload.get("instrument") or "").strip().upper()
            currency=str(event.get("currency") or "").upper()
            if symbol not in RELATED.get(currency,set()):
                raise ValueError("This instrument is not related to the selected event currency.")
            rows,age=validate_candles(payload.get("candles"),payload.get("market_data_updated_at"),payload.get("market_data_stale",False))
            event_dt=parse_event_dt(event)
            now=datetime.now(TZ)
            if now<event_dt:
                stage="PRE_NEWS"
            elif now<=event_dt+timedelta(minutes=5):
                stage="LIVE_REACTION"
            elif now<=event_dt+timedelta(hours=4):
                stage="POST_NEWS"
            else:
                json_response(self,200,{"instrument":symbol,"stage":"EVENT_ENDED","signal":"WAIT","confidence":0,
                  "trend_structure":"The high-impact event window has ended.","event_bias":"No fresh event-driven setup is being issued.",
                  "reasons":["This event is outside the four-hour post-release assessment window."],
                  "invalidation":"Select a current or upcoming high-impact event.","setup_status":"NO_SETUP","order_type":"NONE",
                  "current_price":fmt(rows[-1]["close"]),"market_data_age_seconds":age})
                return
            if stage=="LIVE_REACTION":
                json_response(self,200,{"instrument":symbol,"stage":stage,"signal":"WAIT","confidence":0,
                  "trend_structure":"The initial release reaction is still developing.","event_bias":"Wait for spreads and the first volatility spike to settle.",
                  "reasons":["The first five minutes after a high-impact release can contain sharp reversals and unstable spreads."],
                  "invalidation":"Reassess after the initial release window using fresh candles.","setup_status":"WAITING","order_type":"WAIT",
                  "current_price":fmt(rows[-1]["close"]),"market_data_age_seconds":age})
                return
            outlook=ai_assess(event,symbol,stage,rows)
            out={"instrument":symbol,"event_title":event.get("title",""),"event_currency":currency,"stage":stage,**outlook,
              "market_data_age_seconds":age,"current_price":fmt(rows[-1]["close"]),
              "setup_status":"PRE_NEWS_OUTLOOK" if stage=="PRE_NEWS" else "NO_SETUP",
              "order_type":"NONE","entry":"","stop_loss":"","take_profit_1":"","take_profit_2":"",
              "warning":"News-related market analysis is uncertain. This is not a guaranteed prediction or financial advice."}
            if stage=="PRE_NEWS":
                if outlook["setup_qualified"]:
                    levels=trade_levels(rows,outlook["signal"])
                    if levels:
                        # These are conditional planning levels, not an instruction to enter
                        # before the event reaction confirms the setup.
                        levels.pop("entry_reached",None)
                        levels.pop("late_entry",None)
                        out.update(levels)
                        out["setup_status"]="PRE_NEWS_POTENTIAL_"+outlook["signal"]
                        out["warning"]="Conditional pre-news outlook only. These levels are for planning; do not enter before the event reaction confirms the setup."
                    else:
                        out["signal"]="WAIT"; out["confidence"]=min(outlook["confidence"],50)
                        out["setup_status"]="NO_SETUP"; out["order_type"]="NONE"
                        out["reasons"].append("A safe conditional entry and stop-loss could not be calculated from recent candles.")
                else:
                    out["signal"]="WAIT"
                    out["setup_status"]="PRE_NEWS_OUTLOOK"
                    out["order_type"]="NONE"
            elif outlook["setup_qualified"]:
                levels=trade_levels(rows,outlook["signal"])
                if levels:
                    reached=levels.pop("entry_reached")
                    late=levels.pop("late_entry")
                    out.update(levels)
                    if late:
                        out["setup_status"]="LATE_ENTRY"
                        out["order_type"]="NONE"
                        out["headline"]="TRADE MISSED"
                        out["warning"]="The entry opportunity has moved too far or its first target has already been reached. Do not chase price."
                    else:
                        out["setup_status"]="READY" if reached else "WAITING"
                        out["order_type"]="MARKET" if reached else "WAIT"
                        if reached:
                            out["headline"]=symbol+" "+outlook["signal"]+" NOW"
                        else:
                            out["warning"]="Entry has not been reached. Wait for price to reach the stated entry; do not chase."
                else:
                    out["signal"]="WAIT"; out["confidence"]=min(outlook["confidence"],50)
                    out["setup_status"]="NO_SETUP"; out["order_type"]="NONE"
                    out["reasons"].append("A safe structural entry and stop-loss could not be calculated from recent candles.")
            else:
                out["signal"]="WAIT"; out["setup_status"]="NO_SETUP"; out["order_type"]="NONE"
            json_response(self,200,out)
        except ValueError as exc:
            json_response(self,400,{"error":str(exc)})
        except RuntimeError as exc:
            json_response(self,503,{"error":str(exc) or "News assessment is unavailable."})
        except Exception as exc:
            print("news_signal error:",str(exc)[:220])
            json_response(self,500,{"error":"News-based assessment could not be completed."})
    def log_message(self,*_args):
        return
