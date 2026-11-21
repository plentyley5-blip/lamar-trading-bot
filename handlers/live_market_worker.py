"""Quota-controlled Twelve Data Live Market technical alert worker."""
import hashlib, hmac, json, math, os, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from analysis_common import json_response
from api.lm_push import notify_user_ids
from api.user_security import get_supabase_service_key, get_supabase_url
from handlers.market_candles import SYMBOLS, _candles
from service_cache import claim_provider_credits

INSTRUMENTS = [
 "EURUSD","GBPUSD","USDJPY","USDCHF","USDCAD","AUDUSD","NZDUSD",
 "EURGBP","EURJPY","EURCHF","EURAUD","EURCAD","EURNZD","GBPJPY","GBPCHF",
 "GBPAUD","GBPCAD","GBPNZD","AUDJPY","AUDCAD","AUDCHF","AUDNZD","CADJPY",
 "CADCHF","CHFJPY","NZDJPY","NZDCAD","NZDCHF","XAUUSD","XAGUSD","BTCUSD","ETHUSD"
]
INTERVALS=["5min","15min","1h","4h"]
FREE_INSTRUMENTS={"EURUSD","GBPUSD","USDJPY"}
FREE_INTERVALS={"15min","1h"}
FREE_DAILY_LIMITS={"technical":3,"opportunity":1}
DAILY_SCAN_CREDIT_LIMIT=200
TIMEFRAME_MINUTES={"5min":5,"15min":15,"1h":60,"4h":240}
INTERVAL_LABELS={"5min":"M5","15min":"M15","1h":"H1","4h":"H4"}

def _base(): return get_supabase_url().rstrip("/")
def _headers(prefer=None):
    key=get_supabase_service_key()
    h={"apikey":key,"Authorization":"Bearer "+key,"Accept":"application/json","Content-Type":"application/json"}
    if prefer: h["Prefer"]=prefer
    return h
def _request(method,path,payload=None,prefer=None,timeout=12):
    data=None if payload is None else json.dumps(payload,separators=(",",":")).encode()
    req=urllib.request.Request(_base()+path,data=data,headers=_headers(prefer),method=method)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            raw=r.read().decode("utf-8","replace")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Supabase worker storage returned HTTP %s"%exc.code) from exc
def _rpc(name,payload): return _request("POST","/rest/v1/rpc/"+name,payload,timeout=12)

def _provider_time(value):
    raw=str(value or "").strip()
    if not raw: return None
    try: dt=datetime.fromisoformat(raw.replace("Z","+00:00"))
    except ValueError:
        dt=None
        for fmt in ("%Y-%m-%d %H:%M:%S","%Y-%m-%d %H:%M"):
            try: dt=datetime.strptime(raw,fmt).replace(tzinfo=timezone.utc); break
            except ValueError: pass
    if dt is None: return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

def _ema(values,period):
    if len(values)<period: return []
    alpha=2.0/(period+1.0); current=sum(values[:period])/period
    out=[None]*(period-1)+[current]
    for value in values[period:]:
        current=alpha*value+(1-alpha)*current; out.append(current)
    return out
def _rsi(values,period=14):
    if len(values)<=period: return None
    changes=[values[i]-values[i-1] for i in range(1,len(values))]
    gains=[max(x,0.0) for x in changes]; losses=[max(-x,0.0) for x in changes]
    up=sum(gains[:period])/period; down=sum(losses[:period])/period
    for i in range(period,len(changes)):
        up=(up*(period-1)+gains[i])/period; down=(down*(period-1)+losses[i])/period
    if down<=0: return 100.0 if up>0 else 50.0
    return 100.0-100.0/(1.0+up/down)

def _evaluate(symbol,interval,raw):
    if not isinstance(raw,list) or len(raw)<70: return None
    rows=[]
    for x in raw:
        if not isinstance(x,dict): continue
        stamp=_provider_time(x.get("time"))
        try: op,hi,lo,cl=[float(x[k]) for k in ("open","high","low","close")]
        except (KeyError,TypeError,ValueError): continue
        if stamp is None or not all(math.isfinite(v) and v>0 for v in (op,hi,lo,cl)) or hi<max(op,cl,lo) or lo>min(op,cl,hi): continue
        rows.append({"time":stamp,"open":op,"high":hi,"low":lo,"close":cl})
    rows.sort(key=lambda x:x["time"])
    if len(rows)<70: return None
    now=datetime.now(timezone.utc); minutes=TIMEFRAME_MINUTES[interval]
    # Twelve Data timestamps mark candle starts. Never evaluate a forming candle.
    closed=[x for x in rows if x["time"]+timedelta(minutes=minutes)<=now+timedelta(seconds=20)]
    if len(closed)<70: return None
    last=closed[-1]
    age=(now-(last["time"]+timedelta(minutes=minutes))).total_seconds()
    if age < -30 or age > max(2*minutes*60,1200): return None
    sample=closed[-100:]; closes=[x["close"] for x in sample]
    highs=[x["high"] for x in sample]; lows=[x["low"] for x in sample]
    e20=_ema(closes,20); e50=_ema(closes,50); rsi=_rsi(closes)
    if not e20 or not e50 or e20[-2] is None or rsi is None: return None
    tr=[]
    for i in range(1,len(sample)):
        a,b=sample[i],sample[i-1]
        tr.append(max(a["high"]-a["low"],abs(a["high"]-b["close"]),abs(a["low"]-b["close"])))
    atr=sum(tr[-14:])/14.0
    if not math.isfinite(atr) or atr<=0 or last["high"]-last["low"]>atr*2.5: return None
    up=closes[-1]>e20[-1]>e50[-1] and e20[-1]>e20[-2]
    down=closes[-1]<e20[-1]<e50[-1] and e20[-1]<e20[-2]
    ph=max(highs[-21:-1]); pl=min(lows[-21:-1])
    bull=up and closes[-1]>ph and 54<=rsi<=72
    bear=down and closes[-1]<pl and 28<=rsi<=46
    direction=kind=rationale=""; strength=0
    if bull or bear:
        direction="BUY" if bull else "SELL"; kind="opportunity"; strength=83 if (rsi>=58 and bull) or (rsi<=42 and bear) else 78
        rationale=("Closed above the previous 20-candle high with EMA20 above EMA50 and rising momentum." if bull else "Closed below the previous 20-candle low with EMA20 below EMA50 and falling momentum.")
    else:
        cross_up=e20[-2]<=e50[-2] and e20[-1]>e50[-1] and closes[-1]>e20[-1] and rsi>=52
        cross_dn=e20[-2]>=e50[-2] and e20[-1]<e50[-1] and closes[-1]<e20[-1] and rsi<=48
        reclaim_up=closes[-2]<=e20[-2] and closes[-1]>e20[-1] and e20[-1]>e50[-1] and 52<=rsi<=68
        reclaim_dn=closes[-2]>=e20[-2] and closes[-1]<e20[-1] and e20[-1]<e50[-1] and 32<=rsi<=48
        if cross_up or reclaim_up:
            direction,kind,strength="BUY","technical",68; rationale="Bullish EMA trend confirmation or reclaim of EMA20 on a closed candle."
        elif cross_dn or reclaim_dn:
            direction,kind,strength="SELL","technical",68; rationale="Bearish EMA trend confirmation or loss of EMA20 on a closed candle."
    if not direction: return None
    return {"instrument":symbol,"timeframe":interval,"kind":kind,"direction":direction,"strength":strength,
            "price":round(last["close"],8),"candle_time":last["time"].isoformat(),"rsi14":round(rsi,2),
            "ema20":round(e20[-1],8),"ema50":round(e50[-1],8),"atr14":round(atr,8),"rationale":rationale}

def _insert_alert(s):
    key=hashlib.sha256("|".join((s["instrument"],s["timeframe"],s["candle_time"],s["kind"],s["direction"])).encode()).hexdigest()
    label=INTERVAL_LABELS[s["timeframe"]]
    title="%s %s %s"%(s["instrument"],s["direction"],"OPPORTUNITY" if s["kind"]=="opportunity" else "TECHNICAL ALERT")
    body="%s %s closed at %s. RSI(14): %s. %s This is a market-condition alert, not an automatic trade instruction."%(label,s["direction"],s["price"],s["rsi14"],s["rationale"])
    payload={"alert_key":key,"instrument":s["instrument"],"timeframe":s["timeframe"],"kind":s["kind"],
             "direction":s["direction"],"title":title[:100],"body":body[:500],"strength":s["strength"],
             "price":str(s["price"]),"candle_time":s["candle_time"],
             "details":{k:s[k] for k in ("rsi14","ema20","ema50","atr14","rationale")}}
    rows=_request("POST","/rest/v1/lm_live_market_alerts?on_conflict=alert_key",payload,
                  prefer="resolution=ignore-duplicates,return=representation")
    return rows[0] if isinstance(rows,list) and rows else None

def _recipient_groups():
    now=datetime.now(timezone.utc).isoformat()
    q=urllib.parse.urlencode({"select":"user_id","status":"eq.active","expires_at":"gt."+now,"limit":"5000"})
    subs=_request("GET","/rest/v1/subscriptions?"+q) or []
    paid={str(x.get("user_id","")).strip() for x in subs if x.get("user_id")}
    owner=os.environ.get("OWNER_EMAIL","").strip().lower()
    if owner:
        q=urllib.parse.urlencode({"select":"id","email":"eq."+owner,"limit":"5"})
        profiles=_request("GET","/rest/v1/profiles?"+q) or []
        paid.update(str(x.get("id","")).strip() for x in profiles if x.get("id"))
    q=urllib.parse.urlencode({"select":"user_id","active":"eq.true","limit":"5000"})
    tokens=_request("GET","/rest/v1/device_tokens?"+q) or []
    devices={str(x.get("user_id","")).strip() for x in tokens if x.get("user_id")}
    return sorted(devices&paid),sorted(devices-paid)

def _claim_delivery(alert_id,user,tier,kind):
    r=_rpc("claim_lm_live_market_delivery",{"p_alert_id":alert_id,"p_user_id":user,"p_plan_tier":tier,
       "p_technical_daily_limit":3,"p_opportunity_daily_limit":1})
    if isinstance(r,list) and r: r=r[0]
    return bool(r)

def _deliver(alert,s,paid_users,free_users):
    recipients=[(u,"paid") for u in paid_users]
    if s["instrument"] in FREE_INSTRUMENTS and s["timeframe"] in FREE_INTERVALS:
        recipients += [(u,"free") for u in free_users]
    claimed=[]
    for user,tier in recipients:
        try:
            if _claim_delivery(alert["id"],user,tier,s["kind"]): claimed.append((user,tier))
        except Exception as exc: print("live market delivery claim error:",type(exc).__name__)
    if not claimed: return {"eligible":0,"accepted":0}
    result=notify_user_ids([u for u,_ in claimed],alert["title"],alert["body"],{
        "type":"live_market_alert","alert_id":str(alert["id"]),"instrument":s["instrument"],
        "timeframe":s["timeframe"],"alert_kind":s["kind"],"direction":s["direction"],
        "price":str(s["price"]),"candle_time":s["candle_time"]})
    status="sent" if int(result.get("sent",0))>0 else "failed"
    for user,_tier in claimed:
        q=urllib.parse.urlencode({"alert_id":"eq."+str(alert["id"]),"user_id":"eq."+user})
        _request("PATCH","/rest/v1/lm_live_market_deliveries?"+q,
                 {"status":status,"sent_at":datetime.now(timezone.utc).isoformat() if status=="sent" else None},
                 prefer="return=minimal")
    return {"eligible":len(claimed),"accepted":int(result.get("sent",0)),"failed":int(result.get("failed",0))}

def _process_one():
    value=_rpc("claim_lm_live_market_scan_slot",{"p_daily_limit":DAILY_SCAN_CREDIT_LIMIT})
    if isinstance(value,list) and value: value=value[0]
    try: index=int(value)
    except (TypeError,ValueError): index=-1
    if index<0: return {"scanned":False,"reason":"daily_worker_budget_reached"}
    combos=[(symbol,interval) for symbol in INSTRUMENTS for interval in INTERVALS]
    symbol,interval=combos[index%len(combos)]
    if not SYMBOLS.get(symbol): return {"scanned":True,"instrument":symbol,"timeframe":interval,"reason":"provider_symbol_unavailable"}
    if claim_provider_credits(1,credit_limit=8,window_seconds=60)<1:
        return {"scanned":False,"reason":"shared_minute_credit_budget_reached"}
    data=_candles(symbol,interval,os.environ.get("TWELVE_DATA_API_KEY","").strip())
    signal=_evaluate(symbol,interval,data.get("candles"))
    if not signal: return {"scanned":True,"instrument":symbol,"timeframe":interval,"alert":False}
    alert=_insert_alert(signal)
    if not alert: return {"scanned":True,"instrument":symbol,"timeframe":interval,"alert":False,"reason":"duplicate"}
    paid,free=_recipient_groups()
    delivery=_deliver(alert,signal,paid,free)
    return {"scanned":True,"instrument":symbol,"timeframe":interval,"alert":True,"kind":signal["kind"],
            "direction":signal["direction"],"eligible_users":delivery["eligible"],"pushes_accepted":delivery["accepted"]}

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self): json_response(self,204,{})
    def do_POST(self):
        secret=os.environ.get("LM_NEWS_WORKER_SECRET","").strip()
        timestamp=str(self.headers.get("X-LM-Timestamp","")).strip()
        signature=str(self.headers.get("X-LM-Signature","")).strip().lower()
        try: ts=int(timestamp)
        except (TypeError,ValueError): ts=0
        expected=hmac.new(secret.encode(),timestamp.encode(),hashlib.sha256).hexdigest() if secret else ""
        if not secret or not ts or abs(int(time.time())-ts)>180 or not hmac.compare_digest(signature,expected):
            json_response(self,403,{"error":"Worker authentication failed."}); return
        try: json_response(self,200,{"ok":True,**_process_one()})
        except Exception as exc:
            print("live_market_worker error:",type(exc).__name__)
            json_response(self,503,{"error":"Live Market scanner could not complete this cycle."})
    def do_GET(self): json_response(self,405,{"error":"Method not allowed."})
    def log_message(self,*_args): return
