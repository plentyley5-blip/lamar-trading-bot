import json, time, urllib.error, urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE = {"expires": 0.0, "events": []}

def response(h, status, payload):
    body=json.dumps(payload,separators=(",",":")).encode()
    h.send_response(status); h.send_header("Access-Control-Allow-Origin","*"); h.send_header("Access-Control-Allow-Headers","Content-Type"); h.send_header("Access-Control-Allow-Methods","GET, OPTIONS"); h.send_header("Content-Type","application/json; charset=utf-8"); h.send_header("Cache-Control","public, max-age=300"); h.end_headers(); h.wfile.write(body)

def clean(v): return "" if v is None else str(v).strip()

def fetch():
    now=time.time()
    if CACHE["expires"]>now: return CACHE["events"]
    req=urllib.request.Request(CALENDAR_URL,headers={"User-Agent":"LM-Analyzer/1.0","Accept":"application/json"})
    try:
        with urllib.request.urlopen(req,timeout=15) as r: raw=r.read().decode("utf-8","replace")
    except Exception as e: raise RuntimeError("Economic calendar is temporarily unavailable.") from e
    try: data=json.loads(raw)
    except Exception as e: raise RuntimeError("Economic calendar returned invalid data.") from e
    out=[]
    if not isinstance(data,list): raise RuntimeError("Economic calendar returned an unexpected format.")
    for x in data:
        if not isinstance(x,dict) or clean(x.get("impact")).lower()!="high": continue
        title=clean(x.get("title") or x.get("event")); currency=clean(x.get("country") or x.get("currency"))
        if not title or not currency: continue
        rawdate=clean(x.get("date") or x.get("event_dt")); date=""; tm=""
        if rawdate:
            try:
                dt=datetime.fromisoformat(rawdate.replace("Z","+00:00")); date=dt.strftime("%a %d %b %Y"); tm=dt.strftime("%H:%M")
            except Exception: date=rawdate
        out.append({"currency":currency,"title":title,"impact":"High","date":date,"time":tm,"actual":clean(x.get("actual")),"forecast":clean(x.get("forecast")),"previous":clean(x.get("previous"))})
    CACHE["events"]=out[:20]; CACHE["expires"]=now+600
    return CACHE["events"]

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self): response(self,204,{})
    def do_GET(self):
        try: response(self,200,{"source":"Forex Factory","updated_at":int(time.time()),"events":fetch()})
        except RuntimeError as e: response(self,502,{"error":str(e)})
        except Exception as e: response(self,500,{"error":"News backend error: "+str(e)})
