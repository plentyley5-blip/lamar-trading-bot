// LM AUTO TRADER autonomous scalping engine.
// Strategy ported from the previously agreed LM SCALPER v2.00 rules.
// This file is included by LM_AUTO_TRADER.mq5; do not compile it separately.

#define SCALP_SYMBOL_COUNT 8

string g_scalp_base_symbols[SCALP_SYMBOL_COUNT] = {
   "XAUUSD", "EURUSD", "GBPUSD", "USDJPY",
   "USDCHF", "USDCAD", "AUDUSD", "NZDUSD"
};
string g_scalp_symbols[SCALP_SYMBOL_COUNT];
int g_m1_fast[SCALP_SYMBOL_COUNT];
int g_m1_slow[SCALP_SYMBOL_COUNT];
int g_m5_fast[SCALP_SYMBOL_COUNT];
int g_m5_slow[SCALP_SYMBOL_COUNT];
int g_m1_rsi[SCALP_SYMBOL_COUNT];
int g_m1_atr[SCALP_SYMBOL_COUNT];
datetime g_last_scalp_signal_bar[SCALP_SYMBOL_COUNT];
bool g_calendar_unavailable_warned = false;

bool InitScalperIndicators()
{
   int available = 0;
   for(int i = 0; i < SCALP_SYMBOL_COUNT; i++)
   {
      g_scalp_symbols[i] = "";
      g_m1_fast[i] = INVALID_HANDLE;
      g_m1_slow[i] = INVALID_HANDLE;
      g_m5_fast[i] = INVALID_HANDLE;
      g_m5_slow[i] = INVALID_HANDLE;
      g_m1_rsi[i] = INVALID_HANDLE;
      g_m1_atr[i] = INVALID_HANDLE;
      g_last_scalp_signal_bar[i] = 0;

      string symbol = ResolveBrokerSymbol(g_scalp_base_symbols[i]);
      if(symbol == "")
      {
         Print("LM AUTO TRADER: Scalper symbol unavailable or ambiguous: ", g_scalp_base_symbols[i]);
         continue;
      }
      int h1f = iMA(symbol, PERIOD_M1, 9, 0, MODE_EMA, PRICE_CLOSE);
      int h1s = iMA(symbol, PERIOD_M1, 21, 0, MODE_EMA, PRICE_CLOSE);
      int h5f = iMA(symbol, PERIOD_M5, 9, 0, MODE_EMA, PRICE_CLOSE);
      int h5s = iMA(symbol, PERIOD_M5, 21, 0, MODE_EMA, PRICE_CLOSE);
      int hrsi = iRSI(symbol, PERIOD_M1, 14, PRICE_CLOSE);
      int hatr = iATR(symbol, PERIOD_M1, 14);
      if(h1f == INVALID_HANDLE || h1s == INVALID_HANDLE ||
         h5f == INVALID_HANDLE || h5s == INVALID_HANDLE ||
         hrsi == INVALID_HANDLE || hatr == INVALID_HANDLE)
      {
         if(h1f != INVALID_HANDLE) IndicatorRelease(h1f);
         if(h1s != INVALID_HANDLE) IndicatorRelease(h1s);
         if(h5f != INVALID_HANDLE) IndicatorRelease(h5f);
         if(h5s != INVALID_HANDLE) IndicatorRelease(h5s);
         if(hrsi != INVALID_HANDLE) IndicatorRelease(hrsi);
         if(hatr != INVALID_HANDLE) IndicatorRelease(hatr);
         Print("LM AUTO TRADER: Indicator initialization failed for ", symbol, ". Symbol skipped.");
         continue;
      }
      g_scalp_symbols[i] = symbol;
      g_m1_fast[i] = h1f;
      g_m1_slow[i] = h1s;
      g_m5_fast[i] = h5f;
      g_m5_slow[i] = h5s;
      g_m1_rsi[i] = hrsi;
      g_m1_atr[i] = hatr;
      available++;
   }
   Print("LM AUTO TRADER: Autonomous scalper initialized for ", available, "/", SCALP_SYMBOL_COUNT, " symbols.");
   return available > 0;
}

void ReleaseScalperIndicators()
{
   for(int i = 0; i < SCALP_SYMBOL_COUNT; i++)
   {
      if(g_m1_fast[i] != INVALID_HANDLE) IndicatorRelease(g_m1_fast[i]);
      if(g_m1_slow[i] != INVALID_HANDLE) IndicatorRelease(g_m1_slow[i]);
      if(g_m5_fast[i] != INVALID_HANDLE) IndicatorRelease(g_m5_fast[i]);
      if(g_m5_slow[i] != INVALID_HANDLE) IndicatorRelease(g_m5_slow[i]);
      if(g_m1_rsi[i] != INVALID_HANDLE) IndicatorRelease(g_m1_rsi[i]);
      if(g_m1_atr[i] != INVALID_HANDLE) IndicatorRelease(g_m1_atr[i]);
      g_m1_fast[i] = INVALID_HANDLE;
      g_m1_slow[i] = INVALID_HANDLE;
      g_m5_fast[i] = INVALID_HANDLE;
      g_m5_slow[i] = INVALID_HANDLE;
      g_m1_rsi[i] = INVALID_HANDLE;
      g_m1_atr[i] = INVALID_HANDLE;
   }
}

string ProfitTargetKey(const string suffix)
{
   string server = AccountInfoString(ACCOUNT_SERVER);
   StringReplace(server, " ", "_");
   StringReplace(server, ".", "_");
   StringReplace(server, "-", "_");
   StringReplace(server, "/", "_");
   if(StringLen(server) > 12) server = StringSubstr(server, 0, 12);
   return StringFormat("LMAT_%I64d_%I64u_%d_%s_%s",
                       AccountInfoInteger(ACCOUNT_LOGIN), InpMagicNumber,
                       InpProfitTargetCycle, server, suffix);
}

bool ProfitTargetIsLocked()
{
   if(InpProfitTargetUSD <= 0.0) return false;
   string hit_key = ProfitTargetKey("HIT");
   string base_key = ProfitTargetKey("BASE");
   if(!GlobalVariableCheck(base_key))
      GlobalVariableSet(base_key, AccountInfoDouble(ACCOUNT_BALANCE));
   if(!GlobalVariableCheck(hit_key))
      GlobalVariableSet(hit_key, 0.0);

   double baseline = GlobalVariableGet(base_key);
   if(GlobalVariableGet(hit_key) >= 1.0) return true;
   if(AccountInfoDouble(ACCOUNT_EQUITY) >= baseline + InpProfitTargetUSD)
   {
      GlobalVariableSet(hit_key, 1.0);
      Print("LM AUTO TRADER: Persistent $", DoubleToString(InpProfitTargetUSD, 2),
            " profit target reached. This cycle is locked. Increment InpProfitTargetCycle to start a new cycle.");
      return true;
   }
   return false;
}

void CloseAllOurPositions(const string reason)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || (ulong)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber) continue;
      if(!g_trade.PositionClose(ticket))
         Print("LM AUTO TRADER: Could not close EA position ", ticket, " during ", reason,
               ". Retcode=", g_trade.ResultRetcode(), " ", g_trade.ResultRetcodeDescription());
   }
}

int FindScalperSymbolIndex(const string symbol)
{
   for(int i = 0; i < SCALP_SYMBOL_COUNT; i++)
      if(g_scalp_symbols[i] != "" && g_scalp_symbols[i] == symbol) return i;
   return -1;
}

double ReadInitialRiskFromComment(const string comment)
{
   int marker = StringFind(comment, "LMR=");
   if(marker < 0) return 0.0;
   return StringToDouble(StringSubstr(comment, marker + 4));
}

void ManageOpenScalpPositions()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || (ulong)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber) continue;

      string symbol = PositionGetString(POSITION_SYMBOL);
      datetime opened = (datetime)PositionGetInteger(POSITION_TIME);
      if(InpMaxHoldingMinutes > 0 && TimeCurrent() - opened >= InpMaxHoldingMinutes * 60)
      {
         if(!g_trade.PositionClose(ticket))
            Print("LM AUTO TRADER: Maximum holding-time exit failed for ", symbol, ". ", g_trade.ResultRetcodeDescription());
         else
            Print("LM AUTO TRADER: Closed ", symbol, " after the ", InpMaxHoldingMinutes, "-minute holding limit.");
         continue;
      }

      double initial_risk = ReadInitialRiskFromComment(PositionGetString(POSITION_COMMENT));
      if(initial_risk <= 0.0) continue;
      int index = FindScalperSymbolIndex(symbol);
      if(index < 0 || g_m1_atr[index] == INVALID_HANDLE) continue;

      double atr_values[];
      ArraySetAsSeries(atr_values, true);
      if(CopyBuffer(g_m1_atr[index], 0, 0, 2, atr_values) < 2 || atr_values[1] <= 0.0) continue;

      ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      bool is_buy = (type == POSITION_TYPE_BUY);
      double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
      double current_sl = PositionGetDouble(POSITION_SL);
      double current_tp = PositionGetDouble(POSITION_TP);
      double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      if(point <= 0.0 || open_price <= 0.0) continue;

      MqlTick tick;
      if(!SymbolInfoTick(symbol, tick)) continue;
      double favorable_move = is_buy ? tick.bid - open_price : open_price - tick.ask;
      double proposed_sl = current_sl;

      // Break-even at 1R plus two points.
      if(favorable_move >= initial_risk)
      {
         double break_even = open_price + (is_buy ? 2.0 * point : -2.0 * point);
         if(is_buy && (proposed_sl <= 0.0 || break_even > proposed_sl)) proposed_sl = break_even;
         if(!is_buy && (proposed_sl <= 0.0 || break_even < proposed_sl)) proposed_sl = break_even;
      }

      // Start ATR trailing only after 1.2R has been reached.
      if(favorable_move >= initial_risk * 1.2)
      {
         double trailing = is_buy ? tick.bid - atr_values[1] : tick.ask + atr_values[1];
         if(is_buy && trailing > proposed_sl) proposed_sl = trailing;
         if(!is_buy && (proposed_sl <= 0.0 || trailing < proposed_sl)) proposed_sl = trailing;
      }

      proposed_sl = NormalizeDouble(proposed_sl, digits);
      if(proposed_sl <= 0.0 || proposed_sl == current_sl) continue;
      long stop_level = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
      if(is_buy)
      {
         if(proposed_sl >= tick.bid || (stop_level > 0 && (tick.bid - proposed_sl) / point < stop_level)) continue;
         if(current_sl > 0.0 && proposed_sl <= current_sl) continue;
      }
      else
      {
         if(proposed_sl <= tick.ask || (stop_level > 0 && (proposed_sl - tick.ask) / point < stop_level)) continue;
         if(current_sl > 0.0 && proposed_sl >= current_sl) continue;
      }
      if(!g_trade.PositionModify(ticket, proposed_sl, current_tp))
         Print("LM AUTO TRADER: Could not update protective stop for ", symbol, ". ", g_trade.ResultRetcodeDescription());
   }
}

bool IsHighImpactNewsWindow(const string symbol)
{
   if(!InpUseEconomicCalendar) return false;
   datetime now = TimeTradeServer();
   if(now <= 0) return true; // Fail closed if broker server time is unavailable.

   string currencies[2];
   int currency_count = 0;
   if(StringFind(symbol, "XAUUSD") == 0)
   {
      currencies[0] = "USD";
      currency_count = 1;
   }
   else
   {
      currencies[0] = StringSubstr(symbol, 0, 3);
      currencies[1] = StringSubstr(symbol, 3, 3);
      currency_count = 2;
   }

   for(int c = 0; c < currency_count; c++)
   {
      if(StringLen(currencies[c]) != 3) continue;
      MqlCalendarValue values[];
      ResetLastError();
      int count = CalendarValueHistory(values, now - InpNewsBlockMinutes * 60,
                                       now + InpNewsBlockMinutes * 60, NULL, currencies[c]);
      if(count < 0)
      {
         if(!g_calendar_unavailable_warned)
         {
            Print("LM AUTO TRADER: Economic calendar is unavailable (", GetLastError(),
                  "). New scalping entries are blocked until calendar data is available.");
            g_calendar_unavailable_warned = true;
         }
         return true;
      }
      for(int i = 0; i < count; i++)
      {
         MqlCalendarEvent event;
         if(CalendarEventById(values[i].event_id, event) &&
            event.importance == CALENDAR_IMPORTANCE_HIGH)
            return true;
      }
   }
   return false;
}

void ReportScalpTrade(const ulong order_ticket, const ulong position_id,
                      const string symbol, const bool is_buy,
                      const double requested_volume, const double filled_volume,
                      const double entry_price, const double stop_loss,
                      const double take_profit, const double initial_risk)
{
   string body = StringFormat(
      "{\"action\":\"scalp_trade\",\"magic_number\":%I64u,\"order_ticket\":%I64u,"
      "\"position_id\":%I64u,\"symbol\":\"%s\",\"direction\":\"%s\","
      "\"requested_volume\":%.8f,\"filled_volume\":%.8f,\"entry_price\":%.10f,"
      "\"stop_loss\":%.10f,\"take_profit\":%.10f,\"initial_risk_distance\":%.10f,"
      "\"account_login\":%I64d,\"broker_server\":\"%s\"}",
      InpMagicNumber, order_ticket, position_id, JsonEscape(symbol),
      (is_buy ? "BUY" : "SELL"), requested_volume, filled_volume, entry_price,
      stop_loss, take_profit, initial_risk, AccountInfoInteger(ACCOUNT_LOGIN),
      JsonEscape(AccountInfoString(ACCOUNT_SERVER)));
   string response;
   int status = HttpRequest("POST", "", body, g_device_token, response);
   if(status != 200)
      Print("LM AUTO TRADER: Scalping trade ledger sync failed for position ", position_id,
            ". HTTP ", status, " ", response);
}

void TryScalpingEntry(const int index)
{
   string symbol = g_scalp_symbols[index];
   if(symbol == "" || g_m1_fast[index] == INVALID_HANDLE) return;
   if(CountOurOpenPositions() >= InpMaxOpenPositions || HasAnyPositionOnSymbol(symbol)) return;
   if(!DailyRiskAllowsAnotherTrade()) return;

   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick) || tick.ask <= 0.0 || tick.bid <= 0.0) return;
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(point <= 0.0) return;
   double spread_points = (tick.ask - tick.bid) / point;
   int allowed_spread = (StringFind(symbol, "XAUUSD") == 0 ? InpMaxSpreadGoldPoints : InpMaxSpreadFxPoints);
   if(InpMaxSpreadPoints > 0) allowed_spread = (int)MathMin(allowed_spread, InpMaxSpreadPoints);
   if(spread_points > allowed_spread) return;
   if(IsHighImpactNewsWindow(symbol)) return;

   double m1_fast[], m1_slow[], m5_fast[], m5_slow[], rsi_values[], atr_values[];
   ArraySetAsSeries(m1_fast, true);
   ArraySetAsSeries(m1_slow, true);
   ArraySetAsSeries(m5_fast, true);
   ArraySetAsSeries(m5_slow, true);
   ArraySetAsSeries(rsi_values, true);
   ArraySetAsSeries(atr_values, true);
   if(CopyBuffer(g_m1_fast[index], 0, 0, 3, m1_fast) < 3 ||
      CopyBuffer(g_m1_slow[index], 0, 0, 3, m1_slow) < 3 ||
      CopyBuffer(g_m5_fast[index], 0, 0, 2, m5_fast) < 2 ||
      CopyBuffer(g_m5_slow[index], 0, 0, 2, m5_slow) < 2 ||
      CopyBuffer(g_m1_rsi[index], 0, 0, 2, rsi_values) < 2 ||
      CopyBuffer(g_m1_atr[index], 0, 0, 2, atr_values) < 2) return;

   datetime closed_bar = iTime(symbol, PERIOD_M1, 1);
   if(closed_bar <= 0 || closed_bar == g_last_scalp_signal_bar[index]) return;
   g_last_scalp_signal_bar[index] = closed_bar;

   bool buy_cross = (m1_fast[1] > m1_slow[1] && m1_fast[2] <= m1_slow[2]);
   bool sell_cross = (m1_fast[1] < m1_slow[1] && m1_fast[2] >= m1_slow[2]);
   bool m5_bullish = (m5_fast[1] > m5_slow[1]);
   bool m5_bearish = (m5_fast[1] < m5_slow[1]);
   bool is_buy = buy_cross && m5_bullish && rsi_values[1] >= 52.0;
   bool is_sell = sell_cross && m5_bearish && rsi_values[1] <= 48.0;
   if(!is_buy && !is_sell) return;
   if(atr_values[1] <= 0.0) return;

   double entry = is_buy ? tick.ask : tick.bid;
   double initial_risk = atr_values[1] * 1.5;
   double sl = NormalizeDouble(entry + (is_buy ? -initial_risk : initial_risk), digits);
   double tp = NormalizeDouble(entry + (is_buy ? initial_risk * 1.5 : -initial_risk * 1.5), digits);
   long stops_level = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
   if(stops_level > 0 &&
      (initial_risk / point < stops_level || (initial_risk * 1.5) / point < stops_level)) return;

   double volume = 0.0;
   if(!CalculateSafeVolume(symbol, is_buy, entry, sl, volume)) return;
   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetDeviationInPoints(InpMaximumSlippagePoints);
   if(!g_trade.SetTypeFillingBySymbol(symbol)) return;

   string comment = "LMR=" + DoubleToString(initial_risk, 8);
   bool sent = is_buy ? g_trade.Buy(volume, symbol, 0.0, sl, tp, comment)
                      : g_trade.Sell(volume, symbol, 0.0, sl, tp, comment);
   uint retcode = g_trade.ResultRetcode();
   bool filled = sent && (retcode == TRADE_RETCODE_DONE || retcode == TRADE_RETCODE_DONE_PARTIAL);
   if(!filled)
   {
      Print("LM AUTO TRADER: Scalping order rejected for ", symbol, ". Retcode=",
            retcode, " ", g_trade.ResultRetcodeDescription());
      return;
   }

   ulong deal_ticket = g_trade.ResultDeal();
   ulong order_ticket = g_trade.ResultOrder();
   if(order_ticket == 0) order_ticket = deal_ticket;
   ulong position_id = 0;
   double filled_volume = g_trade.ResultVolume();
   double fill_price = g_trade.ResultPrice();
   if(deal_ticket > 0 && HistoryDealSelect(deal_ticket))
   {
      position_id = (ulong)HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID);
      if(filled_volume <= 0.0) filled_volume = HistoryDealGetDouble(deal_ticket, DEAL_VOLUME);
      if(fill_price <= 0.0) fill_price = HistoryDealGetDouble(deal_ticket, DEAL_PRICE);
   }
   if(position_id > 0)
      ReportScalpTrade(order_ticket, position_id, symbol, is_buy, volume,
                       MathMax(filled_volume, 0.001), MathMax(fill_price, entry), sl, tp, initial_risk);
   Print("LM AUTO TRADER SCALP: ", (is_buy ? "BUY " : "SELL "), symbol,
         " volume=", DoubleToString(filled_volume, 2),
         " SL=", DoubleToString(sl, digits), " TP=", DoubleToString(tp, digits),
         " risk=", DoubleToString(InpRiskPercent, 2), "%");
}

void ScanScalpingSymbols()
{
   if(!InpUseAutonomousScalper || !IsTradingPermissionAvailable()) return;
   if(ProfitTargetIsLocked()) return;
   for(int i = 0; i < SCALP_SYMBOL_COUNT; i++)
      TryScalpingEntry(i);
}
