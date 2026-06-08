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
AUTH_SESSION_FILE = os.path.expanduser("~/.shellstock_auth.json")
AUTH_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60


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
AUTO_REFRESH_INTERVAL_SECONDS = 60
AUTO_REFRESH_DURATION_SECONDS = 600

RANGE_FETCH_TTL_SECONDS = {
    "1 Day": 300,
    "1 Week": 600,
    "1 Month": 900,
    "3 Months": 1200,
    "6 Months": 1800,
    "YTD": 1800,
    "1 Year": 2400,
    "5 Years": 3600,
    "10 Years": 5400,
}

EASTERN_TZ = pytz.timezone("US/Eastern")


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
    now_ts = int(time.time())
    expires_at = now_ts + AUTH_SESSION_TTL_SECONDS

    try:
        with open(AUTH_SESSION_FILE, "w") as f:
            json.dump(
                {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "user_id": user_id,
                    "user_email": user_email,
                    "saved_at": now_ts,
                    "expires_at": expires_at,
                },
                f,
                indent=2,
            )
    except Exception:
        pass

    return True


def _clear_auth_session() -> None:
    st.session_state.pop("supabase_access_token", None)
    st.session_state.pop("supabase_refresh_token", None)
    st.session_state.pop("supabase_user_id", None)
    st.session_state.pop("supabase_user_email", None)
    try:
        if os.path.exists(AUTH_SESSION_FILE):
            os.remove(AUTH_SESSION_FILE)
    except Exception:
        pass


def _restore_auth_session() -> bool:
    """Restore persisted auth tokens so login survives app/browser restarts until sign out."""
    if not _supabase_configured():
        return False

    if _supabase_user_id():
        return True

    if not os.path.exists(AUTH_SESSION_FILE):
        return False

    try:
        with open(AUTH_SESSION_FILE, "r") as f:
            saved = json.load(f)
    except Exception:
        return False

    access_token = saved.get("access_token")
    refresh_token = saved.get("refresh_token")
    if not (access_token and refresh_token):
        return False

    now_ts = int(time.time())
    expires_at = saved.get("expires_at")
    if expires_at is None:
        # Backward compatibility: infer expiry from file mtime for older saved sessions.
        try:
            inferred_saved_at = int(os.path.getmtime(AUTH_SESSION_FILE))
        except Exception:
            inferred_saved_at = now_ts
        expires_at = inferred_saved_at + AUTH_SESSION_TTL_SECONDS

    try:
        expires_at_int = int(expires_at)
    except Exception:
        _clear_auth_session()
        return False

    if now_ts >= expires_at_int:
        _clear_auth_session()
        return False

    st.session_state.supabase_access_token = access_token
    st.session_state.supabase_refresh_token = refresh_token
    st.session_state.supabase_user_id = saved.get("user_id")
    st.session_state.supabase_user_email = saved.get("user_email")

    client = _get_authenticated_supabase_client()
    if client is None:
        _clear_auth_session()
        return False

    try:
        response = client.auth.get_user()
        user = getattr(response, "user", None)
        user_id = getattr(user, "id", None)
        if not user_id:
            _clear_auth_session()
            return False
        st.session_state.supabase_user_id = user_id
        st.session_state.supabase_user_email = getattr(user, "email", "")
        return True
    except Exception:
        _clear_auth_session()
        return False


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
        normalized_holdings: dict[str, dict[str, Any]] = {}

        if isinstance(persisted_holdings, dict):
            for raw_key, raw_holding in persisted_holdings.items():
                if not isinstance(raw_holding, dict):
                    continue

                holding = dict(raw_holding)
                symbol_from_value = _symbol_from_holding_key(holding.get("symbol", ""))
                symbol_from_key = _symbol_from_holding_key(raw_key)
                if symbol_from_value and _looks_like_ticker_symbol(symbol_from_value):
                    normalized_symbol = symbol_from_value
                else:
                    normalized_symbol = symbol_from_key
                if not normalized_symbol:
                    continue

                if "purchase_price" in holding and "purchase_price_usd" not in holding:
                    holding["purchase_price_usd"] = holding.get("purchase_price")
                    holding.pop("purchase_price", None)

                holding.setdefault("purchase_price_usd", None)
                holding.setdefault("purchase_price_cad", None)
                if "purchase_currency" not in holding:
                    if holding.get("purchase_price_cad") is not None and holding.get("purchase_price_usd") is None:
                        holding["purchase_currency"] = "CAD"
                    else:
                        holding["purchase_currency"] = "USD"

                holding.setdefault("must_sell", None)
                holding.setdefault("reasonable_lower", None)
                holding.setdefault("reasonable_upper", None)
                holding.setdefault("broker", "RBC")
                holding.setdefault("quantity", None)
                holding["symbol"] = normalized_symbol

                raw_key_str = str(raw_key)
                base_symbol = _symbol_from_holding_key(raw_key_str)
                _, _, suffix = raw_key_str.rpartition("__")
                is_valid_lot_id = bool(base_symbol and _looks_like_ticker_symbol(base_symbol) and suffix.isdigit())

                # Preserve only real lot IDs like AAPL__1; remap everything else.
                if is_valid_lot_id and raw_key_str not in normalized_holdings:
                    holding_id = raw_key_str
                else:
                    holding_id = _next_holding_id(normalized_symbol, set(normalized_holdings.keys()))

                normalized_holdings[holding_id] = holding

        st.session_state.holdings = normalized_holdings
    
    if "selected_holding" not in st.session_state:
        persisted_selected_holding = str(get_persistent_data_key("selected_holding", "") or "")
        holdings = st.session_state.holdings
        selected_holding_id = ""
        if persisted_selected_holding in holdings:
            selected_holding_id = persisted_selected_holding
        else:
            selected_symbol = sanitize_symbol(persisted_selected_holding)
            if selected_symbol:
                for hid, holding in holdings.items():
                    if sanitize_symbol(str(holding.get("symbol", ""))) == selected_symbol:
                        selected_holding_id = hid
                        break
        st.session_state.selected_holding = selected_holding_id
    
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

    if "auto_refresh_enabled" not in st.session_state:
        st.session_state.auto_refresh_enabled = False

    if "auto_refresh_until" not in st.session_state:
        st.session_state.auto_refresh_until = None


def safe_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value)
    return None


def format_timestamp_et(value: Any, include_seconds: bool = False) -> str:
    """Format timestamps in US/Eastern so UI time labels are consistent."""
    fmt = "%Y-%m-%d %H:%M:%S %Z" if include_seconds else "%Y-%m-%d %H:%M %Z"
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except Exception:
        ts = pd.NaT

    if pd.isna(ts):
        return datetime.now(EASTERN_TZ).strftime(fmt)

    try:
        if getattr(ts, "tzinfo", None) is None:
            return ts.strftime(fmt)
        return ts.tz_convert(EASTERN_TZ).strftime(fmt)
    except Exception:
        return datetime.now(EASTERN_TZ).strftime(fmt)


def get_current_time_display() -> str:
    """Get current time in Eastern and PST timezones."""
    eastern = EASTERN_TZ
    pacific = pytz.timezone('US/Pacific')
    
    now_eastern = datetime.now(eastern)
    now_pacific = datetime.now(pacific)
    
    eastern_str = now_eastern.strftime('%I:%M %p %Z')
    pacific_str = now_pacific.strftime('%I:%M %p %Z')
    
    return f"{eastern_str} | {pacific_str}"


@st.cache_data(ttl=180, show_spinner=False)
def get_usd_cad_rate() -> float:
    """Get real-time USD to CAD exchange rate."""
    info = get_usd_cad_rate_info()
    return info.get("rate", 1.35)


@st.cache_data(ttl=180, show_spinner=False)
def get_usd_cad_rate_info() -> dict:
    """Return USD->CAD rate and timestamp from Yahoo Finance."""
    try:
        ticker = yf.Ticker("CAD=X")
        # Try intraday history first
        hist = ticker.history(period="1d", interval="1m", auto_adjust=False)
        if hist is not None and not hist.empty:
            last_idx = hist.index[-1]
            last_close = float(hist.iloc[-1]["Close"])
            ts_str = format_timestamp_et(last_idx)
            result = {"rate": last_close, "timestamp": ts_str}
            set_persistent_data_key("last_fx_info", result)
            return result

        # Fallback to ticker.info
        info = ticker.info or {}
        current_rate = info.get("currentPrice") or info.get("regularMarketPrice")
        if current_rate:
            result = {"rate": float(current_rate), "timestamp": format_timestamp_et(datetime.now(EASTERN_TZ))}
            set_persistent_data_key("last_fx_info", result)
            return result
    except Exception:
        pass

    # Fallback to last successful FX quote if Yahoo is rate-limited/unavailable.
    cached_fx = get_persistent_data_key("last_fx_info", None)
    if isinstance(cached_fx, dict) and "rate" in cached_fx:
        cached_ts = cached_fx.get("timestamp")
        if cached_ts:
            cached_fx["timestamp"] = format_timestamp_et(cached_ts)
        return cached_fx

    return {"rate": 1.35, "timestamp": "N/A"}


def is_rate_limited_error(error: Any) -> bool:
    text = str(error).lower()
    return (
        "too many requests" in text
        or "rate limit" in text
        or "429" in text
        or "cooldown active" in text
    )


def get_transaction_fee(broker: str, stock_currency: str = "USD") -> dict[str, Any]:
    """Get transaction fee percentage for each broker.

    Returns structured fee data so callers can support percent or flat fees.
    """
    # Return structured fee data so callers can support percent or flat fees
    if broker == "WealthSimple":
        return {"type": "percent", "value": 0.015}
    elif broker == "RBC":
        # RBC charges a flat $9.95 in the trading currency (CAD for Canadian stocks, USD for U.S. stocks).
        normalized_currency = str(stock_currency or "USD").upper()
        fee_currency = "CAD" if normalized_currency == "CAD" else "USD"
        return {"type": "flat", "value": 9.95, "currency": fee_currency}
    return {"type": "percent", "value": 0.0}


def calculate_estimated_sale_price(
    current_price: float | None,
    quantity: float | None,
    broker: str,
    fx_rate: float = 1.0,
    display_currency: str = "USD",
    stock_currency: str = "USD",
) -> dict[str, Any]:
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

    fee_info = get_transaction_fee(broker, stock_currency=stock_currency)
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


def detect_stock_currency_from_yahoo_info(info: dict[str, Any]) -> str:
    """Infer stock trading currency from Yahoo metadata, independent of UI display currency."""
    primary_currency = str(pick_value(info, "currency", "financialCurrency") or "").strip().upper()
    if primary_currency in ("USD", "CAD"):
        return primary_currency

    exchange_hint = str(pick_value(info, "fullExchangeName", "exchange", "market") or "").lower()
    if "toronto" in exchange_hint or "tsx" in exchange_hint or "canada" in exchange_hint:
        return "CAD"

    return "USD"


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


def _default_holding(symbol: str) -> dict[str, Any]:
    return {
        "symbol": sanitize_symbol(symbol),
        "must_sell": None,
        "purchase_price_usd": None,
        "purchase_price_cad": None,
        "purchase_currency": "USD",
        "reasonable_lower": None,
        "reasonable_upper": None,
        "broker": "RBC",
        "quantity": None,
    }


def _next_holding_id(symbol: str, existing_ids: set[str]) -> str:
    base = f"{sanitize_symbol(symbol)}__"
    index = 1
    candidate = f"{base}{index}"
    while candidate in existing_ids:
        index += 1
        candidate = f"{base}{index}"
    return candidate


def _symbol_from_holding_key(raw_key: Any) -> str:
    raw_key_str = str(raw_key or "")
    if "__" in raw_key_str:
        base, _, suffix = raw_key_str.rpartition("__")
        if suffix.isdigit():
            return sanitize_symbol(base)
    return sanitize_symbol(raw_key_str)


def _looks_like_ticker_symbol(symbol: str) -> bool:
    return any(ch.isalpha() for ch in symbol)


def parse_optional_price(value: str) -> float | None:
    raw = value.strip()
    if not raw:
        return None
    return float(raw)


def create_holding(symbol: str) -> str:
    normalized_symbol = sanitize_symbol(symbol)
    if not normalized_symbol:
        raise ValueError("Ticker symbol cannot be empty.")
    holdings: dict[str, dict[str, Any]] = st.session_state.holdings
    holding_id = _next_holding_id(normalized_symbol, set(holdings.keys()))
    holdings[holding_id] = _default_holding(normalized_symbol)
    return holding_id


def get_holding(holding_id: str) -> dict[str, Any]:
    holdings: dict[str, dict[str, Any]] = st.session_state.holdings
    if not holding_id or holding_id not in holdings:
        raise ValueError("Holding ID is invalid.")
    return holdings[holding_id]


def get_holding_symbol(holding_id: str) -> str:
    holdings: dict[str, dict[str, Any]] = st.session_state.holdings
    holding = holdings.get(holding_id, {}) if isinstance(holdings, dict) else {}
    return sanitize_symbol(str(holding.get("symbol", "")))


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
    # Defer news fetches until the News tab is opened to reduce provider calls.
    return history.reset_index(), info, []


@st.cache_data(show_spinner=False, ttl=900)
def load_stock_news(
    symbol: str,
    allow_insecure_ssl: bool,
    force_reload: float | None = None,
) -> list[dict[str, Any]]:
    session = None
    if allow_insecure_ssl:
        session = create_insecure_session()

    ticker = yf.Ticker(symbol, session=session)
    news_items = ticker.news or []
    return news_items[:8]


def load_stock_news_resilient(
    symbol: str,
    allow_insecure_ssl: bool,
    force_reload: float | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    if "last_good_stock_news" not in st.session_state:
        st.session_state.last_good_stock_news = {}

    symbol_key = symbol.upper()
    provider_cooldown_remaining = get_provider_cooldown_remaining_seconds()
    if provider_cooldown_remaining > 0 and force_reload is None:
        cached_news = st.session_state.last_good_stock_news.get(symbol_key)
        if isinstance(cached_news, list):
            message = (
                f"Data provider cooldown active ({provider_cooldown_remaining}s remaining). "
                f"Showing cached news for {symbol_key}."
            )
            return cached_news, message
        raise ValueError(
            f"Data provider cooldown active for {provider_cooldown_remaining}s after rate limiting."
        )

    try:
        news_items = load_stock_news(symbol, allow_insecure_ssl, force_reload=force_reload)
        st.session_state.last_good_stock_news[symbol_key] = news_items
        return news_items, None
    except Exception as error:
        if is_rate_limited_error(error):
            activate_provider_cooldown()

        cached_news = st.session_state.last_good_stock_news.get(symbol_key)
        if isinstance(cached_news, list):
            return cached_news, f"Rate limited by data provider. Showing cached news for {symbol_key}."
        raise


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
    timestamp = format_timestamp_et(datetime.now(EASTERN_TZ), include_seconds=True)

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
            timestamp = format_timestamp_et(last_idx, include_seconds=True)

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


def get_live_quote_for_range(
    symbol: str,
    range_label: str,
    allow_insecure_ssl: bool,
    force_reload: float | None = None,
) -> tuple[float | None, str | None]:
    # 1 Day already uses intraday candles; additional quote merge is unnecessary.
    if range_label == "1 Day":
        return None, None

    try:
        quote, note = load_stock_quote_resilient(
            symbol,
            allow_insecure_ssl,
            force_reload=force_reload,
        )
        return safe_number(quote.get("price")), note
    except Exception:
        return None, None


def merge_live_quote_into_history(history: pd.DataFrame, live_price: float | None) -> pd.DataFrame:
    if history is None or history.empty or live_price is None:
        return history

    merged = history.copy()
    time_col = merged.columns[0]
    last_row = merged.iloc[-1].copy()

    now_ts = datetime.now()
    try:
        last_ts = pd.to_datetime(last_row[time_col], errors="coerce")
    except Exception:
        last_ts = pd.NaT

    should_append = True
    if pd.notna(last_ts):
        try:
            should_append = now_ts.date() > last_ts.date()
        except Exception:
            should_append = True

    if should_append:
        new_row = last_row.copy()
        new_row[time_col] = now_ts
        for col in ("Open", "High", "Low", "Close"):
            if col in merged.columns:
                new_row[col] = live_price
        if "Volume" in merged.columns:
            new_row["Volume"] = 0
        return pd.concat([merged, pd.DataFrame([new_row])], ignore_index=True)

    # Same-day bar exists: keep OHLC coherent while updating to latest live price.
    if "Close" in merged.columns:
        merged.at[merged.index[-1], "Close"] = live_price
    if "High" in merged.columns:
        current_high = safe_number(merged.iloc[-1]["High"])
        merged.at[merged.index[-1], "High"] = max(current_high if current_high is not None else live_price, live_price)
    if "Low" in merged.columns:
        current_low = safe_number(merged.iloc[-1]["Low"])
        merged.at[merged.index[-1], "Low"] = min(current_low if current_low is not None else live_price, live_price)

    return merged


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

    stock_currency = detect_stock_currency_from_yahoo_info(info)

    # Always compute estimated sale values in CAD using today's FX.
    sale_fx_info = get_usd_cad_rate_info()
    sale_fx_rate = float(sale_fx_info.get("rate", 1.35))
    sale_fx_timestamp = sale_fx_info.get("timestamp", "N/A")

    # Calculate estimated sale price (pass stock currency to pick correct flat-fee currency)
    sale_data = calculate_estimated_sale_price(
        current_price,
        quantity,
        broker,
        fx_rate=sale_fx_rate,
        display_currency="CAD",
        stock_currency=stock_currency,
    )
    
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
    if current_price is not None and quantity is not None:
        st.markdown("---")
        st.subheader("Your Returns")
        
        ret_col1, ret_col2 = st.columns(2)
        
        if today_return_pct is not None and today_delta is not None:
            with ret_col1:
                color = "green" if today_delta >= 0 else "red"
                return_text = f"+{today_return_pct:.2f}%" if today_delta >= 0 else f"{today_return_pct:.2f}%"
                delta_val = today_delta * fx_rate
                delta_text = f"+${delta_val:.2f} {currency_mode}" if today_delta >= 0 else f"${delta_val:.2f} {currency_mode}"
                st.metric(
                    f"Today's Return ({currency_mode})",
                    return_text,
                    delta=delta_text,
                    delta_color="off"
                )
        
        if total_return_pct is not None and total_delta is not None:
            with ret_col2:
                color = "green" if total_delta >= 0 else "red"
                return_text = f"+{total_return_pct:.2f}%" if total_delta >= 0 else f"{total_return_pct:.2f}%"
                delta_val = total_delta * fx_rate
                delta_text = f"+${delta_val:.2f} {currency_mode}" if total_delta >= 0 else f"${delta_val:.2f} {currency_mode}"
                st.metric(
                    f"Total Return ({currency_mode})",
                    return_text,
                    delta=delta_text,
                    delta_color="off"
                )

        # Show absolute current market value in both currencies side-by-side.
        market_value_usd = current_price * quantity
        market_value_cad = market_value_usd * sale_fx_rate
        mv_col1, mv_col2 = st.columns(2)
        with mv_col1:
            st.metric("Market Value (USD)", f"${market_value_usd:.2f} USD")
        with mv_col2:
            st.metric("Market Value (CAD)", f"${market_value_cad:.2f} CAD")
    
    # Estimated sale price section
    if quantity is not None and current_price is not None:
        st.markdown("---")
        st.subheader("Estimated Sale Price (CAD)")
        st.caption(f"USD/CAD used: {sale_fx_rate:.4f} (as of {sale_fx_timestamp})")
        
        sale_col1, sale_col2, sale_col3 = st.columns(3)
        
        with sale_col1:
            current_val = sale_data["current_total"] * sale_fx_rate if sale_data["current_total"] else 0
            st.metric(
                "Position Value",
                f"${current_val:.2f} CAD" if sale_data["current_total"] else "N/A"
            )
        with sale_col2:
            broker_name = "WealthSimple" if broker == "WealthSimple" else "RBC"
            fee_type = sale_data.get("fee_type")
            fee_val = 0
            # Percent fee
            if fee_type == "percent":
                fee_percent = sale_data.get("fee_percent") or 0
                fee_text = f"{broker_name} Fee ({fee_percent*100:.2f}%)"
                fee_val = sale_data.get("fee_amount", 0) * sale_fx_rate if sale_data.get("fee_amount") else 0
            else:
                # Flat fee
                fee_currency = sale_data.get("fee_currency", "USD")
                fee_value = sale_data.get("fee_value", 0)
                if fee_currency == "CAD":
                    fee_text = f"{broker_name} Fee ({format_currency(fee_value, 'CAD')})"
                    fee_val = fee_value
                else:
                    fee_text = f"{broker_name} Fee ({format_currency(fee_value * sale_fx_rate, 'CAD')})"
                    fee_val = fee_value * sale_fx_rate

            st.metric(fee_text, f"${fee_val:.2f} CAD" if fee_val else "N/A")
        with sale_col3:
            proceeds_val = sale_data["estimated_proceeds"] * sale_fx_rate if sale_data["estimated_proceeds"] else 0
            st.metric(
                "Proceeds (After Fee)",
                f"${proceeds_val:.2f} CAD" if sale_data["estimated_proceeds"] else "N/A"
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

    holdings: dict[str, dict[str, Any]] = st.session_state.holdings
    holding_ids = sorted(
        holdings.keys(),
        key=lambda hid: (
            sanitize_symbol(str(holdings.get(hid, {}).get("symbol", ""))),
            hid,
        ),
    )

    current_selected = st.session_state.get("selected_holding", "")
    if holding_ids:
        if current_selected not in holdings:
            current_selected = ""
            st.session_state.selected_holding = current_selected
    else:
        st.session_state.selected_holding = ""

    currency_mode = st.session_state.get("currency_mode", "USD")
    fx_rate = get_usd_cad_rate() if currency_mode == "CAD" else 1.0

    symbol_totals: dict[str, int] = {}
    for holding_id in holding_ids:
        symbol = sanitize_symbol(str(holdings.get(holding_id, {}).get("symbol", "")))
        if symbol:
            symbol_totals[symbol] = symbol_totals.get(symbol, 0) + 1

    symbol_counts: dict[str, int] = {}
    holdings_data = []
    for holding_id in holding_ids:
        holding = holdings[holding_id]
        symbol = sanitize_symbol(str(holding.get("symbol", "")))
        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        if symbol_totals.get(symbol, 0) > 1:
            symbol_lot_label = f"{symbol} #{symbol_counts[symbol]}"
        else:
            symbol_lot_label = symbol

        quantity = safe_number(holding.get("quantity"))
        purchase_price_usd = safe_number(holding.get("purchase_price_usd"))
        purchase_price_cad = safe_number(holding.get("purchase_price_cad"))

        base_purchase_usd = None
        if purchase_price_usd is not None:
            base_purchase_usd = purchase_price_usd
        elif purchase_price_cad is not None and fx_rate and fx_rate > 0:
            base_purchase_usd = purchase_price_cad / fx_rate

        book_cost = None
        if quantity and base_purchase_usd:
            book_cost = quantity * base_purchase_usd

        holdings_data.append(
            {
                "holding_id": holding_id,
                "symbol": symbol,
                "symbol_lot_label": symbol_lot_label,
                "quantity": quantity,
                "purchase_price_usd": purchase_price_usd,
                "purchase_price_cad": purchase_price_cad,
                "book_cost": book_cost,
                "broker": holding.get("broker", "RBC"),
            }
        )

    if not holdings_data:
        st.info("No holdings yet. Add a ticker below.")
    else:
        cols = st.columns([1.2, 1.5, 1.5, 2, 1.5, 1])
        with cols[0]:
            st.write("**Holding**")
        with cols[1]:
            st.write("**Quantity**")
        with cols[2]:
            st.write(f"**Purchase Price ({currency_mode})**")
        with cols[3]:
            st.write(f"**Book Cost ({currency_mode})**")
        with cols[4]:
            st.write("**Broker**")
        with cols[5]:
            st.write("**Action**")

        for holding_data in holdings_data:
            cols = st.columns([1.2, 1.5, 1.5, 2, 1.5, 1])
            with cols[0]:
                if st.button(
                    holding_data["symbol_lot_label"],
                    key=f"select_{holding_data['holding_id']}",
                    use_container_width=True,
                ):
                    st.session_state.selected_holding = holding_data["holding_id"]
                    st.session_state.selected_watchlist_symbol = ""
                    set_persistent_data_key("selected_holding", holding_data["holding_id"])
                    st.rerun()
            with cols[1]:
                qty_str = f"{holding_data['quantity']:.4f}" if holding_data["quantity"] else "-"
                st.write(qty_str)
            with cols[2]:
                price_str = "-"
                if currency_mode == "CAD":
                    if holding_data.get("purchase_price_cad") is not None:
                        price_str = f"${holding_data.get('purchase_price_cad'):.2f}"
                    elif holding_data.get("purchase_price_usd") is not None:
                        price_display = holding_data.get("purchase_price_usd") * fx_rate
                        price_str = f"${price_display:.2f}"
                else:
                    if holding_data.get("purchase_price_usd") is not None:
                        price_str = f"${holding_data.get('purchase_price_usd'):.2f}"
                    elif holding_data.get("purchase_price_cad") is not None and fx_rate and fx_rate > 0:
                        price_display = holding_data.get("purchase_price_cad") / fx_rate
                        price_str = f"${price_display:.2f}"
                st.write(price_str)
            with cols[3]:
                if holding_data["book_cost"]:
                    cost_display = holding_data["book_cost"] * fx_rate
                    cost_str = f"${cost_display:.2f}"
                else:
                    cost_str = "-"
                st.write(cost_str)
            with cols[4]:
                st.write(holding_data["broker"])
            with cols[5]:
                remove_id = holding_data["holding_id"]
                if st.button("X", key=f"remove_holding_{remove_id}", use_container_width=True):
                    removed_label = holding_data["symbol_lot_label"]
                    holdings.pop(remove_id, None)
                    if holdings:
                        if st.session_state.selected_holding == remove_id:
                            st.session_state.selected_holding = sorted(holdings.keys())[0]
                    else:
                        st.session_state.selected_holding = ""
                    set_persistent_data_key("holdings", st.session_state.holdings)
                    set_persistent_data_key("selected_holding", st.session_state.selected_holding)
                    st.warning(f"Removed {removed_label} from holdings.")
                    st.rerun()

    st.divider()

    st.markdown("### Add New Holding")
    new_symbol = st.text_input("Holding Ticker", placeholder="e.g., AAPL or MSFT").strip().upper()

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
        holding_id = create_holding(new_symbol)
        st.session_state.selected_holding = holding_id
        st.session_state.selected_watchlist_symbol = ""
        set_persistent_data_key("selected_holding", holding_id)
        set_persistent_data_key("holdings", st.session_state.holdings)
        symbol = get_holding_symbol(holding_id)
        st.success(f"✓ Added {symbol} as a new holding entry.")
        st.rerun()

    if add_to_watchlist:
        if any(w.get("symbol") == new_symbol for w in st.session_state.watchlist):
            st.info(f"{new_symbol} is already in your watchlist.")
        else:
            st.session_state.watchlist.append({"symbol": new_symbol, "note": ""})
            set_persistent_data_key("watchlist", st.session_state.watchlist)
            st.success(f"✓ Added {new_symbol} to watchlist.")
            st.session_state.selected_watchlist_symbol = new_symbol
            st.rerun()

    selected_holding_id = str(st.session_state.get("selected_holding", "") or "")
    if not selected_holding_id or selected_holding_id not in holdings:
        st.caption("Select a holding row above to edit details.")
        return st.session_state.selected_holding

    selected_holding = get_holding(selected_holding_id)
    selected_symbol = sanitize_symbol(str(selected_holding.get("symbol", "")))
    currency_mode = st.session_state.get("currency_mode", "USD")
    fx_rate = get_usd_cad_rate() if currency_mode == "CAD" else 1.0

    st.markdown("---")
    st.markdown(f"### Edit: {selected_symbol}")

    default_purchase_currency = selected_holding.get("purchase_currency", "USD")
    if default_purchase_currency not in ("USD", "CAD"):
        default_purchase_currency = "USD"
    purchase_currency_key = f"purchase_currency_select_{selected_holding_id}"
    if purchase_currency_key not in st.session_state:
        st.session_state[purchase_currency_key] = default_purchase_currency
    purchase_currency = st.selectbox(
        "Purchase Currency",
        options=["USD", "CAD"],
        key=purchase_currency_key,
        help="Choose the currency you originally purchased in. The other currency is calculated using today's FX rate.",
    )

    with st.form("holding-details-form"):
        broker = st.selectbox(
            "Broker",
            options=["RBC", "WealthSimple"],
            index=0 if selected_holding.get("broker", "RBC") == "RBC" else 1,
        )

        quantity_val = st.number_input(
            "Quantity",
            min_value=0.0,
            value=selected_holding.get("quantity") or 0.0,
            step=0.0001,
            format="%.4f",
        )

        stored_usd = selected_holding.get("purchase_price_usd")
        stored_cad = selected_holding.get("purchase_price_cad")
        purchase_fx_rate = get_usd_cad_rate()
        purchase_price_usd_default = stored_usd if stored_usd is not None else (stored_cad / purchase_fx_rate if stored_cad is not None and purchase_fx_rate and purchase_fx_rate > 0 else 0.0)
        purchase_price_cad_default = stored_cad if stored_cad is not None else (stored_usd * purchase_fx_rate if stored_usd is not None else 0.0)

        if purchase_currency == "USD":
            purchase_price_usd_val = st.number_input(
                "Purchase Price (USD)",
                min_value=0.0,
                value=float(purchase_price_usd_default),
                step=0.01,
                help="Editable purchase price in USD.",
            )
            computed_cad = purchase_price_usd_val * purchase_fx_rate if purchase_price_usd_val > 0 else 0.0
            purchase_price_cad_val = st.number_input(
                "Purchase Price (CAD, auto)",
                min_value=0.0,
                value=float(computed_cad),
                step=0.01,
                disabled=True,
                help="Auto-calculated from USD using today's USD/CAD rate.",
            )
        else:
            purchase_price_cad_val = st.number_input(
                "Purchase Price (CAD)",
                min_value=0.0,
                value=float(purchase_price_cad_default),
                step=0.01,
                help="Editable purchase price in CAD.",
            )
            computed_usd = (purchase_price_cad_val / purchase_fx_rate) if (purchase_price_cad_val > 0 and purchase_fx_rate and purchase_fx_rate > 0) else 0.0
            purchase_price_usd_val = st.number_input(
                "Purchase Price (USD, auto)",
                min_value=0.0,
                value=float(computed_usd),
                step=0.01,
                disabled=True,
                help="Auto-calculated from CAD using today's USD/CAD rate.",
            )

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
            value="" if reasonable_lower_display is None else f"{reasonable_lower_display:.2f}",
            help="Leave blank to clear.",
        )

        reasonable_upper_stored = selected_holding.get("reasonable_upper")
        reasonable_upper_display = reasonable_upper_stored * fx_rate if reasonable_upper_stored and fx_rate != 1.0 else reasonable_upper_stored
        reasonable_upper_text = st.text_input(
            f"Reasonable Upper Limit ({currency_mode})",
            value="" if reasonable_upper_display is None else f"{reasonable_upper_display:.2f}",
            help="Leave blank to clear.",
        )
        save_holding_details = st.form_submit_button("Save holding details", use_container_width=True)

    if save_holding_details:
        try:
            updated = get_holding(selected_holding_id)
            updated["symbol"] = selected_symbol
            updated["broker"] = broker
            updated["quantity"] = quantity_val if quantity_val > 0 else None
            updated["purchase_currency"] = purchase_currency
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
        removed = selected_holding_id
        holdings.pop(removed, None)
        st.session_state.selected_holding = ""
        set_persistent_data_key("holdings", st.session_state.holdings)
        set_persistent_data_key("selected_holding", st.session_state.selected_holding)
        st.warning("Removed selected holding entry.")
        st.rerun()

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
    _restore_auth_session()

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
                        st.session_state["auto_refresh_until"] = time.time() + float(
                            st.session_state.get("auto_refresh_duration", AUTO_REFRESH_DURATION_SECONDS)
                        )
                    st.rerun()
            with rcol2:
                auto_enabled = st.checkbox("Auto", value=st.session_state.get("auto_refresh_enabled", False), help="Enable auto-refresh after pressing Refresh Now")
                st.session_state["auto_refresh_enabled"] = auto_enabled

            # Auto-refresh duration stored in session; no direct user entry here
            if "auto_refresh_duration" not in st.session_state:
                st.session_state["auto_refresh_duration"] = AUTO_REFRESH_DURATION_SECONDS

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
                refresh_interval = AUTO_REFRESH_INTERVAL_SECONDS
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
    selected_holding_id = str(st.session_state.get("selected_holding", "") or "")
    selected_holding_symbol = get_holding_symbol(selected_holding_id)
    symbol = st.session_state.get("selected_watchlist_symbol") or selected_holding_symbol
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
            history, info, _, bundle_note = load_stock_bundle_resilient(
                symbol,
                range_label,
                allow_insecure_ssl,
                force_reload=st.session_state.get("last_refresh"),
            )
            live_quote_price, live_quote_note = get_live_quote_for_range(
                symbol,
                range_label,
                allow_insecure_ssl,
                force_reload=st.session_state.get("last_refresh"),
            )
            history = merge_live_quote_into_history(history, live_quote_price)
            current_price = live_quote_price if live_quote_price is not None else safe_number(history.iloc[-1]["Close"])
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
            if live_quote_note:
                st.caption(live_quote_note)

            # Time range selector placed to the right of the watchlist chart
            range_keys = list(INTERVAL_OPTIONS.keys())
            col_chart, col_range = st.columns([3, 1])
            with col_range:
                selected_range = st.select_slider(
                    "",
                    options=range_keys,
                    value=range_label,
                    key="watchlist_range_selector_slider",
                )
                if selected_range != range_label:
                    st.session_state["range_label"] = selected_range
                    st.rerun()

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
                try:
                    news_items, news_note = load_stock_news_resilient(
                        symbol,
                        allow_insecure_ssl,
                        force_reload=st.session_state.get("last_refresh"),
                    )
                    if news_note:
                        st.caption(news_note)
                    render_news(news_items)
                except Exception as error:
                    if is_rate_limited_error(error):
                        st.warning("News is temporarily rate-limited. Try again shortly.")
                    else:
                        st.warning(f"Unable to load news: {error}")

        except Exception as error:
            if is_rate_limited_error(error):
                st.warning(
                    f"Data provider is temporarily rate-limiting requests for {symbol}. "
                    "Showing quote-only fallback when available."
                )
                try:
                    fallback_quote, fallback_note = load_stock_quote_resilient(
                        symbol,
                        allow_insecure_ssl,
                        force_reload=st.session_state.get("last_refresh"),
                    )
                    fallback_price = safe_number(fallback_quote.get("price"))
                    fallback_ts = fallback_quote.get("timestamp", "N/A")
                    if fallback_price is not None:
                        currency_mode = st.session_state.get("currency_mode", "USD")
                        if currency_mode == "CAD":
                            fx_rate = get_usd_cad_rate()
                            display_price = fallback_price * fx_rate
                        else:
                            display_price = fallback_price
                        st.metric("Latest Quote", f"${display_price:.2f} {currency_mode}")
                        st.caption(f"Quote timestamp: {fallback_ts}")
                    if fallback_note:
                        st.caption(fallback_note)
                except Exception:
                    cooldown_remaining = get_provider_cooldown_remaining_seconds()
                    if cooldown_remaining > 0:
                        st.info(
                            f"Provider cooldown in effect for about {cooldown_remaining}s. "
                            "Try Refresh Now after cooldown expires."
                        )
                    else:
                        st.info("Quote fallback is temporarily unavailable. Try again in a moment.")
                st.link_button("Open in Yahoo Finance", f"https://finance.yahoo.com/quote/{symbol}")
            else:
                st.error(f"Unable to fetch data for {symbol}: {error}")
        return

    try:
        history, info, _, bundle_note = load_stock_bundle_resilient(
            symbol,
            range_label,
            allow_insecure_ssl,
            force_reload=st.session_state.get("last_refresh"),
        )
    except Exception as error:
        if is_rate_limited_error(error):
            st.warning(
                f"Data provider is temporarily rate-limiting requests for {symbol}. "
                "Showing quote-only fallback when available."
            )
            try:
                fallback_quote, fallback_note = load_stock_quote_resilient(
                    symbol,
                    allow_insecure_ssl,
                    force_reload=st.session_state.get("last_refresh"),
                )
                fallback_price = safe_number(fallback_quote.get("price"))
                fallback_ts = fallback_quote.get("timestamp", "N/A")
                if fallback_price is not None:
                    currency_mode = st.session_state.get("currency_mode", "USD")
                    if currency_mode == "CAD":
                        fx_rate = get_usd_cad_rate()
                        display_price = fallback_price * fx_rate
                    else:
                        display_price = fallback_price
                    st.metric("Latest Quote", f"${display_price:.2f} {currency_mode}")
                    st.caption(f"Quote timestamp: {fallback_ts}")
                if fallback_note:
                    st.caption(fallback_note)
            except Exception:
                cooldown_remaining = get_provider_cooldown_remaining_seconds()
                if cooldown_remaining > 0:
                    st.info(
                        f"Provider cooldown in effect for about {cooldown_remaining}s. "
                        "Try Refresh Now after cooldown expires."
                    )
                else:
                    st.info("Quote fallback is temporarily unavailable. Try again in a moment.")
            st.link_button("Open in Yahoo Finance", f"https://finance.yahoo.com/quote/{symbol}")
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

    selected_holding = get_holding(selected_holding_id)
    live_quote_price, live_quote_note = get_live_quote_for_range(
        symbol,
        range_label,
        allow_insecure_ssl,
        force_reload=st.session_state.get("last_refresh"),
    )
    history = merge_live_quote_into_history(history, live_quote_price)
    current_price = live_quote_price if live_quote_price is not None else safe_number(history.iloc[-1]["Close"])
    if live_quote_note:
        st.caption(live_quote_note)
    
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
        try:
            news_items, news_note = load_stock_news_resilient(
                symbol,
                allow_insecure_ssl,
                force_reload=st.session_state.get("last_refresh"),
            )
            if news_note:
                st.caption(news_note)
            render_news(news_items)
        except Exception as error:
            if is_rate_limited_error(error):
                st.warning("News is temporarily rate-limited. Try again shortly.")
            else:
                st.warning(f"Unable to load news: {error}")

    last_timestamp = history.iloc[-1][history.columns[0]]
    last_updated = format_timestamp_et(last_timestamp)
    st.caption(f"Latest candle in chart (ET): {last_updated}")


if __name__ == "__main__":
    main()