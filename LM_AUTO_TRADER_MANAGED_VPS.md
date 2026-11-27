# LM AUTO TRADER — managed Windows VPS rollout

## Confirmed product decisions

- Integrated into LM Analyzer as a separate paid product.
- Managed Windows VPS hosting is included in the LM AUTO TRADER subscription.
- Initial instruments: XAUUSD and major forex pairs.
- Billing periods and approved USD pricing: monthly $49.99; 3 months $125.99 (16% off); 6 months $249.99 (17% off).
- Google Play Billing is the intended provider. Purchase verification and plan activation remain paused until the Google Play developer account is available.
- Demo and live account modes are supported. Linking an account never starts trading; only the signed-in account owner pressing Start may activate that account's session.

## Implemented in this branch

1. `handlers/mt5_trader.py` accepts authenticated `dashboard` requests from the signed-in LM Analyzer user.
2. The same endpoint accepts terminal-authenticated `snapshot` requests from the linked EA.
3. Account snapshots contain broker-reported balance, equity, floating P/L, credit, margin, free margin, margin level, currency, leverage, terminal build, and open positions.
4. The backend retains only positions whose magic number equals `26112701`, so unrelated/manual positions are not presented as LM AUTO TRADER positions.
5. `mt5/LM_AUTO_TRADER.mq5` sends account telemetry every EA timer cycle and syncs deal history as before.
6. EA removal disables server-side execution; ordinary terminal reinitialization no longer sends a false OFF command.
7. `sql/lm_auto_trader_managed_vps.sql` adds account snapshots, VPS assignment metadata, and independent AUTO TRADER entitlements.
8. `handlers/lm_auto_trader_ai.py` is a separate Gemini market-decision module. It receives numerical broker M1/M5 OHLC data, not uploaded screenshots or `analysis_jobs`.
9. `sql/lm_auto_trader_ai_sessions_billing.sql` adds per-device market data, trading sessions, AI command queue, daily risk state, equity risk tiers, purchase-token metadata, and the approved plan catalog.
10. `handlers/mt5_trader.py` now exposes authenticated Start/Stop/status actions, receives broker-market data, runs a per-account AI cycle when MT5 polls, validates risk conditions, queues approved commands, and records command execution reports.
11. The dashboard API response includes session state, AI decision history, and broker market-data freshness.
12. Prices are saved in code, but plans remain disabled. Google Play verification is intentionally not active yet.

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

## Independent AI pipeline — current boundary

The backend path is independent from LM Analyzer chart analysis:

1. The linked MT5 terminal uploads broker M1/M5 candles and quotes to `market_data`.
2. The MT5 poll endpoint checks the linked device's own active session and entitlement.
3. Gemini analyses those broker candles per linked account and returns BUY, SELL, or WAIT with two separate confirmations.
4. The backend checks account equity tier, spread, entry deviation, stop/target geometry, minimum reward/risk, daily loss, drawdown, open positions, and trade count.
5. Valid decisions are written to that account's expiring AI command queue. The dashboard API can show those decisions.
6. The intended terminal adapter must consume AI commands, size against broker tick value/volume/margin, execute, and report fills. That terminal adapter is not complete in this branch.

**Critical:** The existing `mt5/LM_AUTO_TRADER.mq5` has not yet been converted to upload M1/M5 data or consume the new `AI_CMD` protocol. It still contains the previous local EMA/RSI scalping engine and optional legacy chart-signal client. Therefore the branch is a backend foundation, not a complete or testable live-trading chain. Do not attach this version and expect it to execute the new backend AI decisions.

## Safety gates

- Account linking leaves the session stopped.
- Start requires fresh terminal telemetry and fresh per-account broker candles.
- Start is account-specific and is authorized by an authenticated LM Analyzer session.
- Stop cancels queued commands and disables new entries; the terminal must continue protective management of open positions.
- Live mode is not globally disabled by the backend design; the explicit Start action is the user's authorization. MT5 terminal trade permissions and broker checks still apply.
- Risk settings are server-owned, but broker-side volume, margin, stop-distance, and quote validation must also run inside MT5.
- Never claim a trade was executed until a broker fill is confirmed and reported.
- A position's broker-side SL must be in place before considering the trade protected against VPS/network outages.
- Validate terminal reconnect, duplicate execution prevention, order rejection, VPS restart, internet outage, daily-loss stop, and user cancellation before launch.

## Android source boundary

The current Android `MainActivity.kt` is maintained separately from this backend repository. The saved Android source must be updated as a complete, additive replacement after the secure VPS provisioning contract and subscription prices are finalized. Do not replace the existing app with a reduced demo screen.
## Legacy EA logic — not the new AI execution client

The branch still contains the prior local LM SCALPER M1/M5 code and its existing break-even / trailing / 15-minute time-exit manager. Those local entry rules are not the new backend AI engine. Do not rely on them as the implementation of this new pipeline.

## Test and activation boundary

- The Supabase migration is committed in the feature branch but has not been applied to the live project.
- Google Play product verification is paused; the plans remain disabled. The owner test bypass is the only intended path for internal backend testing.
- The MQL5 adapter that sends broker candles, parses AI commands, places approved trades and reports broker fills has not been delivered or compiled.
- No end-to-end MT5 test has passed. No demo or live order should be attempted with this branch as the new AI system.
- Managed Windows VPS provisioning is also not implemented.
- Before any release, compile the MQL5 EA in MetaEditor, run unit/integration tests against a non-production database, then complete a controlled demo test covering account isolation, duplicate prevention, Start/Stop, stale data, risk locks, spread/news blocks, broker rejection, fill reporting, break-even, trailing, terminal restart and network loss.