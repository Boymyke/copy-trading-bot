//+------------------------------------------------------------------+
//| Breakout6H.mq5                                                    |
//| 6-hour breakout robot for gold. Runs inside MetaTrader 5 itself: |
//| no server, no MetaApi, no subscription.                           |
//|                                                                   |
//| Rules (back-tested on Exness XAUUSDm, Jun - mid Nov 2026):        |
//|  - signal: a 5-minute bar closes above the highest high (buy) or  |
//|    below the lowest low (sell) of the 72 bars before it (6 hours) |
//|  - only signal bars starting 12:00-16:55 UTC (1-6 pm Lagos),      |
//|    Monday-Friday, at most 2 trades a day, one trade at a time     |
//|  - stop $6 from the entry, held by the broker                     |
//|  - once $2 ahead, the stop follows $2 behind the best price       |
//|  - closed at market after 240 minutes                             |
//+------------------------------------------------------------------+
#property copyright   "Boymyke / copy-trading-bot"
#property version     "1.00"
#property description "6-hour breakout on gold: 5-min close beyond the previous 72 bars, 12-17 UTC, $6 stop, $2 trail after +$2, 4 h limit."

#include <Trade\Trade.mqh>

input group "Strategy"
input int    LookbackBars      = 72;    // 5-minute bars in the range (72 = 6 hours)
input int    SessionStartUTC   = 12;    // first signal hour, UTC (12 = 1 pm Lagos)
input int    SessionEndUTC     = 17;    // no new signals from this hour, UTC (17 = 6 pm Lagos)
input int    MaxTradesPerDay   = 2;     // trades per day (UTC)
input double StopDollars       = 6.0;   // stop distance in price ($ per ounce)
input double TrailStartDollars = 2.0;   // start trailing once this far ahead
input double TrailDollars      = 2.0;   // trailing distance behind the best price
input double TrailStepDollars  = 0.05;  // move the broker stop only when it improves by at least this
input int    MaxHoldMinutes    = 240;   // close after this many minutes
input double MaxSpreadDollars  = 0.80;  // skip a signal when the spread is wider than this

input group "Trade size"
input double RiskPercent       = 1.0;   // % of balance lost if the stop is hit (0 = use FixedLot)
input double FixedLot          = 0.01;  // used only when RiskPercent = 0
input double MaxRiskPercent    = 10.0;  // skip a trade whose smallest possible lot risks more than this

input group "Other"
input int    ServerUTCOffset   = 0;     // broker server time minus UTC, in hours (Exness = 0). Used in the Strategy Tester
input bool   PushNotifications = true;  // send every trade to the MT5 phone app (Tools > Options > Notifications)
input bool   WriteJournal      = true;  // write every closed trade to MQL5\Files\Breakout6H_journal.csv
input long   MagicNumber       = 606072;

CTrade   trade;
datetime g_lastBar     = 0;
int      g_day         = -1;
int      g_tradesToday = 0;
ulong    g_ticket      = 0;
double   g_best        = 0.0;
string   g_closeReason = "";
int      g_utcOffset   = 0;     // seconds: server time - UTC
bool     g_tester      = false;

//+------------------------------------------------------------------+
int OnInit()
  {
   g_tester = (bool)MQLInfoInteger(MQL_TESTER);
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(100);
   trade.SetTypeFillingBySymbol(_Symbol);
   if(StringFind(_Symbol, "XAU") < 0)
      Print("Breakout6H: this robot was tested on gold (XAUUSD). Current chart: ", _Symbol);
   UpdateUtcOffset();
   g_lastBar = iTime(_Symbol, PERIOD_M5, 0);   // first check is the next bar, never an old one
   g_day = UtcDay(TimeCurrent());
   g_tradesToday = CountEntriesOnUtcDay(g_day);
   PickUpPosition();
   EventSetTimer(30);
   Say(StringFormat("Breakout6H started on %s, account %I64d. Risk %.1f%% per trade. Trades today: %d of %d.",
                    _Symbol, AccountInfoInteger(ACCOUNT_LOGIN), RiskPercent, g_tradesToday, MaxTradesPerDay));
   ShowStatus();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   Comment("");
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   if(g_ticket != 0)
      Manage();
   datetime bar = iTime(_Symbol, PERIOD_M5, 0);
   if(bar == 0 || bar == g_lastBar)
      return;
   g_lastBar = bar;
   if(g_ticket == 0)
      CheckSignal();
   ShowStatus();
  }

void OnTimer()
  {
   if(!g_tester && UtcDay(TimeCurrent()) != g_day)
      UpdateUtcOffset();
   if(g_ticket != 0)
      Manage();
   ShowStatus();
  }

//+------------------------------------------------------------------+
//| Time helpers                                                      |
//+------------------------------------------------------------------+
void UpdateUtcOffset()
  {
   if(g_tester)
     {
      g_utcOffset = ServerUTCOffset * 3600;
      return;
     }
   long diff = (long)(TimeTradeServer() - TimeGMT());
   g_utcOffset = (int)(MathRound(diff / 1800.0) * 1800);
   if(g_utcOffset != ServerUTCOffset * 3600)
      Print("Breakout6H: broker server time is UTC", (g_utcOffset >= 0 ? "+" : ""), g_utcOffset / 3600.0,
            " (detected). Set ServerUTCOffset to this value for the Strategy Tester.");
  }

datetime ToUtc(const datetime server) { return (datetime)(server - g_utcOffset); }
int      UtcDay(const datetime server) { return (int)(ToUtc(server) / 86400); }

int CountEntriesOnUtcDay(const int day)
  {
   datetime from = (datetime)((long)day * 86400 + g_utcOffset);
   if(!HistorySelect(from, from + 86400))
      return 0;
   int n = 0;
   for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
     {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0) continue;
      if(HistoryDealGetInteger(deal, DEAL_MAGIC) != MagicNumber) continue;
      if(HistoryDealGetString(deal, DEAL_SYMBOL) != _Symbol) continue;
      if(HistoryDealGetInteger(deal, DEAL_ENTRY) == DEAL_ENTRY_IN) n++;
     }
   return n;
  }

//+------------------------------------------------------------------+
//| Signal                                                            |
//+------------------------------------------------------------------+
void CheckSignal()
  {
   if(Bars(_Symbol, PERIOD_M5) < LookbackBars + 3)
      return;
   datetime sigBar = iTime(_Symbol, PERIOD_M5, 1);
   datetime nowBar = iTime(_Symbol, PERIOD_M5, 0);
   if(sigBar == 0 || nowBar - sigBar > 900)       // the signal bar must be the one that has just closed
      return;
   MqlDateTime t;
   TimeToStruct(ToUtc(sigBar), t);
   if(t.day_of_week == 0 || t.day_of_week == 6)
      return;
   if(t.hour < SessionStartUTC || t.hour >= SessionEndUTC)
      return;
   int day = UtcDay(sigBar);
   if(day != g_day)
     {
      g_day = day;
      g_tradesToday = CountEntriesOnUtcDay(day);
     }
   if(g_tradesToday >= MaxTradesPerDay)
      return;
   int hiIdx = iHighest(_Symbol, PERIOD_M5, MODE_HIGH, LookbackBars, 2);
   int loIdx = iLowest(_Symbol, PERIOD_M5, MODE_LOW, LookbackBars, 2);
   if(hiIdx < 0 || loIdx < 0)
      return;
   double hi = iHigh(_Symbol, PERIOD_M5, hiIdx);
   double lo = iLow(_Symbol, PERIOD_M5, loIdx);
   double cl = iClose(_Symbol, PERIOD_M5, 1);
   if(hi <= 0 || lo <= 0 || cl <= 0)
      return;
   int side = (cl > hi ? 1 : (cl < lo ? -1 : 0));
   if(side != 0)
      Open(side);
  }

//+------------------------------------------------------------------+
//| Opening                                                           |
//+------------------------------------------------------------------+
double Norm(const double price) { return NormalizeDouble(price, _Digits); }

bool LotFor(double &lot, double &riskPct, double &riskMoney)
  {
   double bal   = AccountInfoDouble(ACCOUNT_BALANCE);
   double tv    = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE_LOSS);
   if(tv <= 0) tv = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double ts    = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double vmin  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vmax  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double vstep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(bal <= 0 || tv <= 0 || ts <= 0 || vstep <= 0)
      return false;
   double lossPerLot = StopDollars / ts * tv;            // account currency lost per 1.00 lot at the stop
   if(RiskPercent > 0)
      lot = MathFloor(bal * RiskPercent / 100.0 / lossPerLot / vstep) * vstep;
   else
      lot = FixedLot;
   lot = MathMax(lot, vmin);
   lot = MathMin(lot, vmax);
   int vdigits = (int)MathMax(0, MathCeil(-MathLog10(vstep) - 1e-9));
   lot = NormalizeDouble(MathRound(lot / vstep) * vstep, vdigits);
   riskMoney = lot * lossPerLot;
   riskPct = riskMoney / bal * 100.0;
   return (riskPct <= MaxRiskPercent);
  }

void Open(const int side)
  {
   string name = (side > 0 ? "BUY" : "SELL");
   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk))
      return;
   double spread = tk.ask - tk.bid;
   if(spread > MaxSpreadDollars)
     {
      Say(StringFormat("%s signal skipped: spread %.2f is too wide.", name, spread));
      return;
     }
   double lot = 0, riskPct = 0, riskMoney = 0;
   if(!LotFor(lot, riskPct, riskMoney))
     {
      Say(StringFormat("%s signal skipped: the smallest lot (%.2f) would risk %.0f%% of the balance (limit %.0f%%). Use a cent account or a bigger balance.",
                       name, lot, riskPct, MaxRiskPercent));
      return;
     }
   double price = (side > 0 ? tk.ask : tk.bid);
   double sl = Norm(price - side * StopDollars);
   bool sent = (side > 0 ? trade.Buy(lot, _Symbol, 0.0, sl, 0.0, "B6H")
                         : trade.Sell(lot, _Symbol, 0.0, sl, 0.0, "B6H"));
   uint rc = trade.ResultRetcode();
   if(!sent || (rc != TRADE_RETCODE_DONE && rc != TRADE_RETCODE_PLACED && rc != TRADE_RETCODE_DONE_PARTIAL))
     {
      Say(StringFormat("%s order rejected: %s", name, trade.ResultRetcodeDescription()));
      return;
     }
   g_tradesToday++;
   g_best = 0.0;
   g_closeReason = "";
   PickUpPosition();
   double entry = price;
   if(g_ticket != 0 && PositionSelectByTicket(g_ticket))
     {
      entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double want = Norm(entry - side * StopDollars);         // stop exactly $6 from the real fill
      if(MathAbs(want - PositionGetDouble(POSITION_SL)) > 2 * _Point)
         trade.PositionModify(g_ticket, want, 0.0);
      sl = want;
     }
   Say(StringFormat("%s %.2f %s at %.2f. Stop %.2f (-%.2f %s, %.1f%%). Trade %d of %d today.",
                    name, lot, _Symbol, entry, sl, riskMoney, AccountInfoString(ACCOUNT_CURRENCY), riskPct,
                    g_tradesToday, MaxTradesPerDay));
  }

void PickUpPosition()
  {
   g_ticket = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      g_ticket = ticket;
      int side = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? 1 : -1);
      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl   = PositionGetDouble(POSITION_SL);
      double locked = (sl > 0 ? (sl - open) * side : -StopDollars);
      // a trailed stop is never behind the entry, so it tells us the best price reached so far
      double fromStop = (locked >= -0.01 ? locked + TrailDollars : 0.0);
      if(fromStop > g_best) g_best = fromStop;
      return;
     }
  }

//+------------------------------------------------------------------+
//| Managing                                                          |
//+------------------------------------------------------------------+
void Manage()
  {
   if(!PositionSelectByTicket(g_ticket))
     {
      Closed(g_ticket);
      g_ticket = 0;
      g_best = 0.0;
      return;
     }
   int side = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? 1 : -1);
   double open = PositionGetDouble(POSITION_PRICE_OPEN);
   double sl   = PositionGetDouble(POSITION_SL);
   datetime opened = (datetime)PositionGetInteger(POSITION_TIME);
   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk))
      return;
   double price = (side > 0 ? tk.bid : tk.ask);          // the price the trade would close at
   if(TimeCurrent() - opened > MaxHoldMinutes * 60)
     {
      g_closeReason = "time limit";
      trade.PositionClose(g_ticket);
      return;
     }
   double move = (price - open) * side;
   if(move > g_best)
      g_best = move;
   if(g_best < TrailStartDollars)
      return;
   double want = Norm(open + side * (g_best - TrailDollars));
   bool better = (sl <= 0 || (want - sl) * side >= TrailStepDollars);
   if(!better)
      return;
   double minDist = (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   if((price - want) * side > minDist)
     {
      trade.PositionModify(g_ticket, want, PositionGetDouble(POSITION_TP));
     }
   else if((price - want) * side <= 0)
     {
      g_closeReason = "trailing stop";                    // price jumped through the new stop: take it now
      trade.PositionClose(g_ticket);
     }
  }

void Closed(const ulong positionId)
  {
   if(!HistorySelectByPosition(positionId))
      return;
   double profit = 0, exitPrice = 0, entryPrice = 0, lot = 0;
   long reason = -1, type = -1;
   datetime tin = 0, tout = 0;
   for(int i = 0; i < HistoryDealsTotal(); i++)
     {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0) continue;
      profit += HistoryDealGetDouble(deal, DEAL_PROFIT) + HistoryDealGetDouble(deal, DEAL_COMMISSION) + HistoryDealGetDouble(deal, DEAL_SWAP);
      long entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_IN)
        {
         entryPrice = HistoryDealGetDouble(deal, DEAL_PRICE);
         lot  = HistoryDealGetDouble(deal, DEAL_VOLUME);
         type = HistoryDealGetInteger(deal, DEAL_TYPE);
         tin  = (datetime)HistoryDealGetInteger(deal, DEAL_TIME);
        }
      else if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
        {
         exitPrice = HistoryDealGetDouble(deal, DEAL_PRICE);
         reason = HistoryDealGetInteger(deal, DEAL_REASON);
         tout = (datetime)HistoryDealGetInteger(deal, DEAL_TIME);
        }
     }
   string why = g_closeReason;
   if(reason == DEAL_REASON_SL)
      why = (g_best >= TrailStartDollars ? "trailing stop" : "stop");
   else if(reason == DEAL_REASON_SO)
      why = "stop-out";
   else if(why == "")
      why = "closed by hand";
   g_closeReason = "";
   string name = (type == DEAL_TYPE_BUY ? "BUY" : "SELL");
   int minutes = (int)((tout - tin) / 60);
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   Say(StringFormat("%s closed at %.2f (%s, %d min). Result %s%.2f. Balance %.2f %s.",
                    name, exitPrice, why, minutes, (profit >= 0 ? "+" : "-"), MathAbs(profit), bal, AccountInfoString(ACCOUNT_CURRENCY)));
   if(WriteJournal)
      Journal(tin, tout, name, lot, entryPrice, exitPrice, profit, why, bal);
  }

//+------------------------------------------------------------------+
//| Reporting                                                         |
//+------------------------------------------------------------------+
void Say(const string text)
  {
   Print("Breakout6H: ", text);
   if(PushNotifications && !g_tester)
     {
      string msg = text;
      if(StringLen(msg) > 250) msg = StringSubstr(msg, 0, 250);
      if(!SendNotification(msg))
         Print("Breakout6H: phone notification not sent (set your MetaQuotes ID in Tools > Options > Notifications)");
     }
  }

void Journal(datetime tin, datetime tout, string side, double lot, double entry, double exitPrice, double profit, string why, double bal)
  {
   string file = "Breakout6H_journal.csv";
   int h = FileOpen(file, FILE_READ | FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(h == INVALID_HANDLE)
      return;
   if(FileSize(h) == 0)
      FileWrite(h, "opened_utc", "closed_utc", "side", "lot", "entry", "exit", "profit", "reason", "balance", "account");
   FileSeek(h, 0, SEEK_END);
   FileWrite(h, TimeToString(ToUtc(tin), TIME_DATE | TIME_MINUTES), TimeToString(ToUtc(tout), TIME_DATE | TIME_MINUTES), side,
             DoubleToString(lot, 2), DoubleToString(entry, _Digits), DoubleToString(exitPrice, _Digits),
             DoubleToString(profit, 2), why, DoubleToString(bal, 2), IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)));
   FileClose(h);
  }

void ShowStatus()
  {
   if(g_tester)
      return;
   MqlDateTime t;
   TimeToStruct(ToUtc(TimeCurrent()), t);
   bool inSession = (t.day_of_week >= 1 && t.day_of_week <= 5 && t.hour >= SessionStartUTC && t.hour < SessionEndUTC);
   string pos = "no open trade";
   if(g_ticket != 0 && PositionSelectByTicket(g_ticket))
      pos = StringFormat("%s %.2f at %.2f, stop %.2f, profit %.2f",
                         (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL"),
                         PositionGetDouble(POSITION_VOLUME), PositionGetDouble(POSITION_PRICE_OPEN),
                         PositionGetDouble(POSITION_SL), PositionGetDouble(POSITION_PROFIT));
   string algo = (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && MQLInfoInteger(MQL_TRADE_ALLOWED)) ? "ON" : "OFF - press Algo Trading";
   Comment(StringFormat("Breakout6H v1.00   Algo trading: %s\nSession (1-6 pm Lagos): %s   Trades today: %d of %d\n%s\nBalance %.2f %s",
                        algo, (inSession ? "open" : "closed"), g_tradesToday, MaxTradesPerDay, pos,
                        AccountInfoDouble(ACCOUNT_BALANCE), AccountInfoString(ACCOUNT_CURRENCY)));
  }
//+------------------------------------------------------------------+
