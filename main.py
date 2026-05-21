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

from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from agent_manager import AgentManager
from ai_analyzer import AIAnalyzer
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


def _save() -> None:
    """Persist current state to disk."""
    storage.save(agent_mgr.list_all(), campaigns, call_mgr.all_calls())


def _load_persisted() -> None:
    """Restore agents, campaigns and call history from disk on startup."""
    data = storage.load()
    if not data:
        return
    try:
        for a in data.get("agents", []):
            agent = Agent(**a)
            agent_mgr.register(agent)
        for c in data.get("campaigns", []):
            campaign = Campaign(**c)
            campaigns[campaign.id] = campaign
        for c in data.get("calls", []):
            call = Call(**c)
            call_mgr.add(call)
        logger.info("Loaded persisted state: %d agents, %d campaigns, %d calls",
                    len(data.get("agents", [])),
                    len(data.get("campaigns", [])),
                    len(data.get("calls", [])))
    except Exception as exc:
        logger.error("Failed to restore persisted state: %s", exc)


# ── WebSocket broadcast ────────────────────────────────────────────────────────

def _json_default(obj):
    """Handle datetime and other non-serializable types."""
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

    # Save state whenever a call ends or campaign changes
    if event_type in ("call_ended", "campaign_completed", "campaign_stopped"):
        _save()

    # Post-call AI analysis when a call ends with an agent
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
            "call_id": call_id,
            "summary": summary,
            "sentiment": sentiment,
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
        return   # campaign calls handled by DialerEngine
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
        return   # campaign calls handled by DialerEngine
    call = call_mgr.on_answered(fs_uuid)
    if not call:
        return
    # Start recording
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
        return   # campaign calls handled by DialerEngine
    cause = event.get("Hangup-Cause", "")
    call = call_mgr.on_hangup(fs_uuid, cause)
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
    # Restore persisted state before accepting requests
    _load_persisted()

    try:
        await esl.connect()
        await esl.subscribe(
            "CHANNEL_CREATE",
            "CHANNEL_ANSWER",
            "CHANNEL_HANGUP",
            "CHANNEL_BRIDGE",
            "CHANNEL_UNBRIDGE",
            "BACKGROUND_JOB",
            "CUSTOM",
        )
        logger.info("FreeSWITCH ESL ready")
    except Exception as exc:
        logger.warning("Could not connect to FreeSWITCH ESL: %s", exc)
        logger.warning("Running without ESL — use mock mode for testing")

    yield

    _save()   # save on clean shutdown
    await esl.disconnect()


app = FastAPI(title="AI Predictive Dialer", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="frontend"), name="static")


# ── Dashboard root ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("frontend/index.html") as f:
        return f.read()


# ── Campaign endpoints ─────────────────────────────────────────────────────────

@app.post("/campaigns", response_model=Campaign)
async def create_campaign(body: CampaignCreate):
    c = Campaign(name=body.name)
    campaigns[c.id] = c
    _save()
    return c


@app.get("/campaigns", response_model=List[Campaign])
async def list_campaigns():
    return list(campaigns.values())


@app.get("/campaigns/{campaign_id}", response_model=Campaign)
async def get_campaign(campaign_id: str):
    c = campaigns.get(campaign_id)
    if not c:
        raise HTTPException(404, "Campaign not found")
    return c


@app.post("/campaigns/{campaign_id}/upload")
async def upload_contacts(campaign_id: str, file: UploadFile):
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
async def reset_campaign(campaign_id: str):
    """Mark all contacts as un-dialed and clear stats so the list can be run again."""
    c = campaigns.get(campaign_id)
    if not c:
        raise HTTPException(404, "Campaign not found")

    # Stop the engine if it's running
    engine = engines.get(campaign_id)
    if engine:
        await engine.stop()
        engines.pop(campaign_id, None)

    # Release all agents back to idle immediately so next Start works right away
    await agent_mgr.release_all_to_idle()

    # Un-dial every contact
    for contact in c.contacts:
        contact.dialed = False
        contact.dialed_at = None
        contact.result = None

    # Clear all stats
    from models import CampaignStats
    c.stats = CampaignStats(
        contacts_total=len(c.contacts),
        contacts_remaining=len(c.contacts),
    )
    c.status = CampaignStatus.IDLE
    c.started_at = None
    c.completed_at = None

    await broadcast("campaign_reset", {
        "id": c.id,
        "name": c.name,
        "status": c.status,
        "stats": c.stats.model_dump(),
    })
    return {"status": "reset", "contacts": len(c.contacts)}


@app.post("/campaigns/{campaign_id}/start")
async def start_campaign(campaign_id: str):
    c = campaigns.get(campaign_id)
    if not c:
        raise HTTPException(404, "Campaign not found")
    if not c.contacts:
        raise HTTPException(400, "No contacts loaded")

    engine = DialerEngine(
        esl=esl,
        agent_mgr=agent_mgr,
        call_mgr=call_mgr,
        campaign=c,
        gateway=settings.SIP_GATEWAY,
        caller_id=settings.CALLER_ID_NUMBER,
        dial_prefix=settings.DIAL_PREFIX,
        dial_timeout=settings.DIAL_TIMEOUT,
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
async def pause_campaign(campaign_id: str):
    engine = engines.get(campaign_id)
    if not engine:
        raise HTTPException(404, "Campaign not running")
    await engine.pause()
    return {"status": "paused"}


@app.post("/campaigns/{campaign_id}/resume")
async def resume_campaign(campaign_id: str):
    engine = engines.get(campaign_id)
    if not engine:
        raise HTTPException(404, "Campaign not running")
    await engine.resume()
    return {"status": "running"}


@app.post("/campaigns/{campaign_id}/stop")
async def stop_campaign(campaign_id: str):
    engine = engines.get(campaign_id)
    if not engine:
        raise HTTPException(404, "Campaign not running")
    await engine.stop()
    engines.pop(campaign_id, None)
    return {"status": "stopped"}


# ── Agent endpoints ────────────────────────────────────────────────────────────

@app.post("/agents", response_model=Agent)
async def create_agent(body: AgentCreate):
    agent = Agent(name=body.name, extension=body.extension)
    agent_mgr.register(agent)
    _save()
    return agent


@app.get("/agents", response_model=List[Agent])
async def list_agents():
    return agent_mgr.list_all()


@app.post("/agents/login")
async def agent_login(body: AgentLogin):
    ok = await agent_mgr.login(body.agent_id)
    if not ok:
        raise HTTPException(404, "Agent not found")
    return {"status": "idle"}


@app.post("/agents/logout")
async def agent_logout(body: AgentLogin):
    await agent_mgr.logout(body.agent_id)
    return {"status": "offline"}


@app.post("/agents/break")
async def agent_break(body: AgentLogin):
    ok = await agent_mgr.set_break(body.agent_id)
    if not ok:
        raise HTTPException(400, "Cannot set break from current status")
    return {"status": "break"}


@app.post("/agents/return")
async def agent_return(body: AgentLogin):
    ok = await agent_mgr.return_from_break(body.agent_id)
    if not ok:
        raise HTTPException(400, "Agent is not on break")
    return {"status": "idle"}


# ── Call endpoints ─────────────────────────────────────────────────────────────

@app.get("/calls")
async def list_calls():
    return [c.model_dump() for c in call_mgr.all_calls()]


@app.post("/calls/{call_id}/hangup")
async def hangup_call(call_id: str):
    """Hang up an active call from the dashboard."""
    call = call_mgr.get(call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    if not call.fs_uuid:
        raise HTTPException(400, "Call has no FreeSWITCH UUID yet")
    await esl.hangup(call.fs_uuid)
    return {"status": "hangup sent"}


@app.get("/recordings/{filename}")
async def serve_recording(filename: str, download: bool = False):
    """Stream or download a call recording file.

    Add ?download=true to get an attachment download instead of inline playback.
    """
    # Security: only allow safe filenames (UUID + extension)
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
async def quick_dial(body: dict):
    """Dial a single number immediately — useful for testing."""
    phone = (body.get("phone") or "").strip()
    if not phone:
        raise HTTPException(400, "phone required")

    contact = Contact(phone=phone, name=body.get("name", "Quick Dial"))
    call = Call(contact=contact, campaign_id="quick")
    call_mgr.add(call)

    try:
        job_uuid, channel_uuid = await esl.originate(
            phone,
            settings.SIP_GATEWAY,
            settings.CALLER_ID_NUMBER,
            settings.DIAL_TIMEOUT,
        )
        call.fs_job_uuid = job_uuid
        call_mgr._by_job_uuid[job_uuid] = call.id
        # Pre-map channel UUID so CHANNEL_ANSWER is never missed
        call_mgr.set_fs_uuid(call.id, channel_uuid)
        call.status = CallStatus.RINGING
        await broadcast("call_dialing", call.model_dump())
        logger.info("Quick dial: %s job=%s uuid=%s", phone, job_uuid, channel_uuid)
        return {"status": "dialing", "call_id": call.id, "job_uuid": job_uuid}
    except Exception as exc:
        call.status = CallStatus.FAILED
        call_mgr._calls.pop(call.id, None)
        raise HTTPException(500, str(exc))


@app.post("/calls/disposition")
async def set_disposition(body: AgentDisposition):
    call = call_mgr.set_disposition(body.call_id, body.disposition,
                                    body.notes or "")
    if not call:
        raise HTTPException(404, "Call not found")
    await broadcast("call_disposition", {"call_id": call.id,
                                          "disposition": call.disposition})
    return {"status": "ok"}


@app.post("/calls/{call_id}/ai-script")
async def get_ai_script(call_id: str):
    call = call_mgr.get(call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    script = await ai.generate_script_suggestion(
        contact_name=call.contact.name,
        product="our service",
    )
    return {"script": script}


# ── WebSocket ──────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def admin_ws(ws: WebSocket):
    await ws.accept()
    admin_ws_clients.append(ws)

    # Send current snapshot including call history
    terminal = {CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.DROPPED}
    history = [c.model_dump() for c in call_mgr.all_calls()
               if c.status in terminal]
    snapshot = {
        "agents":       [a.model_dump() for a in agent_mgr.list_all()],
        "campaigns":    [c.model_dump(exclude={"contacts"}) for c in campaigns.values()],
        "active_calls": [c.model_dump() for c in call_mgr.active()],
        "history":      history[-200:],   # last 200 calls
    }
    await ws.send_text(json.dumps({"type": "snapshot", "data": snapshot}, default=_json_default))

    try:
        while True:
            await ws.receive_text()   # keep-alive; client can send pings
    except WebSocketDisconnect:
        admin_ws_clients.remove(ws)


@app.websocket("/ws/agent/{agent_id}")
async def agent_ws(ws: WebSocket, agent_id: str):
    """Individual agent WebSocket for call assignments and push messages."""
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
