# LM Analyzer — Human support-agent takeover

This is an additive upgrade for the existing LM Analyzer Android app and its existing Vercel `support_chat` endpoint. It reuses the existing customer conversation rows, so the AI and human agent work inside the same conversation.

## Included files

```text
android/MainActivity.kt                 Complete Android source based on the saved 17 Nov 2026 MainActivity
android/LMNotificationService.kt        Existing Firebase service extended with the Agent Inbox notification deep-link
api/support_chat.py                     Extended existing support-chat backend
sql/support_agent_handoff_migration.sql  Additive Supabase migration; preserves existing conversation/message rows
requirements-addition.txt               Optional backend dependency line if Firebase Admin is not already installed
```

## What this adds

- Customers can request a human agent from their existing LM SUPPORT BOT chat.
- Gemini can escalate when the customer asks for a person, an account/payment-specific issue requires staff access, or the model cannot confidently resolve the issue. If Gemini is unavailable, the request is still placed in the human queue.
- Human support requests stay in a persistent queue when agents are busy.
- Agents see the queue in the Agent Inbox inside LM Analyzer. The first agent to claim a waiting request owns it; a server-side PostgreSQL function makes the claim atomic.
- The same customer transcript is available to the agent. Agent messages are saved with their authenticated sender ID and display name.
- The Android app registers the agent's FCM device token after sign-in. Push notifications link into the Agent Inbox.
- Each agent defaults to one active conversation. The database allows an owner to configure 1–5 active conversations per agent.
- There is no hard cap in the database on the number of agent accounts. The SQL example shows 12 agents as a starting template; add more rows for a larger team.

## Important source-version warning

The included `android/MainActivity.kt` is based on the saved `MainActivity(20261117-171519).kt` snapshot. Before replacing your Android Studio file, compare it with your current local `MainActivity.kt`. **Do not overwrite a newer local file.** If it contains later changes, merge the support-only additions into that newer source instead. The patch is deliberately limited to the support chat, support agent inbox, and notification routing; it does not intentionally change analysis, subscriptions, billing, Community, or other features.

## Deployment order

### 0. Back up first

- Commit the current GitHub repository or download a ZIP backup.
- Copy your current Android `MainActivity.kt` and `LMNotificationService.kt` to a safe backup folder.
- Do not replace `api/user_security.py`, `api/auth.py`, analysis endpoints, subscription endpoints, Gradle files, or `vercel.json` as part of this upgrade.

### 1. Run the Supabase migration first

1. Open the same Supabase project referenced by the Vercel `SUPABASE_URL`.
2. Open **SQL Editor → New query**.
3. Run the complete `sql/support_agent_handoff_migration.sql` file once.
4. Confirm it finishes without errors. It adds lifecycle/assignment columns to `support_conversations`, sender metadata to `support_messages`, and creates `support_agents`, `support_agent_devices`, and the atomic claim function.
5. Existing support conversations and messages are retained. Existing rows default to `support_status='bot'`.

Do not deploy the changed backend before this migration: the new endpoint reads the columns and tables added by it.

### 2. Create the agent accounts

Each agent must first have their own normal LM Analyzer/Supabase Auth account. Create the accounts using your existing approved account-provisioning process. Do not share one login between agents.

Then edit the optional SQL template near the bottom of the migration file:

- Replace `agent01@example.com` etc. with the actual account emails.
- Replace the example names with the agents' real display names.
- Run that edited `WITH requested_agents ... INSERT` statement in Supabase SQL Editor.
- Add more rows whenever you recruit more agents. There is no fixed maximum.

Only users listed in `public.support_agents` are authorised to see the Agent Inbox or claim customer requests. Do not add ordinary customers to this table.

### 3. Merge the backend file into GitHub

Copy the included `api/support_chat.py` over the existing repository file at exactly:

```text
api/support_chat.py
```

This replaces only the support-chat endpoint, because it is the endpoint that must coordinate the human handoff. Keep all other existing API files unchanged.

The endpoint uses the existing `api.user_security` helpers and existing `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` server environment variables.

### 4. Firebase push configuration

The push sender uses the Firebase Admin SDK and the existing server-side service-account variable:

- `FIREBASE_SERVICE_ACCOUNT_JSON`, or
- `FIREBASE_SERVICE_ACCOUNT`.

Use the service-account JSON for the same Firebase project as the Android app. Keep it only in Vercel **Environment Variables**; never commit it to GitHub, put it in Kotlin, or send it in chat. The service account needs permission to send Firebase Cloud Messaging messages, and the Firebase Cloud Messaging API must be enabled for that project.

The code reuses Firebase Admin SDK, which is already used by the LM Analyzer push-notification code. Check the existing `requirements.txt`; only if `firebase-admin` is missing, merge the line in `requirements-addition.txt` into the existing file. Do not replace the whole requirements file.

### 5. Deploy the backend

1. Commit and push the changed `api/support_chat.py` (and the optional dependency line only if required) to the existing GitHub repository `plentyley5-blip/lamar-trading-bot`.
2. Confirm the Vercel deployment for that commit reports **Ready**.
3. Check Vercel Function Logs for `support_chat` errors. A missing-column/table error means the migration was not run against the Supabase project configured in Vercel.

### 6. Update the Android app

Use the included Android files only after checking the MainActivity source-version warning above:

```text
android/MainActivity.kt
    -> app/src/main/java/com/lamanalyzer/app/MainActivity.kt

android/LMNotificationService.kt
    -> app/src/main/java/com/lamanalyzer/app/LMNotificationService.kt
```

No new Android dependency is required. Keep the existing Firebase Messaging, Google Services, Credential Manager, Play Billing, and Compose configuration.

The existing manifest must retain:

- `android.permission.INTERNET`
- `android.permission.POST_NOTIFICATIONS`
- the existing `.LMNotificationService` service with the `com.google.firebase.MESSAGING_EVENT` intent filter.

Do not add duplicate permissions or service declarations. Build with **Build → Make Project** and install on a physical Android device.

### 7. Register and test every agent's phone

For each agent:

1. Install the updated LM Analyzer app.
2. Sign in using that agent's individual account.
3. Allow notifications when Android asks. On Android 13 or newer, notification permission must be granted.
4. Open **Profile → Live Chat → Agent Inbox** once. This also registers the agent's device token for the private support alert list.
5. Keep the app installed and the agent signed in. The app can receive high-priority data FCM alerts while in the background, subject to Android battery/network settings and notification permission.

### 8. Test before announcing it to customers

Use two or more agent test accounts and one customer test account:

1. Customer sends a message and taps **HUMAN**.
2. Confirm the original conversation changes to Waiting and an alert appears on registered, available agent phones.
3. Tap the alert; it should open the Agent Inbox.
4. Two agents attempt to claim the same request. Exactly one should succeed; the other receives an already-attended response / no longer sees it in the waiting queue.
5. Agent replies. Confirm the customer sees it in the original conversation and the agent's name appears below the reply.
6. Send another customer message while the agent owns the chat. Confirm Gemini does not reply over the human agent.
7. Resolve the request. Confirm the agent's slot becomes available and the next waiting request is alerted.
8. Set all test agents to their active limit and request human support again. Confirm the request remains queued and the bot tells the customer to wait.
9. Test an agent phone with notifications denied and one with app closed. Do not treat successful backend acceptance as proof that a particular handset displayed the notification.

## Limits and honest verification status

- This package has **not** been pushed to GitHub or deployed to Vercel from this session. The GitHub connection is not connected here.
- The backend file passes Python syntax compilation.
- A full Android Gradle build could not be run here because the complete Android Studio project / SDK and its Gradle configuration were not available. Run the build in your actual project before distributing the APK/AAB.
- Live FCM delivery can only be verified after the backend is deployed with Firebase credentials and agent devices have registered.
- The Agent Inbox refreshes its queue periodically; live customer-message updates currently poll the same conversation. This first version does not require a new realtime service or paid service.
