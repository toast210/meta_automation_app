# meta_automation_app
i created this app for meta ads automation, this is very simple app, contributions will be appreciated 
# Meta Ads Optimizer (desktop app)

Standalone desktop app for Meta Ads (Facebook/Instagram), built to match the
structure of your Google Ads optimizer. Runs entirely on your machine.

## What it does

1. Pulls your ad sets and ads (budgets, status)
2. Pulls performance insights for your lookback window (spend, clicks,
   conversions, frequency)
3. Asks an LLM to recommend:
   - **Ads/ad sets to pause** - meaningful spend with zero conversions
   - **Budget increases/decreases** - scale up strong ad sets, pull back weak ones
   - **Creative fatigue alerts** - frequency above your threshold (informational,
     doesn't pause anything by itself)
4. Opens a **review window** with a checkbox per proposed change - check what
   you want, click "Apply Approved Changes"
5. Applies approved changes via the Meta Marketing API
6. Logs every action to `meta_optimizer_log.csv` next to the app

## 1. Get a Meta access token

You need a long-lived access token with `ads_management` and `ads_read`
permissions on the ad account(s) you want to manage. The simplest path:

1. Go to your Meta Business Settings -> **Business Settings > Users > System Users**
2. Create (or use) a System User, generate a token with `ads_management` +
   `ads_read` scopes, and assign it access to the ad account(s)
3. System User tokens can be set to never expire - this is the least
   maintenance option for a desktop app you run repeatedly

Alternatively, use Graph API Explorer to generate a token and exchange it for
a long-lived one (valid ~60 days, then needs refreshing in `config.json`).

Your ad account ID looks like `act_1234567890` - find it in Ads Manager
(Account Overview) or the URL when viewing the account.

## 2. Set up your config

Copy `config.example.json` to `config.json` (same folder) and fill in:

- `access_token` - your Meta access token
- `accounts` - one entry per ad account, with `ad_account_id` (include the
  `act_` prefix)
- `lookback_days` - performance window, matches the Google Ads app default of 7
- `min_spend_before_pause` - spend threshold (in your account's currency)
  before a zero-conversion ad/ad set gets flagged to pause
- `frequency_alert_threshold` - frequency at or above this triggers a
  creative fatigue alert (3.0 is a common starting point)
- `conversion_action_type` - which Meta "action_type" counts as a conversion
  (default `"purchase"` - change to `"lead"`, `"complete_registration"`, etc.
  if that's what you optimize for)
- `budget_increase_pct` / `budget_decrease_pct` - how aggressively to scale
  budgets up/down
- `llm.provider` - `"groq"` or `"openrouter"`, plus the matching API key

**Never commit or share `config.json`** - it holds a live access token.

## 3. Run it directly (test before packaging)

```bash
pip install -r requirements.txt
python meta_optimizer.py
```

It'll ask which account(s) to run in the terminal, fetch data, then open a
window per account listing every proposed change with a checkbox next to it.
Pause/budget changes default to checked; fatigue alerts default to unchecked
since they're informational. Uncheck anything you don't want, then click
**Apply Approved Changes** (or **Cancel** to apply nothing for that account).

## 4. Package it as a Windows .exe

On a Windows machine:

```bash
pip install -r requirements.txt
pyinstaller --onefile --name MetaAdsOptimizer meta_optimizer.py
```

`tkinter` ships with the standard python.org Windows installer, so PyInstaller
bundles it automatically - no extra steps needed for the UI to work in the exe.

This creates `dist/MetaAdsOptimizer.exe`. Copy `config.json` into the same
`dist` folder as the exe.

Double-click to run: a console window opens for account selection, then the
approval window pops up per account.

### Optional: give it a desktop shortcut
Right-click `dist/MetaAdsOptimizer.exe` -> **Send to -> Desktop (create shortcut)**.

## Notes

- Meta stores ad set budgets in **cents** (or the smallest currency unit) via
  the API. The app converts to/from whole currency units automatically so
  the numbers you see in the config and review window match what you'd type
  into Ads Manager.
- Insights are pulled at `level=adset` and `level=ad` using the Graph API's
  `time_range` parameter, not a preset like "last_7d" - so `lookback_days`
  in your config drives the exact window.
- If Meta's API version in `graph_api_version` gets deprecated, bump it in
  `config.json` (Meta typically supports each version for ~2 years).
- Fatigue alerts don't call the API at all when approved - they're just
  logged to the CSV as an acknowledged alert, since "refresh the creative"
  isn't something the Ads API can do for you.
