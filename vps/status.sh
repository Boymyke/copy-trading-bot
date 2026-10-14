#!/usr/bin/env bash
# Gold Copier - quick health report.
#   curl -fsSL https://raw.githubusercontent.com/Boymyke/copy-trading-bot/main/vps/status.sh | bash
C=/home/trader/.mt5/drive_c
u16() { iconv -f UTF-16LE -t UTF-8 2>/dev/null | tr -d '\r'; }
echo "== Watchdog (last starts)"
tail -4 /home/trader/watchdog.log 2>/dev/null
echo "== Running terminals"
pgrep -u trader -af terminal64 | sed 's/^/   /' | cut -c1-150
COMMON=$(find "$C/users" -type d -path '*MetaQuotes/Terminal/Common' 2>/dev/null | head -1)
FEED="$COMMON/Files/gold_copier_feed.txt"
echo "== Master feed"
if [ -f "$FEED" ]; then echo "   updated $(( $(date +%s) - $(stat -c %Y "$FEED") ))s ago: $(head -1 "$FEED" | tr -d '\r')"; else echo "   no feed file yet"; fi
for role in master slave; do
  d="$C/gc_$role"
  echo "== $role terminal journal (last 12)"
  f=$(ls -t "$d"/logs/*.log 2>/dev/null | head -1)
  [ -n "$f" ] && u16 < "$f" | tail -12 | cut -c1-170 | sed 's/^/   /'
  echo "== $role Experts log (last 8)"
  f=$(ls -t "$d"/MQL5/Logs/*.log 2>/dev/null | head -1)
  if [ -n "$f" ]; then u16 < "$f" | tail -8 | cut -c1-170 | sed 's/^/   /'; else echo "   (no Experts log - the EA has never run)"; fi
done
echo "== Start-up config files"
ls -l "$C"/gc_*.ini "$C"/gc_master/MQL5/Experts/GoldCopier/*.ex5 "$C"/gc_slave/MQL5/Experts/GoldCopier/*.ex5 2>&1 | sed 's/^/   /'
