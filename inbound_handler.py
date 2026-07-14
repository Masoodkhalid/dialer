"""Inbound call routing + tracking — persistent across restarts."""
from __future__ import annotations
import json, logging, os, re
import uuid as uuidlib
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple
from esl_client import ESLClient, ESLEvent

logger = logging.getLogger(__name__)
_MAX_LOG = 1000
_PERSIST = "/root/dialer/inbound_history.json"

def normalize_number(num: str) -> str:
    d = re.sub(r"\D", "", num or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d

OwnerLookup = Callable[[str], Optional[Tuple[str, Optional[str]]]]

class InboundCall:
    def __init__(self, fs_uuid, caller, did, owner_username, extension):
        self.id = str(uuidlib.uuid4())
        self.fs_uuid = fs_uuid
        self.caller = caller
        self.did = did
        self.owner_username = owner_username
        self.extension = extension
        self.status = "ringing"
        self.start_time = datetime.now(timezone.utc)
        self.answer_time = None
        self.end_time = None
        self.duration = 0
        self.hangup_cause = None
    def to_dict(self):
        return {"id": self.id, "caller": self.caller, "did": self.did,
                "owner_username": self.owner_username, "extension": self.extension,
                "status": self.status, "start_time": self.start_time.isoformat(),
                "answer_time": self.answer_time.isoformat() if self.answer_time else None,
                "end_time": self.end_time.isoformat() if self.end_time else None,
                "duration": self.duration, "hangup_cause": self.hangup_cause}
    @classmethod
    def from_dict(cls, d):
        c = cls(d.get("fs_uuid", str(uuidlib.uuid4())), d.get("caller", ""),
                d.get("did", ""), d.get("owner_username"), d.get("extension"))
        c.id = d.get("id", c.id)
        c.status = d.get("status", "completed")
        c.duration = d.get("duration", 0) or 0
        c.hangup_cause = d.get("hangup_cause")
        try: c.start_time = datetime.fromisoformat(d["start_time"])
        except Exception: pass
        for k in ("answer_time", "end_time"):
            try:
                if d.get(k): setattr(c, k, datetime.fromisoformat(d[k]))
            except Exception: pass
        return c

class InboundRouter:
    def __init__(self, esl, owner_lookup):
        self._esl = esl
        self._owner_lookup = owner_lookup
        self._calls = OrderedDict()
        self._load()
    def _load(self):
        try:
            if os.path.exists(_PERSIST):
                with open(_PERSIST) as f:
                    for d in (json.load(f) or [])[-_MAX_LOG:]:
                        c = InboundCall.from_dict(d)
                        c.fs_uuid = c.fs_uuid or c.id
                        self._calls[c.fs_uuid] = c
                logger.info("InboundRouter: loaded %d persisted inbound calls", len(self._calls))
        except Exception as e:
            logger.warning("InboundRouter load failed: %s", e)
    def _save(self):
        try:
            with open(_PERSIST, "w") as f:
                json.dump([c.to_dict() for c in self._calls.values()], f, default=str)
        except Exception as e:
            logger.warning("InboundRouter save failed: %s", e)
    def register(self):
        self._esl.add_handler("CHANNEL_ANSWER", self._on_answer)
        self._esl.add_handler("CHANNEL_HANGUP", self._on_hangup)
        logger.info("InboundRouter: tracking handlers registered (persistent)")
    def recent_calls(self, limit=300):
        calls = list(self._calls.values())[-limit:]
        return [c.to_dict() for c in reversed(calls)]
    def stats(self):
        calls = list(self._calls.values())
        today = datetime.now(timezone.utc).date()
        return {"total": len(calls),
                "answered": sum(1 for c in calls if c.status in ("answered","completed")),
                "missed": sum(1 for c in calls if c.status in ("missed","rejected","failed")),
                "live": sum(1 for c in calls if c.status in ("ringing","answered")),
                "today": sum(1 for c in calls if c.start_time.date() == today)}
    def _track(self, call):
        self._calls[call.fs_uuid] = call
        while len(self._calls) > _MAX_LOG:
            self._calls.popitem(last=False)
    def _maybe_track(self, event):
        if event.get("variable_callingio_inbound") != "true":
            return None
        u = event.unique_id
        call = self._calls.get(u)
        if call is not None:
            return call
        dest = event.get("Caller-Destination-Number") or event.get("variable_sip_to_user") or ""
        caller = event.get("Caller-Caller-ID-Number") or "Unknown"
        info = self._owner_lookup(normalize_number(dest))
        ext = info[0] if info else None
        owner = info[1] if info else None
        call = InboundCall(u, caller, dest, owner, ext)
        self._track(call)
        logger.info("Inbound call from %s -> DID %s (ext %s) tracked", caller, dest, ext)
        return call
    async def _on_answer(self, event):
        call = self._maybe_track(event)
        if call and call.status == "ringing":
            call.status = "answered"
            call.answer_time = datetime.now(timezone.utc)
            self._save()
    async def _on_hangup(self, event):
        call = self._maybe_track(event)
        if not call:
            return
        call.end_time = datetime.now(timezone.utc)
        call.hangup_cause = event.get("Hangup-Cause") or call.hangup_cause
        try: billsec = int(event.get("variable_billsec") or 0)
        except Exception: billsec = 0
        if billsec > 0 or call.answer_time:
            call.status = "completed"
            call.duration = billsec or (int((call.end_time - call.answer_time).total_seconds()) if call.answer_time else 0)
        else:
            call.status = "missed"
        logger.info("Inbound call %s -> DID %s ended (%s, %ds)", call.caller, call.did, call.status, call.duration)
        self._save()
