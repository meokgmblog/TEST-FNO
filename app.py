import os
import time
import urllib.parse
from datetime import datetime, timedelta
import zoneinfo
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st


# ==========================================
# PAGE CONFIGURATION
# ==========================================
st.set_page_config(
    page_title="F&O Institutional Sector Radar",
    layout="wide",
    initial_sidebar_state="collapsed"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FNO_EXCEL_PATH = os.path.join(BASE_DIR, "FNO all list.xlsx")
INSTRUMENTS_CSV_PATH = os.path.join(BASE_DIR, "instruments.csv")

# Keep your Upstox token in Streamlit Secrets.
# Local .streamlit/secrets.toml:
# ACCESS_TOKEN = "YOUR_UPSTOX_ACCESS_TOKEN"
ACCESS_TOKEN = st.secrets.get("ACCESS_TOKEN", "")

REFRESH_INTERVAL_SECONDS = 30

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# File Existence Guard
if not os.path.exists(FNO_EXCEL_PATH) or not os.path.exists(INSTRUMENTS_CSV_PATH):
    st.error("⚠️ **Required Data Files Missing!**")
    st.info(f"Looking in folder: `{BASE_DIR}`")
    st.stop()


# ==========================================
# FORMATTING HELPERS
# ==========================================
def format_volume(vol):
    """Formats raw volume into standard K/M units."""
    if vol >= 1_000_000:
        return f"{vol / 1_000_000:.2f}M"
    elif vol >= 1_000:
        return f"{vol / 1_000:.1f}K"
    return str(int(vol))


def format_signed_pct(val):
    """Formats percentage with explicit + / - signs."""
    return f"+{val:.2f}%" if val > 0 else f"{val:.2f}%"


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


# ==========================================
# 1. DATA LOADING & RATE-LIMITED FETCHING
# ==========================================
@st.cache_data(ttl=86400)
def load_instrument_mapping(excel_path, csv_path):
    fno_df = pd.read_excel(excel_path, engine="openpyxl")
    fno_clean = (
        fno_df.dropna(subset=['SYMBOL', 'SECTOR'])
        [['SYMBOL', 'SECTOR']]
        .drop_duplicates()
    )

    inst_df = pd.read_csv(csv_path)

    nse_eq = inst_df[
        (inst_df['segment'] == 'NSE_EQ') &
        (inst_df['instrument_type'] == 'EQ')
    ][['trading_symbol', 'instrument_key', 'name']]

    merged = pd.merge(
        fno_clean,
        nse_eq,
        left_on='SYMBOL',
        right_on='trading_symbol',
        how='inner'
    )

    return merged.drop_duplicates(
        subset=['SYMBOL', 'SECTOR']
    ).reset_index(drop=True)


def _fetch_single_10d_vol(
    key,
    access_token,
    to_date,
    from_date
):
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {access_token}'
    }

    encoded_key = urllib.parse.quote(
        key,
        safe='|:'
    )

    url = (
        f"https://api.upstox.com/v2/historical-candle/"
        f"{encoded_key}/day/{to_date}/{from_date}"
    )

    for attempt in range(2):
        try:
            res = requests.get(
                url,
                headers=headers,
                timeout=4
            )

            if res.status_code == 200:
                candles = res.json().get(
                    'data', {}
                ).get('candles', [])

                if candles:
                    vols = [
                        c[5]
                        for c in candles[1:11]
                    ]

                    if vols:
                        return (
                            key,
                            sum(vols) / len(vols)
                        )

            elif res.status_code == 429:
                time.sleep(0.25)

        except Exception:
            pass

    return key, 0.0


@st.cache_data(ttl=86400)
def fetch_10d_avg_volumes_throttled(
    instrument_keys,
    access_token
):
    today = datetime.now(IST)

    from_date = (
        today - timedelta(days=20)
    ).strftime("%Y-%m-%d")

    to_date = today.strftime("%Y-%m-%d")

    avg_volumes = {}

    with ThreadPoolExecutor(
        max_workers=4
    ) as executor:

        futures = [
            executor.submit(
                _fetch_single_10d_vol,
                key,
                access_token,
                to_date,
                from_date
            )
            for key in instrument_keys
        ]

        for future in as_completed(futures):
            key, vol = future.result()

            avg_volumes[key] = vol
            avg_volumes[
                key.replace('|', ':')
            ] = vol
            avg_volumes[
                key.replace(':', '|')
            ] = vol

    return avg_volumes


def fetch_live_quotes_safe(
    instrument_keys,
    access_token
):
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {access_token}'
    }

    url = (
        "https://api.upstox.com/v2/"
        "market-quote/quotes"
    )

    batch_size = 250

    batches = [
        instrument_keys[i:i + batch_size]
        for i in range(
            0,
            len(instrument_keys),
            batch_size
        )
    ]

    quotes_data = {}
    last_error = None

    for idx, chunk in enumerate(batches):

        keys_param = ",".join(chunk)

        try:
            encoded_params = urllib.parse.urlencode(
                {
                    'instrument_key': keys_param
                },
                safe=',|:'
            )

            res = requests.get(
                f"{url}?{encoded_params}",
                headers=headers,
                timeout=6
            )

            if (
                res.status_code == 200 and
                res.json().get('status') == 'success'
            ):

                quotes_data.update(
                    res.json().get('data', {})
                )

            elif res.status_code == 429:

                last_error = (
                    "HTTP 429 Rate Limit hit. "
                    "Retrying after brief pause..."
                )

                time.sleep(0.5)

                res_retry = requests.get(
                    f"{url}?{encoded_params}",
                    headers=headers,
                    timeout=6
                )

                if res_retry.status_code == 200:

                    quotes_data.update(
                        res_retry.json().get(
                            'data',
                            {}
                        )
                    )

                else:

                    last_error = (
                        f"HTTP {res_retry.status_code}: "
                        f"{res_retry.text}"
                    )

            else:

                last_error = (
                    f"HTTP {res.status_code}: "
                    f"{res.text}"
                )

        except Exception as e:
            last_error = str(e)

        if idx < len(batches) - 1:
            time.sleep(0.15)

    return quotes_data, last_error


def process_market_data(
    mapped_df,
    quotes_dict,
    avg_10d_vol_dict
):
    records = []

    if not quotes_dict:
        return pd.DataFrame()

    normalized_quotes = {}

    for k, v in quotes_dict.items():

        normalized_quotes[k] = v

        normalized_quotes[
            k.replace(':', '|')
        ] = v

        normalized_quotes[
            k.replace('|', ':')
        ] = v

        if ':' in k:
            normalized_quotes[
                k.split(':')[1]
            ] = v

        if '|' in k:
            normalized_quotes[
                k.split('|')[1]
            ] = v

    for _, row in mapped_df.iterrows():

        key = row['instrument_key']
        symbol = row['SYMBOL']
        sector = row['SECTOR']

        quote = (
            normalized_quotes.get(key)
            or normalized_quotes.get(symbol)
            or normalized_quotes.get(
                f"NSE_EQ:{symbol}"
            )
            or normalized_quotes.get(
                f"NSE_EQ|{symbol}"
            )
            or {}
        )

        if not quote:
            continue

        ltp = safe_float(
            quote.get('last_price')
        )

        volume = safe_float(
            quote.get('volume')
        )

        vwap = safe_float(
            quote.get('average_price'),
            ltp
        )

        net_change = quote.get(
            'net_change'
        )

        ohlc = quote.get('ohlc') or {}

        close_price = safe_float(
            ohlc.get('close')
            or quote.get('prev_close')
        )

        if (
            net_change is not None
            and ltp > 0
        ):

            p_change = safe_float(
                net_change
            )

            prev_close = (
                ltp - p_change
            )

            p_change = (
                (p_change / prev_close * 100)
                if prev_close > 0
                else 0.0
            )

        elif (
            close_price > 0
            and close_price != ltp
        ):

            p_change = (
                (ltp - close_price)
                / close_price
            ) * 100

        else:

            p_change = 0.0

        buy_qty = safe_float(
            quote.get(
                'total_buy_quantity'
            )
        )

        sell_qty = safe_float(
            quote.get(
                'total_sell_quantity'
            )
        )

        avg_vol = (
            avg_10d_vol_dict.get(
                key,
                0.0
            )
            or avg_10d_vol_dict.get(
                symbol,
                0.0
            )
        )

        vol_ratio = (
            volume / avg_vol
            if avg_vol > 0
            else 1.0
        )

        vol_ratio_capped = min(
            vol_ratio,
            10.0
        )

        vwap_dist = (
            ((ltp - vwap) / vwap * 100)
            if vwap > 0
            else 0.0
        )

        flow_ratio = (
            (buy_qty / sell_qty)
            if sell_qty > 0
            else (
                1.5
                if buy_qty > 0
                else 1.0
            )
        )

        inst_score = (
            p_change
            + (vwap_dist * 0.8)
            + ((flow_ratio - 1) * 2)
            + (
                (vol_ratio_capped - 1)
                * 0.5
            )
        )

        tv_url = (
            "https://www.tradingview.com/chart/"
            f"?symbol=NSE:{symbol}&interval=5"
        )

        records.append({
            'SYMBOL': symbol,
            'CHART_URL': tv_url,
            'SECTOR': sector,
            'LTP (₹)': round(ltp, 2),
            'CHANGE_%': round(
                p_change,
                2
            ),
            'CHANGE_STR': format_signed_pct(
                p_change
            ),
            'VWAP_DIST_%': round(
                vwap_dist,
                2
            ),
            'VWAP_DIST_STR': format_signed_pct(
                vwap_dist
            ),
            'VOLUME_RAW': int(volume),
            'Volume': format_volume(
                volume
            ),
            'VOL_10D_RATIO_RAW': round(
                vol_ratio,
                2
            ),
            'Vol / 10D Vol': (
                f"{vol_ratio:.2f}x"
            ),
            'FLOW_RATIO_RAW': round(
                flow_ratio,
                2
            ),
            'Order Flow': (
                f"{flow_ratio:.2f}x"
            ),
            'INST_SCORE': round(
                inst_score,
                2
            ),
        })

    return pd.DataFrame(records)


# ==========================================================
# NEW ADDITION
# 20-DAY DAILY PRICE + VOLUME HISTORY
# ==========================================================
def _fetch_single_daily_history(
    key,
    access_token,
    to_date,
    from_date
):
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {access_token}'
    }

    encoded_key = urllib.parse.quote(
        key,
        safe='|:'
    )

    url = (
        f"https://api.upstox.com/v2/historical-candle/"
        f"{encoded_key}/day/{to_date}/{from_date}"
    )

    for attempt in range(2):

        try:

            res = requests.get(
                url,
                headers=headers,
                timeout=6
            )

            if res.status_code == 200:

                candles = res.json().get(
                    'data',
                    {}
                ).get(
                    'candles',
                    []
                )

                cleaned = []

                for candle in candles:

                    if len(candle) < 6:
                        continue

                    try:

                        ts = pd.to_datetime(
                            candle[0],
                            errors='coerce'
                        )

                        if pd.isna(ts):
                            continue

                        cleaned.append({
                            'timestamp': ts,
                            'high': safe_float(
                                candle[2]
                            ),
                            'low': safe_float(
                                candle[3]
                            ),
                            'volume': safe_float(
                                candle[5]
                            )
                        })

                    except Exception:
                        continue

                return key, cleaned

            elif res.status_code == 429:

                time.sleep(0.5)

        except Exception:

            time.sleep(0.15)

    return key, []


@st.cache_data(ttl=86400)
def fetch_20d_daily_history(
    instrument_keys,
    access_token
):
    today = datetime.now(IST)

    # Previous completed day through approximately
    # 20 previous trading sessions.
    from_date = (
        today - timedelta(days=35)
    ).strftime("%Y-%m-%d")

    to_date = (
        today - timedelta(days=1)
    ).strftime("%Y-%m-%d")

    history = {}

    with ThreadPoolExecutor(
        max_workers=4
    ) as executor:

        futures = [
            executor.submit(
                _fetch_single_daily_history,
                key,
                access_token,
                to_date,
                from_date
            )
            for key in instrument_keys
        ]

        for future in as_completed(futures):

            key, candles = future.result()

            candles = sorted(
                candles,
                key=lambda x: x['timestamp'],
                reverse=True
            )

            candles = candles[:20]

            history[key] = candles

            history[
                key.replace('|', ':')
            ] = candles

            history[
                key.replace(':', '|')
            ] = candles

    return history


# ==========================================================
# NEW ADDITION
# TODAY'S 09:15–09:45 ORB
# ==========================================================
def _fetch_today_5m_candles(
    key,
    access_token
):
    """
    Fetch CURRENT DAY 5-minute candles.

    Important:
    The previous version used the standard historical-candle
    endpoint with today's date. For a live ORB, use Upstox V3
    Intraday Candle API instead. The V3 intraday endpoint is
    specifically designed to return the current trading day's
    candles.
    """

    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {access_token}'
    }

    encoded_key = urllib.parse.quote(
        key,
        safe='|:'
    )

    # Upstox V3 current-day intraday endpoint.
    # 5-minute candles are requested directly.
    url = (
        f"https://api.upstox.com/v3/historical-candle/"
        f"intraday/{encoded_key}/minutes/5"
    )

    for attempt in range(2):

        try:

            res = requests.get(
                url,
                headers=headers,
                timeout=6
            )

            if res.status_code == 200:

                payload = res.json()

                candles = (
                    payload
                    .get('data', {})
                    .get('candles', [])
                )

                result = []

                for candle in candles:

                    if len(candle) < 6:
                        continue

                    try:

                        ts = pd.to_datetime(
                            candle[0],
                            errors='coerce'
                        )

                        if pd.isna(ts):
                            continue

                        if ts.tzinfo is not None:
                            ts = ts.tz_convert(IST)
                        else:
                            ts = ts.tz_localize(IST)

                        result.append({
                            'timestamp': ts,
                            'open': safe_float(
                                candle[1]
                            ),
                            'high': safe_float(
                                candle[2]
                            ),
                            'low': safe_float(
                                candle[3]
                            ),
                            'close': safe_float(
                                candle[4]
                            ),
                            'volume': safe_float(
                                candle[5]
                            )
                        })

                    except Exception:
                        continue

                return key, result

            elif res.status_code == 429:

                time.sleep(0.5)

            else:

                # Retry once for transient API failures.
                if attempt == 0:
                    time.sleep(0.25)

        except Exception:

            if attempt == 0:
                time.sleep(0.25)

    return key, []


@st.cache_data(
    ttl=20,
    show_spinner=False
)
def fetch_orb_data_for_symbols_cached(
    symbol_to_key_items,
    symbols,
    access_token,
    today_str
):
    """
    Build today's 09:15–09:45 ORB from Upstox V3
    current-day 5-minute candles.

    ORB range:
        09:15
        09:20
        09:25
        09:30
        09:35
        09:40

    Breakout evaluation starts AFTER 09:45.

    Scoring:
        +3  ORB breakout
        +5  ORB breakout + maintained momentum
        -3  Breakout happened but price returned
            back inside the ORB range
         0  Still inside ORB / no breakout
    """

    symbol_to_key = dict(
        symbol_to_key_items
    )

    results = {}

    now = datetime.now(IST)

    # Before the ORB is complete, do not call the
    # intraday API unnecessarily.
    if (
        now.hour < 9
        or (
            now.hour == 9
            and now.minute < 45
        )
    ):

        for symbol in symbols:

            results[symbol] = {
                'orb_high': 0.0,
                'orb_low': 0.0,
                'orb_status': 'WAITING FOR 09:45',
                'orb_score': 0
            }

        return results

    # ------------------------------------------------------
    # Fetch current-day candles.
    # ------------------------------------------------------
    fetched = {}

    with ThreadPoolExecutor(
        max_workers=4
    ) as executor:

        future_to_symbol = {}

        for symbol in symbols:

            key = symbol_to_key.get(
                symbol
            )

            if key:

                future = executor.submit(
                    _fetch_today_5m_candles,
                    key,
                    access_token
                )

                future_to_symbol[
                    future
                ] = symbol

        for future in as_completed(
            future_to_symbol
        ):

            symbol = future_to_symbol[
                future
            ]

            try:

                key, candles = (
                    future.result()
                )

                fetched[symbol] = candles

            except Exception:

                fetched[symbol] = []

    # ------------------------------------------------------
    # Calculate ORB independently for each stock.
    # ------------------------------------------------------
    for symbol in symbols:

        candles = fetched.get(
            symbol,
            []
        )

        if not candles:

            results[symbol] = {
                'orb_high': 0.0,
                'orb_low': 0.0,
                'orb_status': 'NO DATA',
                'orb_score': 0
            }

            continue

        df = pd.DataFrame(
            candles
        )

        if df.empty:

            results[symbol] = {
                'orb_high': 0.0,
                'orb_low': 0.0,
                'orb_status': 'NO DATA',
                'orb_score': 0
            }

            continue

        df['timestamp'] = pd.to_datetime(
            df['timestamp'],
            errors='coerce'
        )

        df = df.dropna(
            subset=['timestamp']
        )

        if df.empty:

            results[symbol] = {
                'orb_high': 0.0,
                'orb_low': 0.0,
                'orb_status': 'NO DATA',
                'orb_score': 0
            }

            continue

        if df['timestamp'].dt.tz is None:

            df['timestamp'] = (
                df['timestamp']
                .dt.tz_localize(IST)
            )

        else:

            df['timestamp'] = (
                df['timestamp']
                .dt.tz_convert(IST)
            )

        df = df.sort_values(
            'timestamp'
        ).reset_index(
            drop=True
        )

        # Only today's candles.
        df = df[
            df['timestamp'].dt.strftime(
                "%Y-%m-%d"
            ) == today_str
        ].copy()

        if df.empty:

            results[symbol] = {
                'orb_high': 0.0,
                'orb_low': 0.0,
                'orb_status': 'NO TODAY DATA',
                'orb_score': 0
            }

            continue

        # --------------------------------------------------
        # 09:15 through 09:40 = six 5-minute candles.
        # The 09:45 candle is NOT part of the ORB range.
        # --------------------------------------------------
        orb_df = df[
            (
                df['timestamp'].dt.hour == 9
            )
            &
            (
                df['timestamp'].dt.minute >= 15
            )
            &
            (
                df['timestamp'].dt.minute < 45
            )
        ].copy()

        # Require at least one candle. Normally six should
        # exist after 09:45.
        if orb_df.empty:

            results[symbol] = {
                'orb_high': 0.0,
                'orb_low': 0.0,
                'orb_status': 'ORB DATA NOT READY',
                'orb_score': 0
            }

            continue

        orb_high = safe_float(
            orb_df['high'].max()
        )

        orb_low = safe_float(
            orb_df['low'].min()
        )

        if (
            orb_high <= 0
            or orb_low <= 0
            or orb_high <= orb_low
        ):

            results[symbol] = {
                'orb_high': orb_high,
                'orb_low': orb_low,
                'orb_status': 'INVALID ORB',
                'orb_score': 0
            }

            continue

        # --------------------------------------------------
        # Only candles AFTER 09:45 can be breakout candles.
        # --------------------------------------------------
        after_orb = df[
            (
                df['timestamp'].dt.hour > 9
            )
            |
            (
                (
                    df['timestamp'].dt.hour == 9
                )
                &
                (
                    df['timestamp'].dt.minute >= 45
                )
            )
        ].copy()

        if after_orb.empty:

            results[symbol] = {
                'orb_high': orb_high,
                'orb_low': orb_low,
                'orb_status': 'ORB READY',
                'orb_score': 0
            }

            continue

        # --------------------------------------------------
        # Detect actual breakout events using CLOSE.
        #
        # Bull breakout:
        #   close > ORB high
        #
        # Bear breakout:
        #   close < ORB low
        # --------------------------------------------------
        bull_breaks = (
            after_orb['close']
            > orb_high
        )

        bear_breaks = (
            after_orb['close']
            < orb_low
        )

        had_bull_break = bool(
            bull_breaks.any()
        )

        had_bear_break = bool(
            bear_breaks.any()
        )

        latest_close = safe_float(
            after_orb.iloc[-1]['close']
        )

        # --------------------------------------------------
        # If both sides broke at different times, evaluate
        # the most recent breakout direction.
        # --------------------------------------------------
        latest_bull_time = (
            after_orb.loc[
                bull_breaks,
                'timestamp'
            ].max()
            if had_bull_break
            else None
        )

        latest_bear_time = (
            after_orb.loc[
                bear_breaks,
                'timestamp'
            ].max()
            if had_bear_break
            else None
        )

        if (
            latest_bull_time is not None
            and latest_bear_time is not None
        ):

            if latest_bull_time > latest_bear_time:
                last_break_direction = (
                    'BULLISH'
                )
                last_break_time = (
                    latest_bull_time
                )
            else:
                last_break_direction = (
                    'BEARISH'
                )
                last_break_time = (
                    latest_bear_time
                )

        elif latest_bull_time is not None:

            last_break_direction = (
                'BULLISH'
            )
            last_break_time = (
                latest_bull_time
            )

        elif latest_bear_time is not None:

            last_break_direction = (
                'BEARISH'
            )
            last_break_time = (
                latest_bear_time
            )

        else:

            last_break_direction = None
            last_break_time = None

        # --------------------------------------------------
        # No breakout yet.
        # --------------------------------------------------
        if last_break_direction is None:

            results[symbol] = {
                'orb_high': orb_high,
                'orb_low': orb_low,
                'orb_status': 'INSIDE ORB',
                'orb_score': 0
            }

            continue

        # Candles from the latest breakout onward.
        post_break = after_orb[
            after_orb['timestamp']
            >= last_break_time
        ].copy()

        if post_break.empty:
            post_break = after_orb.tail(1)

        # --------------------------------------------------
        # Bullish breakout.
        # --------------------------------------------------
        if last_break_direction == 'BULLISH':

            if latest_close > orb_high:

                # Every candle after the latest breakout
                # must remain above ORB high for the
                # stronger momentum score.
                maintained = bool(
                    (
                        post_break['close']
                        > orb_high
                    ).all()
                )

                if maintained:

                    status = (
                        'BULL ORB + MOMENTUM'
                    )
                    score = 5

                else:

                    status = (
                        'BULL ORB BREAK'
                    )
                    score = 3

            else:

                # It broke above ORB and subsequently
                # returned inside/below the ORB.
                status = (
                    'BULL ORB RETURN / NEGATIVE'
                )
                score = -3

        # --------------------------------------------------
        # Bearish breakout.
        # --------------------------------------------------
        else:

            if latest_close < orb_low:

                # Every candle after the latest breakout
                # must remain below ORB low for the
                # stronger momentum score.
                maintained = bool(
                    (
                        post_break['close']
                        < orb_low
                    ).all()
                )

                if maintained:

                    status = (
                        'BEAR ORB + MOMENTUM'
                    )
                    score = 5

                else:

                    status = (
                        'BEAR ORB BREAK'
                    )
                    score = 3

            else:

                # It broke below ORB and subsequently
                # returned inside/above the ORB.
                status = (
                    'BEAR ORB RETURN / NEGATIVE'
                )
                score = -3

        results[symbol] = {
            'orb_high': round(
                orb_high,
                2
            ),
            'orb_low': round(
                orb_low,
                2
            ),
            'orb_status': status,
            'orb_score': score
        }

    return results


# ==========================================================
# NEW ADDITION
# PRICE RANK
# ==========================================================
def get_history_for_symbol(
    symbol,
    key,
    daily_history
):
    history = (
        daily_history.get(
            key,
            []
        )
        or
        daily_history.get(
            symbol,
            []
        )
    )

    if not history and key:

        history = daily_history.get(
            key.replace('|', ':'),
            []
        )

    return sorted(
        history,
        key=lambda x: x['timestamp'],
        reverse=True
    )[:20]


def calculate_price_rank(
    ltp,
    history,
    direction
):
    """
    Bullish:
        1D high -> 1 point
        2D high -> 2 points
        5D high -> 3 points
        10D high -> 4 points
        20D high -> 5 points

    Bearish:
        1D low -> 1 point
        2D low -> 2 points
        5D low -> 3 points
        10D low -> 4 points
        20D low -> 5 points
    """

    if (
        ltp <= 0
        or not history
    ):
        return 0, "NO HISTORY"

    score = 0
    level_name = "Below 1D Level"

    windows = [
        (1, 1),
        (2, 2),
        (5, 3),
        (10, 4),
        (20, 5)
    ]

    if direction == "BULLISH":

        for window, points in windows:

            sample = history[:window]

            if not sample:
                continue

            level = max(
                x['high']
                for x in sample
            )

            if ltp >= level:

                score = points
                level_name = (
                    f"{window}D HIGH"
                )

    else:

        for window, points in windows:

            sample = history[:window]

            if not sample:
                continue

            level = min(
                x['low']
                for x in sample
            )

            if ltp <= level:

                score = points
                level_name = (
                    f"{window}D LOW"
                )

    return score, level_name


# ==========================================================
# NEW ADDITION
# VOLUME RANK
# ==========================================================
def calculate_volume_rank(
    current_volume,
    history
):
    """
    Current cumulative market volume compared with:
      1 previous trading day
      5-day average
      10-day average
      20-day average

    Same ranking is applied to bullish and bearish stocks.
    """

    if (
        current_volume <= 0
        or not history
    ):
        return 0, "NO VOLUME HISTORY"

    previous_volumes = [
        safe_float(x['volume'])
        for x in history
        if safe_float(
            x['volume']
        ) > 0
    ]

    if not previous_volumes:
        return 0, "NO VOLUME HISTORY"

    comparisons = []

    if len(previous_volumes) >= 1:

        ratio_1d = (
            current_volume
            / previous_volumes[0]
            if previous_volumes[0] > 0
            else 0
        )

        comparisons.append(
            (
                1 if ratio_1d >= 1 else 0,
                "1D"
            )
        )

    if len(previous_volumes) >= 5:

        avg_5d = (
            sum(previous_volumes[:5])
            / 5
        )

        ratio_5d = (
            current_volume / avg_5d
            if avg_5d > 0
            else 0
        )

        comparisons.append(
            (
                2 if ratio_5d >= 1 else 0,
                "5D"
            )
        )

    if len(previous_volumes) >= 10:

        avg_10d = (
            sum(previous_volumes[:10])
            / 10
        )

        ratio_10d = (
            current_volume / avg_10d
            if avg_10d > 0
            else 0
        )

        comparisons.append(
            (
                3 if ratio_10d >= 1 else 0,
                "10D"
            )
        )

    if len(previous_volumes) >= 20:

        avg_20d = (
            sum(previous_volumes[:20])
            / 20
        )

        ratio_20d = (
            current_volume / avg_20d
            if avg_20d > 0
            else 0
        )

        comparisons.append(
            (
                4 if ratio_20d >= 1 else 0,
                "20D"
            )
        )

    if not comparisons:
        return 0, "NO COMPARISON"

    score = max(
        x[0]
        for x in comparisons
    )

    labels = [
        x[1]
        for x in comparisons
        if x[0] == score
        and x[1]
    ]

    label = (
        labels[-1]
        if labels
        else "BELOW"
    )

    return score, label


# ==========================================================
# NEW ADDITION
# OPTIONS OI BUILDING + PCR + PCR OI CHANGE
# ==========================================================
def _fetch_single_option_chain_oi_pcr(
    symbol,
    underlying_key,
    access_token
):
    """
    Fetch the current-month option chain for one F&O stock.

    One Upstox option-chain request gives current OI and previous
    OI for both CE and PE across strikes. We use that single
    response to calculate:
      - Total CE OI / PE OI
      - Total OI change
      - Directional option-OI build bias
      - Current PCR
      - Previous-OI PCR
      - PCR change derived from OI

    This deliberately uses the option-chain API rather than making
    three separate OI/PCR API calls for every stock, which keeps the
    live dashboard much lighter on API requests.
    """

    if not underlying_key:
        return symbol, {
            'oi_status': 'NO INSTRUMENT KEY'
        }

    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {access_token}'
    }

    url = 'https://api.upstox.com/v2/option/chain'

    params = {
        'instrument_key': underlying_key,
        'expiry_date': 'current_month'
    }

    for attempt in range(2):

        try:

            res = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=8
            )

            if res.status_code == 200:

                payload = res.json()

                if payload.get('status') != 'success':
                    return symbol, {
                        'oi_status': 'API UNSUCCESSFUL'
                    }

                chain = payload.get('data', []) or []

                if not chain:
                    return symbol, {
                        'oi_status': 'NO OPTION DATA'
                    }

                total_call_oi = 0.0
                total_put_oi = 0.0
                total_call_prev_oi = 0.0
                total_put_prev_oi = 0.0

                for item in chain:

                    call_md = (
                        (item.get('call_options') or {})
                        .get('market_data') or {}
                    )

                    put_md = (
                        (item.get('put_options') or {})
                        .get('market_data') or {}
                    )

                    total_call_oi += safe_float(
                        call_md.get('oi')
                    )

                    total_put_oi += safe_float(
                        put_md.get('oi')
                    )

                    total_call_prev_oi += safe_float(
                        call_md.get('prev_oi')
                    )

                    total_put_prev_oi += safe_float(
                        put_md.get('prev_oi')
                    )

                total_current_oi = (
                    total_call_oi
                    + total_put_oi
                )

                total_previous_oi = (
                    total_call_prev_oi
                    + total_put_prev_oi
                )

                call_oi_change = (
                    total_call_oi
                    - total_call_prev_oi
                )

                put_oi_change = (
                    total_put_oi
                    - total_put_prev_oi
                )

                total_oi_change = (
                    total_current_oi
                    - total_previous_oi
                )

                current_pcr = (
                    total_put_oi / total_call_oi
                    if total_call_oi > 0
                    else 0.0
                )

                previous_pcr = (
                    total_put_prev_oi
                    / total_call_prev_oi
                    if total_call_prev_oi > 0
                    else 0.0
                )

                pcr_oi_change_pct = (
                    (
                        (current_pcr - previous_pcr)
                        / previous_pcr
                    ) * 100
                    if previous_pcr > 0
                    else 0.0
                )

                # Positive bias = PE OI is building more than CE OI.
                # Negative bias = CE OI is building more than PE OI.
                directional_oi_change = (
                    put_oi_change
                    - call_oi_change
                )

                denominator = max(
                    total_previous_oi,
                    1.0
                )

                oi_bias_pct = (
                    directional_oi_change
                    / denominator
                ) * 100

                # This is a sentiment-oriented options OI build label.
                if directional_oi_change > 0:
                    oi_build = 'BULLISH OI BUILD'
                elif directional_oi_change < 0:
                    oi_build = 'BEARISH OI BUILD'
                elif total_oi_change > 0:
                    oi_build = 'MIXED OI BUILD'
                elif total_oi_change < 0:
                    oi_build = 'OI UNWINDING'
                else:
                    oi_build = 'OI FLAT'

                return symbol, {
                    'oi_status': 'OK',
                    'expiry': chain[0].get('expiry', ''),
                    'call_oi': total_call_oi,
                    'put_oi': total_put_oi,
                    'call_oi_change': call_oi_change,
                    'put_oi_change': put_oi_change,
                    'total_oi': total_current_oi,
                    'total_oi_change': total_oi_change,
                    'current_pcr': current_pcr,
                    'previous_pcr': previous_pcr,
                    'pcr_oi_change_pct': pcr_oi_change_pct,
                    'oi_bias_pct': oi_bias_pct,
                    'oi_build': oi_build
                }

            elif res.status_code == 429:

                time.sleep(0.5)

            else:

                if attempt == 0:
                    time.sleep(0.25)
                else:
                    return symbol, {
                        'oi_status': f'HTTP {res.status_code}'
                    }

        except Exception as exc:

            if attempt == 0:
                time.sleep(0.25)
            else:
                return symbol, {
                    'oi_status': f'ERROR: {str(exc)[:60]}'
                }

    return symbol, {
        'oi_status': 'NO DATA'
    }


@st.cache_data(
    ttl=25,
    show_spinner=False
)
def fetch_options_oi_pcr_cached(
    symbol_to_key_items,
    symbols,
    access_token,
    today_str
):
    """
    Fetch option-chain OI/PCR data for retained candidates.

    Cache is intentionally short so the OI/PCR section follows the
    live 30-second dashboard refresh without creating unnecessary
    duplicate API calls.
    """

    symbol_to_key = dict(symbol_to_key_items)
    results = {}

    with ThreadPoolExecutor(
        max_workers=4
    ) as executor:

        future_to_symbol = {}

        for symbol in symbols:

            key = symbol_to_key.get(
                symbol,
                ''
            )

            if key:

                future = executor.submit(
                    _fetch_single_option_chain_oi_pcr,
                    symbol,
                    key,
                    access_token
                )

                future_to_symbol[
                    future
                ] = symbol

        for future in as_completed(
            future_to_symbol
        ):

            symbol = future_to_symbol[
                future
            ]

            try:
                _, result = future.result()
                results[symbol] = result
            except Exception:
                results[symbol] = {
                    'oi_status': 'FETCH ERROR'
                }

    # Ensure every requested symbol has an entry.
    for symbol in symbols:
        if symbol not in results:
            results[symbol] = {
                'oi_status': 'NO DATA'
            }

    return results


def calculate_oi_build_rank(
    direction,
    oi_bias_pct,
    price_change_pct,
    total_oi_change
):
    """
    Direction-aware OI building rank, 0-5.

    Positive PE-vs-CE OI bias supports bullish candidates.
    Negative PE-vs-CE OI bias supports bearish candidates.

    The underlying price is also used as a confirmation signal:
      Bullish: price up + bullish OI bias = stronger confirmation.
      Bearish: price down + bearish OI bias = stronger confirmation.

    This is an options-OI sentiment rank, not a claim that option
    OI alone identifies whether a participant is buyer or writer.
    """

    bias = safe_float(oi_bias_pct)
    price = safe_float(price_change_pct)
    oi_change = safe_float(total_oi_change)

    if direction == 'BULLISH':
        aligned = bias > 0
        price_aligned = price > 0
    else:
        aligned = bias < 0
        price_aligned = price < 0

    if not aligned:
        return 0, 'AGAINST DIRECTION'

    magnitude = min(
        abs(bias),
        5.0
    )

    score = min(
        4,
        max(1, int(magnitude + 0.5))
    )

    if price_aligned and oi_change > 0:
        score += 1
        label = 'STRONG BUILD'
    elif price_aligned:
        label = 'BUILD'
    elif oi_change > 0:
        label = 'OI BUILD'
    else:
        label = 'WEAK BUILD'

    return min(score, 5), label


def calculate_pcr_rank(
    direction,
    pcr
):
    """
    Direction-aware PCR rank, 0-5.

    Bullish candidates are favored when PCR is above 1.
    Bearish candidates are favored when PCR is below 1.
    The farther PCR moves from 1 in the candidate's direction,
    the higher the rank.
    """

    pcr = safe_float(pcr)

    if pcr <= 0:
        return 0

    if direction == 'BULLISH':
        edge = pcr - 1.0
    else:
        edge = 1.0 - pcr

    if edge <= 0:
        return 0

    score = min(
        5,
        max(1, int(edge * 10))
    )

    return score


def calculate_pcr_oi_change_rank(
    direction,
    pcr_oi_change_pct
):
    """
    Direction-aware rank for change in PCR calculated directly
    from current OI versus previous OI.

    Bullish: rising PCR is supportive.
    Bearish: falling PCR is supportive.
    """

    change_pct = safe_float(
        pcr_oi_change_pct
    )

    if direction == 'BULLISH':
        edge = change_pct
    else:
        edge = -change_pct

    if edge <= 0:
        return 0

    score = min(
        5,
        max(1, int(edge / 2))
    )

    return score


# ==========================================================
# NEW ADDITION
# BUILD COMBINED RANKING
# ==========================================================
def build_combined_rank_table(
    candidate_state,
    data_df,
    mapped_df,
    daily_history,
    orb_data,
    options_oi_data
):
    if not candidate_state:
        return pd.DataFrame()

    current_lookup = (
        data_df
        .drop_duplicates(
            subset=['SYMBOL']
        )
        .set_index('SYMBOL')
    )

    key_lookup = dict(
        zip(
            mapped_df['SYMBOL'],
            mapped_df['instrument_key']
        )
    )

    rows = []

    for symbol, candidate in (
        candidate_state.items()
    ):

        if symbol not in current_lookup.index:
            continue

        row = current_lookup.loc[
            symbol
        ]

        direction = candidate.get(
            'direction',
            'BULLISH'
        )

        key = key_lookup.get(
            symbol,
            ''
        )

        history = get_history_for_symbol(
            symbol,
            key,
            daily_history
        )

        ltp = safe_float(
            row.get('LTP (₹)')
        )

        volume = safe_float(
            row.get('VOLUME_RAW')
        )

        price_score, price_level = (
            calculate_price_rank(
                ltp,
                history,
                direction
            )
        )

        volume_score, volume_level = (
            calculate_volume_rank(
                volume,
                history
            )
        )

        orb = orb_data.get(
            symbol,
            {
                'orb_high': 0.0,
                'orb_low': 0.0,
                'orb_status': 'NO DATA',
                'orb_score': 0
            }
        )

        orb_score = int(
            orb.get(
                'orb_score',
                0
            )
        )

        oi_data = options_oi_data.get(
            symbol,
            {
                'oi_status': 'NO DATA'
            }
        )

        oi_bias_pct = safe_float(
            oi_data.get(
                'oi_bias_pct'
            )
        )

        total_oi_change = safe_float(
            oi_data.get(
                'total_oi_change'
            )
        )

        pcr = safe_float(
            oi_data.get(
                'current_pcr'
            )
        )

        pcr_oi_change_pct = safe_float(
            oi_data.get(
                'pcr_oi_change_pct'
            )
        )

        oi_build_rank, oi_build_label = (
            calculate_oi_build_rank(
                direction,
                oi_bias_pct,
                safe_float(row.get('CHANGE_%')),
                total_oi_change
            )
        )

        pcr_rank = calculate_pcr_rank(
            direction,
            pcr
        )

        pcr_oi_change_rank = (
            calculate_pcr_oi_change_rank(
                direction,
                pcr_oi_change_pct
            )
        )

        total_score = (
            price_score
            + volume_score
            + orb_score
            + oi_build_rank
            + pcr_rank
            + pcr_oi_change_rank
        )

        rows.append({
            'Rank': 0,
            'SYMBOL': symbol,
            'CHART_URL': row.get(
                'CHART_URL',
                ''
            ),
            'Direction': direction,
            'SECTOR': row.get(
                'SECTOR',
                ''
            ),
            'LTP (₹)': ltp,
            'Change %': row.get(
                'CHANGE_STR',
                ''
            ),
            'Inst. Score': safe_float(
                row.get(
                    'INST_SCORE'
                )
            ),
            'Price Rank': price_score,
            'Price Level': price_level,
            'Volume Rank': volume_score,
            'Volume Level': volume_level,
            'ORB High': (
                round(
                    orb.get(
                        'orb_high',
                        0.0
                    ),
                    2
                )
                if orb.get(
                    'orb_high',
                    0.0
                )
                else "-"
            ),
            'ORB Low': (
                round(
                    orb.get(
                        'orb_low',
                        0.0
                    ),
                    2
                )
                if orb.get(
                    'orb_low',
                    0.0
                )
                else "-"
            ),
            'ORB Status': orb.get(
                'orb_status',
                'NO DATA'
            ),
            'ORB Score': orb_score,
            'OI Build': oi_data.get(
                'oi_build',
                'NO DATA'
            ),
            'OI Build Rank': oi_build_rank,
            'PCR': (
                round(pcr, 2)
                if pcr > 0
                else '-'
            ),
            'PCR Rank': pcr_rank,
            'PCR OI Chg %': (
                round(pcr_oi_change_pct, 2)
                if oi_data.get('oi_status') == 'OK'
                else '-'
            ),
            'PCR OI Chg Rank': pcr_oi_change_rank,
            'OI Chg': (
                round(total_oi_change, 0)
                if oi_data.get('oi_status') == 'OK'
                else '-'
            ),
            'OI Status': oi_data.get(
                'oi_status',
                'NO DATA'
            ),
            'Total Rank Score': total_score,
            'Entered Top-30 At': candidate.get(
                'entered_at',
                ''
            )
        })

    if not rows:
        return pd.DataFrame()

    result = pd.DataFrame(rows)

    result = result.sort_values(
        by=[
            'Total Rank Score',
            'Price Rank',
            'Volume Rank',
            'OI Build Rank',
            'PCR Rank',
            'PCR OI Chg Rank',
            'ORB Score',
            'Inst. Score'
        ],
        ascending=[
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False
        ]
    ).reset_index(
        drop=True
    )

    result['Rank'] = (
        result.index + 1
    )

    return result


# ==========================================
# 2. TIME CONTROL
# ==========================================
def is_market_open():

    now = datetime.now(IST)

    if now.weekday() >= 5:
        return False

    start_time = now.replace(
        hour=9,
        minute=14,
        second=0,
        microsecond=0
    )

    end_time = now.replace(
        hour=15,
        minute=13,
        second=0,
        microsecond=0
    )

    return (
        start_time
        <= now
        <= end_time
    )


# ==========================================
# 3. STREAMLIT RENDER LOGIC
# ==========================================
st.title(
    "⚡F&O Institutional Sector Radar"
)

if not ACCESS_TOKEN:

    st.error(
        "⚠️ ACCESS_TOKEN is missing. "
        "Please add ACCESS_TOKEN to Streamlit Secrets."
    )

    st.stop()


with st.spinner(
    "Initializing Market Mapping & Historical Volumes..."
):

    mapped_df = load_instrument_mapping(
        FNO_EXCEL_PATH,
        INSTRUMENTS_CSV_PATH
    )

    unique_keys = (
        mapped_df[
            'instrument_key'
        ]
        .unique()
        .tolist()
    )

    avg_10d_vols = (
        fetch_10d_avg_volumes_throttled(
            unique_keys,
            ACCESS_TOKEN
        )
    )

    # NEW:
    # Previous 20 completed trading sessions.
    daily_history = (
        fetch_20d_daily_history(
            unique_keys,
            ACCESS_TOKEN
        )
    )


# ==========================================
# NEW: SYMBOL -> INSTRUMENT KEY
# ==========================================
symbol_to_key = dict(
    zip(
        mapped_df['SYMBOL'],
        mapped_df['instrument_key']
    )
)


# ==========================================
# NEW: DAY-LEVEL CANDIDATE MEMORY
# ==========================================
if (
    'opening_top30_candidates'
    not in st.session_state
):

    st.session_state[
        'opening_top30_candidates'
    ] = {}


if (
    'candidate_date'
    not in st.session_state
):

    st.session_state[
        'candidate_date'
    ] = datetime.now(
        IST
    ).strftime(
        "%Y-%m-%d"
    )


@st.fragment(
    run_every=(
        REFRESH_INTERVAL_SECONDS
        if is_market_open()
        else None
    )
)
def dashboard_live_loop():

    market_status = (
        is_market_open()
    )

    now = datetime.now(IST)

    now_str = now.strftime(
        "%H:%M:%S IST"
    )

    today_str = now.strftime(
        "%Y-%m-%d"
    )

    # ==========================================
    # RESET DAY MEMORY
    # ==========================================
    if (
        st.session_state.get(
            'candidate_date'
        )
        != today_str
    ):

        st.session_state[
            'opening_top30_candidates'
        ] = {}

        st.session_state[
            'candidate_date'
        ] = today_str

        st.session_state.pop(
            'frozen_df',
            None
        )

        st.session_state.pop(
            'frozen_time',
            None
        )

        st.session_state.pop(
            'frozen_date',
            None
        )

    # Reset cache if a new trading day starts
    if (
        'frozen_date'
        in st.session_state
        and
        st.session_state[
            'frozen_date'
        ]
        != today_str
    ):

        st.session_state.pop(
            'frozen_df',
            None
        )

        st.session_state.pop(
            'frozen_time',
            None
        )

        st.session_state.pop(
            'frozen_date',
            None
        )

    if market_status:

        st.success(
            f"🟢 **MARKET LIVE** — "
            f"Last Updated: {now_str}"
        )

        quotes, api_error = (
            fetch_live_quotes_safe(
                unique_keys,
                ACCESS_TOKEN
            )
        )

        data_df = process_market_data(
            mapped_df,
            quotes,
            avg_10d_vols
        )

        # Existing frozen/latest data behavior
        if not data_df.empty:

            st.session_state[
                'frozen_df'
            ] = data_df

            st.session_state[
                'frozen_time'
            ] = now_str

            st.session_state[
                'frozen_date'
            ] = today_str

    else:

        if (
            'frozen_df'
            in st.session_state
            and
            not st.session_state[
                'frozen_df'
            ].empty
        ):

            data_df = (
                st.session_state[
                    'frozen_df'
                ]
            )

            frozen_at = (
                st.session_state.get(
                    'frozen_time',
                    '3:13:00 IST'
                )
            )

            st.warning(
                "🔴 **MARKET CLOSED — "
                "FROZEN AT 3:13 PM IST** "
                f"(Data locked at: {frozen_at})"
            )

            api_error = None

        else:

            st.warning(
                "🔴 **MARKET CLOSED** — "
                "Fetching final 3:13 PM market snapshot. "
                f"Current time: {now_str}"
            )

            quotes, api_error = (
                fetch_live_quotes_safe(
                    unique_keys,
                    ACCESS_TOKEN
                )
            )

            data_df = process_market_data(
                mapped_df,
                quotes,
                avg_10d_vols
            )

            if not data_df.empty:

                st.session_state[
                    'frozen_df'
                ] = data_df

                st.session_state[
                    'frozen_time'
                ] = now_str

                st.session_state[
                    'frozen_date'
                ] = today_str

    if data_df.empty:

        st.error(
            "⚠️ **Unable to load live quotes "
            "from Upstox API.**"
        )

        if (
            'api_error'
            in locals()
            and api_error
        ):

            st.code(
                "Upstox Response Error Log:\n"
                f"{api_error}",
                language="text"
            )

        return

    # ==========================================
    # SECTOR SUMMARY TABLE
    # EXISTING LOGIC
    # ==========================================
    st.subheader(
        "Sector Performance Breakdown"
    )

    sector_stats = []

    for sector, group in (
        data_df.groupby('SECTOR')
    ):

        avg_chg = (
            group['CHANGE_%'].mean()
        )

        advances = (
            group['CHANGE_%'] > 0
        ).sum()

        declines = (
            group['CHANGE_%'] < 0
        ).sum()

        total_stocks = len(group)

        breadth_ratio = (
            (
                advances
                - declines
            )
            / total_stocks
            if total_stocks > 0
            else 0.0
        )

        top_stock = (
            group.loc[
                group['INST_SCORE'].idxmax()
            ]['SYMBOL']
            if not group.empty
            else "N/A"
        )

        sector_score = (
            avg_chg
            + (
                0.75
                * breadth_ratio
            )
        )

        sector_stats.append({
            "Sector": sector,
            "Avg Change %": (
                format_signed_pct(
                    avg_chg
                )
            ),
            "Adv/Dec": (
                f"{advances}/{declines}"
            ),
            "Breadth Ratio": (
                f"{breadth_ratio:+.2f}"
            ),
            "Avg Order Flow": (
                f"{group['FLOW_RATIO_RAW'].mean():.2f}x"
            ),
            "Top Stock": top_stock,
            "Sector Momentum": (
                "BULLISH"
                if sector_score > 0.3
                else (
                    "BEARISH"
                    if sector_score < -0.3
                    else "NEUTRAL"
                )
            ),
            "_SORT_CHG": avg_chg
        })

    sector_df = (
        pd.DataFrame(
            sector_stats
        )
        .sort_values(
            by="_SORT_CHG",
            ascending=False
        )
        .drop(
            columns=[
                '_SORT_CHG'
            ]
        )
    )

    st.dataframe(
        sector_df,
        use_container_width=True,
        hide_index=True
    )

    # ==========================================
    # TRADINGVIEW CONFIG
    # EXISTING LOGIC
    # ==========================================
    table_column_config = {

        "CHART_URL": st.column_config.LinkColumn(
            "Symbol",
            help=(
                "Click symbol name to "
                "open TradingView chart"
            ),
            display_text=(
                r"symbol=NSE:([^&]+)"
            )
        )

    }

    # ==========================================
    # DEDUPLICATE
    # EXISTING LOGIC
    # ==========================================
    unique_symbols_df = (
        data_df.drop_duplicates(
            subset=['SYMBOL']
        )
    )

    # ==========================================
    # DISPLAY COUNT
    # EXISTING LOGIC
    # ==========================================
    display_option = st.selectbox(
        "Select Number of Stocks to Display:",
        options=[
            "Top 10",
            "Top 20",
            "All"
        ],
        index=0
    )

    if display_option == "Top 10":

        top_n = 10
        label_prefix = "Top 10"

    elif display_option == "Top 20":

        top_n = 20
        label_prefix = "Top 20"

    else:

        top_n = None
        label_prefix = "All"

    col1, col2 = st.columns(2)

    # ==========================================
    # EXISTING BULLISH TABLE
    # ==========================================
    with col1:

        st.subheader(
            f"{label_prefix} "
            "Bullish Momentum Leaders"
        )

        bullish = (
            unique_symbols_df
            .sort_values(
                by='INST_SCORE',
                ascending=False
            )
        )

        if top_n is not None:

            bullish = bullish.head(
                top_n
            )

        bullish = bullish.copy()

        bullish.rename(
            columns={
                'CHANGE_STR': 'Change %',
                'VWAP_DIST_STR': 'VWAP Dist %',
                'INST_SCORE': 'Inst. Score'
            },
            inplace=True
        )

        cols_bullish = [
            'CHART_URL',
            'SECTOR',
            'LTP (₹)',
            'Change %',
            'VWAP Dist %',
            'Volume',
            'Vol / 10D Vol',
            'Order Flow',
            'Inst. Score'
        ]

        st.dataframe(
            bullish[
                cols_bullish
            ],
            column_config=(
                table_column_config
            ),
            use_container_width=True,
            hide_index=True
        )

    # ==========================================
    # EXISTING BEARISH TABLE
    # ==========================================
    with col2:

        st.subheader(
            f"{label_prefix} "
            "Bearish Short Setups"
        )

        bearish = (
            unique_symbols_df
            .sort_values(
                by='INST_SCORE',
                ascending=True
            )
        )

        if top_n is not None:

            bearish = bearish.head(
                top_n
            )

        bearish = bearish.copy()

        bearish.rename(
            columns={
                'CHANGE_STR': 'Change %',
                'VWAP_DIST_STR': 'VWAP Dist %',
                'INST_SCORE': 'Inst. Score'
            },
            inplace=True
        )

        cols_bearish = [
            'CHART_URL',
            'SECTOR',
            'LTP (₹)',
            'Change %',
            'VWAP Dist %',
            'Volume',
            'Vol / 10D Vol',
            'Order Flow',
            'Inst. Score'
        ]

        st.dataframe(
            bearish[
                cols_bearish
            ],
            column_config=(
                table_column_config
            ),
            use_container_width=True,
            hide_index=True
        )

    # ======================================================
    # NEW TABLE
    # OPENING TOP-30 COMBINED RANKING
    # ======================================================
    st.divider()

    st.subheader(
        "🏆 Opening Top-30 Combined Ranking"
    )

    st.caption(
        "Every stock that enters either the Bullish Top-30 "
        "or Bearish Top-30 during the trading day is retained "
        "for this table even if it later leaves Top-30."
    )

    # ==========================================
    # CURRENT TOP 30 BULLISH
    # ==========================================
    current_bullish_top30 = (
        unique_symbols_df
        .sort_values(
            by='INST_SCORE',
            ascending=False
        )
        .head(30)
    )

    # ==========================================
    # CURRENT TOP 30 BEARISH
    # ==========================================
    current_bearish_top30 = (
        unique_symbols_df
        .sort_values(
            by='INST_SCORE',
            ascending=True
        )
        .head(30)
    )

    candidate_state = (
        st.session_state[
            'opening_top30_candidates'
        ]
    )

    # ==========================================
    # RETAIN EVERY BULLISH TOP-30 ENTRY
    # ==========================================
    for _, candidate in (
        current_bullish_top30.iterrows()
    ):

        symbol = candidate[
            'SYMBOL'
        ]

        if symbol not in candidate_state:

            candidate_state[
                symbol
            ] = {
                'direction': 'BULLISH',
                'entered_at': now_str
            }

        else:

            # If it appears in bullish Top-30 now,
            # keep/update its current direction.
            candidate_state[
                symbol
            ]['direction'] = (
                'BULLISH'
            )

    # ==========================================
    # RETAIN EVERY BEARISH TOP-30 ENTRY
    # ==========================================
    for _, candidate in (
        current_bearish_top30.iterrows()
    ):

        symbol = candidate[
            'SYMBOL'
        ]

        if symbol not in candidate_state:

            candidate_state[
                symbol
            ] = {
                'direction': 'BEARISH',
                'entered_at': now_str
            }

        else:

            # Same stock can move from bullish
            # to bearish during the day.
            candidate_state[
                symbol
            ]['direction'] = (
                'BEARISH'
            )

    retained_symbols = list(
        candidate_state.keys()
    )

    # ==========================================
    # NEW ORB DATA
    # ==========================================
    orb_data = (
        fetch_orb_data_for_symbols_cached(
            tuple(sorted(symbol_to_key.items())),
            tuple(sorted(retained_symbols)),
            ACCESS_TOKEN,
            today_str
        )
    )

    # ==========================================
    # NEW OPTIONS OI + PCR DATA
    # ==========================================
    options_oi_data = (
        fetch_options_oi_pcr_cached(
            tuple(sorted(symbol_to_key.items())),
            tuple(sorted(retained_symbols)),
            ACCESS_TOKEN,
            today_str
        )
    )

    # ==========================================
    # BUILD COMBINED RANKING
    # ==========================================
    combined_rank_df = (
        build_combined_rank_table(
            candidate_state,
            data_df,
            mapped_df,
            daily_history,
            orb_data,
            options_oi_data
        )
    )

    if combined_rank_df.empty:

        st.info(
            "No stock has entered the "
            "Bullish/Bearish Top-30 list yet."
        )

    else:

        st.write(
            f"**Retained candidates today: "
            f"{len(combined_rank_df)}**"
        )

        combined_column_config = {

            "CHART_URL": (
                st.column_config.LinkColumn(
                    "Symbol",
                    help=(
                        "Click symbol name to "
                        "open TradingView chart"
                    ),
                    display_text=(
                        r"symbol=NSE:([^&]+)"
                    )
                )
            )

        }

        st.dataframe(
            combined_rank_df,
            column_config=(
                combined_column_config
            ),
            use_container_width=True,
            hide_index=True
        )

        st.caption(
            "Ranking = Price + Volume + ORB + OI Build + PCR + PCR OI Change. "
            "Price: 1D/2D/5D/10D/20D high for bullish "
            "and corresponding low for bearish. "
            "Volume: current cumulative volume versus "
            "previous 1D/5D/10D/20D volume. "
            "ORB = 09:15–09:45; breakout = +3, maintained momentum = +5 total, "
            "return inside ORB = -3. "
            "OI Build uses current-month option OI versus previous OI. "
            "PCR = total PE OI / total CE OI. "
            "PCR OI Chg % = change in PCR calculated from current versus previous OI. "
            "Each OI/PCR component contributes a direction-aware rank from 0 to 5."
        )


dashboard_live_loop()
