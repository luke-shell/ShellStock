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