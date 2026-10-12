//+------------------------------------------------------------------+
//| GoldCopierMaster.mq5                                             |
//| Runs on the SOURCE terminal (TMFinancials, investor login OK).   |
//| Publishes the open XAUUSD.f positions to a shared file every     |
//| 100 ms and on every trade event. It never sends any orders.      |
//+------------------------------------------------------------------+
#property copyright "Gold Copier"
#property version   "1.00"

input string SourceSymbol = "XAUUSD.f";        // Symbol to publish
input string FeedName     = "gold_copier_feed"; // Shared file name (must match the receiver)
input int    PublishMs    = 100;                // Publish interval in milliseconds

string g_tmp, g_file;
int    g_last_count = -1;
long   g_last_conn  = -1;

int OnInit()
  {
   g_file = FeedName + ".txt";
   g_tmp  = FeedName + ".tmp";
   if(!EventSetMillisecondTimer(MathMax(PublishMs, 20)))
     {
      Print("GoldCopierMaster: cannot start timer");
      return(INIT_FAILED);
     }
   Publish();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   Comment("");
  }

void OnTimer() { Publish(); }

void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
  {
   Publish();
  }

void Publish()
  {
   int h = FileOpen(g_tmp, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE)
     {
      Comment("GoldCopierMaster: cannot write feed file (error ", GetLastError(), ")");
      return;
     }
   datetime server_now = TimeTradeServer();
   int count = 0;
   string lines = "";
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != SourceSymbol)
         continue;
      long type = PositionGetInteger(POSITION_TYPE);           // 0 = buy, 1 = sell
      long age  = (long)(server_now - (datetime)PositionGetInteger(POSITION_TIME));
      if(age < 0)
         age = 0;
      lines += "P|" + IntegerToString((long)ticket) + "|" + IntegerToString(type) + "|" + IntegerToString(age) + "|" +
               DoubleToString(PositionGetDouble(POSITION_VOLUME), 6) + "|" +
               DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), (int)SymbolInfoInteger(SourceSymbol, SYMBOL_DIGITS)) + "\r\n";
      count++;
     }
   long connected = TerminalInfoInteger(TERMINAL_CONNECTED);
   FileWriteString(h, "HB|" + IntegerToString((long)TimeGMT()) + "|" + IntegerToString(connected) + "|" +
                   IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "|" + IntegerToString(count) + "\r\n");
   FileWriteString(h, lines);
   FileWriteString(h, "END\r\n");
   FileClose(h);
   if(!FileMove(g_tmp, FILE_COMMON, g_file, FILE_COMMON | FILE_REWRITE))
      Print("GoldCopierMaster: cannot publish feed (error ", GetLastError(), ")");

   if(count != g_last_count || connected != g_last_conn)
     {
      g_last_count = count;
      g_last_conn  = connected;
      Comment("GoldCopierMaster  |  publishing ", count, " open ", SourceSymbol, " position(s)\n",
              "Broker connected: ", (connected ? "yes" : "NO"), "  |  feed: ", g_file);
     }
  }
//+------------------------------------------------------------------+
