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

ACCESS_TOKEN = st.secrets.get("ACCESS_TOKEN", "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI2M0FZSEUiLCJqdGkiOiI2YTMwY2UxNTY4ODI0Zjc3ZDc1NmU3NjgiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlzRXh0ZW5kZWQiOnRydWUsImlhdCI6MTc4MTU4MzM4MSwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxODEzMTgzMjAwfQ.IoRDQhbhcn3w9Fkw75N3eBSamLcaA8GcAhVjf5K-iL8")
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

# ==========================================
# 1. DATA LOADING & RATE-LIMITED FETCHING
# ==========================================
@st.cache_data(ttl=86400)
def load_instrument_mapping(excel_path, csv_path):
    fno_df = pd.read_excel(excel_path, engine="openpyxl")
    fno_clean = fno_df.dropna(subset=['SYMBOL', 'SECTOR'])[['SYMBOL', 'SECTOR']].drop_duplicates()
    
    inst_df = pd.read_csv(csv_path)
    nse_eq = inst_df[(inst_df['segment'] == 'NSE_EQ') & (inst_df['instrument_type'] == 'EQ')][['trading_symbol', 'instrument_key', 'name']]
    
    merged = pd.merge(fno_clean, nse_eq, left_on='SYMBOL', right_on='trading_symbol', how='inner')
    return merged.drop_duplicates(subset=['SYMBOL', 'SECTOR']).reset_index(drop=True)

def _fetch_single_10d_vol(key, access_token, to_date, from_date):
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {access_token}'}
    encoded_key = urllib.parse.quote(key, safe='|:')
    url = f"https://api.upstox.com/v2/historical-candle/{encoded_key}/day/{to_date}/{from_date}"
    
    for attempt in range(2):
        try:
            res = requests.get(url, headers=headers, timeout=4)
            if res.status_code == 200:
                candles = res.json().get('data', {}).get('candles', [])
                if candles:
                    vols = [c[5] for c in candles[1:11]]
                    if vols:
                        return key, sum(vols) / len(vols)
            elif res.status_code == 429:
                time.sleep(0.25)
        except Exception:
            pass
    return key, 0.0

@st.cache_data(ttl=86400)
def fetch_10d_avg_volumes_throttled(instrument_keys, access_token):
    today = datetime.now(IST)
    from_date = (today - timedelta(days=20)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")
    
    avg_volumes = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_fetch_single_10d_vol, key, access_token, to_date, from_date)
            for key in instrument_keys
        ]
        for future in as_completed(futures):
            key, vol = future.result()
            avg_volumes[key] = vol
            avg_volumes[key.replace('|', ':')] = vol
            avg_volumes[key.replace(':', '|')] = vol
            
    return avg_volumes

def fetch_live_quotes_safe(instrument_keys, access_token):
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {access_token}'}
    url = "https://api.upstox.com/v2/market-quote/quotes"
    
    batch_size = 250
    batches = [instrument_keys[i:i + batch_size] for i in range(0, len(instrument_keys), batch_size)]
    
    quotes_data = {}
    last_error = None

    for idx, chunk in enumerate(batches):
        keys_param = ",".join(chunk)
        try:
            encoded_params = urllib.parse.urlencode({'instrument_key': keys_param}, safe=',|:')
            res = requests.get(f"{url}?{encoded_params}", headers=headers, timeout=6)
            
            if res.status_code == 200 and res.json().get('status') == 'success':
                quotes_data.update(res.json().get('data', {}))
            elif res.status_code == 429:
                last_error = "HTTP 429 Rate Limit hit. Retrying after brief pause..."
                time.sleep(0.5)
                res_retry = requests.get(f"{url}?{encoded_params}", headers=headers, timeout=6)
                if res_retry.status_code == 200:
                    quotes_data.update(res_retry.json().get('data', {}))
                else:
                    last_error = f"HTTP {res_retry.status_code}: {res_retry.text}"
            else:
                last_error = f"HTTP {res.status_code}: {res.text}"
        except Exception as e:
            last_error = str(e)
            
        if idx < len(batches) - 1:
            time.sleep(0.15)

    return quotes_data, last_error

def process_market_data(mapped_df, quotes_dict, avg_10d_vol_dict):
    records = []
    if not quotes_dict:
        return pd.DataFrame()

    normalized_quotes = {}
    for k, v in quotes_dict.items():
        normalized_quotes[k] = v
        normalized_quotes[k.replace(':', '|')] = v
        normalized_quotes[k.replace('|', ':')] = v
        if ':' in k:
            normalized_quotes[k.split(':')[1]] = v
        if '|' in k:
            normalized_quotes[k.split('|')[1]] = v

    for _, row in mapped_df.iterrows():
        key = row['instrument_key']
        symbol = row['SYMBOL']
        sector = row['SECTOR']
        
        quote = (
            normalized_quotes.get(key) 
            or normalized_quotes.get(symbol) 
            or normalized_quotes.get(f"NSE_EQ:{symbol}")
            or normalized_quotes.get(f"NSE_EQ|{symbol}")
            or {}
        )
        if not quote:
            continue
            
        ltp = float(quote.get('last_price') or 0.0)
        volume = float(quote.get('volume') or 0)
        vwap = float(quote.get('average_price') or ltp)
        
        net_change = quote.get('net_change')
        ohlc = quote.get('ohlc') or {}
        close_price = float(ohlc.get('close') or quote.get('prev_close') or 0.0)

        if net_change is not None and ltp > 0:
            p_change = float(net_change)
            prev_close = ltp - p_change
            p_change = (p_change / prev_close * 100) if prev_close > 0 else 0.0
        elif close_price > 0 and close_price != ltp:
            p_change = ((ltp - close_price) / close_price) * 100
        else:
            p_change = 0.0
        
        buy_qty = float(quote.get('total_buy_quantity') or 0)
        sell_qty = float(quote.get('total_sell_quantity') or 0)
        
        avg_vol = avg_10d_vol_dict.get(key, 0.0) or avg_10d_vol_dict.get(symbol, 0.0)
        vol_ratio = (volume / avg_vol) if avg_vol > 0 else 1.0
        vol_ratio_capped = min(vol_ratio, 10.0)
        
        vwap_dist = ((ltp - vwap) / vwap * 100) if vwap > 0 else 0.0
        flow_ratio = (buy_qty / sell_qty) if sell_qty > 0 else (1.5 if buy_qty > 0 else 1.0)
        
        inst_score = p_change + (vwap_dist * 0.8) + ((flow_ratio - 1) * 2) + ((vol_ratio_capped - 1) * 0.5)

        tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}&interval=5"

        records.append({
            'SYMBOL': symbol,
            'CHART_URL': tv_url,
            'SECTOR': sector,
            'LTP (₹)': round(ltp, 2),
            'CHANGE_%': round(p_change, 2),
            'CHANGE_STR': format_signed_pct(p_change),
            'VWAP_DIST_%': round(vwap_dist, 2),
            'VWAP_DIST_STR': format_signed_pct(vwap_dist),
            'VOLUME_RAW': int(volume),
            'Volume': format_volume(volume),
            'VOL_10D_RATIO_RAW': round(vol_ratio, 2),
            'Vol / 10D Vol': f"{vol_ratio:.2f}x",
            'FLOW_RATIO_RAW': round(flow_ratio, 2),
            'Order Flow': f"{flow_ratio:.2f}x",
            'INST_SCORE': round(inst_score, 2),
        })
    return pd.DataFrame(records)

# ==========================================
# 2. TIME CONTROL
# ==========================================
def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    start_time = now.replace(hour=9, minute=14, second=0, microsecond=0)
    end_time = now.replace(hour=15, minute=13, second=0, microsecond=0)
    return start_time <= now <= end_time

# ==========================================
# 3. STREAMLIT RENDER LOGIC
# ==========================================
st.title("⚡F&O Institutional Sector Radar")

with st.spinner("Initializing Market Mapping & Historical Volumes..."):
    mapped_df = load_instrument_mapping(FNO_EXCEL_PATH, INSTRUMENTS_CSV_PATH)
    unique_keys = mapped_df['instrument_key'].unique().tolist()
    avg_10d_vols = fetch_10d_avg_volumes_throttled(unique_keys, ACCESS_TOKEN)

@st.fragment(run_every=REFRESH_INTERVAL_SECONDS if is_market_open() else None)
def dashboard_live_loop():
    market_status = is_market_open()
    now = datetime.now(IST)
    now_str = now.strftime("%H:%M:%S IST")
    today_str = now.strftime("%Y-%m-%d")

    # Reset cache if a new trading day starts
    if 'frozen_date' in st.session_state and st.session_state['frozen_date'] != today_str:
        st.session_state.pop('frozen_df', None)
        st.session_state.pop('frozen_time', None)
        st.session_state.pop('frozen_date', None)

    if market_status:
        st.success(f"🟢 **MARKET LIVE** — Last Updated: {now_str}")
        quotes, api_error = fetch_live_quotes_safe(unique_keys, ACCESS_TOKEN)
        data_df = process_market_data(mapped_df, quotes, avg_10d_vols)
        
        # Continuously hold the latest market data in memory
        if not data_df.empty:
            st.session_state['frozen_df'] = data_df
            st.session_state['frozen_time'] = now_str
            st.session_state['frozen_date'] = today_str
    else:
        # Market is CLOSED (After 3:13 PM or Before 9:14 AM / Weekends)
        if 'frozen_df' in st.session_state and not st.session_state['frozen_df'].empty:
            data_df = st.session_state['frozen_df']
            frozen_at = st.session_state.get('frozen_time', '3:13:00 IST')
            st.warning(f"🔴 **MARKET CLOSED — FROZEN AT 3:13 PM IST** (Data locked at: {frozen_at})")
            api_error = None
        else:
            # First load after 3:13 PM (fetch once to freeze final state)
            st.warning(f"🔴 **MARKET CLOSED** — Fetching final 3:13 PM market snapshot. Current time: {now_str}")
            quotes, api_error = fetch_live_quotes_safe(unique_keys, ACCESS_TOKEN)
            data_df = process_market_data(mapped_df, quotes, avg_10d_vols)
            if not data_df.empty:
                st.session_state['frozen_df'] = data_df
                st.session_state['frozen_time'] = now_str
                st.session_state['frozen_date'] = today_str

    if data_df.empty:
        st.error("⚠️ **Unable to load live quotes from Upstox API.**")
        if 'api_error' in locals() and api_error:
            st.code(f"Upstox Response Error Log:\n{api_error}", language="text")
        return

    # --- Sector Summary Table ---
    st.subheader("Sector Performance Breakdown")
    sector_stats = []
    for sector, group in data_df.groupby('SECTOR'):
        avg_chg = group['CHANGE_%'].mean()
        advances = (group['CHANGE_%'] > 0).sum()
        declines = (group['CHANGE_%'] < 0).sum()
        total_stocks = len(group)
        breadth_ratio = (advances - declines) / total_stocks if total_stocks > 0 else 0.0
        top_stock = group.loc[group['INST_SCORE'].idxmax()]['SYMBOL'] if not group.empty else "N/A"
        sector_score = avg_chg + (0.75 * breadth_ratio)
        
        sector_stats.append({
            "Sector": sector,
            "Avg Change %": format_signed_pct(avg_chg),
            "Adv/Dec": f"{advances}/{declines}",
            "Breadth Ratio": f"{breadth_ratio:+.2f}",
            "Avg Order Flow": f"{group['FLOW_RATIO_RAW'].mean():.2f}x",
            "Top Stock": top_stock,
            "Sector Momentum": "BULLISH" if sector_score > 0.3 else ("BEARISH" if sector_score < -0.3 else "NEUTRAL"),
            "_SORT_CHG": avg_chg
        })
    
    sector_df = pd.DataFrame(sector_stats).sort_values(by="_SORT_CHG", ascending=False).drop(columns=['_SORT_CHG'])
    st.dataframe(sector_df, use_container_width=True, hide_index=True)

    # --- Table Config for Interactive TradingView Chart Hyperlinks ---
    table_column_config = {
        "CHART_URL": st.column_config.LinkColumn(
            "Symbol",
            help="Click symbol name to open TradingView chart",
            display_text=r"symbol=NSE:([^&]+)"
        )
    }

    # --- Deduplicate Stocks for Leaders ---
    unique_symbols_df = data_df.drop_duplicates(subset=['SYMBOL'])

    # --- Display Count Selection ---
    display_option = st.selectbox(
        "Select Number of Stocks to Display:",
        options=["Top 10", "Top 20", "All"],
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
    with col1:
        st.subheader(f"{label_prefix} Bullish Momentum Leaders")
        bullish = unique_symbols_df.sort_values(by='INST_SCORE', ascending=False)
        if top_n is not None:
            bullish = bullish.head(top_n)
        bullish = bullish.copy()
        bullish.rename(columns={'CHANGE_STR': 'Change %', 'VWAP_DIST_STR': 'VWAP Dist %', 'INST_SCORE': 'Inst. Score'}, inplace=True)
        cols_bullish = ['CHART_URL', 'SECTOR', 'LTP (₹)', 'Change %', 'VWAP Dist %', 'Volume', 'Vol / 10D Vol', 'Order Flow', 'Inst. Score']
        st.dataframe(
            bullish[cols_bullish],
            column_config=table_column_config,
            use_container_width=True,
            hide_index=True
        )

    with col2:
        st.subheader(f"{label_prefix} Bearish Short Setups")
        bearish = unique_symbols_df.sort_values(by='INST_SCORE', ascending=True)
        if top_n is not None:
            bearish = bearish.head(top_n)
        bearish = bearish.copy()
        bearish.rename(columns={'CHANGE_STR': 'Change %', 'VWAP_DIST_STR': 'VWAP Dist %', 'INST_SCORE': 'Inst. Score'}, inplace=True)
        cols_bearish = ['CHART_URL', 'SECTOR', 'LTP (₹)', 'Change %', 'VWAP Dist %', 'Volume', 'Vol / 10D Vol', 'Order Flow', 'Inst. Score']
        st.dataframe(
            bearish[cols_bearish],
            column_config=table_column_config,
            use_container_width=True,
            hide_index=True
        )

dashboard_live_loop()
