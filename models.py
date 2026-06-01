from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AgentStatus(str, Enum):
    OFFLINE = "offline"
    IDLE = "idle"
    ON_CALL = "on_call"
    WRAP_UP = "wrap_up"
    BREAK = "break"


class CallStatus(str, Enum):
    DIALING = "dialing"
    RINGING = "ringing"
    ANSWERED = "answered"
    AMD_CHECK = "amd_check"
    BRIDGED = "bridged"
    DROPPED = "dropped"
    COMPLETED = "completed"
    FAILED = "failed"


class AMDResult(str, Enum):
    HUMAN = "human"
    MACHINE = "machine"
    UNKNOWN = "unknown"


class UserRole(str, Enum):
    USER = "user"
    SUPERADMIN = "superadmin"


class CampaignStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"


# ── Domain models ──────────────────────────────────────────────────────────────

class User(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    username: str
    password_hash: str
    role: UserRole = UserRole.USER
    extension: Optional[str] = None
    agent_id: Optional[str] = None          # linked Agent.id (auto-created on user create)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    phone_type: str = "softphone"           # "softphone" or "webphone"
    sip_password: Optional[str] = None      # FreeSWITCH SIP auth password (for webphone)


class DID(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    number: str
    label: Optional[str] = None
    active: bool = True


class Contact(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    phone: str
    name: Optional[str] = None
    email: Optional[str] = None
    custom_data: Dict[str, Any] = Field(default_factory=dict)
    dialed: bool = False
    dialed_at: Optional[datetime] = None
    result: Optional[str] = None


class Agent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    extension: str          # SIP extension, e.g. "1001"
    status: AgentStatus = AgentStatus.OFFLINE
    current_call_id: Optional[str] = None
    calls_handled: int = 0
    login_time: Optional[datetime] = None
    phone_type: str = "softphone"  # "softphone" or "webphone" — synced from User on preference change


class Call(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    fs_uuid: Optional[str] = None       # FreeSWITCH Unique-ID (carrier leg)
    fs_job_uuid: Optional[str] = None   # bgapi Job-UUID
    agent_fs_uuid: Optional[str] = None # FreeSWITCH Unique-ID (agent/Zoiper leg)
    contact: Contact
    agent_id: Optional[str] = None
    status: CallStatus = CallStatus.DIALING
    amd_result: Optional[AMDResult] = None
    start_time: datetime = Field(default_factory=datetime.utcnow)
    answer_time: Optional[datetime] = None
    bridge_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration: Optional[int] = None      # seconds
    disposition: Optional[str] = None  # set by agent
    recording_path: Optional[str] = None   # relative path served via /recordings/
    ai_summary: Optional[str] = None
    ai_sentiment: Optional[str] = None
    campaign_id: str = ""
    caller_id: Optional[str] = None        # DID / caller ID used
    sip_code: Optional[str] = None         # SIP response code e.g. "200", "503"
    hangup_cause: Optional[str] = None     # FreeSWITCH cause e.g. "NORMAL_CLEARING"


class CampaignStats(BaseModel):
    contacts_total: int = 0
    contacts_dialed: int = 0
    contacts_remaining: int = 0
    calls_answered: int = 0
    calls_dropped: int = 0
    calls_machine: int = 0
    calls_failed: int = 0
    answer_rate: float = 0.0
    drop_rate: float = 0.0


class Campaign(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    status: CampaignStatus = CampaignStatus.IDLE
    contacts: List[Contact] = Field(default_factory=list)
    stats: CampaignStats = Field(default_factory=CampaignStats)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# ── API request/response schemas ───────────────────────────────────────────────

class CampaignCreate(BaseModel):
    name: str


class AgentCreate(BaseModel):
    name: str
    extension: str


class AgentLogin(BaseModel):
    agent_id: str


class AgentDisposition(BaseModel):
    call_id: str
    disposition: str
    notes: Optional[str] = None


class UserCreate(BaseModel):
    username: str
    password: str = "1234"
    role: UserRole = UserRole.USER
    extension: Optional[str] = None
    sip_password: Optional[str] = None     # if omitted, defaults to web password


class WSMessage(BaseModel):
    type: str
    data: Any
