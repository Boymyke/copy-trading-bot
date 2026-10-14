#!/usr/bin/env bash
# Log one of the VPS MT5 terminals into a broker account WITHOUT typing through VNC
# (VNC often mangles symbols like = % @ * _ in passwords -> "Invalid account").
#
#   curl -fsSL https://raw.githubusercontent.com/Boymyke/copy-trading-bot/main/vps/mt5-login.sh | bash
#
# It asks for account / server / password in this SSH window, writes a one-off MT5
# start-up config, restarts the right terminal with it, then deletes the file.
# Optional argument: source | target  (default: the terminal NOT already on Exness).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }

H=/home/trader
PF="$H/.mt5/drive_c/Program Files"

servers_in() {  # which broker servers a terminal's logs mention
  cat "$PF/$1"/logs/*.log 2>/dev/null | iconv -f UTF-16LE -t UTF-8 2>/dev/null \
    | grep -oE '(Exness|TMFinancials)[A-Za-z0-9-]*' | sort -u | tr '\n' ' ' || true
}

for d in "MT5 Source" "MT5 Target"; do
  [ -f "$PF/$d/terminal64.exe" ] || { echo "$d is missing - re-run install.sh first."; exit 1; }
  echo "   $d has been used with: $(servers_in "$d")"
done

case "${1:-}" in
  source) D="MT5 Source" ;;
  target) D="MT5 Target" ;;
  "")
    if servers_in "MT5 Source" | grep -q Exness && ! servers_in "MT5 Target" | grep -q Exness; then
      D="MT5 Target"
    else
      D="MT5 Source"
    fi ;;
  *) echo "Usage: mt5-login.sh [source|target]"; exit 1 ;;
esac

echo
echo "This will log in: $D"
read -r  -p "Account number: " LOGIN  </dev/tty
read -r  -p "Server name (exactly as your phone shows it): " SERVER </dev/tty
read -rs -p "Password (paste with Cmd+V - nothing will show), then Enter: " PASS </dev/tty; echo
[ -n "$LOGIN" ] && [ -n "$SERVER" ] && [ -n "$PASS" ] || { echo "Something was empty - nothing changed."; exit 1; }

C="$H/.mt5/drive_c"
ROLE=$(cat "$PF/$D/gc_role" 2>/dev/null || true)
umask 077
if [ -n "$ROLE" ] && [ -f "$C/gc_$ROLE.ini" ]; then
  # Copier already set up: keep the login inside this terminal's permanent start-up config,
  # so it logs itself back in after every restart/reboot (file readable by root/trader only).
  INI="$C/gc_$ROLE.ini"; CFGNAME="gc_$ROLE.ini"; KEEP=1
  GC_LOGIN="$LOGIN" GC_PASS="$PASS" GC_SERVER="$SERVER" python3 - "$INI" <<'PY'
import os, sys
p = sys.argv[1]
raw = open(p, 'rb').read().decode('utf-8', 'replace').replace('\r', '')
out, skip = [], False
for line in raw.split('\n'):
    if line.startswith('['):
        skip = (line.strip() == '[Common]')
    if not skip and line != '':
        out.append(line)
common = ['[Common]', 'Login=' + os.environ['GC_LOGIN'], 'Password=' + os.environ['GC_PASS'],
          'Server=' + os.environ['GC_SERVER'], 'KeepPrivate=1']
open(p, 'wb').write(('\r\n'.join(common + out) + '\r\n').encode())
PY
else
  # Path must have no spaces: MT5 mis-parses a quoted /config:"...Program Files..." argument.
  INI="$C/gc-login.ini"; CFGNAME="gc-login.ini"; KEEP=0
  printf '[Common]\r\nLogin=%s\r\nPassword=%s\r\nServer=%s\r\nKeepPrivate=1\r\n' \
    "$LOGIN" "$PASS" "$SERVER" > "$INI"
fi
unset PASS
chown trader:trader "$INI"; chmod 600 "$INI"
umask 022

# Stop the watchdog from restarting the terminal (without the config) while we swap it.
READY="$H/.copier-ready"; HAD_READY=0
[ -f "$READY" ] && { HAD_READY=1; rm -f "$READY"; }
cleanup() {
  [ "$KEEP" = 1 ] || shred -u "$INI" 2>/dev/null || rm -f "$INI"
  [ "$HAD_READY" = 1 ] && sudo -u trader touch "$READY"
}
trap cleanup EXIT

echo "Restarting $D with the new login..."
pkill -u trader -f "$D.terminal64" 2>/dev/null || true
for i in $(seq 1 15); do pgrep -u trader -f "$D.terminal64" >/dev/null || break; sleep 1; done

sudo -u trader env DISPLAY=:1 WINEPREFIX="$H/.mt5" WINEDEBUG=-all \
  bash -c "cd \"$PF/$D\" && nohup wine terminal64.exe /portable /config:C:\\\\$CFGNAME >/dev/null 2>&1 &"

echo "Waiting 60 seconds for it to connect..."
sleep 60
echo
echo "Done. In the remote screen, the $D window should show account $LOGIN and a"
echo "green connection speed bottom-right. If the journal still says 'Invalid account',"
echo "the password or server name entered here was wrong - just run this again."
