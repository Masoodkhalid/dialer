# Server Setup — FreeSWITCH on Ubuntu Cloud Server

## Architecture

```
Your Laptop (dashboard)          Cloud Server (Ubuntu)         VoIP Carrier
┌──────────────────────┐         ┌──────────────────────┐       ┌──────────────┐
│  python run.py       │  ESL    │  FreeSWITCH          │  SIP  │              │
│  FastAPI + Dashboard │────────▶│  port 8021           │──────▶│  SIP Trunk   │
│  localhost:8000      │  8021   │  port 5060/5080 SIP  │       │              │
└──────────────────────┘         └──────────────────────┘       └──────────────┘
                                         │
                                  Recordings saved
                                 /var/lib/freeswitch/
                                    recordings/
```

---

## Step 1 — SSH into your server

```bash
ssh root@YOUR_SERVER_IP
```

---

## Step 2 — Run the install script

Copy the script to your server and run it:

```bash
# On your laptop — copy the script to the server
scp server_setup/install_freeswitch.sh root@YOUR_SERVER_IP:/root/

# On the server — run it
ssh root@YOUR_SERVER_IP
chmod +x /root/install_freeswitch.sh
sudo bash /root/install_freeswitch.sh
```

The script will:
- Install FreeSWITCH (from SignalWire packages or source)
- Configure ESL on port 8021 (open to remote connections)
- Open firewall ports: 22, 8021, 5060, 5080, RTP range
- Create recording directory
- Enable FreeSWITCH as a systemd service

---

## Step 3 — Configure your VoIP carrier gateway

Edit the gateway file on the server:

```bash
nano /etc/freeswitch/sip_profiles/external/voip_carrier.xml
# (or for source install)
nano /usr/local/freeswitch/conf/sip_profiles/external/voip_carrier.xml
```

Fill in your carrier's details:

```xml
<gateway name="us_route">
  <param name="username"  value="YOUR_SIP_USERNAME"/>
  <param name="password"  value="YOUR_SIP_PASSWORD"/>
  <param name="realm"     value="sip.yourcarrier.com"/>
  <param name="proxy"     value="sip.yourcarrier.com"/>
  <param name="register"  value="true"/>
</gateway>
```

Then restart FreeSWITCH:

```bash
systemctl restart freeswitch
```

---

## Step 4 — Verify FreeSWITCH is working

On the server:

```bash
# Connect to FreeSWITCH CLI
fs_cli -H 127.0.0.1 -p ClueCon

# Inside fs_cli — check SIP registration status
sofia status gateway us_route

# Should show:  State: NOREG or REGED
# Type 'exit' to leave
```

---

## Step 5 — Update .env on your laptop

Open `/Users/masoodkhalid/Projects/freeswitch/.env` and update:

```env
FS_HOST=YOUR_SERVER_PUBLIC_IP        # e.g. 45.33.100.200
FS_PORT=8021
FS_PASSWORD=ClueCon
SIP_GATEWAY=us_route
CALLER_ID_NUMBER=18001234567         # your outbound caller ID from carrier
RECORDING_ENABLED=true
RECORDING_DIR=/var/lib/freeswitch/recordings
RECORDING_FORMAT=wav
```

---

## Step 6 — Start the dashboard on your laptop

```bash
cd /Users/masoodkhalid/Projects/freeswitch
pip install -r requirements.txt
python run.py
```

Open http://localhost:8000 — the badge should show **● Connected** (green).

---

## Troubleshooting

### ESL connection refused
```bash
# On the server — check FreeSWITCH is running
systemctl status freeswitch

# Check port 8021 is open
ss -tlnp | grep 8021

# Check firewall
ufw status
```

### Calls not going out
```bash
# In fs_cli on the server
sofia status gateway us_route
# Should show: State REGED

# Test dial manually from FreeSWITCH
originate sofia/gateway/us_route/18002752273 &echo
```

### Recordings not playing in dashboard
The dashboard fetches recordings via `/recordings/{filename}`.  
The Python app serves them from `RECORDING_DIR` on the laptop/server where it's running.  
If you run `python run.py` on your **laptop** but FreeSWITCH saves recordings on the **server**,  
you need to either:
- Run `python run.py` **on the server** too (simplest), or
- Mount the server's recording dir via SFTP/sshfs on your laptop

**Easiest: run the dashboard on the server**

```bash
# On the server
apt install python3-pip python3-venv -y
cd /root
git clone or scp your project folder here
pip3 install -r requirements.txt
python3 run.py
# Then open http://YOUR_SERVER_IP:8000
```

---

## Carrier-specific notes

| Carrier | SIP Host | Notes |
|---------|----------|-------|
| Telnyx  | sip.telnyx.com | Standard SIP, register=true — **recommended for US mobile** |
| VoIP.ms | atlanta.voip.ms (nearest POP) | Cheapest US option, register=true |
| Twilio  | sip.twilio.com | Needs TLS — set `<param name="contact-host" value="sip.twilio.com"/>` |
| Vonage  | sip.nexmo.com  | Standard SIP |
| Generic | your-ip:5060   | May not need registration (`register=false`) |

---

## Switching from SIPNAV / Telcast to a new carrier (US Mobile Fix)

**Problem:** SIPNAV (88.151.132.84) cannot route to US mobile numbers.  
It answers with 200 OK on its own IVR, then BYEs after ~2 seconds — this is why Zoiper drops immediately.

### Option A — Telnyx (Recommended, $10 free trial)

**Step 1 — Sign up and get SIP credentials**
1. Go to https://telnyx.com → Create account
2. Dashboard → **SIP Trunking** → **Connections** → **+ Create**
3. Choose **"Credentials"** (not IP)
4. Name it `dialer`, note the **SIP Username** and **SIP Password**
5. Go to **Numbers** → Buy a US DID number (e.g. +1 800-xxx-xxxx)
6. Assign the number to your `dialer` connection

**Step 2 — Put gateway file on the FreeSWITCH server**
```bash
# On your laptop — copy to server
scp server_setup/telnyx_gateway.xml root@YOUR_SERVER_IP:/etc/freeswitch/sip_profiles/external/telnyx.xml

# SSH into server and fill in your real credentials
ssh root@YOUR_SERVER_IP
nano /etc/freeswitch/sip_profiles/external/telnyx.xml
# Replace: YOUR_TELNYX_SIP_USERNAME  → e.g. 1234567890abc
# Replace: YOUR_TELNYX_SIP_PASSWORD  → your password
```

**Step 3 — Replace the outbound dialplan**
```bash
# On your laptop — copy to server (replaces old SIPNAV version)
scp server_setup/01_outbound_telnyx.xml root@YOUR_SERVER_IP:/etc/freeswitch/dialplan/default/01_outbound.xml
```

**Step 4 — Reload FreeSWITCH config**
```bash
# On the server
fs_cli -H 127.0.0.1 -p ClueCon -x "reloadxml"
fs_cli -H 127.0.0.1 -p ClueCon -x "sofia profile external rescan"

# Verify gateway registered
fs_cli -H 127.0.0.1 -p ClueCon -x "sofia status gateway telnyx"
# → Should show: State: REGED
```

**Step 5 — Update .env on your laptop**
```env
SIP_GATEWAY=telnyx
CALLER_ID_NUMBER=1XXXXXXXXXX    # your Telnyx DID (E.164, no +, e.g. 18005551234)
CALLER_ID_NAME=MyDialer
DIAL_PREFIX=                    # CLEAR THIS — Telnyx needs no prefix
```

**Step 6 — Restart dialer and test**
```bash
# On your laptop
python run.py

# Test quick dial to your eSIM — it should actually ring this time
```

---

### Option B — VoIP.ms (Cheapest, pay-as-you-go)

**Step 1 — Sign up and get SIP credentials**
1. Go to https://voip.ms → Create account (deposit $10)
2. **Main Menu → Sub Accounts → Create Sub Account**
3. Sub account name: `dialer`, set a SIP password
4. Your username = `YOUR_ACCOUNT_NUMBER_dialer` (e.g. `123456_dialer`)
5. **DID Numbers → Order New DID** → pick a US number → assign to sub account

**Step 2 — Put gateway file on server and fill credentials**
```bash
scp server_setup/voipms_gateway.xml root@YOUR_SERVER_IP:/etc/freeswitch/sip_profiles/external/voipms.xml
ssh root@YOUR_SERVER_IP
nano /etc/freeswitch/sip_profiles/external/voipms.xml
# Fill in: YOUR_ACCOUNT_dialer, YOUR_SUBACCOUNT_SIP_PASSWORD
# Change POP if needed: atlanta.voip.ms → chicago, newyork, etc.
```

**Step 3 — Replace outbound dialplan (change "telnyx" to "voipms")**
```bash
scp server_setup/01_outbound_telnyx.xml root@YOUR_SERVER_IP:/etc/freeswitch/dialplan/default/01_outbound.xml
ssh root@YOUR_SERVER_IP
# Edit the file and change "telnyx" → "voipms" in the bridge line:
sed -i 's/gateway\/telnyx\//gateway\/voipms\//' /etc/freeswitch/dialplan/default/01_outbound.xml
```

**Step 4 — Reload FreeSWITCH config**
```bash
fs_cli -H 127.0.0.1 -p ClueCon -x "reloadxml"
fs_cli -H 127.0.0.1 -p ClueCon -x "sofia profile external rescan"
fs_cli -H 127.0.0.1 -p ClueCon -x "sofia status gateway voipms"
# → Should show: State: REGED
```

**Step 5 — Update .env on your laptop**
```env
SIP_GATEWAY=voipms
CALLER_ID_NUMBER=1XXXXXXXXXX    # your VoIP.ms DID
DIAL_PREFIX=                    # CLEAR THIS — no prefix needed
```

---

### Verify everything works

After setup, test from `fs_cli` directly before using the dashboard:
```bash
# Dial your eSIM number directly from FreeSWITCH (replace with your number)
originate {origination_caller_id_number=18005551234}sofia/gateway/telnyx/12018843304 &echo

# If your eSIM rings and you hear echo → carrier is working correctly
# Then test from the dialer dashboard (Quick Dial)
```
