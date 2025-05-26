from flask import Flask, render_template
import requests
from bs4 import BeautifulSoup
import json
import re
import datetime
import time
import os
import pytz
import logging
# import threading # Not used in this GAE-friendly version for background tasks
# from apscheduler.schedulers.background import BackgroundScheduler

# --- 設定 (環境変数から読み込むことを推奨) ---
OPENWEATHERMAP_API_KEY = os.environ.get("OPENWEATHERMAP_API_KEY", "YOUR_OPENWEATHERMAP_API_KEY_HERE")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "YOUR_DISCORD_WEBHOOK_URL_HERE")
WEATHER_LOCATION = "Isehara,JP"
BASE_URL = "http://real.kanachu.jp/pc/displayapproachinfo"
FROM_STOP_NO = "18137"
TO_STOP_NO = "18100"
FROM_STOP_NAME = "産業能率大学"
TO_STOP_NAME = "伊勢原駅北口"
MAX_BUSES_TO_FETCH = 5
WEATHER_FETCH_HOUR = 9
BUS_SERVICE_START_HOUR = 6
BUS_SERVICE_START_MINUTE = 20

TOKYO_TZ = pytz.timezone('Asia/Tokyo')

KEY_DEPARTURE_TIME = "departure_time"
KEY_STATUS_TEXT = "status_text"
KEY_TIME_UNTIL = "time_until_departure"
KEY_IS_URGENT = "is_urgent"

app = Flask(__name__)

# --- ロギング設定 ---
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(funcName)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

# --- グローバル変数 (App Engine/Cloud Runではリクエストごとにリセットされるため、Datastore/Memcache推奨) ---
weather_cache = {"data": None, "timestamp": 0, "error": None}
bus_data_cache = {"data": [], "timestamp": 0, "error": None}
weather_fetched_today_g = False
last_date_weather_checked_g = None

WEATHER_CACHE_DURATION_SECONDS = 30 * 60 # 30 minutes
BUS_DATA_CACHE_DURATION_SECONDS = 30 # 🚍 Bus info cache/fetch interval set to 30 seconds

# (send_discord_notification, get_weather_info, fetch_simplified_bus_departure_times functions remain as provided in the context)
# Minor change in get_weather_info to ensure temp is returned even on some errors if partially available
def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        logging.warning("Discord Webhook URLが未設定のため、通知は送信されません。")
        return
    payload = {"content": message, "username": os.environ.get("DISCORD_USERNAME", "バス情報チェッカー")}
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, data=json.dumps(payload), headers=headers, timeout=5)
        response.raise_for_status()
        logging.info(f"Discord通知送信成功: {message[:50]}...")
    except requests.exceptions.RequestException as e:
        logging.error(f"Discord通知送信失敗: {e}")
    except Exception as e:
        logging.error(f"Discord通知送信中に予期せぬエラー: {e}")

def get_weather_info(api_key, location_query):
    global weather_fetched_today_g
    if not api_key or api_key == "YOUR_OPENWEATHERMAP_API_KEY_HERE":
        logging.warning("OpenWeatherMap APIキーが未設定。")
        return None, None, None, "APIキー未設定" # Added None for temp
    api_url = "http://api.openweathermap.org/data/2.5/weather"
    params = {"q": location_query, "appid": api_key, "units": "metric", "lang": "ja"}
    temp = None # Initialize temp
    try:
        response = requests.get(api_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("main"): temp = data.get("main", {}).get("temp") # Get temp early
        if data.get("weather") and len(data["weather"]) > 0:
            main_condition = data["weather"][0].get("main")
            description = data["weather"][0].get("description")
            logging.info(f"天気情報取得成功 ({location_query}): {main_condition} ({description}), 気温: {temp}°C")
            weather_fetched_today_g = True
            return main_condition, description, temp, None
        return None, None, temp, "APIレスポンス形式不正" # Return temp if available
    except requests.exceptions.Timeout:
        logging.warning(f"天気情報取得タイムアウト ({location_query})")
        return None, None, temp, "タイムアウト" # Return temp if available
    except requests.exceptions.HTTPError as http_err:
        error_message = f"HTTPエラー {http_err.response.status_code}"
        if http_err.response.status_code == 401:
             error_message = "APIキーが無効か認証エラーです。"
        logging.error(f"天気情報取得HTTPエラー ({location_query}): {http_err}")
        return None, None, temp, error_message # Return temp if available
    except Exception as e:
        logging.exception(f"天気情報取得中に予期せぬエラー ({location_query})")
        return None, None, temp, f"予期せぬエラー" # Return temp if available

def fetch_simplified_bus_departure_times(from_stop_no, to_stop_no):
    params = {'fNO': from_stop_no, 'tNO': to_stop_no}
    bus_departure_list = []
    try:
        response = requests.get(BASE_URL, params=params, timeout=10)
        response.raise_for_status()
        html_content = response.content.decode('shift_jis', errors='replace')
        soup = BeautifulSoup(html_content, 'html.parser')
        main_content_area = soup.find('div', class_='inner2 pa01')
        if not main_content_area: return {"buses": [], "error": "メインコンテンツエリアが見つかりません。"}
        bus_info_headings = main_content_area.find_all('h3', class_='heading3')
        if not bus_info_headings: return {"buses": [], "error": None} # No buses is not an error here
        for heading_tag in bus_info_headings:
            if len(bus_departure_list) >= MAX_BUSES_TO_FETCH: break
            hgroup_element = heading_tag.parent
            bus_wrap_element = None
            if hgroup_element and hgroup_element.name == 'div' and 'hgroup01' in hgroup_element.get('class', []):
                bus_wrap_element = hgroup_element.find_next_sibling('div', class_='wrap')
            if not bus_wrap_element: bus_wrap_element = heading_tag.find_next_sibling('div', class_='wrap')
            if not bus_wrap_element: continue
            col02 = bus_wrap_element.find('div', class_='col02')
            if not col02: continue
            frame_box_03 = col02.find('div', class_='frameBox03')
            if not frame_box_03: continue
            approach_info_title_element = frame_box_03.find('p', class_='title01')
            if not approach_info_title_element: continue
            title_text_raw = approach_info_title_element.get_text(strip=True)
            departure_time_str = None
            time_part = None # Initialize time_part
            match_time_candidate = re.search(r'(\d{1,2}:\d{2})発', title_text_raw) # Search in raw text first
            if match_time_candidate: time_part = match_time_candidate.group(1)

            if "まもなく発車します" in title_text_raw or "まもなく到着" in title_text_raw: # Prioritize "まもなく"
                departure_time_str = "まもなく"
            elif "通過しました" in title_text_raw or "出発しました" in title_text_raw:
                departure_time_str = "出発済み"
            elif "予定通り発車します" in title_text_raw:
                if time_part: departure_time_str = f"{time_part}発 (予定通り)"
                else: departure_time_str = "状態不明 (予定通り情報あり)" # Should ideally not happen if format is consistent
            elif "頃発車します" in title_text_raw:
                if time_part: departure_time_str = f"{time_part}発 (遅延可能性あり)"
                else: departure_time_str = "状態不明 (遅延情報あり)"
            elif "発予定" in title_text_raw: # This might also contain a time
                if time_part: departure_time_str = f"{time_part}発 (予定)"
                else: departure_time_str = "状態不明 (予定情報あり)"
            elif time_part: # Generic time found
                departure_time_str = f"{time_part}発"
            
            if departure_time_str:
                bus_departure_list.append({
                    KEY_DEPARTURE_TIME: departure_time_str, # This is the string like "10:30発 (予定)" or "まもなく"
                    KEY_STATUS_TEXT: title_text_raw # This is the full sentence from the website
                })
        return {"buses": bus_departure_list, "error": None}
    except requests.exceptions.Timeout:
        error_msg = "バス情報取得タイムアウト"
        logging.warning(error_msg)
        return {"buses": [], "error": error_msg}
    except requests.exceptions.RequestException as e:
        error_msg = f"バス情報取得エラー: {e}"
        logging.error(error_msg)
        return {"buses": [], "error": error_msg}
    except Exception as e:
        error_msg = f"バス情報取得中に予期せぬ解析エラー: {e}"
        logging.exception(error_msg) # Log full traceback for parsing errors
        return {"buses": [], "error": error_msg}

# calculate_and_format_time_until is not directly used by the route anymore for the final bus object,
# as its logic is integrated into the route for providing departure_timestamp_utc.
# It can be kept for other uses or refactored if needed.

@app.route('/')
def index():
    global weather_cache, bus_data_cache, weather_fetched_today_g, last_date_weather_checked_g

    current_dt_tokyo = datetime.datetime.now(TOKYO_TZ)
    current_time_unix = time.time()
    current_hour = current_dt_tokyo.hour
    current_date = current_dt_tokyo.date()

    # --- 天気情報の取得とキャッシュ (9時台に1回) ---
    weather_data_to_display = {}
    if last_date_weather_checked_g != current_date:
        weather_fetched_today_g = False
        last_date_weather_checked_g = current_date
        logging.info(f"日付変更 ({current_date})。天気取得フラグ解除。")

    if current_hour == WEATHER_FETCH_HOUR and not weather_fetched_today_g:
        logging.info(f"{WEATHER_FETCH_HOUR}時台、天気情報更新試行。")
        condition, description, temp, error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION)
        weather_cache["data"] = {"condition": condition, "description": description, "temp_c": temp, "is_rain": (condition and condition.lower() == "rain")}
        weather_cache["error"] = error
        weather_cache["timestamp"] = current_time_unix
        if not error:
            weather_fetched_today_g = True
            logging.info(f"天気情報 ({WEATHER_LOCATION}) を {weather_cache['timestamp']} にキャッシュしました。")
    
    weather_data_to_display = weather_cache["data"] if weather_cache.get("data") else {}
    weather_data_to_display["error_message"] = weather_cache.get("error")
    if weather_cache["timestamp"] > 0: # Add timestamp to weather if available
        weather_data_to_display["last_updated_readable"] = datetime.datetime.fromtimestamp(weather_cache["timestamp"], TOKYO_TZ).strftime('%Y-%m-%d %H:%M:%S')


    # --- バス情報の取得とキャッシュ ---
    processed_buses = []
    bus_fetch_error = None
    app_state_message = "監視中"

    is_before_service_hours = current_hour < BUS_SERVICE_START_HOUR or \
                              (current_hour == BUS_SERVICE_START_HOUR and current_dt_tokyo.minute < BUS_SERVICE_START_MINUTE)

    if is_before_service_hours:
        app_state_message = f"始発バス待機中 (～{BUS_SERVICE_START_HOUR:02d}:{BUS_SERVICE_START_MINUTE:02d}目安)"
        # Use cached data if available, but state it's before service hours
        if bus_data_cache.get("data"):
            raw_buses_from_cache = bus_data_cache.get("data", [])
        else:
            raw_buses_from_cache = [] # Ensure it's a list
        bus_fetch_error = bus_data_cache.get("error") # Use cached error
    elif current_time_unix - bus_data_cache.get("timestamp", 0) > BUS_DATA_CACHE_DURATION_SECONDS \
         or not bus_data_cache.get("data") and not bus_data_cache.get("error"): # Fetch if cache expired, or no data AND no persistent error
        logging.info("バス情報を更新します (キャッシュ期限切れまたは初回、またはエラーからの再試行)。")
        bus_result = fetch_simplified_bus_departure_times(FROM_STOP_NO, TO_STOP_NO)
        bus_data_cache["data"] = bus_result.get("buses", [])
        bus_data_cache["error"] = bus_result.get("error") # Store new error or None
        bus_data_cache["timestamp"] = current_time_unix
        raw_buses_from_cache = bus_data_cache["data"]
        bus_fetch_error = bus_data_cache["error"]
        if bus_fetch_error:
            send_discord_notification(f"🚨 バス情報取得エラー発生: {bus_fetch_error}")
    else: # Use existing cache
        raw_buses_from_cache = bus_data_cache.get("data", [])
        bus_fetch_error = bus_data_cache.get("error")
        logging.info(f"バス情報をキャッシュから使用 (最終取得: {bus_data_cache.get('timestamp', 0)})")

    # Process buses for display (calculate countdowns, timestamps for JS)
    if isinstance(raw_buses_from_cache, list): # Ensure it's a list before iterating
        for bus_info_original in raw_buses_from_cache:
            bus_info = bus_info_original.copy()
            
            departure_time_display_str = bus_info_original.get(KEY_DEPARTURE_TIME, "") # e.g., "10:30発 (予定)" or "まもなく"
            status_text_from_site = bus_info_original.get(KEY_STATUS_TEXT, "") # e.g., "10:30頃発車します"

            time_until_str = ""
            is_urgent = False
            departure_timestamp_utc = None

            if "まもなく" in departure_time_display_str:
                time_until_str = "まもなく発車" # For initial display
                is_urgent = True
                # For JS: estimate departure a few seconds from now
                departure_timestamp_utc = int((current_dt_tokyo + datetime.timedelta(seconds=30)).timestamp())
            elif "出発済み" in departure_time_display_str or "通過しました" in status_text_from_site:
                time_until_str = "出発済み"
                is_urgent = False
                # departure_timestamp_utc remains None (or could be a fixed past time if needed)
            else:
                # Try to parse HH:MM from the departure_time_display_str
                match = re.search(r'(\d{1,2}:\d{2})発', departure_time_display_str)
                if not match: # If not in departure_time_display_str, try from status_text_from_site (e.g. "XX:XX頃発車します")
                    match = re.search(r'(\d{1,2}:\d{2})(?:頃発車します|発予定)', status_text_from_site)

                if match:
                    bus_hour, bus_minute = map(int, match.group(1).split(':'))
                    try:
                        bus_dt_candidate = current_dt_tokyo.replace(hour=bus_hour, minute=bus_minute, second=0, microsecond=0)
                        
                        # Handle overnight buses: if bus time is early AM (e.g., 0:xx-5:xx) and current time is late PM (e.g. 20:xx-23:xx)
                        if bus_dt_candidate < current_dt_tokyo and \
                           (current_dt_tokyo.hour >= 20 and bus_hour <= 5):
                            bus_dt_candidate += datetime.timedelta(days=1)
                        
                        departure_timestamp_utc = int(bus_dt_candidate.timestamp())

                        if bus_dt_candidate < current_dt_tokyo:
                            # Bus time has passed
                            if "予定通り発車します" not in status_text_from_site and \
                               "通過しました" not in status_text_from_site and \
                               "出発しました" not in status_text_from_site:
                                time_until_str = "発車済みのおそれあり"
                            else:
                                time_until_str = "出発済み"
                            is_urgent = False
                        else:
                            # Bus is upcoming
                            delta = bus_dt_candidate - current_dt_tokyo
                            total_seconds = int(delta.total_seconds())
                            if total_seconds <= 15:
                                time_until_str = "まもなく発車"
                                is_urgent = True
                            elif total_seconds <= 180:  # 3 minutes
                                minutes_until = total_seconds // 60
                                seconds_until = total_seconds % 60
                                time_until_str = f"あと{minutes_until}分{seconds_until}秒" if minutes_until > 0 else f"あと{seconds_until}秒"
                                is_urgent = True
                            else:
                                minutes_until = total_seconds // 60
                                time_until_str = f"あと{minutes_until}分"
                                # Retain urgency if it's a delay, even if more than 3 mins out
                                if "遅延可能性あり" in departure_time_display_str or "頃発車します" in status_text_from_site:
                                    is_urgent = True
                    except ValueError: # Invalid date/time from parsed values
                        time_until_str = "時刻解析エラー"
                        departure_timestamp_utc = None # Cannot determine timestamp
                else: # No specific time found in either string
                    if "遅延可能性あり" in departure_time_display_str or "頃発車します" in status_text_from_site :
                        time_until_str = "遅延可能性あり"
                        is_urgent = True
                    elif "予定" in departure_time_display_str:
                        time_until_str = "時間未定" # Or keep empty
                    # else: time_until_str remains ""

            bus_info[KEY_TIME_UNTIL] = time_until_str
            bus_info[KEY_IS_URGENT] = is_urgent
            bus_info['departure_timestamp_utc'] = departure_timestamp_utc
            bus_info['raw_departure_text'] = departure_time_display_str # Pass the original parsed departure string
            # bus_info[KEY_STATUS_TEXT] is already the full status_text_from_site
            processed_buses.append(bus_info)
    else: # raw_buses_from_cache was not a list (e.g. if cache was corrupted, though unlikely here)
        logging.error("raw_buses_from_cache is not a list. Skipping processing.")
        bus_fetch_error = bus_fetch_error or "内部データエラー" # Assign an error if none exists


    if not is_before_service_hours and not bus_fetch_error and not processed_buses:
        # Only consider "運行終了" if not before service hours, no errors, and no buses listed
        # Check if the last successful fetch was recent enough, otherwise it might just be a temporary glitch
        if bus_data_cache["timestamp"] > 0 and (current_time_unix - bus_data_cache["timestamp"] < BUS_DATA_CACHE_DURATION_SECONDS * 3) : # Check if fetch is reasonably recent
             app_state_message = "情報なし/周辺に運行中のバスなし"
        # else, it might be that the first fetch of the day after service hours hasn't found buses yet
        # or an older error is still present. The bus_fetch_error message would be more relevant.


    return render_template('index.html',
                           from_stop=FROM_STOP_NAME,
                           to_stop=TO_STOP_NAME,
                           weather_data=weather_data_to_display,
                           app_state_message=app_state_message,
                           buses_to_display=processed_buses,
                           bus_error_message=bus_fetch_error,
                           bus_last_updated_str=datetime.datetime.fromtimestamp(bus_data_cache["timestamp"], TOKYO_TZ).strftime('%H:%M:%S') if bus_data_cache["timestamp"] > 0 else "N/A",
                           config={'ACTIVE_DATA_FETCH_INTERVAL': BUS_DATA_CACHE_DURATION_SECONDS} # Pass interval to template
                           )

if __name__ == '__main__':
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        logging.warning("ローカルテスト: Discord Webhook URL未設定")
    if not OPENWEATHERMAP_API_KEY or OPENWEATHERMAP_API_KEY == "YOUR_OPENWEATHERMAP_API_KEY_HERE":
        logging.warning("ローカルテスト: OpenWeatherMap APIキー未設定")
    # send_discord_notification("🚌 バス情報チェッカー (ローカル開発版) が起動しました。") # Optional: startup notification
    app.run(host='127.0.0.1', port=8080, debug=True)
