# LM Analyzer Web — parity build

This is the isolated browser client for the 27 November 2026 Android UI snapshot. It lives in `web/` so the existing Vercel API, Android app, and root website are not replaced by this work.

## Cloudflare Pages deployment

Create the Cloudflare Pages project only after confirming that the project name `lmanalyzer` is available. The desired hostname is `lmanalyzer.pages.dev`. During project creation, verify that Cloudflare assigns exactly `lmanalyzer.pages.dev` before completing setup. Cloudflare documents that a project's `pages.dev` subdomain cannot be changed after the project is created, so do not proceed with a different slug by mistake.

Connect this GitHub repository:

- Repository: `plentyley5-blip/lamar-trading-bot`
- Production branch for the initial review: `lm-analyzer-web-parity`
- Root directory: `web`
- Framework preset: `None`
- Build command: `exit 0`
- Build output directory: `.`

The site is plain static HTML, CSS and JavaScript. The public product/SEO page is available at `/about/`; the authenticated application is served from `/`. It uses the existing API at `https://lamar-trading-bot.vercel.app/api`. Do not add Supabase service-role keys, Gemini keys, Google Play service-account keys, MT5 passwords, or other secrets to this browser bundle.

After the branch is reviewed and the web flows are tested, change the Pages production branch to the approved release branch. Do not connect Cloudflare Pages to `main` yet.

## Android parity baseline

The intended source of truth is `MainActivity(20261127-191359).kt` from 27 November 2026.

Primary navigation, in order:

1. HOME
2. TRADE
3. COMMUNITY
4. MARKETS
5. TOOLS
6. PROFILE

Android quota rules carried into the web UI:

- Owner: unlimited access.
- Free: four lifetime analyses.
- Weekly: USD 9.23, five analyses per day.
- Monthly: USD 25.21, ten analyses per day.
- 3 Months: USD 63.69, thirty analyses per day.
- 6 Months: USD 127.38, fifty analyses per day.
- High Impact News, related instruments, and AI news outlooks: owner or active paid subscriber only.
- Backend checks remain authoritative; front-end checks are display/navigation gates only.

## Current implementation status

The browser build includes the six navigation tabs, Blue Glass theme, Android-style home order and rotating recommendation card with current High Impact News events, chart upload and AI analysis submission, detailed Trade history, live market quotes/alerts/candlestick charts, community image posts/likes/comments/followers, profile avatars and verified status, grouped Profile and Tools menus, Learning Hub quizzes/progress, Smart Risk Manager, Journal, Feedback, customer Support Bot with human escalation, owner support monitoring, account deletion, exact target plan list, and the three-stage High Impact News flow (events → related instruments → AI outlook).

This is **not yet complete production parity**. Items that still require implementation or end-to-end verification include setting the browser Google OAuth client ID and authorizing the final pages.dev origin, market watchlist management and notification preferences, native-equivalent push registration and delivery, support agent inbox actions, browser payment/verification if web checkout is desired, full MT5 Auto Trader controls, and the final responsive visual review against current Android screenshots.

## Payment and platform differences

The web Google sign-in uses the existing server-side `/api/google_login` exchange. Set the public `googleClientId` value in `web/config.js` and add the final `https://<project>.pages.dev` hostname to the Google OAuth client's Authorized JavaScript origins before enabling it. Do not place an OAuth client secret in `config.js`.

The Android app uses Google Play Billing. A normal website browser cannot launch Android's Play Billing flow. The web plans screen therefore shows the same price and allowance definitions but does not activate a subscription when a plan is selected. A web checkout needs an explicitly approved web-compatible payment provider, or users must complete the purchase in the Android app. The server must verify every purchase before granting paid access.

Android Firebase push notifications are not automatically delivered to browsers. Browser push needs a separate web-push subscription and server delivery path. The current web Settings screen only requests browser notification permission and can send a local test notification.

## MT5 backend contract mismatch to resolve

The latest Android Auto Trader screen calls the `/api/mt5_trader` endpoint using `dashboard` and `connect_credentials` actions. The current repository handler supports `create_link_code`, `link`, `revoke_all`, `configure`, `claim`, `report`, and `deal`, but does not currently implement `dashboard` or `connect_credentials`. Do not collect or transmit an MT5 account password from the browser until the backend contract, credential handling, encryption, and session behavior are reconciled and reviewed.

## Release checklist

- Compare every web screen with the current Android screen at phone and laptop widths.
- Complete the missing screens and interactions listed above.
- Verify the actual API contracts and authorization rules against staging data.
- Test owner, free-lifetime, active-paid, and expired-subscription accounts.
- Test High Impact News gating on both UI and server.
- Test that no browser bundle contains secrets or trading credentials.
- Confirm the Cloudflare project slug before first creation; after the final hostname is known, add the absolute XML sitemap URL and canonical metadata.
- Deploy to a preview first; only then move the approved branch to production.
