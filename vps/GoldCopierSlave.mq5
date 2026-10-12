//+------------------------------------------------------------------+
//| GoldCopierSlave.mq5                                              |
//| Runs on the TARGET terminal (Exness).                            |
//| Reads the feed written by GoldCopierMaster and mirrors it:       |
//|   source position opens  -> open TargetSymbol at FixedLot        |
//|   source position closes -> close the matching target position   |
//| No SL, no TP, no trailing, no other exit logic.                  |
//| Sends Telegram messages for every copy, close and problem.       |
//+------------------------------------------------------------------+
#property copyright "Gold Copier"
#property version   "1.00"

input string TargetSymbol      = "XAUUSDm";         // Symbol to trade on this account
input double FixedLot          = 0.01;              // Fixed lot for every copied trade
input string FeedName          = "gold_copier_feed"; // Must match GoldCopierMaster
input long   Magic             = 26101201;          // Identifies trades opened by this copier
input int    SlippagePoints    = 300;               // Max slippage (points) for opens/closes
input int    MaxOpenDelaySec   = 120;               // Do not copy source trades older than this
input int    MaxFeedAgeSec     = 10;                // Feed older than this = source terminal down (no actions)
input int    CloseConfirmMs    = 400;               // Source position must be gone this long before closing
input int    TimerMs           = 100;               // Check interval in milliseconds
input string TelegramBotToken  = "";                // From @BotFather (leave empty to disable)
input string TelegramChatId    = "";                // Your numeric id (from @userinfobot)
input int    DailyReportHourGMT = 7;                // Daily "still running" message hour (GMT), -1 = off

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

ulong NowMs() { return(GetMicrosecondCount() / 1000); }

//+------------------------------------------------------------------+
int OnInit()
  {
   if(!SymbolSelect(TargetSymbol, true))
     {
      Alert("GoldCopierSlave: symbol ", TargetSymbol, " not found on this account");
      return(INIT_FAILED);
     }
   g_state_file = "gold_copier_state_" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + ".txt";
   LoadState();
   if(!EventSetMillisecondTimer(MathMax(TimerMs, 20)))
      return(INIT_FAILED);
   Notify("🟢 Gold copier started on " + AccountInfoString(ACCOUNT_SERVER) + " #" +
          IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "\nCopying " + TargetSymbol + " " +
          DoubleToString(FixedLot, 2) + " lot. Open trades tracked: " + IntegerToString(ArraySize(map_src)));
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   SaveState();
   Comment("");
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
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
         Notify("✅ Copied " + side + " " + TargetSymbol + " " + DoubleToString(FixedLot, 2) + " @ " +
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
   double lot = FixedLot;
   if(vstep > 0) lot = MathRound(lot / vstep) * vstep;
   if(lot < vmin) lot = vmin;
   return(NormalizeDouble(lot, 2));
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
   Notify("📊 Daily check: copier running. Source feed " + (g_feed_ok ? "OK" : "DOWN") +
          ", open copied trades: " + IntegerToString(ArraySize(map_src)) +
          ", balance " + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) +
          ", equity " + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + " " + AccountInfoString(ACCOUNT_CURRENCY));
  }

void UpdateComment(bool connected)
  {
   Comment("GoldCopierSlave  |  ", TargetSymbol, " ", DoubleToString(FixedLot, 2), " lot\n",
           "Source feed: ", (g_feed_ok ? "OK" : "DOWN"), " (", ArraySize(s_ticket), " open)  |  Exness connected: ",
           (connected ? "yes" : "NO"), "\nCopied trades open: ", ArraySize(map_src),
           "  |  Telegram: ", (TelegramBotToken == "" ? "off" : "on"));
  }

//+------------------------------------------------------------------+
//| Telegram                                                         |
//+------------------------------------------------------------------+
void Notify(string text)
  {
   Print("GoldCopierSlave: ", text);
   if(TelegramBotToken == "" || TelegramChatId == "") return;
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

   string body = "chat_id=" + UrlEncode(TelegramChatId) + "&text=" + UrlEncode(text);
   char data[], result[];
   int n = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(n > 0) ArrayResize(data, n - 1);   // drop trailing zero
   string headers = "Content-Type: application/x-www-form-urlencoded\r\n", resp_headers;
   ResetLastError();
   int code = WebRequest("POST", "https://api.telegram.org/bot" + TelegramBotToken + "/sendMessage",
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
