# ShellStock

ShellStock is a lightweight Streamlit dashboard for browsing live stock charts and core company metrics.

## Features

- Live historical charts for 1 day, 1 week, 3 months, 6 months, YTD, 1 year, 5 years, and 10 years
- Candlestick chart with volume bars
- Quick valuation and quality metrics including trailing P/E, forward P/E, PEG ratio, dividend yield, margins, growth, ROE, and ROA
- Holdings manager with switchable stock view
- Per-holding level overlays: Must-Sell Point, Bought Point, Reasonable Lower Limit, and Reasonable Upper Limit
- Price alerts with in-app notifications
- Company profile and recent news links

## Setup

1. Install Python 3.11 or newer.
2. Create and activate a virtual environment.
3. Install dependencies:

```powershell
pip install -r requirements.txt
```

4. Start the app:

```powershell
streamlit run app.py
```

## Streamlit Cloud Persistent Storage (Supabase)

This app supports two storage backends:

- Local JSON fallback (for local development)
- Supabase Postgres with per-user isolation (recommended for Streamlit Cloud)

### 1) Create Supabase table

Run the SQL in [supabase_schema.sql](supabase_schema.sql) once in Supabase SQL Editor.

### 2) Add Streamlit Secrets

In Streamlit Cloud app settings, add:

```toml
SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"
SUPABASE_ANON_KEY = "YOUR_SUPABASE_ANON_KEY"
```

When these secrets are present, the app uses Supabase and requires user sign-in.

### 3) Authentication and data isolation

- Users sign in using Supabase Auth directly in the app.
- All saved data is stored in `shellstock_user_state` keyed by `user_id`.
- Row Level Security (RLS) policies ensure each user can only access their own rows.

### 4) Security notes

- Data in Supabase persists across Streamlit app restarts and redeploys.
- Do not commit secrets into the repository.
- In Supabase Auth, disable open signups if you want invite-only access.

## Notes

- Data is sourced from Yahoo Finance through the `yfinance` package.
- Some tickers expose more fundamental fields than others, so a few metrics may show as `N/A`.

## Alerts And In-App Notifications

- Alerts are evaluated when you click `Run alert scan now`.
- Use `Show test in-app alert` in the Alerts tab to validate notifications.
- Notifications appear inside the running app and are platform-independent.

## SSL Certificate Errors

If you see an error like `CERTIFICATE_VERIFY_FAILED`, your network may be using a corporate SSL inspection certificate.

- This can be worked around by disabling SSL verification for the app (not recommended for general use). Edit the app to set `st.session_state['allow_insecure_ssl'] = True` before loading data if you need this fallback.