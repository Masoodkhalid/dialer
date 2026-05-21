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
| Twilio  | sip.twilio.com | Needs TLS — set `<param name="contact-host" value="sip.twilio.com"/>` |
| Telnyx  | sip.telnyx.com | Standard SIP, register=true |
| Vonage  | sip.nexmo.com  | Standard SIP |
| VoIP.ms | atlanta.voip.ms (nearest POP) | Set register=true, use your API password |
| Generic | your-ip:5060   | May not need registration (`register=false`) |
