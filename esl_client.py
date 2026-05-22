"""
Async FreeSWITCH Event Socket Library (ESL) client.

Protocol overview (inbound mode — client connects to FS on port 8021):
  1. Server sends  Content-Type: auth/request
  2. Client sends  auth <password>\\n\\n
  3. Server sends  Content-Type: command/reply  Reply-Text: +OK accepted
  4. Client sends  event plain <events>\\n\\n
  5. All subsequent messages are either:
       - Content-Type: text/event-plain   → dispatched to handlers
       - Content-Type: command/reply      → queued for api/bgapi callers
       - Content-Type: api/response       → queued for api callers
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
import uuid
from collections import defaultdict
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)


class ESLEvent(dict):
    """A parsed FreeSWITCH event (dict subclass for convenient access)."""

    @property
    def name(self) -> str:
        return self.get("Event-Name", "")

    @property
    def unique_id(self) -> str:
        return self.get("Unique-ID", "")

    @property
    def job_uuid(self) -> str:
        return self.get("Job-UUID", "")


Handler = Callable[[ESLEvent], Coroutine[Any, Any, None]]


class ESLClient:
    """
    Async FreeSWITCH ESL client.

    Usage::

        client = ESLClient("127.0.0.1", 8021, "ClueCon")
        await client.connect()
        await client.subscribe("CHANNEL_ANSWER", "CHANNEL_HANGUP", "BACKGROUND_JOB")

        @client.on("CHANNEL_ANSWER")
        async def on_answer(event: ESLEvent):
            print("Answered:", event.unique_id)

        await client.listen()   # blocks; run as a task
    """

    def __init__(self, host: str, port: int, password: str) -> None:
        self.host = host
        self.port = port
        self.password = password

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._send_lock = asyncio.Lock()
        self._api_lock = asyncio.Lock()          # serialize synchronous api calls
        self._response_queue: asyncio.Queue = asyncio.Queue()
        self._event_handlers: Dict[str, List[Handler]] = defaultdict(list)
        self._listen_task: Optional[asyncio.Task] = None
        self.connected = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def on(self, event_name: str) -> Callable[[Handler], Handler]:
        """Decorator to register an async event handler."""
        def decorator(fn: Handler) -> Handler:
            self._event_handlers[event_name].append(fn)
            return fn
        return decorator

    def add_handler(self, event_name: str, fn: Handler) -> None:
        self._event_handlers[event_name].append(fn)

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

        # Auth handshake — direct reads before the listener starts
        auth_msg = await self._read_raw()
        if auth_msg.get("Content-Type") != "auth/request":
            raise ConnectionError(f"Unexpected first message: {auth_msg}")

        await self._send_raw(f"auth {self.password}")
        reply = await self._read_raw()
        if "+OK accepted" not in reply.get("Reply-Text", ""):
            raise ConnectionError(f"ESL auth failed: {reply.get('Reply-Text')}")

        self.connected = True
        logger.info("Connected to FreeSWITCH ESL at %s:%s", self.host, self.port)

        self._listen_task = asyncio.create_task(self._listen_loop())

    async def subscribe(self, *events: str) -> None:
        """Subscribe to one or more event names.  Pass no args for ALL."""
        event_str = " ".join(events) if events else "ALL"
        await self._send_raw(f"event plain {event_str}")
        reply = await asyncio.wait_for(self._response_queue.get(), timeout=5.0)
        logger.debug("Subscribe reply: %s", reply.get("Reply-Text"))

    async def api(self, command: str) -> str:
        """Send a synchronous `api` command.  Returns the response body."""
        async with self._api_lock:
            await self._send_raw(f"api {command}")
            msg = await asyncio.wait_for(self._response_queue.get(), timeout=30.0)
            return msg.get("body", "")

    async def bgapi(self, command: str) -> str:
        """Send a background `bgapi` command.  Returns the Job-UUID."""
        job_uuid = str(uuid.uuid4())
        await self._send_raw(f"bgapi {command}\nJob-UUID: {job_uuid}")
        await asyncio.wait_for(self._response_queue.get(), timeout=5.0)
        return job_uuid

    async def originate(self, phone: str, gateway: str, caller_id: str,
                        timeout: int = 30) -> tuple[str, str]:
        """Originate an outbound call, park it on answer.

        Returns (job_uuid, channel_uuid).

        channel_uuid is pre-set via origination_uuid so CHANNEL_ANSWER can be
        matched immediately — even before the BACKGROUND_JOB reply arrives.

        If `gateway` looks like an IP address (e.g. '88.151.132.84') the call
        is sent directly to that SIP host (IP-authenticated carrier).
        Otherwise a named FreeSWITCH gateway is used.
        """
        import re
        channel_uuid = str(uuid.uuid4())

        if re.match(r"^\d{1,3}(\.\d{1,3}){3}", gateway):
            endpoint = f"sofia/external/{phone}@{gateway}"
        else:
            endpoint = f"sofia/gateway/{gateway}/{phone}"

        dial = (
            f"{{origination_uuid={channel_uuid},"
            f"originate_timeout={timeout},"
            f"ignore_early_media=true,"
            f"origination_caller_id_number={caller_id}}}"
            f"{endpoint}"
        )
        job_uuid = await self.bgapi(f"originate {dial} &park()")
        return job_uuid, channel_uuid

    async def bridge_to_agent(self, call_uuid: str, extension: str,
                              caller_id: str = "") -> str:
        """Ring the agent and park them; caller bridges both with uuid_bridge.

        Parks the agent leg with &park() so FreeSWITCH holds both legs
        independently. The BACKGROUND_JOB +OK returns the agent UUID;
        the caller then uses uuid_bridge(carrier_uuid, agent_uuid) to
        connect the two parked channels.

        This avoids CHAN_NOT_IMPLEMENTED that occurs when &bridge() is run
        from the B-leg pointing at a carrier leg that has been in park/IVR
        for an extended period (18–30 s ring time).

        NOTE: api (synchronous) must NOT be used for originate because it
        holds the API lock for the entire ring duration and times out if the
        agent doesn't answer within 30 s, corrupting the response queue.
        """
        cid = caller_id or "Dialer"
        cmd = (
            f"originate {{"
            f"origination_caller_id_number={cid},"
            f"origination_caller_id_name={cid},"
            f"leg_timeout=30"
            f"}}user/{extension} &park()"
        )
        logger.info("bridge_to_agent → bgapi %s", cmd)
        job_uuid = await self.bgapi(cmd)
        logger.info("bridge_to_agent dispatched job=%s (call=%s ext=%s)",
                    job_uuid, call_uuid, extension)
        return job_uuid

    async def uuid_bridge(self, uuid_a: str, uuid_b: str) -> str:
        """Bridge two already-answered/parked channels together.

        Both channels must exist and be in a bridgeable state.
        Returns the api response string.
        """
        resp = await self.api(f"uuid_bridge {uuid_a} {uuid_b}")
        logger.info("uuid_bridge %s <-> %s → %s", uuid_a, uuid_b, resp.strip())
        return resp

    async def hangup(self, call_uuid: str, cause: str = "NORMAL_CLEARING") -> str:
        return await self.api(f"uuid_kill {call_uuid} {cause}")

    async def listen(self) -> None:
        """Block until the ESL connection closes."""
        if self._listen_task:
            await self._listen_task

    async def disconnect(self) -> None:
        self.connected = False
        if self._listen_task:
            self._listen_task.cancel()
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _send_raw(self, cmd: str) -> None:
        async with self._send_lock:
            self._writer.write(f"{cmd}\n\n".encode("utf-8"))
            await self._writer.drain()

    async def _read_raw(self) -> dict:
        """Read one ESL message directly from the socket (no dispatch)."""
        headers: Dict[str, str] = {}
        while True:
            line = await self._reader.readline()
            line = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                break
            if ": " in line:
                k, v = line.split(": ", 1)
                headers[k] = v

        body = ""
        cl = int(headers.get("Content-Length", 0))
        if cl > 0:
            body = (await self._reader.readexactly(cl)).decode("utf-8", errors="replace")

        return {**headers, "body": body}

    async def _listen_loop(self) -> None:
        while self.connected:
            try:
                headers: Dict[str, str] = {}
                while True:
                    line = await self._reader.readline()
                    if not line:
                        raise ConnectionResetError("EOF from FreeSWITCH")
                    line = line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        break
                    if ": " in line:
                        k, v = line.split(": ", 1)
                        headers[k] = v

                cl = int(headers.get("Content-Length", 0))
                body = ""
                if cl > 0:
                    body = (await self._reader.readexactly(cl)).decode("utf-8", errors="replace")

                ct = headers.get("Content-Type", "")

                if ct == "text/event-plain":
                    event = self._parse_event_body(body)
                    asyncio.create_task(self._dispatch(event))

                elif ct in ("command/reply", "api/response"):
                    await self._response_queue.put({**headers, "body": body})

                elif ct == "text/disconnect-notice":
                    logger.warning("FreeSWITCH sent disconnect notice")
                    self.connected = False
                    break

            except (asyncio.IncompleteReadError, ConnectionResetError) as exc:
                logger.error("ESL connection closed: %s", exc)
                self.connected = False
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("ESL listen error: %s", exc, exc_info=True)

    @staticmethod
    def _parse_event_body(body: str) -> ESLEvent:
        event = ESLEvent()
        lines = body.split("\n")
        blank_idx = len(lines)
        for i, line in enumerate(lines):
            line = line.rstrip("\r\n")
            if not line:
                blank_idx = i
                break
            if ": " in line:
                k, v = line.split(": ", 1)
                try:
                    event[k] = urllib.parse.unquote(v)
                except Exception:
                    event[k] = v

        # Capture nested body (e.g. BACKGROUND_JOB result after inner Content-Length)
        inner_cl = int(event.get("Content-Length", 0))
        if inner_cl > 0 and blank_idx < len(lines) - 1:
            inner = "\n".join(lines[blank_idx + 1:])
            event["body"] = inner[:inner_cl].strip()

        return event

    async def _dispatch(self, event: ESLEvent) -> None:
        name = event.name
        handlers = list(self._event_handlers.get(name, []))
        handlers += list(self._event_handlers.get("*", []))
        for handler in handlers:
            try:
                await handler(event)
            except Exception as exc:
                logger.error("Handler error for %s: %s", name, exc, exc_info=True)
