import os
import sys
import time
import json
import requests
import subprocess
from datetime import datetime, timedelta
from google.cloud import storage

# ==============================================================================
# STRATEGY & CLOUD STORAGE CONFIGURATION
# ==============================================================================
API_KEY = os.environ.get("MASSIVE_API_KEY")
TARGET_GAIN = 0.50             # 50% minimum close-to-close gain to qualify
MIN_LIQUIDITY_VOLUME = 100000  # Filter out zero-volume zombie tickers
MIN_PRICE_FLOOR = 1.00         # Filter out sub-dollar trash to control borrow fees

BUCKET_NAME = "momentum-lifecycle-vault"   # Replace with your unique GCP Bucket name
GCS_KEY_PATH = os.path.expanduser("~/.credentials/gcs_tracker_key.json")

CACHE_FILE = os.path.expanduser("~/tracker/history.json")
INTRADAY_DIR = os.path.expanduser("~/tracker/intraday")
WEB_DIR = os.path.expanduser("~/tracker/www")

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

def get_market_days_needed(count=6):
    days = []
    check_date = datetime.now() - timedelta(days=1) 
    while len(days) < count:
        if check_date.weekday() < 5:
            days.append(check_date.strftime("%Y-%m-%d"))
        check_date -= timedelta(days=1)
    days.reverse()  
    return days

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
                for bar in results:
                    bar_utc = datetime.utcfromtimestamp(bar["t"] / 1000.0)
                    bar_est = bar_utc + timedelta(hours=-4)
                    parsed_minutes.append({
                        "time": bar_est.strftime("%I:%M %p"),
                        "open": float(bar["o"]),
                        "high": float(bar["h"]),
                        "low": float(bar["l"]),
                        "close": float(bar["c"]),
                        "volume": int(bar["v"])
                    })
                
                local_file = save_intraday_json(ticker, date_str, parsed_minutes)
                
                # ──> DATA PIPELINE GOES TO CLOUD BUCKET ONLY
                upload_file_to_gcs(local_file, f"intraday/{ticker}_intraday.json")
                
                timestamp_ms = hod_bar["t"]
                utc_dt = datetime.utcfromtimestamp(timestamp_ms / 1000.0)
                est_dt = utc_dt + timedelta(hours=-4)
                
                market_open = est_dt.replace(hour=9, minute=30, second=0, microsecond=0)
                market_close = est_dt.replace(hour=16, minute
