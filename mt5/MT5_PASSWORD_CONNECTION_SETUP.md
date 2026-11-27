# LM AUTO TRADER — direct MT5 password connection

The Android connection form now requires these fields in this order:

1. Broker name
2. Exact MT5 server
3. Account number / login
4. MT5 trading password

The app sends them to `POST /api/mt5_trader` using the signed-in user's access token and action `connect_credentials`.

## Credential handling

- The backend validates the fields and forwards the password in memory to the Windows MT5 connector over HTTPS.
- The password is not written to `lm_mt5_devices`, any Supabase table, or application logs.
- The Android field is masked by default and the password state is cleared immediately after the connect action is submitted.
- Do not add password columns to Supabase or print request bodies in connector logs.

## Required connector service

Direct MT5 authentication cannot be completed by a Vercel function alone. The connector must run on a Windows host with a MetaTrader 5 terminal installed. MetaQuotes' Python integration accepts the login, password and server in `mt5.initialize(...)` / `mt5.login(...)`.

Configure these Vercel environment variables:

- `LM_MT5_CONNECTOR_URL`: private HTTPS URL for the Windows connector API.
- `LM_MT5_CONNECTOR_TOKEN`: high-entropy bearer token shared only by the backend and connector.

The connector must support two authenticated POST actions at that URL.

### 1. authenticate

Request fields:

- `action`: `authenticate`
- `connection_id`: one-request correlation ID
- `broker_name`
- `broker_server`
- `account_login`
- `account_password`

On success return JSON containing:

```json
{
  "authenticated": true,
  "account_login": 12345678,
  "broker_server": "Broker-Server",
  "account_mode": "demo"
}
```

Return `authenticated: false` for rejected credentials. Never echo the password.

### 2. activate

After the backend has registered the account-specific device, it sends:

- `action`: `activate`
- `connection_id`
- `device_id`
- `device_token`
- `user_id`
- `account_login`
- `broker_name`
- `broker_server`
- `account_mode`
- `backend_url`

On success return:

```json
{ "ready": true }
```

The connector must then keep the correct account-specific MT5 terminal instance running and use the issued device token for its authenticated terminal heartbeat, market-data and execution-report traffic. Use isolated terminal instances/processes per account; a single MetaTrader5 Python process must not be switched between different customers' accounts.

The connector must also provision the token for the MT5 Expert Advisor. The EA reads an account-specific state file from the terminal's Common\\Files directory. The exact filename is:

`LM_AUTO_TRADER_<account_login>_<server_sanitized>.dat`

For the server portion, replace spaces, slash, backslash and colon with underscores (matching `MakeStateFileName()` in `mt5/LM_AUTO_TRADER.mq5`). Write three newline-separated values: the device token, last-deal timestamp (initially `0`), and last-deal ticket (initially `0`). The EA polls for this file while waiting, so no one-time code or manual token copy is required.

## Database migration

Apply `sql/lm_auto_trader_password_connection.sql` after the existing MT5 schema migration. It adds the broker-name column and drops the obsolete one-time link-code table.

## Current completion boundary

The Android form and Vercel-side credential handoff are implemented in the feature branch. The Windows connector service itself is not yet installed/configured, so the backend returns a clear service-unavailable response until `LM_MT5_CONNECTOR_URL` and `LM_MT5_CONNECTOR_TOKEN` point to that service. Do not label an account connected until the connector returns successful authentication and activation.
