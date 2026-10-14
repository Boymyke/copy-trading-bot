#!/usr/bin/env bash
# Gold Copier - one-command "switch it on" after both MT5 terminals are logged in.
#
#   curl -fsSL https://raw.githubusercontent.com/Boymyke/copy-trading-bot/main/vps/setup-copier.sh | bash
#
# What it does (no clicking in VNC needed):
#   1. finds which terminal is TMFinancials (master) and which is Exness (slave)
#   2. downloads the latest EAs into both and compiles them
#   3. asks for your Telegram bot token + chat id (pasted here, never shown), tests them
#   4. makes both terminals start with Algo Trading ON, Telegram allowed, and the EA
#      attached to the gold chart - also after every crash or reboot
#   5. restarts both terminals and shows you whether the copier is live
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }

RAW="https://raw.githubusercontent.com/Boymyke/copy-trading-bot/main/vps"
U=trader
H=/home/$U
PFX=$H/.mt5
C="$PFX/drive_c"
PF="$C/Program Files"
MASTER_SYMBOL="XAUUSD.f"
SLAVE_SYMBOL="XAUUSDm"

say() { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }
ok()  { printf '   \033[1;32mOK\033[0m %s\n' "$*"; }
bad() { printf '   \033[1;31m!!\033[0m %s\n' "$*"; }
w()   { sudo -u "$U" env DISPLAY=:1 WINEPREFIX="$PFX" WINEDEBUG=-all "$@"; }
u16() { iconv -f UTF-16LE -t UTF-8 2>/dev/null | tr -d '\r' || true; }

servers_in() {
  cat "$PF/$1"/logs/*.log 2>/dev/null | u16 | grep -oE '(Exness|TMFinancials)[A-Za-z0-9-]*' | sort -u | tr '\n' ' ' || true
}

# ---------------------------------------------------------------- 1
say "1/5 Finding the TMFinancials (master) and Exness (slave) terminals"
MASTER=""; SLAVE=""
for d in "MT5 Source" "MT5 Target"; do
  [ -f "$PF/$d/terminal64.exe" ] || { bad "$d is missing - run install.sh first"; exit 1; }
  s="$(servers_in "$d")"
  echo "   $d: $s"
  if echo "$s" | grep -q TMFinancials && ! echo "$s" | grep -q Exness; then MASTER="$d"; fi
  if echo "$s" | grep -q Exness && ! echo "$s" | grep -q TMFinancials; then SLAVE="$d"; fi
done
if [ -z "$MASTER" ] || [ -z "$SLAVE" ] || [ "$MASTER" = "$SLAVE" ]; then
  bad "Could not tell them apart. Log one terminal into TMFinancials and the other into Exness, then re-run."
  exit 1
fi
ok "master (TMFinancials, reads trades): $MASTER"
ok "slave  (Exness, places trades):     $SLAVE"
echo master > "$PF/$MASTER/gc_role"
echo slave  > "$PF/$SLAVE/gc_role"
# paths without spaces - MT5/MetaEditor mis-read quoted "Program Files" arguments
ln -sfn "$PF/$MASTER" "$C/gc_master"
ln -sfn "$PF/$SLAVE"  "$C/gc_slave"
chown -h "$U:$U" "$C/gc_master" "$C/gc_slave" "$PF/$MASTER/gc_role" "$PF/$SLAVE/gc_role"

# ---------------------------------------------------------------- 2
say "2/5 Downloading and compiling the copier EAs"
TMPD=$(mktemp -d)
for f in GoldCopierMaster.mq5 GoldCopierSlave.mq5; do
  curl -fsSL -H 'Cache-Control: no-cache' -o "$TMPD/$f" "$RAW/$f?v=$(date +%s)"
done
COMPILED=1
for role in master slave; do
  dir="$C/gc_$role"
  ea=$([ $role = master ] && echo GoldCopierMaster || echo GoldCopierSlave)
  mkdir -p "$dir/MQL5/Experts/GoldCopier"
  cp "$TMPD/"*.mq5 "$dir/MQL5/Experts/GoldCopier/"
  chown -R "$U:$U" "$dir/MQL5/Experts/GoldCopier"
  EDITOR=$(ls "$dir" | grep -i '^metaeditor64\.exe$' | head -1 || true)
  if [ -n "$EDITOR" ]; then
    (cd "$dir" && w timeout 180 wine "C:\\gc_$role\\$EDITOR" /portable \
        "/compile:C:\\gc_$role\\MQL5\\Experts\\GoldCopier\\$ea.mq5" /log >/dev/null 2>&1) || true
  fi
  if [ "$dir/MQL5/Experts/GoldCopier/$ea.ex5" -nt "$dir/MQL5/Experts/GoldCopier/$ea.mq5" ]; then
    ok "$ea compiled"
  else
    COMPILED=0
    bad "$ea did not compile automatically."
    u16 < "$dir/MQL5/Experts/GoldCopier/$ea.log" 2>/dev/null | grep -iE 'error|warning' | head -10 | sed 's/^/      /' || true
  fi
done
rm -rf "$TMPD"

# ---------------------------------------------------------------- 3
say "3/5 Telegram alerts and /lot commands"
COMMON=$(find "$C/users" -type d -path '*MetaQuotes/Terminal/Common' 2>/dev/null | head -1 || true)
[ -n "$COMMON" ] || COMMON="$C/users/$U/AppData/Roaming/MetaQuotes/Terminal/Common"
mkdir -p "$COMMON/Files"; chown -R "$U:$U" "$C/users/$U/AppData/Roaming/MetaQuotes" 2>/dev/null || true
TGF="$COMMON/Files/gold_copier_telegram.txt"
TOKEN=""; CHAT=""
if [ -f "$TGF" ]; then
  TOKEN=$(grep '^token=' "$TGF" | cut -d= -f2- | tr -d '\r' || true)
  CHAT=$(grep '^chat=' "$TGF" | cut -d= -f2- | tr -d '\r' || true)
  echo "   Telegram is already set up (chat $CHAT). Press Enter at both questions to keep it."
fi
echo "   Your bot token is in Railway (variable TELEGRAM_BOT_TOKEN) or from @BotFather in Telegram."
read -rs -p "   Paste the bot token (Cmd+V, nothing shows), then Enter: " T </dev/tty; echo
[ -n "$T" ] && TOKEN="$T"
read -r  -p "   Your chat id (Enter = find it for me): " CI </dev/tty
[ -n "$CI" ] && CHAT="$CI"
if [ -n "$CHAT" ] && ! [[ "$CHAT" =~ ^-?[0-9]+$ ]]; then
  echo "   \"$CHAT\" is not a chat id (it is a number) - I'll find it for you instead."
  CHAT=""
fi
if [ -n "$TOKEN" ]; then
  if ! curl -fsS "https://api.telegram.org/bot$TOKEN/getMe" >/dev/null 2>&1; then
    bad "Telegram rejected that bot token - alerts stay OFF. Re-run this script to try again."
    TOKEN=""
  fi
fi
if [ -n "$TOKEN" ]; then
  WH=$(curl -fsS "https://api.telegram.org/bot$TOKEN/getWebhookInfo" | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"].get("url",""))' || true)
  if [ -n "$WH" ]; then
    echo "   This bot is still connected to another program (old Railway bot): $WH"
    read -r -p "   Disconnect it so /lot commands reach the copier? [Y/n] " A </dev/tty
    if [ "${A:-Y}" != "n" ] && [ "${A:-Y}" != "N" ]; then
      curl -fsS "https://api.telegram.org/bot$TOKEN/deleteWebhook" >/dev/null && ok "old webhook removed"
    fi
  fi
  if [ -z "$CHAT" ]; then
    read -r -p "   Open Telegram, send any message (e.g. hi) to your bot, then press Enter here... " _ </dev/tty
    CHAT=$(curl -fsS "https://api.telegram.org/bot$TOKEN/getUpdates" | python3 -c '
import sys,json
r=json.load(sys.stdin).get("result",[])
ids=[str(u[k]["chat"]["id"]) for u in r for k in ("message","edited_message") if k in u]
print(ids[-1] if ids else "")' || true)
    [ -n "$CHAT" ] && ok "found your chat id: $CHAT" || bad "No message found - re-run later to finish Telegram."
  fi
fi
if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
  if curl -fsS "https://api.telegram.org/bot$TOKEN/sendMessage" --data-urlencode "chat_id=$CHAT" \
       --data-urlencode "text=✅ Gold copier VPS: Telegram connected. You will get a message for every copied trade." >/dev/null; then
    ok "test message sent - check Telegram"
    umask 077
    printf 'token=%s\r\nchat=%s\r\n' "$TOKEN" "$CHAT" > "$TGF"
    chown "$U:$U" "$TGF"; chmod 600 "$TGF"
    umask 022
  else
    bad "could not send to chat $CHAT - Telegram not saved. Re-run and press Enter at the chat id question."
  fi
else
  echo "   Telegram skipped - the copier still works, just without messages."
fi
unset TOKEN T

# ---------------------------------------------------------------- 4
say "4/5 Start-up settings: Algo Trading ON, Telegram allowed, EA on the gold chart"
utf16() { printf '\xff\xfe'; iconv -f UTF-8 -t UTF-16LE; }
mkdir -p "$C/gc_master/MQL5/Presets" "$C/gc_slave/MQL5/Presets"
printf 'SourceSymbol=%s\r\nFeedName=gold_copier_feed\r\nPublishMs=100\r\n' "$MASTER_SYMBOL" \
  | utf16 > "$C/gc_master/MQL5/Presets/gc_master.set"
printf 'TargetSymbol=%s\r\nBaseLot=0.01\r\nBalancePerBaseLot=0\r\nMaxLot=1.00\r\nFeedName=gold_copier_feed\r\nMagic=26101201\r\nSlippagePoints=300\r\nMaxOpenDelaySec=120\r\nMaxFeedAgeSec=10\r\nCloseConfirmMs=400\r\nTimerMs=100\r\nTelegramBotToken=\r\nTelegramChatId=\r\nDailyReportHourGMT=7\r\n' "$SLAVE_SYMBOL" \
  | utf16 > "$C/gc_slave/MQL5/Presets/gc_slave.set"
for role in master slave; do
  # keep a saved login ([Common] block written by mt5-login.sh) when rewriting the config
  COMMON_BLOCK=""
  if [ -f "$C/gc_$role.ini" ]; then
    COMMON_BLOCK=$(tr -d '\r' < "$C/gc_$role.ini" | awk '/^\[/{f=($0=="[Common]")} f' | sed 's/$/\r/' || true)
  fi
  sym=$([ $role = master ] && echo "$MASTER_SYMBOL" || echo "$SLAVE_SYMBOL")
  ea=$([ $role = master ] && echo GoldCopierMaster || echo GoldCopierSlave)
  printf '[Experts]\r\nAllowLiveTrading=1\r\nAllowDllImport=0\r\nEnabled=1\r\nAccount=0\r\nProfile=0\r\nWebRequest=1\r\nWebRequestUrl=https://api.telegram.org\r\n[StartUp]\r\nExpert=GoldCopier\\%s\r\nExpertParameters=gc_%s.set\r\nSymbol=%s\r\nPeriod=M1\r\n' \
    "$ea" "$role" "$sym" > "$C/gc_$role.ini.new"
  { [ -n "$COMMON_BLOCK" ] && printf '%s\n' "$COMMON_BLOCK"; cat "$C/gc_$role.ini.new"; } > "$C/gc_$role.ini"
  rm -f "$C/gc_$role.ini.new"
  chmod 600 "$C/gc_$role.ini"
done
chown -R "$U:$U" "$C/gc_master/MQL5/Presets" "$C/gc_slave/MQL5/Presets" "$C/gc_master.ini" "$C/gc_slave.ini"
ok "settings written"

# watchdog: always start each terminal with its start-up settings (survives crashes/reboots)
cat > /usr/local/bin/gold-copier-desktop.new <<'EOS'
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
        CFG=""
        role=$(cat "$PF/$d/gc_role" 2>/dev/null || true)
        [ -n "$role" ] && [ -f "$WINEPREFIX/drive_c/gc_$role.ini" ] && CFG="/config:C:\\gc_$role.ini"
        echo "$(date -u) starting $d $CFG" >> "$HOME/watchdog.log"
        (cd "$PF/$d" && nohup wine "$PF/$d/terminal64.exe" /portable $CFG >/dev/null 2>&1 &)
      fi
    done
  fi
  sleep 30
done
exit 1
EOS
chmod +x /usr/local/bin/gold-copier-desktop.new
mv -f /usr/local/bin/gold-copier-desktop.new /usr/local/bin/gold-copier-desktop

# ---------------------------------------------------------------- 5
say "5/5 Restarting both terminals (your VNC window will blink - just reconnect if it closes)"
sudo -u "$U" touch "$H/.copier-ready"
pkill -u "$U" -f 'gc-login\.ini' 2>/dev/null || true   # leftovers from mt5-login.sh
START=$(date +%s)
systemctl restart gold-copier.service
echo "   Waiting 90 seconds for both terminals to log in and start the EAs..."
sleep 90

echo
FEED="$COMMON/Files/gold_copier_feed.txt"
if [ -f "$FEED" ] && [ $(( $(date +%s) - $(stat -c %Y "$FEED") )) -lt 15 ]; then
  HB=$(head -1 "$FEED" | tr -d '\r')
  ok "Master EA is live on TMFinancials (feed updated just now; open $MASTER_SYMBOL trades: ${HB##*|})"
  [ "$(echo "$HB" | cut -d'|' -f3)" = 1 ] || bad "...but the TMFinancials terminal says it is NOT connected to the broker"
else
  bad "Master feed is not updating - the master EA is not running on $MASTER."
fi
LOGF="$C/gc_slave/MQL5/Logs/$(date -u +%Y%m%d).log"
SL=$(u16 < "$LOGF" 2>/dev/null | grep 'GoldCopierSlave' | tail -6 || true)
if echo "$SL" | grep -q 'Gold copier started'; then
  ok "Slave EA is live on Exness:"
else
  bad "Slave EA has not reported 'started' yet. Its latest log lines:"
fi
echo "$SL" | sed 's/^/      /'
if u16 < "$LOGF" 2>/dev/null | grep -q 'Telegram blocked'; then
  bad "MT5 is blocking Telegram: in the Exness terminal open Tools > Options > Expert Advisors,"
  bad "tick 'Allow WebRequest for listed URL' and add https://api.telegram.org"
fi
[ "$COMPILED" = 1 ] || bad "Compile the EAs in MetaEditor (open it, select each GoldCopier file, press F7), then re-run this script."
echo
echo "Next: open a small XAUUSD.f trade on TMFinancials - it should appear on Exness within ~1 second."
