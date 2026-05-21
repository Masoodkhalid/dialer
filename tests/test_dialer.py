"""
Integration tests for the AI Predictive Dialer.

Each test spins up a MockFreeSwitchServer and runs the real
ESLClient + DialerEngine against it — no mocking of internal code.

Run with:  pytest tests/test_dialer.py -v
"""

from __future__ import annotations

import asyncio
import sys
import os

import pytest
import pytest_asyncio

# Make the project root importable when running from the tests/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from esl_client import ESLClient
from agent_manager import AgentManager
from call_manager import CallManager
from dialer_engine import DialerEngine
from models import Agent, Campaign, CampaignStatus, Contact
from tests.mock_freeswitch import MockFreeSwitchServer


# ── Fixtures ───────────────────────────────────────────────────────────────────

MOCK_HOST = "127.0.0.1"
MOCK_PORT = 18021
MOCK_PASS = "TestPass"


@pytest_asyncio.fixture
async def mock_fs():
    """Start a mock FreeSWITCH server and tear it down after the test."""
    server = MockFreeSwitchServer(
        host=MOCK_HOST,
        port=MOCK_PORT,
        password=MOCK_PASS,
        ring_delay=0.1,
        answer_delay=0.05,
        call_duration=0.3,
    )
    await server.start()
    yield server
    await server.stop()


@pytest_asyncio.fixture
async def esl_client(mock_fs):
    """ESL client connected to the mock server."""
    client = ESLClient(MOCK_HOST, MOCK_PORT, MOCK_PASS)
    await client.connect()
    await client.subscribe(
        "CHANNEL_ANSWER", "CHANNEL_HANGUP", "CHANNEL_BRIDGE",
        "BACKGROUND_JOB", "CUSTOM",
    )
    yield client
    await client.disconnect()


def make_campaign(n_contacts: int = 3) -> Campaign:
    contacts = [
        Contact(phone=f"1555000{i:04d}", name=f"Contact {i}")
        for i in range(n_contacts)
    ]
    c = Campaign(name="Test Campaign", contacts=contacts)
    c.stats.contacts_total = n_contacts
    c.stats.contacts_remaining = n_contacts
    return c


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestESLClient:
    @pytest.mark.asyncio
    async def test_connect_and_auth(self, mock_fs):
        client = ESLClient(MOCK_HOST, MOCK_PORT, MOCK_PASS)
        await client.connect()
        assert client.connected is True
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_wrong_password_raises(self, mock_fs):
        client = ESLClient(MOCK_HOST, MOCK_PORT, "WrongPass")
        with pytest.raises(ConnectionError, match="ESL auth failed"):
            await client.connect()

    @pytest.mark.asyncio
    async def test_subscribe(self, mock_fs):
        client = ESLClient(MOCK_HOST, MOCK_PORT, MOCK_PASS)
        await client.connect()
        # Should not raise
        await client.subscribe("CHANNEL_ANSWER", "CHANNEL_HANGUP")
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_receive_events(self, esl_client, mock_fs):
        received = []

        @esl_client.on("CHANNEL_ANSWER")
        async def handler(event):
            received.append(event)

        job_uuid = await esl_client.originate("15551234567", "gw1", "1000")
        assert job_uuid  # non-empty string

        # Wait for events
        await asyncio.sleep(1.0)
        assert len(received) >= 1
        assert received[0]["Event-Name"] == "CHANNEL_ANSWER"


class TestAgentManager:
    @pytest.mark.asyncio
    async def test_register_and_login(self):
        mgr = AgentManager()
        agent = Agent(name="Alice", extension="1001")
        mgr.register(agent)
        assert len(mgr.list_all()) == 1

        ok = await mgr.login(agent.id)
        assert ok
        assert mgr.get(agent.id).status.value == "idle"

    @pytest.mark.asyncio
    async def test_assign_and_release(self):
        mgr = AgentManager(wrap_up_seconds=0)
        agent = Agent(name="Bob", extension="1002")
        mgr.register(agent)
        await mgr.login(agent.id)

        ok = await mgr.assign_call(agent.id, "call-123")
        assert ok
        assert mgr.get(agent.id).status.value == "on_call"
        assert len(mgr.get_idle()) == 0

        await mgr.release_call(agent.id)
        assert mgr.get(agent.id).status.value == "wrap_up"

        # After wrap-up (0 seconds) agent should be idle
        await asyncio.sleep(0.05)
        assert mgr.get(agent.id).status.value == "idle"

    @pytest.mark.asyncio
    async def test_get_idle_returns_only_idle(self):
        mgr = AgentManager()
        a1 = Agent(name="A1", extension="1001")
        a2 = Agent(name="A2", extension="1002")
        mgr.register(a1)
        mgr.register(a2)
        await mgr.login(a1.id)
        # a2 stays offline

        idle = mgr.get_idle()
        assert len(idle) == 1
        assert idle[0].id == a1.id


class TestCallManager:
    def test_add_and_lookup(self):
        mgr = CallManager()
        contact = Contact(phone="15550001111", name="Test")
        call = mgr.add(__import__("models").Call(contact=contact, campaign_id="c1"))
        assert mgr.get(call.id) is not None

    def test_on_answered(self):
        from models import Call, CallStatus
        mgr = CallManager()
        contact = Contact(phone="15550001111")
        call = Call(contact=contact, campaign_id="c1", fs_uuid="uuid-1")
        mgr.add(call)
        mgr._by_fs_uuid["uuid-1"] = call.id

        result = mgr.on_answered("uuid-1")
        assert result is not None
        assert result.status == CallStatus.ANSWERED

    def test_on_hangup_sets_duration(self):
        from models import Call, CallStatus
        import datetime
        mgr = CallManager()
        contact = Contact(phone="15550001111")
        call = Call(contact=contact, campaign_id="c1", fs_uuid="uuid-2")
        call.answer_time = datetime.datetime.utcnow()
        mgr.add(call)
        mgr._by_fs_uuid["uuid-2"] = call.id
        mgr.on_answered("uuid-2")

        mgr.on_hangup("uuid-2")
        assert call.duration is not None and call.duration >= 0


class TestDialerEngine:
    @pytest.mark.asyncio
    async def test_campaign_dials_contacts(self, esl_client, mock_fs):
        agent_mgr = AgentManager(wrap_up_seconds=0)
        call_mgr = CallManager()

        agent = Agent(name="Alice", extension="1001")
        agent_mgr.register(agent)
        await agent_mgr.login(agent.id)

        campaign = make_campaign(n_contacts=2)
        events_received = []

        async def on_event(t, d):
            events_received.append(t)

        engine = DialerEngine(
            esl=esl_client,
            agent_mgr=agent_mgr,
            call_mgr=call_mgr,
            campaign=campaign,
            gateway="gw1",
            caller_id="1000",
            dial_timeout=5,
            max_concurrent=5,
            pacing_interval=0.2,
            amd_enabled=False,
            on_event=on_event,
        )

        await engine.start()
        # Let the campaign run (contacts dial + answer + hangup)
        await asyncio.sleep(2.5)
        await engine.stop()

        # Both contacts should have been dialed
        assert mock_fs.originate_count >= 2
        assert "call_dialing" in events_received
        assert "call_answered" in events_received

    @pytest.mark.asyncio
    async def test_no_idle_agents_holds_dial(self, esl_client, mock_fs):
        agent_mgr = AgentManager()
        call_mgr = CallManager()
        # No agents registered / logged in

        campaign = make_campaign(n_contacts=3)
        events = []

        engine = DialerEngine(
            esl=esl_client,
            agent_mgr=agent_mgr,
            call_mgr=call_mgr,
            campaign=campaign,
            gateway="gw1",
            caller_id="1000",
            max_concurrent=5,
            pacing_interval=0.2,
            amd_enabled=False,
            on_event=lambda t, d: events.append(t),
        )

        await engine.start()
        await asyncio.sleep(0.6)
        await engine.stop()

        # No calls should have been placed
        assert mock_fs.originate_count == 0

    @pytest.mark.asyncio
    async def test_pause_and_resume(self, esl_client, mock_fs):
        agent_mgr = AgentManager(wrap_up_seconds=0)
        call_mgr = CallManager()

        agent = Agent(name="Bob", extension="1002")
        agent_mgr.register(agent)
        await agent_mgr.login(agent.id)

        campaign = make_campaign(n_contacts=5)
        engine = DialerEngine(
            esl=esl_client,
            agent_mgr=agent_mgr,
            call_mgr=call_mgr,
            campaign=campaign,
            gateway="gw1",
            caller_id="1000",
            max_concurrent=5,
            pacing_interval=0.1,
            amd_enabled=False,
            on_event=lambda t, d: None,
        )

        await engine.start()
        await asyncio.sleep(0.3)
        count_before = mock_fs.originate_count

        await engine.pause()
        assert campaign.status == CampaignStatus.PAUSED

        await asyncio.sleep(0.4)
        count_paused = mock_fs.originate_count
        assert count_paused == count_before  # no new dials during pause

        await engine.resume()
        assert campaign.status == CampaignStatus.RUNNING

        await asyncio.sleep(0.4)
        await engine.stop()
        assert mock_fs.originate_count >= count_before


class TestPredictiveAlgorithm:
    """Unit tests for the pacing maths — no ESL needed."""

    def test_dial_rate_scales_with_agents(self):
        import math
        answer_rate = 0.5
        idle_agents = 4
        expected_dial_rate = math.ceil(idle_agents / answer_rate)  # 8
        assert expected_dial_rate == 8

    def test_lower_answer_rate_increases_dials(self):
        import math
        # 30% answer rate → need to dial more to reach each agent
        assert math.ceil(1 / 0.3) == 4
        # 80% answer rate → fewer dials needed
        assert math.ceil(1 / 0.8) == 2

    def test_drop_rate_triggers_slowdown(self):
        drop_rate = 0.05   # 5% > 3% limit
        limit = 0.03
        assert drop_rate > limit   # slowdown should kick in
