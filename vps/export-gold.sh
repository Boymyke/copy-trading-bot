#!/usr/bin/env bash
# Export Exness gold (XAUUSDm) 1-minute and 5-minute price history since late May to /root/gold_bars.tar.gz
# so trades can be checked against real prices. Read-only: it never trades and does not touch the live terminals.
#   curl -fsSL https://raw.githubusercontent.com/Boymyke/copy-trading-bot/<ref>/vps/export-gold.sh | bash
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
U=trader; H=/home/$U; PFX=$H/.mt5; C="$PFX/drive_c"; PF="$C/Program Files"
w() { sudo -u "$U" env DISPLAY=:1 WINEPREFIX="$PFX" WINEDEBUG=-all "$@"; }

EXNAME=""
for d in "MT5 Source" "MT5 Target"; do
  [ "$(cat "$PF/$d/gc_role" 2>/dev/null || true)" = slave ] && EXNAME="$d"
done
[ -n "$EXNAME" ] || { echo "Exness terminal not found"; exit 1; }
TD="$PF/MT5 Tester"
if [ ! -f "$TD/terminal64.exe" ]; then
  echo "Creating a separate copy of the Exness terminal for the export..."
  cp -a "$PF/$EXNAME" "$TD"
  rm -f "$TD/gc_role"
  find "$TD/MQL5/Profiles/Charts" -name '*.chr' -delete 2>/dev/null || true
fi
ln -sfn "$TD" "$C/gc_tester"
mkdir -p "$TD/MQL5/Scripts"
COMMON=$(find "$C/users" -type d -path '*MetaQuotes/Terminal/Common' 2>/dev/null | head -1)
[ -n "$COMMON" ] || { echo "MT5 common folder not found"; exit 1; }
mkdir -p "$COMMON/Files"
rm -f "$COMMON/Files"/gold_M1.csv "$COMMON/Files"/gold_M5.csv "$COMMON/Files"/gold_export_done.txt

cat > "$TD/MQL5/Scripts/ExportBars.mq5" <<'MQ'
#property copyright "export"
#property version   "1.00"
string   Sym  = "XAUUSDm";
datetime From = D'2026.05.25 00:00';

int Export(ENUM_TIMEFRAMES tf, string name)
  {
   MqlRates r[];
   int n = -1, prev = -2, stable = 0;
   for(int k = 0; k < 120 && !IsStopped(); k++)
     {
      n = CopyRates(Sym, tf, From, TimeLocal() + 172800, r);
      if(n > 0 && n == prev) { stable++; if(stable >= 4) break; }
      else stable = 0;
      prev = n;
      Sleep(3000);
     }
   if(n <= 0) { Print("ExportBars: no data for ", name, " err ", GetLastError()); return(0); }
   int h = FileOpen(name, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE) { Print("ExportBars: cannot write ", name); return(0); }
   FileWriteString(h, "epoch,time,open,high,low,close,spread_points\r\n");
   for(int i = 0; i < n; i++)
      FileWriteString(h, IntegerToString((long)r[i].time) + "," + TimeToString(r[i].time, TIME_DATE | TIME_MINUTES) + "," +
                      DoubleToString(r[i].open, 3) + "," + DoubleToString(r[i].high, 3) + "," +
                      DoubleToString(r[i].low, 3) + "," + DoubleToString(r[i].close, 3) + "," +
                      IntegerToString(r[i].spread) + "\r\n");
   FileClose(h);
   Print("ExportBars: ", name, " ", n, " bars from ", TimeToString(r[0].time), " to ", TimeToString(r[n - 1].time));
   return(n);
  }

void OnStart()
  {
   SymbolSelect(Sym, true);
   int a = Export(PERIOD_M5, "gold_M5.csv");
   int b = Export(PERIOD_M1, "gold_M1.csv");
   int h = FileOpen("gold_export_done.txt", FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h != INVALID_HANDLE) { FileWriteString(h, "M5=" + IntegerToString(a) + " M1=" + IntegerToString(b) + "\r\n"); FileClose(h); }
  }
MQ
chown -R "$U:$U" "$TD/MQL5/Scripts"; chown -h "$U:$U" "$C/gc_tester"

echo "Compiling the export script..."
ED=$(ls "$TD/" | grep -i '^metaeditor64\.exe$' | head -1)
(cd "$TD" && w timeout 180 wine "C:\\gc_tester\\$ED" /portable "/compile:C:\\gc_tester\\MQL5\\Scripts\\ExportBars.mq5" /log >/dev/null 2>&1) || true
if [ ! -f "$TD/MQL5/Scripts/ExportBars.ex5" ]; then
  echo "Compile failed:"; iconv -f UTF-16LE -t UTF-8 "$TD/MQL5/Scripts/ExportBars.log" 2>/dev/null | tr -d '\r' | grep -i error | head; exit 1
fi

printf '[Charts]\r\nMaxBars=5000000\r\n[StartUp]\r\nScript=ExportBars\r\nSymbol=XAUUSDm\r\nPeriod=M5\r\nShutdownTerminal=1\r\n' > "$C/gc_export.ini"
chown "$U:$U" "$C/gc_export.ini"
echo "Downloading gold price history from Exness (up to 15 minutes)..."
(cd "$TD" && w timeout 1200 wine terminal64.exe /portable "/config:C:\\gc_export.ini" >/dev/null 2>&1) &
for i in $(seq 1 120); do [ -f "$COMMON/Files/gold_export_done.txt" ] && break; sleep 10; done
sleep 5
pkill -u "$U" -f 'MT5 Tester.terminal64' 2>/dev/null || true

if [ ! -f "$COMMON/Files/gold_export_done.txt" ]; then
  echo "!! The export did not finish. Last lines of the terminal's log:"
  f=$(ls -t "$TD"/MQL5/Logs/*.log "$TD"/logs/*.log 2>/dev/null | head -2)
  for x in $f; do iconv -f UTF-16LE -t UTF-8 "$x" 2>/dev/null | tr -d '\r' | tail -8 | cut -c1-160; done
  exit 1
fi
cd "$COMMON/Files"
for f in gold_M5.csv gold_M1.csv; do
  [ -f "$f" ] && echo "   $f: $(($(wc -l < "$f") - 1)) bars, $(sed -n 2p "$f" | cut -d, -f2) -> $(tail -1 "$f" | tr -d '\r' | cut -d, -f2)"
done
tar czf /root/gold_bars.tar.gz gold_M5.csv $( [ -f gold_M1.csv ] && echo gold_M1.csv )
echo
echo "OK. Saved /root/gold_bars.tar.gz ($(du -h /root/gold_bars.tar.gz | cut -f1))."
echo "Now type 'exit', then on your Mac run:"
echo "   scp root@157.173.108.91:/root/gold_bars.tar.gz ~/Downloads/Trades/"
