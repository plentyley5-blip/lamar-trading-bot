# LM AUTO TRADER — managed Windows VPS rollout

## Confirmed product decisions

- Integrated into LM Analyzer as a separate paid product.
- Managed Windows VPS hosting is included in the LM AUTO TRADER subscription.
- Initial instruments: XAUUSD and major forex pairs.
- Billing periods: monthly, 3 months, and 6 months.
- Demo and live account modes are supported in the product design. Demo is the validation default; live requires separate user authorization and a server-side feature gate.

## Implemented in this branch

1. `handlers/mt5_trader.py` accepts authenticated `dashboard` requests from the signed-in LM Analyzer user.
2. The same endpoint accepts terminal-authenticated `snapshot` requests from the linked EA.
3. Account snapshots contain broker-reported balance, equity, floating P/L, credit, margin, free margin, margin level, currency, leverage, terminal build, and open positions.
4. The backend retains only positions whose magic number equals `26112701`, so unrelated/manual positions are not presented as LM AUTO TRADER positions.
5. `mt5/LM_AUTO_TRADER.mq5` sends account telemetry every EA timer cycle and syncs deal history as before.
6. EA removal disables server-side execution; ordinary terminal reinitialization no longer sends a false OFF command.
7. `sql/lm_auto_trader_managed_vps.sql` adds account snapshots, VPS assignment metadata, a separate subscription catalog, and independent LM AUTO TRADER entitlements. Prices are deliberately NULL and plans disabled until commercial prices are approved.

## API contract

### App → dashboard

`POST /api/mt5_trader`

Headers: `Authorization: Bearer <Supabase access token>`

Body:
```json
{"action":"dashboard"}
```

The response reports `telemetry_status` as:
- `not_connected`: no active linked terminal.
- `waiting_for_terminal`: terminal linked but has not submitted a snapshot.
- `live`: a connected terminal snapshot is no more than 45 seconds old.
- `stale`: terminal heartbeat is old or disconnected.

The dashboard never invents account values. Missing data remains null/empty.

### MT5 terminal → snapshot

`POST /api/mt5_trader` with the linked device token in the Authorization header.

The EA sends `{"action":"snapshot", ...}` with actual terminal account values and LM AUTO TRADER-owned positions. The server validates the linked login/server and magic number before storing the snapshot.

## Managed VPS phase still required

This branch is the telemetry and schema foundation. It does **not** yet provision Windows VPS machines or accept/store broker passwords. Do not advertise one-tap managed VPS onboarding until all of these exist and are tested:

1. Select and approve the Windows VPS provider and region.
2. Build a Windows provisioning agent/service that registers to the backend using a one-time enrollment token.
3. Provision an isolated MT5 terminal environment for each subscribed customer.
4. Use end-to-end credential handoff: the Android app encrypts MT5 credentials to the assigned VPS agent's public key; only that agent decrypts them locally. Never log or persist plaintext passwords in Vercel, Supabase, Android preferences, analytics, or support logs.
5. Agent installs/updates the signed EA, stores terminal credentials in Windows-protected local storage, reports heartbeat and terminal health, and restarts MT5 after machine reboot.
6. Add server-side subscription entitlement checks to link, enable, and provision actions. The new plans are inactive until prices and payment flow are approved.
7. Add a per-account kill switch, disconnect/revoke workflow, stale-heartbeat alerts, and safe behavior when the VPS is offline.

## Safety gates

- Demo is the default account mode.
- `LM_MT5_LIVE_TRADING_ENABLED` remains unset/false.
- EA `InpAllowLiveTrading` remains false by default.
- No automatic live activation or risk-limit bypass is permitted.
- A position's broker-side SL must be in place before considering the trade protected against VPS/network outages.
- Validate terminal reconnect, duplicate execution prevention, order rejection, VPS restart, internet outage, daily-loss stop, and user cancellation before launch.

## Android source boundary

The current Android `MainActivity.kt` is maintained separately from this backend repository. The saved Android source must be updated as a complete, additive replacement after the secure VPS provisioning contract and subscription prices are finalized. Do not replace the existing app with a reduced demo screen.
## Autonomous scalping strategy port

The EA now includes `mt5/LM_SCALPER_ENGINE.mqh`, which ports the previously agreed LM SCALPER v2.00 rules:

- M1 EMA 9/21 fresh closed-bar crossover.
- M5 EMA 9/21 directional confirmation.
- M1 RSI(14): BUY at or above 52; SELL at or below 48.
- M1 ATR(14): stop at 1.5 ATR; target at 1.5R.
- Risk cap 0.50% per trade, with the existing volume/margin safety buffer and 0.10 lot cap.
- Maximum two LM AUTO TRADER positions globally and one position per symbol.
- FX spread ceiling 30 points; XAUUSD ceiling 100 points.
- High-impact MT5 economic-calendar event block within 20 minutes before/after the event. Calendar times use broker trade-server time; if calendar lookup fails, new entries fail closed.
- Break-even at 1R plus two points; ATR(14) trailing starts at 1.2R.
- Maximum holding time 15 minutes (hard time-based exit for multi-scalping).
- 2% daily loss stop and the existing maximum-three-entries-per-day safety cap.
- Persistent $50 equity target above the terminal's cycle baseline. On target, LM AUTO TRADER closes only positions with its own magic number and locks that cycle. Increment `InpProfitTargetCycle` to start a new cycle.

Autonomous scalping is enabled as the EA strategy mode, but `InpEnableAutoTrading` remains false by default. The legacy LM Analyzer signal executor is retained behind `InpUseLMAnalyzerSignals=false` so it does not compete with the scalper.

## Strategy verification still required

The MQL5 source has not yet been compiled in MetaEditor in this workflow. Compile the EA and included `.mqh` on the target MT5 build, then test on demo across all eight symbols, including spread/news blocks, order rejections, stop modifications, max holding exit, daily loss stop, and the persistent profit lock. The $50 target is denominated in account currency in this initial EA implementation; only USD-denominated accounts should use the literal $50 setting until currency conversion is implemented.