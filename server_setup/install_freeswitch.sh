#!/bin/bash
# FreeSWITCH — Build from source on Ubuntu 24.04 / 26.04
# Usage: bash install_freeswitch.sh
set -e
export DEBIAN_FRONTEND=noninteractive

echo "============================================"
echo " FreeSWITCH Install — Ubuntu (source build)"
echo "============================================"

# ── 1. System update ──────────────────────────────────────────────────────────
echo "[1/7] Updating system..."
apt-get update -y && apt-get upgrade -y

# ── 2. Build dependencies ─────────────────────────────────────────────────────
echo "[2/7] Installing build dependencies..."
apt-get install -y \
  build-essential cmake automake autoconf libtool pkg-config \
  git wget curl unzip \
  libjpeg-dev libsqlite3-dev libcurl4-openssl-dev \
  libpcre2-dev libspeexdsp-dev libldns-dev libedit-dev \
  liblua5.2-dev libopus-dev libsndfile1-dev \
  libavformat-dev libswscale-dev libvpx-dev libtiff-dev \
  uuid-dev zlib1g-dev libssl-dev libxml2-dev libxslt1-dev \
  libyaml-dev libpq-dev python3-dev \
  sox libsox-fmt-all ffmpeg ufw

# ── 3. Clone FreeSWITCH source ────────────────────────────────────────────────
echo "[3/7] Cloning FreeSWITCH source (this may take a minute)..."
cd /usr/src
rm -rf freeswitch
git clone --depth 1 https://github.com/signalwire/freeswitch.git freeswitch
cd freeswitch

# ── 4. Configure modules ──────────────────────────────────────────────────────
echo "[4/7] Configuring modules..."

# Create modules.conf from scratch (minimal set needed for predictive dialer)
cat > modules.conf << 'EOF'
applications/mod_commands
applications/mod_dptools
applications/mod_expr
applications/mod_fifo
applications/mod_hash
applications/mod_sms
applications/mod_spandsp
codecs/mod_g723_1
codecs/mod_opus
codecs/mod_siren
dialplans/mod_dialplan_xml
endpoints/mod_loopback
endpoints/mod_sofia
event_handlers/mod_event_socket
formats/mod_local_stream
formats/mod_native_file
formats/mod_sndfile
formats/mod_tone_stream
loggers/mod_console
loggers/mod_logfile
loggers/mod_syslog
say/mod_say_en
EOF

# ── 5. Compile ────────────────────────────────────────────────────────────────
echo "[5/7] Compiling FreeSWITCH (this takes 15-25 minutes)..."
./bootstrap.sh -j
./configure --prefix=/usr/local/freeswitch
make -j"$(nproc)"
make install
make sounds-install moh-install

# Symlinks
ln -sf /usr/local/freeswitch/bin/freeswitch /usr/local/bin/freeswitch
ln -sf /usr/local/freeswitch/bin/fs_cli     /usr/local/bin/fs_cli

FS_CONF=/usr/local/freeswitch/conf

# ── 6. Configure ESL + recording ──────────────────────────────────────────────
echo "[6/7] Configuring ESL and recording..."

# Allow remote ESL connections (open to all IPs)
cat > "$FS_CONF/autoload_configs/event_socket.conf.xml" << 'XML'
<configuration name="event_socket.conf" description="Socket Client">
  <settings>
    <param name="nat-map"      value="false"/>
    <param name="listen-ip"    value="0.0.0.0"/>
    <param name="listen-port"  value="8021"/>
    <param name="password"     value="ClueCon"/>
  </settings>
</configuration>
XML

# Recording directory
mkdir -p /var/lib/freeswitch/recordings
chmod 755 /var/lib/freeswitch/recordings

# Copy gateway file if it exists
if [ -f /root/dialer/server_setup/voip_carrier.xml ]; then
  cp /root/dialer/server_setup/voip_carrier.xml \
     "$FS_CONF/sip_profiles/external/telcastc.xml"
  echo "  ✓ Gateway config copied"
fi

# Copy dialplan if it exists
if [ -f /root/dialer/server_setup/01_outbound.xml ]; then
  cp /root/dialer/server_setup/01_outbound.xml \
     "$FS_CONF/dialplan/default/01_outbound.xml"
  echo "  ✓ Dialplan copied"
fi

# ── 7. Systemd service + firewall ─────────────────────────────────────────────
echo "[7/7] Setting up service and firewall..."

cat > /etc/systemd/system/freeswitch.service << 'UNIT'
[Unit]
Description=FreeSWITCH
After=network.target

[Service]
Type=forking
ExecStart=/usr/local/bin/freeswitch -ncwait -nonat
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable freeswitch
systemctl start freeswitch

# Firewall
ufw allow 22/tcp
ufw allow 8000/tcp
ufw allow 8021/tcp
ufw allow 5060/udp
ufw allow 5080/udp
ufw allow 16384:32768/udp
ufw --force enable

echo ""
echo "============================================"
echo " Done! FreeSWITCH installed."
echo "============================================"
echo ""
echo " Test ESL:  fs_cli -H 127.0.0.1 -p ClueCon"
echo " Gateway:   $FS_CONF/sip_profiles/external/telcastc.xml"
echo " Logs:      /usr/local/freeswitch/log/freeswitch.log"
echo ""
