#!/usr/bin/env bash
# Gold Copier — one-command VPS setup (Ubuntu 22.04/24.04, run as root).
#
#   curl -fsSL https://raw.githubusercontent.com/Boymyke/copy-trading-bot/main/vps/install.sh | bash
#
# What it does:
#   1. installs Wine (WineHQ stable) and a tiny virtual desktop (Xvfb + fluxbox + x11vnc)
#   2. runs that desktop as a service that starts on boot and restarts itself
#   3. starts the official MetaTrader 5 installer on the virtual desktop (you click Next in VNC)
#   4. makes two MT5 copies ("MT5 Source" for TMFinancials, "MT5 Target" for Exness),
#      puts the two Gold Copier EAs in both, compiles them, and keeps both terminals running
#
# VNC only listens on localhost: connect through an SSH tunnel (see README).
set -euo pipefail

RAW="https://raw.githubusercontent.com/Boymyke/copy-trading-bot/main/vps"
MT5_SETUP_URL="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
U=trader
H=/home/$U
PFX=$H/.mt5
PF="$PFX/drive_c/Program Files"

say() { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "Run as root (ssh root@your-server)"; exit 1; }

say "1/6 Choose a VNC password (6-8 characters) for viewing the MT5 screens"
VNCPW=""
while [ ${#VNCPW} -lt 6 ]; do
  read -r -s -p "VNC password: " VNCPW < /dev/tty; echo
done

say "2/6 Installing system packages and Wine (5-10 minutes)"
export DEBIAN_FRONTEND=noninteractive
dpkg --add-architecture i386
apt-get update -y
apt-get install -y wget curl gnupg2 ca-certificates xvfb x11vnc fluxbox xterm procps cabextract
mkdir -pm755 /etc/apt/keyrings
wget -qO- https://dl.winehq.org/wine-builds/winehq.key | gpg --dearmor --yes -o /etc/apt/keyrings/winehq-archive.key
CODENAME=$(. /etc/os-release; echo "$VERSION_CODENAME")
wget -qNP /etc/apt/sources.list.d/ "https://dl.winehq.org/wine-builds/ubuntu/dists/$CODENAME/winehq-$CODENAME.sources"
apt-get update -y
apt-get install -y --install-recommends winehq-stable

say "3/6 Creating user '$U' and the virtual desktop service"
id "$U" >/dev/null 2>&1 || useradd -m -s /bin/bash "$U"
mkdir -p "$H/.vnc"
x11vnc -storepasswd "$VNCPW" "$H/.vnc/passwd" >/dev/null
chown -R "$U:$U" "$H/.vnc"

cat > /usr/local/bin/gold-copier-desktop <<'EOS'
#!/usr/bin/env bash
# Virtual screen :1 + window manager + VNC (localhost only) + watchdog for both MT5 terminals.
export DISPLAY=:1 HOME=/home/trader WINEPREFIX=/home/trader/.mt5 WINEDEBUG=-all
PF="$WINEPREFIX/drive_c/Program Files"
Xvfb :1 -screen 0 1600x900x24 -nolisten tcp &
XV=$!
sleep 3
while kill -0 "$XV" 2>/dev/null; do
  pgrep -u trader -x fluxbox >/dev/null || (fluxbox >/dev/null 2>&1 &)
  pgrep -u trader -x x11vnc >/dev/null || x11vnc -display :1 -localhost -rfbport 5900 -forever -shared \
      -rfbauth "$HOME/.vnc/passwd" -bg -quiet -o "$HOME/x11vnc.log"
  if [ -f "$HOME/.copier-ready" ]; then
    for d in "MT5 Source" "MT5 Target"; do
      if ! pgrep -u trader -f "$d.terminal64" >/dev/null; then
        echo "$(date -u) starting $d" >> "$HOME/watchdog.log"
        (cd "$PF/$d" && nohup wine "$PF/$d/terminal64.exe" /portable >/dev/null 2>&1 &)
      fi
    done
  fi
  sleep 30
done
exit 1
EOS
chmod +x /usr/local/bin/gold-copier-desktop

cat > /etc/systemd/system/gold-copier.service <<'EOS'
[Unit]
Description=Gold Copier desktop (Xvfb + VNC + two MT5 terminals)
After=network-online.target
Wants=network-online.target

[Service]
User=trader
ExecStart=/usr/local/bin/gold-copier-desktop
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOS
systemctl daemon-reload
systemctl enable --now gold-copier.service
sleep 5

say "4/6 Preparing Wine and downloading MetaTrader 5 + the copier EAs"
sudo -u "$U" mkdir -p "$H/gold-copier"
for f in GoldCopierMaster.mq5 GoldCopierSlave.mq5; do
  sudo -u "$U" wget -qO "$H/gold-copier/$f" "$RAW/$f"
done
sudo -u "$U" wget -qO "$H/gold-copier/mt5setup.exe" "$MT5_SETUP_URL"
sudo -u "$U" env DISPLAY=:1 WINEPREFIX="$PFX" WINEDEBUG=-all WINEDLLOVERRIDES="mscoree=;mshtml=" wineboot -i >/dev/null 2>&1 || true
sudo -u "$U" env DISPLAY=:1 WINEPREFIX="$PFX" WINEDEBUG=-all \
  wine reg add 'HKEY_CURRENT_USER\Software\Wine' /v Version /t REG_SZ /d win10 /f >/dev/null 2>&1 || true

if [ ! -f "$PF/MetaTrader 5/terminal64.exe" ]; then
  say "5/6 ACTION NEEDED: open VNC now and click through the MetaTrader 5 installer"
  echo "   On your Mac: Finder > Go > Connect to Server > vnc://localhost:5901"
  echo "   (keep this SSH window open — it carries the secure tunnel)"
  echo "   Click 'Next' / 'Finish'. If MT5 opens afterwards, just close it."
  sudo -u "$U" env DISPLAY=:1 WINEPREFIX="$PFX" WINEDEBUG=-all \
    nohup wine "$H/gold-copier/mt5setup.exe" >/dev/null 2>&1 &
  for i in $(seq 1 360); do
    [ -f "$PF/MetaTrader 5/terminal64.exe" ] && break
    sleep 5
  done
  [ -f "$PF/MetaTrader 5/terminal64.exe" ] || { echo "MT5 was not installed after 30 min. Re-run this script."; exit 1; }
  echo "   MetaTrader 5 installed. Finishing in 30 seconds..."
  sleep 30
fi
sudo -u "$U" env WINEPREFIX="$PFX" wineserver -k || true
sleep 3

say "6/6 Creating 'MT5 Source' and 'MT5 Target', installing and compiling the EAs"
for d in "MT5 Source" "MT5 Target"; do
  if [ ! -d "$PF/$d" ]; then
    cp -a "$PF/MetaTrader 5" "$PF/$d"
  fi
  mkdir -p "$PF/$d/MQL5/Experts/GoldCopier"
  cp "$H/gold-copier/"*.mq5 "$PF/$d/MQL5/Experts/GoldCopier/"
  chown -R "$U:$U" "$PF/$d"
  EDITOR=$(ls "$PF/$d" | grep -i '^metaeditor64.exe$' | head -1 || true)
  if [ -n "$EDITOR" ]; then
    sudo -u "$U" env DISPLAY=:1 WINEPREFIX="$PFX" WINEDEBUG=-all timeout 180 \
      wine "C:\\Program Files\\$d\\$EDITOR" /portable \
      "/compile:C:\\Program Files\\$d\\MQL5\\Experts\\GoldCopier" /log >/dev/null 2>&1 || true
  fi
  if ls "$PF/$d/MQL5/Experts/GoldCopier/"*.ex5 >/dev/null 2>&1; then
    echo "   $d: EAs compiled"
  else
    echo "   $d: EAs not compiled automatically — compile them in MetaEditor (README step 6)"
  fi
done
sudo -u "$U" touch "$H/.copier-ready"
systemctl restart gold-copier.service

say "Done. Both MT5 terminals will appear in VNC within ~1 minute and restart automatically after crashes or reboots."
echo "Next: follow README steps 5-8 (log in, enable Algo Trading + Telegram, attach the EAs)."
