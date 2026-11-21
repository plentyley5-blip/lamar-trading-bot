"""Authenticated Live Market alert history for LM Analyzer."""
import json, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler
from analysis_common import json_response
from api.user_security import extract_bearer_token, verify_access_token, is_owner_user
from handlers.subscription_access import has_active_subscription

def _get(path):
    from api.user_security import get_supabase_service_key, get_supabase_url
    key=get_supabase_service_key()
    req=urllib.request.Request(get_supabase_url().rstrip("/") + path,
       headers={"apikey":key,"Authorization":"Bearer "+key,"Accept":"application/json"},method="GET")
    with urllib.request.urlopen(req,timeout=12) as response:
        raw=response.read().decode("utf-8","replace")
        return json.loads(raw) if raw else []

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self): json_response(self,204,{})
    def do_GET(self):
        try:
            user=verify_access_token(extract_bearer_token(self))
            user_id=str(user.get("id","")).strip()
            if not user_id: raise ValueError("A valid signed-in session is required.")
        except ValueError as exc:
            json_response(self,401,{"error":str(exc)}); return
        except Exception:
            json_response(self,503,{"error":"Live Market history is temporarily unavailable."}); return
        try:
            from urllib.parse import parse_qs,urlsplit
            params=parse_qs(urlsplit(self.path).query)
            try: limit=max(1,min(50,int((params.get("limit") or ["30"])[0])))
            except (TypeError,ValueError): limit=30
            q=urllib.parse.urlencode({"user_id":"eq."+user_id,
               "select":"created_at,plan_tier,status,alert:lm_live_market_alerts(id,instrument,timeframe,kind,direction,title,body,strength,price,candle_time,created_at,details)",
               "order":"created_at.desc","limit":str(limit)})
            rows=_get("/rest/v1/lm_live_market_deliveries?"+q)
            items=[]
            for row in rows if isinstance(rows,list) else []:
                alert=row.get("alert") if isinstance(row,dict) else None
                if isinstance(alert,dict):
                    items.append({**alert,"delivery_status":row.get("status","sent"),
                                  "plan_tier":row.get("plan_tier","free"),"delivered_at":row.get("created_at","")})
            paid=has_active_subscription(user_id) or is_owner_user(user)
            json_response(self,200,{"ok":True,"provider":"Twelve Data","plan_tier":"paid" if paid else "free",
                "free_instruments":["EURUSD","GBPUSD","USDJPY"],"free_timeframes":["15min","1h"],"items":items})
        except Exception as exc:
            print("live_market history error:",type(exc).__name__)
            json_response(self,503,{"error":"Live Market history could not be loaded."})
    def do_POST(self): json_response(self,405,{"error":"Method not allowed."})
    def log_message(self,*_args): return
