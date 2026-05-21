"""
Mock FreeSWITCH ESL server for testing.

Simulates the ESL inbound protocol:
  auth/request → auth ClueCon → subscribe → bgapi originate → events
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional


class MockFreeSwitchServer:
    """
    Lightweight TCP server that speaks ESL just enough to test the dialer.

    Simulated call flow (per originate):
      1. Ack bgapi with command/reply
      2. Send BACKGROUND_JOB with +OK <call_uuid>   (after ring_delay)
      3. Send CHANNEL_ANSWER                          (after answer_delay)
      4. Send CHANNEL_HANGUP                          (after call_duration)
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18021,
        password: str = "TestPass",
        ring_delay: float = 0.3,
        answer_delay: float = 0.2,
        call_duration: float = 1.0,
        answer_rate: float = 1.0,   # fraction of calls that "answer"
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.ring_delay = ring_delay
        self.answer_delay = answer_delay
        self.call_duration = call_duration
        self.answer_rate = answer_rate
        self._server: Optional[asyncio.AbstractServer] = None
        self.originate_count = 0
        self.hangup_count = 0
        self.bridge_count = 0
        self._writers: list = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ── Connection handler ─────────────────────────────────────────────────────

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        try:
            await self._run_session(reader, writer)
        except Exception:
            pass
        finally:
            self._writers.remove(writer)
            writer.close()

    async def _run_session(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
        # 1. Send auth challenge
        self._write(writer, "Content-Type: auth/request\n\n")

        # 2. Read auth command
        auth_line = await reader.readuntil(b"\n\n")
        if self.password not in auth_line.decode():
            self._write(writer, "Content-Type: command/reply\nReply-Text: -ERR invalid\n\n")
            return
        self._write(writer, "Content-Type: command/reply\nReply-Text: +OK accepted\n\n")

        # 3. Command loop
        buf = b""
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                cmd_bytes, buf = buf.split(b"\n\n", 1)
                cmd = cmd_bytes.decode(errors="replace").strip()
                await self._process(cmd, writer)

    async def _process(self, cmd: str, writer: asyncio.StreamWriter) -> None:
        lines = cmd.split("\n")
        first = lines[0].strip()

        if first.startswith("event plain"):
            self._write(writer,
                "Content-Type: command/reply\n"
                "Reply-Text: +OK event listener enabled plain\n\n")

        elif first.startswith("bgapi originate"):
            job_uuid = ""
            for line in lines[1:]:
                if line.startswith("Job-UUID:"):
                    job_uuid = line.split(":", 1)[1].strip()
            if not job_uuid:
                job_uuid = str(uuid.uuid4())

            self._write(writer,
                f"Content-Type: command/reply\n"
                f"Reply-Text: +OK Job-UUID: {job_uuid}\n\n")

            self.originate_count += 1
            call_uuid = str(uuid.uuid4())

            # Extract phone number from originate string (for reference in events)
            phone = "15550000000"
            for part in first.split("/"):
                if part.replace("+", "").replace("-", "").isdigit() and len(part) > 6:
                    phone = part
                    break

            asyncio.create_task(
                self._simulate_call(writer, job_uuid, call_uuid, phone)
            )

        elif first.startswith("api uuid_transfer") or first.startswith("api uuid_bridge"):
            self.bridge_count += 1
            self._write(writer,
                "Content-Type: api/response\n"
                "Content-Length: 3\n\n"
                "+OK")

        elif first.startswith("api uuid_kill"):
            self.hangup_count += 1
            self._write(writer,
                "Content-Type: api/response\n"
                "Content-Length: 3\n\n"
                "+OK")

        elif first.startswith("api"):
            self._write(writer,
                "Content-Type: api/response\n"
                "Content-Length: 3\n\n"
                "+OK")

    async def _simulate_call(self, writer: asyncio.StreamWriter,
                             job_uuid: str, call_uuid: str, phone: str) -> None:
        await asyncio.sleep(self.ring_delay)

        # BACKGROUND_JOB — originate result
        job_result = f"+OK {call_uuid}"
        body = (
            f"Event-Name: BACKGROUND_JOB\n"
            f"Job-UUID: {job_uuid}\n"
            f"Job-Command: originate\n"
            f"Content-Length: {len(job_result)}\n\n"
            f"{job_result}"
        )
        self._send_event(writer, body)

        await asyncio.sleep(self.answer_delay)

        # CHANNEL_ANSWER
        answer_body = (
            f"Event-Name: CHANNEL_ANSWER\n"
            f"Unique-ID: {call_uuid}\n"
            f"Caller-Destination-Number: {phone}\n"
            f"Caller-Caller-ID-Number: 15550000001\n"
            f"Answer-State: answered\n"
        )
        self._send_event(writer, answer_body)

        await asyncio.sleep(self.call_duration)

        # CHANNEL_HANGUP
        hangup_body = (
            f"Event-Name: CHANNEL_HANGUP\n"
            f"Unique-ID: {call_uuid}\n"
            f"Hangup-Cause: NORMAL_CLEARING\n"
            f"Caller-Destination-Number: {phone}\n"
        )
        self._send_event(writer, hangup_body)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _send_event(self, writer: asyncio.StreamWriter, body: str) -> None:
        packet = (
            f"Content-Length: {len(body)}\n"
            f"Content-Type: text/event-plain\n\n"
            f"{body}"
        )
        self._write(writer, packet)

    @staticmethod
    def _write(writer: asyncio.StreamWriter, data: str) -> None:
        try:
            writer.write(data.encode("utf-8"))
        except Exception:
            pass
