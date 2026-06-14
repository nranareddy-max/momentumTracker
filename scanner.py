import os
import sys
import time
import json
import requests
from datetime import datetime, timedelta

# ==============================================================================
# STRATEGY & INFRASTRUCTURE CONFIGURATION
# ==============================================================================
API_KEY = os.environ.get("MASSIVE_API_KEY")
TARGET_GAIN = 0.50             # 50% minimum close-to-close gain to qualify
MIN_LIQUIDITY_VOLUME = 100000  # Filter out zero-volume "sub-penny zombie" spikes
MIN_PRICE_FLOOR = 1.00         # Filter out sub-dollar junk to control locate fees
CACHE_FILE = os.path.expanduser("~/tracker/history.json")
INTRADAY_DIR = os.path.expanduser("~/tracker/intraday")
WEB_DIR = os.path.expanduser("~/tracker/www")

if not API_KEY:
    print("ERROR: MASSIVE_API_KEY environment variable is missing.")
    sys.exit(1)

# Ensure local directory structure is intact
os.makedirs(INTRADAY_DIR, exist_ok=True)

# ==============================================================================
# CORE FILTERING & API ACCESS FUNCTIONS
# ==============================================================================
def is_common_stock_symbol(ticker):
    """Filters out structural market derivatives like warrants, rights, and units."""
    if len(ticker) == 5 and ticker[-1] in ['W', 'R', 'U']:
        return False
    if 'WS' in ticker or '+' in ticker or '.' in ticker:
        return False
    return True

def get_market_days_needed(count=6):
    """Returns the last N market days, anchored backward from yesterday."""
    days = []
    check_date = datetime.now() - timedelta(days=1) 
    while len(days) < count:
        if check_date.weekday() < 5:  # Monday - Friday
            days.append(check_date.strftime("%Y-%m-%d"))
        check_date -= timedelta(days=1)
    days.reverse()  
    return days

def get_bulk_data(date_str):
    """Fetches full market end-of-day snapshots from Massive API."""
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
    """Fetches ticker asset classification metadata to flag and filter out ETFs."""
    url = f"https://api.massive.com/v3/reference/tickers/{ticker}?apiKey={API_KEY}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json().get("results", {})
    return {}

def save_intraday_json(ticker, date_str, parsed_minutes):
    """Saves per-minute OHLCV data array to an isolated, stock-specific local file."""
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

def get_high_of_day_metrics(ticker, date_str):
    """
    Parses 1-minute historical aggregate bars (4:00 AM to 8:00 PM EST).
    Extracts high-level range markers for the main dashboard and archives
    the full per-minute candlestick path locally.
    """
    url = f"https://api.massive.com/v2/aggs/ticker/{ticker}/range/1/minute/{date_str}/{date_str}?adjusted=true&sort=asc&apiKey={API_KEY}"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            results = response.json().get("results", [])
            if results:
                hod_bar = max(results, key=lambda x: x["h"])
                lod_bar = min(results, key=lambda x: x["l"])
                
                # Format full minute-by-minute array for local archive storage
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
                
                # Write per-minute database files out asynchronously
                save_intraday_json(ticker, date_str, parsed_minutes)
                
                # Process structural dashboard tracking targets
                timestamp_ms = hod_bar["t"]
                utc_dt = datetime.utcfromtimestamp(timestamp_ms / 1000.0)
                est_dt = utc_dt + timedelta(hours=-4)
                
                market_open = est_dt.replace(hour=9, minute=30, second=0, microsecond=0)
                market_close = est_dt.replace(hour=16, minute=0, second=0, microsecond=0)
                
                if est_dt < market_open:
                    duration_str = "Premarket"
                elif est_dt > market_close:
                    duration_str = "Post-Hours"
                else:
                    diff = est_dt - market_open
                    mins_elapsed = int(diff.total_seconds() // 60)
                    duration_str = f"{mins_elapsed}m from Open"
                
                return {
                    "high": float(hod_bar["h"]),
                    "low": float(lod_bar["l"]),
                    "time": est_dt.strftime("%I:%M %p"),
                    "duration": duration_str,
                    "peak_min_vol": int(hod_bar["v"])
                }
    except Exception:
        pass
    return {"high": 0.0, "low": 0.0, "time": "N/A", "duration": "—", "peak_min_vol": 0}

# ==============================================================================
# DATABASE PERSISTENCE LAYER
# ==============================================================================
def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"market_sessions": {}, "ticker_metadata": {}, "hod_matrix": {}}

def save_cache(cache_data):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache_data, f, indent=2)

# ==============================================================================
# MAIN PROCESSING & SYNTHESIS ENGINE
# ==============================================================================
def main():
    cache = load_cache()
    if "hod_matrix" not in cache:
        cache["hod_matrix"] = {}
        
    target_dates = get_market_days_needed(6)
    print(f"Syncing bulk matrix tracking for: {target_dates}")
    
    # Step 1: Accumulate Daily Session Data (Bulk Lookups)
    cache_updated = False
    for date_str in target_dates:
        if date_str not in cache["market_sessions"]:
            print(f" -> Cache miss for bulk data on {date_str}. Fetching...")
            day_data = get_bulk_data(date_str)
            if day_data:
                cache["market_sessions"][date_str] = day_data
                cache_updated = True
                time.sleep(12)  # Strict Free-Tier Pacing Buffer
    
    active_sessions = {d: cache["market_sessions"][d] for d in target_dates if d in cache["market_sessions"]}
    cache["market_sessions"] = active_sessions
    
    if cache_updated:
        save_cache(cache)

    available_dates = [d for d in target_dates if d in cache["market_sessions"]]
    if len(available_dates) < 6:
        print("Error: Rebuilding tracking cache. Insufficient baseline historical sessions data.")
        return

    display_days = available_dates[1:] 
    baseline_days = available_dates[:-1]
    
    # Step 2: Parse Universe & Isolate Legitimate 50%+ Setup Qualifiers
    universe = set()
    calculated_data = {} 

    for i in range(5):
        d0 = baseline_days[i]
        d1 = display_days[i]
        
        d0_snapshot = cache["market_sessions"][d0]
        d1_snapshot = cache["market_sessions"][d1]
        
        for ticker, d1_meta in d1_snapshot.items():
            if ticker in d0_snapshot:
                close_d0 = d0_snapshot[ticker]["c"]
                close_d1 = d1_meta["c"]
                vol_d1 = d1_meta["v"]
                
                if close_d0 > 0:
                    gain = (close_d1 - close_d0) / close_d0
                    is_qualifier = (gain >= TARGET_GAIN) and (vol_d1 >= MIN_LIQUIDITY_VOLUME) and (close_d1 >= MIN_PRICE_FLOOR)
                    
                    if ticker not in calculated_data:
                        calculated_data[ticker] = {}
                        
                    calculated_data[ticker][d1] = {
                        "gain": gain * 100,
                        "close": close_d1,
                        "volume": vol_d1,
                        "qualified": is_qualifier
                    }
                    
                    if is_qualifier:
                        universe.add(ticker)

    print(f"\nFiltered universe contains {len(universe)} tickers. Mapping composite matrix parameters...")
    metadata_updated = False

    # Step 3: Fetch Float and Multi-Resolution Range Data
    filtered_universe = []
    for idx, ticker in enumerate(sorted(universe)):
        if ticker not in cache["ticker_metadata"]:
            details = get_ticker_details(ticker)
            cache["ticker_metadata"][ticker] = {
                "float": details.get("weighted_shares_outstanding", None),
                "type": details.get("type", "CS")
            }
            metadata_updated = True
            time.sleep(12)
            
        if cache["ticker_metadata"][ticker]["type"] == "ETF":
            continue
            
        filtered_universe.append(ticker)

        if ticker not in cache["hod_matrix"]:
            cache["hod_matrix"][ticker] = {}
            
        for d in display_days:
            # Force re-query if checking from a raw cache start to generate internal pricing files
            if d not in cache["hod_matrix"][ticker] or "duration" not in cache["hod_matrix"][ticker][d]:
                print(f" -> Mapping 1-min metrics and compiling intraday archive for {ticker} on {d}...")
                metrics = get_high_of_day_metrics(ticker, d)
                cache["hod_matrix"][ticker][d] = metrics
                metadata_updated = True
                time.sleep(12)

    if metadata_updated:
        save_cache(cache)

    # Step 4: Package and Sort Rows Descending by Most Recent Session's Performance
    final_rows = []
    latest_day = display_days[-1]
    
    for ticker in filtered_universe:
        ticker_history = []
        max_latest_gain = -999.0
        
        for d in display_days:
            day_metrics = calculated_data.get(ticker, {}).get(d, {"gain": 0.0, "close": 0.0, "volume": 0, "qualified": False})
            hod_metrics = cache["hod_matrix"].get(ticker, {}).get(d, {"high": 0.0, "low": 0.0, "time": "N/A", "duration": "—", "peak_min_vol": 0})
            
            ticker_history.append({
                "date": d,
                "gain": day_metrics["gain"],
                "close": day_metrics["close"],
                "volume": day_metrics["volume"],
                "qualified": day_metrics["qualified"],
                "high": hod_metrics.get("high", 0.0),
                "low": hod_metrics.get("low", 0.0),
                "time": hod_metrics.get("time", "N/A"),
                "duration": hod_metrics.get("duration", "—"),
                "peak_min_vol": hod_metrics.get("peak_min_vol", 0)
            })
            if d == latest_day:
                max_latest_gain = day_metrics["gain"]
                
        final_rows.append({
            "ticker": ticker,
            "float": cache["ticker_metadata"][ticker]["float"],
            "history": ticker_history,
            "sort_key": max_latest_gain
        })

    final_rows.sort(key=lambda x: x["sort_key"], reverse=True)

    # ==============================================================================
    # STEP 5: RENDER DYNAMIC RESPONSIVE INTERFACE (HTML/CSS)
    # ==============================================================================
    html = f"""<!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Momentum Lifecycle Grid</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; background-color: #0f172a; color: #f8fafc; padding: 20px; margin: 0; }}
            .container {{ max-width: 1100px; margin: 0 auto; background: #1e293b; padding: 25px; border-radius: 12px; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.5); }}
            h2 {{ margin-top: 0; color: #38bdf8; border-bottom: 2px solid #334155; padding-bottom: 10px; }}
            p {{ color: #94a3b8; font-size: 13px; margin-bottom: 20px; }}
            table {{ width: 100%; border-collapse: collapse; text-align: left; }}
            th {{ background-color: #334155; color: #38bdf8; font-weight: 600; padding: 12px 8px; font-size: 13px; }}
            .master-row {{ cursor: pointer; border-bottom: 1px solid #334155; transition: background 0.15s ease; }}
            .master-row:hover {{ background-color: #2d3d54; }}
            .master-row td {{ padding: 14px 8px; font-size: 14px; }}
            .ticker-box {{ font-weight: bold; color: #fff; }}
            .float-box {{ color: #cbd5e1; }}
            .pct-box {{ text-align: center; font-weight: 500; font-size: 13px; }}
            .spark-pop {{ background-color: rgba(74, 222, 128, 0.25); color: #4ade80; border: 1px solid rgba(74, 222, 128, 0.4); border-radius: 4px; font-weight: bold; }}
            .spark-fade {{ color: #f87171; }}
            .spark-neutral {{ color: #94a3b8; }}
            .detail-row {{ display: none; background-color: #111827; }}
            .detail-container {{ padding: 15px; border-left: 3px solid #38bdf8; }}
            .sub-table {{ width: 100%; max-width: 1000px; margin: 5px 0; border: none; }}
            .sub-table th {{ background-color: #1f2937; color: #94a3b8; padding: 8px; font-size: 11px; text-align: left; }}
            .sub-table td {{ padding: 8px; font-size: 12px; color: #e2e8f0; border-bottom: 1px solid #374151; }}
            .timestamp {{ font-size: 11px; color: #64748b; text-align: right; margin-top: 25px; }}
            .vol-highlight {{ color: #38bdf8; font-weight: bold; }}
            .duration-highlight {{ color: #22d3ee; font-weight: 500; }}
            .high-highlight {{ color: #f59e0b; font-weight: bold; }}
            .low-highlight {{ color: #c084fc; font-weight: bold; }}
        </style>
        <script>
            function toggleRow(id) {{
                var el = document.getElementById(id);
                el.style.display = (el.style.display === 'table-row') ? 'none' : 'table-row';
            }}
        </script>
    </head>
    <body>
        <div class="container">
            <h2>📈 Multi-Day Trajectory Grid (Omni-Resolution View)</h2>
            <p>Click rows to expand full multi-day bounce profiles. Intraday minute-by-minute granular price layers are archived straight to your local server array folder.</p>
            <table>
                <tr>
                    <th>Ticker</th>
                    <th>Proxy Float</th>
    """
    for d in display_days:
        formatted_date = datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d")
        html += f"<th style='text-align:center;'>{formatted_date}</th>"
    html += "</tr>"

    for idx, row in enumerate(final_rows):
        float_str = f"{row['float']:,}" if row['float'] else "N/A"
        detail_id = f"detail_{idx}"
        
        html += f"""
        <tr class="master-row" onclick="toggleRow('{detail_id}')">
            <td class="ticker-box">{row['ticker']}</td>
            <td class="float-box">{float_str}</td>
        """
        for day in row["history"]:
            val = f"{day['gain']:.1f}%"
            if day['gain'] > 0: val = f"+{val}"
            cell_class = "spark-pop" if day["qualified"] else ("spark-fade" if day["gain"] < 0 else "spark-neutral")
            html += f"<td class='pct-box {cell_class}'>{val}</td>"
            
        html += f"""
        </tr>
        <tr id="{detail_id}" class="detail-row">
            <td colspan="{2 + len(display_days)}">
                <div class="detail-container">
                    <strong style="color:#38bdf8; font-size:12px;">📊 High-Resolution Intraday Liquidity Bounds: {row['ticker']}</strong>
                    <table class="sub-table">
                        <tr>
                            <th>Date</th>
                            <th>High Price</th>
                            <th>Low Price</th>
                            <th>Close Price</th>
                            <th>Daily Move</th>
                            <th>High Time (1m)</th>
                            <th>Duration to Peak</th>
                            <th>Daily Agg Vol</th>
                            <th>Peak 1-Min Vol</th>
                        </tr>
        """
        for day in row["history"]:
            day_f = datetime.strptime(day['date'], "%Y-%m-%d").strftime("%b %d, %Y")
            h_str = f"${day['high']:.2f}" if day['high'] > 0 else "—"
            l_str = f"${day['low']:.2f}" if day['low'] > 0 else "—"
            c_str = f"${day['close']:.2f}" if day['close'] > 0 else "—"
            g_str = f"+{day['gain']:.2f}%" if day['gain'] > 0 else f"{day['gain']:.2f}%"
            t_str = f"⏰ {day['time']}" if day['time'] != "N/A" else "—"
            dur_str = day['duration']
            v_str = f"{day['volume']:,}" if day['volume'] else "0"
            p1_str = f"{day['peak_min_vol']:,}" if day['peak_min_vol'] > 0 else "—"
            
            html += f"""
                        <tr>
                            <td>{day_f}</td>
                            <td class="high-highlight">{h_str}</td>
                            <td class="low-highlight">{l_str}</td>
                            <td>{c_str}</td>
                            <td style="color: {('#4ade80' if day['gain'] >= 0 else '#f87171')}; font-weight:500;">{g_str}</td>
                            <td style="color: #cbd5e1; font-weight: 500;">{t_str}</td>
                            <td class="duration-highlight">{dur_str}</td>
                            <td>{v_str}</td>
                            <td class="vol-highlight">{p1_str}</td>
                        </tr>
            """
        html += """
                    </table>
                </div>
            </td>
        </tr>
        """

    html += f"""
            </table>
            <div class="timestamp">Last Automatic Midnight Roll Calculation: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} EST</div>
        </div>
    </body>
    </html>
    """

    os.makedirs(WEB_DIR, exist_ok=True)
    with open(os.path.join(WEB_DIR, "index.html"), "w") as f:
        f.write(html)
    print("Dashboard matrix completely updated and HTML rendered.")

    # ==============================================================================
    # AUTOMATED GIT PUSH BACKUP ENGINE
    # ==============================================================================
    import subprocess
    try:
        repo_dir = os.path.expanduser("~/tracker")
        
        # Track BOTH the root high-level JSON and the entire folder of granular files
        subprocess.run(["git", "add", "history.json", "intraday/"], cwd=repo_dir, check=True)
        
        # Check if anything changed to avoid empty commit errors
        status_check = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True)
        
        if status_check.stdout.strip():
            print(" -> Detected updates in dataset matrix files. Committing changes...")
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            subprocess.run(["git", "commit", "-m", f"Automated dataset sync: {timestamp}"], cwd=repo_dir, check=True)
            subprocess.run(["git", "push", "origin", "main"], cwd=repo_dir, check=True)
            print("Successfully backed up full data structures to GitHub repo.")
        else:
            print(" -> No changes detected in any data frames. Skipping backup push.")
            
    except Exception as git_error:
        print(f" ! Git automated backup engine failed: {git_error}")

if __name__ == "__main__":
    main()
