#!/usr/bin/env bash
# Switch GrowthBot's live trading on or off on the Exness terminal (no re-testing).
#   curl -fsSL https://raw.githubusercontent.com/Boymyke/copy-trading-bot/<ref>/vps/growthbot-switch.sh | bash -s -- on
#   ... | bash -s -- off
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
MODE="${1:-on}"
case "$MODE" in on) VAL=true ;; off) VAL=false ;; *) echo "Use: on | off"; exit 1 ;; esac
U=trader; H=/home/$U; PF="$H/.mt5/drive_c/Program Files"

EXNAME=""
for d in "MT5 Source" "MT5 Target"; do
  [ "$(cat "$PF/$d/gc_role" 2>/dev/null || true)" = slave ] && EXNAME="$d"
done
[ -n "$EXNAME" ] || { echo "Exness terminal not found"; exit 1; }
EX="$PF/$EXNAME"
SET="$EX/MQL5/Presets/gb_live.set"
[ -f "$SET" ] || { echo "GrowthBot is not installed yet - run growthbot-setup.sh first"; exit 1; }

python3 - "$SET" "$VAL" <<'PY'
import sys, re
p, val = sys.argv[1], sys.argv[2]
t = open(p, "rb").read().decode("utf-16")
t, n = re.subn(r"TradingEnabled=\w+", "TradingEnabled=" + val, t)
if n == 0:
    t = t.rstrip("\r\n") + "\r\nTradingEnabled=" + val + "\r\n"
open(p, "wb").write(b"\xff\xfe" + t.encode("utf-16-le"))
PY
chown "$U:$U" "$SET"

# restart the Exness terminal so GrowthBot reloads with the new setting;
# remove saved GrowthBot charts so an old copy with the previous setting cannot start first
READY="$H/.copier-ready"
rm -f "$READY"
pkill -u "$U" -f "$EXNAME.terminal64" 2>/dev/null || true
for i in $(seq 1 20); do pgrep -u "$U" -f "$EXNAME.terminal64" >/dev/null || break; sleep 1; done
python3 - "$EX/MQL5/Profiles/Charts" <<'PY'
import sys, os, glob
for f in glob.glob(os.path.join(sys.argv[1], "*", "*.chr")):
    raw = open(f, "rb").read()
    try: t = raw.decode("utf-16")
    except Exception: t = raw.decode("utf-8", "replace")
    if "GrowthBot" in t or "GoldCopierSlave" in t:
        os.remove(f)
PY
sudo -u "$U" touch "$READY"
echo "GrowthBot trading switched $MODE. The Exness terminal restarts within 30 seconds;"
echo "Telegram will show '🤖 GrowthBot started'$([ "$VAL" = false ] && echo " ... Trading is OFF")."
