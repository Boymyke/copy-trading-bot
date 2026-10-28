//+------------------------------------------------------------------+
//| GrowthBot.mq5                                                    |
//| Rules-based trend-pullback scalper for small accounts.           |
//|                                                                  |
//|  Trend   : EMA50 vs EMA200 on H1, price on the trend side         |
//|  Entry   : M5 pullback that touched EMA20 in the last 3 bars,     |
//|            closes back on the trend side with RSI confirming      |
//|  Exit    : stop 1.5 x ATR(M5), target TpR x stop, stop moved to   |
//|            break-even once price has gone 1R in our favour        |
//|  Risk    : RiskPercent of equity per trade. If even the smallest  |
//|            lot would risk more than MaxRiskPercent the trade is   |
//|            skipped (so gold is only traded once the account can  |
//|            afford it). Daily loss limit, max trades/day, London + |
//|            New York session only, spread filter.                  |
//|  Reports : every open / close and a daily summary go to Telegram  |
//|            through the VPS helper (gold_copier_outbox.txt).       |
//+------------------------------------------------------------------+
#property copyright "GrowthBot"
#property version   "1.00"

#include <Trade\Trade.mqh>

input string          InpSymbols          = "EURUSD,GBPUSD,USDJPY,XAUUSD"; // Base names; broker suffix is found automatically
input ENUM_TIMEFRAMES EntryTF             = PERIOD_M5;
input ENUM_TIMEFRAMES TrendTF             = PERIOD_H1;
input int             EmaPullback         = 20;
input int             EmaTrendFast        = 50;
input int             EmaTrendSlow        = 200;
input int             RsiPeriod           = 14;
input int             AtrPeriod           = 14;
input double          SlAtrMult           = 1.5;    // stop distance = this x ATR
input double          TpR                 = 2.0;    // target = this x stop distance
input double          BreakevenR          = 1.0;    // move stop to entry after this many R
input double          RiskPercent         = 5.0;    // % of equity risked per trade
input double          MaxRiskPercent      = 10.0;   // allow the minimum lot only if it risks <= this %
input int             SessionStartGMT     = 7;
input int             SessionEndGMT       = 17;
input double          MaxSpreadFracOfSL   = 0.25;   // skip if spread > this fraction of the stop
input int             MaxTradesPerDay     = 6;
input int             MaxOpenPositions    = 2;
input double          DailyLossLimitPct   = 15.0;   // no new trades for the day after this % equity drop
input double          StopBelowEquity     = 0;      // hard floor: no new trades below this equity (0 = off)
input double          GoalBalance         = 1000;
input long            Magic               = 26102801;
input bool            TradingEnabled      = true;

CTrade   trade;
string   sym[];
int      hTF[], hTS[], hE[], hR[], hA[];
datetime lastBar[];
int      skipDay[];

int    g_day = -1;
double g_dayEq = 0, g_dayPL = 0;
int    g_dayTrades = 0, g_dayWins = 0, g_dayLosses = 0;
bool   g_dayLimitHit = false, g_floorHit = false, g_started = false;
bool   g_tester = false;
string g_pfx;

// single instance
bool   g_have_lock = false;
ulong  g_lock_wait = 0, g_last_hb = 0;

ulong NowMs() { return(GetMicrosecondCount() / 1000); }

//+------------------------------------------------------------------+
int OnInit()
  {
   g_tester = (MQLInfoInteger(MQL_TESTER) != 0 || MQLInfoInteger(MQL_OPTIMIZATION) != 0);
   g_pfx = "GB_" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "_";
   string sfx = (StringLen(_Symbol) > 6 ? StringSubstr(_Symbol, 6) : "");
   string parts[];
   int n = StringSplit(InpSymbols, ',', parts);
   for(int i = 0; i < n; i++)
     {
      string base = parts[i];
      StringTrimLeft(base); StringTrimRight(base);
      if(base == "") continue;
      string s = Resolve(base, sfx);
      if(s == "") { Print("GrowthBot: ", base, " not available on this account - skipped"); continue; }
      int a = iMA(s, TrendTF, EmaTrendFast, 0, MODE_EMA, PRICE_CLOSE);
      int b = iMA(s, TrendTF, EmaTrendSlow, 0, MODE_EMA, PRICE_CLOSE);
      int c = iMA(s, EntryTF, EmaPullback, 0, MODE_EMA, PRICE_CLOSE);
      int d = iRSI(s, EntryTF, RsiPeriod, PRICE_CLOSE);
      int e = iATR(s, EntryTF, AtrPeriod);
      if(a == INVALID_HANDLE || b == INVALID_HANDLE || c == INVALID_HANDLE || d == INVALID_HANDLE || e == INVALID_HANDLE)
        { Print("GrowthBot: indicators failed for ", s); continue; }
      int k = ArraySize(sym);
      ArrayResize(sym, k + 1); ArrayResize(hTF, k + 1); ArrayResize(hTS, k + 1); ArrayResize(hE, k + 1);
      ArrayResize(hR, k + 1); ArrayResize(hA, k + 1); ArrayResize(lastBar, k + 1); ArrayResize(skipDay, k + 1);
      sym[k] = s; hTF[k] = a; hTS[k] = b; hE[k] = c; hR[k] = d; hA[k] = e; lastBar[k] = 0; skipDay[k] = -1;
     }
   if(ArraySize(sym) == 0) { Print("GrowthBot: no tradable symbols"); return(INIT_FAILED); }
   trade.SetExpertMagicNumber((ulong)Magic);
   trade.SetDeviationInPoints(50);
   EventSetTimer(1);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(g_have_lock) { GlobalVariableDel(g_pfx + "LOCK"); GlobalVariableDel(g_pfx + "HB"); }
   Comment("");
  }

string Resolve(string base, string sfx)
  {
   string c[];
   ArrayResize(c, 8);
   c[0] = sfx; c[1] = ""; c[2] = "m"; c[3] = "c"; c[4] = "z"; c[5] = ".f"; c[6] = "-ecn"; c[7] = ".a";
   for(int k = 0; k < ArraySize(c); k++)
     {
      string s = base + c[k];
      if(!SymbolSelect(s, true)) continue;
      if(SymbolInfoInteger(s, SYMBOL_TRADE_MODE) == SYMBOL_TRADE_MODE_FULL) return(s);
     }
   return("");
  }

void OnTick()  { Process(); }
void OnTimer() { Process(); }

//+------------------------------------------------------------------+
void Process()
  {
   if(!HoldLock()) return;
   DayRollover();
   if(!g_started)
     {
      g_started = true;
      string list = "";
      for(int i = 0; i < ArraySize(sym); i++) list += (i ? ", " : "") + sym[i];
      Notify("🤖 GrowthBot started on #" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) +
             "\nBalance $" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + " · goal $" + DoubleToString(GoalBalance, 0) +
             "\nMarkets: " + list +
             "\nRisk " + DoubleToString(RiskPercent, 1) + "% per trade, max " + IntegerToString(MaxTradesPerDay) +
             " trades/day, stops trading for the day after -" + DoubleToString(DailyLossLimitPct, 0) + "%." +
             (TradingEnabled ? "" : "\n⚠️ Trading is OFF (standing by)."));
     }
   ManagePositions();
   for(int i = 0; i < ArraySize(sym); i++)
     {
      datetime t = iTime(sym[i], EntryTF, 0);
      if(t == 0 || t == lastBar[i]) continue;
      bool first = (lastBar[i] == 0);
      lastBar[i] = t;
      if(!first) Evaluate(i);
     }
   if(!g_tester) UpdateComment();
  }

//+------------------------------------------------------------------+
//| Signal                                                           |
//+------------------------------------------------------------------+
void Evaluate(int i)
  {
   if(!TradingEnabled || !CanOpenNew() || !InSession()) return;
   string s = sym[i];
   if(HasPosition(s) || CountPositions() >= MaxOpenPositions) return;

   double tf[], ts[], e[], r[], a[];
   MqlRates rt[];
   ArraySetAsSeries(tf, true); ArraySetAsSeries(ts, true); ArraySetAsSeries(e, true);
   ArraySetAsSeries(r, true);  ArraySetAsSeries(a, true);  ArraySetAsSeries(rt, true);
   if(CopyBuffer(hTF[i], 0, 1, 1, tf) < 1 || CopyBuffer(hTS[i], 0, 1, 1, ts) < 1) return;
   if(CopyBuffer(hE[i], 0, 1, 3, e) < 3 || CopyBuffer(hR[i], 0, 1, 2, r) < 2 || CopyBuffer(hA[i], 0, 1, 1, a) < 1) return;
   if(CopyRates(s, EntryTF, 1, 3, rt) < 3) return;
   double hc = iClose(s, TrendTF, 1);
   if(hc <= 0 || a[0] <= 0) return;

   bool up = (tf[0] > ts[0] && hc > tf[0]);
   bool dn = (tf[0] < ts[0] && hc < tf[0]);
   if(!up && !dn) return;

   bool touchUp = false, touchDn = false;
   for(int k = 0; k < 3; k++)
     {
      if(rt[k].low  <= e[k]) touchUp = true;
      if(rt[k].high >= e[k]) touchDn = true;
     }
   bool buy  = up && touchUp && rt[0].close > e[0] && rt[0].close > rt[0].open && r[0] > 50 && r[0] < 70 && r[0] >= r[1];
   bool sell = dn && touchDn && rt[0].close < e[0] && rt[0].close < rt[0].open && r[0] < 50 && r[0] > 30 && r[0] <= r[1];
   if(buy || sell) Open(i, buy, a[0]);
  }

void Open(int i, bool buy, double atr)
  {
   string s = sym[i];
   MqlTick tk;
   if(!SymbolInfoTick(s, tk) || tk.ask <= 0 || tk.bid <= 0) return;
   double pt = SymbolInfoDouble(s, SYMBOL_POINT);
   int    dg = (int)SymbolInfoInteger(s, SYMBOL_DIGITS);
   double spread = tk.ask - tk.bid;
   double sld = SlAtrMult * atr;
   double minStop = (double)SymbolInfoInteger(s, SYMBOL_TRADE_STOPS_LEVEL) * pt + spread;
   if(sld < minStop) sld = minStop;
   if(spread > MaxSpreadFracOfSL * sld) return;

   double risk = 0; string why = "";
   double lots = LotsFor(s, sld, risk, why);
   if(lots <= 0) { NoteSkip(i, why); return; }

   double price = buy ? tk.ask : tk.bid;
   double sl = NormalizeDouble(buy ? price - sld : price + sld, dg);
   double tp = NormalizeDouble(buy ? price + TpR * sld : price - TpR * sld, dg);
   double margin = 0;
   if(!OrderCalcMargin(buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, s, lots, price, margin) ||
      margin > AccountInfoDouble(ACCOUNT_MARGIN_FREE) * 0.8)
     { NoteSkip(i, "not enough free margin"); return; }

   trade.SetTypeFillingBySymbol(s);
   bool ok = buy ? trade.Buy(lots, s, price, sl, tp, "GrowthBot") : trade.Sell(lots, s, price, sl, tp, "GrowthBot");
   uint rc = trade.ResultRetcode();
   if(ok && (rc == TRADE_RETCODE_DONE || rc == TRADE_RETCODE_PLACED))
     {
      g_dayTrades++;
      SaveDay();
     }
   else
      Notify("🔴 Could not open " + (buy ? "BUY " : "SELL ") + s + " " + DoubleToString(lots, 2) +
             ": " + IntegerToString(rc) + " " + trade.ResultRetcodeDescription());
  }

double LotsFor(string s, double sld, double &risk, string &why)
  {
   double tv = SymbolInfoDouble(s, SYMBOL_TRADE_TICK_VALUE_LOSS);
   if(tv <= 0) tv = SymbolInfoDouble(s, SYMBOL_TRADE_TICK_VALUE);
   double tsz = SymbolInfoDouble(s, SYMBOL_TRADE_TICK_SIZE);
   if(tv <= 0 || tsz <= 0) { why = "no tick value"; return(0); }
   double lossPerLot = sld / tsz * tv;
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   double vmin = SymbolInfoDouble(s, SYMBOL_VOLUME_MIN);
   double vstep = SymbolInfoDouble(s, SYMBOL_VOLUME_STEP);
   double vmax = SymbolInfoDouble(s, SYMBOL_VOLUME_MAX);
   if(vstep <= 0) vstep = 0.01;
   double lots = MathFloor((eq * RiskPercent / 100.0) / lossPerLot / vstep + 1e-9) * vstep;
   if(lots < vmin)
     {
      if(vmin * lossPerLot <= eq * MaxRiskPercent / 100.0) lots = vmin;
      else
        {
         why = StringFormat("smallest lot would risk $%.2f (%.0f%% of the account) - waiting until the account is bigger",
                            vmin * lossPerLot, vmin * lossPerLot / eq * 100.0);
         return(0);
        }
     }
   if(vmax > 0 && lots > vmax) lots = vmax;
   risk = lots * lossPerLot;
   return(NormalizeDouble(lots, 2));
  }

//+------------------------------------------------------------------+
//| Position management                                              |
//+------------------------------------------------------------------+
void ManagePositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0 || PositionGetInteger(POSITION_MAGIC) != Magic) continue;
      string s  = PositionGetString(POSITION_SYMBOL);
      double op = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl = PositionGetDouble(POSITION_SL);
      double tp = PositionGetDouble(POSITION_TP);
      bool   isBuy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
      if(sl <= 0) continue;
      double dist = isBuy ? op - sl : sl - op;
      if(dist <= 0) continue;                       // already at break-even or better
      double bid = SymbolInfoDouble(s, SYMBOL_BID), ask = SymbolInfoDouble(s, SYMBOL_ASK);
      double gain = isBuy ? bid - op : op - ask;
      if(gain < BreakevenR * dist) continue;
      int    dg = (int)SymbolInfoInteger(s, SYMBOL_DIGITS);
      double buf = (ask - bid) * 0.5;
      double nsl = NormalizeDouble(isBuy ? op + buf : op - buf, dg);
      double lvl = (double)SymbolInfoInteger(s, SYMBOL_TRADE_STOPS_LEVEL) * SymbolInfoDouble(s, SYMBOL_POINT);
      if(isBuy ? (bid - nsl < lvl) : (nsl - ask < lvl)) continue;
      trade.PositionModify(t, nsl, tp);
     }
  }

bool HasPosition(string s)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t != 0 && PositionGetInteger(POSITION_MAGIC) == Magic && PositionGetString(POSITION_SYMBOL) == s) return(true);
     }
   return(false);
  }

int CountPositions()
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t != 0 && PositionGetInteger(POSITION_MAGIC) == Magic) n++;
     }
   return(n);
  }

//+------------------------------------------------------------------+
//| Limits, sessions, daily bookkeeping                              |
//+------------------------------------------------------------------+
bool InSession()
  {
   MqlDateTime t;
   TimeToStruct(TimeGMT(), t);
   if(t.day_of_week == 0 || t.day_of_week == 6) return(false);
   return(t.hour >= SessionStartGMT && t.hour < SessionEndGMT);
  }

bool CanOpenNew()
  {
   if(g_dayTrades >= MaxTradesPerDay) return(false);
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   if(DailyLossLimitPct > 0 && g_dayEq > 0 && eq <= g_dayEq * (1.0 - DailyLossLimitPct / 100.0))
     {
      if(!g_dayLimitHit)
        {
         g_dayLimitHit = true;
         Notify("⛔ Daily loss limit reached (" + DoubleToString((eq / g_dayEq - 1.0) * 100.0, 1) +
                "% today). No new trades until tomorrow.");
        }
      return(false);
     }
   if(StopBelowEquity > 0 && eq < StopBelowEquity)
     {
      if(!g_floorHit)
        {
         g_floorHit = true;
         Notify("⛔ Equity $" + DoubleToString(eq, 2) + " is below the safety floor $" + DoubleToString(StopBelowEquity, 2) +
                ". GrowthBot has stopped opening trades.");
        }
      return(false);
     }
   return(true);
  }

void DayRollover()
  {
   MqlDateTime t;
   TimeToStruct(TimeGMT(), t);
   int d = t.year * 1000 + t.day_of_year;
   if(d == g_day) return;
   if(g_day == -1 && !g_tester && GlobalVariableCheck(g_pfx + "DAY") && (int)GlobalVariableGet(g_pfx + "DAY") == d)
     {
      // restarted during the day: keep today's numbers
      g_day = d;
      g_dayEq     = GlobalVariableGet(g_pfx + "DAYEQ");
      g_dayPL     = GlobalVariableGet(g_pfx + "DAYPL");
      g_dayTrades = (int)GlobalVariableGet(g_pfx + "DAYTR");
      g_dayWins   = (int)GlobalVariableGet(g_pfx + "DAYW");
      g_dayLosses = (int)GlobalVariableGet(g_pfx + "DAYL");
      return;
     }
   if(g_day != -1)
     {
      double bal = AccountInfoDouble(ACCOUNT_BALANCE);
      Notify("📊 Day summary: " + IntegerToString(g_dayWins + g_dayLosses) + " trades closed (" +
             IntegerToString(g_dayWins) + " won, " + IntegerToString(g_dayLosses) + " lost), P/L " + Money(g_dayPL) +
             "\nBalance $" + DoubleToString(bal, 2) + " · " + DoubleToString(bal / GoalBalance * 100.0, 1) +
             "% of the $" + DoubleToString(GoalBalance, 0) + " goal");
     }
   g_day = d;
   g_dayEq = AccountInfoDouble(ACCOUNT_EQUITY);
   g_dayPL = 0; g_dayTrades = 0; g_dayWins = 0; g_dayLosses = 0;
   g_dayLimitHit = false; g_floorHit = false;
   SaveDay();
  }

void SaveDay()
  {
   if(g_tester) return;
   GlobalVariableSet(g_pfx + "DAY", g_day);
   GlobalVariableSet(g_pfx + "DAYEQ", g_dayEq);
   GlobalVariableSet(g_pfx + "DAYPL", g_dayPL);
   GlobalVariableSet(g_pfx + "DAYTR", g_dayTrades);
   GlobalVariableSet(g_pfx + "DAYW", g_dayWins);
   GlobalVariableSet(g_pfx + "DAYL", g_dayLosses);
  }

void NoteSkip(int i, string why)
  {
   Print("GrowthBot: skipped ", sym[i], ": ", why);
   if(skipDay[i] == g_day) return;                 // tell Telegram at most once per symbol per day
   skipDay[i] = g_day;
   Notify("⏭ Signal on " + sym[i] + " skipped: " + why);
  }

string Money(double v) { return((v >= 0 ? "+$" : "-$") + DoubleToString(MathAbs(v), 2)); }

//+------------------------------------------------------------------+
//| Trade reports                                                    |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &tr, const MqlTradeRequest &rq, const MqlTradeResult &rs)
  {
   if(tr.type != TRADE_TRANSACTION_DEAL_ADD || tr.deal == 0) return;
   if(!HistoryDealSelect(tr.deal)) return;
   if(HistoryDealGetInteger(tr.deal, DEAL_MAGIC) != Magic) return;
   long   entry = HistoryDealGetInteger(tr.deal, DEAL_ENTRY);
   string s     = HistoryDealGetString(tr.deal, DEAL_SYMBOL);
   double vol   = HistoryDealGetDouble(tr.deal, DEAL_VOLUME);
   double price = HistoryDealGetDouble(tr.deal, DEAL_PRICE);
   long   dtype = HistoryDealGetInteger(tr.deal, DEAL_TYPE);
   int    dg    = (int)SymbolInfoInteger(s, SYMBOL_DIGITS);
   if(entry == DEAL_ENTRY_IN)
     {
      ulong pid = (ulong)HistoryDealGetInteger(tr.deal, DEAL_POSITION_ID);
      double sl = 0, tp = 0;
      if(PositionSelectByTicket(pid)) { sl = PositionGetDouble(POSITION_SL); tp = PositionGetDouble(POSITION_TP); }
      double risk = 0;
      double tv = SymbolInfoDouble(s, SYMBOL_TRADE_TICK_VALUE_LOSS), tsz = SymbolInfoDouble(s, SYMBOL_TRADE_TICK_SIZE);
      if(sl > 0 && tv > 0 && tsz > 0) risk = MathAbs(price - sl) / tsz * tv * vol;
      Notify("🟢 OPENED " + (dtype == DEAL_TYPE_BUY ? "BUY " : "SELL ") + s + " " + DoubleToString(vol, 2) + " lot @ " +
             DoubleToString(price, dg) + "\nStop " + DoubleToString(sl, dg) + " · Target " + DoubleToString(tp, dg) +
             "\nMax loss ≈ $" + DoubleToString(risk, 2) + " · Balance $" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
     }
   else if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
     {
      double pl = HistoryDealGetDouble(tr.deal, DEAL_PROFIT) + HistoryDealGetDouble(tr.deal, DEAL_SWAP) +
                  HistoryDealGetDouble(tr.deal, DEAL_COMMISSION);
      long reason = HistoryDealGetInteger(tr.deal, DEAL_REASON);
      string why = (reason == DEAL_REASON_TP ? "target hit" :
                    reason == DEAL_REASON_SL ? (pl >= 0 ? "break-even stop" : "stop-loss hit") :
                    reason == DEAL_REASON_SO ? "STOP-OUT by broker" : "closed");
      g_dayPL += pl;
      if(pl >= 0) g_dayWins++; else g_dayLosses++;
      SaveDay();
      double bal = AccountInfoDouble(ACCOUNT_BALANCE);
      Notify((pl >= 0 ? "✅ PROFIT " : "❌ LOSS ") + Money(pl) + " — " + (dtype == DEAL_TYPE_SELL ? "BUY " : "SELL ") + s + " " +
             DoubleToString(vol, 2) + " lot closed @ " + DoubleToString(price, dg) + " (" + why + ")" +
             "\nBalance $" + DoubleToString(bal, 2) + " · today " + Money(g_dayPL) +
             " · " + DoubleToString(bal / GoalBalance * 100.0, 1) + "% of $" + DoubleToString(GoalBalance, 0));
      if(!g_tester && bal >= GoalBalance && !GlobalVariableCheck(g_pfx + "GOAL"))
        {
         GlobalVariableSet(g_pfx + "GOAL", 1);
         Notify("🎯 Goal reached: balance $" + DoubleToString(bal, 2));
        }
     }
  }

void UpdateComment()
  {
   Comment("GrowthBot  |  ", (TradingEnabled ? "trading" : "STANDING BY"), "  |  open ", CountPositions(),
           "  |  today: ", g_dayTrades, " trades, ", Money(g_dayPL),
           "\nBalance $", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2),
           "  equity $", DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2),
           "  |  goal $", DoubleToString(GoalBalance, 0));
  }

//+------------------------------------------------------------------+
//| Single instance per account                                      |
//+------------------------------------------------------------------+
bool HoldLock()
  {
   if(g_tester) return(true);
   ulong now = NowMs();
   string ln = g_pfx + "LOCK", hn = g_pfx + "HB";
   if(g_have_lock)
     {
      if(now - g_last_hb >= 1000) { GlobalVariableSet(hn, (double)TimeLocal()); g_last_hb = now; }
      return(true);
     }
   if(g_lock_wait == 0) g_lock_wait = now;
   if(!GlobalVariableCheck(ln)) GlobalVariableTemp(ln);
   double holder = GlobalVariableGet(ln);
   bool stale = (now - g_lock_wait > 3000 && GlobalVariableCheck(hn) && (double)TimeLocal() - GlobalVariableGet(hn) > 5);
   if((holder == 0 || stale) && GlobalVariableSetOnCondition(ln, (double)ChartID(), holder))
     {
      g_have_lock = true;
      GlobalVariableSet(hn, (double)TimeLocal());
      g_last_hb = now;
      return(true);
     }
   if(now - g_lock_wait > 10000) { Print("GrowthBot: already running on another chart - removing this copy"); ExpertRemove(); }
   return(false);
  }

//+------------------------------------------------------------------+
//| Telegram via the VPS helper (one message per line)               |
//+------------------------------------------------------------------+
void Notify(string text)
  {
   Print("GrowthBot: ", text);
   if(g_tester) return;
   int h = INVALID_HANDLE;
   for(int tries = 0; tries < 5 && h == INVALID_HANDLE; tries++)
     {
      h = FileOpen("gold_copier_outbox.txt", FILE_READ | FILE_WRITE | FILE_BIN | FILE_COMMON | FILE_SHARE_READ);
      if(h == INVALID_HANDLE) Sleep(100);           // the helper may be swapping the file
     }
   if(h == INVALID_HANDLE) return;
   FileSeek(h, 0, SEEK_END);
   string t = text;
   StringReplace(t, "\\", "\\\\");
   StringReplace(t, "\r", "");
   StringReplace(t, "\n", "\\n");
   uchar b[];
   int n = StringToCharArray(t + "\n", b, 0, WHOLE_ARRAY, CP_UTF8);
   if(n > 1) FileWriteArray(h, b, 0, n - 1);
   FileClose(h);
  }
//+------------------------------------------------------------------+
