# LM Analyzer Web — parity build

This is the isolated browser client for the 27 November 2026 Android UI snapshot. It lives in `web/` so the existing Vercel API, Android app, and root website are not replaced by this work.

## Cloudflare Pages deployment

Create the Cloudflare Pages project only after confirming that the project name `lmanalyzer` is available. The desired hostname is `lmanalyzer.pages.dev`; Cloudflare may add a suffix if that name is already taken. Cloudflare documents that the `pages.dev` subdomain cannot be changed after the project is created, so choose the name carefully.

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

The first browser shell includes the six navigation tabs, current colour palette, home-screen order and rotating recommendation card, chart upload and analysis submission flow, analysis history filters, market quote categories, community feed with image posts/likes/comments, Tools hub, risk calculation, journal form, feedback form, Learning Hub lesson/quiz/progress flow, customer-side Support Bot chat with human-support request, plan display, and the three-stage High Impact News flow (events → related instruments → AI outlook).

This is **not yet complete production parity**. Items that still require implementation or end-to-end verification include the full Android Google sign-in experience, all market chart/watchlist/live-alert interactions, community profile/avatar/follow/follower features, support agent inbox and owner-side monitoring, browser push registration, full MT5 Auto Trader controls, and the final responsive visual review against current Android screenshots.

## Payment and platform differences

The Android app uses Google Play Billing. A normal website browser cannot launch Android's Play Billing flow. The web plans screen therefore shows the same price and allowance definitions but does not activate a subscription when a plan is selected. A web checkout needs an explicitly approved web-compatible payment provider, or users must complete the purchase in the Android app. The server must verify every purchase before granting paid access.

Android Firebase push notifications are not automatically delivered to browsers. Browser push needs a separate web-push subscription and server delivery path.

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
