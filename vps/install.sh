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

# Ask for anything interactive here, then hand the real work to a detached
# background process writing to a log. A dropped SSH connection (sleeping
# laptop, wifi blip) then cannot kill the install, and no terminal is needed.
LOG=/root/gold-copier-install.log
if [ -z "${GC_CHILD:-}" ]; then
  say "1/6 VNC password (for viewing the MT5 screens)"
  VNCPW=""
  if [ -f /home/trader/.vnc/passwd ]; then
    echo "   Keeping the VNC password you already set."
  else
    while [ ${#VNCPW} -lt 6 ]; do
      read -r -s -p "Choose a VNC password (6-8 characters): " VNCPW < /dev/tty; echo
    done
  fi

  SELF=/root/gold-copier-install.sh
  curl -fsSL -o "$SELF" "$RAW/install.sh?v=$(date +%s)"
  chmod +x "$SELF"
  tmux kill-session -t goldcopier 2>/dev/null || true
  pkill -f "$SELF" 2>/dev/null || true
  sleep 1
  : > "$LOG"
  GC_CHILD=1 GC_VNCPW="$VNCPW" setsid bash "$SELF" >>"$LOG" 2>&1 </dev/null &
  sleep 2
  echo
  echo "The installer is now running in the background. It keeps going even if"
  echo "this window closes or your connection drops."
  echo
  echo "   Watch it any time with:   tail -f $LOG"
  echo "   Ctrl+C only stops watching - it does NOT stop the install."
  echo
  exec tail -f "$LOG"
fi
VNCPW="${GC_VNCPW:-}"
say "1/6 Running in the background (this log: $LOG)"

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
if [ -n "$VNCPW" ]; then
  x11vnc -storepasswd "$VNCPW" "$H/.vnc/passwd" >/dev/null
fi
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
      -rfbauth "$HOME/.vnc/passwd" -bg -quiet -o "$HOME/x11vnc.log" \
      || echo "$(date -u) x11vnc failed to start" >> "$HOME/watchdog.log"
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

# MetaQuotes' installer aborts with "A debugger has been found running in your
# system" because Wine registers its own crash debugger. Remove those keys.
for k in 'HKLM\Software\Microsoft\Windows NT\CurrentVersion\AeDebug' \
         'HKLM\Software\Wow6432Node\Microsoft\Windows NT\CurrentVersion\AeDebug'; do
  sudo -u "$U" env DISPLAY=:1 WINEPREFIX="$PFX" WINEDEBUG=-all \
    wine reg delete "$k" /f >/dev/null 2>&1 || true
done
pkill -u "$U" -f mt5setup >/dev/null 2>&1 || true

if [ ! -f "$PF/MetaTrader 5/terminal64.exe" ]; then
  say "5/6 Installing MetaTrader 5 (silent - no clicking needed, up to 10 minutes)"
  sudo -u "$U" env DISPLAY=:1 WINEPREFIX="$PFX" WINEDEBUG=-all \
    nohup wine "$H/gold-copier/mt5setup.exe" /auto >/dev/null 2>&1 &
  for i in $(seq 1 60); do
    [ -f "$PF/MetaTrader 5/terminal64.exe" ] && break
    sleep 10
  done

  if [ ! -f "$PF/MetaTrader 5/terminal64.exe" ]; then
    say "5/6 Silent install did not finish - open VNC and click through the installer"
    echo "   On your Mac: Finder > Go > Connect to Server > vnc://localhost:5901"
    echo "   Click 'Next' / 'Finish'. If MT5 opens afterwards, just close it."
    echo "   (this window is safe to leave - it survives a dropped connection)"
    sudo -u "$U" env WINEPREFIX="$PFX" wineserver -k >/dev/null 2>&1 || true
    sleep 3
    sudo -u "$U" env DISPLAY=:1 WINEPREFIX="$PFX" WINEDEBUG=-all \
      nohup wine "$H/gold-copier/mt5setup.exe" >/dev/null 2>&1 &
    for i in $(seq 1 360); do
      [ -f "$PF/MetaTrader 5/terminal64.exe" ] && break
      sleep 10
    done
  fi

  [ -f "$PF/MetaTrader 5/terminal64.exe" ] || { echo "MetaTrader 5 was not installed. Re-run this script."; exit 1; }
  echo "   MetaTrader 5 installed. Settling for 30 seconds..."
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
