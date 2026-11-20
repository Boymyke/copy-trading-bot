#!/bin/bash
# Double-click to install the Breakout6H robot into MetaTrader 5 on this Mac.
# It copies Breakout6H.mq5 (from the same folder as this file) into MT5's
# MQL5/Experts folder(s) and compiles it with MT5's own MetaEditor.
# It does not log in, trade, or change any MT5 setting.

cd "$(dirname "$0")" || exit 1
SRC="$PWD/Breakout6H.mq5"
LOG="$PWD/Install Breakout6H - result.txt"
exec > >(tee "$LOG") 2>&1

echo "=== Installing Breakout6H into MetaTrader 5 ==="
date
if [ ! -f "$SRC" ]; then
  echo "!! Breakout6H.mq5 is not next to this file ($PWD). Nothing done."
  read -r -p "Press Return to close." _; exit 1
fi

# 1) Find every MetaTrader 5 MQL5 folder on this Mac
FOUND=()
while IFS= read -r -d '' term; do
  dir="$(dirname "$term")"
  [ -d "$dir/MQL5" ] && FOUND+=("$dir/MQL5")
done < <(find "$HOME/Library/Application Support" "$HOME/.wine" "$HOME/Library/Containers" -maxdepth 9 -iname "terminal64.exe" -print0 2>/dev/null)
while IFS= read -r -d '' mql; do
  FOUND+=("$mql")
done < <(find "$HOME/Library/Application Support" "$HOME/.wine" -maxdepth 12 -type d -path "*MetaQuotes/Terminal/*/MQL5" -print0 2>/dev/null)

if [ ${#FOUND[@]} -eq 0 ]; then
  echo "!! Could not find MetaTrader 5's MQL5 folder. Send this window's text to Claude."
  read -r -p "Press Return to close." _; exit 1
fi

# de-duplicate
UNIQ=()
for m in "${FOUND[@]}"; do
  skip=0; for u in "${UNIQ[@]}"; do [ "$u" = "$m" ] && skip=1; done
  [ $skip -eq 0 ] && UNIQ+=("$m")
done

# 2) Copy the robot into each Experts folder
for m in "${UNIQ[@]}"; do
  mkdir -p "$m/Experts"
  cp -f "$SRC" "$m/Experts/Breakout6H.mq5" && echo "Copied to: $m/Experts/"
done

# 3) Compile with MetaEditor through MT5's bundled Wine
WINE=""
for app in "/Applications/MetaTrader 5.app" "$HOME/Applications/MetaTrader 5.app"; do
  for b in "$app/Contents/SharedSupport/wine/bin/wine64" "$app/Contents/SharedSupport/wine/bin/wine" \
           "$app/Contents/SharedSupport/metatrader5/wine/bin/wine64" "$app/Contents/SharedSupport/metatrader5/wine/bin/wine"; do
    [ -x "$b" ] && { WINE="$b"; break 2; }
  done
done
if [ -z "$WINE" ]; then
  WINE="$(find /Applications "$HOME/Applications" -maxdepth 7 -type f \( -name wine64 -o -name wine \) -perm -u+x 2>/dev/null | grep -i metatrader | head -1)"
fi

compiled=0
for m in "${UNIQ[@]}"; do
  term_dir="$(dirname "$m")"
  editor="$(find "$term_dir" -maxdepth 1 -iname "metaeditor64.exe" 2>/dev/null | head -1)"
  prefix="${m%%/drive_c/*}"
  if [ -z "$WINE" ] || [ -z "$editor" ] || [ "$prefix" = "$m" ]; then
    continue
  fi
  # MetaEditor dislikes spaces in command-line paths: use a space-free link to the MQL5 folder
  link="/tmp/b6h_mql5_$$"
  ln -sfn "$m" "$link"
  winlink="Z:${link//\//\\}"
  echo "Compiling with MetaEditor (this can take up to a minute)..."
  WINEPREFIX="$prefix" WINEDEBUG=-all "$WINE" "$editor" \
    "/compile:$winlink\\Experts\\Breakout6H.mq5" "/inc:$winlink" "/log:$winlink\\Experts\\Breakout6H_compile.log" >/dev/null 2>&1
  rm -f "$link"
  clog="$m/Experts/Breakout6H_compile.log"
  if [ -f "$clog" ]; then
    echo "--- compiler output ---"
    iconv -f UTF-16LE -t UTF-8 "$clog" 2>/dev/null | tr -d '\r' | tail -n 15 || tail -n 15 "$clog"
  fi
  if [ -f "$m/Experts/Breakout6H.ex5" ]; then
    echo "OK: Breakout6H.ex5 created in $m/Experts/"
    compiled=1
  fi
done

echo
if [ $compiled -eq 1 ]; then
  echo "DONE. In MT5: Navigator -> Expert Advisors -> right-click -> Refresh. Breakout6H is there."
else
  echo "The robot file is in place but could not be compiled automatically."
  echo "In MT5: Navigator -> Expert Advisors -> right-click -> Refresh, then double-click Breakout6H"
  echo "and press Compile in MetaEditor. Or send this window's text to Claude."
fi
echo "(This report is saved as 'Install Breakout6H - result.txt' next to this file.)"
read -r -p "Press Return to close." _
