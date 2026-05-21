#!/bin/bash
# FreeSWITCH — Full build from source on Ubuntu 24.04 / 26.04
# Handles: spandsp, sofia-sip, libcrypt symlink, -Werror, compiler issues
# Usage: bash install_freeswitch.sh
set -e
export DEBIAN_FRONTEND=noninteractive

echo "============================================"
echo " FreeSWITCH Install — Ubuntu (full build)"
echo "============================================"

# ── 1. System update ──────────────────────────────────────────────────────────
echo "[1/9] Updating system..."
apt-get update -y && apt-get upgrade -y

# ── 2. Build dependencies ─────────────────────────────────────────────────────
echo "[2/9] Installing build dependencies..."
apt-get install -y \
  build-essential cmake automake autoconf libtool pkg-config \
  git wget curl unzip yasm nasm \
  libjpeg-dev libsqlite3-dev libcurl4-openssl-dev \
  libpcre2-dev libspeex-dev libspeexdsp-dev libldns-dev libedit-dev \
  liblua5.2-dev libopus-dev libsndfile1-dev \
  libavformat-dev libswscale-dev libvpx-dev libtiff-dev \
  uuid-dev zlib1g-dev libssl-dev libxml2-dev libxslt1-dev \
  libyaml-dev libpq-dev python3-dev \
  sox libsox-fmt-all ffmpeg ufw

# ── Fix libcrypt (Ubuntu 26.04 only ships versioned .so.2, linker needs .so) ──
echo "[2b] Fixing libcrypt symlink..."
CRYPT_SO=$(find /usr/lib/x86_64-linux-gnu -name "libcrypt.so.*" 2>/dev/null | sort | tail -1)
if [ -n "$CRYPT_SO" ]; then
  LINK_TARGET="/usr/lib/x86_64-linux-gnu/libcrypt.so"
  if [ ! -e "$LINK_TARGET" ]; then
    ln -sf "$CRYPT_SO" "$LINK_TARGET"
    echo "  ✓ Created: $LINK_TARGET → $CRYPT_SO"
  else
    echo "  ✓ libcrypt.so already present"
  fi
else
  echo "  ⚠ libcrypt not found — trying libxcrypt-dev..."
  apt-get install -y libxcrypt-dev 2>/dev/null || \
  apt-get install -y libxcrypt-compat 2>/dev/null || \
  echo "  ⚠ Could not install libxcrypt — build may fail"
fi
ldconfig

# ── 3. Build spandsp from source ──────────────────────────────────────────────
echo "[3/9] Building spandsp from source..."
cd /usr/src
rm -rf spandsp
git clone --depth 1 https://github.com/freeswitch/spandsp.git spandsp
cd spandsp
./bootstrap.sh 2>/dev/null || autoreconf -fvi
./configure --prefix=/usr/local
make -j"$(nproc)"
make install
ldconfig
echo "  ✓ spandsp installed"

# ── 4. Build sofia-sip from source ────────────────────────────────────────────
echo "[4/9] Building sofia-sip from source..."
cd /usr/src
rm -rf sofia-sip
git clone --depth 1 https://github.com/freeswitch/sofia-sip.git sofia-sip
cd sofia-sip
./bootstrap.sh 2>/dev/null || autoreconf -fvi
./configure --prefix=/usr/local
make -j"$(nproc)"
make install
ldconfig
echo "  ✓ sofia-sip installed"

# ── 5. Clone FreeSWITCH source ────────────────────────────────────────────────
echo "[5/9] Cloning FreeSWITCH source (this may take a minute)..."
cd /usr/src
rm -rf freeswitch
git clone --depth 1 https://github.com/signalwire/freeswitch.git freeswitch
cd freeswitch

# ── 6. Configure modules (minimal set for predictive dialer) ──────────────────
echo "[6/9] Configuring modules..."
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

# ── 7. Compile ────────────────────────────────────────────────────────────────
echo "[7/9] Compiling FreeSWITCH (15-25 minutes)..."

./bootstrap.sh -j

export PKG_CONFIG_PATH="/usr/local/lib/pkgconfig:/usr/lib/x86_64-linux-gnu/pkgconfig:$PKG_CONFIG_PATH"
export CFLAGS="-I/usr/local/include -Wno-error -Wno-discarded-qualifiers"
export CXXFLAGS="-I/usr/local/include -Wno-error"
export LDFLAGS="-L/usr/local/lib -L/usr/lib/x86_64-linux-gnu"
export LD_LIBRARY_PATH="/usr/local/lib:$LD_LIBRARY_PATH"

./configure \
  --prefix=/usr/local/freeswitch \
  --with-spandsp=/usr/local \
  --with-sofia-sip=/usr/local

# Strip all -Werror flags so newer GCC doesn't abort on warnings
find . -name "Makefile" -exec sed -i 's/-Werror[^ ]*//g' {} \; 2>/dev/null || true

make -j"$(nproc)"
make install
make sounds-install moh-install

# Symlinks so freeswitch / fs_cli work from anywhere
ln -sf /usr/local/freeswitch/bin/freeswitch /usr/local/bin/freeswitch
ln -sf /usr/local/freeswitch/bin/fs_cli     /usr/local/bin/fs_cli
ldconfig
echo "  ✓ FreeSWITCH compiled and installed"

FS_CONF=/usr/local/freeswitch/conf

# ── 8. Configure ESL + recording ──────────────────────────────────────────────
echo "[8/9] Configuring ESL and recording..."

# Allow remote ESL connections
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

# Copy gateway config if it exists
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

# ── 9. Systemd service + firewall ─────────────────────────────────────────────
echo "[9/9] Setting up service and firewall..."

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
echo " Done! FreeSWITCH installed and running."
echo "============================================"
echo ""
echo " Status:   systemctl status freeswitch"
echo " ESL test: fs_cli -H 127.0.0.1 -p ClueCon"
echo " Gateway:  $FS_CONF/sip_profiles/external/telcastc.xml"
echo " Logs:     /usr/local/freeswitch/log/freeswitch.log"
echo ""
