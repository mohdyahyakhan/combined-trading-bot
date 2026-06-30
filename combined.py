import requests
import time
import os
from flask import Flask, jsonify
import threading
import pandas as pd
import numpy as np
import json
import math
import ccxt
from datetime import datetime
import pytz
import sys

app = Flask(__name__)

# ===== COMMON CONFIG =====
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

# ===== BOT1 CONFIG - Pump Scanner =====
PUMP_PERCENT_24H = 40
WATCHLIST_DAYS = 2
ATR_PERIOD = 10
ATR_MULTIPLIER = 3
EMA_PERIOD = 300
GIST_ID = "5ef25a569ac5dcb8b1a7425aab22cced"
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"}
GIST_URL = f"https://api.github.com/gists/{GIST_ID}"

WATCHLIST = {}
PAPER_TRADES = {}
total_pnl_lifetime = 0.0

# ===== BOT2 CONFIG - Algo Long Strategy =====
P_L_FILE = "pnl.json"
if os.path.exists(P_L_FILE):
    with open(P_L_FILE, "r") as f:
        total_pnl = json.load(f).get("total_pnl", 0.0)
else:
    total_pnl = 0.0

COINS_CONFIG = {
    "TAOUSDT": {"main_tf": "4h", "confirm_tf": "1h"},
    "BTCUSDT": {"main_tf": "4h", "confirm_tf": "1h"},
    "HYPEUSDT": {"main_tf": "1h", "confirm_tf": "4h"},
    "SOLUSDT": {"main_tf": "1h", "confirm_tf": "4h"}
}
SYMBOLS = list(COINS_CONFIG.keys())
CHECK_INTERVAL_SEC = 600
EMA_PERIOD_ALGO = 50
TP_PCT = 0.02
SL_PCT = 0.01
positions = {}

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "linear"},
})

# ===== COMMON FUNCTIONS =====
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing!", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        res = requests.post(url, data=data, timeout=10)
        if res.status_code!= 200:
            print(f"Telegram API Error: {res.text}", flush=True)
    except Exception as e:
        print(f"Telegram Error: {e}", flush=True)

def send_ntfy_plain(msg):
    topic = NTFY_TOPIC
    if not topic: return
    clean_msg = msg.replace('<b>', '').replace('</b>', '').replace('&lt;', '<').replace('&gt;', '>')
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=clean_msg.encode('utf-8'))
    except Exception as e:
        print(f"ntfy Error: {e}", flush=True)

# ===== BOT1 - GIST FUNCTIONS =====
def gist_get(filename):
    try:
        res = requests.get(GIST_URL, headers=GIST_HEADERS, timeout=10).json()
        content = res['files'][filename]['content']
        data = json.loads(content)
        return data
    except Exception as e:
        print(f"Gist GET error {filename}: {e}", flush=True)
        return None

def gist_save(filename, data):
    try:
        payload = {
            "files": {
                filename: {
                    "content": json.dumps(data, indent=2)
                }
            }
        }
        requests.patch(GIST_URL, headers=GIST_HEADERS, json=payload, timeout=10)
    except Exception as e:
        print(f"Gist SAVE error {filename}: {e}", flush=True)

def get_coindcx_futures_symbols():
    try:
        url = "https://api.coindcx.com/exchange/ticker"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=20)
        data = res.json()
        print(f"Bot1 Debug: CoinDCX returned {len(data)} tickers", flush=True)
        futures_symbols = set()
        for t in data:
            market = t.get('market', '')
            if market.endswith('USDT') and not market.startswith('B-'):
                base = market.replace('_USDT', '').replace('USDT', '')
                symbol = f"{base}USDT"
                futures_symbols.add(symbol)
        print(f"Bot1: CoinDCX se {len(futures_symbols)} USDT pairs mile", flush=True)
        return futures_symbols
    except Exception as e:
        print(f"Bot1: CoinDCX futures list error: {e}", flush=True)
        return set()

def load_watchlist():
    global WATCHLIST
    data = gist_get('watchlist.json')
    if data and isinstance(data, list):
        WATCHLIST = {}
        for symbol in data:
            WATCHLIST[symbol] = {'time': time.time(), 'cross_count': 0, 'last_state': 'not_short'}
        print(f"Gist watchlist loaded: {len(WATCHLIST)} coins", flush=True)
    else:
        print("Gist watchlist empty or failed, keeping memory watchlist", flush=True)

def save_watchlist():
    if not WATCHLIST:
        print("Watchlist empty, skipping Gist save to prevent overwrite", flush=True)
        return
    try:
        symbol_list = list(WATCHLIST.keys())
        gist_save('watchlist.json', symbol_list)
        print(f"Saved {len(symbol_list)} coins to Gist", flush=True)
    except Exception as e:
        print(f"Save watchlist error: {e}", flush=True)

def load_paper_trades():
    global PAPER_TRADES
    data = gist_get('paper_trades.json')
    if data and 'trades' in data:
        PAPER_TRADES = data['trades']
        print(f"Loaded {len(PAPER_TRADES)} paper trades from Gist", flush=True)
    else:
        print("Gist paper_trades empty or failed, keeping memory", flush=True)

def save_paper_trades():
    data = {'trades': PAPER_TRADES}
    gist_save('paper_trades.json', data)

def load_total_pnl():
    global total_pnl_lifetime
    data = gist_get('lifetime_pnl.json')
    if data and 'total_pnl' in data:
        total_pnl_lifetime = data['total_pnl']
        print(f"Loaded Lifetime PnL: {total_pnl_lifetime:.2f}%", flush=True)
    else:
        print("Gist PnL empty or failed, keeping memory value", flush=True)

def save_total_pnl(value):
    global total_pnl_lifetime
    current_gist = gist_get('lifetime_pnl.json')
    if current_gist and 'total_pnl' in current_gist:
        total_pnl_lifetime = max(current_gist['total_pnl'], value)
    else:
        total_pnl_lifetime = value
    data = {'total_pnl': total_pnl_lifetime}
    gist_save('lifetime_pnl.json', data)

def process_pump_alert(symbol, change_24h, price, source, alerted_symbols):
    if symbol in alerted_symbols:
        return
    alerted_symbols.add(symbol)
    cdcx_name = symbol.replace('USDT', '-USDT')
    if symbol not in WATCHLIST:
        WATCHLIST[symbol] = {'time': time.time(), 'cross_count': 0, 'last_state': 'not_short'}
        save_watchlist()
        print(f"Bot1 [{source}]: {cdcx_name} +{change_24h:.2f}% added to watchlist, no TG alert", flush=True)
    else:
        WATCHLIST[symbol]['time'] = time.time()
        save_watchlist()
        print(f"Bot1 [{source}]: {cdcx_name} +{change_24h:.2f}% still pumping, already in watchlist", flush=True)

def calculate_supertrend(df, period=10, multiplier=3):
    df = df.copy()
    df['h-l'] = df['high'] - df['low']
    df['h-pc'] = abs(df['high'] - df['close'].shift(1))
    df['l-pc'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
    df['atr'] = df['tr'].ewm(alpha=1 / period, adjust=False).mean()
    hl2 = (df['high'] + df['low']) / 2
    df['upperband'] = hl2 + (multiplier * df['atr'])
    df['lowerband'] = hl2 - (multiplier * df['atr'])
    df['final_upperband'] = 0.0
    df['final_lowerband'] = 0.0
    df['supertrend'] = True
    df['st_line'] = 0.0
    for i in range(len(df)):
        if i == 0:
            df.loc[df.index[i], 'final_upperband'] = df['upperband'].iloc[i]
            df.loc[df.index[i], 'final_lowerband'] = df['lowerband'].iloc[i]
            df.loc[df.index[i], 'st_line'] = df['upperband'].iloc[i]
            continue
        if (df['upperband'].iloc[i] < df['final_upperband'].iloc[i - 1] or
                df['close'].iloc[i - 1] > df['final_upperband'].iloc[i - 1]):
            df.loc[df.index[i], 'final_upperband'] = df['upperband'].iloc[i]
        else:
            df.loc[df.index[i], 'final_upperband'] = df['final_upperband'].iloc[i - 1]
        if (df['lowerband'].iloc[i] > df['final_lowerband'].iloc[i - 1] or
                df['close'].iloc[i - 1] < df['final_lowerband'].iloc[i - 1]):
            df.loc[df.index[i], 'final_lowerband'] = df['lowerband'].iloc[i]
        else:
            df.loc[df.index[i], 'final_lowerband'] = df['final_lowerband'].iloc[i - 1]
        prev_st = df['supertrend'].iloc[i - 1]
        close_i = df['close'].iloc[i]
        if prev_st and close_i < df['final_lowerband'].iloc[i]:
            df.loc[df.index[i], 'supertrend'] = False
        elif not prev_st and close_i > df['final_upperband'].iloc[i]:
            df.loc[df.index[i], 'supertrend'] = True
        else:
            df.loc[df.index[i], 'supertrend'] = prev_st
        if df['supertrend'].iloc[i]:
            df.loc[df.index[i], 'st_line'] = df['final_lowerband'].iloc[i]
        else:
            df.loc[df.index[i], 'st_line'] = df['final_upperband'].iloc[i]
    ema_raw = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    df['ema_val'] = ema_raw.rolling(window=9, min_periods=1).mean()
    return df

def get_klines_bybit(symbol, interval='5', limit=351):
    url = "https://api.bybit.com/v5/market/kline"
    params = {'category': 'linear', 'symbol': symbol, 'interval': interval, 'limit': limit}
    try:
        res = requests.get(url, params=params, timeout=15).json()
        if res['retCode'] == 0 and res['result']['list']:
            data = res['result']['list']
            if len(data) == 0:
                print(f"Bot2 Debug: Bybit {symbol} empty kline list", flush=True)
                return None
            df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
            df = df.astype({'timestamp': 'int64', 'open': float, 'high': float, 'low': float, 'close': float})
            df = df.iloc[::-1].reset_index(drop=True)
            df = df.iloc[:-1].reset_index(drop=True)
            if len(df) < EMA_PERIOD + 50:
                print(f"Bot2 Debug: Bybit {symbol} only {len(df)} rows", flush=True)
                return None
            return df
        else:
            print(f"Bot2 Debug: Bybit {symbol} retCode={res.get('retCode')} retMsg={res.get('retMsg')}", flush=True)
    except Exception as e:
        print(f"Bybit Kline Error {symbol}: {e}", flush=True)
    return None

def get_klines_coindcx(symbol, interval='5m', limit=351):
    base = symbol.replace('USDT', '')
    pair = f"{base}USDT"
    url = "https://api.coindcx.com/exchange/v1/candles"
    params = {'pair': pair, 'interval': interval, 'limit': limit}
    try:
        res = requests.get(url, params=params, timeout=15)
        print(f"Bot2 Debug: CoinDCX candles {pair} status={res.status_code}", flush=True)
        if res.status_code!= 200:
            return None
        data = res.json()
        if not data or not isinstance(data, list):
            print(f"Bot2 Debug: CoinDCX candles {pair} empty data", flush=True)
            return None
        df = pd.DataFrame(data)
        df = df.rename(columns={'time': 'timestamp'})
        df['timestamp'] = df['timestamp'].astype('int64')
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['close'] = df['close'].astype(float)
        df = df[['timestamp', 'open', 'high', 'low', 'close']]
        df = df.sort_values('timestamp').reset_index(drop=True)
        df = df.iloc[:-1].reset_index(drop=True)
        if len(df) < EMA_PERIOD + 50:
            print(f"Bot2 Debug: CoinDCX candles {pair} only {len(df)} rows", flush=True)
            return None
        return df
    except Exception as e:
        print(f"CoinDCX Kline Error {symbol}: {e}", flush=True)
    return None

def get_klines_binance(symbol, interval='5m', limit=351):
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {'symbol': symbol, 'interval': interval, 'limit': limit}
    try:
        res = requests.get(url, params=params, timeout=15)
        print(f"Bot2 Debug: Binance candles {symbol} status={res.status_code}", flush=True)
        if res.status_code!= 200:
            return None
        data = res.json()
        if not data:
            return None
        df = pd.DataFrame(data, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])
        df = df[['timestamp', 'open', 'high', 'low', 'close']]
        df = df.astype({'timestamp': 'int64', 'open': float, 'high': float,
                       'low': float, 'close': float})
        df = df.iloc[:-1].reset_index(drop=True)
        if len(df) < EMA_PERIOD + 50:
            print(f"Bot2 Debug: Binance {symbol} only {len(df)} rows", flush=True)
            return None
        return df
    except Exception as e:
        print(f"Binance Kline Error {symbol}: {e}", flush=True)
    return None

def get_klines(symbol, interval='5'):
    df = get_klines_bybit(symbol, interval=interval)
    if df is not None:
        print(f"Bot2: [{symbol}] Data from Bybit", flush=True)
        return df
    df = get_klines_binance(symbol, interval=f"{interval}m")
    if df is not None:
        print(f"Bot2: [{symbol}] Data from Binance", flush=True)
        return df
    df = get_klines_coindcx(symbol, interval=f"{interval}m")
    if df is not None:
        print(f"Bot2: [{symbol}] Data from CoinDCX", flush=True)
        return df
    print(f"Bot2: [{symbol}] Data nahi mila kahi se bhi", flush=True)
    return None

def check_paper_trades(df, symbol):
    global total_pnl_lifetime
    if symbol not in PAPER_TRADES:
        return
    trade = PAPER_TRADES[symbol]
    if trade['status']!= 'OPEN':
        return
    current_price = df['close'].iloc[-1]
    tp = trade['tp']
    sl = trade['sl']
    cdcx_name = symbol.replace('USDT', '-USDT')
    entry_time = trade['time']
    if current_price <= tp:
        pnl = ((trade['entry'] - tp) / trade['entry']) * 100
        duration = int((time.time() - entry_time) / 60)
        trade['status'] = 'CLOSED_TP'
        trade['exit'] = tp
        trade['pnl'] = round(pnl, 2)
        trade['exit_time'] = time.time()
        save_total_pnl(total_pnl_lifetime + pnl)
        msg = (
            f"✅ <b>TRADE CLOSED - TARGET HIT</b> ✅\n\n"
            f"<b>Coin:</b> {cdcx_name}\n"
            f"<b>Entry:</b> ${trade['entry']:.6f}\n"
            f"<b>Exit TP:</b> ${tp:.6f}\n"
            f"<b>PnL:</b> +{pnl:.2f}%\n"
            f"<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%\n"
            f"<b>Duration:</b> {duration} min"
        )
        send_telegram(msg)
        send_ntfy_plain(msg)
        print(f"Paper Trade TP: {cdcx_name} +{pnl:.2f}% in {duration}min", flush=True)
    elif current_price >= sl:
        pnl = ((trade['entry'] - sl) / trade['entry']) * 100
        duration = int((time.time() - entry_time) / 60)
        trade['status'] = 'CLOSED_SL'
        trade['exit'] = sl
        trade['pnl'] = round(pnl, 2)
        trade['exit_time'] = time.time()
        save_total_pnl(total_pnl_lifetime + pnl)
        msg = (
            f"❌ <b>TRADE CLOSED - SL HIT</b> ❌\n\n"
            f"<b>Coin:</b> {cdcx_name}\n"
            f"<b>Entry:</b> ${trade['entry']:.6f}\n"
            f"<b>Exit SL:</b> ${sl:.6f}\n"
            f"<b>PnL:</b> {pnl:.2f}%\n"
            f"<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%\n"
            f"<b>Duration:</b> {duration} min"
        )
        send_telegram(msg)
        send_ntfy_plain(msg)
        print(f"Paper Trade SL: {cdcx_name} {pnl:.2f}% in {duration}min", flush=True)
    save_paper_trades()

# ===== BOT1 MAIN LOOP =====
def bot1_scan_bybit_futures():
    load_watchlist()
    print("Bot1 started — Triple Source (Bybit + CoinDCX + Bybit Pump Scan)", flush=True)
    while True:
        alerted_symbols = set()
        try:
            coindcx_futures = get_coindcx_futures_symbols()
            if not coindcx_futures:
                print("Bot1: CoinDCX futures list nahi mili, 60s wait...", flush=True)
                time.sleep(60)
                continue
            try:
                url = "https://api.bybit.com/v5/market/tickers"
                params = {'category': 'linear'}
                data = requests.get(url, params=params, timeout=20).json()
                if data['retCode'] == 0:
                    tickers = data['result']['list']
                    pumped_bybit = 0
                    for ticker in tickers:
                        symbol = ticker['symbol']
                        if not symbol.endswith('USDT'):
                            continue
                        change_24h = float(ticker['price24hPcnt']) * 100
                        if change_24h >= PUMP_PERCENT_24H:
                            process_pump_alert(symbol, change_24h, ticker['lastPrice'], 'Bybit Futures', alerted_symbols)
                            pumped_bybit += 1
                    print(f"Bot1 [Bybit Pump]: {len(tickers)} pairs checked | Pumped: {pumped_bybit}", flush=True)
                else:
                    print(f"Bot1 Bybit API Error: {data['retMsg']}", flush=True)
            except Exception as e:
                print(f"Bot1 Bybit Pump Error: {e}", flush=True)
            bybit_symbols_found = set()
            try:
                url = "https://api.bybit.com/v5/market/tickers"
                params = {'category': 'linear'}
                headers = {'User-Agent': 'Mozilla/5.0'}
                response = requests.get(url, params=params, headers=headers, timeout=20)
                data = response.json()
                if data['retCode'] == 0:
                    tickers = data['result']['list']
                    cdcx_count = 0
                    pumped = 0
                    for ticker in tickers:
                        symbol = ticker['symbol']
                        if symbol not in coindcx_futures:
                            continue
                        cdcx_count += 1
                        bybit_symbols_found.add(symbol)
                        change_24h = float(ticker['price24hPcnt']) * 100
                        if change_24h >= PUMP_PERCENT_24H:
                            process_pump_alert(symbol, change_24h, ticker['lastPrice'], 'Bybit Futures', alerted_symbols)
                            pumped += 1
                    print(f"Bot1 [Bybit Match]: {cdcx_count} pairs checked | Pumped: {pumped}", flush=True)
                else:
                    print(f"Bot1 Bybit API Error: {data['retMsg']}", flush=True)
            except Exception as e:
                print(f"Bot1 Bybit Error: {e}", flush=True)
            try:
                coindcx_only = coindcx_futures - bybit_symbols_found
                url = "https://api.coindcx.com/exchange/ticker"
                headers = {'User-Agent': 'Mozilla/5.0'}
                res = requests.get(url, headers=headers, timeout=20).json()
                cdcx_map = {}
                for t in res:
                    market = t.get('market', '')
                    if market.endswith('USDT'):
                        base = market.replace('_USDT', '').replace('USDT', '')
                        symbol = f"{base}USDT"
                        cdcx_map[symbol] = t
                pumped_cdcx = 0
                for symbol in coindcx_only:
                    if symbol not in cdcx_map:
                        continue
                    ticker = cdcx_map[symbol]
                    try:
                        change_str = str(ticker.get('change_24_hour', ticker.get('change_24h', '0')))
                        change_24h = float(change_str)
                        if pumped_cdcx < 3:
                            print(f"Bot1 Debug: {symbol} change_24h={change_24h}", flush=True)
                        price = ticker.get('last_price', '0')
                        if change_24h >= PUMP_PERCENT_24H:
                            print(f"Bot1 Debug: PUMP FOUND {symbol} {change_24h:.2f}%", flush=True)
                            process_pump_alert(symbol, change_24h, price, 'CoinDCX Futures', alerted_symbols)
                            pumped_cdcx += 1
                    except Exception as e:
                        print(f"Bot1 Debug: {symbol} parse error: {e}", flush=True)
                        continue
                print(f"Bot1 [CoinDCX]: {len(coindcx_only)} pairs checked | Pumped: {pumped_cdcx}", flush=True)
            except Exception as e:
                print(f"Bot1 CoinDCX Error: {e}", flush=True)
            print(f"Bot1: Total Watchlist: {len(WATCHLIST)} coins\n", flush=True)
        except Exception as e:
            print(f"Bot1 Error: {e}", flush=True)
        time.sleep(300)

# ===== BOT2 SUPER TREND SHORT =====
def bot2_supertrend_short():
    print("Bot2 started", flush=True)
    while True:
        global WATCHLIST
        if isinstance(WATCHLIST, list):
            temp_list = WATCHLIST.copy()
            WATCHLIST = {}
            for symbol in temp_list:
                WATCHLIST[symbol] = {'time': time.time(), 'cross_count': 0, 'last_state': 'not_short'}
        try:
            if not WATCHLIST:
                print("Bot2: Watchlist empty, 30s wait...", flush=True)
                time.sleep(30)
                continue
            print(f"\nBot2: ===== NEW CYCLE — {len(WATCHLIST)} coins =====", flush=True)
            to_remove = []
            for symbol, info in list(WATCHLIST.items()):
                cdcx_name = symbol.replace('USDT', '-USDT')
                if time.time() - info['time'] > WATCHLIST_DAYS * 86400:
                    print(f"Bot2: [{cdcx_name}] Expire — remove", flush=True)
                    to_remove.append(symbol)
                    continue
                df = get_klines(symbol)
                if df is None or len(df) < EMA_PERIOD + 2:
                    print(f"Bot2: [{cdcx_name}] SKIP — data nahi mila", flush=True)
                    if info.get('data_fail_count', 0) >= 2:
                        to_remove.append(symbol)
                        print(f"Bot2: [{cdcx_name}] 3x fail — watchlist se remove", flush=True)
                    else:
                        WATCHLIST[symbol]['data_fail_count'] = info.get('data_fail_count', 0) + 1
                    continue
                else:
                    WATCHLIST[symbol]['data_fail_count'] = 0
                try:
                    df = calculate_supertrend(df, ATR_PERIOD, ATR_MULTIPLIER)
                except Exception as e:
                    print(f"Bot2: [{cdcx_name}] ST error: {e}", flush=True)
                    continue
                st_line = df['st_line'].iloc[-1]
                ema_val = df['ema_val'].iloc[-1]
                close_price = df['close'].iloc[-1]
                if any(math.isnan(v) for v in [st_line, ema_val, close_price]):
                    print(f"Bot2: [{cdcx_name}] SKIP — NaN", flush=True)
                    continue
                check_paper_trades(df, symbol)
                price_below_st = close_price < st_line
                st_below_ema = st_line < ema_val
                current_short = price_below_st and st_below_ema
                reset_state = (close_price > st_line) and (st_line > ema_val)
                last_state = info.get('last_state', 'reset')
                new_cross = (last_state == 'reset' and current_short == True)
                print(f"Bot2: [{cdcx_name}] Price={close_price:.6f} | ST={st_line:.6f} | EMA{EMA_PERIOD}={ema_val:.6f} | SHORT={current_short} | NEW_CROSS={new_cross}", flush=True)
                if new_cross:
                    cross_count = info.get('cross_count', 0)
                    if cross_count >= 3:
                        print(f"Bot2: [{cdcx_name}] Limit 3/3 reach — skip", flush=True)
                    else:
                        tp_price = round(close_price * 0.95, 6)
                        sl_price = round(close_price * 1.02, 6)
                        PAPER_TRADES[symbol] = {
                            'entry': close_price,
                            'tp': tp_price,
                            'sl': sl_price,
                            'status': 'OPEN',
                            'time': time.time()
                        }
                        save_paper_trades()
                        msg = (
                            f"📝 <b>PAPER SHORT ENTRY</b> 📝\n\n"
                            f"<b>Coin:</b> {cdcx_name}\n"
                            f"<b>Signal:</b> #{cross_count + 1}/3\n"
                            f"<b>Entry:</b> ${close_price:.6f}\n"
                            f"<b>TP 5%:</b> ${tp_price}\n"
                            f"<b>SL 2%:</b> ${sl_price}\n\n"
                            f"Price &lt; ST &lt; EMA{EMA_PERIOD}"
                        )
                        send_telegram(msg)
                        send_ntfy_plain(msg)
                        WATCHLIST[symbol]['cross_count'] = cross_count + 1
                        print(f"Bot2: [{cdcx_name}] ✅ PAPER SHORT #{cross_count + 1} @ {close_price:.6f}", flush=True)
                if current_short:
                    WATCHLIST[symbol]['last_state'] = 'short'
                elif reset_state:
                    WATCHLIST[symbol]['last_state'] = 'reset'
                save_watchlist()
                time.sleep(1)
            for symbol in to_remove:
                WATCHLIST.pop(symbol, None)
                save_watchlist()
            print(f"Bot2: ===== CYCLE COMPLETE — 30s wait =====\n", flush=True)
        except Exception as e:
            import traceback
            print(f"Bot2 Error: {e}", flush=True)
            print(traceback.format_exc(), flush=True)
        time.sleep(30)

# ===== BOT3 - ALGO LONG STRATEGY =====
def calculate_st_ema_algo(df, atr_period=10, multiplier=3, ema_period=EMA_PERIOD_ALGO):
    high, low, close = df["high"], df["low"], df["close"]
    tr = np.maximum(high - low, np.maximum(abs(high - close.shift()), abs(low - close.shift())))
    atr = tr.rolling(window=atr_period).mean()
    hl2 = (high + low) / 2
    df["ema"] = close.ewm(span=ema_period).mean()
    upper_band = hl2 + (multiplier * atr)
    lower_band = hl2 - (multiplier * atr)
    st = pd.Series(index=df.index, dtype=float)
    st_dir = pd.Series(index=df.index, dtype=int)
    for i in range(1, len(df)):
        prev_st = st.iloc[i-1]
        if np.isnan(prev_st):
            st.iloc[i] = lower_band.iloc[i]
            st_dir.iloc[i] = 1
        else:
            if close.iloc[i] > prev_st:
                st.iloc[i] = max(lower_band.iloc[i], prev_st)
                st_dir.iloc[i] = 1
            else:
                st.iloc[i] = min(upper_band.iloc[i], prev_st)
                st_dir.iloc[i] = -1
    df["st"] = st
    df["st_dir"] = st_dir
    return df

def get_price_data_algo(symbol, timeframe):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=300)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except Exception as e:
        print(f"Algo Error {symbol} {timeframe}: {e}", flush=True)
        return None

def save_pnl_algo():
    with open(P_L_FILE, "w") as f:
        json.dump({"total_pnl": total_pnl}, f)

def check_exit_algo(symbol, current_price):
    global total_pnl
    if symbol not in positions:
        return False
    pos = positions[symbol]
    if current_price >= pos['tp']:
        pnl_pct = ((current_price/pos['entry_price']-1)*100)
        total_pnl += pnl_pct
        save_pnl_algo()
        msg = f"🎯 ALGO {symbol} TP HIT! PnL:{pnl_pct:.2f}% | Total P&L:{total_pnl:.2f}%"
        print(msg, flush=True)
        send_telegram(msg)
        send_ntfy_plain(msg)
        log_trade_algo(symbol, pos, current_price, "TP", pnl_pct)
        del positions[symbol]
        return True
    elif current_price <= pos['sl']:
        pnl_pct = ((current_price/pos['entry_price']-1)*100)
        total_pnl += pnl_pct
        save_pnl_algo()
        msg = f"🛑 ALGO {symbol} SL HIT! PnL:{pnl_pct:.2f}% | Total P&L:{total_pnl:.2f}%"
        print(msg, flush=True)
        send_telegram(msg)
        send_ntfy_plain(msg)
        log_trade_algo(symbol, pos, current_price, "SL", pnl_pct)
        del positions[symbol]
        return True
    return False

def open_position_algo(symbol, entry_price):
    tp = entry_price * (1 + TP_PCT)
    sl = entry_price * (1 - SL_PCT)
    positions[symbol] = {
        'entry_price': entry_price,
        'tp': tp,
        'sl': sl,
        'entry_time': datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    msg = f"🔥 ALGO SIGNAL: {symbol} | Entry:{entry_price:.2f} | TP:{tp:.2f} | SL:{sl:.2f}"
    print(msg, flush=True)
    send_telegram(msg)
    send_ntfy_plain(msg)

def log_trade_algo(symbol, pos, exit_price, exit_type, pnl_pct):
    with open("paper_trades_algo.txt", "a") as f:
        f.write(f"{datetime.now()} | {symbol} | {exit_type} | Entry:{pos['entry_price']:.2f} Exit:{exit_price:.2f} PnL:{pnl_pct:.2f}% | Total:{total_pnl:.2f}%\n")

def check_signal_algo(symbol):
    config = COINS_CONFIG[symbol]
    main_tf = config["main_tf"]
    confirm_tf = config["confirm_tf"]
    df_main = get_price_data_algo(symbol, main_tf)
    if df_main is None:
        return
    current_price = df_main['close'].iloc[-1]
    if check_exit_algo(symbol, current_price):
        return
    if symbol in positions:
        return
    df_confirm = get_price_data_algo(symbol, confirm_tf)
    if df_confirm is None or len(df_main) < 2:
        return
    df_main = calculate_st_ema_algo(df_main)
    df_confirm = calculate_st_ema_algo(df_confirm)
    last_close_main = df_main['close'].iloc[-1]
    last_ema_main = df_main['ema'].iloc[-1]
    last_st_main = df_main['st_dir'].iloc[-1]
    prev_st_main = df_main['st_dir'].iloc[-2]
    last_st_confirm = df_confirm['st_dir'].iloc[-1]
    print(f"Algo {symbol}[{main_tf}]: Price:{last_close_main:.0f} EMA:{last_ema_main:.0f} ST:{last_st_main}", end=" | ", flush=True)
    if last_close_main > last_ema_main and prev_st_main == -1 and last_st_main == 1 and last_st_confirm == 1:
        open_position_algo(symbol, last_close_main)
    else:
        print("No signal", flush=True)

def bot3_algo_long():
    print("Bot3 Algo started. 24/7 Paper Trading Running...", flush=True)
    print(f"TP: {TP_PCT*100}% | SL: {SL_PCT*100}% | EMA: {EMA_PERIOD_ALGO}", flush=True)
    print(f"Timeframes: TAO/BTC=4H, HYPE
