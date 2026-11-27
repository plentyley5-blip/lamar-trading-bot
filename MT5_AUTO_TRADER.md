# LM AUTO TRADER — MT5 integration

This is an additive MT5 Expert Advisor and server bridge for LM Analyzer. It does not change the Android app's existing analysis, authentication, subscriptions, Community, High Impact News, support chat, or other screens.

## What is included

- A new authenticated API route at /api/mt5_trader.
- A one-time account-link code, valid for 15 minutes, issued to a signed-in LM Analyzer user.
- A unique random device token stored as a hash in Supabase. The EA never receives a Supabase key and never asks for an MT5 master or investor password.
- An MT5 Expert Advisor that runs inside the user's Windows MetaTrader 5 terminal or an MT5 VPS.
- Server-side storage of per-account risk settings, claimed signals, execution results, and deal-history records.
- A separate feature gate for live accounts.

## Safety and signal eligibility

Auto trading is OFF by default in the EA and in the database. Live trading is also OFF by default and requires both the EA input InpAllowLiveTrading and server environment variable LM_MT5_LIVE_TRADING_ENABLED=true. Do not enable the live gate until applicable regulatory review, broker approval, user terms, and comprehensive demo validation are complete.

The first version trades only newly completed LM Analyzer signals that satisfy every condition below:

- BUY or SELL.
- Chart instrument status is MATCH.
- Setup status is READY.
- Order type is MARKET.
- Confidence meets the user's configured threshold.
- Entry, stop-loss and TP1 each contain one exact numeric price.
- Stop and target are on the correct side of the entry.
- Signal age is within the configured limit.

WAITING, LATE ENTRY, INVALID, NO_SETUP, LIMIT, STOP, WAIT, NO TRADE, ambiguous price zones, and stale signals are not executed. The EA does not guess prices or convert pending setups into market orders.

## Risk controls

The EA uses the broker's live symbol contract specification through OrderCalcProfit to estimate account-currency loss at the stop. It rounds volume down, applies an additional 15 percent size buffer, and rejects a trade if broker calculations, margin, symbol metadata, stop distance, spread, or price deviation cannot be checked. It also enforces user-configured limits for risk per trade, daily loss, open positions, daily entries, maximum lot, signal confidence, signal age, spread, and entry deviation.

Each account is restricted to one LM Analyzer position per symbol at a time. The EA does not modify or close positions that do not carry its own magic number.

Risk controls cannot prevent losses from gaps, slippage, execution delays, broker outages, terminal/VPS outages, or other market events. A stop-loss is not a guaranteed maximum loss.

## Deployment

### 1. Database

The migration was applied to the LM Analyzer Supabase project and the SQL is committed at sql/lm_mt5_auto_trader.sql. The new tables have Row Level Security enabled and no anon/authenticated policies. Only server-side service_role requests receive table privileges.

### 2. Vercel environment

The endpoint reuses the existing SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_ANON_KEY and Supabase Auth setup. Optionally set LM_MT5_TOKEN_PEPPER to a high-entropy secret. Keep it only in Vercel environment variables.

Leave LM_MT5_LIVE_TRADING_ENABLED unset or false. The endpoint defaults to live trading disabled.

### 3. Android link-code integration

The current repository does not contain the Android application source. Therefore this change does not overwrite or synthesize MainActivity.kt. The existing Android app needs an additive button or section that calls:

POST https://lamar-trading-bot.vercel.app/api/mt5_trader

Header:
Authorization: Bearer <current Supabase access token>

JSON body:
{"action":"create_link_code"}

Show the returned one-time link_code in the signed-in user's app with a copy action and the 15-minute expiry. Never expose the Supabase service-role key to Android. The endpoint is ready for this app integration, but the Android UI is not included in this branch.

### 4. MetaTrader 5 installation

1. Download the EA source from mt5/LM_AUTO_TRADER.mq5 and open it in MetaEditor.
2. Compile the source in MetaEditor. This repository change has not been compiled by MetaEditor in this environment.
3. In MT5 desktop, open Tools → Options → Expert Advisors and add https://lamar-trading-bot.vercel.app to allowed WebRequest URLs.
4. Sign into an MT5 DEMO account first.
5. Generate a one-time link code from the signed-in LM Analyzer account.
6. Attach LM AUTO TRADER to one chart and enter the link code in InpOneTimeLinkCode.
7. Leave InpEnableAutoTrading=false for the first link check. Confirm the EA logs that the account is linked.
8. Configure conservative demo limits. Initial defaults are 0.25 percent risk per trade, 2 percent daily loss cap, one open position, three entries per day, 0.10 maximum lot and 75 minimum confidence.
9. After confirming the EA links, polling and account settings, explicitly enable InpEnableAutoTrading on the DEMO account to test actual demo execution.
10. Do not use a real account during initial testing.

The terminal needs to remain running and connected for signal polling and deal-history synchronization. MT5 mobile cannot host Expert Advisors. Users who need the terminal continuously available can use their own Windows PC or MT5 VPS.

### 5. Revoke access

A signed-in user can revoke all linked MT5 terminals by POSTing the same endpoint with the body:
{"action":"revoke_all"}

The server marks linked devices revoked and turns their server-side auto-trade flags off.

## Not included yet

- Automatic performance-fee settlement or broker-side commission splitting. MT5 deal history is captured for accounting, but an Expert Advisor cannot independently withdraw a user's funds. A broker/payment partner and compliant fee authorization are separate requirements.
- The Android link-code screen. The Android source is not present in this repository.
- MetaEditor compilation and a live broker demo. The Python endpoint unit tests are included; actual MT5 execution must be validated in MetaTrader 5 Strategy Tester/demo environment before wider testing.
- Live-account execution. It remains blocked server-side by default.
