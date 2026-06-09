# Inbound Calling Setup (callingio)

This adds **inbound** calling — external callers dialing a purchased DID ring
the owner's callingio app — **without changing any outbound code**.

## What was added

| File | Purpose |
|------|---------|
| `inbound_handler.py` | Isolated `InboundRouter`: catches parked inbound calls, looks up the DID owner, rings their SIP extension. Registers its own `CHANNEL_PARK` handler only. |
| `server_setup/02_inbound.xml` | Dedicated **public** context dialplan: parks inbound DID calls and flags them `callingio_inbound=true`. |
| `main.py` (additive only) | Imports the router, defines `_inbound_owner_extension()` lookup, subscribes to `CHANNEL_PARK`, and calls `inbound_router.register()` on startup. |

Outbound quick-dial and campaign dialing are untouched. The router strictly
filters on `callingio_inbound=true`, so the `&park()` that outbound carrier
legs already use is ignored.

## How a call flows

```
Caller dials DID  ──▶  Carrier  ──▶  FreeSWITCH public context
                                          │
                          02_inbound.xml: set callingio_inbound=true; park()
                                          │  CHANNEL_PARK event
                                          ▼
                          InboundRouter._on_park()
                                          │  look up DID owner → extension
                                          ▼
                          uuid_transfer <uuid> 'bridge:user/<ext>' inline
                                          │
                                          ▼
                          callingio app (registered to <ext>) rings
```

DID → owner mapping uses an **active subscription** first, then raw
`DID.owner_username` as a fallback.

## One-time server step

Copy the inbound dialplan onto the FreeSWITCH box (it must be in the
**public** context, NOT default):

```bash
sudo cp 02_inbound.xml /etc/freeswitch/dialplan/public/02_inbound.xml
sudo fs_cli -x "reloadxml"
```

Then restart the backend so the `CHANNEL_PARK` subscription + router register:

```bash
# however you run it, e.g.
python run.py
```

## Requirements / checklist

1. **Carrier points the DID to this FreeSWITCH server.** Your DID provider
   (Telnyx / voip.ms / Telcast, etc.) must route inbound calls for the
   purchased number to this server's IP on the SIP port. Without this, no
   inbound call ever reaches FreeSWITCH.
2. **The DID has an owner with an extension.** Buy/assign the number in the
   app so a subscription (or `owner_username`) exists, and that user has a SIP
   `extension`. Otherwise the router rejects with `CALL_REJECTED`.
3. **The app is registered.** The callingio app must be open and SIP-registered
   (green chip) for the call to be answered. (Background/closed ringing needs
   the separate CallKit/push phase — not part of this change.)

## Verifying

Tail the backend log while placing a test call to the DID:

```
Inbound call from <caller> → DID <number> (<digits>) owned by ext <ext>; ringing app
Inbound transfer to user/<ext> → +OK ...
```

If you see `no active owner/extension → rejecting`, the DID isn't owned by a
user with an extension (see checklist #2).

## Rolling back

Inbound is fully isolated. To disable it with zero impact on outbound:

1. Remove (or don't load) `/etc/freeswitch/dialplan/public/02_inbound.xml`, run `reloadxml`.
2. Optionally comment out the two `inbound_router` lines and the `"CHANNEL_PARK"`
   entry in `main.py`'s `lifespan`.
