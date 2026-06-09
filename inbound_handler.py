"""
Inbound call routing + tracking — FULLY ISOLATED from the outbound dialer.

When an external caller dials a purchased DID, the carrier delivers the call to
FreeSWITCH's `public` context. The dedicated inbound dialplan
(server_setup/02_inbound.xml) parks the call and tags it with the channel
variable `callingio_inbound=true`, then this module takes over:

    1. CHANNEL_PARK fires with `variable_callingio_inbound=true`.
    2. We read the dialed DID, normalise it, and look up which user owns it.
    3. We ring that user's SIP extension (`user/<ext>`) — the same registered
       endpoint the callingio mobile app uses — and bridge on answer.
    4. We record the call lifecycle (ringing → answered/missed → ended) in an
       in-memory log so the admin dashboard can show inbound call tracking.

Design rules (do NOT break outbound):
    * This module registers its OWN ESL handlers (CHANNEL_PARK / ANSWER / HANGUP)
      and only ever acts on inbound UUIDs it is tracking. ESL dispatches every
      handler for an event, so these coexist with the outbound handlers without
      modifying them.
    * It NEVER touches the outbound quick-dial / campaign handlers in main.py.
    * It strictly filters on `callingio_inbound=true`, so the `&park()` used by
      outbound carrier legs is ignored.
    * The owner lookup is injected as a callable, so this file has no knowledge
      of main.py globals.
"""

from __future__ import annotations

import logging
import re
import uuid as uuidlib
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from esl_client import ESLClient, ESLEvent

logger = logging.getLogger(__name__)

_MAX_LOG = 500   # cap the in-memory inbound call log


def normalize_number(num: str) -> str:
    """Reduce a phone number to bare digits, dropping a leading US '1'."""
    digits = re.sub(r"\D", "", num or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


# owner_lookup(dialed_digits) -> (extension, username) that should ring, or None
OwnerLookup = Callable[[str], Optional[Tuple[str, Optional[str]]]]


class InboundCall:
    """One tracked inbound call (carrier leg)."""

    def __init__(self, fs_uuid: str, caller: str, did: str,
                 owner_username: Optional[str], extension: Optional[str]) -> None:
        self.id: str = str(uuidlib.uuid4())
        self.fs_uuid: str = fs_uuid
        self.caller: str = caller
        self.did: str = did
        self.owner_username: Optional[str] = owner_username
        self.extension: Optional[str] = extension
        # ringing | answered | missed | completed | rejected | failed
        self.status: str = "ringing"
        self.start_time: datetime = datetime.now(timezone.utc)
        self.answer_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self.duration: int = 0           # talk seconds (answer → end)
        self.hangup_cause: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "caller": self.caller,
            "did": self.did,
            "owner_username": self.owner_username,
            "extension": self.extension,
            "status": self.status,
            "start_time": self.start_time.isoformat(),
            "answer_time": self.answer_time.isoformat() if self.answer_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration": self.duration,
            "hangup_cause": self.hangup_cause,
        }


class InboundRouter:
    """Routes inbound DID calls to the owning user's SIP extension and tracks them."""

    def __init__(self, esl: ESLClient, owner_lookup: OwnerLookup) -> None:
        self._esl = esl
        self._owner_lookup = owner_lookup
        # fs_uuid → InboundCall, insertion-ordered, capped at _MAX_LOG
        self._calls: "OrderedDict[str, InboundCall]" = OrderedDict()

    # ── Registration ────────────────────────────────────────────────────────────
    def register(self) -> None:
        """Attach handlers to the shared ESL client."""
        self._esl.add_handler("CHANNEL_PARK", self._on_park)
        self._esl.add_handler("CHANNEL_ANSWER", self._on_answer)
        self._esl.add_handler("CHANNEL_HANGUP", self._on_hangup)
        logger.info("InboundRouter: handlers registered (inbound calling enabled)")

    # ── Read API (for the admin dashboard) ───────────────────────────────────────
    def recent_calls(self, limit: int = 200) -> List[dict]:
        """Most-recent-first list of tracked inbound calls."""
        calls = list(self._calls.values())[-limit:]
        return [c.to_dict() for c in reversed(calls)]

    def stats(self) -> dict:
        calls = list(self._calls.values())
        total = len(calls)
        answered = sum(1 for c in calls if c.status in ("answered", "completed"))
        missed = sum(1 for c in calls if c.status in ("missed", "rejected", "failed"))
        live = sum(1 for c in calls if c.status in ("ringing", "answered"))
        today = datetime.now(timezone.utc).date()
        today_count = sum(1 for c in calls if c.start_time.date() == today)
        return {
            "total": total,
            "answered": answered,
            "missed": missed,
            "live": live,
            "today": today_count,
        }

    # ── Internal helpers ─────────────────────────────────────────────────────────
    def _track(self, call: InboundCall) -> None:
        self._calls[call.fs_uuid] = call
        while len(self._calls) > _MAX_LOG:
            self._calls.popitem(last=False)

    # ── Event handlers ───────────────────────────────────────────────────────────
    async def _on_park(self, event: ESLEvent) -> None:
        # Only handle calls our inbound dialplan explicitly flagged. Outbound
        # carrier legs are also parked but never carry this variable.
        if event.get("variable_callingio_inbound") != "true":
            return

        fs_uuid = event.unique_id
        dest = (
            event.get("Caller-Destination-Number")
            or event.get("variable_sip_to_user")
            or event.get("variable_sip_req_user")
            or ""
        )
        caller = event.get("Caller-Caller-ID-Number") or "Unknown"
        digits = normalize_number(dest)
        owner_info = self._owner_lookup(digits)
        ext = owner_info[0] if owner_info else None
        owner = owner_info[1] if owner_info else None

        call = InboundCall(fs_uuid, caller, dest, owner, ext)
        self._track(call)

        if not ext:
            call.status = "rejected"
            call.end_time = datetime.now(timezone.utc)
            logger.info(
                "Inbound call to %s (%s) from %s: no active owner/extension → rejecting",
                dest, digits, caller,
            )
            try:
                await self._esl.hangup(fs_uuid, "CALL_REJECTED")
            except Exception as exc:
                logger.error("Inbound reject hangup failed: %s", exc)
            return

        logger.info(
            "Inbound call from %s → DID %s (%s) owned by ext %s; ringing app",
            caller, dest, digits, ext,
        )
        try:
            await self._esl.api(f"uuid_setvar {fs_uuid} effective_caller_id_number {caller}")
            await self._esl.api(f"uuid_setvar {fs_uuid} hangup_after_bridge true")
            # Ring the registered SIP user. FreeSWITCH negotiates WebRTC/G.711
            # natively — no codec pinning so the WebRTC app can use Opus.
            resp = await self._esl.api(f"uuid_transfer {fs_uuid} 'bridge:user/{ext}' inline")
            logger.info("Inbound transfer to user/%s → %s", ext, resp.strip())
        except Exception as exc:
            call.status = "failed"
            call.end_time = datetime.now(timezone.utc)
            logger.error("Inbound routing failed for ext %s: %s", ext, exc)
            try:
                await self._esl.hangup(fs_uuid, "NORMAL_TEMPORARY_FAILURE")
            except Exception:
                pass

    async def _on_answer(self, event: ESLEvent) -> None:
        call = self._calls.get(event.unique_id)
        if not call or call.status != "ringing":
            return
        call.status = "answered"
        call.answer_time = datetime.now(timezone.utc)
        logger.info("Inbound call %s answered by ext %s", call.caller, call.extension)

    async def _on_hangup(self, event: ESLEvent) -> None:
        call = self._calls.get(event.unique_id)
        if not call:
            return
        if call.end_time is None:
            call.end_time = datetime.now(timezone.utc)
        call.hangup_cause = event.get("Hangup-Cause") or call.hangup_cause
        if call.answer_time:
            call.duration = int((call.end_time - call.answer_time).total_seconds())
            call.status = "completed"
        elif call.status == "ringing":
            # Rang but never answered before hangup
            call.status = "missed"
        logger.info(
            "Inbound call %s ended (%s, %ds, cause=%s)",
            call.caller, call.status, call.duration, call.hangup_cause,
        )
