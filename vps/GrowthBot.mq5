//+------------------------------------------------------------------+
//| GrowthBot.mq5                                                    |
//| Rules-based multi-strategy robot (StrategyId selects one):       |
//|  1 trend pullback   2 channel breakout   3 range mean reversion  |
//|  4 EMA cross + ADX  5 London breakout of the Asian range         |
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
#property version   "2.00"

#include <Trade\Trade.mqh>

input string          InpSymbols          = "EURUSD,GBPUSD,USDJPY,XAUUSD"; // Base names; broker suffix is found automatically
input ENUM_TIMEFRAMES EntryTF             = PERIOD_M5;
input ENUM_TIMEFRAMES TrendTF             = PERIOD_H1;
input int             StrategyId          = 1;      // 1 pullback, 2 breakout, 3 mean reversion, 4 EMA cross, 5 London breakout
input int             EmaPullback         = 20;
input int             EmaTrendFast        = 50;
input int             EmaTrendSlow        = 200;
input int             RsiPeriod           = 14;
input int             AtrPeriod           = 14;
input int             BreakoutBars        = 20;     // 2: channel length
input int             BBPeriod            = 20;     // 3: Bollinger period
input double          BBDev               = 2.0;    // 3: Bollinger width
input int             AdxPeriod           = 14;
input double          AdxTrendMin         = 25;     // 4: minimum ADX for a cross
input double          AdxRangeMax         = 20;     // 3: maximum ADX for mean reversion
input int             CrossFast           = 20;     // 4
input int             CrossSlow           = 50;     // 4
input int             RangeStartGMT       = 0;      // 5: Asian range from (server time)
input int             RangeEndGMT         = 7;      // 5: Asian range until / breakouts from
input int             BreakoutEndGMT      = 11;     // 5: no new breakouts after
input double          MaxRangeAtr         = 10.0;   // 5: skip if the Asian range is wider than this x ATR
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
int      hTF[], hTS[], hE[], hR[], hA[], hB[], hX[], hCF[], hCS[];
int      tradedDay[];
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
      int f = iBands(s, EntryTF, BBPeriod, 0, BBDev, PRICE_CLOSE);
      int g = iADX(s, EntryTF, AdxPeriod);
      int h1 = iMA(s, EntryTF, CrossFast, 0, MODE_EMA, PRICE_CLOSE);
      int h2 = iMA(s, EntryTF, CrossSlow, 0, MODE_EMA, PRICE_CLOSE);
      if(a == INVALID_HANDLE || b == INVALID_HANDLE || c == INVALID_HANDLE || d == INVALID_HANDLE || e == INVALID_HANDLE ||
         f == INVALID_HANDLE || g == INVALID_HANDLE || h1 == INVALID_HANDLE || h2 == INVALID_HANDLE)
        { Print("GrowthBot: indicators failed for ", s); continue; }
      int k = ArraySize(sym);
      ArrayResize(sym, k + 1); ArrayResize(hTF, k + 1); ArrayResize(hTS, k + 1); ArrayResize(hE, k + 1);
      ArrayResize(hR, k + 1); ArrayResize(hA, k + 1); ArrayResize(lastBar, k + 1); ArrayResize(skipDay, k + 1);
      ArrayResize(hB, k + 1); ArrayResize(hX, k + 1); ArrayResize(hCF, k + 1); ArrayResize(hCS, k + 1); ArrayResize(tradedDay, k + 1);
      sym[k] = s; hTF[k] = a; hTS[k] = b; hE[k] = c; hR[k] = d; hA[k] = e; lastBar[k] = 0; skipDay[k] = -1;
      hB[k] = f; hX[k] = g; hCF[k] = h1; hCS[k] = h2; tradedDay[k] = -1;
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
      Notify("🤖 GrowthBot started (" + StrategyName() + ") on #" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) +
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
string StrategyName()
  {
   switch(StrategyId)
     {
      case 2: return("channel breakout");
      case 3: return("range mean reversion");
      case 4: return("EMA cross + ADX");
      case 5: return("London breakout");
     }
   return("trend pullback");
  }

void Evaluate(int i)
  {
   if(!TradingEnabled || !CanOpenNew()) return;
   string s = sym[i];
   if(HasPosition(s) || CountPositions() >= MaxOpenPositions) return;
   double a[];
   ArraySetAsSeries(a, true);
   if(CopyBuffer(hA[i], 0, 1, 1, a) < 1 || a[0] <= 0) return;
   if(StrategyId == 5) { EvalRange(i, a[0]); return; }
   if(!InSession()) return;
   int sig = 0;
   if(StrategyId == 2)      sig = SigBreakout(i);
   else if(StrategyId == 3) sig = SigMeanRev(i);
   else if(StrategyId == 4) sig = SigCross(i);
   else                     sig = SigPullback(i);
   if(sig != 0) Open(i, sig > 0, SlAtrMult * a[0]);
  }

int TrendDir(int i)
  {
   double tf[], ts[];
   ArraySetAsSeries(tf, true); ArraySetAsSeries(ts, true);
   if(CopyBuffer(hTF[i], 0, 1, 1, tf) < 1 || CopyBuffer(hTS[i], 0, 1, 1, ts) < 1) return(0);
   double hc = iClose(sym[i], TrendTF, 1);
   if(hc <= 0) return(0);
   if(tf[0] > ts[0] && hc > tf[0]) return(1);
   if(tf[0] < ts[0] && hc < tf[0]) return(-1);
   return(0);
  }

// 1: pullback to EMA20 in the higher-timeframe trend, RSI confirming
int SigPullback(int i)
  {
   int dir = TrendDir(i);
   if(dir == 0) return(0);
   double e[], r[];
   MqlRates rt[];
   ArraySetAsSeries(e, true); ArraySetAsSeries(r, true); ArraySetAsSeries(rt, true);
   if(CopyBuffer(hE[i], 0, 1, 3, e) < 3 || CopyBuffer(hR[i], 0, 1, 2, r) < 2) return(0);
   if(CopyRates(sym[i], EntryTF, 1, 3, rt) < 3) return(0);
   bool touchUp = false, touchDn = false;
   for(int k = 0; k < 3; k++)
     {
      if(rt[k].low  <= e[k]) touchUp = true;
      if(rt[k].high >= e[k]) touchDn = true;
     }
   if(dir > 0 && touchUp && rt[0].close > e[0] && rt[0].close > rt[0].open && r[0] > 50 && r[0] < 70 && r[0] >= r[1]) return(1);
   if(dir < 0 && touchDn && rt[0].close < e[0] && rt[0].close < rt[0].open && r[0] < 50 && r[0] > 30 && r[0] <= r[1]) return(-1);
   return(0);
  }

// 2: close beyond the last BreakoutBars-bar high/low, in the higher-timeframe trend
int SigBreakout(int i)
  {
   int dir = TrendDir(i);
   if(dir == 0) return(0);
   MqlRates rt[];
   ArraySetAsSeries(rt, true);
   if(CopyRates(sym[i], EntryTF, 1, BreakoutBars + 1, rt) < BreakoutBars + 1) return(0);
   double hh = -DBL_MAX, ll = DBL_MAX;
   for(int k = 1; k <= BreakoutBars; k++) { hh = MathMax(hh, rt[k].high); ll = MathMin(ll, rt[k].low); }
   if(dir > 0 && rt[0].close > hh) return(1);
   if(dir < 0 && rt[0].close < ll) return(-1);
   return(0);
  }

// 3: in a quiet market (low ADX), fade a close outside the Bollinger band with RSI stretched
int SigMeanRev(int i)
  {
   double up[], lo[], r[], adx[];
   ArraySetAsSeries(up, true); ArraySetAsSeries(lo, true); ArraySetAsSeries(r, true); ArraySetAsSeries(adx, true);
   if(CopyBuffer(hB[i], 1, 1, 1, up) < 1 || CopyBuffer(hB[i], 2, 1, 1, lo) < 1) return(0);
   if(CopyBuffer(hR[i], 0, 1, 1, r) < 1 || CopyBuffer(hX[i], 0, 1, 1, adx) < 1) return(0);
   if(adx[0] > AdxRangeMax) return(0);
   double c1 = iClose(sym[i], EntryTF, 1);
   if(c1 <= 0) return(0);
   if(c1 < lo[0] && r[0] < 30) return(1);
   if(c1 > up[0] && r[0] > 70) return(-1);
   return(0);
  }

// 4: fast/slow EMA cross with a strong ADX trend, not against the higher timeframe
int SigCross(int i)
  {
   double f[], sl[], adx[], pdi[], mdi[];
   ArraySetAsSeries(f, true); ArraySetAsSeries(sl, true); ArraySetAsSeries(adx, true);
   ArraySetAsSeries(pdi, true); ArraySetAsSeries(mdi, true);
   if(CopyBuffer(hCF[i], 0, 1, 2, f) < 2 || CopyBuffer(hCS[i], 0, 1, 2, sl) < 2) return(0);
   if(CopyBuffer(hX[i], 0, 1, 1, adx) < 1 || CopyBuffer(hX[i], 1, 1, 1, pdi) < 1 || CopyBuffer(hX[i], 2, 1, 1, mdi) < 1) return(0);
   if(adx[0] < AdxTrendMin) return(0);
   int dir = TrendDir(i);
   if(f[0] > sl[0] && f[1] <= sl[1] && pdi[0] > mdi[0] && dir >= 0) return(1);
   if(f[0] < sl[0] && f[1] >= sl[1] && mdi[0] > pdi[0] && dir <= 0) return(-1);
   return(0);
  }

// 5: once a day, trade the first close outside the Asian-session range; stop at the other side
void EvalRange(int i, double atr)
  {
   string s = sym[i];
   datetime now = TimeCurrent();
   MqlDateTime t;
   TimeToStruct(now, t);
   if(t.day_of_week == 0 || t.day_of_week == 6) return;
   if(t.hour < RangeEndGMT || t.hour >= BreakoutEndGMT) return;
   if(tradedDay[i] == g_day) return;
   datetime day0 = now - (t.hour * 3600 + t.min * 60 + t.sec);
   MqlRates rr[];
   int n = CopyRates(s, EntryTF, day0 + RangeStartGMT * 3600, day0 + RangeEndGMT * 3600 - 1, rr);
   if(n < 4) return;
   double hi = -DBL_MAX, lo = DBL_MAX;
   for(int k = 0; k < n; k++) { hi = MathMax(hi, rr[k].high); lo = MathMin(lo, rr[k].low); }
   double range = hi - lo;
   if(range <= 0 || range > MaxRangeAtr * atr) return;
   double c1 = iClose(s, EntryTF, 1);
   MqlTick tk;
   if(c1 <= 0 || !SymbolInfoTick(s, tk)) return;
   if(c1 > hi && tk.ask > lo)      { if(Open(i, true,  tk.ask - lo)) tradedDay[i] = g_day; }
   else if(c1 < lo && tk.bid < hi) { if(Open(i, false, hi - tk.bid)) tradedDay[i] = g_day; }
  }

bool Open(int i, bool buy, double sld)
  {
   string s = sym[i];
   MqlTick tk;
   if(!SymbolInfoTick(s, tk) || tk.ask <= 0 || tk.bid <= 0) return(false);
   double pt = SymbolInfoDouble(s, SYMBOL_POINT);
   int    dg = (int)SymbolInfoInteger(s, SYMBOL_DIGITS);
   double spread = tk.ask - tk.bid;
   double minStop = (double)SymbolInfoInteger(s, SYMBOL_TRADE_STOPS_LEVEL) * pt + spread;
   if(sld < minStop) sld = minStop;
   if(spread > MaxSpreadFracOfSL * sld) return(false);

   double risk = 0; string why = "";
   double lots = LotsFor(s, sld, risk, why);
   if(lots <= 0) { NoteSkip(i, why); return(false); }

   double price = buy ? tk.ask : tk.bid;
   double sl = NormalizeDouble(buy ? price - sld : price + sld, dg);
   double tp = NormalizeDouble(buy ? price + TpR * sld : price - TpR * sld, dg);
   double margin = 0;
   if(!OrderCalcMargin(buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, s, lots, price, margin) ||
      margin > AccountInfoDouble(ACCOUNT_MARGIN_FREE) * 0.8)
     { NoteSkip(i, "not enough free margin"); return(false); }

   trade.SetTypeFillingBySymbol(s);
   bool ok = buy ? trade.Buy(lots, s, price, sl, tp, "GrowthBot") : trade.Sell(lots, s, price, sl, tp, "GrowthBot");
   uint rc = trade.ResultRetcode();
   if(ok && (rc == TRADE_RETCODE_DONE || rc == TRADE_RETCODE_PLACED))
     {
      g_dayTrades++;
      SaveDay();
      return(true);
     }
   Notify("🔴 Could not open " + (buy ? "BUY " : "SELL ") + s + " " + DoubleToString(lots, 2) +
          ": " + IntegerToString(rc) + " " + trade.ResultRetcodeDescription());
   return(false);
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
