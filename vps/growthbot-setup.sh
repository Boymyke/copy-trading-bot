#!/usr/bin/env bash
# GrowthBot - test, validate and switch on the trading robot on the Exness terminal.
#
#   curl -fsSL https://raw.githubusercontent.com/Boymyke/copy-trading-bot/<ref>/vps/growthbot-setup.sh | GC_REF=<ref> bash
#
# Runs in the background (safe to close the window). It:
#   1. makes a separate "MT5 Tester" copy of the Exness terminal (no robots attached)
#   2. compiles GrowthBot and backtests 5 settings on months 6..2 ago with a $12 deposit
#   3. re-tests the best one on the last 2 months it has never seen
#   4. only if that is also profitable: puts GrowthBot live on the Exness terminal
#      (replacing the gold copier) - otherwise it attaches it in stand-by and tells you why
#   5. sends the full results to Telegram
# GB_FORCE=1 puts the best setting live even if the unseen-months check fails.
set -euo pipefail

REF="${GC_REF:-main}"
RAW="https://raw.githubusercontent.com/Boymyke/copy-trading-bot/$REF/vps"
U=trader
H=/home/$U
PFX=$H/.mt5
C="$PFX/drive_c"
PF="$C/Program Files"
LOG=/root/growthbot-setup.log
MAIN="XAUUSDm"

[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
if [ -z "${GB_CHILD:-}" ]; then
  SELF=/root/growthbot-setup.sh
  curl -fsSL -H 'Cache-Control: no-cache' -o "$SELF" "$RAW/growthbot-setup.sh"
  : > "$LOG"
  GB_CHILD=1 GC_REF="$REF" GB_FORCE="${GB_FORCE:-}" setsid bash "$SELF" >>"$LOG" 2>&1 </dev/null &
  echo
  echo "GrowthBot setup is running in the background (about 30-90 minutes)."
  echo "You can close this window. Results arrive in Telegram."
  echo "To watch it: tail -f $LOG   (Ctrl+C stops watching only)"
  exit 0
fi

say() { echo; echo "== $(date -u '+%H:%M:%S') $*"; }
w()   { sudo -u "$U" env DISPLAY=:1 WINEPREFIX="$PFX" WINEDEBUG=-all "$@"; }
u16() { iconv -f UTF-16LE -t UTF-8 2>/dev/null | tr -d '\r' || true; }
utf16() { printf '\xff\xfe'; iconv -f UTF-8 -t UTF-16LE; }

COMMON=$(find "$C/users" -type d -path '*MetaQuotes/Terminal/Common' 2>/dev/null | head -1 || true)
TGF="$COMMON/Files/gold_copier_telegram.txt"
tg() {
  python3 - "$TGF" "$1" <<'PY' || true
import sys, json, urllib.request, urllib.parse
cfg = {}
try:
    for l in open(sys.argv[1], encoding="utf-8", errors="replace"):
        k, _, v = l.strip().partition("=")
        cfg[k] = v.strip()
    data = urllib.parse.urlencode({"chat_id": cfg["chat"], "text": sys.argv[2][:4000]}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{cfg['token']}/sendMessage", data=data, timeout=20)
except Exception as e:
    print("telegram:", e)
PY
}
fail() { echo "FAILED: $*"; tg "🔴 GrowthBot setup failed: $*"; exit 1; }

# ------------------------------------------------------------------ 1
say "1/5 Finding the Exness terminal"
EXNAME=""
for d in "MT5 Source" "MT5 Target"; do
  [ "$(cat "$PF/$d/gc_role" 2>/dev/null || true)" = slave ] && EXNAME="$d"
done
[ -n "$EXNAME" ] || fail "could not find the Exness terminal (run setup-copier.sh first)"
EX="$PF/$EXNAME"
ln -sfn "$EX" "$C/gc_slave"
echo "   Exness terminal: $EXNAME"
tg "⚙️ GrowthBot setup started. Testing the strategy on the last 6 months of real prices - results in about an hour."

TD="$PF/MT5 Tester"
if [ ! -f "$TD/terminal64.exe" ]; then
  echo "   creating the tester copy (no robots attached, never trades)"
  cp -a "$EX" "$TD"
fi
rm -f "$TD/gc_role"
find "$TD/MQL5/Profiles/Charts" -name '*.chr' -delete 2>/dev/null || true   # no live robots in the tester copy
rm -rf "$TD/MQL5/Experts/GoldCopier"
ln -sfn "$TD" "$C/gc_tester"
mkdir -p "$TD/MQL5/Profiles/Tester" "$TD/reports"
chown -R "$U:$U" "$TD"; chown -h "$U:$U" "$C/gc_tester" "$C/gc_slave"

# ------------------------------------------------------------------ 2
say "2/5 Compiling GrowthBot"
curl -fsSL -H 'Cache-Control: no-cache' -o /tmp/GrowthBot.mq5 "$RAW/GrowthBot.mq5?v=$(date +%s)"
for link in gc_tester gc_slave; do
  dir="$C/$link"
  mkdir -p "$dir/MQL5/Experts/GrowthBot"
  cp /tmp/GrowthBot.mq5 "$dir/MQL5/Experts/GrowthBot/GrowthBot.mq5"
  chown -R "$U:$U" "$dir/MQL5/Experts/GrowthBot"
  ED=$(ls "$dir/" | grep -i '^metaeditor64\.exe$' | head -1 || true)
  [ -n "$ED" ] || fail "MetaEditor not found in $link"
  (cd "$dir" && w timeout 180 wine "C:\\$link\\$ED" /portable \
      "/compile:C:\\$link\\MQL5\\Experts\\GrowthBot\\GrowthBot.mq5" /log >/dev/null 2>&1) || true
  if [ "$dir/MQL5/Experts/GrowthBot/GrowthBot.ex5" -nt "$dir/MQL5/Experts/GrowthBot/GrowthBot.mq5" ]; then
    echo "   OK compiled in $link"
  else
    ERR=$(u16 < "$dir/MQL5/Experts/GrowthBot/GrowthBot.log" | grep -iE ' error|: error' | head -5 || true)
    echo "$ERR"
    fail "GrowthBot did not compile: $ERR"
  fi
done

# ------------------------------------------------------------------ 3
say "3/5 Backtesting 5 settings on months 6..2 ago (deposit \$12)"
TRAIN_FROM=$(date -u -d '180 days ago' +%Y.%m.%d)
TRAIN_TO=$(date -u -d '60 days ago' +%Y.%m.%d)
CHECK_FROM=$TRAIN_TO
CHECK_TO=$(date -u +%Y.%m.%d)

params() {  # $1 symbols  $2 TpR  $3 TradingEnabled
  printf 'InpSymbols=%s\r\nEntryTF=5\r\nTrendTF=16385\r\nEmaPullback=20\r\nEmaTrendFast=50\r\nEmaTrendSlow=200\r\nRsiPeriod=14\r\nAtrPeriod=14\r\nSlAtrMult=1.5\r\nTpR=%s\r\nBreakevenR=1.0\r\nRiskPercent=5.0\r\nMaxRiskPercent=10.0\r\nSessionStartGMT=7\r\nSessionEndGMT=17\r\nMaxSpreadFracOfSL=0.25\r\nMaxTradesPerDay=6\r\nMaxOpenPositions=2\r\nDailyLossLimitPct=15.0\r\nStopBelowEquity=0\r\nGoalBalance=1000\r\nMagic=26102801\r\nTradingEnabled=%s\r\n' "$1" "$2" "$3"
}

run_test() {  # $1 name  $2 symbols  $3 TpR  $4 from  $5 to
  local name=$1
  params "$2" "$3" true | utf16 > "$TD/MQL5/Profiles/Tester/$name.set"
  printf '[Tester]\r\nExpert=GrowthBot\\GrowthBot\r\nExpertParameters=%s.set\r\nSymbol=%s\r\nPeriod=M5\r\nModel=1\r\nExecutionMode=0\r\nOptimization=0\r\nFromDate=%s\r\nToDate=%s\r\nForwardMode=0\r\nDeposit=12\r\nCurrency=USD\r\nProfitInPips=0\r\nLeverage=1:500\r\nReport=reports\\%s\r\nReplaceReport=1\r\nShutdownTerminal=1\r\nVisual=0\r\n' \
    "$name" "$MAIN" "$4" "$5" "$name" > "$C/gb_$name.ini"
  chown "$U:$U" "$TD/MQL5/Profiles/Tester/$name.set" "$C/gb_$name.ini"
  rm -f "$TD/reports/$name".htm*
  (cd "$TD" && w timeout 3600 wine terminal64.exe /portable "/config:C:\\gb_$name.ini" >/dev/null 2>&1) || true
  for i in $(seq 1 360); do pgrep -u "$U" -f 'MT5 Tester.terminal64' >/dev/null || break; sleep 10; done
  pkill -u "$U" -f 'MT5 Tester.terminal64' 2>/dev/null || true
  python3 - "$TD/reports/$name" <<'PY'
import sys, re, glob
f = sorted(glob.glob(sys.argv[1] + ".htm*"))
if not f:
    print("NA NA NA NA"); sys.exit()
raw = open(f[0], "rb").read()
try: t = raw.decode("utf-16")
except Exception: t = raw.decode("utf-8", "replace")
t = re.sub(r"<[^>]+>", " ", t).replace("&nbsp;", " ")
t = re.sub(r"\s+", " ", t)
def num(label):
    m = re.search(re.escape(label) + r"\s*(-?\d[\d ]*(?:\.\d+)?)", t)
    return m.group(1).replace(" ", "") if m else "NA"
dd = re.search(r"Equity Drawdown Maximal:\s*[-\d ]+(?:\.\d+)?\s*\(([\d.]+)%\)", t)
print(num("Total Net Profit:"), num("Profit Factor:"), num("Total Trades:"), dd.group(1) if dd else "NA")
PY
}

declare -A SYMS TPR
SYMS[c1]="EURUSD,GBPUSD,USDJPY,XAUUSD";        TPR[c1]=2.0
SYMS[c2]="EURUSD,GBPUSD,USDJPY,XAUUSD";        TPR[c2]=1.5
SYMS[c3]="EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD"; TPR[c3]=2.0
SYMS[c4]="EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD"; TPR[c4]=1.5
SYMS[c5]="XAUUSD";                             TPR[c5]=2.0

SUMMARY="Backtest $TRAIN_FROM → $TRAIN_TO, \$12 start:"
BEST=""; BESTNET=-999999; BESTANY=""; BESTANYNET=-999999
for c in c1 c2 c3 c4 c5; do
  echo "   running $c: ${SYMS[$c]} TpR ${TPR[$c]}"
  read -r NET PFAC TR DDP < <(run_test "train_$c" "${SYMS[$c]}" "${TPR[$c]}" "$TRAIN_FROM" "$TRAIN_TO")
  NET=${NET:-NA}; PFAC=${PFAC:-NA}; TR=${TR:-NA}; DDP=${DDP:-NA}
  echo "   $c -> net $NET, profit factor $PFAC, trades $TR, max drawdown $DDP%"
  SUMMARY="$SUMMARY
• ${SYMS[$c]} (target ${TPR[$c]}R): net \$$NET, PF $PFAC, $TR trades, worst dip $DDP%"
  ok=$(python3 -c "
n,p,t,d='$NET','$PFAC','$TR','$DDP'
try: print(int(float(n)>0 and float(p)>=1.15 and float(t)>=30 and float(d)<=60))
except: print(0)")
  better=$(python3 -c "print(int(float('$NET' if '$NET'!='NA' else -1e9) > $BESTNET))")
  if [ "$ok" = 1 ] && [ "$better" = 1 ]; then BEST=$c; BESTNET=$NET; fi
  better=$(python3 -c "print(int(float('$NET' if '$NET'!='NA' else -1e9) > $BESTANYNET))")
  if [ "$better" = 1 ] && [ "$NET" != NA ]; then BESTANY=$c; BESTANYNET=$NET; fi
done
echo "$SUMMARY"

# ------------------------------------------------------------------ 4
say "4/5 Checking the best setting on the last 2 months (never used for choosing)"
LIVE=false
PICK="$BEST"
REASON=""
if [ -z "$PICK" ] && [ -n "${GB_FORCE:-}" ]; then PICK="$BESTANY"; fi
if [ -n "$PICK" ]; then
  read -r NET PFAC TR DDP < <(run_test "check_$PICK" "${SYMS[$PICK]}" "${TPR[$PICK]}" "$CHECK_FROM" "$CHECK_TO")
  NET=${NET:-NA}; PFAC=${PFAC:-NA}; TR=${TR:-NA}; DDP=${DDP:-NA}
  echo "   check -> net $NET, profit factor $PFAC, trades $TR, max drawdown $DDP%"
  SUMMARY="$SUMMARY

Unseen check $CHECK_FROM → $CHECK_TO with ${SYMS[$PICK]} (target ${TPR[$PICK]}R):
net \$$NET, PF $PFAC, $TR trades, worst dip $DDP%"
  passed=$(python3 -c "
try: print(int(float('$NET')>0 and float('$PFAC')>=1.0 and float('$TR')>=8))
except: print(0)")
  if [ "$passed" = 1 ] || [ -n "${GB_FORCE:-}" ]; then LIVE=true; fi
  [ "$passed" = 1 ] || REASON="the best setting lost money on the 2 months it had not seen"
else
  REASON="no setting was profitable enough in testing (needs profit, profit factor ≥ 1.15, ≥ 30 trades, worst dip ≤ 60%)"
  PICK="$BESTANY"; [ -n "$PICK" ] || PICK=c1
fi

# ------------------------------------------------------------------ 5
say "5/5 Attaching GrowthBot to the Exness terminal (live=$LIVE)"
mkdir -p "$EX/MQL5/Presets"
params "${SYMS[$PICK]}" "${TPR[$PICK]}" "$LIVE" | utf16 > "$EX/MQL5/Presets/gb_live.set"
chown "$U:$U" "$EX/MQL5/Presets/gb_live.set"

python3 - "$C/gc_slave.ini" <<'PY'
import sys
p = sys.argv[1]
try: raw = open(p, "rb").read().decode("utf-8", "replace").replace("\r", "")
except OSError: raw = ""
keep, sec = [], None
for line in raw.split("\n"):
    if line.startswith("["):
        sec = line.strip()
    if sec != "[StartUp]" and line != "":
        keep.append(line)
keep += ["[StartUp]", "Expert=GrowthBot\\GrowthBot", "ExpertParameters=gb_live.set", "Symbol=XAUUSDm", "Period=M5"]
open(p, "wb").write(("\r\n".join(keep) + "\r\n").encode())
PY
chown "$U:$U" "$C/gc_slave.ini"; chmod 600 "$C/gc_slave.ini"

# restart the Exness terminal without the old copier chart
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
    if "GoldCopierSlave" in t or "GrowthBot" in t:
        os.remove(f); print("   removed old chart", os.path.basename(f))
PY
sudo -u "$U" touch "$READY"
curl -fsSL -H 'Cache-Control: no-cache' -o /usr/local/bin/gc-telegram.py "$RAW/gc-telegram.py?v=$(date +%s)" && \
  chmod 755 /usr/local/bin/gc-telegram.py && systemctl restart gold-copier-telegram.service || true
echo "   the watchdog restarts the Exness terminal with GrowthBot within 30 seconds"

if [ "$LIVE" = true ]; then
  tg "✅ GrowthBot is LIVE on your Exness account.

$SUMMARY

You'll get a message for every trade opened and closed, plus a daily summary. Past results do not guarantee future profit."
else
  tg "⏸ GrowthBot is installed but NOT trading: $REASON.

$SUMMARY

I did not switch it on, because a setting that lost money in testing would most likely lose your \$12. To switch it on anyway, run the setup again with GB_FORCE=1."
fi
say "Done."
