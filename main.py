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

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
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
users: Dict[str, User] = {}      # username → User
dids: List[DID] = []


def _save() -> None:
    storage.save(agent_mgr.list_all(), campaigns, call_mgr.all_calls(),
                 list(users.values()), dids)


def _load_persisted() -> None:
    data = storage.load()
    if data:
        try:
            for a in data.get("agents", []):
                agent_mgr.register(Agent(**a))
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
            logger.info("Loaded persisted state: %d agents, %d campaigns, %d calls, %d users, %d DIDs",
                        len(data.get("agents", [])), len(data.get("campaigns", [])),
                        len(data.get("calls", [])), len(data.get("users", [])),
                        len(data.get("dids", [])))
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
    if event_type in ("call_ended", "campaign_completed", "campaign_stopped"):
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
    if not call or call.campaign_id != "quick":
        return
    if body.startswith("+OK"):
        fs_uuid = body.split()[-1]
        call_mgr.set_fs_uuid(call.id, fs_uuid)
    else:
        call_mgr.on_failed(job_uuid, body)
        await broadcast("call_failed", {"call_id": call.id, "reason": body})
        await broadcast("call_ended", call.model_dump())


async def _global_on_answer(event) -> None:
    fs_uuid = event.unique_id
    logger.info("CHANNEL_ANSWER received fs_uuid=%s", fs_uuid)
    call = call_mgr.by_fs_uuid(fs_uuid)
    logger.info("CHANNEL_ANSWER call lookup → %s", call.id if call else "NOT FOUND")
    if not call or call.campaign_id != "quick":
        return
    call = call_mgr.on_answered(fs_uuid)
    if not call:
        return
    if settings.RECORDING_ENABLED and call.fs_uuid:
        rec_file = f"{settings.RECORDING_DIR}/{call.id}.{settings.RECORDING_FORMAT}"
        try:
            await esl.api(f"uuid_record {call.fs_uuid} start {rec_file}")
            call.recording_path = f"{call.id}.{settings.RECORDING_FORMAT}"
            logger.info("Recording started: %s", rec_file)
        except Exception as exc:
            logger.warning("Recording start failed: %s", exc)

    idle = agent_mgr.get_idle()
    if idle:
        agent = idle[0]
        await agent_mgr.assign_call(agent.id, call.id)
        call.agent_id = agent.id
        try:
            await esl.bridge_to_agent(call.fs_uuid, agent.extension)
            logger.info("Quick-dial bridged %s → agent %s", call.contact.phone, agent.extension)
        except Exception as exc:
            logger.error("Bridge failed: %s", exc)
            await agent_mgr.release_call(agent.id)
            call.agent_id = None
    else:
        logger.warning("Quick-dial: no idle agent for %s — dropping", call.contact.phone)
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
    # Log all SIP-related variables to help diagnose carrier issues
    sip_vars = {k: v for k, v in event.items() if "sip" in k.lower()}
    logger.info("HANGUP fs=%s cause=%s sip_code=%s sip_vars=%s",
                fs_uuid, cause, sip_code, sip_vars)
    call = call_mgr.on_hangup(fs_uuid, cause, sip_code)
    if not call:
        return
    if call.agent_id:
        await agent_mgr.release_call(call.agent_id)
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
        )
        logger.info("FreeSWITCH ESL ready")
    except Exception as exc:
        logger.warning("Could not connect to FreeSWITCH ESL: %s", exc)
    yield
    _save()
    await esl.disconnect()


app = FastAPI(title="AI Predictive Dialer", version="1.0.0", lifespan=lifespan)
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


# ── Admin: User management ─────────────────────────────────────────────────────

@app.get("/admin/users")
async def list_users(payload: dict = Depends(require_admin)):
    return [
        {"id": u.id, "username": u.username, "role": u.role,
         "extension": u.extension, "created_at": u.created_at}
        for u in users.values()
    ]


@app.post("/admin/users")
async def create_user(body: UserCreate, payload: dict = Depends(require_admin)):
    username = body.username.strip().lower()
    if not username:
        raise HTTPException(400, "username required")
    if username in users:
        raise HTTPException(409, "Username already exists")
    user = User(
        username=username,
        password_hash=hash_password(body.password or "1234"),
        role=body.role,
        extension=body.extension,
    )
    users[username] = user
    _save()
    return {"id": user.id, "username": user.username, "role": user.role,
            "extension": user.extension, "created_at": user.created_at}


@app.post("/admin/users/{username}/reset-password")
async def reset_password(username: str, body: dict, payload: dict = Depends(require_admin)):
    user = users.get(username)
    if not user:
        raise HTTPException(404, "User not found")
    new_pw = (body.get("password") or "1234")
    user.password_hash = hash_password(new_pw)
    _save()
    return {"status": "ok"}


@app.delete("/admin/users/{username}")
async def delete_user(username: str, payload: dict = Depends(require_admin)):
    if username not in users:
        raise HTTPException(404, "User not found")
    if username == payload.get("username"):
        raise HTTPException(400, "Cannot delete yourself")
    users.pop(username)
    _save()
    return {"status": "deleted"}


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


# ── Admin: Reports ─────────────────────────────────────────────────────────────

@app.get("/admin/reports/calls")
async def report_calls(payload: dict = Depends(require_admin)):
    return [c.model_dump() for c in call_mgr.all_calls()]


@app.get("/admin/reports/summary")
async def report_summary(payload: dict = Depends(require_admin)):
    calls = call_mgr.all_calls()
    sip_codes: Dict[str, int] = {}
    dispositions: Dict[str, int] = {}
    causes: Dict[str, int] = {}
    for c in calls:
        code = c.sip_code or "—"
        sip_codes[code] = sip_codes.get(code, 0) + 1
        disp = c.disposition or "—"
        dispositions[disp] = dispositions.get(disp, 0) + 1
        cause = c.hangup_cause or "—"
        causes[cause] = causes.get(cause, 0) + 1

    answered = sum(1 for c in calls if c.answer_time)
    return {
        "total":        len(calls),
        "answered":     answered,
        "dropped":      sum(1 for c in calls if c.status == CallStatus.DROPPED),
        "failed":       sum(1 for c in calls if c.status == CallStatus.FAILED),
        "completed":    sum(1 for c in calls if c.status == CallStatus.COMPLETED),
        "sip_codes":    sip_codes,
        "dispositions": dispositions,
        "hangup_causes": causes,
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

    # Allow overriding caller_id with a specific DID
    caller_id = (body or {}).get("caller_id") or settings.CALLER_ID_NUMBER

    engine = DialerEngine(
        esl=esl, agent_mgr=agent_mgr, call_mgr=call_mgr, campaign=c,
        gateway=settings.SIP_GATEWAY, caller_id=caller_id,
        dial_prefix=settings.DIAL_PREFIX, dial_timeout=settings.DIAL_TIMEOUT,
        max_concurrent=settings.MAX_CONCURRENT_CALLS,
        drop_rate_limit=settings.DROP_RATE_LIMIT,
        pacing_interval=settings.PACING_INTERVAL,
        amd_enabled=settings.AMD_ENABLED,
        recording_enabled=settings.RECORDING_ENABLED,
        recording_dir=settings.RECORDING_DIR,
        recording_format=settings.RECORDING_FORMAT,
        on_event=on_dialer_event,
    )
    engines[campaign_id] = engine
    await engine.start()
    return {"status": "running"}


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
    caller_id = (body.get("caller_id") or settings.CALLER_ID_NUMBER).strip()

    contact = Contact(phone=phone, name=body.get("name", "Quick Dial"))
    call = Call(contact=contact, campaign_id="quick", caller_id=caller_id)
    call_mgr.add(call)

    try:
        job_uuid, channel_uuid = await esl.originate(
            phone, settings.SIP_GATEWAY, caller_id, settings.DIAL_TIMEOUT,
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
