#!/usr/bin/env bash
# StratLab - backtest every GrowthBot strategy on forex and gold, then re-check the best ones
# on months they have never seen. Runs in the background; results go to Telegram.
#
#   curl -fsSL https://raw.githubusercontent.com/Boymyke/copy-trading-bot/<ref>/vps/stratlab.sh | GC_REF=<ref> bash
#
#   * 5 strategies x 2 markets (forex basket, gold) x 2 profit targets = 20 tests
#   * "training" period: 18 to 6 months ago, \$1,000 test balance, 1% risk per trade
#   * the 3 best (profit factor >= 1.2, >= 50 trades, worst dip <= 30%) are re-tested on the
#     last 6 months, which played no part in choosing them
#   * a winner is installed on the Exness terminal in STAND-BY (not trading). You switch it on
#     with growthbot-switch.sh when you decide to.
set -euo pipefail

REF="${GC_REF:-main}"
RAW="https://raw.githubusercontent.com/Boymyke/copy-trading-bot/$REF/vps"
U=trader
H=/home/$U
PFX=$H/.mt5
C="$PFX/drive_c"
PF="$C/Program Files"
LOG=/root/stratlab.log

[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
if [ -z "${SL_CHILD:-}" ]; then
  SELF=/root/stratlab.sh
  curl -fsSL -H 'Cache-Control: no-cache' -o "$SELF" "$RAW/stratlab.sh"
  : > "$LOG"
  SL_CHILD=1 GC_REF="$REF" setsid bash "$SELF" >>"$LOG" 2>&1 </dev/null &
  echo
  echo "StratLab is running in the background (about 2-5 hours: 20+ backtests)."
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
fail() { echo "FAILED: $*"; tg "🔴 StratLab failed: $*"; exit 1; }

say "1/4 Preparing"
rm -f /root/stratlab.winner
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
tg "🧪 StratLab started: testing 5 strategies on forex and gold over the last 18 months (20+ backtests, about 2-5 hours). I'll send progress and the final results here."

say "2/4 Compiling GrowthBot v2 (5 strategies)"
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
    fail "GrowthBot v2 did not compile: $ERR"
  fi
done

say "3/4 Backtesting"
export TD C EX TGF PFX
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
STRATS = [
    ("Trend pullback",       dict(StrategyId=1, EntryTF=15,    TrendTF=16388, SlAtrMult=1.5), (1.5, 2.5)),
    ("Channel breakout",     dict(StrategyId=2, EntryTF=16385, TrendTF=16388, SlAtrMult=2.0), (2.0, 3.0)),
    ("Range mean reversion", dict(StrategyId=3, EntryTF=16385, SlAtrMult=1.5),                (1.0, 1.5)),
    ("EMA cross + ADX",      dict(StrategyId=4, EntryTF=16385, TrendTF=16388, SlAtrMult=2.0), (2.0, 3.0)),
    ("London breakout",      dict(StrategyId=5, EntryTF=15),                                  (1.5, 2.5)),
]
MARKETS = [("Forex", "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD", "EURUSDm"), ("Gold", "XAUUSD", "XAUUSDm")]
PERIOD = {5: "M5", 15: "M15", 16385: "H1", 16388: "H4"}

now = datetime.datetime.utcnow()
fmt = lambda d: d.strftime("%Y.%m.%d")
TRAIN = (fmt(now - datetime.timedelta(days=540)), fmt(now - datetime.timedelta(days=180)))
CHECK = (fmt(now - datetime.timedelta(days=180)), fmt(now))

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

results = []
total = len(STRATS) * len(MARKETS) * 2
done = 0
for sname, sp, tprs in STRATS:
    for mname, syms, main in MARKETS:
        for tpr in tprs:
            p = dict(BASE); p.update(sp); p.update(InpSymbols=syms, TpR=tpr)
            name = f"s{p['StrategyId']}_{mname.lower()}_{str(tpr).replace('.', '')}"
            r = run(name, p, main, TRAIN)
            done += 1
            line = f"{sname} · {mname} · target {tpr}R"
            if r is None:
                print(f"   {line}: no report", flush=True)
            else:
                print(f"   {line}: net ${r['net']:.2f} on $1000, PF {r['pf']:.2f}, {r['trades']} trades, worst dip {r['dd']:.1f}%", flush=True)
                results.append(dict(label=line, params=p, main=main, **r))
            if done % 5 == 0 and done < total:
                tg(f"🧪 StratLab: {done}/{total} backtests done...")

def ok_train(r): return r["net"] > 0 and r["pf"] >= 1.2 and r["trades"] >= 50 and r["dd"] <= 30
ranked = sorted(results, key=lambda r: (ok_train(r), r["pf"]), reverse=True)
table = "\n".join(f"{'✅' if ok_train(r) else '•'} {r['label']}: {r['net']:+.0f}$ ({r['net']/10:+.1f}%), PF {r['pf']:.2f}, "
                  f"{r['trades']} trades, dip {r['dd']:.0f}%" for r in ranked)
msg = f"🧪 StratLab results {TRAIN[0]} → {TRAIN[1]} ($1,000 test balance, 1% risk/trade):\n{table}"

winners = []
for r in [r for r in ranked if ok_train(r)][:3]:
    c = run("check_" + re.sub(r"\W+", "_", r["label"]).lower(), r["params"], r["main"], CHECK)
    if c is None:
        continue
    passed = c["net"] > 0 and c["pf"] >= 1.1 and c["trades"] >= 15
    print(f"   CHECK {r['label']}: net ${c['net']:.2f}, PF {c['pf']:.2f}, {c['trades']} trades, dip {c['dd']:.1f}% -> {'PASS' if passed else 'fail'}", flush=True)
    r["check"] = c
    if passed:
        winners.append(r)

if any("check" in r for r in ranked):
    msg += f"\n\nRe-tested on unseen months {CHECK[0]} → {CHECK[1]}:\n" + "\n".join(
        f"{'✅' if r in winners else '❌'} {r['label']}: {r['check']['net']:+.0f}$, PF {r['check']['pf']:.2f}, "
        f"{r['check']['trades']} trades, dip {r['check']['dd']:.0f}%" for r in ranked if "check" in r)

if winners:
    best = max(winners, key=lambda r: r["check"]["pf"])
    live = dict(best["params"])
    live.update(RiskPercent=2.0, MaxRiskPercent=5.0, MaxOpenPositions=2, DailyLossLimitPct=10.0, TradingEnabled="false")
    os.makedirs(f"{EX}/MQL5/Presets", exist_ok=True)
    write_utf16(f"{EX}/MQL5/Presets/gb_live.set", "".join(f"{k}={v}\r\n" for k, v in live.items()))
    open("/root/stratlab.winner", "w").write(best["label"])
    msg += (f"\n\n🏆 Winner: {best['label']}. It is installed on your Exness terminal in STAND-BY (not trading).\n"
            "Next: run it on a demo account for 2-4 weeks, or switch it on with growthbot-switch.sh when you decide.\n"
            "Note: on a $12 standard account most of its trades would be skipped because even the smallest trade "
            "risks too much - a cent account (or a bigger balance) is needed to run it as tested.")
else:
    msg += ("\n\n❌ No strategy passed both the training test and the unseen-months check, so nothing was changed. "
            "Running any of these with real money would most likely lose.")
print(msg, flush=True)
tg(msg)
PY

say "4/4 Restarting the Exness terminal if a winner was installed"
if [ -f /root/stratlab.winner ]; then
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
  rm -f /root/stratlab.winner
fi
say "Done."
