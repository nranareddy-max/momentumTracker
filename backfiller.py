import os
import sys
import time
import json
import requests
from datetime import datetime, timedelta
from google.cloud import storage

# ==============================================================================
# STRATEGY & CLOUD STORAGE CONFIGURATION
# ==============================================================================
API_KEY = os.environ.get("MASSIVE_API_KEY")
TARGET_GAIN = 0.50             # 50% minimum close-to-close gain to qualify
MIN_LIQUIDITY_VOLUME = 100000  # Filter out zero-volume zombie tickers
MIN_PRICE_FLOOR = 1.00         # Filter out sub-dollar trash to control borrow fees

BUCKET_NAME = "momentum-lifecycle-vault"   # Your unique GCP Bucket name
GCS_KEY_PATH = os.path.expanduser("~/.credentials/gcs_tracker_key.json")

CACHE_FILE = os.path.expanduser("~/tracker/history.json")
INTRADAY_DIR = os.path.expanduser("~/tracker/intraday")
CHECKPOINT_FILE = os.path.expanduser("~/tracker/backfill_checkpoint.json")

if not API_KEY:
    print("ERROR: MASSIVE_API_KEY environment variable is missing.")
    sys.exit(1)

os.makedirs(INTRADAY_DIR, exist_ok=True)

# ==============================================================================
# CORE FILTERING & API ACCESS FUNCTIONS
# ==============================================================================
def is_common_stock_symbol(ticker):
    if len(ticker) == 5 and ticker[-1] in ['W', 'R', 'U']:
        return False
    if 'WS' in ticker or '+' in ticker or '.' in ticker:
        return False
    return True

def get_market_days_range(start_date_str, end_date_str):
    """Generates all weekday dates between start and end (inclusive) sorted forward."""
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
    
    dates = []
    curr_dt = start_dt
    while curr_dt <= end_dt:
        if curr_dt.weekday() < 5:  # Monday through Friday
            dates.append(curr_dt.strftime("%Y-%m-%d"))
        curr_dt += timedelta(days=1)
    return dates

def get_bulk_data(date_str):
    url = f"https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/{date_str}?adjusted=true&apiKey={API_KEY}"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        if "results" in data:
            return {
                item["T"]: {"c": item["c"], "v": int(item["v"])} 
                for item in data["results"] 
                if is_common_stock_symbol(item["T"])
            }
    return {}

def get_ticker_details(ticker):
    url = f"https://api.massive.com/v3/reference/tickers/{ticker}?apiKey={API_KEY}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json().get("results", {})
    return {}

def save_intraday_json(ticker, date_str, parsed_minutes):
    file_path = os.path.join(INTRADAY_DIR, f"{ticker}_intraday.json")
    data = {}
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                data = json.load(f)
        except Exception:
            pass
    data[date_str] = parsed_minutes
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2)
    return file_path

def get_high_of_day_metrics(ticker, date_str):
    url = f"https://api.massive.com/v2/aggs/ticker/{ticker}/range/1/minute/{date_str}/{date_str}?adjusted=true&sort=asc&apiKey={API_KEY}"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            results = response.json().get("results", [])
            if results:
                hod_bar = max(results, key=lambda x: x["h"])
                lod_bar = min(results, key=lambda x: x["l"])
                
                parsed_minutes = []
                cum_vol = 0
                cum_pv = 0
                
                for bar in results:
                    bar_utc = datetime.utcfromtimestamp(bar["t"] / 1000.0)
                    bar_est = bar_utc + timedelta(hours=-4)
                    
                    v = int(bar["v"])
                    p = float(bar["c"])
                    cum_vol += v
                    cum_pv += (p * v)
                    current_vwap = cum_pv / cum_vol if cum_vol > 0 else p
                    
                    parsed_minutes.append({
                        "time": bar_est
