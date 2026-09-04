
import sys, time, json, os
sys.path.insert(0, '/opt/data/projects/cozyvtt-mcp')
from auth import AuthManager
from ws_listener import WSListener

auth = AuthManager(os.environ['COZYVTT_URL'], os.environ['COZYVTT_EMAIL'], os.environ['COZYVTT_PASSWORD'])
auth.login()
print("login ok")

ws = WSListener(
    base_url=os.environ['COZYVTT_URL'],
    campaign_id=os.environ['COZYVTT_CAMPAIGN_ID'],
    cookie_getter=auth.cookie_header,
)
ws.start()
for i in range(20):
    if ws.authenticated:
        break
    time.sleep(0.5)
print("authenticated:", ws.authenticated, "last_error:", ws.last_error)
if ws.authenticated:
    ws.emit("dice.roll", {"expression": "1d100", "isSecret": False})
    time.sleep(2)
    ev = ws.latest("dice.rolled")
    print("dice.rolled event:", json.dumps(ev, ensure_ascii=False)[:300] if ev else None)
ws.stop()
