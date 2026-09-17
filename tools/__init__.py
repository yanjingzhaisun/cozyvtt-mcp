"""cozyvtt-mcp — tools package. Shared Ctx and register_all."""
from __future__ import annotations

import logging
import os
import threading
import time

from auth import AuthManager
from client import CozyClient
from ws_listener import WSListener

log = logging.getLogger("cozyvtt.tools")

DICE_MIN_INTERVAL = 2.1  # Dice WS limit: 30/min; minimum client interval: 2.1s

# Upstream GameSystem enum (backend/prisma/schema.prisma)
SYSTEM_DND5E = "DND_5E"
SYSTEM_PF2E = "PATHFINDER_2E"
SYSTEM_SR6 = "SHADOWRUN_6E"
SYSTEM_COC7E = "CALL_OF_CTHULHU_7E"
KNOWN_SYSTEMS = (SYSTEM_DND5E, SYSTEM_PF2E, SYSTEM_SR6, SYSTEM_COC7E)
# Systems where initiative.roll actually rolls dice (CoC7e sorts by DEX instead)
INITIATIVE_ROLL_SYSTEMS = (SYSTEM_DND5E, SYSTEM_PF2E, SYSTEM_SR6)
HITDICE_SYSTEMS = (SYSTEM_DND5E,)
E_OLD = "This CozyVTT instance does not provide this feature; upgrade to a supported version and retry."
E_RESOURCE = "Resource not found or inaccessible to the current account (HTTP 404): "
E_PENDING = "Operation sent; the result is not yet confirmed. Read events or state before taking further action; do not repeat the operation."
E_CURSOR = "This instance does not support reliable cursor pagination for history; only the latest page can be read."

_SYSTEM_UNSET = object()  # get_system cache sentinel (None is valid for flexible campaigns)


def require_system(ctx, allowed: tuple, feature: str) -> None:
    """Raise ValueError if the campaign gameSystem is not allowed (wrap returns {ok:False}).
    Campaigns with no gameSystem (flexible) also fail this gate: the feature requires system semantics."""
    system = ctx.get_system()
    if system not in allowed:
        raise ValueError(
            f"{feature} is available only for {'/'.join(allowed)} campaigns; "
            f"current campaign system: {system or 'unset (flexible)'}")


class Ctx:
    """Shared tool context: REST client, auth, WS listener, and campaign ID."""

    def __init__(self, client: CozyClient, auth: AuthManager, ws: WSListener, campaign_id: str):
        self.client = client
        self.auth = auth
        self.ws = ws
        self.campaign_id = campaign_id
        self._ws_started = False
        self._ws_lock = threading.Lock()
        self._dice_lock = threading.Lock()
        self._last_dice_ts = 0.0
        self._character_lock = threading.Lock()
        self._saved_roll_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache_generation = 0
        self._system = _SYSTEM_UNSET
        self._role = _SYSTEM_UNSET
        self._cursor_supported = None
        if client is not None:
            client.on_unauthorized = self.on_unauthorized
        if ws is not None:
            ws.on_context_invalidated = self.invalidate_campaign

    @classmethod
    def from_env(cls) -> "Ctx":
        base_url = os.environ.get("COZYVTT_URL", "http://localhost:8899")
        email = os.environ.get("COZYVTT_EMAIL", "")
        password = os.environ.get("COZYVTT_PASSWORD", "")
        campaign_id = os.environ.get("COZYVTT_CAMPAIGN_ID", "")
        if not email or not password:
            raise RuntimeError("Missing COZYVTT_EMAIL / COZYVTT_PASSWORD environment variables")
        if not campaign_id:
            raise RuntimeError("Missing COZYVTT_CAMPAIGN_ID environment variable")
        auth = AuthManager(base_url, email, password)
        client = CozyClient(base_url, auth)
        ws = WSListener(base_url, campaign_id, auth.cookie_header, auth_refresh=auth.relogin)
        return cls(client, auth, ws, campaign_id)

    # ---- Campaign system (lazy cache; None = unset/flexible) ----

    def get_system(self):
        with self._cache_lock:
            system = self._system
        if system is _SYSTEM_UNSET:
            return self.read_campaign().get("gameSystem")
        return system

    def invalidate_campaign(self):
        with self._cache_lock:
            self._cache_generation += 1
            self._system = self._role = _SYSTEM_UNSET
            self._cursor_supported = None

    def on_unauthorized(self):
        self.invalidate_campaign()
        # Upstream session revocation does not disconnect existing WS; invalidate auth and reconnect with a fresh Cookie.
        if self.ws is not None and hasattr(self.ws, "invalidate_auth"):
            self.ws.invalidate_auth("REST returned 401; previous WS authentication has been invalidated")

    def role_from_campaign(self, camp):
        role = camp.get("userRole")
        if role is None:
            me = (self.auth.user or {}).get("id")
            role = next((m.get("role") for m in camp.get("memberships", [])
                         if me and m.get("userId") == me), None)
        return role  # ownerId is not evidence of the DM role

    def read_campaign(self):
        with self._cache_lock:
            generation = self._cache_generation
        camp = self.client.get(f"/api/campaigns/{self.campaign_id}").get("campaign", {})
        with self._cache_lock:
            if generation == self._cache_generation:
                self._system = camp.get("gameSystem")
                self._role = self.role_from_campaign(camp)
        return camp

    def cached_role(self):
        with self._cache_lock:
            return None if self._role is _SYSTEM_UNSET else self._role

    def get_role(self):
        # Refresh before writing to avoid stale roles when a transfer broadcast was missed.
        return self.role_from_campaign(self.read_campaign())

    # ---- Lazy WS startup ----

    def ensure_ws(self) -> None:
        with self._ws_lock:
            self._ws_started = self.ws.connected and self.ws.authenticated
            if not self._ws_started:
                self.ws.start()
                self._ws_started = self.ws.connected and self.ws.authenticated

    def close(self) -> None:
        try:
            self.ws.stop()
        finally:
            self.auth.stop()

    # ---- Dice throttling (queue rather than reject) ----

    def send_dice(self, payload: dict) -> dict:
        with self._dice_lock:
            wait = DICE_MIN_INTERVAL - (time.monotonic() - self._last_dice_ts)
            if wait > 0:
                time.sleep(wait)
            result = self.ws.emit("dice.roll", payload)
            self._last_dice_ts = time.monotonic()
            return result


class PartialFailure(Exception):
    """An upstream side effect already occurred; include recovery details to prevent duplicate creation."""
    def __init__(self, message: str, data: dict):
        super().__init__(message)
        self.data = data


def _ok(data=None):
    return {"ok": True, "data": data}


def _err(msg: str):
    return {"ok": False, "error": msg}


def wrap(fn):
    """Convert tool-body exceptions to {ok,error}; FastMCP validates argument schemas before calling.
    functools.wraps preserves the signature for FastMCP schema generation."""
    import functools
    from client import ApiError

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        try:
            return _ok(fn(*args, **kwargs))
        except PartialFailure as e:
            return {"ok": False, "error": str(e), "data": e.data}
        except ApiError as e:
            if e.status == 404:
                message = E_OLD if e.message == "The requested resource does not exist" else E_RESOURCE + e.message
            else:
                message = f"HTTP {e.status}: {e.message}" if e.status else e.message
            result = _err(message)
            result["data"] = {"status": e.status, "upstream": e.details or {"message": e.message}}
            return result
        except Exception as e:
            log.exception("Tool call failed: %s", fn.__name__)
            return _err(f"{type(e).__name__}: {e}")
    return inner


def register_all(mcp, get_ctx) -> None:
    from tools import read_tools, write_tools, document_tools, campaign_tools
    read_tools.register(mcp, get_ctx)
    write_tools.register(mcp, get_ctx)
    document_tools.register(mcp, get_ctx)
    campaign_tools.register(mcp, get_ctx)
