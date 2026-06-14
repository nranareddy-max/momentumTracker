import os
import sys
import time
import json
import requests
from datetime import datetime, timedelta
from google.cloud import storage
from google.auth import default
from google.auth import impersonated_credentials

# ==============================================================================
# STRATEGY & CLOUD STORAGE CONFIGURATION (KEYLESS IMPERSONATION)
# ==============================================================================
API_KEY = os.environ.get("MASSIVE_API_KEY")
TARGET_GAIN = 0.50             # 50% minimum close-to-close gain to qualify
MIN_LIQUIDITY_VOLUME = 100000  # Filter out zero-volume zombie tickers
MIN_PRICE_FLOOR = 1.00         # Filter out sub-dollar trash to control borrow fees

BUCKET_NAME = "momentum-lifecycle-vault"   # Your unique GCP Bucket name
GCS_KEY_PATH = ""                          # LEAVE BLANK - Using Identity Impersonation
SERVICE_ACCOUNT_EMAIL = "storage-bucket-agent@project-be5f51f6-acba-4886-8d6.iam.gserviceaccount.com"

CACHE_FILE = "/home/ubuntu/tracker/history.json"
INTRADAY_DIR = "/home/ubuntu/tracker/intraday"
CHECKPOINT_FILE = "/home/ubuntu/tracker/backfill_checkpoint.json"

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
                        "time": bar_est.strftime("%I:%M %p"),
                        "open": float(bar["o"]),
                        "high": float(bar["h"]),
                        "low": float(bar["l"]),
                        "close": p,
                        "volume": v,
                        "vwap": current_vwap
                    })
                
                local_file = save_intraday_json(ticker, date_str, parsed_minutes)
                upload_file_to_gcs(local_file, f"intraday/{ticker}_intraday.json")
                
                timestamp_ms = hod_bar["t"]
                hod_dt = datetime.utcfromtimestamp(timestamp_ms / 1000.0) + timedelta(hours=-4)
                
                # Opening Range Breakdown Indicators (09:30 - 09:45 AM)
                orb_high = 0.0
                for bar in results:
                    b_utc = datetime.utcfromtimestamp(bar["t"] / 1000.0)
                    b_est = b_utc + timedelta(hours=-4)
                    if b_est.hour == 9 and 30 <= b_est.minute <= 45:
                        if float(bar["h"]) > orb_high: orb_high = float(bar["h"])
                
                orb_status = "Rejected Below" if float(hod_bar["h"]) <= orb_high else "Clean Breakout"
                if hod_dt.hour == 9 and hod_dt.minute <= 45: orb_status = "Morning Peak"
                
                # Backside Rejections & Relief Bounces
                hod_index = 0
                for idx, bar in enumerate(results):
                    if float(bar["h"]) == hod_bar["h"]:
                        hod_index = idx
                        break
                
                backside_bars = parsed_minutes[hod_index:]
                reject_time = "—"
                bounce_time = "—"
                bounce_price = 0.0
                
                if backside_bars:
                    for bm in backside_bars:
                        if bm["close"] < bm["vwap"]:
                            reject_time = bm["time"]
                            break
                    if len(backside_bars) > 5:
                        peak_bounce = max(backside_bars[2:], key=lambda x: x["high"])
                        bounce_time = peak_bounce["time"]
                        bounce_price = peak_bounce["high"]
                
                market_open = hod_dt.replace(hour=9, minute=30, second=0, microsecond=0)
                market_close = hod_dt.replace(hour=16, minute=0, second=0, microsecond=0)
                
                if hod_dt < market_open: duration_str = "Premarket"
                elif hod_dt > market_close: duration_str = "Post-Hours"
                else:
                    diff = hod_dt - market_open
                    duration_str = f"{int(diff.total_seconds() // 60)}m from Open"
                
                return {
                    "high": float(hod_bar["h"]),
                    "low": float(lod_bar["l"]),
                    "time": hod_dt.strftime("%I:%M %p"),
                    "duration": duration_str,
                    "peak_min_vol": int(hod_bar["v"]),
                    "orb": orb_status,
                    "reject": reject_time,
                    "bounce_t": bounce_time,
                    "bounce_p": bounce_price
                }
    except Exception:
        pass
    return {"high": 0.0, "low": 0.0, "time": "N/A", "duration": "—", "peak_min_vol": 0, "orb": "—", "reject": "—", "bounce_t": "—", "bounce_p": 0.0}

# ==============================================================================
# KEYLESS SERVICE ACCOUNT IMPERSONATION HANDSHAKE LAYER
# ==============================================================================
def upload_file_to_gcs(local_file_path, destination_blob_name):
    try:
        # 1. Acquire base VM Compute engine workspace credentials
        base_creds, project = default()
        
        # 2. Impersonate the storage-bucket-agent role account dynamically
        target_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
        impersonated_creds = impersonated_credentials.Credentials(
            source_credentials=base_creds,
            target_principal=SERVICE_ACCOUNT_EMAIL,
            target_scopes=target_scopes,
            lifetime=3600
        )
        
        # 3. Stream data package chunk directly to GCS
        storage_client = storage.Client(credentials=impersonated_creds)
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(destination_blob_name)
        blob.upload_from_filename(local_file_path, content_type='application/json')
        return True
    except Exception:
        return False

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f: return json.load(f)
        except Exception: pass
    return {"market_sessions": {}, "ticker_metadata": {}, "hod_matrix": {}}

def save_cache(cache_data):
    with open(CACHE_FILE, "w") as f: json.dump(cache_data, f, indent=2)
    upload_file_to_gcs(CACHE_FILE, "history.json")

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r") as f: return json.load(f).get("completed_dates", [])
    return []

def save_checkpoint(completed_dates):
    with open(CHECKPOINT_FILE, "w") as f: json.dump({"completed_dates": completed_dates}, f, indent=2)
    upload_file_to_gcs(CHECKPOINT_FILE, "backfill_checkpoint.json")

# ==============================================================================
# CHUNKED EXECUTION ENGINE
# ==============================================================================
def main():
    cache = load_cache()
    completed_dates = load_checkpoint()
    
    # Establish complete timeline target bounds
    start_history = "2026-01-01"
    end_history = datetime.now().strftime("%Y-%m-%d")
    all_market_days = get_market_days_range(start_history, end_history)
    
    # Isolate remaining days left to pull
    pending_dates = [d for d in all_market_days if d not in completed_dates]
    
    if not pending_dates:
        print("🎉 SUCCESS: All historical ranges from 2026-01-01 to today are completely sync'd!")
        return

    # Process exactly 5 market sessions during this execution block to avoid timeouts
    run_batch = pending_dates[:5]
    print(f"🚀 Initializing Backfill Engine Batch Session. Targeting: {run_batch}")
    
    for date_str in run_batch:
        print(f"\nProcessing Grouped Aggregates for: {date_str}")
        if date_str not in cache["market_sessions"]:
            day_data = get_bulk_data(date_str)
            if day_data:
                cache["market_sessions"][date_str] = day_data
                save_cache(cache)
                time.sleep(12)  # API Free-Tier Throttle Guard
        
        # Build universe criteria for this date
        day_snapshot = cache["market_sessions"].get(date_str, {})
        
        # We need the previous day's data to calculate the close-to-close gain accurately
        all_days = get_market_days_range("2025-12-20", date_str)
        if len(all_days) < 2:
            completed_dates.append(date_str)
            save_checkpoint(completed_dates)
            continue
            
        prev_date = all_days[-2]
        if prev_date not in cache["market_sessions"]:
            prev_data = get_bulk_data(prev_date)
            if prev_data: 
                cache["market_sessions"][prev_date] = prev_data
                save_cache(cache)
                time.sleep(12)
                
        prev_snapshot = cache["market_sessions"].get(prev_date, {})
        
        # Isolate anomalous movers passing criteria rules
        movers = []
        for ticker, data in day_snapshot.items():
            if ticker in prev_snapshot:
                c0 = prev_snapshot[ticker]["c"]
                c1 = data["c"]
                v1 = data["v"]
                if c0 > 0:
                    gain = (c1 - c0) / c0
                    if gain >= TARGET_GAIN and v1 >= MIN_LIQUIDITY_VOLUME and c1 >= MIN_PRICE_FLOOR:
                        if is_common_stock_symbol(ticker):
                            movers.append(ticker)
                            
        print(f" -> Found {len(movers)} qualified >50% anomaly movers on {date_str}.")
        
        # Fetch high-resolution minute candles for day movers
        for ticker in sorted(movers):
            if ticker not in cache["ticker_metadata"]:
                details = get_ticker_details(ticker)
                cache["ticker_metadata"][ticker] = {"float": details.get("weighted_shares_outstanding", None), "type": details.get("type", "CS")}
                save_cache(cache)
                time.sleep(12)
                
            if cache["ticker_metadata"][ticker]["type"] == "ETF": continue
            
            if ticker not in cache["hod_matrix"]: cache["hod_matrix"][ticker] = {}
            
            if date_str not in cache["hod_matrix"][ticker] or "orb" not in cache["hod_matrix"][ticker][date_str]:
                print(f"    -> Extracting high resolution matrix loops for ${ticker}...")
                metrics = get_high_of_day_metrics(ticker, date_str)
                cache["hod_matrix"][ticker][date_str] = metrics
                save_cache(cache)
                time.sleep(12)
                
        # Mark day fully complete in checkpoint state log
        completed_dates.append(date_str)
        save_checkpoint(completed_dates)
        print(f"✅ Finished logging day {date_str} successfully.")

    print("\nBatch cluster finished processing. State safely saved downstream to cloud storage bucket.")

if __name__ == "__main__":
    main()
