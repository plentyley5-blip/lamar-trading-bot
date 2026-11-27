//+------------------------------------------------------------------+
//| LM AUTO TRADER                                                   |
//| LM Analyzer signal execution bridge for MetaTrader 5             |
//|                                                                  |
//| SAFETY DEFAULTS: auto trading OFF; live trading OFF.              |
//| First validation must be performed on an MT5 DEMO account.       |
//+------------------------------------------------------------------+
#property strict
#property version   "1.00"
#property description "Executes eligible LM Analyzer READY/MARKET signals with user-defined risk limits."

#include <Trade/Trade.mqh>

input string InpApiBase = "https://lamar-trading-bot.vercel.app/api/mt5_trader";
input string InpOneTimeLinkCode = "";
input bool   InpEnableAutoTrading = false;
input bool   InpAllowLiveTrading = false;
input double InpRiskPercent = 0.25;
input double InpMaxDailyLossPercent = 2.0;
input int    InpMaxOpenPositions = 1;
input int    InpMaxTradesPerDay = 3;
input double InpMaximumLot = 0.10;
input int    InpMinimumConfidence = 75;
input int    InpMaximumSignalAgeMinutes = 10;
input int    InpMaxEntryDeviationPoints = 50;
input int    InpMaxSpreadPoints = 50;
input int    InpMaximumSlippagePoints = 20;
input int    InpPollIntervalSeconds = 5;
input ulong  InpMagicNumber = 26112701;

#define LM_API_TIMEOUT_MS 5000
#define LM_RISK_BUFFER 0.85

CTrade g_trade;
string g_device_token = "";
string g_state_file = "";
datetime g_last_deal_time = 0;
ulong g_last_deal_ticket = 0;
bool g_server_live_enabled = false;
datetime g_last_configure = 0;

string JsonEscape(string value)
{
   StringReplace(value, "\\", "\\\\");
   StringReplace(value, "\"", "\\\"");
   StringReplace(value, "\r", " ");
   StringReplace(value, "\n", " ");
   return value;
}

string GetJsonString(const string source, const string key)
{
   string marker = "\"" + key + "\"";
   int p = StringFind(source, marker);
   if(p < 0) return "";
   p = StringFind(source, ":", p + StringLen(marker));
   if(p < 0) return "";
   p++;
   while(p < StringLen(source))
   {
      ushort c = StringGetCharacter(source, p);
      if(c != ' ' && c != '\t' && c != '\r' && c != '\n') break;
      p++;
   }
   if(p >= StringLen(source) || StringGetCharacter(source, p) != '"') return "";
   p++;
   int end = p;
   while(end < StringLen(source))
   {
      ushort c = StringGetCharacter(source, end);
      if(c == '"' && (end == p || StringGetCharacter(source, end - 1) != '\\')) break;
      end++;
   }
   if(end >= StringLen(source)) return "";
   return StringSubstr(source, p, end - p);
}

int HttpRequest(const string method, const string path, const string body,
                const string bearer, string &response)
{
   string url = InpApiBase + path;
   string headers = "Content-Type: application/json\r\n";
   if(bearer != "") headers += "Authorization: Bearer " + bearer + "\r\n";

   char request_data[];
   ArrayResize(request_data, 0);
   if(body != "")
   {
      int copied = StringToCharArray(body, request_data, 0, WHOLE_ARRAY, CP_UTF8);
      if(copied > 0) ArrayResize(request_data, copied - 1);
   }

   char result_data[];
   string result_headers = "";
   ResetLastError();
   int status = WebRequest(method, url, headers, LM_API_TIMEOUT_MS,
                           request_data, result_data, result_headers);
   if(status == -1)
   {
      Print("LM AUTO TRADER: WebRequest failed. Error=", GetLastError(),
            ". Add https://lamar-trading-bot.vercel.app to MT5 allowed URLs.");
      response = "";
      return -1;
   }
   response = CharArrayToString(result_data, 0, -1, CP_UTF8);
   return status;
}

string AccountModeText()
{
   ENUM_ACCOUNT_TRADE_MODE mode = (ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(mode == ACCOUNT_TRADE_MODE_REAL) return "real";
   if(mode == ACCOUNT_TRADE_MODE_CONTEST) return "contest";
   return "demo";
}

string MakeStateFileName()
{
   string server = AccountInfoString(ACCOUNT_SERVER);
   StringReplace(server, " ", "_");
   StringReplace(server, "/", "_");
   StringReplace(server, "\\", "_");
   StringReplace(server, ":", "_");
   return StringFormat("LM_AUTO_TRADER_%I64d_%s.dat",
                       AccountInfoInteger(ACCOUNT_LOGIN), server);
}

void LoadState()
{
   g_state_file = MakeStateFileName();
   int handle = FileOpen(g_state_file, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE) return;
   if(!FileIsEnding(handle)) g_device_token = FileReadString(handle);
   if(!FileIsEnding(handle)) g_last_deal_time = (datetime)StringToInteger(FileReadString(handle));
   if(!FileIsEnding(handle)) g_last_deal_ticket = (ulong)StringToInteger(FileReadString(handle));
   FileClose(handle);
}

void SaveState()
{
   int handle = FileOpen(g_state_file, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
   {
      Print("LM AUTO TRADER: Could not save local link state. Error=", GetLastError());
      return;
   }
   FileWriteString(handle, g_device_token + "\r\n");
   FileWriteString(handle, IntegerToString((long)g_last_deal_time) + "\r\n");
   FileWriteString(handle, IntegerToString((long)g_last_deal_ticket) + "\r\n");
   FileClose(handle);
}

bool LinkTerminal()
{
   if(StringLen(InpOneTimeLinkCode) < 40)
   {
      Print("LM AUTO TRADER: No valid one-time link code. Generate it from the signed-in LM Analyzer account first.");
      return false;
   }
   string body = StringFormat(
      "{\"action\":\"link\",\"link_code\":\"%s\",\"account_login\":%I64d,\"broker_server\":\"%s\",\"account_mode\":\"%s\"}",
      JsonEscape(InpOneTimeLinkCode), AccountInfoInteger(ACCOUNT_LOGIN),
      JsonEscape(AccountInfoString(ACCOUNT_SERVER)), AccountModeText());
   string response;
   int status = HttpRequest("POST", "", body, "", response);
   if(status != 200)
   {
      Print("LM AUTO TRADER: Account linking failed. HTTP ", status, " ", response);
      return false;
   }
   g_device_token = GetJsonString(response, "device_token");
   if(StringLen(g_device_token) < 40)
   {
      Print("LM AUTO TRADER: The server did not return a valid device token.");
      g_device_token = "";
      return false;
   }
   SaveState();
   Print("LM AUTO TRADER: MT5 account linked. Auto trading remains OFF until enabled in EA inputs.");
   return true;
}

bool ConfigureServer()
{
   if(g_device_token == "") return false;
   string body = StringFormat(
      "{\"action\":\"configure\",\"account_login\":%I64d,\"broker_server\":\"%s\","
      "\"auto_trade_enabled\":%s,\"risk_per_trade\":%.4f,\"max_daily_loss_percent\":%.4f,"
      "\"max_open_positions\":%d,\"max_trades_per_day\":%d,\"max_lot\":%.4f,"
      "\"min_confidence\":%d,\"max_signal_age_minutes\":%d,"
      "\"max_entry_deviation_points\":%d,\"max_spread_points\":%d}",
      AccountInfoInteger(ACCOUNT_LOGIN), JsonEscape(AccountInfoString(ACCOUNT_SERVER)),
      (InpEnableAutoTrading ? "true" : "false"), InpRiskPercent, InpMaxDailyLossPercent,
      InpMaxOpenPositions, InpMaxTradesPerDay, InpMaximumLot, InpMinimumConfidence,
      InpMaximumSignalAgeMinutes, InpMaxEntryDeviationPoints, InpMaxSpreadPoints);
   string response;
   int status = HttpRequest("POST", "", body, g_device_token, response);
   if(status != 200)
   {
      Print("LM AUTO TRADER: Could not save risk settings. HTTP ", status, " ", response);
      return false;
   }
   g_last_configure = TimeCurrent();
   return true;
}

bool IsTradingPermissionAvailable()
{
   if(!InpEnableAutoTrading) return false;
   if(!TerminalInfoInteger(TERMINAL_CONNECTED)) return false;
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) return false;
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED)) return false;
   if(!AccountInfoInteger(ACCOUNT_TRADE_ALLOWED)) return false;
   if(!AccountInfoInteger(ACCOUNT_TRADE_EXPERT)) return false;

   ENUM_ACCOUNT_TRADE_MODE mode = (ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(mode == ACCOUNT_TRADE_MODE_REAL)
   {
      if(!InpAllowLiveTrading || !g_server_live_enabled)
      {
         Print("LM AUTO TRADER: Live trading is blocked. The EA input and server-side live authorization must both be enabled.");
         return false;
      }
   }
   else if(mode != ACCOUNT_TRADE_MODE_DEMO && mode != ACCOUNT_TRADE_MODE_CONTEST)
   {
      return false;
   }
   return true;
}

string ResolveBrokerSymbol(const string base_symbol)
{
   if(SymbolSelect(base_symbol, true)) return base_symbol;
   string match = "";
   int total = SymbolsTotal(false);
   for(int i = 0; i < total; i++)
   {
      string candidate = SymbolName(i, false);
      if(StringLen(candidate) < StringLen(base_symbol)) continue;
      if(StringSubstr(candidate, 0, StringLen(base_symbol)) == base_symbol)
      {
         if(match != "") return "";
         match = candidate;
      }
   }
   if(match != "" && SymbolSelect(match, true)) return match;
   return "";
}

int VolumeDigits(const double step)
{
   for(int digits = 0; digits <= 8; digits++)
   {
      if(MathAbs(NormalizeDouble(step, digits) - step) < 0.000000001)
         return digits;
   }
   return 2;
}

double RoundVolumeDown(const string symbol, const double requested)
{
   double min_volume = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double max_volume = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0 || min_volume <= 0.0 || max_volume <= 0.0) return 0.0;
   double capped = MathMin(requested, MathMin(max_volume, InpMaximumLot));
   double rounded = MathFloor((capped + 1e-10) / step) * step;
   rounded = NormalizeDouble(rounded, VolumeDigits(step));
   if(rounded < min_volume || rounded > max_volume) return 0.0;
   return rounded;
}

int CountOurOpenPositions()
{
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) == InpMagicNumber) count++;
   }
   return count;
}

bool HasAnyPositionOnSymbol(const string symbol)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == symbol) return true;
   }
   return false;
}

int CountOurEntriesToday(double &today_net)
{
   today_net = 0.0;
   MqlDateTime parts;
   TimeToStruct(TimeCurrent(), parts);
   parts.hour = 0;
   parts.min = 0;
   parts.sec = 0;
   datetime day_start = StructToTime(parts);
   if(!HistorySelect(day_start, TimeCurrent())) return -1;
   int entries = 0;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      if((ulong)HistoryDealGetInteger(ticket, DEAL_MAGIC) != InpMagicNumber) continue;
      today_net += HistoryDealGetDouble(ticket, DEAL_PROFIT)
                 + HistoryDealGetDouble(ticket, DEAL_COMMISSION)
                 + HistoryDealGetDouble(ticket, DEAL_SWAP);
      ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_IN || entry == DEAL_ENTRY_INOUT) entries++;
   }
   for(int p = PositionsTotal() - 1; p >= 0; p--)
   {
      ulong position_ticket = PositionGetTicket(p);
      if(position_ticket == 0) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber) continue;
      today_net += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
   }
   return entries;
}

double FloatingOurPnl()
{
   double value = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber) continue;
      value += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
   }
   return value;
}

bool DailyRiskAllowsAnotherTrade()
{
   double today_net = 0.0;
   int entries = CountOurEntriesToday(today_net);
   if(entries < 0)
   {
      Print("LM AUTO TRADER: Could not read today's trade history. New order blocked.");
      return false;
   }
   if(entries >= InpMaxTradesPerDay)
   {
      Print("LM AUTO TRADER: Daily trade-count limit reached.");
      return false;
   }
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double starting_balance = balance - (today_net - FloatingOurPnl());
   if(starting_balance <= 0.0) starting_balance = AccountInfoDouble(ACCOUNT_EQUITY);
   double max_loss_cash = starting_balance * InpMaxDailyLossPercent / 100.0;
   if(today_net <= -max_loss_cash)
   {
      Print("LM AUTO TRADER: Daily loss limit reached. New orders are blocked until the next broker-server day.");
      return false;
   }
   return true;
}

bool CalculateSafeVolume(const string symbol, const bool is_buy, const double market_price,
                         const double stop_loss, double &volume_out)
{
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(equity <= 0.0) return false;
   double risk_cash = equity * InpRiskPercent / 100.0 * LM_RISK_BUFFER;
   ENUM_ORDER_TYPE order_type = is_buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double one_lot_loss = 0.0;
   if(!OrderCalcProfit(order_type, symbol, 1.0, market_price, stop_loss, one_lot_loss))
   {
      Print("LM AUTO TRADER: Broker could not calculate stop-loss exposure. Trade blocked.");
      return false;
   }
   if(one_lot_loss >= 0.0 || !MathIsValidNumber(one_lot_loss)) return false;
   double raw_volume = risk_cash / MathAbs(one_lot_loss);
   double volume = RoundVolumeDown(symbol, raw_volume);
   if(volume <= 0.0)
   {
      Print("LM AUTO TRADER: Safe position size is below broker minimum or exceeds no configured volume.");
      return false;
   }
   double actual_loss = 0.0;
   if(!OrderCalcProfit(order_type, symbol, volume, market_price, stop_loss, actual_loss)) return false;
   if(actual_loss >= 0.0 || MathAbs(actual_loss) > risk_cash * 1.001) return false;

   double margin = 0.0;
   if(!OrderCalcMargin(order_type, symbol, volume, market_price, margin))
   {
      Print("LM AUTO TRADER: Broker could not calculate required margin. Trade blocked.");
      return false;
   }
   double free_margin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   if(margin <= 0.0 || margin > free_margin * 0.50)
   {
      Print("LM AUTO TRADER: Insufficient free-margin safety buffer. Trade blocked.");
      return false;
   }
   volume_out = volume;
   return true;
}

bool ClaimSignal(const string signal_id)
{
   string body = StringFormat(
      "{\"action\":\"claim\",\"signal_id\":\"%s\",\"account_login\":%I64d,\"broker_server\":\"%s\"}",
      signal_id, AccountInfoInteger(ACCOUNT_LOGIN), JsonEscape(AccountInfoString(ACCOUNT_SERVER)));
   string response;
   int status = HttpRequest("POST", "", body, g_device_token, response);
   if(status != 200)
   {
      if(status != 400) Print("LM AUTO TRADER: Signal claim failed (HTTP ", status, "). ", response);
      return false;
   }
   return (StringFind(response, "\"claimed\":true") >= 0);
}

void ReportExecution(const string signal_id, const bool executed, const ulong order_ticket,
                     const ulong position_id, const double requested_volume,
                     const double filled_volume, const double fill_price, const string reason)
{
   string body;
   if(executed)
   {
      body = StringFormat(
         "{\"action\":\"report\",\"signal_id\":\"%s\",\"status\":\"executed\","
         "\"order_ticket\":%I64u,\"position_id\":%I64u,\"requested_volume\":%.8f,"
         "\"filled_volume\":%.8f,\"fill_price\":%.10f,\"account_login\":%I64d,\"broker_server\":\"%s\"}",
         signal_id, order_ticket, position_id, requested_volume, filled_volume, fill_price,
         AccountInfoInteger(ACCOUNT_LOGIN), JsonEscape(AccountInfoString(ACCOUNT_SERVER)));
   }
   else
   {
      body = StringFormat(
         "{\"action\":\"report\",\"signal_id\":\"%s\",\"status\":\"rejected\","
         "\"reason\":\"%s\",\"account_login\":%I64d,\"broker_server\":\"%s\"}",
         signal_id, JsonEscape(reason), AccountInfoInteger(ACCOUNT_LOGIN),
         JsonEscape(AccountInfoString(ACCOUNT_SERVER)));
   }
   string response;
   int status = HttpRequest("POST", "", body, g_device_token, response);
   if(status != 200) Print("LM AUTO TRADER: Execution report did not reach server. HTTP ", status, " ", response);
}

void ProcessSignal(string &fields[])
{
   if(ArraySize(fields) != 9) return;
   string signal_id = fields[1];
   string instrument = fields[2];
   string direction = fields[3];
   double signal_entry = StringToDouble(fields[4]);
   double sl = StringToDouble(fields[5]);
   double tp = StringToDouble(fields[6]);
   int confidence = (int)StringToInteger(fields[7]);
   datetime created = (datetime)StringToInteger(fields[8]);

   if(!IsTradingPermissionAvailable()) return;
   if(confidence < InpMinimumConfidence) return;
   if(created <= 0 || TimeCurrent() - created > InpMaximumSignalAgeMinutes * 60 || created > TimeCurrent() + 60) return;
   if(signal_entry <= 0.0 || sl <= 0.0 || tp <= 0.0) return;
   bool is_buy = (direction == "BUY");
   if(!is_buy && direction != "SELL") return;
   if(is_buy && !(sl < signal_entry && tp > signal_entry)) return;
   if(!is_buy && !(tp < signal_entry && sl > signal_entry)) return;

   string symbol = ResolveBrokerSymbol(instrument);
   if(symbol == "")
   {
      Print("LM AUTO TRADER: Could not resolve one unambiguous broker symbol for ", instrument, ".");
      return;
   }
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick) || tick.ask <= 0.0 || tick.bid <= 0.0) return;
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(point <= 0.0) return;
   double market_price = is_buy ? tick.ask : tick.bid;
   double spread_points = (tick.ask - tick.bid) / point;
   if(spread_points > InpMaxSpreadPoints)
   {
      Print("LM AUTO TRADER: Spread filter blocked ", symbol, ".");
      return;
   }
   if(MathAbs(market_price - signal_entry) / point > InpMaxEntryDeviationPoints)
   {
      Print("LM AUTO TRADER: Price moved outside the allowed entry deviation for ", symbol, ". No chasing.");
      return;
   }
   sl = NormalizeDouble(sl, digits);
   tp = NormalizeDouble(tp, digits);
   if(is_buy && !(sl < tick.bid && tp > tick.ask)) return;
   if(!is_buy && !(sl > tick.ask && tp < tick.bid)) return;
   long stops_level = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
   if(stops_level > 0)
   {
      if(MathAbs(market_price - sl) / point < stops_level || MathAbs(tp - market_price) / point < stops_level)
      {
         Print("LM AUTO TRADER: Broker stop-distance rules block this setup.");
         return;
      }
   }
   if(CountOurOpenPositions() >= InpMaxOpenPositions) return;
   if(HasAnyPositionOnSymbol(symbol))
   {
      Print("LM AUTO TRADER: An existing position already uses ", symbol, ". No netting/hedging collision allowed.");
      return;
   }
   if(!DailyRiskAllowsAnotherTrade()) return;

   double volume = 0.0;
   if(!CalculateSafeVolume(symbol, is_buy, market_price, sl, volume)) return;
   if(!ClaimSignal(signal_id)) return;

   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetDeviationInPoints(InpMaximumSlippagePoints);
   if(!g_trade.SetTypeFillingBySymbol(symbol))
   {
      ReportExecution(signal_id, false, 0, 0, volume, 0, 0, "Broker filling mode could not be configured.");
      return;
   }
   string comment = "LM-" + StringSubstr(signal_id, 0, 20);
   bool sent = is_buy ? g_trade.Buy(volume, symbol, 0.0, sl, tp, comment)
                      : g_trade.Sell(volume, symbol, 0.0, sl, tp, comment);
   uint retcode = g_trade.ResultRetcode();
   bool filled = sent && (retcode == TRADE_RETCODE_DONE || retcode == TRADE_RETCODE_DONE_PARTIAL);
   if(!filled)
   {
      string reason = StringFormat("Broker rejected the order. Retcode=%u; %s", retcode, g_trade.ResultRetcodeDescription());
      ReportExecution(signal_id, false, 0, 0, volume, 0, 0, reason);
      Print("LM AUTO TRADER: ", reason);
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
   ReportExecution(signal_id, true, order_ticket, position_id, volume,
                   MathMax(filled_volume, 0.001), MathMax(fill_price, market_price), "");
   Print("LM AUTO TRADER: Executed ", direction, " ", symbol, " volume=", DoubleToString(filled_volume, 2),
         " SL=", DoubleToString(sl, digits), " TP1=", DoubleToString(tp, digits),
         " risk cap=", DoubleToString(InpRiskPercent, 2), "%");
}

void PollServer()
{
   if(g_device_token == "") return;
   string response;
   int status = HttpRequest("GET", "?action=poll", "", g_device_token, response);
   if(status != 200)
   {
      if(status != 401) Print("LM AUTO TRADER: Signal polling failed. HTTP ", status, " ", response);
      return;
   }
   string lines[];
   int line_count = StringSplit(response, StringGetCharacter("\n", 0), lines);
   if(line_count <= 0) return;
   g_server_live_enabled = false;
   for(int i = 0; i < line_count; i++)
   {
      string line = lines[i];
      StringTrimLeft(line);
      StringTrimRight(line);
      if(line == "") continue;
      string fields[];
      int count = StringSplit(line, StringGetCharacter("|", 0), fields);
      if(count <= 0) continue;
      if(fields[0] == "STATE")
      {
         g_server_live_enabled = (StringFind(line, "LIVE=1") >= 0);
         if(count >= 2 && fields[1] != "ACTIVE" && fields[1] != "AUTO_DISABLED")
            Print("LM AUTO TRADER: Server state: ", line);
      }
      else if(fields[0] == "SIGNAL")
      {
         ProcessSignal(fields);
         return;
      }
   }
}

void SyncRecentDeals()
{
   if(g_device_token == "") return;
   datetime now = TimeCurrent();
   datetime from = (g_last_deal_time > 0 ? g_last_deal_time - 2 : now - 30 * 86400);
   if(!HistorySelect(from, now)) return;
   int total = HistoryDealsTotal();
   bool changed = false;
   for(int i = 0; i < total; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      datetime deal_time = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);
      if(deal_time < g_last_deal_time || (deal_time == g_last_deal_time && ticket <= g_last_deal_ticket)) continue;
      if((ulong)HistoryDealGetInteger(ticket, DEAL_MAGIC) == InpMagicNumber)
      {
         ulong position_id = (ulong)HistoryDealGetInteger(ticket, DEAL_POSITION_ID);
         if(position_id > 0)
         {
            string symbol = HistoryDealGetString(ticket, DEAL_SYMBOL);
            string entry = EnumToString((ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket, DEAL_ENTRY));
            string deal_type = EnumToString((ENUM_DEAL_TYPE)HistoryDealGetInteger(ticket, DEAL_TYPE));
            double volume = HistoryDealGetDouble(ticket, DEAL_VOLUME);
            double price = HistoryDealGetDouble(ticket, DEAL_PRICE);
            double profit = HistoryDealGetDouble(ticket, DEAL_PROFIT);
            double commission = HistoryDealGetDouble(ticket, DEAL_COMMISSION);
            double swap = HistoryDealGetDouble(ticket, DEAL_SWAP);
            string body = StringFormat(
               "{\"action\":\"deal\",\"magic_number\":%I64u,\"deal_ticket\":%I64u,\"position_id\":%I64u,"
               "\"symbol\":\"%s\",\"deal_entry\":\"%s\",\"deal_type\":\"%s\",\"volume\":%.8f,"
               "\"price\":%.10f,\"profit\":%.8f,\"commission\":%.8f,\"swap\":%.8f,"
               "\"deal_time_epoch\":%I64d,\"account_login\":%I64d,\"broker_server\":\"%s\"}",
               InpMagicNumber, ticket, position_id, JsonEscape(symbol), entry, deal_type, volume,
               price, profit, commission, swap, (long)deal_time,
               AccountInfoInteger(ACCOUNT_LOGIN), JsonEscape(AccountInfoString(ACCOUNT_SERVER)));
            string response;
            int status = HttpRequest("POST", "", body, g_device_token, response);
            if(status != 200 && status != 409)
               Print("LM AUTO TRADER: Deal history sync failed for ticket ", ticket, ". HTTP ", status);
         }
      }
      g_last_deal_time = deal_time;
      g_last_deal_ticket = ticket;
      changed = true;
   }
   if(changed) SaveState();
}

int OnInit()
{
   if(InpRiskPercent < 0.1 || InpRiskPercent > 1.0 ||
      InpMaxDailyLossPercent < 0.5 || InpMaxDailyLossPercent > 5.0 ||
      InpMaxOpenPositions < 1 || InpMaxOpenPositions > 3 ||
      InpMaxTradesPerDay < 1 || InpMaxTradesPerDay > 10 ||
      InpMaximumLot < 0.01 || InpMaximumLot > 5.0 ||
      InpMinimumConfidence < 50 || InpMinimumConfidence > 100 ||
      InpMaximumSignalAgeMinutes < 1 || InpMaximumSignalAgeMinutes > 120 ||
      InpMaxEntryDeviationPoints < 1 || InpMaxEntryDeviationPoints > 2000 ||
      InpMaxSpreadPoints < 1 || InpMaxSpreadPoints > 2000 ||
      InpMaximumSlippagePoints < 0 || InpMaximumSlippagePoints > 1000 ||
      InpPollIntervalSeconds < 3 || InpPollIntervalSeconds > 60)
   {
      Print("LM AUTO TRADER: One or more risk settings are outside permitted limits.");
      return INIT_PARAMETERS_INCORRECT;
   }
   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetDeviationInPoints(InpMaximumSlippagePoints);
   LoadState();
   if(g_device_token == "" && !LinkTerminal()) return INIT_FAILED;
   if(!ConfigureServer()) Print("LM AUTO TRADER: Risk settings could not be synchronized; no trades will be opened until sync succeeds.");
   EventSetTimer(InpPollIntervalSeconds);
   Print("LM AUTO TRADER initialized. Auto trading: ", (InpEnableAutoTrading ? "ON" : "OFF"),
         ". Account type: ", AccountModeText(), ". Live permission: server-gated and OFF by default.");
   if(!InpEnableAutoTrading) Print("LM AUTO TRADER is linked but idle. Set InpEnableAutoTrading=true to enable demo execution.");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   if(g_device_token != "")
   {
      string body = StringFormat(
         "{\"action\":\"configure\",\"account_login\":%I64d,\"broker_server\":\"%s\","
         "\"auto_trade_enabled\":false,\"risk_per_trade\":%.4f,\"max_daily_loss_percent\":%.4f,"
         "\"max_open_positions\":%d,\"max_trades_per_day\":%d,\"max_lot\":%.4f,"
         "\"min_confidence\":%d,\"max_signal_age_minutes\":%d,"
         "\"max_entry_deviation_points\":%d,\"max_spread_points\":%d}",
         AccountInfoInteger(ACCOUNT_LOGIN), JsonEscape(AccountInfoString(ACCOUNT_SERVER)),
         InpRiskPercent, InpMaxDailyLossPercent, InpMaxOpenPositions, InpMaxTradesPerDay,
         InpMaximumLot, InpMinimumConfidence, InpMaximumSignalAgeMinutes,
         InpMaxEntryDeviationPoints, InpMaxSpreadPoints);
      string response;
      HttpRequest("POST", "", body, g_device_token, response);
   }
   SaveState();
}

void OnTimer()
{
   if(g_device_token == "") return;
   if(TimeCurrent() - g_last_configure > 60) ConfigureServer();
   PollServer();
   SyncRecentDeals();
}

void OnTick()
{
   // Deliberately empty. Signal polling is timer-based; no trade is initiated
   // from arbitrary chart ticks, and no unrelated/manual position is modified.
}
