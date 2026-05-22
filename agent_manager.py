from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from models import Agent, AgentStatus

logger = logging.getLogger(__name__)


class AgentManager:
    def __init__(self, wrap_up_seconds: int = 30) -> None:
        self._agents: Dict[str, Agent] = {}
        self._ws_queues: Dict[str, asyncio.Queue] = {}   # agent_id -> message queue
        self._wrap_up_tasks: Dict[str, asyncio.Task] = {}
        self.wrap_up_seconds = wrap_up_seconds
        self._change_callbacks: List = []

    # ── CRUD ───────────────────────────────────────────────────────────────────

    def register(self, agent: Agent) -> Agent:
        self._agents[agent.id] = agent
        return agent

    def remove(self, agent_id: str) -> bool:
        """Unregister an agent (called when the linked user is deleted)."""
        agent = self._agents.pop(agent_id, None)
        if agent:
            task = self._wrap_up_tasks.pop(agent_id, None)
            if task:
                task.cancel()
            self._ws_queues.pop(agent_id, None)
            logger.info("Agent %s (%s) removed", agent.name, agent.extension)
            return True
        return False

    def by_extension(self, extension: str) -> Optional[Agent]:
        """Find an agent by SIP extension number."""
        return next((a for a in self._agents.values() if a.extension == extension), None)

    def get(self, agent_id: str) -> Optional[Agent]:
        return self._agents.get(agent_id)

    def list_all(self) -> List[Agent]:
        return list(self._agents.values())

    def get_idle(self) -> List[Agent]:
        return [a for a in self._agents.values() if a.status == AgentStatus.IDLE]

    # ── Status transitions ─────────────────────────────────────────────────────

    async def login(self, agent_id: str) -> bool:
        agent = self._agents.get(agent_id)
        if not agent:
            return False
        agent.status = AgentStatus.IDLE
        agent.login_time = datetime.utcnow()
        await self._notify_change(agent)
        return True

    async def logout(self, agent_id: str) -> bool:
        agent = self._agents.get(agent_id)
        if not agent:
            return False
        agent.status = AgentStatus.OFFLINE
        await self._notify_change(agent)
        return True

    async def assign_call(self, agent_id: str, call_id: str) -> bool:
        agent = self._agents.get(agent_id)
        if not agent or agent.status != AgentStatus.IDLE:
            return False
        agent.status = AgentStatus.ON_CALL
        agent.current_call_id = call_id
        await self._notify_change(agent)
        return True

    async def release_all_to_idle(self) -> None:
        """Immediately move all wrap_up / on_call agents back to idle.
        Called on campaign reset so the next campaign starts with agents ready."""
        for agent in self._agents.values():
            if agent.status in (AgentStatus.WRAP_UP, AgentStatus.ON_CALL):
                # Cancel pending wrap-up timer
                task = self._wrap_up_tasks.pop(agent.id, None)
                if task:
                    task.cancel()
                agent.status = AgentStatus.IDLE
                agent.current_call_id = None
                await self._notify_change(agent)
                logger.info("Reset: agent %s → idle", agent.name)

    async def release_call(self, agent_id: str) -> None:
        agent = self._agents.get(agent_id)
        if not agent:
            return

        # Cancel any pending wrap-up timer
        if agent_id in self._wrap_up_tasks:
            self._wrap_up_tasks[agent_id].cancel()

        agent.status = AgentStatus.WRAP_UP
        agent.calls_handled += 1
        agent.current_call_id = None
        await self._notify_change(agent)

        # Auto-return to idle after wrap-up period
        task = asyncio.create_task(self._wrap_up_timer(agent_id))
        self._wrap_up_tasks[agent_id] = task

    async def set_break(self, agent_id: str) -> bool:
        agent = self._agents.get(agent_id)
        if not agent or agent.status not in (AgentStatus.IDLE, AgentStatus.WRAP_UP):
            return False
        agent.status = AgentStatus.BREAK
        await self._notify_change(agent)
        return True

    async def return_from_break(self, agent_id: str) -> bool:
        agent = self._agents.get(agent_id)
        if not agent or agent.status != AgentStatus.BREAK:
            return False
        agent.status = AgentStatus.IDLE
        await self._notify_change(agent)
        return True

    # ── WebSocket push ─────────────────────────────────────────────────────────

    def attach_ws_queue(self, agent_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._ws_queues[agent_id] = q
        return q

    def detach_ws_queue(self, agent_id: str) -> None:
        self._ws_queues.pop(agent_id, None)

    async def push_to_agent(self, agent_id: str, message: dict) -> None:
        q = self._ws_queues.get(agent_id)
        if q:
            await q.put(message)

    # ── Change callbacks ───────────────────────────────────────────────────────

    def on_change(self, cb) -> None:
        self._change_callbacks.append(cb)

    async def _notify_change(self, agent: Agent) -> None:
        for cb in self._change_callbacks:
            try:
                await cb(agent)
            except Exception as exc:
                logger.error("Agent change callback error: %s", exc)

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _wrap_up_timer(self, agent_id: str) -> None:
        try:
            await asyncio.sleep(self.wrap_up_seconds)
            agent = self._agents.get(agent_id)
            if agent and agent.status == AgentStatus.WRAP_UP:
                agent.status = AgentStatus.IDLE
                await self._notify_change(agent)
                logger.debug("Agent %s wrap-up complete → idle", agent.name)
        except asyncio.CancelledError:
            pass
