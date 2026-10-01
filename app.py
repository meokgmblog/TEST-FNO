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
    page_title="F&O Institutional Sector & Money Flow Radar",
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
    if vol >= 1_000_000:
        return f"{vol / 1_000_000:.2f}M"
    elif vol >= 1_000:
        return f"{vol / 1_000:.1f}K"
    return str(int(vol))

def format_signed_pct(val):
    return f"+{val:.2f}%" if val > 0 else f"{val:.2f}%"

def format_money(val_cr):
    """Formats institutional cash flow into Crores (₹)."""
    if abs(val_cr) >= 100:
        return f"₹{val_cr:+,.1f} Cr"
    elif abs(val_cr) >= 1:
        return f"₹{val_cr:+,.2f} Cr"
    else:
        return f"₹{val_cr * 100:+,.1f} Lakhs"

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

@st.cache_data(ttl=3600)
def fetch_historical_candles_20d(instrument_keys, access_token):
    today = datetime.now(IST)
    from_date = (today - timedelta(days=35)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {access_token}'}
    
    history_data = {}
    def fetch_one(key):
        encoded_key = urllib.parse.quote(key, safe='|:')
        url = f"https://api.upstox.com/v2/historical-candle/{encoded_key}/day/{to_date}/{from_date}"
        try:
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200:
                candles = res.json().get('data', {}).get('candles', [])
                if candles:
                    return key, candles
        except Exception:
            pass
        return key, []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_one, k) for k in instrument_keys]
        for future in as_completed(futures):
            k, candles = future.result()
            history_data[k] = candles
            history_data[k.replace('|', ':')] = candles
            history_data[k.replace(':', '|')] = candles
    return history_data

@st.cache_data(ttl=300)
def fetch_intraday_candles_today(instrument_keys, access_token):
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {access_token}'}
    intraday_data = {}
    def fetch_intra(key):
        encoded_key = urllib.parse.quote(key, safe='|:')
        url = f"https://api.upstox.com/v2/historical-candle/{encoded_key}/5minute"
        try:
            res = requests.get(url, headers=headers, timeout=4)
            if res.status_code == 200:
                candles = res.json().get('data', {}).get('candles', [])
                if candles:
                    return key, candles
        except Exception:
            pass
        return key, []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_intra, k) for k in instrument_keys]
        for future in as_completed(futures):
            k, candles = future.result()
            intraday_data[k] = candles
            intraday_data[k.replace('|', ':')] = candles
            intraday_data[k.replace(':', '|')] = candles
    return intraday_data

def fetch_live_quotes_safe(instrument_keys, access_token):
    headers = {'Accept': 'application/json', 'Authorization': f'Bearer {access_token}'}
    url = "https://api.upstox.com/v2/market-quote/quotes"
    
    # Safe chunking (Upstox limit is 500, using 200 to be completely safe against URL length limits)
    batch_size = 200
    batches = [instrument_keys[i:i + batch_size] for i in range(0, len(instrument_keys), batch_size)]
    
    quotes_data = {}
    last_error = None

    for idx, chunk in enumerate(batches):
        keys_param = ",".join(chunk)
        try:
            # Let requests handle the url-encoding securely via 'params' dictionary
            res = requests.get(url, headers=headers, params={'instrument_key': keys_param}, timeout=8)
            
            if res.status_code == 200:
                resp_json = res.json()
                if resp_json.get('status') == 'success':
                    quotes_data.update(resp_json.get('data', {}))
                else:
                    last_error = f"API Error Status: {resp_json}"
            elif res.status_code == 429:
                last_error = "HTTP 429 Rate Limit hit. Retrying..."
                time.sleep(0.5)
                res_retry = requests.get(url, headers=headers, params={'instrument_key': keys_param}, timeout=8)
                if res_retry.status_code == 200 and res_retry.json().get('status') == 'success':
                    quotes_data.update(res_retry.json().get('data', {}))
                else:
                    last_error = f"HTTP 429 Retry Failed: {res_retry.text}"
            else:
                last_error = f"HTTP {res.status_code}: {res.text}"
        except Exception as e:
            last_error = str(e)
            
        if idx < len(batches) - 1:
            time.sleep(0.1)

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
        total_qty_sum = buy_qty + sell_qty
        
        if total_qty_sum > 0:
            raw_flow = (buy_qty / total_qty_sum) * 2.0
            flow_ratio = max(0.1, min(10.0, raw_flow if buy_qty > sell_qty else (2.0 - (sell_qty / total_qty_sum) * 2.0)))
        else:
            flow_ratio = 1.0

        turnover_cr = (ltp * volume) / 10.0**7
        if total_qty_sum > 0:
            net_money_flow_cr = turnover_cr * ((buy_qty - sell_qty) / total_qty_sum)
        else:
            net_money_flow_cr = turnover_cr * (1.0 if p_change > 0 else -1.0) if volume > 0 else 0.0
        
        avg_vol = avg_10d_vol_dict.get(key, 0.0) or avg_10d_vol_dict.get(symbol, 0.0)
        vol_ratio = (volume / avg_vol) if avg_vol > 0 else 1.0
        vol_ratio_capped = min(vol_ratio, 10.0)
        
        vwap_dist = ((ltp - vwap) / vwap * 100) if vwap > 0 else 0.0
        inst_score = p_change + (vwap_dist * 0.8) + (net_money_flow_cr * 0.1) + ((vol_ratio_capped - 1) * 0.5)

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
            'NET_MONEY_FLOW_CR': round(net_money_flow_cr, 4),
            'Net Money Flow': format_money(net_money_flow_cr),
            'INST_SCORE': round(inst_score, 2),
        })
    df = pd.DataFrame(records)
    if not df.empty and 'NET_MONEY_FLOW_CR' not in df.columns:
        df['NET_MONEY_FLOW_CR'] = 0.0
    return df

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
st.title("⚡F&O Institutional Sector & Money Flow Radar")

with st.spinner("Initializing Market Mapping & Historical Volumes..."):
    mapped_df = load_instrument_mapping(FNO_EXCEL_PATH, INSTRUMENTS_CSV_PATH)
    unique_keys = mapped_df['instrument_key'].unique().tolist()
    avg_10d_vols = fetch_10d_avg_volumes_throttled(unique_keys, ACCESS_TOKEN)
    hist_20d_data = fetch_historical_candles_20d(unique_keys, ACCESS_TOKEN)

@st.fragment(run_every=REFRESH_INTERVAL_SECONDS if is_market_open() else None)
def dashboard_live_loop():
    market_status = is_market_open()
    now = datetime.now(IST)
    now_str = now.strftime("%H:%M:%S IST")
    today_str = now.strftime("%Y-%m-%d")

    if 'frozen_date' in st.session_state and st.session_state['frozen_date'] != today_str:
        st.session_state.pop('frozen_df', None)
        st.session_state.pop('frozen_time', None)
        st.session_state.pop('frozen_date', None)
        st.session_state.pop('tracked_session_symbols', None)

    if market_status:
        st.success(f"🟢 **MARKET LIVE** — Last Updated: {now_str}")
        quotes, api_error = fetch_live_quotes_safe(unique_keys, ACCESS_TOKEN)
        data_df = process_market_data(mapped_df, quotes, avg_10d_vols)
        
        if not data_df.empty:
            st.session_state['frozen_df'] = data_df
            st.session_state['frozen_time'] = now_str
            st.session_state['frozen_date'] = today_str
    else:
        if 'frozen_df' in st.session_state and not st.session_state['frozen_df'].empty:
            data_df = st.session_state['frozen_df']
            frozen_at = st.session_state.get('frozen_time', '3:13:00 IST')
            st.warning(f"🔴 **MARKET CLOSED — FROZEN AT 3:13 PM IST** (Data locked at: {frozen_at})")
            api_error = None
        else:
            st.warning(f"🔴 **MARKET CLOSED** — Fetching final 3:13 PM market snapshot. Current time: {now_str}")
            quotes, api_error = fetch_live_quotes_safe(unique_keys, ACCESS_TOKEN)
            data_df = process_market_data(mapped_df, quotes, avg_10d_vols)
            if not data_df.empty:
                st.session_state['frozen_df'] = data_df
                st.session_state['frozen_time'] = now_str
                st.session_state['frozen_date'] = today_str

    if data_df.empty or 'NET_MONEY_FLOW_CR' not in data_df.columns:
        st.error("⚠️ **Unable to load live quotes or parse required columns from Upstox API.**")
        if 'api_error' in locals() and api_error:
            st.code(f"Upstox Response Error Log:\n{api_error}", language="text")
        return

    # --- Sector Summary Table ---
    st.subheader("Sector Performance & Institutional Net Inflow Breakdown")
    sector_stats = []
    for sector, group in data_df.groupby('SECTOR'):
        avg_chg = group['CHANGE_%'].mean()
        total_inflow = group['NET_MONEY_FLOW_CR'].sum() if 'NET_MONEY_FLOW_CR' in group.columns else 0.0
        advances = (group['CHANGE_%'] > 0).sum()
        declines = (group['CHANGE_%'] < 0).sum()
        total_stocks = len(group)
        breadth_ratio = (advances - declines) / total_stocks if total_stocks > 0 else 0.0
        top_stock = group.loc[group['INST_SCORE'].idxmax()]['SYMBOL'] if not group.empty else "N/A"
        sector_score = avg_chg + (0.75 * breadth_ratio) + (total_inflow * 0.01)
        
        sector_stats.append({
            "Sector": sector,
            "Avg Change %": format_signed_pct(avg_chg),
            "Net Money Flow": format_money(total_inflow),
            "Adv/Dec": f"{advances}/{declines}",
            "Breadth Ratio": f"{breadth_ratio:+.2f}",
            "Top Stock": top_stock,
            "Sector Sentiment": "BULLISH" if sector_score > 0.3 else ("BEARISH" if sector_score < -0.3 else "NEUTRAL"),
            "_SORT_INFLOW": total_inflow
        })
    
    sector_df = pd.DataFrame(sector_stats).sort_values(by="_SORT_INFLOW", ascending=False).drop(columns=['_SORT_INFLOW'])
    st.dataframe(sector_df, use_container_width=True, hide_index=True)

    table_column_config = {
        "CHART_URL": st.column_config.LinkColumn(
            "Symbol",
            help="Click symbol name to open TradingView chart",
            display_text=r"symbol=NSE:([^&]+)"
        )
    }

    unique_symbols_df = data_df.drop_duplicates(subset=['SYMBOL'])

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
        st.subheader(f"{label_prefix} Bullish Institutional Inflow Leaders")
        bullish = unique_symbols_df.sort_values(by='NET_MONEY_FLOW_CR', ascending=False)
        bullish_display = bullish.head(top_n) if top_n is not None else bullish
        bullish_display = bullish_display.copy()
        bullish_display.rename(columns={'CHANGE_STR': 'Change %', 'VWAP_DIST_STR': 'VWAP Dist %', 'INST_SCORE': 'Inst. Score'}, inplace=True)
        cols_bullish = ['CHART_URL', 'SECTOR', 'LTP (₹)', 'Change %', 'Net Money Flow', 'VWAP Dist %', 'Volume', 'Vol / 10D Vol', 'Inst. Score']
        st.dataframe(bullish_display[cols_bullish], column_config=table_column_config, use_container_width=True, hide_index=True)

    with col2:
        st.subheader(f"{label_prefix} Bearish Institutional Outflow Leaders")
        bearish = unique_symbols_df.sort_values(by='NET_MONEY_FLOW_CR', ascending=True)
        bearish_display = bearish.head(top_n) if top_n is not None else bearish
        bearish_display = bearish_display.copy()
        bearish_display.rename(columns={'CHANGE_STR': 'Change %', 'VWAP_DIST_STR': 'VWAP Dist %', 'INST_SCORE': 'Inst. Score'}, inplace=True)
        cols_bearish = ['CHART_URL', 'SECTOR', 'LTP (₹)', 'Change %', 'Net Money Flow', 'VWAP Dist %', 'Volume', 'Vol / 10D Vol', 'Inst. Score']
        st.dataframe(bearish_display[cols_bearish], column_config=table_column_config, use_container_width=True, hide_index=True)

    # =========================================================================
    # 4. CUMULATIVE TOP 30 INSTITUTIONAL MONEY FLOW & 09:45 ORB LEADERBOARD
    # =========================================================================
    st.markdown("---")
    st.subheader("🏆 Cumulative Top 30 Institutional Money Flow & 09:45 ORB Leaderboard")
    st.caption("Ranked via professional institutional net capital turnover (₹ Crores), 20-day high/low breakout zones, multi-period volume expansion, and 09:45 ORB breakouts.")

    curr_top30_bull = set(unique_symbols_df.sort_values(by='NET_MONEY_FLOW_CR', ascending=False).head(30)['SYMBOL'].tolist())
    curr_top30_bear = set(unique_symbols_df.sort_values(by='NET_MONEY_FLOW_CR', ascending=True).head(30)['SYMBOL'].tolist())
    curr_combined_30 = curr_top30_bull.union(curr_top30_bear)

    if 'tracked_session_symbols' not in st.session_state:
        st.session_state['tracked_session_symbols'] = {}

    for sym in curr_combined_30:
        if sym not in st.session_state['tracked_session_symbols']:
            bias = 'BULLISH' if sym in curr_top30_bull else 'BEARISH'
            st.session_state['tracked_session_symbols'][sym] = bias

    tracked_symbols_list = list(st.session_state['tracked_session_symbols'].keys())

    if tracked_symbols_list:
        intraday_data_map = fetch_intraday_candles_today(unique_keys, ACCESS_TOKEN)
        
        ranked_records = []
        for sym in tracked_symbols_list:
            row_data = unique_symbols_df[unique_symbols_df['SYMBOL'] == sym]
            if row_data.empty:
                continue
            r = row_data.iloc[0]
            ltp = r['LTP (₹)']
            current_vol = r['VOLUME_RAW']
            net_money_cr = r['NET_MONEY_FLOW_CR']
            instrument_key = mapped_df[mapped_df['SYMBOL'] == sym]['instrument_key'].values
            if len(instrument_key) == 0:
                continue
            ikey = instrument_key[0]
            
            # --- FACTOR 1: N-Day High/Low Price Zone Ranking (Up to 20 days) ---
            candles_20d = hist_20d_data.get(ikey, []) or hist_20d_data.get(ikey.replace('|', ':'), [])
            price_rank_score = 0
            days_high_hit = 0
            if len(candles_20d) >= 2:
                daily_highs = [c[2] for c in candles_20d[1:21]]
                daily_lows = [c[3] for c in candles_20d[1:21]]
                
                for idx_d, (h_val, l_val) in enumerate(zip(daily_highs, daily_lows)):
                    day_lookback = idx_d + 1
                    if ltp >= h_val:
                        days_high_hit = day_lookback
                        price_rank_score = max(price_rank_score, day_lookback * 4)
                    elif ltp <= l_val:
                        days_high_hit = -day_lookback
                        price_rank_score = max(price_rank_score, day_lookback * 4)

            # --- FACTOR 2: Multi-Period Volume Spikes ---
            vol_multiplier_score = 0.0
            if len(candles_20d) >= 21:
                vols_list = [c[5] for c in candles_20d[1:21]]
                vol_1d_avg = vols_list[0] if len(vols_list) > 0 else 1
                vol_5d_avg = sum(vols_list[:5]) / 5 if len(vols_list) >= 5 else vol_1d_avg
                vol_10d_avg = sum(vols_list[:10]) / 10 if len(vols_list) >= 10 else vol_5d_avg
                vol_20d_avg = sum(vols_list) / len(vols_list) if len(vols_list) > 0 else 1
                
                r_1d = current_vol / vol_1d_avg if vol_1d_avg > 0 else 1.0
                r_5d = current_vol / vol_5d_avg if vol_5d_avg > 0 else 1.0
                r_10d = current_vol / vol_10d_avg if vol_10d_avg > 0 else 1.0
                r_20d = current_vol / vol_20d_avg if vol_20d_avg > 0 else 1.0
                
                vol_multiplier_score = (r_1d * 0.5) + (r_5d * 1.0) + (r_10d * 1.5) + (r_20d * 2.0)

            # --- FACTOR 3: Fixed 09:45 Opening Range Break (ORB) Engine ---
            orb_status = "No Break"
            orb_score = 0.0
            intra_candles = intraday_data_map.get(ikey, []) or intraday_data_map.get(ikey.replace('|', ':'), [])
            if intra_candles:
                opening_candles = []
                for ic in intra_candles:
                    try:
                        timestamp_val = ic[0]
                        if isinstance(timestamp_val, (int, float)):
                            dt_candle = datetime.fromtimestamp(timestamp_val / 1000.0, tz=IST)
                        else:
                            dt_candle = pd.to_datetime(timestamp_val)
                            if dt_candle.tzinfo is None:
                                dt_candle = dt_candle.tz_localize(IST)
                            else:
                                dt_candle = dt_candle.tz_convert(IST)
                        
                        if dt_candle.strftime("%Y-%m-%d") == today_str:
                            t_val = dt_candle.time()
                            if datetime.strptime("09:15:00", "%H:%M:%S").time() <= t_val <= datetime.strptime("09:45:00", "%H:%M:%S").time():
                                opening_candles.append(ic)
                    except Exception:
                        continue

                if opening_candles:
                    orb_high = max(c[2] for c in opening_candles)
                    orb_low = min(c[3] for c in opening_candles)
                    
                    if orb_high > 0 and orb_low < 999999:
                        if ltp > orb_high:
                            orb_status = "Bullish ORB Break (+)"
                            orb_score = 30.0
                        elif ltp < orb_low:
                            orb_status = "Bearish ORB Break (-)"
                            orb_score = 30.0
                        else:
                            orb_status = "Inside ORB Range"
                            orb_score = 0.0

            # Professional Institutional Composite Score weighting Net Money Flow heavily
            composite_rank_score = abs(net_money_cr) * 1.5 + price_rank_score + vol_multiplier_score + orb_score

            ranked_records.append({
                'SYMBOL': sym,
                'CHART_URL': r['CHART_URL'],
                'SECTOR': r['SECTOR'],
                'Bias': st.session_state['tracked_session_symbols'][sym],
                'LTP (₹)': ltp,
                'Change %': r['CHANGE_STR'],
                'Net Money Flow': r['Net Money Flow'],
                '20D High/Low Zone': f"{days_high_hit:+d}D Zone" if days_high_hit != 0 else "Range Bound",
                'Vol Multiplier': f"{vol_multiplier_score:.1f}pts",
                'ORB 09:45 Status': orb_status,
                'Session Institutional Score': round(composite_rank_score, 2)
            })

        session_ranked_df = pd.DataFrame(ranked_records)
        if not session_ranked_df.empty:
            session_ranked_df = session_ranked_df.sort_values(by='Session Institutional Score', ascending=False).reset_index(drop=True)
            
            st.dataframe(
                session_ranked_df,
                column_config=table_column_config,
                use_container_width=True,
                hide_index=True
            )
    else:
        st.info("Accumulating institutional leader stats for session ranking...")

dashboard_live_loop()
