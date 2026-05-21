#!/bin/bash
# FreeSWITCH Installation Script for Ubuntu 24.04
# Run this on your cloud server as root or with sudo
# Usage: sudo bash install_freeswitch.sh

set -e
export DEBIAN_FRONTEND=noninteractive

echo "============================================"
echo " FreeSWITCH Setup — Ubuntu 24.04"
echo "============================================"

# ── 1. System update ──────────────────────────────────────────────────────────
echo "[1/8] Updating system packages..."
apt-get update -y
apt-get upgrade -y

# ── 2. Install dependencies ───────────────────────────────────────────────────
echo "[2/8] Installing dependencies..."
apt-get install -y \
  wget curl gnupg2 ca-certificates lsb-release \
  ufw git software-properties-common \
  sox libsox-fmt-all ffmpeg

# ── 3. Add SignalWire FreeSWITCH repository ───────────────────────────────────
echo "[3/8] Adding FreeSWITCH repository..."
# SignalWire token — free signup at signalwire.com/freeswitch
# If you have a token set it here, otherwise we compile from source below
SW_TOKEN="${SIGNALWIRE_TOKEN:-}"

if [ -n "$SW_TOKEN" ]; then
  # Package install via SignalWire repo (fastest)
  wget --http-user=signalwire --http-password="$SW_TOKEN" \
       -O /usr/share/keyrings/signalwire-freeswitch-repo.gpg \
       "https://freeswitch.signalwire.com/repo/deb/debian-release/signalwire-freeswitch-repo.gpg"

  echo "machine freeswitch.signalwire.com login signalwire password $SW_TOKEN" \
       > /etc/apt/auth.conf
  chmod 600 /etc/apt/auth.conf

  echo "deb [signed-by=/usr/share/keyrings/signalwire-freeswitch-repo.gpg] \
https://freeswitch.signalwire.com/repo/deb/debian-release/ bookworm main" \
       > /etc/apt/sources.list.d/freeswitch.list

  apt-get update -y
  apt-get install -y freeswitch-meta-all
else
  # Compile from source (no token needed)
  echo "[3/8] No SignalWire token — building FreeSWITCH from source..."
  install_from_source
fi

function install_from_source() {
  apt-get install -y \
    build-essential cmake automake autoconf libtool \
    libjpeg-dev libsqlite3-dev libcurl4-openssl-dev \
    libpcre3-dev libspeexdsp-dev libldns-dev libedit-dev \
    libpq-dev liblua5.2-dev libopus-dev libsndfile1-dev \
    libavformat-dev libswscale-dev libvpx-dev libtiff-dev \
    uuid-dev zlib1g-dev libssl-dev libxml2-dev

  cd /usr/src
  git clone --depth 1 https://github.com/signalwire/freeswitch.git freeswitch
  cd freeswitch

  # Enable minimal module set
  cp build/modules.conf.minimal modules.conf

  # Add essential modules
  cat >> modules.conf <<'MODS'
applications/mod_commands
applications/mod_dptools
applications/mod_esf
applications/mod_expr
applications/mod_fifo
applications/mod_hash
applications/mod_httapi
applications/mod_sms
applications/mod_sound_test
applications/mod_spandsp
applications/mod_valet_parking
applications/mod_voicemail
codecs/mod_g723_1
codecs/mod_g729
codecs/mod_h26x
codecs/mod_opus
codecs/mod_siren
dialplans/mod_dialplan_xml
endpoints/mod_loopback
endpoints/mod_sofia
event_handlers/mod_event_socket
formats/mod_local_stream
formats/mod_native_file
formats/mod_shout
formats/mod_sndfile
formats/mod_tone_stream
languages/mod_lua
loggers/mod_console
loggers/mod_logfile
loggers/mod_syslog
say/mod_say_en
MODS

  ./bootstrap.sh -j
  ./configure --prefix=/usr/local/freeswitch
  make -j"$(nproc)"
  make install
  make sounds-install moh-install

  # Create symlink so `freeswitch` is in PATH
  ln -sf /usr/local/freeswitch/bin/freeswitch /usr/local/bin/freeswitch
  ln -sf /usr/local/freeswitch/bin/fs_cli /usr/local/bin/fs_cli

  FS_CONF=/usr/local/freeswitch/conf
  FS_LOG=/usr/local/freeswitch/log
  FS_REC=/usr/local/freeswitch/recordings
}

# Detect config dir (package vs source install)
if [ -d /etc/freeswitch ]; then
  FS_CONF=/etc/freeswitch
  FS_LOG=/var/log/freeswitch
  FS_REC=/var/lib/freeswitch/recordings
else
  FS_CONF=/usr/local/freeswitch/conf
  FS_LOG=/usr/local/freeswitch/log
  FS_REC=/usr/local/freeswitch/recordings
fi

# ── 4. Recording directory ────────────────────────────────────────────────────
echo "[4/8] Creating recording directory..."
mkdir -p "$FS_REC"
chown -R freeswitch:freeswitch "$FS_REC" 2>/dev/null || true
chmod 755 "$FS_REC"

# ── 5. ESL configuration (allow remote dashboard) ────────────────────────────
echo "[5/8] Configuring Event Socket Layer..."
cat > "$FS_CONF/autoload_configs/event_socket.conf.xml" <<'XML'
<configuration name="event_socket.conf" description="Socket Client">
  <settings>
    <!-- Listen on all interfaces so your laptop can connect -->
    <param name="nat-map" value="false"/>
    <param name="listen-ip" value="0.0.0.0"/>
    <param name="listen-port" value="8021"/>
    <param name="password" value="ClueCon"/>
    <!-- Remove the IP restriction so your laptop IP is allowed -->
    <!-- <param name="apply-inbound-acl" value="loopback.auto"/> -->
  </settings>
</configuration>
XML

# ── 6. SIP profile for outbound calls ────────────────────────────────────────
echo "[6/8] Writing SIP external profile..."
# The external profile is used for connecting to your VoIP carrier
# We only adjust the outbound gateway section here

GATEWAY_FILE="$FS_CONF/sip_profiles/external/voip_carrier.xml"
cat > "$GATEWAY_FILE" <<'XML'
<!--
  VoIP Carrier Gateway
  Edit GATEWAY_HOST below with your carrier's SIP server address.
  Edit USERNAME / PASSWORD with your SIP trunk credentials.
-->
<include>
  <gateway name="us_route">
    <param name="username"    value="YOUR_SIP_USERNAME"/>
    <param name="password"    value="YOUR_SIP_PASSWORD"/>
    <param name="realm"       value="YOUR_CARRIER_SIP_HOST"/>
    <param name="proxy"       value="YOUR_CARRIER_SIP_HOST"/>
    <param name="register"    value="true"/>
    <param name="caller-id-in-from" value="false"/>
    <param name="contact-params"    value=""/>
    <param name="ping"        value="25"/>
  </gateway>
</include>
XML

echo ""
echo "  >>> Edit $GATEWAY_FILE with your carrier credentials before starting."
echo ""

# ── 7. Dialplan for outbound calls ───────────────────────────────────────────
echo "[7/8] Adding outbound dialplan..."
DIALPLAN_FILE="$FS_CONF/dialplan/default/01_outbound.xml"
mkdir -p "$(dirname "$DIALPLAN_FILE")"
cat > "$DIALPLAN_FILE" <<'XML'
<include>
  <!-- Route any 10/11-digit number via the carrier gateway -->
  <extension name="outbound_pstn">
    <condition field="destination_number" expression="^(\+?1?\d{10,11})$">
      <action application="set"       data="effective_caller_id_number=${outbound_caller_id_number}"/>
      <action application="set"       data="effective_caller_id_name=${outbound_caller_id_name}"/>
      <action application="set"       data="call_timeout=30"/>
      <action application="bridge"    data="sofia/gateway/us_route/$1"/>
    </condition>
  </extension>
</include>
XML

# ── 8. Firewall ───────────────────────────────────────────────────────────────
echo "[8/8] Configuring firewall..."
ufw allow 22/tcp    comment "SSH"
ufw allow 8021/tcp  comment "FreeSWITCH ESL"
ufw allow 5060/udp  comment "SIP"
ufw allow 5080/udp  comment "SIP (external profile)"
ufw allow 16384:32768/udp comment "RTP media"
ufw --force enable

# ── Systemd service (package install already creates this) ────────────────────
if ! systemctl is-enabled freeswitch &>/dev/null; then
  cat > /etc/systemd/system/freeswitch.service <<'UNIT'
[Unit]
Description=FreeSWITCH
After=network.target

[Service]
Type=forking
PIDFile=/var/run/freeswitch/freeswitch.pid
EnvironmentFile=-/etc/default/freeswitch
ExecStart=/usr/local/bin/freeswitch -ncwait -nonat
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
fi

systemctl enable freeswitch
systemctl start  freeswitch || true

echo ""
echo "============================================"
echo " Installation complete!"
echo "============================================"
echo ""
echo " Recording dir : $FS_REC"
echo " ESL port      : 8021  (password: ClueCon)"
echo " SIP profile   : $GATEWAY_FILE"
echo ""
echo " Next steps:"
echo "  1. Edit $GATEWAY_FILE with your carrier SIP credentials"
echo "  2. Restart FreeSWITCH:  systemctl restart freeswitch"
echo "  3. Check ESL is up:     fs_cli -H 127.0.0.1 -p ClueCon"
echo "  4. Update .env on your laptop:"
echo "       FS_HOST=<this-server-public-ip>"
echo "       RECORDING_DIR=$FS_REC"
echo ""
