"""
AI-Powered Predictive Dialer — FastAPI application
────────────────────────────────────────────────────
REST API + WebSocket dashboard backed by FreeSWITCH ESL and Claude AI.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from agent_manager import AgentManager
from ai_analyzer import AIAnalyzer
from auth import (
    create_token, decode_token, get_payload, require_admin, require_any,
    hash_password, verify_password,
)
from call_manager import CallManager
from config import settings
from dialer_engine import DialerEngine
from esl_client import ESLClient
from inbound_handler import InboundRouter, normalize_number
import storage
from models import (
    Agent,
    AgentCreate,
    AgentDisposition,
    AgentLogin,
    Call,
    CallStatus,
    Campaign,
    CampaignCreate,
    CampaignStatus,
    Contact,
    DID,
    Subscription,
    User,
    UserCreate,
    UserRole,
    WSMessage,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Singletons ─────────────────────────────────────────────────────────────────

esl = ESLClient(settings.FS_HOST, settings.FS_PORT, settings.FS_PASSWORD)
agent_mgr = AgentManager(wrap_up_seconds=settings.WRAP_UP_TIME)
call_mgr = CallManager()
ai = AIAnalyzer(api_key=settings.ANTHROPIC_API_KEY, model=settings.CLAUDE_MODEL)

campaigns: Dict[str, Campaign] = {}
engines: Dict[str, DialerEngine] = {}
admin_ws_clients: List[WebSocket] = []
users: Dict[str, User] = {}              # username → User
dids: List[DID] = []
subscriptions: Dict[str, Subscription] = {}  # username → Subscription


# ── Inbound calling (isolated — see inbound_handler.py) ─────────────────────────
def _inbound_owner_extension(dialed_digits: str):
    """Given a dialed DID (bare digits), return (extension, username) of the owner.

    Read-only over existing state; does not touch outbound logic. An active
    subscription takes priority, then raw DID ownership as a fallback. Returns
    None if the number is unowned or the owner has no extension.
    """
    for sub in subscriptions.values():
        if sub.is_active and normalize_number(sub.did_number) == dialed_digits:
            user = users.get(sub.username)
            if user and user.extension:
                return (user.extension, user.username)
    for did in dids:
        if did.owner_username and normalize_number(did.number) == dialed_digits:
            user = users.get(did.owner_username)
            if user and user.extension:
                return (user.extension, user.username)
    return None


inbound_router = InboundRouter(esl, _inbound_owner_extension)


def _save() -> None:
    storage.save(agent_mgr.list_all(), campaigns, call_mgr.all_calls(),
                 list(users.values()), dids, list(subscriptions.values()))


def _load_persisted() -> None:
    data = storage.load()
    if data:
        try:
            # Load campaigns, calls, users, DIDs first — agents are rebuilt from users below
            for c in data.get("campaigns", []):
                campaign = Campaign(**c)
                campaigns[campaign.id] = campaign
            for c in data.get("calls", []):
                call_mgr.add(Call(**c))
            for u in data.get("users", []):
                user = User(**u)
                users[user.username] = user
            for d in data.get("dids", []):
                dids.append(DID(**d))
            for s in data.get("subscriptions", []):
                sub = Subscription(**s)
                subscriptions[sub.username] = sub
            logger.info("Loaded persisted state: %d campaigns, %d calls, %d users, %d DIDs, %d subs",
                        len(data.get("campaigns", [])), len(data.get("calls", [])),
                        len(data.get("users", [])), len(data.get("dids", [])),
                        len(data.get("subscriptions", [])))
        except Exception as exc:
            logger.error("Failed to restore persisted state: %s", exc)

    # Seed default admin if no users exist
    if not users:
        admin = User(
            username=settings.ADMIN_USERNAME,
            password_hash=hash_password(settings.ADMIN_PASSWORD),
            role=UserRole.SUPERADMIN,
        )
        users[admin.username] = admin
        logger.info("Created default superadmin: %s", admin.username)

    # Seed configured DIDs if none exist
    if not dids:
        for number in [
            "16823109571", "19284662191", "15594609869",
            "17542038050", "18705690442",
        ]:
            dids.append(DID(number=number, label=f"DID {number[-4:]}"))
        logger.info("Seeded %d default DIDs", len(dids))

    # ── Agent reconciliation (runs every startup) ──────────────────────────────
    # Agents are ONLY created from users — any orphaned agents are purged.
    # This also migrates old manually-created agents to the user-linked system.
    for user in users.values():
        if user.extension and not user.agent_id:
            # User has an extension but no linked agent → create one now
            agent = Agent(name=user.username, extension=user.extension,
                          phone_type=user.phone_type)
            agent_mgr.register(agent)
            user.agent_id = agent.id
            logger.info("Auto-created agent '%s' ext=%s for user '%s'",
                        user.username, user.extension, user.username)
        elif user.agent_id:
            # Re-register the agent (restore from data); carry phone_type from user
            agent = Agent(
                id=user.agent_id,
                name=user.username,
                extension=user.extension or "",
                phone_type=user.phone_type,
            )
            agent_mgr.register(agent)

    logger.info("Agents after reconciliation: %d", len(agent_mgr.list_all()))
    _save()


# ── WebSocket broadcast ────────────────────────────────────────────────────────

def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


async def broadcast(event_type: str, data) -> None:
    msg = json.dumps({"type": event_type, "data": data}, default=_json_default)
    dead = []
    for ws in admin_ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        admin_ws_clients.remove(ws)


async def on_dialer_event(event_type: str, data: dict) -> None:
    await broadcast(event_type, data)
    if event_type in ("call_ended", "campaign_completed", "campaign_stopped", "hopper_advanced"):
        _save()
    if event_type == "call_ended":
        call_id = data.get("id")
        call = call_mgr.get(call_id)
        if call and call.agent_id and call.duration and settings.ANTHROPIC_API_KEY:
            asyncio.create_task(_run_ai_analysis(call_id))


async def _run_ai_analysis(call_id: str) -> None:
    call = call_mgr.get(call_id)
    if not call:
        return
    try:
        summary, sentiment = await ai.analyze_call(
            contact_name=call.contact.name,
            contact_phone=call.contact.phone,
            duration_seconds=call.duration or 0,
            disposition=call.disposition,
        )
        call_mgr.set_ai_analysis(call_id, summary, sentiment)
        await broadcast("call_ai_analysis", {
            "call_id": call_id, "summary": summary, "sentiment": sentiment,
        })
    except Exception as exc:
        logger.error("AI analysis failed: %s", exc)


# ── Agent change hook ──────────────────────────────────────────────────────────

async def _on_agent_change(agent: Agent) -> None:
    await broadcast("agent_update", agent.model_dump())


agent_mgr.on_change(_on_agent_change)


# ── Global ESL handlers (QUICK DIAL only — campaign calls handled by DialerEngine)

async def _global_on_background_job(event) -> None:
    job_uuid = event.job_uuid
    body = event.get("body", "").strip()
    call = call_mgr.by_job_uuid(job_uuid)
    if not call:
        logger.info("BACKGROUND_JOB job=%s (no matching call) body=%r", job_uuid, body)
        return
    if call.campaign_id != "quick":
        return

    if body.startswith("+OK"):
        result_uuid = body.split()[-1]
        if call.fs_uuid and result_uuid != call.fs_uuid and call.agent_fs_uuid is None:
            # Agent (Zoiper) answered and is now parked — register UUID then uuid_bridge.
            # Small delay lets Zoiper's RTP stabilise before bridging.
            call.agent_fs_uuid = result_uuid
            call_mgr._by_fs_uuid[result_uuid] = call.id
            logger.info("Agent parked (BACKGROUND_JOB): agent_uuid=%s → uuid_bridge carrier=%s",
                        result_uuid, call.fs_uuid)
            await asyncio.sleep(1)
            try:
                await esl.uuid_bridge(call.fs_uuid, result_uuid)
                call_mgr.on_bridged(call.fs_uuid, call.agent_id or "")
                logger.info("uuid_bridge OK: carrier=%s <-> agent=%s", call.fs_uuid, result_uuid)
                await broadcast("call_bridged", call.model_dump())
            except Exception as exc:
                logger.error("uuid_bridge failed: %s", exc)
                await esl.hangup(call.fs_uuid)
        else:
            # Originate job confirmation — update carrier UUID in case it differs
            call_mgr.set_fs_uuid(call.id, result_uuid)
    else:
        logger.warning("BACKGROUND_JOB failed: job=%s call=%s reason=%r", job_uuid, call.id, body)
        call_mgr.on_failed(job_uuid, body)
        await broadcast("call_failed", {"call_id": call.id, "reason": body})
        await broadcast("call_ended", call.model_dump())


async def _global_on_answer(event) -> None:
    fs_uuid = event.unique_id
    logger.info("CHANNEL_ANSWER received fs_uuid=%s", fs_uuid)
    call = call_mgr.by_fs_uuid(fs_uuid)
    logger.info("CHANNEL_ANSWER call lookup → %s", call.id if call else "NOT FOUND")
    if not call or call.campaign_id != "quick":
        logger.info("CHANNEL_ANSWER skipped (campaign_id=%s)", call.campaign_id if call else "N/A")
        return
    call = call_mgr.on_answered(fs_uuid)
    if not call:
        logger.warning("CHANNEL_ANSWER on_answered returned None for %s", fs_uuid)
        return

    logger.info("Quick-dial answered: phone=%s fs_uuid=%s", call.contact.phone, fs_uuid)

    if settings.RECORDING_ENABLED and call.fs_uuid:
        rec_file = f"{settings.RECORDING_DIR}/{call.id}.{settings.RECORDING_FORMAT}"
        try:
            await esl.api(f"uuid_record {call.fs_uuid} start {rec_file}")
            call.recording_path = f"{call.id}.{settings.RECORDING_FORMAT}"
            logger.info("Recording started: %s", rec_file)
        except Exception as exc:
            logger.warning("Recording start failed: %s", exc)

    sip_domain = settings.FS_SIP_DOMAIN or settings.FS_HOST

    # ── Mobile quick-dial: bridge back to the caller's own SIP extension ──────
    # Only when explicitly requested by the mobile app (bridge_to_caller flag).
    # Web quick-dial leaves this False and falls through to the idle-agent pool.
    if call.bridge_to_caller and call.caller_username:
        caller_user = users.get(call.caller_username)
        if caller_user and caller_user.extension:
            logger.info("Mobile quick-dial answered: bridging back to %s ext=%s",
                        call.caller_username, caller_user.extension)
            try:
                job = await esl.bridge_to_agent(
                    call.fs_uuid, caller_user.extension, call.contact.phone,
                    phone_type=caller_user.phone_type or "webphone",
                    sip_domain=sip_domain,
                )
                call_mgr._by_job_uuid[job] = call.id
                logger.info("Mobile bridge dispatched job=%s → %s ext=%s",
                            job, call.caller_username, caller_user.extension)
            except Exception as exc:
                logger.error("Mobile bridge failed: %s", exc, exc_info=True)
                if call.fs_uuid:
                    await esl.hangup(call.fs_uuid)
                call.status = CallStatus.DROPPED
        else:
            logger.warning("Mobile quick-dial: no extension for user %s — dropping",
                           call.caller_username)
            if call.fs_uuid:
                await esl.hangup(call.fs_uuid)
            call.status = CallStatus.DROPPED
        await broadcast("call_answered", call.model_dump())
        return

    # ── Predictive dialer: bridge to an idle desk-phone agent ─────────────────
    all_agents = agent_mgr.list_all()
    idle = agent_mgr.get_idle()
    logger.info("Agents: total=%d idle=%d names=%s",
                len(all_agents), len(idle),
                [(a.name, a.status) for a in all_agents])

    if idle:
        agent = idle[0]
        logger.info("Bridging to agent %s ext=%s phone_type=%s", agent.name, agent.extension, agent.phone_type)
        await agent_mgr.assign_call(agent.id, call.id)
        call.agent_id = agent.id
        try:
            job = await esl.bridge_to_agent(
                call.fs_uuid, agent.extension, call.contact.phone,
                phone_type=agent.phone_type, sip_domain=sip_domain,
            )
            call_mgr._by_job_uuid[job] = call.id
            logger.info("Bridge dispatched job=%s → agent %s ext=%s",
                        job, agent.name, agent.extension)
        except Exception as exc:
            logger.error("Bridge failed: %s", exc, exc_info=True)
            await agent_mgr.release_call(agent.id)
            call.agent_id = None
    else:
        logger.warning("Quick-dial: NO IDLE AGENTS for %s — dropping call", call.contact.phone)
        if call.fs_uuid:
            await esl.hangup(call.fs_uuid)
        call.status = CallStatus.DROPPED
    await broadcast("call_answered", call.model_dump())


async def _global_on_hangup(event) -> None:
    fs_uuid = event.unique_id
    call = call_mgr.by_fs_uuid(fs_uuid)
    if not call or call.campaign_id != "quick":
        return
    cause = event.get("Hangup-Cause", "")
    sip_code = (
        event.get("variable_sip_term_status") or
        event.get("variable_sip_invite_failure_status") or
        event.get("variable_sip_term_cause") or ""
    )
    sip_vars = {k: v for k, v in event.items() if "sip" in k.lower()}
    logger.info("HANGUP fs=%s cause=%s sip_code=%s sip_vars=%s",
                fs_uuid, cause, sip_code, sip_vars)

    # ── Agent (Zoiper) leg hung up ─────────────────────────────────────────────
    # When Zoiper drops, the carrier leg stays alive in FreeSWITCH.
    # We must kill it explicitly so the call ends and the dashboard updates.
    if fs_uuid == call.agent_fs_uuid:
        logger.info("Agent leg %s hung up → killing carrier leg %s", fs_uuid, call.fs_uuid)
        if call.fs_uuid:
            try:
                await esl.hangup(call.fs_uuid)
            except Exception as exc:
                logger.warning("Could not hang up carrier leg: %s", exc)
        # The CHANNEL_HANGUP for the carrier leg will arrive and do final cleanup
        return

    # ── Carrier leg hung up (normal path) ─────────────────────────────────────
    call = call_mgr.on_hangup(fs_uuid, cause, sip_code)
    if not call:
        return
    if call.agent_id:
        await agent_mgr.release_call(call.agent_id)

    # ── Deduct minutes from subscription (quick-dial only) ────────────────
    if call.caller_username and call.duration:
        sub = subscriptions.get(call.caller_username)
        if sub and sub.is_active:
            minutes_used = round(call.duration / 60, 4)
            sub.minutes_used = round(sub.minutes_used + minutes_used, 4)
            minutes_remaining = round(sub.minutes_total - sub.minutes_used, 4)
            if minutes_remaining <= 0:
                sub.is_active = False
                sub.minutes_used = float(sub.minutes_total)  # cap at total
                logger.info("Subscription EXPIRED for %s (used %.1f min)",
                            call.caller_username, sub.minutes_total)
            else:
                logger.info("Subscription: %s used %.2f min (%.2f remaining)",
                            call.caller_username, sub.minutes_used, minutes_remaining)
            await broadcast("subscription_update", {
                "username": sub.username,
                "minutes_used": sub.minutes_used,
                "minutes_total": sub.minutes_total,
                "minutes_remaining": max(0, sub.minutes_total - sub.minutes_used),
                "is_active": sub.is_active,
            })

    _save()
    await broadcast("call_ended", call.model_dump())


esl.add_handler("BACKGROUND_JOB", _global_on_background_job)
esl.add_handler("CHANNEL_ANSWER",  _global_on_answer)
esl.add_handler("CHANNEL_HANGUP",  _global_on_hangup)


# ── App startup / shutdown ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_persisted()
    try:
        await esl.connect()
        await esl.subscribe(
            "CHANNEL_CREATE", "CHANNEL_ANSWER", "CHANNEL_HANGUP",
            "CHANNEL_BRIDGE", "CHANNEL_UNBRIDGE", "BACKGROUND_JOB", "CUSTOM",
            "CHANNEL_PARK",   # inbound calling (handled by InboundRouter)
        )
        # Inbound DID routing — isolated handler, does not affect outbound.
        inbound_router.register()
        logger.info("FreeSWITCH ESL ready")
    except Exception as exc:
        logger.warning("Could not connect to FreeSWITCH ESL: %s", exc)
    yield
    _save()
    await esl.disconnect()


app = FastAPI(title="AI Predictive Dialer", version="1.0.0", lifespan=lifespan)

# Allow Flutter web (localhost) and the mobile app to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten to specific origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="frontend"), name="static")


# ── Page routes ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("frontend/index.html") as f:
        return f.read()


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    with open("frontend/login.html") as f:
        return f.read()


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    with open("frontend/admin.html") as f:
        return f.read()


# ── Auth endpoints ─────────────────────────────────────────────────────────────

@app.post("/auth/login")
async def auth_login(body: dict):
    username = (body.get("username") or "").strip().lower()
    password = body.get("password") or ""
    user = users.get(username)
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(401, "Invalid username or password")
    token = create_token(user.id, user.role, user.username)
    return {"token": token, "role": user.role, "username": user.username}


@app.get("/auth/me")
async def auth_me(payload: dict = Depends(require_any)):
    return {"username": payload.get("username"), "role": payload.get("role")}


@app.get("/agent/config")
async def agent_config(request: Request, payload: dict = Depends(require_any)):
    """Return the authenticated user's SIP / webphone configuration.

    Web dashboard (https page) needs a secure wss:// socket; the native mobile
    app can use plain ws://. The mobile app sends header `X-Client-Type: mobile`
    so each platform gets the right WebSocket URL without disturbing the other.
    """
    username = payload.get("username")
    user = users.get(username)
    if not user:
        raise HTTPException(404, "User not found")

    # Platform-aware WebSocket URL
    client_type = request.headers.get("x-client-type", "").lower()
    if client_type == "mobile":
        # Native app → plain ws:// (no browser cert/mixed-content limits)
        ws_url = settings.FS_WS_URL_MOBILE or settings.FS_WS_URL or f"ws://{settings.FS_HOST}:5066"
    else:
        # Web dashboard (https) → secure wss://
        ws_url = settings.FS_WS_URL or f"ws://{settings.FS_HOST}:5066"
    sip_domain = settings.FS_SIP_DOMAIN or settings.FS_HOST

    # Include subscribed DID if user has one
    sub = subscriptions.get(username)
    subscribed_did = sub.did_number if sub else None

    return {
        "extension":      user.extension,
        "sip_password":   user.sip_password,
        "phone_type":     user.phone_type,
        "ws_url":         ws_url,
        "sip_domain":     sip_domain,
        "display_name":   username,
        "subscribed_did": subscribed_did,
    }


@app.patch("/agent/preferences")
async def update_agent_preferences(body: dict, payload: dict = Depends(require_any)):
    """Update the authenticated user's phone type and/or SIP password."""
    username = payload.get("username")
    user = users.get(username)
    if not user:
        raise HTTPException(404, "User not found")
    if "phone_type" in body and body["phone_type"] in ("softphone", "webphone"):
        user.phone_type = body["phone_type"]
        # Keep the linked Agent in sync so DialerEngine reads the right value
        if user.agent_id:
            agent = agent_mgr.get(user.agent_id)
            if agent:
                agent.phone_type = body["phone_type"]
    if "sip_password" in body:
        user.sip_password = body["sip_password"] or None
    _save()
    return {"status": "ok", "phone_type": user.phone_type}


# ── Admin: User management ─────────────────────────────────────────────────────

@app.get("/admin/users")
async def list_users(payload: dict = Depends(require_admin)):
    return [
        {"id": u.id, "username": u.username, "role": u.role,
         "extension": u.extension, "agent_id": u.agent_id, "created_at": u.created_at}
        for u in users.values()
    ]


@app.post("/admin/users")
async def create_user(body: UserCreate, payload: dict = Depends(require_admin)):
    username = body.username.strip().lower()
    if not username:
        raise HTTPException(400, "username required")
    if username in users:
        raise HTTPException(409, "Username already exists")

    # Block duplicate extensions — each extension can only belong to one agent
    if body.extension:
        conflict = agent_mgr.by_extension(body.extension)
        if conflict:
            raise HTTPException(409, f"Extension {body.extension} is already assigned to agent '{conflict.name}'")

    web_password = body.password or "1234"
    user = User(
        username=username,
        password_hash=hash_password(web_password),
        role=body.role,
        extension=body.extension,
        # sip_password defaults to the web password so admin has one less thing to configure
        sip_password=body.sip_password or web_password,
    )

    # Auto-register as a dialer agent when an extension is provided
    if body.extension:
        agent = Agent(name=username, extension=body.extension, phone_type=user.phone_type)
        agent_mgr.register(agent)
        user.agent_id = agent.id
        logger.info("Auto-created agent '%s' ext=%s for user '%s'",
                    username, body.extension, username)

    users[username] = user
    _save()
    return {"id": user.id, "username": user.username, "role": user.role,
            "extension": user.extension, "agent_id": user.agent_id, "created_at": user.created_at}


@app.post("/admin/users/{username}/reset-password")
async def reset_password(username: str, body: dict, payload: dict = Depends(require_admin)):
    user = users.get(username)
    if not user:
        raise HTTPException(404, "User not found")
    new_pw = (body.get("password") or "1234")
    user.password_hash = hash_password(new_pw)
    _save()
    return {"status": "ok"}


@app.post("/admin/users/{username}/reset-sip-password")
async def reset_sip_password(username: str, body: dict, payload: dict = Depends(require_admin)):
    """Set the SIP password used by the webphone to authenticate with FreeSWITCH."""
    user = users.get(username)
    if not user:
        raise HTTPException(404, "User not found")
    user.sip_password = body.get("password") or "1234"
    _save()
    return {"status": "ok"}


@app.delete("/admin/users/{username}")
async def delete_user(username: str, payload: dict = Depends(require_admin)):
    if username not in users:
        raise HTTPException(404, "User not found")
    if username == payload.get("username"):
        raise HTTPException(400, "Cannot delete yourself")
    user = users.pop(username)
    # Remove the linked agent so the extension is freed
    if user.agent_id:
        agent_mgr.remove(user.agent_id)
        await broadcast("agent_removed", {"agent_id": user.agent_id})
    _save()
    return {"status": "deleted"}


# ── User-facing DID list (for mobile DID picker) ───────────────────────────────

@app.get("/dids")
async def list_dids_user(payload: dict = Depends(require_any)):
    """Return all active DIDs — used by the mobile app DID selector."""
    return [d for d in dids if d.active]


# ── Admin: DID management ──────────────────────────────────────────────────────

@app.get("/admin/dids")
async def list_dids(payload: dict = Depends(require_any)):
    return dids


@app.post("/admin/dids")
async def add_did(body: dict, payload: dict = Depends(require_admin)):
    number = (body.get("number") or "").strip()
    if not number:
        raise HTTPException(400, "number required")
    did = DID(number=number, label=body.get("label") or f"DID {number[-4:]}")
    dids.append(did)
    _save()
    return did


@app.patch("/admin/dids/{did_id}")
async def toggle_did(did_id: str, body: dict, payload: dict = Depends(require_admin)):
    did = next((d for d in dids if d.id == did_id), None)
    if not did:
        raise HTTPException(404, "DID not found")
    if "active" in body:
        did.active = bool(body["active"])
    if "label" in body:
        did.label = body["label"]
    _save()
    return did


@app.delete("/admin/dids/{did_id}")
async def delete_did(did_id: str, payload: dict = Depends(require_admin)):
    global dids
    orig = len(dids)
    dids = [d for d in dids if d.id != did_id]
    if len(dids) == orig:
        raise HTTPException(404, "DID not found")
    _save()
    return {"status": "deleted"}


# ── DID Store ─────────────────────────────────────────────────────────────────

@app.get("/store/plans")
async def store_plans(payload: dict = Depends(require_any)):
    """List DIDs available for purchase (for_sale=True, not yet owned)."""
    available = [
        {
            "id":      d.id,
            "number":  d.number,
            "label":   d.label or f"USA Local Number",
            "price":   d.price,
            "minutes": d.minutes,
            "country": "USA",
        }
        for d in dids
        if d.active and d.for_sale and d.owner_username is None
    ]
    return available


@app.post("/store/purchase/{did_id}")
async def store_purchase(did_id: str, payload: dict = Depends(require_any)):
    """Purchase a DID (dummy — no real payment). Creates subscription."""
    username = payload.get("username")
    user = users.get(username)
    if not user:
        raise HTTPException(404, "User not found")

    # Check no existing active subscription
    existing = subscriptions.get(username)
    if existing and existing.is_active:
        raise HTTPException(400, "You already have an active plan. Renew instead.")

    did = next((d for d in dids if d.id == did_id), None)
    if not did:
        raise HTTPException(404, "DID not found")
    if not did.for_sale:
        raise HTTPException(400, "This number is not available for purchase")
    if did.owner_username and did.owner_username != username:
        raise HTTPException(409, "This number has already been purchased")

    # Mark DID as owned
    did.owner_username = username

    # Create / replace subscription
    sub = Subscription(
        username=username,
        did_id=did.id,
        did_number=did.number,
        plan_name=f"USA {did.minutes}-min Pack",
        price=did.price,
        minutes_total=did.minutes,
        minutes_used=0.0,
        is_active=True,
    )
    subscriptions[username] = sub

    # Assign DID as the user's caller ID (update their config for quick-dial)
    # We store it via a convention: use the purchased DID as default caller_id
    _save()
    logger.info("PURCHASE: user=%s did=%s plan=%s", username, did.number, sub.plan_name)

    return {
        "status": "purchased",
        "subscription": sub.model_dump(),
        "did_number": did.number,
        "minutes_total": sub.minutes_total,
        "price_paid": sub.price,
    }


@app.get("/my/subscription")
async def my_subscription(payload: dict = Depends(require_any)):
    """Get the calling user's active subscription (if any)."""
    username = payload.get("username")
    sub = subscriptions.get(username)
    if not sub:
        return {"has_subscription": False}

    minutes_remaining = max(0.0, round(sub.minutes_total - sub.minutes_used, 2))
    return {
        "has_subscription": True,
        "id":               sub.id,
        "did_number":       sub.did_number,
        "plan_name":        sub.plan_name,
        "price":            sub.price,
        "minutes_total":    sub.minutes_total,
        "minutes_used":     round(sub.minutes_used, 2),
        "minutes_remaining": minutes_remaining,
        "is_active":        sub.is_active,
        "purchased_at":     sub.purchased_at.isoformat(),
        "renewals":         sub.renewals,
    }


@app.post("/my/subscription/renew")
async def renew_subscription(payload: dict = Depends(require_any)):
    """Renew (top-up) 10 more minutes for $5 (dummy payment)."""
    username = payload.get("username")
    sub = subscriptions.get(username)
    if not sub:
        raise HTTPException(404, "No subscription found — purchase a plan first")

    # Add 10 more minutes and reactivate
    sub.minutes_total += 10
    sub.is_active = True
    sub.renewals += 1

    _save()
    minutes_remaining = max(0.0, round(sub.minutes_total - sub.minutes_used, 2))
    logger.info("RENEW: user=%s renewal#%d total=%d remaining=%.1f",
                username, sub.renewals, sub.minutes_total, minutes_remaining)

    return {
        "status":           "renewed",
        "minutes_total":    sub.minutes_total,
        "minutes_used":     round(sub.minutes_used, 2),
        "minutes_remaining": minutes_remaining,
        "renewals":         sub.renewals,
        "price_paid":       5.0,
    }


# ── Admin: Store management ────────────────────────────────────────────────────

@app.patch("/admin/dids/{did_id}/store")
async def admin_did_store(did_id: str, body: dict, payload: dict = Depends(require_admin)):
    """Admin: set price, minutes, for_sale flag on a DID."""
    did = next((d for d in dids if d.id == did_id), None)
    if not did:
        raise HTTPException(404, "DID not found")
    if "for_sale" in body:
        did.for_sale = bool(body["for_sale"])
    if "price" in body:
        did.price = float(body["price"])
    if "minutes" in body:
        did.minutes = int(body["minutes"])
    _save()
    return did


# ── Admin: Reports ─────────────────────────────────────────────────────────────

@app.get("/admin/reports/calls")
async def report_calls(payload: dict = Depends(require_admin)):
    result = []
    for c in call_mgr.all_calls():
        d = c.model_dump()
        # Enrich with human-readable names (avoids ID lookups in JS)
        d["agent_name"] = None
        if c.agent_id:
            ag = agent_mgr.get(c.agent_id)
            if ag:
                d["agent_name"] = ag.name
        d["campaign_name"] = None
        if c.campaign_id == "quick":
            d["campaign_name"] = "Quick Dial"
        elif c.campaign_id and c.campaign_id in campaigns:
            d["campaign_name"] = campaigns[c.campaign_id].name
        result.append(d)
    return result


@app.get("/admin/reports/summary")
async def report_summary(payload: dict = Depends(require_admin)):
    calls = call_mgr.all_calls()
    sip_codes: Dict[str, int] = {}
    dispositions: Dict[str, int] = {}
    causes: Dict[str, int] = {}
    statuses: Dict[str, int] = {}
    total_dur = 0
    dur_count = 0

    for c in calls:
        sip_codes[c.sip_code or "—"]       = sip_codes.get(c.sip_code or "—", 0) + 1
        dispositions[c.disposition or "—"] = dispositions.get(c.disposition or "—", 0) + 1
        causes[c.hangup_cause or "—"]      = causes.get(c.hangup_cause or "—", 0) + 1
        st = c.status.value if hasattr(c.status, "value") else str(c.status)
        statuses[st] = statuses.get(st, 0) + 1
        if c.duration:
            total_dur += c.duration
            dur_count += 1

    answered    = sum(1 for c in calls if c.answer_time)
    total       = len(calls)
    avg_dur     = round(total_dur / dur_count) if dur_count else 0
    answer_rate = round(answered / total * 100, 1) if total else 0.0

    # Per-agent performance
    perf: Dict[str, dict] = {}
    for c in calls:
        if not c.agent_id:
            continue
        ag   = agent_mgr.get(c.agent_id)
        name = ag.name if ag else c.agent_id[:8]
        if name not in perf:
            perf[name] = {"calls": 0, "total_dur": 0, "answered": 0}
        perf[name]["calls"] += 1
        if c.duration:
            perf[name]["total_dur"] += c.duration
            perf[name]["answered"]  += 1

    agent_perf = [
        {
            "name":         name,
            "calls":        d["calls"],
            "answered":     d["answered"],
            "avg_duration": round(d["total_dur"] / d["answered"]) if d["answered"] else 0,
        }
        for name, d in sorted(perf.items(), key=lambda x: -x[1]["calls"])
    ]

    return {
        "total":             total,
        "answered":          answered,
        "dropped":           sum(1 for c in calls if c.status == CallStatus.DROPPED),
        "failed":            sum(1 for c in calls if c.status == CallStatus.FAILED),
        "completed":         sum(1 for c in calls if c.status == CallStatus.COMPLETED),
        "avg_duration":      avg_dur,
        "answer_rate":       answer_rate,
        "sip_codes":         sip_codes,
        "dispositions":      dispositions,
        "hangup_causes":     causes,
        "statuses":          statuses,
        "agent_performance": agent_perf,
    }


# ── Admin: Inbound call tracking (isolated — data from InboundRouter) ────────────
@app.get("/admin/inbound")
async def admin_inbound(payload: dict = Depends(require_admin)):
    """Inbound call log + KPIs. Sourced entirely from the isolated InboundRouter;
    does not read or affect outbound call state."""
    return {
        "stats": inbound_router.stats(),
        "calls": inbound_router.recent_calls(limit=300),
    }


# ── Campaign endpoints ─────────────────────────────────────────────────────────

@app.post("/campaigns", response_model=Campaign)
async def create_campaign(body: CampaignCreate, payload: dict = Depends(require_any)):
    c = Campaign(name=body.name)
    campaigns[c.id] = c
    _save()
    return c


@app.get("/campaigns", response_model=List[Campaign])
async def list_campaigns(payload: dict = Depends(require_any)):
    return list(campaigns.values())


@app.get("/campaigns/{campaign_id}", response_model=Campaign)
async def get_campaign(campaign_id: str, payload: dict = Depends(require_any)):
    c = campaigns.get(campaign_id)
    if not c:
        raise HTTPException(404, "Campaign not found")
    return c


@app.post("/campaigns/{campaign_id}/upload")
async def upload_contacts(campaign_id: str, file: UploadFile,
                          payload: dict = Depends(require_any)):
    c = campaigns.get(campaign_id)
    if not c:
        raise HTTPException(404, "Campaign not found")
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    added = 0
    for row in reader:
        phone = row.get("phone") or row.get("Phone") or row.get("PHONE")
        if not phone:
            continue
        contact = Contact(
            phone=phone.strip(),
            name=(row.get("name") or row.get("Name") or "").strip() or None,
            email=(row.get("email") or row.get("Email") or "").strip() or None,
        )
        c.contacts.append(contact)
        added += 1
    c.stats.contacts_total = len(c.contacts)
    c.stats.contacts_remaining = len(c.contacts)
    _save()
    return {"added": added, "total": len(c.contacts)}


@app.post("/campaigns/{campaign_id}/reset")
async def reset_campaign(campaign_id: str, payload: dict = Depends(require_any)):
    c = campaigns.get(campaign_id)
    if not c:
        raise HTTPException(404, "Campaign not found")
    engine = engines.get(campaign_id)
    if engine:
        await engine.stop()
        engines.pop(campaign_id, None)
    await agent_mgr.release_all_to_idle()
    for contact in c.contacts:
        contact.dialed = False
        contact.dialed_at = None
        contact.result = None
    from models import CampaignStats
    c.stats = CampaignStats(
        contacts_total=len(c.contacts),
        contacts_remaining=len(c.contacts),
    )
    c.status = CampaignStatus.IDLE
    c.started_at = None
    c.completed_at = None
    await broadcast("campaign_reset", {
        "id": c.id, "name": c.name, "status": c.status,
        "stats": c.stats.model_dump(),
    })
    return {"status": "reset", "contacts": len(c.contacts)}


@app.post("/campaigns/{campaign_id}/start")
async def start_campaign(campaign_id: str, body: dict = {},
                         payload: dict = Depends(require_any)):
    c = campaigns.get(campaign_id)
    if not c:
        raise HTTPException(404, "Campaign not found")
    if not c.contacts:
        raise HTTPException(400, "No contacts loaded")

    b = body or {}
    # Allow overriding caller_id with a specific DID
    caller_id = b.get("caller_id") or settings.CALLER_ID_NUMBER
    # Optional per-run overrides (admin can tune without touching .env)
    hopper_size   = int(b.get("hopper_size",   200))
    max_concurrent = int(b.get("max_concurrent", settings.MAX_CONCURRENT_CALLS))

    engine = DialerEngine(
        esl=esl, agent_mgr=agent_mgr, call_mgr=call_mgr, campaign=c,
        gateway=settings.SIP_GATEWAY, caller_id=caller_id,
        dial_prefix=settings.DIAL_PREFIX, dial_timeout=settings.DIAL_TIMEOUT,
        max_concurrent=max_concurrent,
        drop_rate_limit=settings.DROP_RATE_LIMIT,
        pacing_interval=settings.PACING_INTERVAL,
        amd_enabled=settings.AMD_ENABLED,
        recording_enabled=settings.RECORDING_ENABLED,
        recording_dir=settings.RECORDING_DIR,
        recording_format=settings.RECORDING_FORMAT,
        sip_domain=settings.FS_SIP_DOMAIN or settings.FS_HOST,
        hopper_size=hopper_size,
        on_event=on_dialer_event,
    )
    engines[campaign_id] = engine
    await engine.start()
    return {"status": "running", "hopper_size": hopper_size, "max_concurrent": max_concurrent}


@app.post("/campaigns/{campaign_id}/pause")
async def pause_campaign(campaign_id: str, payload: dict = Depends(require_any)):
    engine = engines.get(campaign_id)
    if not engine:
        raise HTTPException(404, "Campaign not running")
    await engine.pause()
    return {"status": "paused"}


@app.post("/campaigns/{campaign_id}/resume")
async def resume_campaign(campaign_id: str, payload: dict = Depends(require_any)):
    engine = engines.get(campaign_id)
    if not engine:
        raise HTTPException(404, "Campaign not running")
    await engine.resume()
    return {"status": "running"}


@app.post("/campaigns/{campaign_id}/stop")
async def stop_campaign(campaign_id: str, payload: dict = Depends(require_any)):
    engine = engines.get(campaign_id)
    if not engine:
        raise HTTPException(404, "Campaign not running")
    await engine.stop()
    engines.pop(campaign_id, None)
    return {"status": "stopped"}


@app.post("/campaigns/{campaign_id}/redial")
async def redial_campaign(campaign_id: str, body: dict = {},
                          payload: dict = Depends(require_any)):
    """
    Re-queue contacts by their previous call result (disposition).

    body.filter options:
      "no_answer"  – SIP 480/408 or cause NO_ANSWER
      "busy"       – SIP 486
      "machine"    – detected as answering machine
      "dropped"    – call was dropped (no available agent)
      "failed"     – originate/carrier failure
      "answered"   – completed calls (agent spoke to contact)
      "unanswered" – everything except answered (no_answer + busy + machine + dropped + failed)
      "all"        – reset every contact regardless of result
    """
    c = campaigns.get(campaign_id)
    if not c:
        raise HTTPException(404, "Campaign not found")

    # Cannot redial a running campaign
    if c.status == CampaignStatus.RUNNING:
        raise HTTPException(400, "Stop or pause the campaign before redialling")

    b = body or {}
    flt = (b.get("filter") or "all").lower().strip()

    VALID_FILTERS = {"no_answer", "busy", "machine", "dropped", "failed",
                     "answered", "unanswered", "all"}
    if flt not in VALID_FILTERS:
        raise HTTPException(400, f"filter must be one of {sorted(VALID_FILTERS)}")

    reset_count = 0
    for contact in c.contacts:
        if not contact.dialed:
            # Never dialled — skip (already eligible)
            continue

        result = (contact.result or "").lower()

        match = False
        if flt == "all":
            match = True
        elif flt == "unanswered":
            match = result in ("no_answer", "busy", "machine", "dropped", "failed", "")
        else:
            match = (result == flt)

        if match:
            contact.dialed = False
            contact.dialed_at = None
            contact.result = None
            reset_count += 1

    # Update stats to reflect new remaining count
    undialed = sum(1 for ct in c.contacts if not ct.dialed)
    from models import CampaignStats
    c.stats = CampaignStats(
        contacts_total=len(c.contacts),
        contacts_remaining=undialed,
        hopper_size=c.stats.hopper_size,
    )
    # Keep campaign in COMPLETED/IDLE so user can hit Start again
    if c.status == CampaignStatus.COMPLETED:
        c.status = CampaignStatus.IDLE

    _save()
    await broadcast("campaign_reset", {
        "id": c.id, "name": c.name, "status": c.status,
        "stats": c.stats.model_dump(),
        "redial_filter": flt,
        "redial_count": reset_count,
    })
    return {"status": "ready", "filter": flt, "reset": reset_count, "total_undialed": undialed}


# ── Agent endpoints ────────────────────────────────────────────────────────────

@app.post("/agents", response_model=Agent)
async def create_agent(body: AgentCreate, payload: dict = Depends(require_any)):
    agent = Agent(name=body.name, extension=body.extension)
    agent_mgr.register(agent)
    _save()
    return agent


@app.get("/agents", response_model=List[Agent])
async def list_agents(payload: dict = Depends(require_any)):
    return agent_mgr.list_all()


@app.post("/agents/login")
async def agent_login(body: AgentLogin, payload: dict = Depends(require_any)):
    ok = await agent_mgr.login(body.agent_id)
    if not ok:
        raise HTTPException(404, "Agent not found")
    return {"status": "idle"}


@app.post("/agents/logout")
async def agent_logout(body: AgentLogin, payload: dict = Depends(require_any)):
    await agent_mgr.logout(body.agent_id)
    return {"status": "offline"}


@app.post("/agents/break")
async def agent_break(body: AgentLogin, payload: dict = Depends(require_any)):
    ok = await agent_mgr.set_break(body.agent_id)
    if not ok:
        raise HTTPException(400, "Cannot set break from current status")
    return {"status": "break"}


@app.post("/agents/return")
async def agent_return(body: AgentLogin, payload: dict = Depends(require_any)):
    ok = await agent_mgr.return_from_break(body.agent_id)
    if not ok:
        raise HTTPException(400, "Agent is not on break")
    return {"status": "idle"}


# ── Call endpoints ─────────────────────────────────────────────────────────────

@app.get("/calls")
async def list_calls(payload: dict = Depends(require_any)):
    return [c.model_dump() for c in call_mgr.all_calls()]


@app.get("/calls/{call_id}")
async def get_call(call_id: str, payload: dict = Depends(require_any)):
    """Get a single call by ID — used by mobile app to poll call status."""
    call = call_mgr.get(call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    return call.model_dump()


@app.post("/calls/{call_id}/hangup")
async def hangup_call(call_id: str, payload: dict = Depends(require_any)):
    call = call_mgr.get(call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    if not call.fs_uuid:
        raise HTTPException(400, "Call has no FreeSWITCH UUID yet")
    await esl.hangup(call.fs_uuid)
    return {"status": "hangup sent"}


@app.get("/recordings/{filename}")
async def serve_recording(filename: str, download: bool = False,
                          payload: dict = Depends(require_any)):
    import re
    if not re.match(r'^[\w\-]+\.(wav|mp3|ogg)$', filename):
        raise HTTPException(400, "Invalid filename")
    path = os.path.join(settings.RECORDING_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Recording not found")
    mt = "audio/mpeg" if filename.endswith(".mp3") else (
         "audio/ogg"  if filename.endswith(".ogg") else "audio/wav")
    disposition = "attachment" if download else "inline"
    return FileResponse(path, media_type=mt, headers={
        "Content-Disposition": f'{disposition}; filename="{filename}"'
    })


@app.post("/calls/quick-dial")
async def quick_dial(body: dict, payload: dict = Depends(require_any)):
    phone = (body.get("phone") or "").strip()
    if not phone:
        raise HTTPException(400, "phone required")

    username = payload.get("username")

    # Resolve caller ID: request body → subscription DID → env default
    caller_id = (body.get("caller_id") or "").strip()
    if not caller_id:
        sub = subscriptions.get(username or "")
        if sub and sub.is_active and sub.did_number:
            caller_id = sub.did_number
            logger.info("quick_dial: using subscription DID %s as caller_id for %s", caller_id, username)
    if not caller_id:
        caller_id = settings.CALLER_ID_NUMBER
    contact = Contact(phone=phone, name=body.get("name", "Quick Dial"))
    # Mobile app sets bridge_to_caller=true → call is bridged back to the caller's
    # own SIP extension. Web dashboard omits it → uses the idle-agent pool (original behavior).
    bridge_to_caller = bool(body.get("bridge_to_caller", False))
    call = Call(contact=contact, campaign_id="quick", caller_id=caller_id,
                caller_username=username, bridge_to_caller=bridge_to_caller)
    call_mgr.add(call)

    try:
        dialed = f"{settings.DIAL_PREFIX}{phone}" if settings.DIAL_PREFIX else phone
        job_uuid, channel_uuid = await esl.originate(
            dialed, settings.SIP_GATEWAY, caller_id, settings.DIAL_TIMEOUT,
        )
        call.fs_job_uuid = job_uuid
        call_mgr._by_job_uuid[job_uuid] = call.id
        call_mgr.set_fs_uuid(call.id, channel_uuid)
        call.status = CallStatus.RINGING
        await broadcast("call_dialing", call.model_dump())
        logger.info("Quick dial: %s job=%s uuid=%s caller=%s", phone, job_uuid, channel_uuid, caller_id)
        return {"status": "dialing", "call_id": call.id, "job_uuid": job_uuid}
    except Exception as exc:
        call.status = CallStatus.FAILED
        call_mgr._calls.pop(call.id, None)
        raise HTTPException(500, str(exc))


@app.post("/calls/disposition")
async def set_disposition(body: AgentDisposition, payload: dict = Depends(require_any)):
    call = call_mgr.set_disposition(body.call_id, body.disposition, body.notes or "")
    if not call:
        raise HTTPException(404, "Call not found")
    await broadcast("call_disposition", {"call_id": call.id, "disposition": call.disposition})
    return {"status": "ok"}


@app.post("/calls/{call_id}/ai-script")
async def get_ai_script(call_id: str, payload: dict = Depends(require_any)):
    call = call_mgr.get(call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    script = await ai.generate_script_suggestion(
        contact_name=call.contact.name, product="our service",
    )
    return {"script": script}


# ── WebSocket ──────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def admin_ws(ws: WebSocket, token: Optional[str] = Query(None)):
    # Verify token if provided (soft auth — allows unauthenticated for now)
    if token:
        payload = decode_token(token)
        if not payload:
            await ws.close(code=4001)
            return

    await ws.accept()
    admin_ws_clients.append(ws)

    terminal = {CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.DROPPED}
    history = [c.model_dump() for c in call_mgr.all_calls() if c.status in terminal]
    snapshot = {
        "agents":       [a.model_dump() for a in agent_mgr.list_all()],
        "campaigns":    [c.model_dump(exclude={"contacts"}) for c in campaigns.values()],
        "active_calls": [c.model_dump() for c in call_mgr.active()],
        "history":      history[-200:],
        "dids":         [d.model_dump() for d in dids],
    }
    await ws.send_text(json.dumps({"type": "snapshot", "data": snapshot}, default=_json_default))

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        admin_ws_clients.remove(ws)


@app.websocket("/ws/agent/{agent_id}")
async def agent_ws(ws: WebSocket, agent_id: str):
    await ws.accept()
    q = agent_mgr.attach_ws_queue(agent_id)

    async def reader():
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass

    async def writer():
        while True:
            msg = await q.get()
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                break

    await asyncio.gather(reader(), writer(), return_exceptions=True)
    agent_mgr.detach_ws_queue(agent_id)
