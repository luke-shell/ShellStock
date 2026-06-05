from __future__ import annotations

from datetime import date, datetime
import time
import json
import os
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import pytz
import requests
import streamlit as st
import yfinance as yf
from plotly.subplots import make_subplots

try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

try:
    from supabase import Client, create_client
except Exception:
    Client = Any
    create_client = None

# Persistence file path
PERSISTENCE_FILE = os.path.expanduser("~/.shellstock_data.json")


st.set_page_config(
    page_title="ShellStock",
    page_icon="SS",
    layout="wide",
)


INTERVAL_OPTIONS = {
    "1 Day": {"period": "1d", "interval": "5m"},
    "1 Week": {"period": "7d", "interval": "1h"},
    "1 Month": {"period": "1mo", "interval": "1d"},
    "3 Months": {"period": "3mo", "interval": "1d"},
    "6 Months": {"period": "6mo", "interval": "1d"},
    "YTD": {"period": None, "interval": "1d"},
    "1 Year": {"period": "1y", "interval": "1d"},
    "5 Years": {"period": "5y", "interval": "1wk"},
    "10 Years": {"period": "10y", "interval": "1mo"},
}

SYMBOL_FETCH_COOLDOWN_SECONDS = 45
QUOTE_FETCH_COOLDOWN_SECONDS = 20
PROVIDER_COOLDOWN_SECONDS = 90

RANGE_FETCH_TTL_SECONDS = {
    "1 Day": 30,
    "1 Week": 60,
    "1 Month": 180,
    "3 Months": 300,
    "6 Months": 420,
    "YTD": 420,
    "1 Year": 600,
    "5 Years": 900,
    "10 Years": 1200,
}


def get_range_fetch_ttl_seconds(range_label: str) -> int:
    return int(RANGE_FETCH_TTL_SECONDS.get(range_label, SYMBOL_FETCH_COOLDOWN_SECONDS))


def get_provider_cooldown_remaining_seconds() -> int:
    until = float(st.session_state.get("provider_cooldown_until", 0.0) or 0.0)
    remaining = int(until - time.time())
    return max(0, remaining)


def activate_provider_cooldown(seconds: int = PROVIDER_COOLDOWN_SECONDS) -> None:
    st.session_state.provider_cooldown_until = time.time() + max(1, int(seconds))


def _supabase_configured() -> bool:
    if create_client is None:
        return False

    try:
        secrets = st.secrets
        url = secrets.get("SUPABASE_URL")
        key = secrets.get("SUPABASE_ANON_KEY")
        return bool(url and key)
    except Exception:
        return False


def _get_supabase_client() -> Any:
    if not _supabase_configured() or create_client is None:
        return None

    try:
        return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_ANON_KEY"])
    except Exception:
        return None


def _get_auth_redirect_url() -> str | None:
    """Optional redirect URL used by Supabase email links (signup/recovery)."""
    try:
        redirect = str(st.secrets.get("AUTH_REDIRECT_URL", "")).strip()
        return redirect or None
    except Exception:
        return None


def _save_auth_session(auth_response: Any) -> bool:
    session = getattr(auth_response, "session", None)
    user = getattr(auth_response, "user", None)
    if session is None or user is None:
        return False

    access_token = getattr(session, "access_token", None)
    refresh_token = getattr(session, "refresh_token", None)
    user_id = getattr(user, "id", None)
    user_email = getattr(user, "email", "")

    if not (access_token and refresh_token and user_id):
        return False

    st.session_state.supabase_access_token = access_token
    st.session_state.supabase_refresh_token = refresh_token
    st.session_state.supabase_user_id = user_id
    st.session_state.supabase_user_email = user_email
    return True


def _clear_auth_session() -> None:
    st.session_state.pop("supabase_access_token", None)
    st.session_state.pop("supabase_refresh_token", None)
    st.session_state.pop("supabase_user_id", None)
    st.session_state.pop("supabase_user_email", None)


def _supabase_user_id() -> str | None:
    user_id = st.session_state.get("supabase_user_id")
    return str(user_id) if user_id else None


def _get_authenticated_supabase_client() -> Any:
    client = _get_supabase_client()
    if client is None:
        return None

    access_token = st.session_state.get("supabase_access_token")
    refresh_token = st.session_state.get("supabase_refresh_token")
    if not (access_token and refresh_token):
        return None

    try:
        client.auth.set_session(access_token, refresh_token)
        return client
    except Exception:
        return None


def _sign_in_supabase(email: str, password: str) -> tuple[bool, str]:
    client = _get_supabase_client()
    if client is None:
        return False, "Supabase client is not configured."

    try:
        response = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as error:
        return False, str(error)

    if _save_auth_session(response):
        return True, "Signed in successfully."
    return False, "Sign in failed. Check your email/password."


def _sign_up_supabase(email: str, password: str) -> tuple[bool, str]:
    client = _get_supabase_client()
    if client is None:
        return False, "Supabase client is not configured."

    payload: dict[str, Any] = {"email": email, "password": password}
    redirect_url = _get_auth_redirect_url()
    if redirect_url:
        payload["options"] = {"email_redirect_to": redirect_url}

    try:
        response = client.auth.sign_up(payload)
    except Exception as error:
        return False, str(error)

    if _save_auth_session(response):
        return True, "Account created and signed in."
    return True, "Account created. Check your email for confirmation, then sign in."


def _send_password_reset_email(email: str) -> tuple[bool, str]:
    client = _get_supabase_client()
    if client is None:
        return False, "Supabase client is not configured."

    redirect_url = _get_auth_redirect_url()

    try:
        if redirect_url:
            try:
                client.auth.reset_password_email(email, {"redirect_to": redirect_url})
            except TypeError:
                client.auth.reset_password_email(email)
        else:
            client.auth.reset_password_email(email)
        return True, "Password reset email sent. Use the link in your inbox."
    except Exception as error:
        return False, str(error)


def _update_password_supabase(new_password: str) -> tuple[bool, str]:
    client = _get_authenticated_supabase_client()
    if client is None:
        return False, "No active recovery session. Open the latest reset link again."

    try:
        client.auth.update_user({"password": new_password})
        st.session_state.supabase_recovery_mode = False
        return True, "Password updated successfully."
    except Exception as error:
        return False, str(error)


def _clear_auth_query_params() -> None:
    try:
        st.query_params.clear()
    except Exception:
        pass


def _handle_supabase_auth_callback() -> None:
    """Process Supabase email-link callbacks for signup confirmation and recovery."""
    if not _supabase_configured():
        return

    try:
        query_params = st.query_params
        token_hash = query_params.get("token_hash")
        callback_type = query_params.get("type")
    except Exception:
        return

    if not token_hash or callback_type not in ("signup", "recovery"):
        return

    client = _get_supabase_client()
    if client is None:
        return

    try:
        response = client.auth.verify_otp({"type": callback_type, "token_hash": token_hash})
        if callback_type == "signup":
            if _save_auth_session(response):
                st.session_state.auth_callback_message = ("success", "Email confirmed and signed in.")
            else:
                st.session_state.auth_callback_message = ("success", "Email confirmed. You can now sign in.")
        else:
            _save_auth_session(response)
            st.session_state.supabase_recovery_mode = True
            st.session_state.auth_callback_message = (
                "success",
                "Recovery link verified. Set your new password below.",
            )
    except Exception as error:
        st.session_state.auth_callback_message = (
            "error",
            f"Auth link handling failed: {error}",
        )

    _clear_auth_query_params()
    st.rerun()


def _load_from_supabase() -> dict[str, Any]:
    user_id = _supabase_user_id()
    client = _get_authenticated_supabase_client()
    if client is None:
        return {}
    if not user_id:
        return {}

    try:
        response = (
            client.table("shellstock_user_state")
            .select("data_key,data_value")
            .eq("user_id", user_id)
            .execute()
        )
        rows = response.data or []
        return {row.get("data_key"): row.get("data_value") for row in rows if row.get("data_key")}
    except Exception:
        return {}


def _save_to_supabase(data: dict[str, Any]) -> bool:
    user_id = _supabase_user_id()
    client = _get_authenticated_supabase_client()
    if client is None:
        return False
    if not user_id:
        return False

    try:
        payload = [{"user_id": user_id, "data_key": key, "data_value": value} for key, value in data.items()]
        if payload:
            client.table("shellstock_user_state").upsert(payload, on_conflict="user_id,data_key").execute()
        return True
    except Exception:
        return False


def apply_custom_theme() -> None:
    st.markdown(
        """
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&display=swap');

            :root {
                color-scheme: light dark;
                --shell-bg: #f6f8fc;
                --shell-sidebar-bg: #e9effa;
                --shell-card-bg: #ffffff;
                --shell-input-bg: #ffffff;
                --shell-border: rgba(12, 25, 49, 0.12);
                --shell-text: #101826;
                --shell-muted: #5b6578;
                --shell-accent: #0099b8;
                --shell-accent-soft: rgba(0, 153, 184, 0.12);
                --shell-banner-border: rgba(0, 153, 184, 0.30);
                --shell-banner-bg: linear-gradient(115deg, rgba(0, 153, 184, 0.14), rgba(255, 177, 64, 0.15));
                --shell-shadow: 0 10px 25px rgba(20, 31, 56, 0.10);
            }

            @media (prefers-color-scheme: dark) {
                :root {
                    --shell-bg: #1a1a2e;
                    --shell-sidebar-bg: #0f3460;
                    --shell-card-bg: #16213e;
                    --shell-input-bg: #16213e;
                    --shell-border: rgba(255, 255, 255, 0.15);
                    --shell-text: #ffffff;
                    --shell-muted: #b0b0b0;
                    --shell-accent: #00d9ff;
                    --shell-accent-soft: rgba(0, 217, 255, 0.16);
                    --shell-banner-border: rgba(0, 217, 255, 0.25);
                    --shell-banner-bg: linear-gradient(115deg, rgba(0, 217, 255, 0.16), rgba(255, 185, 58, 0.10));
                    --shell-shadow: 0 10px 25px rgba(0, 0, 0, 0.3);
                }
            }

            html, body, [class*="css"]  {
                font-family: 'Space Grotesk', sans-serif;
                color: var(--shell-text);
            }

            p, li, label, span, div {
                color: var(--shell-text);
            }

            .stApp {
                background: var(--shell-bg);
            }

            .block-container {
                padding-top: 1.3rem;
                padding-bottom: 2rem;
            }

            section[data-testid="stSidebar"] {
                background: var(--shell-sidebar-bg);
                border-right: 1px solid var(--shell-border);
            }

            div[data-testid="stMetric"] {
                background: var(--shell-card-bg);
                border: 1px solid var(--shell-border);
                border-radius: 14px;
                padding: 0.85rem 1rem;
                box-shadow: var(--shell-shadow);
                backdrop-filter: blur(6px);
            }

            .stTabs [data-baseweb="tab-list"] {
                gap: 0.5rem;
            }

            .stTabs [data-baseweb="tab"] {
                background: color-mix(in srgb, var(--shell-card-bg) 84%, transparent);
                border: 1px solid var(--shell-border);
                border-radius: 10px;
                color: var(--shell-muted);
                padding: 0.4rem 0.9rem;
            }

            .stTabs [aria-selected="true"] {
                background: var(--shell-accent);
                color: #041017;
            }

            /* Make metric numeric values and deltas smaller for tighter layouts */
            div[data-testid="stMetric"] [data-testid="stMetricValue"] {
                font-size: 0.95rem !important;
                line-height: 1 !important;
            }
            div[data-testid="stMetric"] [data-testid="stMetricDelta"] {
                font-size: 0.8rem !important;
            }

            div[data-testid="stDataFrame"] {
                border: 1px solid var(--shell-border);
                border-radius: 12px;
                overflow: hidden;
            }

            .stButton button, .stDownloadButton button {
                border-radius: 999px;
                border: 1px solid var(--shell-accent);
                padding: 0.35rem 1rem;
                font-weight: 600;
                color: #041017;
                background: var(--shell-accent);
            }

            .stTextInput input, .stTextArea textarea, .stNumberInput input, .stSelectbox div[data-baseweb="select"] > div {
                background: var(--shell-input-bg);
                color: var(--shell-text);
                border: 1px solid var(--shell-border);
            }

            .stRadio label, .stCheckbox label, .stCaption, [data-testid="stMarkdownContainer"] p {
                color: var(--shell-text);
            }

            .shell-banner {
                border: 1px solid var(--shell-banner-border);
                background: var(--shell-banner-bg);
                border-radius: 18px;
                padding: 1rem 1.1rem;
                margin-bottom: 0.9rem;
                box-shadow: var(--shell-shadow);
            }

            .shell-subtle {
                color: var(--shell-muted);
                margin-top: 0.2rem;
            }

            /* Keep Plotly labels/ticks legible in both system modes */
            .js-plotly-plot .main-svg text,
            .js-plotly-plot .legend text,
            .js-plotly-plot .gtitle,
            .js-plotly-plot .xtick text,
            .js-plotly-plot .ytick text {
                fill: var(--shell-text) !important;
            }

            .js-plotly-plot .xgrid path,
            .js-plotly-plot .ygrid path {
                stroke: color-mix(in srgb, var(--shell-text) 18%, transparent) !important;
            }

            /* Keep yellow annotation labels readable in all themes. */
            .js-plotly-plot .annotation-text,
            .js-plotly-plot .annotation text,
            .js-plotly-plot g.annotation text,
            .js-plotly-plot g.annotation text tspan {
                fill: #000000 !important;
            }

            @media (max-width: 900px) {
                .block-container {
                    padding-top: 0.7rem;
                }
                .shell-banner {
                    border-radius: 14px;
                    padding: 0.9rem;
                }
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def load_persistent_data() -> dict[str, Any]:
    """Load data from persistent storage file."""
    if _supabase_configured():
        return _load_from_supabase()

    if os.path.exists(PERSISTENCE_FILE):
        try:
            with open(PERSISTENCE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_persistent_data(data: dict[str, Any]) -> None:
    """Save data to persistent storage file."""
    if _supabase_configured():
        _save_to_supabase(data)
        return

    try:
        with open(PERSISTENCE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def get_persistent_data_key(key: str, default: Any = None) -> Any:
    """Get a specific key from persistent data."""
    data = load_persistent_data()
    return data.get(key, default)


def set_persistent_data_key(key: str, value: Any) -> None:
    """Set a specific key in persistent data."""
    data = load_persistent_data()
    data[key] = value
    save_persistent_data(data)


def initialize_state() -> None:
    if "holdings" not in st.session_state:
        persisted_holdings = get_persistent_data_key("holdings", {})
        if not persisted_holdings:
            persisted_holdings = {
                "AAPL": {
                    "must_sell": None,
                    "bought_point": None,
                    "reasonable_lower": None,
                    "reasonable_upper": None,
                    "broker": "RBC",
                    "quantity": None,
                    "purchase_price_usd": None,
                    "purchase_price_cad": None,
                }
            }
        else:
            # Migrate any legacy `purchase_price` fields into `purchase_price_usd` for compatibility
            for sym, h in list(persisted_holdings.items()):
                if isinstance(h, dict):
                    if "purchase_price" in h and "purchase_price_usd" not in h:
                        h["purchase_price_usd"] = h.get("purchase_price")
                        h.pop("purchase_price", None)
                    # Ensure both keys exist
                    h.setdefault("purchase_price_usd", None)
                    h.setdefault("purchase_price_cad", None)
        st.session_state.holdings = persisted_holdings
    
    if "selected_holding" not in st.session_state:
        st.session_state.selected_holding = get_persistent_data_key("selected_holding", "AAPL")
    
    if "alerts" not in st.session_state:
        st.session_state.alerts = get_persistent_data_key("alerts", [])
    
    if "currency_mode" not in st.session_state:
        st.session_state.currency_mode = get_persistent_data_key("currency_mode", "CAD")

    if "selected_watchlist_symbol" not in st.session_state:
        st.session_state.selected_watchlist_symbol = ""

    # Initialize watchlist (persisted as list of strings or list of dicts)
    if "watchlist" not in st.session_state:
        persisted_watch = get_persistent_data_key("watchlist", [])
        normalized: list[dict[str, str]] = []
        for item in persisted_watch:
            if isinstance(item, str):
                normalized.append({"symbol": item.upper(), "note": ""})
            elif isinstance(item, dict):
                normalized.append({"symbol": (item.get("symbol") or "").upper(), "note": item.get("note", "")})
        st.session_state.watchlist = normalized

    if "master_note" not in st.session_state:
        st.session_state.master_note = get_persistent_data_key("master_note", "")

    if "master_note_saved_at" not in st.session_state:
        st.session_state.master_note_saved_at = get_persistent_data_key("master_note_saved_at", "")

    if "symbol_notes" not in st.session_state:
        persisted_symbol_notes = get_persistent_data_key("symbol_notes", {})
        if not isinstance(persisted_symbol_notes, dict):
            persisted_symbol_notes = {}

        # Migrate legacy watchlist note fields into symbol-scoped notes.
        for item in st.session_state.watchlist:
            symbol = (item.get("symbol") or "").upper()
            note_text = (item.get("note") or "").strip()
            if symbol and note_text and symbol not in persisted_symbol_notes:
                persisted_symbol_notes[symbol] = note_text

        st.session_state.symbol_notes = persisted_symbol_notes

    if "symbol_note_saved_at" not in st.session_state:
        persisted_note_saved_at = get_persistent_data_key("symbol_note_saved_at", {})
        if not isinstance(persisted_note_saved_at, dict):
            persisted_note_saved_at = {}
        st.session_state.symbol_note_saved_at = persisted_note_saved_at

    if "notification_history" not in st.session_state:
        persisted_notification_history = get_persistent_data_key("notification_history", [])
        if not isinstance(persisted_notification_history, list):
            persisted_notification_history = []
        st.session_state.notification_history = persisted_notification_history


def safe_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value)
    return None


def get_current_time_display() -> str:
    """Get current time in Eastern and PST timezones."""
    eastern = pytz.timezone('US/Eastern')
    pacific = pytz.timezone('US/Pacific')
    
    now_eastern = datetime.now(eastern)
    now_pacific = datetime.now(pacific)
    
    eastern_str = now_eastern.strftime('%I:%M %p %Z')
    pacific_str = now_pacific.strftime('%I:%M %p %Z')
    
    return f"{eastern_str} | {pacific_str}"


@st.cache_data(ttl=60, show_spinner=False)
def get_usd_cad_rate() -> float:
    """Get real-time USD to CAD exchange rate."""
    info = get_usd_cad_rate_info()
    return info.get("rate", 1.35)


@st.cache_data(ttl=60, show_spinner=False)
def get_usd_cad_rate_info() -> dict:
    """Return USD->CAD rate and timestamp from Yahoo Finance."""
    try:
        ticker = yf.Ticker("CAD=X")
        # Try intraday history first
        hist = ticker.history(period="1d", interval="1m", auto_adjust=False)
        if hist is not None and not hist.empty:
            last_idx = hist.index[-1]
            last_close = float(hist.iloc[-1]["Close"])
            # Format timestamp
            try:
                ts_str = last_idx.strftime("%Y-%m-%d %H:%M %Z") if hasattr(last_idx, "tz") and last_idx.tz is not None else last_idx.strftime("%Y-%m-%d %H:%M")
            except Exception:
                ts_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            result = {"rate": last_close, "timestamp": ts_str}
            set_persistent_data_key("last_fx_info", result)
            return result

        # Fallback to ticker.info
        info = ticker.info or {}
        current_rate = info.get("currentPrice") or info.get("regularMarketPrice")
        if current_rate:
            result = {"rate": float(current_rate), "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M")}
            set_persistent_data_key("last_fx_info", result)
            return result
    except Exception:
        pass

    # Fallback to last successful FX quote if Yahoo is rate-limited/unavailable.
    cached_fx = get_persistent_data_key("last_fx_info", None)
    if isinstance(cached_fx, dict) and "rate" in cached_fx:
        return cached_fx

    return {"rate": 1.35, "timestamp": "N/A"}


def is_rate_limited_error(error: Any) -> bool:
    text = str(error).lower()
    return "too many requests" in text or "rate limit" in text or "429" in text


def get_transaction_fee(broker: str) -> float:
    """Get transaction fee percentage for each broker.

    Returns structured fee data so callers can support percent or flat fees.
    """
    # Return structured fee data so callers can support percent or flat fees
    if broker == "WealthSimple":
        return {"type": "percent", "value": 0.015}
    elif broker == "RBC":
        # RBC charges a flat CAD fee of $9.95
        return {"type": "flat", "value": 9.95, "currency": "CAD"}
    return {"type": "percent", "value": 0.0}


def calculate_estimated_sale_price(current_price: float | None, quantity: float | None, broker: str, fx_rate: float = 1.0, display_currency: str = "USD") -> dict[str, Any]:
    """Calculate estimated sale price considering transaction fees."""
    if current_price is None or quantity is None:
        return {
            "current_total": None,
            "fee_amount": None,
            "estimated_proceeds": None,
            "fee_type": None,
            "fee_value": None,
            "fee_currency": None,
            "fee_percent": None,
        }

    fee_info = get_transaction_fee(broker)
    current_total = current_price * quantity

    fee_amount_usd = 0.0
    fee_percent = None
    fee_type = fee_info.get("type")
    fee_value = fee_info.get("value")
    fee_currency = fee_info.get("currency", "USD")

    if fee_type == "percent":
        fee_percent = fee_value
        fee_amount_usd = current_total * fee_value
    elif fee_type == "flat":
        # Flat fee provided in fee_currency (e.g., CAD). Convert to USD for arithmetic using fx_rate (USD->CAD).
        if fee_currency == "CAD":
            # fx_rate is USD -> CAD (1 USD = fx_rate CAD). To convert CAD -> USD: usd = cad / fx_rate
            fee_amount_usd = (fee_value / fx_rate) if fx_rate and fx_rate > 0 else fee_value
        else:
            fee_amount_usd = fee_value

    estimated_proceeds = current_total - fee_amount_usd

    return {
        "current_total": current_total,
        "fee_amount": fee_amount_usd,
        "estimated_proceeds": estimated_proceeds,
        "fee_type": fee_type,
        "fee_value": fee_value,
        "fee_currency": fee_currency,
        "fee_percent": fee_percent,
    }


def pick_value(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, "", "N/A"):
            return value
    return None


def format_currency(value: Any, currency: str = "USD") -> str:
    number = safe_number(value)
    if number is None:
        return "N/A"
    magnitude = abs(number)
    currency_symbol = "$"
    currency_label = f" {currency}"
    
    if magnitude >= 1_000_000_000_000:
        return f"{currency_symbol}{number / 1_000_000_000_000:.2f}T{currency_label}"
    if magnitude >= 1_000_000_000:
        return f"{currency_symbol}{number / 1_000_000_000:.2f}B{currency_label}"
    if magnitude >= 1_000_000:
        return f"{currency_symbol}{number / 1_000_000:.2f}M{currency_label}"
    return f"{currency_symbol}{number:,.2f}{currency_label}"


def format_ratio(value: Any) -> str:
    number = safe_number(value)
    if number is None:
        return "N/A"
    return f"{number:.2f}"


def format_percent(value: Any) -> str:
    number = safe_number(value)
    if number is None:
        return "N/A"
    return f"{number * 100:.2f}%"


def format_large_number(value: Any) -> str:
    number = safe_number(value)
    if number is None:
        return "N/A"
    return f"{number:,.0f}"


def sanitize_symbol(value: str) -> str:
    return value.strip().upper()


def parse_optional_price(value: str) -> float | None:
    raw = value.strip()
    if not raw:
        return None
    return float(raw)


def get_holding(symbol: str) -> dict[str, float | None]:
    holdings: dict[str, dict[str, float | None]] = st.session_state.holdings
    if symbol not in holdings:
        holdings[symbol] = {
            "must_sell": None,
            "purchase_price_usd": None,
            "purchase_price_cad": None,
            "reasonable_lower": None,
            "reasonable_upper": None,
            "broker": "RBC",
            "quantity": None,
        }
    return holdings[symbol]


def create_insecure_session() -> Any:
    if curl_requests is not None:
        session = curl_requests.Session()
        session.verify = False
        return session
    session = requests.Session()
    session.verify = False
    return session


@st.cache_data(show_spinner=False, ttl=900)
def load_stock_bundle(
    symbol: str,
    range_label: str,
    allow_insecure_ssl: bool,
    force_reload: float | None = None,
) -> tuple[pd.DataFrame, dict[str, Any], list[dict[str, Any]]]:
    session = None
    if allow_insecure_ssl:
        session = create_insecure_session()

    ticker = yf.Ticker(symbol, session=session)
    interval_config = INTERVAL_OPTIONS[range_label]
    period = interval_config["period"]
    interval = interval_config["interval"]

    if range_label == "YTD":
        start_date = date(date.today().year, 1, 1)
        history = ticker.history(start=start_date, interval=interval, auto_adjust=False)
    else:
        history = ticker.history(period=period, interval=interval, auto_adjust=False)

    if history.empty:
        raise ValueError("No historical price data was returned for this ticker and time range.")

    info = ticker.info or {}
    news_items = ticker.news or []
    return history.reset_index(), info, news_items[:8]


def load_stock_bundle_resilient(
    symbol: str,
    range_label: str,
    allow_insecure_ssl: bool,
    force_reload: float | None = None,
) -> tuple[pd.DataFrame, dict[str, Any], list[dict[str, Any]], str | None]:
    """Load bundle with retries and stale fallback when provider rate-limits requests."""
    cache_key = f"{symbol.upper()}::{range_label}"
    if "last_good_stock_bundles" not in st.session_state:
        st.session_state.last_good_stock_bundles = {}

    if "symbol_fetch_cache" not in st.session_state:
        st.session_state.symbol_fetch_cache = {}

    ttl_seconds = get_range_fetch_ttl_seconds(range_label)
    provider_cooldown_remaining = get_provider_cooldown_remaining_seconds()
    if provider_cooldown_remaining > 0 and force_reload is None:
        cached = st.session_state.last_good_stock_bundles.get(cache_key)
        if cached and isinstance(cached, dict):
            cached_history = pd.DataFrame(cached.get("history", {}))
            if not cached_history.empty:
                message = (
                    f"Data provider cooldown active ({provider_cooldown_remaining}s remaining). "
                    f"Showing cached {symbol.upper()} data from {cached.get('saved_at', 'an earlier time')}."
                )
                return cached_history, cached.get("info", {}), cached.get("news", []), message
        raise ValueError(
            f"Data provider cooldown active for {provider_cooldown_remaining}s after rate limiting."
        )

    cooldown_key = f"{symbol.upper()}::{range_label}::{int(bool(allow_insecure_ssl))}"
    now_ts = time.time()
    cooldown_entry = st.session_state.symbol_fetch_cache.get(cooldown_key)
    if force_reload is None and isinstance(cooldown_entry, dict):
        fetched_at = float(cooldown_entry.get("fetched_at", 0.0) or 0.0)
        if (now_ts - fetched_at) < ttl_seconds:
            cooldown_bundle = cooldown_entry.get("bundle")
            if isinstance(cooldown_bundle, tuple) and len(cooldown_bundle) == 4:
                run_history, run_info, run_news, run_note = cooldown_bundle
                if isinstance(run_history, pd.DataFrame):
                    return run_history.copy(), run_info, run_news, run_note

    if "request_cycle_bundle_cache" not in st.session_state:
        st.session_state.request_cycle_bundle_cache = {}

    refresh_marker = None
    if force_reload is not None:
        try:
            refresh_marker = round(float(force_reload), 3)
        except Exception:
            refresh_marker = str(force_reload)

    request_cache_key = f"{symbol.upper()}::{range_label}::{int(bool(allow_insecure_ssl))}::{refresh_marker}"
    request_cycle_cache: dict[str, Any] = st.session_state.request_cycle_bundle_cache
    cached_for_run = request_cycle_cache.get(request_cache_key)
    if isinstance(cached_for_run, tuple) and len(cached_for_run) == 4:
        run_history, run_info, run_news, run_note = cached_for_run
        if isinstance(run_history, pd.DataFrame):
            return run_history.copy(), run_info, run_news, run_note

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            bundle = load_stock_bundle(symbol, range_label, allow_insecure_ssl, force_reload=force_reload)
            st.session_state.last_good_stock_bundles[cache_key] = {
                "history": bundle[0].to_dict(orient="list"),
                "info": bundle[1],
                "news": bundle[2],
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            result = (bundle[0], bundle[1], bundle[2], None)
            request_cycle_cache[request_cache_key] = result
            st.session_state.symbol_fetch_cache[cooldown_key] = {
                "fetched_at": now_ts,
                "bundle": result,
            }
            return result
        except Exception as error:
            last_error = error
            if is_rate_limited_error(error) and attempt < 2:
                activate_provider_cooldown()
                time.sleep(1.2 + attempt)
                continue
            break

    cached = st.session_state.last_good_stock_bundles.get(cache_key)
    if cached and isinstance(cached, dict):
        cached_history = pd.DataFrame(cached.get("history", {}))
        if not cached_history.empty:
            message = (
                f"Rate limited by data provider. Showing cached {symbol.upper()} data "
                f"from {cached.get('saved_at', 'an earlier time')}."
            )
            result = (cached_history, cached.get("info", {}), cached.get("news", []), message)
            request_cycle_cache[request_cache_key] = result
            st.session_state.symbol_fetch_cache[cooldown_key] = {
                "fetched_at": now_ts,
                "bundle": result,
            }
            return result

    if last_error is not None:
        if is_rate_limited_error(last_error):
            activate_provider_cooldown()
        raise last_error
    raise ValueError("Unable to load stock data.")


def load_stock_quote(
    symbol: str,
    allow_insecure_ssl: bool,
    force_reload: float | None = None,
) -> dict[str, Any]:
    session = None
    if allow_insecure_ssl:
        session = create_insecure_session()

    ticker = yf.Ticker(symbol, session=session)

    price = None
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        fast_info = ticker.fast_info or {}
        fast_price = fast_info.get("lastPrice") or fast_info.get("regularMarketPrice")
        if fast_price not in (None, ""):
            price = float(fast_price)
    except Exception:
        pass

    if price is None:
        hist = ticker.history(period="1d", interval="1m", auto_adjust=False)
        if hist is not None and not hist.empty:
            price = float(hist.iloc[-1]["Close"])
            last_idx = hist.index[-1]
            try:
                timestamp = (
                    last_idx.strftime("%Y-%m-%d %H:%M %Z")
                    if hasattr(last_idx, "tz") and last_idx.tz is not None
                    else last_idx.strftime("%Y-%m-%d %H:%M")
                )
            except Exception:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if price is None:
        info = ticker.info or {}
        fallback_price = info.get("currentPrice") or info.get("regularMarketPrice")
        if fallback_price not in (None, ""):
            price = float(fallback_price)

    if price is None:
        raise ValueError("No quote data was returned for this ticker.")

    return {
        "symbol": symbol.upper(),
        "price": price,
        "timestamp": timestamp,
    }


def load_stock_quote_resilient(
    symbol: str,
    allow_insecure_ssl: bool,
    force_reload: float | None = None,
) -> tuple[dict[str, Any], str | None]:
    if "quote_fetch_cache" not in st.session_state:
        st.session_state.quote_fetch_cache = {}
    if "last_good_quotes" not in st.session_state:
        st.session_state.last_good_quotes = {}

    symbol_key = symbol.upper()
    cooldown_key = f"{symbol_key}::{int(bool(allow_insecure_ssl))}"
    now_ts = time.time()

    provider_cooldown_remaining = get_provider_cooldown_remaining_seconds()
    if provider_cooldown_remaining > 0 and force_reload is None:
        cached_quote = st.session_state.last_good_quotes.get(symbol_key)
        if isinstance(cached_quote, dict) and safe_number(cached_quote.get("price")) is not None:
            message = (
                f"Data provider cooldown active ({provider_cooldown_remaining}s remaining). "
                f"Showing cached quote for {symbol_key}."
            )
            return cached_quote, message
        raise ValueError(
            f"Data provider cooldown active for {provider_cooldown_remaining}s after rate limiting."
        )

    cache_entry = st.session_state.quote_fetch_cache.get(cooldown_key)
    if force_reload is None and isinstance(cache_entry, dict):
        fetched_at = float(cache_entry.get("fetched_at", 0.0) or 0.0)
        if (now_ts - fetched_at) < QUOTE_FETCH_COOLDOWN_SECONDS:
            quote = cache_entry.get("quote")
            note = cache_entry.get("note")
            if isinstance(quote, dict) and safe_number(quote.get("price")) is not None:
                return quote, note

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            quote = load_stock_quote(symbol, allow_insecure_ssl, force_reload=force_reload)
            st.session_state.last_good_quotes[symbol_key] = quote
            st.session_state.quote_fetch_cache[cooldown_key] = {
                "fetched_at": now_ts,
                "quote": quote,
                "note": None,
            }
            return quote, None
        except Exception as error:
            last_error = error
            if is_rate_limited_error(error):
                activate_provider_cooldown()
            if is_rate_limited_error(error) and attempt < 1:
                time.sleep(1.2)
                continue
            break

    cached_quote = st.session_state.last_good_quotes.get(symbol_key)
    if isinstance(cached_quote, dict) and safe_number(cached_quote.get("price")) is not None:
        message = f"Rate limited by data provider. Showing cached quote for {symbol_key}."
        st.session_state.quote_fetch_cache[cooldown_key] = {
            "fetched_at": now_ts,
            "quote": cached_quote,
            "note": message,
        }
        return cached_quote, message

    if last_error is not None:
        raise last_error
    raise ValueError("Unable to load quote data.")


def build_price_chart(
    history: pd.DataFrame,
    symbol: str,
    range_label: str,
    holding: dict[str, float | None],
) -> go.Figure:
    # Get currency mode for chart title
    currency_mode = st.session_state.get("currency_mode", "USD")
    
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        row_heights=[0.75, 0.25],
    )

    x_axis = history[history.columns[0]]
    figure.add_trace(
        go.Candlestick(
            x=x_axis,
            open=history["Open"],
            high=history["High"],
            low=history["Low"],
            close=history["Close"],
            name="Price",
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Bar(
            x=x_axis,
            y=history["Volume"],
            name="Volume",
            marker_color="#5b8def",
            opacity=0.6,
        ),
        row=2,
        col=1,
    )

    figure.update_layout(
        title=f"{symbol.upper()} price history ({range_label}) - {currency_mode}",
        xaxis_rangeslider_visible=False,
        margin={"l": 20, "r": 20, "t": 60, "b": 20},
        height=680,
        legend={"orientation": "h", "y": 1.02, "x": 1, "xanchor": "right", "yanchor": "bottom"},
    )

    must_sell = safe_number(holding.get("must_sell"))
    # Prefer USD purchase price; fall back to CAD converted to USD using current FX
    purchase_price_usd = safe_number(holding.get("purchase_price_usd"))
    if purchase_price_usd is None:
        purchase_price_cad = safe_number(holding.get("purchase_price_cad"))
        if purchase_price_cad is not None:
            fx = get_usd_cad_rate()
            # convert CAD -> USD
            purchase_price_usd = purchase_price_cad / fx if fx and fx > 0 else purchase_price_cad
    reasonable_lower = safe_number(holding.get("reasonable_lower"))
    reasonable_upper = safe_number(holding.get("reasonable_upper"))

    if must_sell is not None:
        figure.add_hline(
            y=must_sell,
            line_width=2,
            line_dash="dash",
            line_color="#b22222",
            row=1,
            col=1,
        )
        figure.add_annotation(
            xref="paper",
            x=0.01,
            xanchor="left",
            yref="y1",
            y=must_sell,
            text=f"Must-Sell: ${must_sell:.2f}",
            showarrow=False,
            bgcolor="#fff176",
            bordercolor="#b22222",
            borderpad=4,
            font={"size": 12, "color": "#000000"},
        )

    if purchase_price_usd is not None:
        figure.add_hline(
            y=purchase_price_usd,
            line_width=2,
            line_dash="dot",
            line_color="#1f6f43",
            row=1,
            col=1,
        )
        figure.add_annotation(
            xref="paper",
            x=0.01,
            xanchor="left",
            yref="y1",
            y=purchase_price_usd,
            text=f"Purchase Price: ${purchase_price_usd:.2f}",
            showarrow=False,
            bgcolor="#fff176",
            bordercolor="#1f6f43",
            borderpad=4,
            font={"size": 12, "color": "#000000"},
        )

    if reasonable_lower is not None and reasonable_upper is not None:
        lower = min(reasonable_lower, reasonable_upper)
        upper = max(reasonable_lower, reasonable_upper)
        figure.add_hrect(
            y0=lower,
            y1=upper,
            line_width=0,
            fillcolor="rgba(13, 110, 253, 0.16)",
            row=1,
            col=1,
        )
        mid = (lower + upper) / 2
        figure.add_annotation(
            xref="paper",
            x=0.99,
            xanchor="right",
            yref="y1",
            y=mid,
            text=f"Reasonable Range: ${lower:.2f} - ${upper:.2f}",
            showarrow=False,
            bgcolor="#fff176",
            bordercolor="#0d6efd",
            borderpad=4,
            font={"size": 12, "color": "#000000"},
        )

    figure.update_yaxes(title_text=f"Price ({currency_mode})", row=1, col=1)
    figure.update_yaxes(title_text="Volume", row=2, col=1)
    return figure


def price_summary(history: pd.DataFrame) -> tuple[str, str, str]:
    first_close = safe_number(history.iloc[0]["Close"])
    last_close = safe_number(history.iloc[-1]["Close"])
    high = safe_number(history["High"].max())
    low = safe_number(history["Low"].min())

    if first_close is None or last_close is None or high is None or low is None:
        return "N/A", "N/A", "N/A"

    change = last_close - first_close
    change_pct = 0.0 if first_close == 0 else (change / first_close) * 100
    return f"${last_close:,.2f}", f"{change:+.2f} ({change_pct:+.2f}%)", f"${low:,.2f} - ${high:,.2f}"


def render_metric_cards(info: dict[str, Any], history: pd.DataFrame, holding: dict[str, Any], current_price: float | None) -> None:
    latest_price, period_return, period_range = price_summary(history)

    # currency_mode and fx_rate early so we can interpret stored purchase prices
    currency_mode = st.session_state.get("currency_mode", "USD")
    if currency_mode == "CAD":
        fx_rate = get_usd_cad_rate()
    else:
        fx_rate = 1.0

    # Get current broker and quantity
    broker = holding.get("broker", "RBC")
    quantity = safe_number(holding.get("quantity"))

    # Determine purchase price in USD (prefer explicit USD entry; fall back to CAD conversion)
    purchase_price_usd = safe_number(holding.get("purchase_price_usd"))
    if purchase_price_usd is None:
        purchase_price_cad = safe_number(holding.get("purchase_price_cad"))
        if purchase_price_cad is not None and fx_rate and fx_rate > 0:
            purchase_price_usd = purchase_price_cad / fx_rate

    # Calculate Today's Return and Total Return with colors
    today_return_pct = None
    today_return_value = None
    total_return_pct = None
    today_delta = None
    total_delta = None

    if current_price is not None and quantity is not None:
        # Use the previous candle's close (penultimate) for "Today's Return" calculation
        # This is more stable and avoids incorrect deltas when the history range starts earlier.
        prev_price = None
        try:
            if len(history) >= 2:
                prev_price = safe_number(history.iloc[-2]["Close"])
            else:
                prev_price = safe_number(history.iloc[0]["Close"])
        except Exception:
            prev_price = safe_number(history.iloc[0]["Close"]) if len(history) > 0 else None

        if prev_price is not None:
            today_return_value = (current_price - prev_price) * quantity
            today_return_pct = ((current_price - prev_price) / prev_price * 100) if prev_price != 0 else 0
            today_delta = today_return_value

        if purchase_price_usd is not None and purchase_price_usd > 0:
            total_return_value = (current_price - purchase_price_usd) * quantity
            total_return_pct = ((current_price - purchase_price_usd) / purchase_price_usd * 100) if purchase_price_usd != 0 else 0
            total_delta = total_return_value

    # Calculate estimated sale price (pass fx_rate for flat-fee conversions)
    sale_data = calculate_estimated_sale_price(current_price, quantity, broker, fx_rate=fx_rate, display_currency=currency_mode)
    
    # CSS to make metric labels shorter to prevent wrapping
    st.markdown(
        """
        <style>
        .metric-label { font-size: 0.8em !important; }
        </style>
        """,
        unsafe_allow_html=True
    )
    
    # Build metric columns - 4 per row
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        price_val = current_price * fx_rate if current_price else None
        st.metric("Last Close", f"${price_val:.2f} {currency_mode}" if price_val else "N/A")
    with col2:
        st.metric("Period", period_return)
    with col3:
        st.metric("Range", period_range)
    with col4:
        market_cap_val = pick_value(info, "marketCap")
        if market_cap_val:
            market_cap_val = market_cap_val * fx_rate if currency_mode == "CAD" else market_cap_val
            market_cap_formatted = f"${market_cap_val/1e9:.2f}B {currency_mode}" if market_cap_val >= 1e9 else f"${market_cap_val/1e6:.2f}M {currency_mode}"
        else:
            market_cap_formatted = "N/A"
        st.metric("Market Cap", market_cap_formatted)
    
    col5, col6, col7, col8 = st.columns(4)
    
    with col5:
        st.metric("P/E (Trailing)", format_ratio(pick_value(info, "trailingPE")))
    with col6:
        st.metric("P/E (Forward)", format_ratio(pick_value(info, "forwardPE")))
    with col7:
        st.metric("PEG Ratio", format_ratio(pick_value(info, "pegRatio")))
    with col8:
        st.metric("Div. Yield", format_percent(pick_value(info, "dividendYield")))
    
    # Today's Return and Total Return section
    if today_return_pct is not None or total_return_pct is not None:
        st.markdown("---")
        st.subheader("Your Returns")
        
        ret_col1, ret_col2 = st.columns(2)
        
        if today_return_pct is not None and today_delta is not None:
            with ret_col1:
                color = "green" if today_delta >= 0 else "red"
                return_text = f"+{today_return_pct:.2f}%" if today_delta >= 0 else f"{today_return_pct:.2f}%"
                delta_val = today_delta * fx_rate
                delta_text = f"+${delta_val:.2f}" if today_delta >= 0 else f"${delta_val:.2f}"
                st.metric(
                    "Today's Return",
                    return_text,
                    delta=delta_text,
                    delta_color="off"
                )
        
        if total_return_pct is not None and total_delta is not None:
            with ret_col2:
                color = "green" if total_delta >= 0 else "red"
                return_text = f"+{total_return_pct:.2f}%" if total_delta >= 0 else f"{total_return_pct:.2f}%"
                delta_val = total_delta * fx_rate
                delta_text = f"+${delta_val:.2f}" if total_delta >= 0 else f"${delta_val:.2f}"
                st.metric(
                    "Total Return",
                    return_text,
                    delta=delta_text,
                    delta_color="off"
                )
    
    # Estimated sale price section
    if quantity is not None and current_price is not None:
        st.markdown("---")
        st.subheader("Estimated Sale Price")
        
        sale_col1, sale_col2, sale_col3 = st.columns(3)
        
        with sale_col1:
            current_val = sale_data["current_total"] * fx_rate if sale_data["current_total"] else 0
            st.metric(
                "Position Value",
                f"${current_val:.2f}" if sale_data["current_total"] else "N/A"
            )
        with sale_col2:
            broker_name = "WealthSimple" if broker == "WealthSimple" else "RBC"
            fee_type = sale_data.get("fee_type")
            fee_val = 0
            # Percent fee
            if fee_type == "percent":
                fee_percent = sale_data.get("fee_percent") or 0
                fee_text = f"{broker_name} Fee ({fee_percent*100:.2f}%)"
                fee_val = sale_data.get("fee_amount", 0) * fx_rate if sale_data.get("fee_amount") else 0
            else:
                # Flat fee
                fee_currency = sale_data.get("fee_currency", "USD")
                fee_value = sale_data.get("fee_value", 0)
                if currency_mode == "CAD":
                    if fee_currency == "CAD":
                        fee_text = f"{broker_name} Fee ({format_currency(fee_value, 'CAD')})"
                        fee_val = fee_value
                    else:
                        fee_text = f"{broker_name} Fee ({format_currency(sale_data.get('fee_amount',0)*fx_rate, 'CAD')})"
                        fee_val = sale_data.get("fee_amount", 0) * fx_rate
                else:
                    # display USD
                    if fee_currency == "CAD":
                        fee_text = f"{broker_name} Fee ({format_currency(sale_data.get('fee_amount',0), 'USD')})"
                        fee_val = sale_data.get("fee_amount", 0)
                    else:
                        fee_text = f"{broker_name} Fee ({format_currency(fee_value, 'USD')})"
                        fee_val = fee_value

            st.metric(fee_text, f"${fee_val:.2f}" if fee_val else "N/A")
        with sale_col3:
            proceeds_val = sale_data["estimated_proceeds"] * fx_rate if sale_data["estimated_proceeds"] else 0
            st.metric(
                "Proceeds (After Fee)",
                f"${proceeds_val:.2f}" if sale_data["estimated_proceeds"] else "N/A"
            )
        
        # Fee reference links
        fee_links = {
            "RBC": "https://www.rbcdirectinvesting.com/pricing/",
            "WealthSimple": "https://help.wealthsimple.com/hc/en-ca/articles/4414660979355-Upgrade-to-USD-accounts-for-stock-and-crypto-trading"
        }
        if broker in fee_links:
            st.caption(f"[📎 {broker} Fee Reference]({fee_links[broker]})")


def render_fundamentals(info: dict[str, Any]) -> None:
    valuation_rows = pd.DataFrame(
        [
            {"Metric": "Price to book", "Value": format_ratio(pick_value(info, "priceToBook"))},
            {"Metric": "Enterprise value", "Value": format_currency(pick_value(info, "enterpriseValue"), "USD")},
            {"Metric": "EV / EBITDA", "Value": format_ratio(pick_value(info, "enterpriseToEbitda"))},
            {"Metric": "EPS", "Value": format_ratio(pick_value(info, "trailingEps"))},
            {"Metric": "Beta", "Value": format_ratio(pick_value(info, "beta"))},
            {"Metric": "Shares outstanding", "Value": format_large_number(pick_value(info, "sharesOutstanding"))},
        ]
    )

    quality_rows = pd.DataFrame(
        [
            {"Metric": "Gross margin", "Value": format_percent(pick_value(info, "grossMargins"))},
            {"Metric": "Operating margin", "Value": format_percent(pick_value(info, "operatingMargins"))},
            {"Metric": "Profit margin", "Value": format_percent(pick_value(info, "profitMargins"))},
            {"Metric": "Revenue growth", "Value": format_percent(pick_value(info, "revenueGrowth"))},
            {"Metric": "Return on equity", "Value": format_percent(pick_value(info, "returnOnEquity"))},
            {"Metric": "Return on assets", "Value": format_percent(pick_value(info, "returnOnAssets"))},
        ]
    )

    trading_rows = pd.DataFrame(
        [
            {"Metric": "Previous close", "Value": format_currency(pick_value(info, "previousClose"), "USD")},
            {"Metric": "Open", "Value": format_currency(pick_value(info, "open"), "USD")},
            {"Metric": "Day low", "Value": format_currency(pick_value(info, "dayLow"), "USD")},
            {"Metric": "Day high", "Value": format_currency(pick_value(info, "dayHigh"), "USD")},
            {"Metric": "52 week low", "Value": format_currency(pick_value(info, "fiftyTwoWeekLow"), "USD")},
            {"Metric": "52 week high", "Value": format_currency(pick_value(info, "fiftyTwoWeekHigh"), "USD")},
        ]
    )

    left_column, right_column = st.columns(2)
    with left_column:
        st.subheader("Valuation")
        st.dataframe(valuation_rows, use_container_width=True, hide_index=True)
        st.subheader("Trading snapshot")
        st.dataframe(trading_rows, use_container_width=True, hide_index=True)
    with right_column:
        st.subheader("Business quality")
        st.dataframe(quality_rows, use_container_width=True, hide_index=True)


def render_news(news_items: list[dict[str, Any]]) -> None:
    st.subheader("Recent news")
    if not news_items:
        st.info("No recent news was returned for this ticker.")
        return

    for item in news_items:
        content = item.get("content") or {}
        title = content.get("title") or item.get("title") or "Untitled article"
        summary = content.get("summary") or item.get("summary") or ""
        publisher = content.get("provider", {}).get("displayName") or item.get("publisher") or "Unknown publisher"
        canonical_url = content.get("canonicalUrl", {}).get("url") or item.get("link")
        published_at = content.get("pubDate") or item.get("providerPublishTime")

        st.markdown(f"**{title}**")
        st.caption(f"{publisher} | {published_at if published_at else 'Publish time unavailable'}")
        if summary:
            st.write(summary)
        if canonical_url:
            st.markdown(f"[Open article]({canonical_url})")
        st.divider()


def render_watchlist_display() -> None:
    """Render watchlist table and management controls."""
    st.markdown("### 🔭 Watchlist")

    if not st.session_state.watchlist:
        st.info("Your watchlist is empty. Use the Add to Watchlist button in My Holdings.")
    else:
        # Build watchlist display with current prices
        currency_mode = st.session_state.get("currency_mode", "USD")
        price_label = f"Current Price ({currency_mode})"
        
        # Column headers
        header_col1, header_col2, header_col3 = st.columns([1.5, 1.5, 1])
        with header_col1:
            st.markdown("<center><strong>Ticker</strong></center>", unsafe_allow_html=True)
        with header_col2:
            st.markdown(f"<center><strong>{price_label}</strong></center>", unsafe_allow_html=True)
        with header_col3:
            st.markdown("<center><strong>Action</strong></center>", unsafe_allow_html=True)
        
        for idx, item in enumerate(list(st.session_state.watchlist)):
            sym = item.get("symbol", "")
            current_price_str = "—"
            try:
                allow_insecure_ssl = st.session_state.get("allow_insecure_ssl", False)
                watch_quote, _ = load_stock_quote_resilient(
                    sym,
                    allow_insecure_ssl,
                    force_reload=st.session_state.get("last_refresh"),
                )
                current_price = safe_number(watch_quote.get("price"))
                if current_price:
                    if currency_mode == "CAD":
                        fx_rate = get_usd_cad_rate()
                        display_price = current_price * fx_rate
                    else:
                        display_price = current_price
                    current_price_str = f"${display_price:.2f}"
            except Exception:
                pass

            row_col1, row_col2, row_col3 = st.columns([1.5, 1.5, 1])
            with row_col1:
                if st.button(sym, key=f"watch_select_{idx}", use_container_width=True):
                    st.session_state.selected_watchlist_symbol = sym
                    st.session_state.search_symbol = sym
                    st.rerun()
            with row_col2:
                st.markdown(f"<center>{current_price_str}</center>", unsafe_allow_html=True)
            with row_col3:
                remove_key = f"watch_remove_btn_{idx}"
                if st.button("Remove", key=remove_key, use_container_width=True):
                    st.session_state.watchlist.pop(idx)
                    set_persistent_data_key("watchlist", st.session_state.watchlist)
                    st.warning(f"Removed {sym} from watchlist.")
                    st.rerun()


def render_notes_panel(selected_symbol: str) -> None:
    st.markdown("---")
    with st.expander("📝 Notes", expanded=False):
        st.caption("Notes auto-save when you edit each field.")

        if "master_note_input" not in st.session_state:
            st.session_state.master_note_input = st.session_state.get("master_note", "")

        def _save_master_note() -> None:
            note_text = st.session_state.get("master_note_input", "")
            saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            st.session_state.master_note = note_text
            st.session_state.master_note_saved_at = saved_at
            set_persistent_data_key("master_note", note_text)
            set_persistent_data_key("master_note_saved_at", saved_at)

        st.text_area(
            "Master note (all stocks)",
            key="master_note_input",
            height=140,
            placeholder="Write portfolio-level notes, reminders, strategy, or checklist...",
            on_change=_save_master_note,
        )
        if st.button("Save master note now", key="save_master_note_now", use_container_width=True):
            _save_master_note()
            st.success("Master note saved.")

        master_saved_at = st.session_state.get("master_note_saved_at", "")
        st.caption(f"Master note auto-saved: {master_saved_at if master_saved_at else 'Not saved yet'}")

        symbol_key = (selected_symbol or "").upper()
        if not symbol_key:
            st.info("Select a holding or watchlist ticker to edit an individual note.")
            return

        symbol_notes: dict[str, str] = st.session_state.get("symbol_notes", {})
        current_symbol_note = symbol_notes.get(symbol_key, "")

        symbol_input_key = f"symbol_note_input_{symbol_key}"
        if symbol_input_key not in st.session_state:
            st.session_state[symbol_input_key] = current_symbol_note

        def _save_symbol_note(sym: str, input_key: str) -> None:
            notes = st.session_state.get("symbol_notes", {})
            note_saved_at = st.session_state.get("symbol_note_saved_at", {})
            note_text = st.session_state.get(input_key, "")
            cleaned_note = note_text.strip()
            saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if cleaned_note:
                notes[sym] = cleaned_note
            else:
                notes.pop(sym, None)
            note_saved_at[sym] = saved_at
            st.session_state.symbol_notes = notes
            st.session_state.symbol_note_saved_at = note_saved_at
            set_persistent_data_key("symbol_notes", notes)
            set_persistent_data_key("symbol_note_saved_at", note_saved_at)

        st.text_area(
            f"Note for {symbol_key}",
            key=symbol_input_key,
            height=140,
            placeholder=f"Write ticker-specific notes for {symbol_key}...",
            on_change=_save_symbol_note,
            args=(symbol_key, symbol_input_key),
        )
        if st.button(f"Save {symbol_key} note now", key=f"save_symbol_note_now_{symbol_key}", use_container_width=True):
            _save_symbol_note(symbol_key, symbol_input_key)
            st.success(f"Note for {symbol_key} saved.")

        symbol_saved_at_map: dict[str, str] = st.session_state.get("symbol_note_saved_at", {})
        symbol_saved_at = symbol_saved_at_map.get(symbol_key, "")
        st.caption(f"{symbol_key} note auto-saved: {symbol_saved_at if symbol_saved_at else 'Not saved yet'}")


def render_holding_manager() -> str:
    st.markdown("### 📊 My Holdings")
    
    holdings: dict[str, dict[str, float | None]] = st.session_state.holdings
    symbols = sorted(holdings.keys())

    if not symbols:
        holdings["AAPL"] = {
            "must_sell": None,
            "purchase_price_usd": None,
            "purchase_price_cad": None,
            "reasonable_lower": None,
            "reasonable_upper": None,
            "broker": "RBC",
            "quantity": None,
        }
        symbols = ["AAPL"]

    current_selected = st.session_state.selected_holding
    if current_selected not in holdings:
        current_selected = symbols[0]
        st.session_state.selected_holding = current_selected

    # Get currency mode and FX rate for display
    currency_mode = st.session_state.get("currency_mode", "USD")
    if currency_mode == "CAD":
        fx_rate = get_usd_cad_rate()
    else:
        fx_rate = 1.0

    # Holdings table with clickable rows
    holdings_data = []
    for symbol in symbols:
        holding = holdings[symbol]
        quantity = safe_number(holding.get("quantity"))
        purchase_price_usd = safe_number(holding.get("purchase_price_usd"))
        purchase_price_cad = safe_number(holding.get("purchase_price_cad"))

        # Determine base purchase price in USD for book cost calculation
        base_purchase_usd = None
        if purchase_price_usd is not None:
            base_purchase_usd = purchase_price_usd
        elif purchase_price_cad is not None and fx_rate and fx_rate > 0:
            base_purchase_usd = purchase_price_cad / fx_rate

        book_cost = None
        if quantity and base_purchase_usd:
            book_cost = quantity * base_purchase_usd

        holdings_data.append({
            "symbol": symbol,
            "quantity": quantity,
            "purchase_price_usd": purchase_price_usd,
            "purchase_price_cad": purchase_price_cad,
            "book_cost": book_cost,
            "broker": holding.get("broker", "RBC"),
        })
    
    # Create clickable table
    cols = st.columns([1, 1.5, 1.5, 2, 1.5])
    with cols[0]:
        st.write("**Ticker**")
    with cols[1]:
        st.write("**Quantity**")
    with cols[2]:
        st.write(f"**Purchase Price ({currency_mode})**")
    with cols[3]:
        st.write(f"**Book Cost ({currency_mode})**")
    with cols[4]:
        st.write("**Broker**")
    
    for holding_data in holdings_data:
        cols = st.columns([1, 1.5, 1.5, 2, 1.5])
        with cols[0]:
            if st.button(
                holding_data["symbol"],
                key=f"select_{holding_data['symbol']}",
                use_container_width=True
            ):
                st.session_state.selected_holding = holding_data["symbol"]
                st.session_state.selected_watchlist_symbol = ""
                set_persistent_data_key("selected_holding", holding_data["symbol"])
                st.rerun()
        with cols[1]:
            qty_str = f"{holding_data['quantity']:.4f}" if holding_data["quantity"] else "-"
            st.write(qty_str)
        with cols[2]:
            # Display purchase price in selected currency (prefer explicit field)
            price_str = "-"
            if currency_mode == "CAD":
                if holding_data.get("purchase_price_cad") is not None:
                    price_display = holding_data.get("purchase_price_cad")
                    price_str = f"${price_display:.2f}"
                elif holding_data.get("purchase_price_usd") is not None:
                    price_display = holding_data.get("purchase_price_usd") * fx_rate
                    price_str = f"${price_display:.2f}"
            else:
                if holding_data.get("purchase_price_usd") is not None:
                    price_display = holding_data.get("purchase_price_usd")
                    price_str = f"${price_display:.2f}"
                elif holding_data.get("purchase_price_cad") is not None and fx_rate and fx_rate > 0:
                    price_display = holding_data.get("purchase_price_cad") / fx_rate
                    price_str = f"${price_display:.2f}"
            st.write(price_str)
        with cols[3]:
            if holding_data["book_cost"]:
                # book_cost is stored in USD base, convert for display
                cost_display = holding_data["book_cost"] * fx_rate
                cost_str = f"${cost_display:.2f}"
            else:
                cost_str = "-"
            st.write(cost_str)
        with cols[4]:
            st.write(holding_data["broker"])
    
    st.divider()

    # Add / search / watchlist actions
    st.markdown("### Add New Holding")
    new_symbol = st.text_input("Holding Ticker", placeholder="e.g., AAPL or MSFT").strip().upper()

    search_performed = False
    add_to_holding = False
    add_to_watchlist = False
    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        if st.button("Search", use_container_width=True, key="search_btn"):
            if new_symbol:
                st.session_state.selected_watchlist_symbol = new_symbol
                st.rerun()
            else:
                st.warning("Please enter a ticker symbol.")
    with col2:
        if st.button("Add to Holding", use_container_width=True, key="add_to_holding_btn"):
            if not new_symbol:
                st.warning("Please enter a ticker symbol.")
            else:
                add_to_holding = True
    with col3:
        if st.button("Add to Watchlist", use_container_width=True, key="add_to_watchlist_btn"):
            if not new_symbol:
                st.warning("Please enter a ticker symbol.")
            else:
                add_to_watchlist = True

    if add_to_holding:
        if new_symbol not in holdings:
            get_holding(new_symbol)
            st.session_state.selected_holding = new_symbol
            set_persistent_data_key("selected_holding", new_symbol)
            set_persistent_data_key("holdings", st.session_state.holdings)
            st.success(f"✓ Added {new_symbol} to holdings.")
            st.rerun()
        else:
            st.warning(f"{new_symbol} is already in your holdings.")

    if add_to_watchlist:
        if any(w.get("symbol") == new_symbol for w in st.session_state.watchlist):
            st.info(f"{new_symbol} is already in your watchlist.")
        else:
            st.session_state.watchlist.append({"symbol": new_symbol, "note": ""})
            set_persistent_data_key("watchlist", st.session_state.watchlist)
            st.success(f"✓ Added {new_symbol} to watchlist.")
            st.session_state.selected_watchlist_symbol = new_symbol
            st.rerun()

    # Holding details form
    selected_holding = get_holding(st.session_state.selected_holding)
    currency_mode = st.session_state.get("currency_mode", "USD")
    
    # Get FX rate for currency conversion
    if currency_mode == "CAD":
        fx_rate = get_usd_cad_rate()
    else:
        fx_rate = 1.0
    
    st.markdown("---")
    st.markdown(f"### Edit: {st.session_state.selected_holding}")
    
    with st.form("holding-details-form"):
        # Broker selection
        broker = st.selectbox(
            "Broker",
            options=["RBC", "WealthSimple"],
            index=0 if selected_holding.get("broker", "RBC") == "RBC" else 1,
        )
        
        # Quantity (support 4 decimals for fractional trading)
        quantity_val = st.number_input(
            "Quantity",
            min_value=0.0,
            value=selected_holding.get("quantity") or 0.0,
            step=0.0001,
            format="%.4f",
        )
        
        # Purchase price in both USD and CAD (allows entering both when purchase FX differed)
        stored_usd = selected_holding.get("purchase_price_usd")
        stored_cad = selected_holding.get("purchase_price_cad")

        purchase_price_usd_default = stored_usd if stored_usd is not None else (stored_cad / fx_rate if stored_cad is not None and fx_rate and fx_rate > 0 else 0.0)
        purchase_price_cad_default = stored_cad if stored_cad is not None else (stored_usd * fx_rate if stored_usd is not None else 0.0)

        purchase_price_usd_val = st.number_input(
            "Purchase Price (USD)",
            min_value=0.0,
            value=float(purchase_price_usd_default),
            step=0.01,
            help="Enter the purchase price in USD (if known).",
        )

        purchase_price_cad_val = st.number_input(
            "Purchase Price (CAD)",
            min_value=0.0,
            value=float(purchase_price_cad_default),
            step=0.01,
            help="Enter the purchase price in CAD (if known).",
        )
        
        # Price levels with currency conversion for display
        must_sell_stored = selected_holding.get("must_sell")
        must_sell_display = must_sell_stored * fx_rate if must_sell_stored and fx_rate != 1.0 else must_sell_stored
        must_sell_text = st.text_input(
            f"Must-Sell Point ({currency_mode})",
            value="" if must_sell_display is None else f"{must_sell_display:.2f}",
            help="Leave blank to clear.",
        )
        
        reasonable_lower_stored = selected_holding.get("reasonable_lower")
        reasonable_lower_display = reasonable_lower_stored * fx_rate if reasonable_lower_stored and fx_rate != 1.0 else reasonable_lower_stored
        reasonable_lower_text = st.text_input(
            f"Reasonable Lower Limit ({currency_mode})",
            value=""
            if reasonable_lower_display is None
            else f"{reasonable_lower_display:.2f}",
            help="Leave blank to clear.",
        )
        
        reasonable_upper_stored = selected_holding.get("reasonable_upper")
        reasonable_upper_display = reasonable_upper_stored * fx_rate if reasonable_upper_stored and fx_rate != 1.0 else reasonable_upper_stored
        reasonable_upper_text = st.text_input(
            f"Reasonable Upper Limit ({currency_mode})",
            value=""
            if reasonable_upper_display is None
            else f"{reasonable_upper_display:.2f}",
            help="Leave blank to clear.",
        )
        save_holding_details = st.form_submit_button("Save holding details", use_container_width=True)
    
    if save_holding_details:
        try:
            updated = get_holding(st.session_state.selected_holding)
            updated["broker"] = broker
            updated["quantity"] = quantity_val if quantity_val > 0 else None
            # Store both USD and CAD purchase prices (allow user to specify both)
            updated["purchase_price_usd"] = purchase_price_usd_val if purchase_price_usd_val > 0 else None
            updated["purchase_price_cad"] = purchase_price_cad_val if purchase_price_cad_val > 0 else None
            updated["must_sell"] = (parse_optional_price(must_sell_text) / fx_rate) if parse_optional_price(must_sell_text) else None
            updated["reasonable_lower"] = (parse_optional_price(reasonable_lower_text) / fx_rate) if parse_optional_price(reasonable_lower_text) else None
            updated["reasonable_upper"] = (parse_optional_price(reasonable_upper_text) / fx_rate) if parse_optional_price(reasonable_upper_text) else None
            set_persistent_data_key("holdings", st.session_state.holdings)
            st.success("✓ Holding details saved.")
        except ValueError:
            st.error("One or more values are invalid. Use numeric values like 180.5.")

    if len(holdings) > 1 and st.button("Delete selected holding", use_container_width=True):
        removed = st.session_state.selected_holding
        holdings.pop(removed, None)
        st.session_state.selected_holding = sorted(holdings.keys())[0]
        set_persistent_data_key("holdings", st.session_state.holdings)
        set_persistent_data_key("selected_holding", st.session_state.selected_holding)
        st.warning(f"Removed {removed} from holdings.")
        st.rerun()

    # Return selected symbol
    return st.session_state.selected_holding


def send_in_app_alert(title: str, message_body: str) -> tuple[bool, str]:
    full_message = f"{title}: {message_body}"
    try:
        # Streamlit toast is cross-platform and stays inside the app.
        if hasattr(st, "toast"):
            st.toast(full_message, icon="🔔")

        history = st.session_state.get("notification_history", [])
        history.insert(
            0,
            {
                "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "title": title,
                "message": message_body,
                "status": "active",
            },
        )
        st.session_state.notification_history = history[:500]
        set_persistent_data_key("notification_history", st.session_state.notification_history)
        return True, "In-app notification shown"
    except Exception as error:
        return False, str(error)


def evaluate_alerts(
    allow_insecure_ssl: bool,
) -> list[str]:
    notifications: list[str] = []
    alerts = st.session_state.alerts
    if not alerts:
        return notifications

    now_ts = time.time()
    last_scan_at = float(st.session_state.get("last_alert_scan_at", 0.0) or 0.0)
    cooldown_seconds = 60
    elapsed = now_ts - last_scan_at
    if elapsed < cooldown_seconds:
        remaining = max(1, int(cooldown_seconds - elapsed))
        return [f"Alert scan throttled to protect data provider limits. Try again in {remaining}s."]

    st.session_state.last_alert_scan_at = now_ts

    for index, alert in enumerate(alerts):
        if not alert.get("enabled", True) or alert.get("sent", False):
            continue

        symbol = alert.get("symbol", "").upper()
        if not symbol:
            continue

        try:
            quote_data, quote_note = load_stock_quote_resilient(
                symbol,
                allow_insecure_ssl,
                force_reload=st.session_state.get("last_refresh"),
            )
            if quote_note:
                notifications.append(f"{symbol}: {quote_note}")
        except Exception as error:
            notifications.append(f"{symbol}: failed to evaluate alert ({error})")
            continue

        current_price = safe_number(quote_data.get("price"))
        target_price = safe_number(alert.get("target"))
        direction = alert.get("direction")
        if current_price is None or target_price is None:
            continue

        triggered = (direction == "above" and current_price >= target_price) or (
            direction == "below" and current_price <= target_price
        )
        if not triggered:
            continue

        title = f"ShellStock Alert: {symbol}"
        body = (
            f"{symbol} is {direction} {target_price:.2f}. "
            f"Current: {current_price:.2f}."
        )
        sent_ok, response = send_in_app_alert(title, body)

        if sent_ok:
            st.session_state.alerts[index]["sent"] = True
            st.session_state.alerts[index]["last_triggered_price"] = current_price
            st.session_state.alerts[index]["triggered_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            notifications.append(f"In-app alert shown for {symbol} at ${current_price:.2f}")
        else:
            notifications.append(f"{symbol}: in-app alert failed ({response})")

    return notifications


def render_alert_manager(default_symbol: str) -> list[str]:
    st.subheader("Price alerts")
    st.caption("Alerts trigger in-app notifications when you click 'Run alert scan now'.")

    test_col, hint_col = st.columns([1, 2])
    if test_col.button("Show test in-app alert", use_container_width=True):
        ok, msg = send_in_app_alert(
            title="ShellStock test alert",
            message_body=f"In-app notification test at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        )
        if ok:
            st.success("Test in-app alert shown.")
        else:
            st.error(f"Test in-app alert failed: {msg}")
    with hint_col:
        st.caption("In-app alerts are cross-platform and do not rely on OS notification services.")
    
    # Add alert form
    st.markdown("### Create price alert")
    with st.form("add-alert-form", clear_on_submit=True):
        alert_symbol = st.text_input("Ticker", value=default_symbol).strip().upper()
        direction = st.selectbox("Trigger when price goes", options=["above", "below"])
        
        # Currency selection for alert
        alert_currency = st.selectbox(
            "Target price currency",
            options=["USD", "CAD"],
            index=0
        )
        
        target = st.number_input(f"Target price ({alert_currency})", min_value=0.01, value=200.00, step=0.5)
        submitted = st.form_submit_button("Create alert", use_container_width=True)

        if submitted:
            if not alert_symbol:
                st.warning("Please enter a valid ticker for the alert.")
            else:
                st.session_state.alerts.append(
                    {
                        "symbol": alert_symbol,
                        "direction": direction,
                        "target": float(target),
                        "target_currency": alert_currency,
                        "enabled": True,
                        "sent": False,
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
                set_persistent_data_key("alerts", st.session_state.alerts)
                st.success(f"Alert created for {alert_symbol}.")

    # Display existing alerts
    if st.session_state.alerts:
        st.markdown("### Active alerts")
        alert_rows = []
        for index, alert in enumerate(st.session_state.alerts):
            target_currency = alert.get("target_currency", "USD")
            alert_rows.append(
                {
                    "Select": False,
                    "Ticker": alert["symbol"],
                    "Condition": f"{alert['direction']} ${alert['target']:.2f} {target_currency}",
                    "Enabled": "✓" if alert.get("enabled", True) else "✗",
                    "Sent": "✓" if alert.get("sent", False) else "",
                }
            )

        alert_df = pd.DataFrame(alert_rows)
        edited_alert_df = st.data_editor(
            alert_df,
            use_container_width=True,
            hide_index=True,
            key="alerts_table_editor",
            column_config={
                "Select": st.column_config.CheckboxColumn("Select", help="Pick one alert"),
            },
            disabled=["Ticker", "Condition", "Enabled", "Sent"],
        )

        selected_alert_positions = [
            idx for idx, selected in enumerate(edited_alert_df.get("Select", [])) if bool(selected)
        ]
        selected_index = selected_alert_positions[0] if selected_alert_positions else None
        if len(selected_alert_positions) > 1:
            st.caption("Multiple alerts selected. Actions will use the first selected row.")

        action_left, action_mid, action_right = st.columns(3)
        if action_left.button("Toggle enable", use_container_width=True):
            if selected_index is None:
                st.warning("Select an alert row first.")
            else:
                st.session_state.alerts[int(selected_index)]["enabled"] = not st.session_state.alerts[int(selected_index)].get(
                    "enabled", True
                )
                set_persistent_data_key("alerts", st.session_state.alerts)
                st.rerun()
        if action_mid.button("Reset sent", use_container_width=True):
            if selected_index is None:
                st.warning("Select an alert row first.")
            else:
                st.session_state.alerts[int(selected_index)]["sent"] = False
                st.session_state.alerts[int(selected_index)].pop("triggered_at", None)
                set_persistent_data_key("alerts", st.session_state.alerts)
                st.rerun()
        if action_right.button("Delete alert", use_container_width=True):
            if selected_index is None:
                st.warning("Select an alert row first.")
            else:
                st.session_state.alerts.pop(int(selected_index))
                set_persistent_data_key("alerts", st.session_state.alerts)
                st.rerun()
    else:
        st.info("No alerts created yet.")

    st.caption("Notification history is managed in the sidebar master hub (🔔 Notifications).")

    pending_notifications: list[str] = []
    last_scan_at = float(st.session_state.get("last_alert_scan_at", 0.0) or 0.0)
    if last_scan_at > 0:
        elapsed = time.time() - last_scan_at
        if elapsed < 60:
            st.caption(f"Next alert scan available in {max(1, int(60 - elapsed))}s.")
        else:
            st.caption(f"Last alert scan: {datetime.fromtimestamp(last_scan_at).strftime('%Y-%m-%d %H:%M:%S')}")

    if st.button("Run alert scan now", type="primary"):
        pending_notifications = ["SCAN_REQUESTED"]
    
    return pending_notifications


def render_notification_hub() -> None:
    st.markdown("### 🔔 Master Notification Hub")

    history: list[dict[str, Any]] = st.session_state.get("notification_history", [])
    if not history:
        st.info("No notifications yet.")
        return

    status_filter = st.radio(
        "Show",
        options=["Active", "Archived", "All"],
        horizontal=True,
        key="notification_filter",
        label_visibility="collapsed",
    )

    if status_filter == "Active":
        filtered_notifications = [item for item in history if item.get("status", "active") == "active"]
    elif status_filter == "Archived":
        filtered_notifications = [item for item in history if item.get("status") == "archived"]
    else:
        filtered_notifications = history

    if not filtered_notifications:
        st.caption("No notifications match this filter.")
        return

    display_rows = []
    for item in filtered_notifications:
        display_rows.append(
            {
                "Select": False,
                "When": item.get("created_at", ""),
                "Status": item.get("status", "active"),
                "Title": item.get("title", ""),
                "Message": item.get("message", ""),
            }
        )

    notifications_df = pd.DataFrame(display_rows)
    edited_notifications_df = st.data_editor(
        notifications_df,
        use_container_width=True,
        hide_index=True,
        key="notifications_table_editor",
        column_config={
            "Select": st.column_config.CheckboxColumn("Select", help="Pick one notification"),
        },
        disabled=["When", "Status", "Title", "Message"],
    )

    selected_notification_positions = [
        idx for idx, selected in enumerate(edited_notifications_df.get("Select", [])) if bool(selected)
    ]
    selected_notification_idx = selected_notification_positions[0] if selected_notification_positions else None
    if len(selected_notification_positions) > 1:
        st.caption("Multiple notifications selected. Actions will use the first selected row.")

    action_col_1, action_col_2, action_col_3 = st.columns(3)

    if action_col_1.button("Archive selected", use_container_width=True, key="hub_archive_selected"):
        if selected_notification_idx is None:
            st.warning("Select a notification row first.")
        else:
            selected_id = str(filtered_notifications[int(selected_notification_idx)].get("id", ""))
            for item in history:
                if str(item.get("id", "")) == selected_id:
                    item["status"] = "archived"
                    break
            st.session_state.notification_history = history
            set_persistent_data_key("notification_history", history)
            st.rerun()

    if action_col_2.button("Unarchive selected", use_container_width=True, key="hub_unarchive_selected"):
        if selected_notification_idx is None:
            st.warning("Select a notification row first.")
        else:
            selected_id = str(filtered_notifications[int(selected_notification_idx)].get("id", ""))
            for item in history:
                if str(item.get("id", "")) == selected_id:
                    item["status"] = "active"
                    break
            st.session_state.notification_history = history
            set_persistent_data_key("notification_history", history)
            st.rerun()

    if action_col_3.button("Delete selected", use_container_width=True, key="hub_delete_selected"):
        if selected_notification_idx is None:
            st.warning("Select a notification row first.")
        else:
            selected_id = str(filtered_notifications[int(selected_notification_idx)].get("id", ""))
            history = [item for item in history if str(item.get("id", "")) != selected_id]
            st.session_state.notification_history = history
            set_persistent_data_key("notification_history", history)
            st.rerun()


def render_auth_gate() -> bool:
    """Render a simple Supabase Auth gate when cloud persistence is enabled."""
    if not _supabase_configured():
        return True

    if _supabase_user_id():
        return True

    st.title("ShellStock")
    st.subheader("Sign in to access your saved data")
    st.caption("Your holdings, watchlist, notes, alerts, and notification history are saved per user account.")

    callback_message = st.session_state.pop("auth_callback_message", None)
    if callback_message:
        level, text = callback_message
        if level == "success":
            st.success(text)
        else:
            st.error(text)

    tab_sign_in, tab_sign_up = st.tabs(["Sign In", "Create Account"])

    with tab_sign_in:
        with st.form("supabase_sign_in_form"):
            login_email = st.text_input("Email", key="supabase_login_email")
            login_password = st.text_input("Password", type="password", key="supabase_login_password")
            sign_in_submitted = st.form_submit_button("Sign In", use_container_width=True)

        if sign_in_submitted:
            ok, message = _sign_in_supabase(login_email.strip(), login_password)
            if ok:
                st.success(message)
                st.rerun()
            else:
                st.error(message)

        st.markdown("---")
        st.caption("Forgot your password?")
        with st.form("supabase_forgot_password_form"):
            forgot_email = st.text_input("Email for reset link", key="supabase_forgot_email")
            forgot_submitted = st.form_submit_button("Send reset link", use_container_width=True)

        if forgot_submitted:
            ok, message = _send_password_reset_email(forgot_email.strip())
            if ok:
                st.success(message)
                if not _get_auth_redirect_url():
                    st.caption("Tip: set AUTH_REDIRECT_URL in Streamlit secrets to avoid broken redirect pages.")
            else:
                st.error(message)

        if st.session_state.get("supabase_recovery_mode", False):
            st.markdown("---")
            st.caption("Recovery session detected. Set your new password.")
            with st.form("supabase_set_new_password_form"):
                new_password = st.text_input("New password", type="password", key="supabase_new_password")
                update_submitted = st.form_submit_button("Update password", use_container_width=True)

            if update_submitted:
                ok, message = _update_password_supabase(new_password)
                if ok:
                    st.success(message)
                else:
                    st.error(message)

    with tab_sign_up:
        with st.form("supabase_sign_up_form"):
            signup_email = st.text_input("Email", key="supabase_signup_email")
            signup_password = st.text_input("Password", type="password", key="supabase_signup_password")
            sign_up_submitted = st.form_submit_button("Create Account", use_container_width=True)

        if sign_up_submitted:
            ok, message = _sign_up_supabase(signup_email.strip(), signup_password)
            if ok:
                st.success(message)
                if _supabase_user_id():
                    st.rerun()
            else:
                st.error(message)

    return False


def render_auth_sidebar_controls() -> None:
    if not _supabase_configured():
        return

    user_email = st.session_state.get("supabase_user_email", "")
    if user_email:
        st.caption(f"Signed in as: {user_email}")
    else:
        st.caption("Signed in")

    if st.button("Sign out", use_container_width=True, key="signout_btn"):
        _clear_auth_session()
        st.rerun()


def main() -> None:
    apply_custom_theme()
    _handle_supabase_auth_callback()

    if _supabase_configured() and not render_auth_gate():
        return

    initialize_state()

    # Clear per-rerun request cache so repeated fetches in one run are shared.
    st.session_state.request_cycle_bundle_cache = {}

    st.title("ShellStock")
    
    # Display current time in Eastern and PST
    current_time = get_current_time_display()
    st.markdown(
        f"""
        <div class="shell-banner">
            <h3 style="margin:0;">{current_time}</h3>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        # Create three tabs in the sidebar: Controls, My Holdings, Watchlist
        tab_controls, tab_holdings, tab_watchlist, tab_notifications = st.tabs([
            "⚙️ Controls",
            "📊 My Holdings",
            "🔭 Watchlist",
            "🔔 Notifications",
        ])
        
        with tab_controls:
            backend_label = "Supabase (per-user)" if _supabase_configured() else "Local JSON"
            st.caption(f"Storage backend: {backend_label}")
            render_auth_sidebar_controls()

            # Refresh + auto-refresh compact row
            rcol1, rcol2 = st.columns([2, 1])
            with rcol1:
                if st.button("Refresh Now", use_container_width=True):
                    st.session_state["last_refresh"] = time.time()
                    if st.session_state.get("auto_refresh_enabled"):
                        st.session_state["auto_refresh_until"] = time.time() + float(st.session_state.get("auto_refresh_duration", 60))
                    st.rerun()
            with rcol2:
                auto_enabled = st.checkbox("Auto", value=st.session_state.get("auto_refresh_enabled", False), help="Enable auto-refresh after pressing Refresh Now")
                st.session_state["auto_refresh_enabled"] = auto_enabled

            # Auto-refresh duration stored in session; no direct user entry here
            if "auto_refresh_duration" not in st.session_state:
                st.session_state["auto_refresh_duration"] = 60

            # Display currency selector (compact)
            st.selectbox(
                "Display currency",
                options=["USD", "CAD"],
                index=0 if st.session_state.currency_mode == "USD" else 1,
                key="display_currency_select",
            )
            # Wire display currency change
            new_currency = st.session_state.get("display_currency_select")
            if new_currency and new_currency != st.session_state.currency_mode:
                st.session_state.currency_mode = new_currency
                set_persistent_data_key("currency_mode", new_currency)
                st.rerun()

            # SSL verification option removed from UI; default to False
            if "allow_insecure_ssl" not in st.session_state:
                st.session_state["allow_insecure_ssl"] = False
            allow_insecure_ssl = st.session_state.get("allow_insecure_ssl", False)

            # FX mini-chart (compact)
            fx_info = get_usd_cad_rate_info()
            fx_rate = fx_info.get("rate", 1.35)
            fx_timestamp = fx_info.get("timestamp", "N/A")
            st.caption(f"USD/CAD: {fx_rate:.4f} (as of {fx_timestamp})")
            fx_range_options = ["1 Day", "1 Week", "1 Month", "3 Months", "6 Months", "YTD", "1 Year"]
            fx_range_label = st.select_slider(
                "FX range",
                options=fx_range_options,
                value=fx_range_options[2],
                key="fx_range_select",
            )
            # Add vertical spacing between the slider and the FX mini-chart for readability
            st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
            try:
                fx_history, fx_info, _, fx_note = load_stock_bundle_resilient(
                    "CAD=X",
                    fx_range_label,
                    allow_insecure_ssl,
                    force_reload=st.session_state.get("last_refresh"),
                )
                fx_series = fx_history.set_index(fx_history.columns[0])["Close"]

                fig_fx = go.Figure()
                fig_fx.add_trace(go.Scatter(x=fx_series.index, y=fx_series.values, mode="lines", line=dict(color="#00d9ff", width=2), showlegend=False))
                last_x = fx_series.index[-1]
                last_y = float(fx_series.iloc[-1])
                y_min = float(fx_series.min())
                y_max = float(fx_series.max())
                span = (y_max - y_min) if y_max != y_min else max(0.0001, y_max * 0.0005)
                lower_pad = span * 0.06
                upper_pad = span * 0.20
                fig_fx.update_layout(
                    height=120,
                    margin=dict(l=20, r=6, t=24, b=20),
                    plot_bgcolor="rgba(0,0,0,0)",
                    paper_bgcolor="rgba(0,0,0,0)",
                    showlegend=False,
                    title={"text": "CAD to USD", "x": 0.5, "xanchor": "center", "font": {"size": 14}},
                )
                fig_fx.update_yaxes(range=[y_min - lower_pad, y_max + upper_pad], tickformat=".4f", gridcolor="rgba(120,120,120,0.20)")
                fig_fx.add_trace(go.Scatter(x=[last_x], y=[last_y], mode="markers", marker=dict(color="#ffab00", size=8), showlegend=False))
                fig_fx.add_annotation(x=last_x, y=last_y, text=f"{last_y:.4f}", showarrow=False, bgcolor="#fff176", bordercolor="#000", borderpad=4, font={"size": 10, "color": "#000000"}, xanchor="left", yanchor="bottom")
                st.plotly_chart(fig_fx, use_container_width=True)
                st.markdown("[Source: Yahoo Finance — CAD=X](https://finance.yahoo.com/quote/CAD%3DX)")
                if fx_note:
                    st.caption(fx_note)
            except Exception:
                st.caption("FX data temporarily unavailable (provider may be rate-limiting).")

            # If auto-refresh window is active, inject a meta refresh to reload periodically
            st.markdown("---")
            auto_until = st.session_state.get("auto_refresh_until")
            if auto_until and time.time() < auto_until:
                refresh_interval = 5
                st.markdown(f"<meta http-equiv='refresh' content='{refresh_interval}'>", unsafe_allow_html=True)
                end_time_str = datetime.fromtimestamp(auto_until).strftime("%Y-%m-%d %H:%M:%S")
                st.caption(f"Auto-refreshing every {refresh_interval}s until {end_time_str}")
        
        with tab_holdings:
            # Holdings manager
            symbol = render_holding_manager()
        
        with tab_watchlist:
            # Watchlist display
            render_watchlist_display()

        with tab_notifications:
            render_notification_hub()

    # Determine is_watchlist_selected based on session state
    is_watchlist_selected = bool(st.session_state.get("selected_watchlist_symbol"))
    symbol = st.session_state.get("selected_watchlist_symbol") or st.session_state.selected_holding
    allow_insecure_ssl = st.session_state.get("allow_insecure_ssl", False)

    # Determine time range (default stored in session; selector shown near the chart)
    range_label = st.session_state.get("range_label", "1 Month")

    if not symbol:
        st.warning("Enter a valid ticker symbol to load market data.")
        return

    render_notes_panel(symbol)

    # If watchlist selected, render preview on the right; otherwise render holding
    if is_watchlist_selected:
        try:
            history, info, news_items, bundle_note = load_stock_bundle_resilient(
                symbol,
                range_label,
                allow_insecure_ssl,
                force_reload=st.session_state.get("last_refresh"),
            )
            current_price = safe_number(history.iloc[-1]["Close"])
            company_name = pick_value(info, "longName", "shortName") or symbol
            sector = pick_value(info, "sector") or "N/A"
            industry = pick_value(info, "industry") or "N/A"
            website = pick_value(info, "website")

            st.markdown("### 📊 Watchlist Preview")
            header_left, header_mid, header_right = st.columns([2.5, 1, 1])
            with header_left:
                st.subheader(company_name)
                st.caption(f"Sector: {sector} | Industry: {industry}")
            with header_mid:
                st.link_button("📊 Yahoo Finance", f"https://finance.yahoo.com/quote/{symbol}", use_container_width=True)
            with header_right:
                if website:
                    st.link_button("🌐 Company", website, use_container_width=True)
                currency_mode = st.session_state.get("currency_mode", "USD")
                st.caption(f"Displayed in {currency_mode}")
            if bundle_note:
                st.warning(bundle_note)

            # Show chart
            preview_holding = {"broker": "RBC", "quantity": None, "purchase_price_usd": None, "purchase_price_cad": None, "must_sell": None, "reasonable_lower": None, "reasonable_upper": None}
            st.plotly_chart(build_price_chart(history, symbol, range_label, preview_holding), use_container_width=True)

            # Show tabs
            summary_tab, fundamentals_tab, alerts_tab, news_tab = st.tabs(["Summary", "Fundamentals", "Alerts", "News"])
            with summary_tab:
                summary = pick_value(info, "longBusinessSummary")
                if summary:
                    st.write(summary)
                else:
                    st.info("No business summary is available for this ticker.")

            with fundamentals_tab:
                render_fundamentals(info)

            with alerts_tab:
                pending_notifications = render_alert_manager(symbol)
                if pending_notifications:
                    outcomes = evaluate_alerts(
                        allow_insecure_ssl=allow_insecure_ssl,
                    )
                    if outcomes:
                        for outcome in outcomes:
                            if "alert sent" in outcome.lower() or "notification" in outcome.lower():
                                st.success(outcome)
                            else:
                                st.warning(outcome)
                    else:
                        st.info("No alerts were triggered during this scan.")

            with news_tab:
                render_news(news_items)

        except Exception as error:
            st.error(f"Unable to fetch data for {symbol}: {error}")
        return

    try:
        history, info, news_items, bundle_note = load_stock_bundle_resilient(
            symbol,
            range_label,
            allow_insecure_ssl,
            force_reload=st.session_state.get("last_refresh"),
        )
    except Exception as error:
        if is_rate_limited_error(error):
            st.error(
                f"Data provider rate limit reached for {symbol}. Please wait a minute and try Refresh Now."
            )
        else:
            st.error(f"Unable to load data for {symbol}: {error}")
        if "CERTIFICATE_VERIFY_FAILED" in str(error) and not allow_insecure_ssl:
            st.info("Certificate verification failed; consider disabling SSL verification in a secure environment.")
        return

    if bundle_note:
        st.warning(bundle_note)

    company_name = pick_value(info, "longName", "shortName") or symbol
    sector = pick_value(info, "sector") or "N/A"
    industry = pick_value(info, "industry") or "N/A"
    website = pick_value(info, "website")

    header_left, header_mid, header_right = st.columns([2.5, 1, 1])
    with header_left:
        st.subheader(company_name)
        st.caption(f"Sector: {sector} | Industry: {industry}")
    with header_mid:
        st.link_button("📊 Yahoo Finance", f"https://finance.yahoo.com/quote/{symbol}", use_container_width=True)
    with header_right:
        if website:
            st.link_button("🌐 Company", website, use_container_width=True)
        # Show which currency the UI is currently displaying
        display_currency = st.session_state.get("currency_mode", "USD")
        st.caption(f"Displayed in {display_currency}")

    selected_holding = get_holding(symbol)
    current_price = safe_number(history.iloc[-1]["Close"])
    
    # Time range selector placed to the right of the chart
    range_keys = list(INTERVAL_OPTIONS.keys())
    try:
        range_idx = range_keys.index(range_label)
    except ValueError:
        range_idx = 2
    col_chart, col_range = st.columns([3, 1])
    with col_range:
        # Compact horizontal selector using a slider for a cleaner UI
        selected_range = st.select_slider(
            "",
            options=range_keys,
            value=range_label,
            key="range_selector_slider",
        )
        if selected_range != range_label:
            st.session_state["range_label"] = selected_range
            st.rerun()

    # Display chart first
    st.plotly_chart(build_price_chart(history, symbol, range_label, selected_holding), use_container_width=True)
    
    # Display metrics below chart
    render_metric_cards(info, history, selected_holding, current_price)

    summary_tab, fundamentals_tab, alerts_tab, news_tab = st.tabs(["Summary", "Fundamentals", "Alerts", "News"])
    with summary_tab:
        summary = pick_value(info, "longBusinessSummary")
        if summary:
            st.write(summary)
        else:
            st.info("No business summary is available for this ticker.")

    with fundamentals_tab:
        render_fundamentals(info)

    with alerts_tab:
        pending_notifications = render_alert_manager(symbol)
        if pending_notifications:
            outcomes = evaluate_alerts(
                allow_insecure_ssl=allow_insecure_ssl,
            )
            if outcomes:
                for outcome in outcomes:
                    if "alert sent" in outcome.lower() or "notification" in outcome.lower():
                        st.success(outcome)
                    else:
                        st.warning(outcome)
            else:
                st.info("No alerts were triggered during this scan.")

    with news_tab:
        render_news(news_items)

    last_timestamp = history.iloc[-1][history.columns[0]]
    if isinstance(last_timestamp, datetime):
        last_updated = last_timestamp.strftime("%Y-%m-%d %H:%M")
    else:
        last_updated = str(last_timestamp)
    st.caption(f"Latest candle in chart: {last_updated}")


if __name__ == "__main__":
    main()