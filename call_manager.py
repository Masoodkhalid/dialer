from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from models import Call, CallStatus, AMDResult

logger = logging.getLogger(__name__)


class CallManager:
    def __init__(self) -> None:
        self._calls: Dict[str, Call] = {}          # call.id → Call
        self._by_fs_uuid: Dict[str, str] = {}       # fs_uuid → call.id
        self._by_job_uuid: Dict[str, str] = {}      # job_uuid → call.id

    # ── Registration ───────────────────────────────────────────────────────────

    def add(self, call: Call) -> Call:
        self._calls[call.id] = call
        if call.fs_job_uuid:
            self._by_job_uuid[call.fs_job_uuid] = call.id
        return call

    def set_fs_uuid(self, call_id: str, fs_uuid: str) -> None:
        call = self._calls.get(call_id)
        if call:
            call.fs_uuid = fs_uuid
            self._by_fs_uuid[fs_uuid] = call_id

    # ── Lookups ────────────────────────────────────────────────────────────────

    def get(self, call_id: str) -> Optional[Call]:
        return self._calls.get(call_id)

    def by_fs_uuid(self, fs_uuid: str) -> Optional[Call]:
        cid = self._by_fs_uuid.get(fs_uuid)
        return self._calls.get(cid) if cid else None

    def by_job_uuid(self, job_uuid: str) -> Optional[Call]:
        cid = self._by_job_uuid.get(job_uuid)
        return self._calls.get(cid) if cid else None

    def active(self) -> List[Call]:
        terminal = {CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.DROPPED}
        return [c for c in self._calls.values() if c.status not in terminal]

    def active_count(self) -> int:
        return len(self.active())

    def all_calls(self) -> List[Call]:
        return list(self._calls.values())

    # ── State transitions ──────────────────────────────────────────────────────

    def on_answered(self, fs_uuid: str) -> Optional[Call]:
        call = self.by_fs_uuid(fs_uuid)
        terminal = {CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.DROPPED}
        if call and call.status not in terminal:
            call.status = CallStatus.ANSWERED
            call.answer_time = datetime.utcnow()
            return call
        return None

    def on_amd_result(self, fs_uuid: str, result: AMDResult) -> Optional[Call]:
        call = self.by_fs_uuid(fs_uuid)
        if call:
            call.amd_result = result
        return call

    def on_bridged(self, fs_uuid: str, agent_id: str) -> Optional[Call]:
        call = self.by_fs_uuid(fs_uuid)
        if call:
            call.status = CallStatus.BRIDGED
            call.bridge_time = datetime.utcnow()
            call.agent_id = agent_id
        return call

    def on_hangup(self, fs_uuid: str, cause: str = "", sip_code: str = "") -> Optional[Call]:
        call = self.by_fs_uuid(fs_uuid)
        terminal = {CallStatus.COMPLETED, CallStatus.FAILED}
        if not call or call.status in terminal:
            return None
        call.end_time = datetime.utcnow()
        if cause:
            call.hangup_cause = cause
        if sip_code:
            call.sip_code = sip_code
        if call.answer_time:
            call.duration = int((call.end_time - call.answer_time).total_seconds())

        if call.status in (CallStatus.DIALING, CallStatus.RINGING, CallStatus.ANSWERED, CallStatus.AMD_CHECK):
            call.status = CallStatus.DROPPED
            if not call.disposition:
                call.disposition = "no-answer"
        else:
            call.status = CallStatus.COMPLETED
            if not call.disposition:
                call.disposition = "answered"
        return call

    def on_failed(self, job_uuid: str, reason: str = "") -> Optional[Call]:
        call = self.by_job_uuid(job_uuid)
        if call:
            call.status = CallStatus.FAILED
            call.end_time = datetime.utcnow()
            logger.debug("Call failed job=%s reason=%s", job_uuid, reason)
        return call

    def set_disposition(self, call_id: str, disposition: str, notes: str = "") -> Optional[Call]:
        call = self._calls.get(call_id)
        if call:
            call.disposition = disposition
        return call

    def set_ai_analysis(self, call_id: str, summary: str, sentiment: str) -> None:
        call = self._calls.get(call_id)
        if call:
            call.ai_summary = summary
            call.ai_sentiment = sentiment
