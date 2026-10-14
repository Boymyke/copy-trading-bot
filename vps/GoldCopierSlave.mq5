//+------------------------------------------------------------------+
//| GoldCopierSlave.mq5                                              |
//| Runs on the TARGET terminal (Exness).                            |
//| Reads the feed written by GoldCopierMaster and mirrors it:       |
//|   source position opens  -> open TargetSymbol at today's lot     |
//|   source position closes -> close the matching target position   |
//| No SL, no TP, no trailing, no other exit logic.                  |
//| Sends Telegram messages for every copy, close and problem.       |
//| Lot: auto = BaseLot per BalancePerBaseLot of balance (grows as   |
//| the account grows), or a fixed lot set from Telegram. Telegram   |
//| commands are read ONCE a day (morning) and at start-up:          |
//|   /lot 0.05   fixed lot      /lot auto   back to auto-scaling    |
//|   /per 500    auto mode: one BaseLot per 500 of balance          |
//+------------------------------------------------------------------+
#property copyright "Gold Copier"
#property version   "1.10"

input string TargetSymbol      = "XAUUSDm";         // Symbol to trade on this account
input double BaseLot           = 0.01;              // Smallest lot (auto mode adds one of these per BalancePerBaseLot)
input double BalancePerBaseLot = 0;                 // Balance per BaseLot in auto mode (0 = balance at first start)
input double MaxLot            = 1.00;              // Safety cap on the lot, whatever Telegram/auto says
input string FeedName          = "gold_copier_feed"; // Must match GoldCopierMaster
input long   Magic             = 26101201;          // Identifies trades opened by this copier
input int    SlippagePoints    = 300;               // Max slippage (points) for opens/closes
input int    MaxOpenDelaySec   = 120;               // Do not copy source trades older than this
input int    MaxFeedAgeSec     = 10;                // Feed older than this = source terminal down (no actions)
input int    CloseConfirmMs    = 400;               // Source position must be gone this long before closing
input int    TimerMs           = 100;               // Check interval in milliseconds
input string TelegramBotToken  = "";                // From @BotFather (empty = read gold_copier_telegram.txt in Common Files)
input string TelegramChatId    = "";                // Your numeric id (empty = read from the same file)
input int    DailyReportHourGMT = 7;                // Morning check hour (GMT): reads Telegram /lot, sets today's lot, sends report

// ---- feed snapshot -------------------------------------------------
ulong  s_ticket[];
int    s_type[];
long   s_age[];
double s_price[];
bool   g_feed_ok = false;
long   g_feed_gmt = 0;

// ---- persistent state ---------------------------------------------
ulong  done_src[];          // source tickets already handled (copied, skipped or failed)
ulong  map_src[];           // source ticket -> target position identifier
ulong  map_tgt[];

// ---- runtime -------------------------------------------------------
ulong  miss_src[];
ulong  miss_since[];
ulong  fail_src[];
int    fail_count[];
ulong  fail_last[];
string tg_queue[];
string g_state_file;
bool   g_feed_alerted = false;
ulong  g_feed_bad_since = 0;
bool   g_conn_alerted = false;
ulong  g_conn_bad_since = 0;
bool   g_algo_alerted = false;
bool   g_url_warned = false;
int    g_last_report_day = -1;
bool   g_first_feed = true;

// ---- lot sizing / Telegram settings ----------------------------------
string g_tg_token = "", g_tg_chat = "";
string g_lot_mode = "auto";  // "auto" or "fixed"
double g_fixed_lot = 0;
double g_per = 0;            // balance per BaseLot (auto mode)
long   g_tg_offset = 0;      // next Telegram update id to read
double g_lot = 0;            // lot used for every copy today
string g_settings_file;

// ---- single instance (a second chart with this EA must not trade) ---
string g_lock_name, g_hb_name;
bool   g_have_lock = false;
ulong  g_lock_wait_since = 0;
ulong  g_last_hb = 0;

ulong NowMs() { return(GetMicrosecondCount() / 1000); }

//+------------------------------------------------------------------+
int OnInit()
  {
   if(!SymbolSelect(TargetSymbol, true))
     {
      Alert("GoldCopierSlave: symbol ", TargetSymbol, " not found on this account");
      return(INIT_FAILED);
     }
   string login = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   g_state_file    = "gold_copier_state_" + login + ".txt";
   g_settings_file = "gold_copier_lot_" + login + ".txt";
   g_lock_name     = "GC_SLAVE_LOCK_" + login;
   g_hb_name       = "GC_SLAVE_HB_" + login;
   LoadTelegramConfig();
   LoadState();
   if(!EventSetMillisecondTimer(MathMax(TimerMs, 20)))
      return(INIT_FAILED);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(g_have_lock)
     {
      SaveState();
      GlobalVariableDel(g_lock_name);
      GlobalVariableDel(g_hb_name);
     }
   Comment("");
  }

//+------------------------------------------------------------------+
//| Only one copy of this EA per account may trade.                  |
//+------------------------------------------------------------------+
bool HoldLock()
  {
   ulong now = NowMs();
   if(g_have_lock)
     {
      if(now - g_last_hb >= 1000) { GlobalVariableSet(g_hb_name, (double)TimeLocal()); g_last_hb = now; }
      return(true);
     }
   if(g_lock_wait_since == 0) g_lock_wait_since = now;
   if(!GlobalVariableCheck(g_lock_name)) GlobalVariableTemp(g_lock_name);
   double holder = GlobalVariableGet(g_lock_name);
   // (the holder sets the heartbeat right after taking the lock; a missing heartbeat is not "stale")
   // A live holder refreshes it every second, so only call it stale after waiting 3 s ourselves.
   bool stale = (now - g_lock_wait_since > 3000 && GlobalVariableCheck(g_hb_name) &&
                 (double)TimeLocal() - GlobalVariableGet(g_hb_name) > 5);
   if((holder == 0 || stale) && GlobalVariableSetOnCondition(g_lock_name, (double)ChartID(), holder))
     {
      g_have_lock = true;
      GlobalVariableSet(g_hb_name, (double)TimeLocal());
      g_last_hb = now;
      LoadSettings();
      PollTelegram();
      g_lot = ComputeLot();
      SaveSettings();
      Notify("🟢 Gold copier started on " + AccountInfoString(ACCOUNT_SERVER) + " #" +
             IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "\nCopying " + TargetSymbol + " at " + LotText() +
             "\nOpen trades tracked: " + IntegerToString(ArraySize(map_src)) +
             "\nChange lot any time by sending /lot 0.02, /lot auto or /per 500 — applied at the next morning check (" +
             IntegerToString(DailyReportHourGMT) + ":00 GMT) or restart.");
      return(true);
     }
   if(now - g_lock_wait_since > 10000)
     {
      Print("GoldCopierSlave: another chart already runs the copier for this account - removing this copy.");
      ExpertRemove();
     }
   return(false);
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   if(!HoldLock())
     {
      Comment("GoldCopierSlave: standby (another chart is already copying for this account)");
      return;
     }
   ReadFeed();
   bool connected = (TerminalInfoInteger(TERMINAL_CONNECTED) != 0);
   CheckHealth(connected);

   if(g_feed_ok && connected)
     {
      if(g_first_feed)
        {
         // After a (re)start, never jump into source trades that are already old.
         for(int i = 0; i < ArraySize(s_ticket); i++)
            if(s_age[i] > MaxOpenDelaySec && FindIdx(done_src, s_ticket[i]) < 0 && FindIdx(map_src, s_ticket[i]) < 0)
               AddU(done_src, s_ticket[i]);
         g_first_feed = false;
         SaveState();
        }
      HandleOpens();
      HandleCloses();
     }
   DailyReport();
   FlushTelegram();
   UpdateComment(connected);
  }

//+------------------------------------------------------------------+
//| Feed                                                             |
//+------------------------------------------------------------------+
void ReadFeed()
  {
   g_feed_ok = false;
   string name = FeedName + ".txt";
   if(!FileIsExist(name, FILE_COMMON))
      return;
   int h = FileOpen(name, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE)
      return;
   ulong tk[]; int ty[]; long ag[]; double pr[];
   bool ended = false, connected = false;
   long hb = 0;
   while(!FileIsEnding(h))
     {
      string line = FileReadString(h);
      string p[];
      int n = StringSplit(line, StringGetCharacter("|", 0), p);
      if(line == "END") { ended = true; break; }
      if(n >= 3 && p[0] == "HB")
        {
         hb = StringToInteger(p[1]);
         connected = (StringToInteger(p[2]) != 0);
        }
      else if(n >= 6 && p[0] == "P")
        {
         int k = ArraySize(tk);
         ArrayResize(tk, k + 1); ArrayResize(ty, k + 1); ArrayResize(ag, k + 1); ArrayResize(pr, k + 1);
         tk[k] = (ulong)StringToInteger(p[1]);
         ty[k] = (int)StringToInteger(p[2]);
         long lag = (long)TimeGMT() - hb;
         if(lag < 0) lag = 0;
         ag[k] = StringToInteger(p[3]) + lag;
         pr[k] = StringToDouble(p[5]);
        }
     }
   FileClose(h);
   if(!ended || hb == 0)
      return;                          // partial file, try again next tick
   g_feed_gmt = hb;
   if((long)TimeGMT() - hb > MaxFeedAgeSec || !connected)
      return;                          // source terminal stopped or lost its broker connection
   ArrayResize(s_ticket, ArraySize(tk)); ArrayResize(s_type, ArraySize(tk));
   ArrayResize(s_age, ArraySize(tk));    ArrayResize(s_price, ArraySize(tk));
   for(int i = 0; i < ArraySize(tk); i++)
     { s_ticket[i] = tk[i]; s_type[i] = ty[i]; s_age[i] = ag[i]; s_price[i] = pr[i]; }
   g_feed_ok = true;
  }

//+------------------------------------------------------------------+
//| Open copies of new source positions                              |
//+------------------------------------------------------------------+
void HandleOpens()
  {
   bool algo = (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) != 0 && MQLInfoInteger(MQL_TRADE_ALLOWED) != 0);
   for(int i = 0; i < ArraySize(s_ticket); i++)
     {
      ulong src = s_ticket[i];
      if(FindIdx(done_src, src) >= 0 || FindIdx(map_src, src) >= 0)
         continue;
      string side = (s_type[i] == 0 ? "BUY" : "SELL");
      if(s_age[i] > MaxOpenDelaySec)
        {
         AddU(done_src, src);
         SaveState();
         Notify("⏭ Skipped source " + side + " #" + IntegerToString((long)src) + ": it was already open for " +
                IntegerToString(s_age[i]) + "s (limit " + IntegerToString(MaxOpenDelaySec) + "s).");
         continue;
        }
      if(!algo)
        {
         if(!g_algo_alerted)
           {
            g_algo_alerted = true;
            Notify("🔴 Algo Trading is OFF on the Exness terminal — source " + side + " #" + IntegerToString((long)src) +
                   " cannot be copied. Turn on the 'Algo Trading' button.");
           }
         continue;
        }
      int fi = FindIdx(fail_src, src);
      if(fi >= 0 && NowMs() - fail_last[fi] < 1000)
         continue;                     // wait 1 s between attempts

      string err = "";
      ulong pos_id = 0;
      double price = 0;
      if(OpenCopy(s_type[i], src, pos_id, price, err))
        {
         AddU(map_src, src);
         AddU(map_tgt, pos_id);
         AddU(done_src, src);
         if(fi >= 0) RemoveFail(fi);
         SaveState();
         Notify("✅ Copied " + side + " " + TargetSymbol + " " + DoubleToString(LotSize(), 2) + " @ " +
                DoubleToString(price, (int)SymbolInfoInteger(TargetSymbol, SYMBOL_DIGITS)) +
                "\nSource #" + IntegerToString((long)src) + " @ " + DoubleToString(s_price[i], 3) +
                " · " + IntegerToString(s_age[i]) + "s after source");
        }
      else
        {
         if(fi < 0)
           {
            AddU(fail_src, src);
            int k = ArraySize(fail_count);
            ArrayResize(fail_count, k + 1); ArrayResize(fail_last, k + 1);
            fi = k; fail_count[fi] = 0;
           }
         fail_count[fi]++;
         fail_last[fi] = NowMs();
         Print("GoldCopierSlave: open failed for source ", src, ": ", err);
         if(fail_count[fi] >= 3)
           {
            AddU(done_src, src);
            RemoveFail(fi);
            SaveState();
            Notify("🔴 Could not copy source " + side + " #" + IntegerToString((long)src) + " after 3 tries: " + err);
           }
        }
     }
  }

//+------------------------------------------------------------------+
//| Close target positions whose source position is gone             |
//+------------------------------------------------------------------+
void HandleCloses()
  {
   // forget "missing" timers for sources that are visible again
   for(int m = ArraySize(miss_src) - 1; m >= 0; m--)
      if(FindIdx(s_ticket, miss_src[m]) >= 0)
         RemoveMiss(m);

   for(int j = ArraySize(map_src) - 1; j >= 0; j--)
     {
      ulong src = map_src[j];
      ulong tgt = map_tgt[j];
      ulong ticket = 0; double vol = 0; long type = 0; double profit = 0;
      bool tgt_open = SelectTarget(tgt, src, ticket, vol, type, profit);
      if(tgt_open && ticket != tgt)
        {
         // the stored id did not match but the comment did: remember the real position id
         map_tgt[j] = (ulong)PositionGetInteger(POSITION_IDENTIFIER);
         tgt = map_tgt[j];
         SaveState();
        }

      if(!tgt_open)
        {
         if(!TargetClosedInHistory(tgt))
            continue;                  // terminal has not listed it yet; never assume closed
         // closed outside the copier (manual close, stop-out, ...): stop tracking, never reopen
         RemoveMap(j);
         SaveState();
         if(FindIdx(s_ticket, src) >= 0)
            Notify("⚠️ Target position #" + IntegerToString((long)tgt) + " was closed outside the copier while source #" +
                   IntegerToString((long)src) + " is still open. It will not be reopened.");
         continue;
        }
      if(FindIdx(s_ticket, src) >= 0)
         continue;                     // source still open

      int mi = FindIdx(miss_src, src);
      if(mi < 0)
        {
         AddU(miss_src, src);
         AddU(miss_since, NowMs());
         continue;
        }
      if(NowMs() - miss_since[mi] < (ulong)CloseConfirmMs)
         continue;

      string err = "";
      double close_price = 0;
      if(CloseTarget(ticket, vol, type, close_price, err))
        {
         RemoveMap(j);
         RemoveMiss(mi);
         SaveState();
         Notify("✅ Closed " + (type == POSITION_TYPE_BUY ? "BUY " : "SELL ") + TargetSymbol + " " + DoubleToString(vol, 2) +
                " @ " + DoubleToString(close_price, (int)SymbolInfoInteger(TargetSymbol, SYMBOL_DIGITS)) +
                " (source #" + IntegerToString((long)src) + " closed)\nP/L ≈ " + DoubleToString(profit, 2) + " " +
                AccountInfoString(ACCOUNT_CURRENCY));
        }
      else
        {
         Print("GoldCopierSlave: close failed for target ", ticket, ": ", err);
         miss_since[mi] = NowMs();     // retry after CloseConfirmMs
         Notify("🔴 Close failed for target #" + IntegerToString((long)ticket) + " (source #" + IntegerToString((long)src) +
                " closed): " + err + ". Retrying.");
        }
     }

   // Source tickets are never reused, so handled tickets are remembered permanently
   // (a source trade that briefly vanishes from the feed is never copied twice).
   // Keep only the newest 1000 to bound the state file.
   int extra = ArraySize(done_src) - 1000;
   if(extra > 0)
     {
      for(int k = 0; k < ArraySize(done_src) - extra; k++) done_src[k] = done_src[k + extra];
      ArrayResize(done_src, ArraySize(done_src) - extra);
      SaveState();
     }
  }

//+------------------------------------------------------------------+
//| Trading helpers                                                  |
//+------------------------------------------------------------------+
ENUM_ORDER_TYPE_FILLING Filling()
  {
   long modes = SymbolInfoInteger(TargetSymbol, SYMBOL_FILLING_MODE);
   if((modes & SYMBOL_FILLING_FOK) == SYMBOL_FILLING_FOK) return(ORDER_FILLING_FOK);
   if((modes & SYMBOL_FILLING_IOC) == SYMBOL_FILLING_IOC) return(ORDER_FILLING_IOC);
   return(ORDER_FILLING_RETURN);
  }

double LotSize()
  {
   double vmin  = SymbolInfoDouble(TargetSymbol, SYMBOL_VOLUME_MIN);
   double vstep = SymbolInfoDouble(TargetSymbol, SYMBOL_VOLUME_STEP);
   double vmax  = SymbolInfoDouble(TargetSymbol, SYMBOL_VOLUME_MAX);
   double lot = (g_lot > 0 ? g_lot : BaseLot);
   if(MaxLot > 0 && lot > MaxLot) lot = MaxLot;
   if(vstep > 0) lot = MathFloor(lot / vstep + 1e-9) * vstep;
   if(lot < vmin) lot = vmin;
   if(vmax > 0 && lot > vmax) lot = vmax;
   return(NormalizeDouble(lot, 2));
  }

//+------------------------------------------------------------------+
//| Lot sizing                                                       |
//+------------------------------------------------------------------+
double ComputeLot()
  {
   double lot;
   if(g_lot_mode == "fixed" && g_fixed_lot > 0)
      lot = g_fixed_lot;
   else
     {
      double bal  = AccountInfoDouble(ACCOUNT_BALANCE);
      double mult = (g_per > 0 ? MathFloor(bal / g_per + 1e-9) : 1);
      if(mult < 1) mult = 1;
      lot = BaseLot * mult;
     }
   if(MaxLot > 0 && lot > MaxLot) lot = MaxLot;
   return(NormalizeDouble(lot, 2));
  }

string LotText()
  {
   string cur = AccountInfoString(ACCOUNT_CURRENCY);
   if(g_lot_mode == "fixed")
      return(DoubleToString(LotSize(), 2) + " lot (fixed - set from Telegram)");
   return(DoubleToString(LotSize(), 2) + " lot (auto: " + DoubleToString(BaseLot, 2) + " per " +
          DoubleToString(g_per, 0) + " " + cur + " balance, cap " + DoubleToString(MaxLot, 2) + ")");
  }

void LoadSettings()
  {
   g_lot_mode = "auto"; g_fixed_lot = 0; g_per = 0;
   if(FileIsExist(g_settings_file, FILE_COMMON))
     {
      int h = FileOpen(g_settings_file, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
      if(h != INVALID_HANDLE)
        {
         while(!FileIsEnding(h))
           {
            string p[];
            if(StringSplit(FileReadString(h), StringGetCharacter("=", 0), p) < 2) continue;
            if(p[0] == "mode")   g_lot_mode  = p[1];
            if(p[0] == "fixed")  g_fixed_lot = StringToDouble(p[1]);
            if(p[0] == "per")    g_per       = StringToDouble(p[1]);
            if(p[0] == "offset") g_tg_offset = StringToInteger(p[1]);
           }
         FileClose(h);
        }
     }
   if(g_per <= 0)
     {
      g_per = (BalancePerBaseLot > 0 ? BalancePerBaseLot : AccountInfoDouble(ACCOUNT_BALANCE));
      if(g_per <= 0) g_per = 100;
     }
  }

void SaveSettings()
  {
   int h = FileOpen(g_settings_file, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE) return;
   FileWriteString(h, "mode=" + g_lot_mode + "\r\nfixed=" + DoubleToString(g_fixed_lot, 2) +
                   "\r\nper=" + DoubleToString(g_per, 2) + "\r\noffset=" + IntegerToString(g_tg_offset) + "\r\n");
   FileClose(h);
  }

//+------------------------------------------------------------------+
//| Telegram commands - read once a day, never polled continuously.  |
//+------------------------------------------------------------------+
void PollTelegram()
  {
   if(g_tg_token == "" || g_tg_chat == "") return;
   string url = "https://api.telegram.org/bot" + g_tg_token + "/getUpdates?timeout=0&allowed_updates=%5B%22message%22%5D";
   if(g_tg_offset > 0) url += "&offset=" + IntegerToString(g_tg_offset);
   char data[], result[];
   string resp_headers;
   ResetLastError();
   int code = WebRequest("GET", url, "", 10000, data, result, resp_headers);
   if(code != 200)
     {
      Print("GoldCopierSlave: Telegram getUpdates failed, HTTP ", code, " err ", GetLastError(), " ",
            CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8));
      if(code == 409)
         Notify("⚠️ Could not read your Telegram commands: another program is reading this bot's messages "
                "(the old Railway bot?). Stop it, or /lot commands will be missed.");
      return;
     }
   string json = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   string key = "\"update_id\":";
   int pos = StringFind(json, key);
   long max_id = -1;
   string changes = "";
   while(pos >= 0)
     {
      int next = StringFind(json, key, pos + 1);
      string seg = (next < 0 ? StringSubstr(json, pos) : StringSubstr(json, pos, next - pos));
      long uid = StringToInteger(StringSubstr(seg, StringLen(key)));
      if(uid > max_id) max_id = uid;
      string chat = JsonNumberAfter(seg, "\"chat\":{\"id\":");
      string text = JsonStringAfter(seg, "\"text\":\"");
      if(chat == g_tg_chat && text != "")
        {
         string r = ApplyCommand(text);
         if(r != "") changes += r + "\n";
        }
      pos = next;
     }
   if(max_id >= 0)
     {
      g_tg_offset = max_id + 1;
      // acknowledge so the same messages are not read again
      char ack[];
      WebRequest("GET", "https://api.telegram.org/bot" + g_tg_token + "/getUpdates?timeout=0&limit=1&offset=" +
                 IntegerToString(g_tg_offset), "", 5000, data, ack, resp_headers);
     }
   SaveSettings();
   if(changes != "") Notify("⚙️ Lot settings updated from Telegram:\n" + changes);
  }

string ApplyCommand(string text)
  {
   string t = text;
   StringTrimLeft(t); StringTrimRight(t);
   StringToLower(t);
   StringReplace(t, "@", " @");               // "/lot@MyBot 0.02" -> "/lot @MyBot 0.02"
   string w[];
   int n = StringSplit(t, StringGetCharacter(" ", 0), w);
   string a[]; int m = 0;
   for(int i = 0; i < n; i++)
      if(w[i] != "" && StringGetCharacter(w[i], 0) != '@') { ArrayResize(a, m + 1); a[m++] = w[i]; }
   if(m < 2) return("");
   string cmd = a[0];
   if(StringGetCharacter(cmd, 0) == '/') cmd = StringSubstr(cmd, 1);
   if(cmd == "lot")
     {
      if(a[1] == "auto") { g_lot_mode = "auto"; return("• /lot auto → auto-scaling"); }
      double v = StringToDouble(a[1]);
      if(v <= 0) return("• ignored \"" + text + "\" (lot must be a number like 0.02)");
      if(MaxLot > 0 && v > MaxLot) return("• ignored \"" + text + "\" (above the " + DoubleToString(MaxLot, 2) + " safety cap)");
      g_lot_mode = "fixed"; g_fixed_lot = v;
      return("• /lot " + DoubleToString(v, 2) + " → fixed lot");
     }
   if(cmd == "per")
     {
      double v = StringToDouble(a[1]);
      if(v <= 0) return("• ignored \"" + text + "\" (use e.g. /per 500)");
      g_per = v;
      return("• /per " + DoubleToString(v, 0) + " → auto mode uses " + DoubleToString(BaseLot, 2) + " per " + DoubleToString(v, 0) + " balance");
     }
   return("");
  }

string JsonNumberAfter(string s, string key)
  {
   int p = StringFind(s, key);
   if(p < 0) return("");
   p += StringLen(key);
   string out = "";
   for(int i = p; i < StringLen(s); i++)
     {
      ushort c = StringGetCharacter(s, i);
      if((c >= '0' && c <= '9') || (c == '-' && out == "")) out += ShortToString(c);
      else break;
     }
   return(out);
  }

string JsonStringAfter(string s, string key)
  {
   int p = StringFind(s, key);
   if(p < 0) return("");
   p += StringLen(key);
   string out = "";
   for(int i = p; i < StringLen(s); i++)
     {
      ushort c = StringGetCharacter(s, i);
      if(c == '\\' && i + 1 < StringLen(s)) { i++; out += ShortToString(StringGetCharacter(s, i)); continue; }
      if(c == '"') break;
      out += ShortToString(c);
     }
   return(out);
  }

void LoadTelegramConfig()
  {
   g_tg_token = TelegramBotToken;
   g_tg_chat  = TelegramChatId;
   if(g_tg_token != "" && g_tg_chat != "") return;
   string f = "gold_copier_telegram.txt";
   if(!FileIsExist(f, FILE_COMMON)) return;
   int h = FileOpen(f, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON | FILE_SHARE_READ);
   if(h == INVALID_HANDLE) return;
   while(!FileIsEnding(h))
     {
      string line = FileReadString(h);
      StringTrimLeft(line); StringTrimRight(line);
      int eq = StringFind(line, "=");
      if(eq <= 0) continue;
      string k = StringSubstr(line, 0, eq), v = StringSubstr(line, eq + 1);
      if(k == "token" && g_tg_token == "") g_tg_token = v;
      if(k == "chat"  && g_tg_chat  == "") g_tg_chat  = v;
     }
   FileClose(h);
  }

bool OpenCopy(int src_type, ulong src, ulong &pos_id, double &price, string &err)
  {
   MqlTradeRequest r; MqlTradeResult res;
   ZeroMemory(r); ZeroMemory(res);
   r.action       = TRADE_ACTION_DEAL;
   r.symbol       = TargetSymbol;
   r.volume       = LotSize();
   r.type         = (src_type == 0 ? ORDER_TYPE_BUY : ORDER_TYPE_SELL);
   r.price        = (src_type == 0 ? SymbolInfoDouble(TargetSymbol, SYMBOL_ASK) : SymbolInfoDouble(TargetSymbol, SYMBOL_BID));
   r.deviation    = (ulong)SlippagePoints;
   r.magic        = (ulong)Magic;
   r.comment      = "GC#" + IntegerToString((long)src);
   r.type_filling = Filling();
   bool sent = OrderSend(r, res);
   if(!sent || (res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_PLACED))
     {
      err = "retcode " + IntegerToString(res.retcode) + " " + res.comment;
      return(false);
     }
   price  = res.price;
   pos_id = res.order;
   if(res.deal > 0 && HistoryDealSelect(res.deal))
      pos_id = (ulong)HistoryDealGetInteger(res.deal, DEAL_POSITION_ID);
   return(true);
  }

bool SelectTarget(ulong pos_id, ulong src, ulong &ticket, double &vol, long &type, double &profit)
  {
   string tag = "GC#" + IntegerToString((long)src);
   for(int pass = 0; pass < 2; pass++)
     {
      for(int i = PositionsTotal() - 1; i >= 0; i--)
        {
         ulong t = PositionGetTicket(i);
         if(t == 0) continue;
         if(PositionGetString(POSITION_SYMBOL) != TargetSymbol) continue;
         if(pass == 0 && (ulong)PositionGetInteger(POSITION_IDENTIFIER) != pos_id && t != pos_id) continue;
         if(pass == 1 && ((ulong)PositionGetInteger(POSITION_MAGIC) != (ulong)Magic || PositionGetString(POSITION_COMMENT) != tag)) continue;
         ticket = t;
         vol    = PositionGetDouble(POSITION_VOLUME);
         type   = PositionGetInteger(POSITION_TYPE);
         profit = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
         return(true);
        }
     }
   return(false);
  }

bool TargetClosedInHistory(ulong pos_id)
  {
   if(!HistorySelectByPosition(pos_id)) return(false);
   for(int i = 0; i < HistoryDealsTotal(); i++)
     {
      ulong d = HistoryDealGetTicket(i);
      if(d == 0) continue;
      long entry = HistoryDealGetInteger(d, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY) return(true);
     }
   return(false);
  }

bool CloseTarget(ulong ticket, double vol, long type, double &price, string &err)
  {
   MqlTradeRequest r; MqlTradeResult res;
   ZeroMemory(r); ZeroMemory(res);
   r.action       = TRADE_ACTION_DEAL;
   r.position     = ticket;
   r.symbol       = TargetSymbol;
   r.volume       = vol;
   r.type         = (type == POSITION_TYPE_BUY ? ORDER_TYPE_SELL : ORDER_TYPE_BUY);
   r.price        = (type == POSITION_TYPE_BUY ? SymbolInfoDouble(TargetSymbol, SYMBOL_BID) : SymbolInfoDouble(TargetSymbol, SYMBOL_ASK));
   r.deviation    = (ulong)SlippagePoints;
   r.magic        = (ulong)Magic;
   r.type_filling = Filling();
   bool sent = OrderSend(r, res);
   if(!sent || (res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_PLACED))
     {
      err = "retcode " + IntegerToString(res.retcode) + " " + res.comment;
      return(false);
     }
   price = res.price;
   return(true);
  }

//+------------------------------------------------------------------+
//| Health, reporting                                                |
//+------------------------------------------------------------------+
void CheckHealth(bool connected)
  {
   ulong now = NowMs();
   if(!g_feed_ok)
     {
      if(g_feed_bad_since == 0) g_feed_bad_since = now;
      if(!g_feed_alerted && now - g_feed_bad_since > 60000)
        {
         g_feed_alerted = true;
         Notify("🔴 No live feed from the TMFinancials terminal for 60s (terminal closed, EA removed, or broker disconnected). "
                "Nothing will be copied or closed until it is back.");
        }
     }
   else
     {
      if(g_feed_alerted) Notify("🟢 Source feed is back.");
      g_feed_alerted = false;
      g_feed_bad_since = 0;
     }
   if(!connected)
     {
      if(g_conn_bad_since == 0) g_conn_bad_since = now;
      if(!g_conn_alerted && now - g_conn_bad_since > 60000)
        {
         g_conn_alerted = true;
         Notify("🔴 Exness terminal lost its broker connection for 60s.");
        }
     }
   else
     {
      if(g_conn_alerted) Notify("🟢 Exness connection is back.");
      g_conn_alerted = false;
      g_conn_bad_since = 0;
     }
   if(TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) != 0 && MQLInfoInteger(MQL_TRADE_ALLOWED) != 0)
      g_algo_alerted = false;
  }

void DailyReport()
  {
   if(DailyReportHourGMT < 0) return;
   MqlDateTime t;
   TimeToStruct(TimeGMT(), t);
   if(t.hour != DailyReportHourGMT || t.day_of_year == g_last_report_day) return;
   g_last_report_day = t.day_of_year;
   PollTelegram();
   g_lot = ComputeLot();
   SaveSettings();
   Notify("📊 Morning check: copier running. Today's lot: " + LotText() + "\nSource feed " + (g_feed_ok ? "OK" : "DOWN") +
          ", open copied trades: " + IntegerToString(ArraySize(map_src)) +
          ", balance " + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) +
          ", equity " + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + " " + AccountInfoString(ACCOUNT_CURRENCY));
  }

void UpdateComment(bool connected)
  {
   Comment("GoldCopierSlave  |  ", TargetSymbol, " ", LotText(), "\n",
           "Source feed: ", (g_feed_ok ? "OK" : "DOWN"), " (", ArraySize(s_ticket), " open)  |  Exness connected: ",
           (connected ? "yes" : "NO"), "\nCopied trades open: ", ArraySize(map_src),
           "  |  Telegram: ", (g_tg_token == "" ? "off" : "on"));
  }

//+------------------------------------------------------------------+
//| Telegram                                                         |
//+------------------------------------------------------------------+
void Notify(string text)
  {
   Print("GoldCopierSlave: ", text);
   if(g_tg_token == "" || g_tg_chat == "") return;
   int k = ArraySize(tg_queue);
   if(k > 50) return;
   ArrayResize(tg_queue, k + 1);
   tg_queue[k] = text;
  }

void FlushTelegram()
  {
   if(ArraySize(tg_queue) == 0) return;
   string text = tg_queue[0];
   for(int i = 0; i < ArraySize(tg_queue) - 1; i++) tg_queue[i] = tg_queue[i + 1];
   ArrayResize(tg_queue, ArraySize(tg_queue) - 1);

   string body = "chat_id=" + UrlEncode(g_tg_chat) + "&text=" + UrlEncode(text);
   char data[], result[];
   int n = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(n > 0) ArrayResize(data, n - 1);   // drop trailing zero
   string headers = "Content-Type: application/x-www-form-urlencoded\r\n", resp_headers;
   ResetLastError();
   int code = WebRequest("POST", "https://api.telegram.org/bot" + g_tg_token + "/sendMessage",
                         headers, 5000, data, result, resp_headers);
   if(code == -1 && !g_url_warned)
     {
      g_url_warned = true;
      Alert("GoldCopierSlave: Telegram blocked (error ", GetLastError(),
            "). Tools > Options > Expert Advisors > tick 'Allow WebRequest' and add https://api.telegram.org");
     }
   else if(code != 200 && code != -1)
      Print("GoldCopierSlave: Telegram HTTP ", code, " ", CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8));
  }

string UrlEncode(string s)
  {
   uchar b[];
   int n = StringToCharArray(s, b, 0, WHOLE_ARRAY, CP_UTF8);
   string out = "";
   for(int i = 0; i < n - 1; i++)
     {
      uchar c = b[i];
      if((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~')
         out += CharToString(c);
      else
         out += StringFormat("%%%02X", c);
     }
   return(out);
  }

//+------------------------------------------------------------------+
//| State persistence (Common Files folder)                          |
//+------------------------------------------------------------------+
void SaveState()
  {
   int h = FileOpen(g_state_file, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE) return;
   for(int i = 0; i < ArraySize(done_src); i++)
      FileWriteString(h, "D|" + IntegerToString((long)done_src[i]) + "\r\n");
   for(int j = 0; j < ArraySize(map_src); j++)
      FileWriteString(h, "M|" + IntegerToString((long)map_src[j]) + "|" + IntegerToString((long)map_tgt[j]) + "\r\n");
   FileClose(h);
  }

void LoadState()
  {
   ArrayResize(done_src, 0); ArrayResize(map_src, 0); ArrayResize(map_tgt, 0);
   if(!FileIsExist(g_state_file, FILE_COMMON)) return;
   int h = FileOpen(g_state_file, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE) return;
   while(!FileIsEnding(h))
     {
      string p[];
      int n = StringSplit(FileReadString(h), StringGetCharacter("|", 0), p);
      if(n >= 2 && p[0] == "D") AddU(done_src, (ulong)StringToInteger(p[1]));
      if(n >= 3 && p[0] == "M") { AddU(map_src, (ulong)StringToInteger(p[1])); AddU(map_tgt, (ulong)StringToInteger(p[2])); }
     }
   FileClose(h);
  }

//+------------------------------------------------------------------+
//| Small array helpers                                              |
//+------------------------------------------------------------------+
int FindIdx(const ulong &arr[], ulong v)
  {
   for(int i = 0; i < ArraySize(arr); i++) if(arr[i] == v) return(i);
   return(-1);
  }

void AddU(ulong &arr[], ulong v)
  {
   int k = ArraySize(arr);
   ArrayResize(arr, k + 1);
   arr[k] = v;
  }

void RemoveMap(int j)
  {
   for(int k = j; k < ArraySize(map_src) - 1; k++) { map_src[k] = map_src[k + 1]; map_tgt[k] = map_tgt[k + 1]; }
   ArrayResize(map_src, ArraySize(map_src) - 1);
   ArrayResize(map_tgt, ArraySize(map_tgt) - 1);
  }

void RemoveMiss(int m)
  {
   for(int k = m; k < ArraySize(miss_src) - 1; k++) { miss_src[k] = miss_src[k + 1]; miss_since[k] = miss_since[k + 1]; }
   ArrayResize(miss_src, ArraySize(miss_src) - 1);
   ArrayResize(miss_since, ArraySize(miss_since) - 1);
  }

void RemoveFail(int f)
  {
   for(int k = f; k < ArraySize(fail_src) - 1; k++)
     { fail_src[k] = fail_src[k + 1]; fail_count[k] = fail_count[k + 1]; fail_last[k] = fail_last[k + 1]; }
   ArrayResize(fail_src, ArraySize(fail_src) - 1);
   ArrayResize(fail_count, ArraySize(fail_count) - 1);
   ArrayResize(fail_last, ArraySize(fail_last) - 1);
  }
//+------------------------------------------------------------------+
