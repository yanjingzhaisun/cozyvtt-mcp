
import sys, time, os
sys.path.insert(0, '/opt/data/projects/cozyvtt-mcp')
from auth import AuthManager
import socketio, logging
logging.basicConfig(level=logging.INFO)
for lg in ('socketio.client','engineio.client'):
    logging.getLogger(lg).setLevel(logging.INFO)

auth = AuthManager(os.environ['COZYVTT_URL'], os.environ['COZYVTT_EMAIL'], os.environ['COZYVTT_PASSWORD'])
auth.login()
print("login ok, cookie:", auth.cookie_header()[:40], "...")

sio = socketio.Client(reconnection=False, logger=True, engineio_logger=False)

@sio.event
def connect():
    print(">> connect, emit authenticate")
    sio.emit("authenticate", {"campaignId": os.environ['COZYVTT_CAMPAIGN_ID']})

@sio.event
def authenticated(data=None):
    print(">> AUTHENTICATED:", data)

@sio.event
def error(data=None):
    print(">> ERROR EVENT:", data)

def catchall(data=None):
    print(">> catchall:", data)

# 常见可能事件名都挂上
for ev in ("authenticated","error","auth.error","unauthorized","user.joined","campaign.state"):
    try: sio.on(ev, catchall)
    except Exception: pass

sio.connect(os.environ['COZYVTT_URL'], headers={"Cookie": auth.cookie_header()}, wait_timeout=15)
time.sleep(6)
print("done")
sio.disconnect()
