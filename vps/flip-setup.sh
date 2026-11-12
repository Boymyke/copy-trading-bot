#!/usr/bin/env bash
# FlipBot - the "turn $12 into $1,000 in a month" attempt, done the mathematically best way
# (few, large bets with big targets = "bold play"), with honest odds measured first.
#
#   curl -fsSL https://raw.githubusercontent.com/Boymyke/copy-trading-bot/<ref>/vps/flip-setup.sh | GC_REF=<ref> bash
#
#   * 3 high-risk settings, each replayed on each of the last 12 calendar months starting
#     from $12: how many months reached $1,000, how many ended above $100 / $50 / below $6
#   * the best setting is installed on the Exness terminal:
#       GB_LIVE=1  -> switched ON straight away (real money)
#       otherwise  -> stand-by; switch on later with growthbot-switch.sh
set -euo pipefail

REF="${GC_REF:-main}"
RAW="https://raw.githubusercontent.com/Boymyke/copy-trading-bot/$REF/vps"
U=trader
H=/home/$U
PFX=$H/.mt5
C="$PFX/drive_c"
PF="$C/Program Files"
LOG=/root/flip-setup.log

[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
if [ -z "${SL_CHILD:-}" ]; then
  SELF=/root/flip-setup.sh
  curl -fsSL -H 'Cache-Control: no-cache' -o "$SELF" "$RAW/flip-setup.sh"
  : > "$LOG"
  SL_CHILD=1 GC_REF="$REF" GB_LIVE="${GB_LIVE:-}" GB_GOAL="${GB_GOAL:-1000}" setsid bash "$SELF" >>"$LOG" 2>&1 </dev/null &
  echo
  echo "FlipBot setup is running in the background (about 1-2 hours: 36 short backtests)."
  echo "You can close this window. Progress and results arrive in Telegram."
  echo "To watch it: tail -f $LOG   (Ctrl+C stops watching only)"
  exit 0
fi

say() { echo; echo "== $(date -u '+%H:%M:%S') $*"; }
w()   { sudo -u "$U" env DISPLAY=:1 WINEPREFIX="$PFX" WINEDEBUG=-all "$@"; }
u16() { iconv -f UTF-16LE -t UTF-8 2>/dev/null | tr -d '\r' || true; }
COMMON=$(find "$C/users" -type d -path '*MetaQuotes/Terminal/Common' 2>/dev/null | head -1 || true)
TGF="$COMMON/Files/gold_copier_telegram.txt"
tg() {
  python3 - "$TGF" "$1" <<'PY' || true
import sys, urllib.request, urllib.parse
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
fail() { echo "FAILED: $*"; tg "🔴 FlipBot setup failed: $*"; exit 1; }

say "1/4 Preparing"
EXNAME=""
for d in "MT5 Source" "MT5 Target"; do
  [ "$(cat "$PF/$d/gc_role" 2>/dev/null || true)" = slave ] && EXNAME="$d"
done
[ -n "$EXNAME" ] || fail "could not find the Exness terminal"
EX="$PF/$EXNAME"
ln -sfn "$EX" "$C/gc_slave"
TD="$PF/MT5 Tester"
[ -f "$TD/terminal64.exe" ] || cp -a "$EX" "$TD"
rm -f "$TD/gc_role"
find "$TD/MQL5/Profiles/Charts" -name '*.chr' -delete 2>/dev/null || true
rm -rf "$TD/MQL5/Experts/GoldCopier"
ln -sfn "$TD" "$C/gc_tester"
mkdir -p "$TD/MQL5/Profiles/Tester" "$TD/reports"
chown -R "$U:$U" "$TD"; chown -h "$U:$U" "$C/gc_tester" "$C/gc_slave"
tg "🎲 FlipBot setup started: replaying 3 high-risk settings on each of the last 12 months from \$12 (about 1-2 hours). Results will come here."

say "2/4 Compiling GrowthBot"
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
    ERR=$(u16 < "$dir/MQL5/Experts/GrowthBot/GrowthBot.log" | grep -iE 'error' | head -6 || true)
    echo "$ERR"
    fail "GrowthBot did not compile: $ERR"
  fi
done

say "3/4 Replaying the last 12 months from \$12"
export TD C EX TGF PFX GB_LIVE="${GB_LIVE:-}" GB_GOAL="${GB_GOAL:-1000}"
python3 - <<'PY'
import os, re, glob, shutil, subprocess, time, datetime, urllib.request, urllib.parse

TD, C, EX, TGF, PFX = (os.environ[k] for k in ("TD", "C", "EX", "TGF", "PFX"))

def tg(msg):
    try:
        cfg = dict(l.strip().partition("=")[::2] for l in open(TGF, encoding="utf-8", errors="replace") if "=" in l)
        data = urllib.parse.urlencode({"chat_id": cfg["chat"].strip(), "text": msg[:4000]}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{cfg['token'].strip()}/sendMessage", data=data, timeout=20)
    except Exception as e:
        print("telegram:", e, flush=True)

def write_utf16(path, text):
    with open(path, "wb") as f:
        f.write(b"\xff\xfe" + text.encode("utf-16-le"))
    shutil.chown(path, "trader", "trader")

BASE = dict(InpSymbols="EURUSD", EntryTF=5, TrendTF=16385, StrategyId=1, EmaPullback=20, EmaTrendFast=50,
            EmaTrendSlow=200, RsiPeriod=14, AtrPeriod=14, BreakoutBars=20, BBPeriod=20, BBDev=2.0, AdxPeriod=14,
            AdxTrendMin=25, AdxRangeMax=20, CrossFast=20, CrossSlow=50, RangeStartGMT=0, RangeEndGMT=7,
            BreakoutEndGMT=11, MaxRangeAtr=10.0, SlAtrMult=1.5, TpR=2.0, BreakevenR=1.0, RiskPercent=1.0,
            MaxRiskPercent=2.0, SessionStartGMT=6, SessionEndGMT=20, MaxSpreadFracOfSL=0.25, MaxTradesPerDay=6,
            MaxOpenPositions=3, DailyLossLimitPct=5.0, StopBelowEquity=0, GoalBalance=1000, Magic=26102801,
            TradingEnabled="true")
PERIOD = {5: "M5", 15: "M15", 16385: "H1", 16388: "H4"}

def parse(base):
    f = sorted(glob.glob(base + ".htm*"))
    if not f:
        return None
    raw = open(f[0], "rb").read()
    try: t = raw.decode("utf-16")
    except Exception: t = raw.decode("utf-8", "replace")
    t = re.sub(r"<[^>]+>", " ", t).replace("&nbsp;", " ")
    t = re.sub(r"\s+", " ", t)
    def num(label):
        m = re.search(re.escape(label) + r"\s*(-?\d[\d ]*(?:\.\d+)?)", t)
        return float(m.group(1).replace(" ", "")) if m else 0.0
    dd = re.search(r"Equity Drawdown Maximal:\s*[-\d ]+(?:\.\d+)?\s*\(([\d.]+)%\)", t)
    return dict(net=num("Total Net Profit:"), pf=num("Profit Factor:"), trades=int(num("Total Trades:")),
                dd=float(dd.group(1)) if dd else 0.0)

def run(name, params, main, period, deposit=1000):
    write_utf16(f"{TD}/MQL5/Profiles/Tester/{name}.set", "".join(f"{k}={v}\r\n" for k, v in params.items()))
    ini = (f"[Tester]\r\nExpert=GrowthBot\\GrowthBot\r\nExpertParameters={name}.set\r\nSymbol={main}\r\n"
           f"Period={PERIOD[params['EntryTF']]}\r\nModel=1\r\nExecutionMode=0\r\nOptimization=0\r\n"
           f"FromDate={period[0]}\r\nToDate={period[1]}\r\nForwardMode=0\r\nDeposit={deposit}\r\nCurrency=USD\r\n"
           f"ProfitInPips=0\r\nLeverage=1:500\r\nReport=reports\\{name}\r\nReplaceReport=1\r\nShutdownTerminal=1\r\nVisual=0\r\n")
    path = f"{C}/gb_{name}.ini"
    open(path, "w", newline="").write(ini)
    shutil.chown(path, "trader", "trader")
    for f in glob.glob(f"{TD}/reports/{name}.htm*"):
        os.remove(f)
    subprocess.run(["sudo", "-u", "trader", "env", "DISPLAY=:1", f"WINEPREFIX={PFX}", "WINEDEBUG=-all",
                    "timeout", "5400", "wine", "terminal64.exe", "/portable", f"/config:C:\\gb_{name}.ini"],
                   cwd=TD, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(540):
        if subprocess.run(["pgrep", "-u", "trader", "-f", "MT5 Tester.terminal64"], stdout=subprocess.DEVNULL).returncode:
            break
        time.sleep(10)
    subprocess.run(["pkill", "-u", "trader", "-f", "MT5 Tester.terminal64"])
    return parse(f"{TD}/reports/{name}")


import calendar, statistics
GOAL = int(float(os.environ.get("GB_GOAL") or 1000))
FLIP = dict(RiskPercent=35.0, MaxRiskPercent=60.0, BreakevenR=99, DailyLossLimitPct=0, MaxTradesPerDay=4,
            MaxOpenPositions=1, SessionStartGMT=6, SessionEndGMT=20, GoalBalance=GOAL, StopAtGoal="true",
            InpSymbols="EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,XAUUSD")
CONFIGS = [
    ("Breakout flip (1-hour chart, target 3x the risk)",  dict(StrategyId=2, EntryTF=16385, TrendTF=16388, SlAtrMult=2.0, TpR=3.0)),
    ("London-open flip (15-min chart, target 3x)",        dict(StrategyId=5, EntryTF=15, TpR=3.0)),
    ("Pullback flip (15-min chart, target 5x)",           dict(StrategyId=1, EntryTF=15, TrendTF=16388, SlAtrMult=1.5, TpR=5.0)),
]
today = datetime.date.today()
months = []
y, m = today.year, today.month
for _ in range(12):
    m -= 1
    if m == 0: y, m = y - 1, 12
    months.append((y, m))
months.reverse()

summary = []
n = 0
for ci, (label, cp) in enumerate(CONFIGS):
    p = dict(BASE); p.update(FLIP); p.update(cp)
    finals = []
    for (yy, mm) in months:
        last = calendar.monthrange(yy, mm)[1]
        r = run(f"flip{ci}_{yy}{mm:02d}", p, "EURUSDm", (f"{yy}.{mm:02d}.01", f"{yy}.{mm:02d}.{last}"), deposit=12)
        n += 1
        if r is None:
            print(f"   {label} {yy}-{mm:02d}: no report", flush=True); continue
        fin = 12 + r["net"]
        finals.append(fin)
        print(f"   {label} {yy}-{mm:02d}: ended ${fin:.2f} after {r['trades']} trades", flush=True)
        if n % 12 == 0 and n < 36: tg(f"🎲 FlipBot: {n}/36 monthly replays done...")
    if finals:
        summary.append(dict(label=label, params=p, finals=finals, hit=sum(f >= GOAL for f in finals),
                            h100=sum(f >= 100 for f in finals), h50=sum(f >= 50 for f in finals),
                            up=sum(f > 12 for f in finals), low=sum(f < 6 for f in finals),
                            med=statistics.median(finals), best=max(finals)))
if not summary:
    tg("🔴 FlipBot setup: no backtest produced a report, nothing was changed."); raise SystemExit(1)

lines = []
for r in summary:
    k = len(r["finals"])
    lines.append(f"• {r['label']}\n   reached ${GOAL}: {r['hit']} of {k} months · ended above $100: {r['h100']} · above $50: {r['h50']} · "
                 f"in profit: {r['up']} · ended under $6: {r['low']}\n   typical month ended at ${r['med']:.2f}, best month ${r['best']:.2f}")
best = max(summary, key=lambda r: (r["hit"], r["h100"], r["h50"], r["med"]))
live = os.environ.get("GB_LIVE") == "1"
lp = dict(best["params"]); lp["TradingEnabled"] = "true" if live else "false"
os.makedirs(f"{EX}/MQL5/Presets", exist_ok=True)
write_utf16(f"{EX}/MQL5/Presets/gb_live.set", "".join(f"{k}={v}\r\n" for k, v in lp.items()))
msg = ("🎲 FlipBot results - each setting started with $12 on the 1st of each of the last 12 months:\n" + "\n".join(lines) +
       f"\n\nInstalled: {best['label']}.\n" +
       ("✅ It is LIVE on your Exness account now. " if live else "⏸ It is in STAND-BY (not trading). Switch it on with growthbot-switch.sh. ") +
       "It risks about a third of the account on each trade, so most runs end with a few dollars left; "
       f"it stops by itself at ${GOAL} or when the account is too small to trade.")
print(msg, flush=True)
tg(msg)
PY

say "4/4 Loading the chosen setting on the Exness terminal"
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
say "Done."
