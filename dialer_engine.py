"""
Predictive Dialer Engine
────────────────────────
Algorithm:
  dial_rate  = ceil(idle_agents / max(answer_rate, 0.1))
  lines_free = max_concurrent - active_calls
  to_dial    = min(lines_free, dial_rate)

If drop_rate exceeds the limit the engine reduces the dial rate by 1
until the drop rate recovers.  If no agents are idle, no calls are placed.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime
from typing import Callable, Coroutine, List, Optional

from agent_manager import AgentManager
from call_manager import CallManager
from esl_client import ESLClient, ESLEvent
from models import (
    AMDResult,
    Call,
    CallStatus,
    Campaign,
    CampaignStatus,
    Contact,
)

logger = logging.getLogger(__name__)


class DialerEngine:
    def __init__(
        self,
        esl: ESLClient,
        agent_mgr: AgentManager,
        call_mgr: CallManager,
        campaign: Campaign,
        *,
        gateway: str,
        caller_id: str,
        dial_prefix: str = "",
        dial_timeout: int = 30,
        max_concurrent: int = 10,
        drop_rate_limit: float = 0.03,
        pacing_interval: float = 5.0,
        amd_enabled: bool = True,
        recording_enabled: bool = False,
        recording_dir: str = "/var/lib/freeswitch/recordings",
        recording_format: str = "wav",
        on_event: Optional[Callable[[str, dict], Coroutine]] = None,
    ) -> None:
        self.esl = esl
        self.agent_mgr = agent_mgr
        self.call_mgr = call_mgr
        self.campaign = campaign
        self.gateway = gateway
        self.caller_id = caller_id
        self.dial_prefix = dial_prefix   # e.g. "4164#" for Telcast carrier
        self.dial_timeout = dial_timeout
        self.max_concurrent = max_concurrent
        self.drop_rate_limit = drop_rate_limit
        self.pacing_interval = pacing_interval
        self.amd_enabled = amd_enabled
        self.recording_enabled = recording_enabled
        self.recording_dir = recording_dir
        self.recording_format = recording_format
        self._on_event = on_event

        self._contact_queue: List[Contact] = []
        self._dial_slowdown = 0   # penalty counter when drop rate is high
        self._pacing_task: Optional[asyncio.Task] = None

        # Register ESL event handlers
        esl.add_handler("BACKGROUND_JOB", self._on_background_job)
        esl.add_handler("CHANNEL_ANSWER", self._on_channel_answer)
        esl.add_handler("CHANNEL_HANGUP", self._on_channel_hangup)
        esl.add_handler("CHANNEL_BRIDGE", self._on_channel_bridge)
        esl.add_handler("CUSTOM", self._on_custom)  # AMD events

    # ── Campaign lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if self.campaign.status == CampaignStatus.RUNNING:
            return
        self.campaign.status = CampaignStatus.RUNNING
        self.campaign.started_at = datetime.utcnow()
        self._contact_queue = [c for c in self.campaign.contacts if not c.dialed]
        self._dial_slowdown = 0
        # Reset stats so stale numbers from previous runs don't poison pacing
        from models import CampaignStats
        self.campaign.stats = CampaignStats(contacts_total=len(self.campaign.contacts))
        self._update_stats()
        self._pacing_task = asyncio.create_task(self._pacing_loop())
        logger.info("Campaign '%s' started. Contacts: %d", self.campaign.name,
                    len(self._contact_queue))
        await self._emit("campaign_started", self.campaign.model_dump(exclude={"contacts"}))

    async def pause(self) -> None:
        if self.campaign.status != CampaignStatus.RUNNING:
            return
        self.campaign.status = CampaignStatus.PAUSED
        if self._pacing_task:
            self._pacing_task.cancel()
        await self._emit("campaign_paused", {"id": self.campaign.id})

    async def resume(self) -> None:
        if self.campaign.status != CampaignStatus.PAUSED:
            return
        self.campaign.status = CampaignStatus.RUNNING
        self._pacing_task = asyncio.create_task(self._pacing_loop())
        await self._emit("campaign_resumed", {"id": self.campaign.id})

    async def stop(self) -> None:
        self.campaign.status = CampaignStatus.IDLE
        if self._pacing_task:
            self._pacing_task.cancel()
        # Hang up + close all active calls so stale ESL events are ignored
        for call in self.call_mgr.active():
            call.status = CallStatus.COMPLETED   # mark first → handler ignores it
            if call.fs_uuid:
                try:
                    await self.esl.hangup(call.fs_uuid)
                except Exception:
                    pass
        # Remove this engine's handlers from ESL so a new engine starts clean
        for event_name, handler in [
            ("BACKGROUND_JOB", self._on_background_job),
            ("CHANNEL_ANSWER",  self._on_channel_answer),
            ("CHANNEL_HANGUP",  self._on_channel_hangup),
            ("CHANNEL_BRIDGE",  self._on_channel_bridge),
            ("CUSTOM",          self._on_custom),
        ]:
            handlers = self.esl._event_handlers.get(event_name, [])
            if handler in handlers:
                handlers.remove(handler)
        await self._emit("campaign_stopped", {"id": self.campaign.id})

    # ── Pacing loop ────────────────────────────────────────────────────────────

    async def _pacing_loop(self) -> None:
        while self.campaign.status == CampaignStatus.RUNNING:
            try:
                await self._maybe_dial()
            except Exception as exc:
                logger.error("Pacing error: %s", exc, exc_info=True)

            # Check completion INSIDE the loop — all contacts dialed + no active calls
            if not self._contact_queue and self.call_mgr.active_count() == 0:
                self.campaign.status = CampaignStatus.COMPLETED
                self.campaign.completed_at = datetime.utcnow()
                await self._emit("campaign_completed", {
                    "id":     self.campaign.id,
                    "status": self.campaign.status,
                    "stats":  self.campaign.stats.model_dump(),
                })
                break

            await asyncio.sleep(self.pacing_interval)

    async def _maybe_dial(self) -> None:
        if not self._contact_queue:
            return

        idle_agents = self.agent_mgr.get_idle()
        if not idle_agents:
            logger.debug("No idle agents — holding dial")
            return

        active = self.call_mgr.active_count()
        lines_free = self.max_concurrent - active
        if lines_free <= 0:
            return

        # Predictive rate
        ans_rate = max(self.campaign.stats.answer_rate, 0.1)
        dial_rate = math.ceil(len(idle_agents) / ans_rate)

        # Apply drop-rate penalty
        if self.campaign.stats.drop_rate > self.drop_rate_limit:
            self._dial_slowdown = min(self._dial_slowdown + 1, dial_rate)
        else:
            self._dial_slowdown = max(self._dial_slowdown - 1, 0)

        to_dial = min(lines_free, dial_rate - self._dial_slowdown)
        to_dial = max(to_dial, 0)

        logger.debug(
            "Pacing: idle=%d active=%d rate=%.2f slowdown=%d to_dial=%d",
            len(idle_agents), active, ans_rate, self._dial_slowdown, to_dial,
        )

        for _ in range(to_dial):
            if not self._contact_queue:
                break
            contact = self._contact_queue.pop(0)
            await self._dial(contact)

    async def _dial(self, contact: Contact) -> None:
        contact.dialed = True
        contact.dialed_at = datetime.utcnow()

        call = Call(contact=contact, campaign_id=self.campaign.id, caller_id=self.caller_id)
        self.call_mgr.add(call)

        try:
            # Prepend carrier route prefix (e.g. "4164#") if configured
            dial_number = f"{self.dial_prefix}{contact.phone}"
            job_uuid, channel_uuid = await self.esl.originate(
                dial_number, self.gateway, self.caller_id, self.dial_timeout
            )
            call.fs_job_uuid = job_uuid
            self.call_mgr._by_job_uuid[job_uuid] = call.id
            # Pre-map channel UUID so CHANNEL_ANSWER is never missed
            self.call_mgr.set_fs_uuid(call.id, channel_uuid)
            call.status = CallStatus.RINGING
            logger.info("Dialing %s job=%s uuid=%s", contact.phone, job_uuid, channel_uuid)
            self.campaign.stats.contacts_dialed += 1
            self._update_stats()
            await self._emit("call_dialing", call.model_dump())

        except Exception as exc:
            call.status = CallStatus.FAILED
            call.end_time = datetime.utcnow()
            self.campaign.stats.calls_failed += 1
            logger.error("Originate failed for %s: %s", contact.phone, exc)

    # ── ESL event handlers ─────────────────────────────────────────────────────

    async def _on_background_job(self, event: ESLEvent) -> None:
        job_uuid = event.job_uuid
        body = event.get("body", "").strip()

        call = self.call_mgr.by_job_uuid(job_uuid)
        if not call:
            return

        if body.startswith("+OK"):
            result_uuid = body.split()[-1]
            if call.fs_uuid and result_uuid != call.fs_uuid and call.agent_fs_uuid is None:
                # Bridge job returned — agent (Zoiper) answered and &bridge() connected audio.
                # FreeSWITCH already joined the channels; do NOT call uuid_bridge again.
                # Just register the agent UUID so CHANNEL_HANGUP can find the call.
                call.agent_fs_uuid = result_uuid
                self.call_mgr._by_fs_uuid[result_uuid] = call.id
                logger.info("Agent bridged (BACKGROUND_JOB): agent_uuid=%s carrier=%s",
                            result_uuid, call.fs_uuid)
                # CHANNEL_BRIDGE will fire and call on_bridged / emit call_bridged
            else:
                # Originate job — confirm/update carrier UUID
                self.call_mgr.set_fs_uuid(call.id, result_uuid)
                logger.debug("BACKGROUND_JOB: call %s → fs_uuid %s", call.id, result_uuid)
        else:
            # Originate failed
            self.call_mgr.on_failed(job_uuid, body)
            self.campaign.stats.calls_failed += 1
            self._update_stats()
            await self._emit("call_failed", {"call_id": call.id, "reason": body})

    async def _on_channel_answer(self, event: ESLEvent) -> None:
        fs_uuid = event.unique_id
        logger.info("ENGINE CHANNEL_ANSWER fs_uuid=%s", fs_uuid)
        call = self.call_mgr.on_answered(fs_uuid)
        if not call:
            logger.info("ENGINE CHANNEL_ANSWER: no call found for %s", fs_uuid)
            return

        self.campaign.stats.calls_answered += 1
        self._update_stats()
        logger.info("Answered: %s (fs_uuid=%s)", call.contact.phone, fs_uuid)

        # Start call recording
        if self.recording_enabled and fs_uuid:
            rec_file = f"{self.recording_dir}/{call.id}.{self.recording_format}"
            try:
                await self.esl.api(f"uuid_record {fs_uuid} start {rec_file}")
                call.recording_path = f"{call.id}.{self.recording_format}"
                logger.info("Recording started: %s", rec_file)
            except Exception as exc:
                logger.warning("Recording failed to start: %s", exc)

        if self.amd_enabled:
            # FreeSWITCH mod_amd fires CUSTOM::sofia::amd — handled in _on_custom
            # For now, assume human and bridge
            call.amd_result = AMDResult.HUMAN
            await self._bridge_call(call)
        else:
            call.amd_result = AMDResult.HUMAN
            await self._bridge_call(call)

        await self._emit("call_answered", call.model_dump())

    async def _on_channel_hangup(self, event: ESLEvent) -> None:
        fs_uuid = event.unique_id
        cause = event.get("Hangup-Cause", "")
        sip_code = (
            event.get("variable_sip_term_status") or
            event.get("variable_sip_invite_failure_status") or
            event.get("variable_sip_term_cause") or ""
        )
        sip_vars = {k: v for k, v in event.items() if "sip" in k.lower()}
        logger.info("HANGUP fs=%s cause=%s sip_code=%s sip_vars=%s",
                    fs_uuid, cause, sip_code, sip_vars)

        # ── Agent (Zoiper) leg hung up ─────────────────────────────────────────
        # When Zoiper drops, the carrier leg stays alive in FreeSWITCH.
        # Kill it explicitly so the call ends and the dashboard updates.
        call = self.call_mgr.by_fs_uuid(fs_uuid)
        if call and fs_uuid == call.agent_fs_uuid:
            logger.info("Agent leg %s hung up → killing carrier leg %s", fs_uuid, call.fs_uuid)
            if call.fs_uuid:
                try:
                    await self.esl.hangup(call.fs_uuid)
                except Exception as exc:
                    logger.warning("Could not hang up carrier leg: %s", exc)
            # The CHANNEL_HANGUP for the carrier leg will arrive and do final cleanup
            return

        # ── Carrier leg hung up (normal path) ─────────────────────────────────
        # Stop recording before closing the call
        if self.recording_enabled and fs_uuid:
            try:
                await self.esl.api(f"uuid_record {fs_uuid} stop all")
            except Exception:
                pass
        call = self.call_mgr.on_hangup(fs_uuid, cause, sip_code)
        if not call:
            return

        if call.status == CallStatus.DROPPED:
            self.campaign.stats.calls_dropped += 1

        # Release the agent
        if call.agent_id:
            await self.agent_mgr.release_call(call.agent_id)

        self._update_stats()
        logger.info("Hangup: %s cause=%s status=%s", call.contact.phone, cause, call.status)
        await self._emit("call_ended", call.model_dump())

    async def _on_channel_bridge(self, event: ESLEvent) -> None:
        fs_uuid = event.unique_id
        call = self.call_mgr.by_fs_uuid(fs_uuid)
        if call and call.agent_id:
            self.call_mgr.on_bridged(fs_uuid, call.agent_id)
            await self._emit("call_bridged", {"call_id": call.id, "agent_id": call.agent_id})

    async def _on_custom(self, event: ESLEvent) -> None:
        subclass = event.get("Event-Subclass", "")
        if "amd" not in subclass.lower():
            return

        fs_uuid = event.unique_id
        amd_result = event.get("AMD-Result", "UNKNOWN").lower()
        result = AMDResult.HUMAN if "human" in amd_result else (
            AMDResult.MACHINE if "machine" in amd_result else AMDResult.UNKNOWN
        )
        call = self.call_mgr.on_amd_result(fs_uuid, result)
        if not call:
            return

        if result == AMDResult.MACHINE:
            self.campaign.stats.calls_machine += 1
            self._update_stats()
            await self.esl.hangup(fs_uuid)
            logger.info("AMD: machine detected → hanging up %s", call.contact.phone)
        else:
            await self._bridge_call(call)

    # ── Bridging ───────────────────────────────────────────────────────────────

    async def _bridge_call(self, call: Call) -> None:
        all_agents = self.agent_mgr.list_all()
        idle = self.agent_mgr.get_idle()
        logger.info("_bridge_call: phone=%s fs_uuid=%s total_agents=%d idle=%d",
                    call.contact.phone, call.fs_uuid, len(all_agents), len(idle))
        if not idle:
            # No agents available — this call becomes a drop
            if call.fs_uuid:
                await self.esl.hangup(call.fs_uuid)
            call.status = CallStatus.DROPPED
            self.campaign.stats.calls_dropped += 1
            self._update_stats()
            logger.warning("No idle agent for %s — dropping (agents=%s)",
                           call.contact.phone, [(a.name, a.status) for a in all_agents])
            return

        agent = idle[0]
        logger.info("Assigning agent %s (ext=%s) to call %s", agent.name, agent.extension, call.id)
        await self.agent_mgr.assign_call(agent.id, call.id)
        call.agent_id = agent.id

        try:
            job = await self.esl.bridge_to_agent(call.fs_uuid, agent.extension, call.contact.phone)
            self.call_mgr._by_job_uuid[job] = call.id   # register so BACKGROUND_JOB finds bridge +OK
            logger.info("Bridge dispatched job=%s: %s → agent %s (%s)",
                        job, call.contact.phone, agent.name, agent.extension)
        except Exception as exc:
            logger.error("Bridge failed: %s", exc, exc_info=True)
            await self.agent_mgr.release_call(agent.id)
            call.agent_id = None

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _update_stats(self) -> None:
        s = self.campaign.stats
        s.contacts_total = len(self.campaign.contacts)
        s.contacts_remaining = len(self._contact_queue)
        dialed = s.contacts_dialed
        if dialed > 0:
            s.answer_rate = s.calls_answered / dialed
            s.drop_rate = s.calls_dropped / dialed
        # Push live stats to dashboard
        asyncio.create_task(self._emit("campaign_update", {
            "id":     self.campaign.id,
            "status": self.campaign.status,
            "stats":  self.campaign.stats.model_dump(),
        }))

    async def _emit(self, event_type: str, data: dict) -> None:
        if self._on_event:
            try:
                await self._on_event(event_type, data)
            except Exception as exc:
                logger.error("Event emit error: %s", exc)
