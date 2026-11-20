"""Authenticated OHLC history endpoint for the Live Markets candlestick view."""
import json, os, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from analysis_common import json_response
from api.user_security import extract_bearer_token, verify_access_token
from service_cache import read_shared_cache, write_shared_cache, claim_shared_cache_refresh, release_shared_cache_refresh, claim_provider_credits, SharedCacheError

SYMBOLS = {
 "EURUSD":"EUR/USD","GBPUSD":"GBP/USD","USDJPY":"USD/JPY","USDCHF":"USD/CHF","USDCAD":"USD/CAD","AUDUSD":"AUD/USD","NZDUSD":"NZD/USD",
 "EURGBP":"EUR/GBP","EURJPY":"EUR/JPY","EURCHF":"EUR/CHF","EURAUD":"EUR/AUD","EURCAD":"EUR/CAD","EURNZD":"EUR/NZD",
 "GBPJPY":"GBP/JPY","GBPCHF":"GBP/CHF","GBPAUD":"GBP/AUD","GBPCAD":"GBP/CAD","GBPNZD":"GBP/NZD",
 "AUDJPY":"AUD/JPY","AUDCAD":"AUD/CAD","AUDCHF":"AUD/CHF","AUDNZD":"AUD/NZD",
 "CADJPY":"CAD/JPY","CADCHF":"CAD/CHF","CHFJPY":"CHF/JPY","NZDJPY":"NZD/JPY","NZDCAD":"NZD/CAD","NZDCHF":"NZD/CHF",
 "XAUUSD":"XAU/USD","XAGUSD":"XAG/USD","XPTUSD":"XPT/USD",
 "WTI":"WTI","BRENT":"BRENT","COPPER":"XCU/USD",
 "BTCUSD":"BTC/USD","ETHUSD":"ETH/USD","SOLUSD":"SOL/USD","XRPUSD":"XRP/USD","LTCUSD":"LTC/USD","ADAUSD":"ADA/USD",
 "AAPL":"AAPL","MSFT":"MSFT","NVDA":"NVDA","TSLA":"TSLA","AMZN":"AMZN","GOOGL":"GOOGL","META":"META",
 "US500":os.environ.get("TWELVE_DATA_US500_SYMBOL","").strip(),
 "NAS100":os.environ.get("TWELVE_DATA_NAS100_SYMBOL","").strip(),
 "US30":os.environ.get("TWELVE_DATA_US30_SYMBOL","").strip(),
 "GER40":os.environ.get("TWELVE_DATA_GER40_SYMBOL","").strip(),
 "UK100":os.environ.get("TWELVE_DATA_UK100_SYMBOL","").strip(),
}
INTERVALS={"1min":"1min","5min":"5min","15min":"15min","1h":"1h","4h":"4h","1day":"1day"}

def _candles(symbol, interval, api_key):
    provider_symbol=SYMBOLS.get(symbol)
    if not provider_symbol:
        raise ValueError("No verified provider symbol is configured for this market.")
    if not api_key: raise RuntimeError("Live market data is not configured.")
    query=urllib.parse.urlencode({"symbol":provider_symbol,"interval":interval,"outputsize":"100","apikey":api_key},safe="/")
    req=urllib.request.Request("https://api.twelvedata.com/time_series?"+query,headers={"Accept":"application/json","User-Agent":"LMAnalyzer/1.0"})
    try:
        with urllib.request.urlopen(req,timeout=12) as response: data=json.loads(response.read().decode("utf-8",errors="replace"))
    except urllib.error.HTTPError as exc:
        try: message=json.loads(exc.read().decode("utf-8",errors="replace")).get("message","Provider rejected candle request.")
        except Exception: message="Provider rejected candle request."
        raise RuntimeError(str(message)[:160]) from exc
    except (urllib.error.URLError,TimeoutError) as exc: raise RuntimeError("Market chart provider is temporarily unavailable.") from exc
    if not isinstance(data,dict) or data.get("status")=="error" or not isinstance(data.get("values"),list):
        raise RuntimeError(str((data or {}).get("message","No candle history is available."))[:160])
    values=[]
    for row in reversed(data["values"]):
        try:
            values.append({"time":row["datetime"],"open":float(row["open"]),"high":float(row["high"]),"low":float(row["low"]),"close":float(row["close"])})
        except (KeyError,TypeError,ValueError): continue
    if not values: raise RuntimeError("The provider returned no valid candles.")
    return {"symbol":symbol,"interval":interval,"candles":values,"provider":"Twelve Data","updated_at":__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00","Z")}

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self): json_response(self,204,{})
    def do_GET(self):
        try: verify_access_token(extract_bearer_token(self))
        except Exception as exc:
            json_response(self,401,{"error":str(exc) or "Sign in to view market charts."}); return
        from urllib.parse import parse_qs,urlsplit
        params=parse_qs(urlsplit(self.path).query)
        symbol=(params.get("symbol") or [""])[0].upper()
        interval=(params.get("interval") or ["15min"])[0].lower()
        if symbol not in SYMBOLS or interval not in INTERVALS:
            json_response(self,400,{"error":"Unsupported instrument or timeframe."}); return
        cache_key="market_candles_"+symbol+"_"+interval
        try:
            cached=read_shared_cache(cache_key)
            payload=cached.get("payload") if isinstance(cached,dict) else None
            stamp=str(cached.get("updated_at") or "") if isinstance(cached,dict) else ""
            try: fresh=(datetime.now(timezone.utc)-datetime.fromisoformat(stamp.replace("Z","+00:00"))).total_seconds()<55
            except Exception: fresh=False
            if fresh and isinstance(payload,dict):
                response=dict(payload); response["cached"]=True; json_response(self,200,response); return
            if not claim_shared_cache_refresh(cache_key,lock_seconds=25):
                if isinstance(payload,dict):
                    response=dict(payload); response["cached"]=True; json_response(self,200,response); return
                json_response(self,503,{"error":"Candles are refreshing. Retry shortly."}); return
            try:
                granted_credits = claim_provider_credits(1, credit_limit=8, window_seconds=60)
                if granted_credits < 1:
                    release_shared_cache_refresh(cache_key)
                    if isinstance(payload, dict) and isinstance(payload.get("candles"), list):
                        response = dict(payload)
                        response["cached"] = True
                        response["stale"] = True
                        response["rate_limited"] = True
                        json_response(self, 200, response)
                    else:
                        json_response(self, 429, {
                            "error": "The shared Twelve Data credit allowance is used for this minute. Retry after the next minute.",
                            "retry_after_seconds": 60,
                        })
                    return
                result=_candles(symbol,INTERVALS[interval],os.environ.get("TWELVE_DATA_API_KEY","").strip())
                write_shared_cache(cache_key,result)
                json_response(self,200,result)
            except Exception:
                release_shared_cache_refresh(cache_key); raise
        except (ValueError,RuntimeError,SharedCacheError) as exc: json_response(self,503,{"error":str(exc)[:180]})
        except Exception as exc:
            print("market_candles error:",str(exc)[:180]); json_response(self,500,{"error":"Market chart could not be loaded."})
    def log_message(self,*_args): return
