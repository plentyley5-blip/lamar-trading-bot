import os, json, base64, urllib.request
from http.server import BaseHTTPRequestHandler

SYSTEM='''You are the vision analysis engine for LAMAR TRADING BOT. Analyze the supplied 4H and 15M trading charts. Return ONLY valid JSON. Be conservative: if chart quality, price labels, structure, or setup are insufficient, signal NO TRADE and explain why. Confidence is an analysis-confidence score, not a guaranteed probability of profit. Never invent exact prices that cannot be read from the images. Use the visible market structure, support/resistance, price action, liquidity behavior, Fibonacci/premium-discount context and multi-timeframe confirmation internally, but do not present strategy names as UI labels. Required JSON keys: signal (BUY|SELL|NO TRADE), confidence (0-100), instrument, higher_timeframe_context, lower_timeframe_confirmation, entry, stop_loss, take_profit_1, take_profit_2, risk_reward, data_analysis, explanation, warnings.'''

def response(handler, code, obj):
    raw=json.dumps(obj).encode(); handler.send_response(code); handler.send_header('Content-Type','application/json'); handler.send_header('Access-Control-Allow-Origin','*'); handler.send_header('Access-Control-Allow-Headers','Content-Type'); handler.send_header('Access-Control-Allow-Methods','POST, OPTIONS'); handler.end_headers(); handler.wfile.write(raw)

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self): response(self,204,{})
    def do_POST(self):
        if self.path!='/api/analyze': return response(self,404,{'error':'Not found'})
        key=os.getenv('OPENAI_API_KEY')
        if not key: return response(self,500,{'error':'OPENAI_API_KEY is not configured on the server'})
        try:
            n=int(self.headers.get('Content-Length','0')); data=json.loads(self.rfile.read(n))
            inst=data.get('instrument','Unknown'); h=data.get('higher_timeframe_image'); m=data.get('lower_timeframe_image')
            if not h or not m: return response(self,400,{'error':'Both 4H and 15M chart images are required'})
            content=[{'type':'input_text','text':f'Instrument: {inst}. Analyze the two charts. The first image is 4H and the second is 15M.'},{'type':'input_image','image_url':h},{'type':'input_image','image_url':m}]
            payload={'model':'gpt-5.6-luna','input':[{'role':'system','content':SYSTEM},{'role':'user','content':content}], 'text':{'format':{'type':'json_object'}}}
            req=urllib.request.Request('https://api.openai.com/v1/responses',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=120) as r: api=json.loads(r.read())
            txt=api.get('output_text')
            if not txt:
                for item in api.get('output',[]):
                    for c in item.get('content',[]):
                        if c.get('type')=='output_text': txt=c.get('text'); break
            if not txt: raise RuntimeError('No analysis returned by model')
            return response(self,200,json.loads(txt))
        except Exception as e: return response(self,500,{'error':str(e)})
