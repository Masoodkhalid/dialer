#!/usr/bin/env python3
"""
Demo mode — full AI Predictive Dialer with a built-in mock FreeSWITCH.

No real FreeSWITCH installation required.

Usage:
    .venv/bin/python demo.py

What it does:
  1. Starts a mock FreeSWITCH ESL server on :18021
  2. Starts the FastAPI web server on :8000
  3. Auto-creates 3 demo agents + 1 campaign with 20 fake contacts
  4. Logs in all agents so calls start flowing immediately
  5. Dashboard → http://localhost:8000
"""

from __future__ import annotations

import asyncio
import sys
import os

# Must be set before importing config / main
os.environ.setdefault("FS_HOST", "127.0.0.1")
os.environ.setdefault("FS_PORT", "18021")
os.environ.setdefault("FS_PASSWORD", "DemoPass")
os.environ.setdefault("PACING_INTERVAL", "3.0")
os.environ.setdefault("MAX_CONCURRENT_CALLS", "5")
os.environ.setdefault("AMD_ENABLED", "false")
os.environ.setdefault("RECORDING_ENABLED", "false")  # no real FS in demo — files won't exist

import httpx
import uvicorn

sys.path.insert(0, os.path.dirname(__file__))
from tests.mock_freeswitch import MockFreeSwitchServer


DEMO_AGENTS = [
    {"name": "Alice Johnson", "extension": "1001"},
    {"name": "Bob Martinez",  "extension": "1002"},
    {"name": "Carol Chen",    "extension": "1003"},
]

DEMO_CONTACTS = [
    {"phone": f"1555{str(i).zfill(7)}", "name": f"Demo Contact {i}"}
    for i in range(1, 6)
] + [
    {"phone": "18002752273", "name": "Apple Store USA"},
    {"phone": "18882804331", "name": "Amazon USA"},
] + [
    {"phone": f"1555{str(i).zfill(7)}", "name": f"Demo Contact {i}"}
    for i in range(6, 11)
]


async def seed_demo_data() -> None:
    """Hit the REST API to create agents, campaign, contacts, then start."""
    base = "http://127.0.0.1:8000"
    async with httpx.AsyncClient(timeout=10) as client:

        # Wait for the server to be ready
        for _ in range(20):
            try:
                await client.get(f"{base}/agents")
                break
            except Exception:
                await asyncio.sleep(0.5)

        print("\n[demo] Seeding agents...")
        agent_ids = []
        for a in DEMO_AGENTS:
            r = await client.post(f"{base}/agents", json=a)
            agent = r.json()
            agent_ids.append(agent["id"])
            print(f"  + Agent: {a['name']} (ext {a['extension']})")

        print("[demo] Logging in agents...")
        for aid in agent_ids:
            await client.post(f"{base}/agents/login", json={"agent_id": aid})

        print("[demo] Creating campaign...")
        r = await client.post(f"{base}/campaigns", json={"name": "Demo Campaign"})
        campaign = r.json()
        cid = campaign["id"]

        print("[demo] Uploading contacts (CSV)...")
        csv_lines = ["phone,name"] + [
            f"{c['phone']},{c['name']}" for c in DEMO_CONTACTS
        ]
        csv_bytes = "\n".join(csv_lines).encode()
        r = await client.post(
            f"{base}/campaigns/{cid}/upload",
            files={"file": ("contacts.csv", csv_bytes, "text/csv")},
        )
        print(f"  + {r.json()['added']} contacts loaded")

        print("[demo] Starting campaign — calls will begin in ~3 seconds...")
        await client.post(f"{base}/campaigns/{cid}/start")

        print("\n" + "=" * 55)
        print("  Dashboard ready → http://localhost:8000")
        print("  Watch live calls in the Active Calls table.")
        print("  Press Ctrl+C to stop.")
        print("=" * 55 + "\n")


async def main() -> None:
    # 1. Start mock FreeSWITCH
    mock = MockFreeSwitchServer(
        host="127.0.0.1",
        port=18021,
        password="DemoPass",
        ring_delay=2.0,     # 2 s ring before answer
        answer_delay=1.0,   # 1 s pause before CHANNEL_ANSWER event
        call_duration=10.0, # call lasts 10 s
    )
    await mock.start()
    print("[demo] Mock FreeSWITCH ESL listening on :18021")

    # 2. Start FastAPI (non-blocking)
    config = uvicorn.Config(
        "main:app",
        host="0.0.0.0",
        port=8000,
        log_level="warning",   # quiet — demo output is cleaner
        reload=False,
    )
    server = uvicorn.Server(config)

    # 3. Seed data after server boots, then run server until Ctrl+C
    async with asyncio.TaskGroup() as tg:
        tg.create_task(server.serve())
        tg.create_task(seed_demo_data())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[demo] Stopped.")
