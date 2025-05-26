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
# import threading # Not used in this web app version for background tasks

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

WEATHER_CACHE_DURATION_SECONDS = 30 * 60 # Weather cache for 30 minutes
BUS_DATA_CACHE_DURATION_SECONDS = 10 # Bus data cache (and page reload interval suggestion)

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
    global weather_fetched_today_g # App Engine/Cloud Runでは注意
    if not api_key or api_key == "YOUR_OPENWEATHERMAP_API_KEY_HERE":
        logging.warning("OpenWeatherMap APIキーが未設定。")
        return None, None, None, "APIキー未設定"
    api_url = "http://api.openweathermap.org/data/2.5/weather"
    params = {"q": location_query, "appid": api_key, "units": "metric", "lang": "ja"}
    try:
        response = requests.get(api_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("weather") and len(data["weather"]) > 0:
            main_condition = data["weather"][0].get("main")
            description = data["weather"][0].get("description")
            temp = data.get("main", {}).get("temp")
            logging.info(f"天気情報取得成功 ({location_query}): {main_condition} ({description}), 気温: {temp}°C")
            # weather_fetched_today_g is managed in the route
            return main_condition, description, temp, None
        return None, None, None, "APIレスポンス形式不正"
    except requests.exceptions.Timeout:
        logging.warning(f"天気情報取得タイムアウト ({location_query})")
        return None, None, None, "タイムアウト"
    except requests.exceptions.HTTPError as http_err:
        error_message = f"HTTPエラー {http_err.response.status_code}"
        if http_err.response.status_code == 401:
             error_message = "APIキーが無効か認証エラーです。"
        logging.error(f"天気情報取得HTTPエラー ({location_query}): {http_err}")
        return None, None, None, error_message
    except Exception as e:
        logging.exception(f"天気情報取得中に予期せぬエラー ({location_query})")
        return None, None, None, "予期せぬエラー"

def fetch_simplified_bus_departure_times(from_stop_no, to_stop_no):
    params = {'fNO': from_stop_no, 'tNO': to_stop_no}
    bus_departure_list = []
    try:
        response = requests.get(BASE_URL, params=params, timeout=10)
        response.raise_for_status()
        html_content = response.content.decode('shift_jis', errors='replace')
        soup = BeautifulSoup(html_content, 'html.parser')
        main_content_area = soup.find('div', class_='inner2 pa01')
        if not main_content_area:
            logging.warning("バス情報HTMLから 'inner2 pa01' クラスが見つかりません。")
            return {"buses": [], "error": None} # No structural error, just no parsable content area
        bus_info_headings = main_content_area.find_all('h3', class_='heading3')
        if not bus_info_headings:
            logging.info("バス情報が見つかりませんでした (h3.heading3なし)。")
            return {"buses": [], "error": None} # No buses listed
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
            time_part = None
            match_time_candidate = re.search(r'(\d{1,2}:\d{2})発?', title_text_raw)
            if match_time_candidate: time_part = match_time_candidate.group(1)

            if "まもなく発車します" in title_text_raw or "まもなく到着" in title_text_raw: departure_time_str = "まもなく"
            elif "通過しました" in title_text_raw or "出発しました" in title_text_raw: departure_time_str = "出発済み"
            elif "予定通り発車します" in title_text_raw:
                departure_time_str = f"{time_part}発 (予定通り)" if time_part else "状態不明 (予定通り情報あり)"
            elif "頃発車します" in title_text_raw:
                departure_time_str = f"{time_part}発 (遅延可能性あり)" if time_part else "状態不明 (遅延情報あり)"
            elif "発予定" in title_text_raw: # "XX:XX発予定"
                departure_time_str = f"{time_part}発 (予定)" if time_part else "状態不明 (予定情報あり)"
            elif time_part: # Default to just time if present
                departure_time_str = f"{time_part}発"
            
            if departure_time_str:
                bus_departure_list.append({KEY_DEPARTURE_TIME: departure_time_str, KEY_STATUS_TEXT: title_text_raw})
        return {"buses": bus_departure_list, "error": None}
    except requests.exceptions.RequestException as e:
        error_msg = f"バス情報取得時のネットワークエラー: {e}"
        logging.error(error_msg)
        # send_discord_notification(f"🛑 **バス情報取得エラー:** {error_msg}")
        return {"buses": [], "error": error_msg}
    except Exception as e:
        error_msg = f"バス情報取得中に予期せぬエラー: {e}"
        logging.exception(error_msg) # Log full traceback
        # send_discord_notification(f"🛑 **バス情報取得中の予期せぬエラー:** {error_msg}")
        return {"buses": [], "error": error_msg}

def calculate_and_format_time_until(departure_str, status_text_raw, current_dt_tokyo):
    is_urgent = False 
    time_until_str = ""
    if "まもなく" in departure_str:
        time_until_str = "まもなく"
        is_urgent = True 
    elif "出発済み" in departure_str:
        time_until_str = "出発済み"
    else:
        match = re.search(r'(\d{1,2}:\d{2})発', departure_str)
        if not match:
            if "(予定通り)" in departure_str: time_until_str = "" 
            elif "(遅延可能性あり)" in departure_str: time_until_str = "遅延可能性あり"
            elif "(予定)" in departure_str: time_until_str = "予定"
            else: time_until_str = "時刻情報なし" # More descriptive
            return time_until_str, is_urgent
        
        bus_time_str = match.group(1)
        try:
            bus_hour, bus_minute = map(int, bus_time_str.split(':'))
            # Create a datetime object for the bus for today in Tokyo time
            bus_dt_today_tokyo = current_dt_tokyo.replace(hour=bus_hour, minute=bus_minute, second=0, microsecond=0)

            # Handle buses scheduled for after midnight (e.g., current time 23:00, bus time 00:30)
            # This simplified logic assumes buses won't be scheduled more than a few hours past midnight
            # and that current_dt_tokyo is correctly localized.
            if bus_dt_today_tokyo < current_dt_tokyo and (current_dt_tokyo.hour >= 20 and bus_hour <= 5) : # Heuristic for next day bus
                 bus_dt_today_tokyo += datetime.timedelta(days=1)


            if bus_dt_today_tokyo < current_dt_tokyo:
                # If the bus time is in the past
                # Check raw status for "予定通り発車します", "通過しました", "出発しました"
                # If these are present, it's definitively "出発済み". Otherwise, "発車済みのおそれあり".
                if not any(s in status_text_raw for s in ["予定通り発車します", "通過しました", "出発しました"]):
                    time_until_str = "発車済みのおそれあり"
                else:
                    time_until_str = "出発済み"
            else:
                delta = bus_dt_today_tokyo - current_dt_tokyo
                total_seconds = int(delta.total_seconds())
                if total_seconds <= 15: # 15秒以内
                    time_until_str = "まもなく発車"
                    is_urgent = True
                elif total_seconds <= 180: # 3分以内 (180秒)
                    minutes_until = total_seconds // 60
                    seconds_until = total_seconds % 60
                    is_urgent = True 
                    if minutes_until > 0:
                        time_until_str = f"あと{minutes_until}分{seconds_until}秒"
                    else:
                        time_until_str = f"あと{seconds_until}秒"
                else: # 3分を超える
                    minutes_until = total_seconds // 60
                    time_until_str = f"あと{minutes_until}分"
        except ValueError:
            time_until_str = f"時刻形式エラー ({departure_str})"
        except Exception as e:
            logging.error(f"calculate_and_format_time_until でエラー: {e} (departure_str: {departure_str})")
            time_until_str = "時間計算エラー"
    return time_until_str, is_urgent

@app.route('/')
def index():
    global weather_cache, bus_data_cache, weather_fetched_today_g, last_date_weather_checked_g

    current_dt_tokyo = datetime.datetime.now(TOKYO_TZ)
    current_time_unix_seconds = time.time() # Used for cache timestamping
    current_hour = current_dt_tokyo.hour
    current_date = current_dt_tokyo.date()

    # --- 天気情報の取得とキャッシュ (9時台に1回試行) ---
    weather_data_to_display = {}
    if last_date_weather_checked_g != current_date:
        weather_fetched_today_g = False
        last_date_weather_checked_g = current_date
        logging.info(f"日付変更 ({current_date})。天気取得フラグ解除。")

    # Try to fetch weather if it's the designated hour AND (we haven't fetched today OR cache is old)
    # In a stateless env, weather_fetched_today_g might reset. Cache helps reduce redundant calls.
    should_fetch_weather = False
    if current_hour == WEATHER_FETCH_HOUR:
        if not weather_fetched_today_g:
            should_fetch_weather = True
            logging.info(f"{WEATHER_FETCH_HOUR}時台、天気情報本日未取得のため更新試行。")
        elif weather_cache["timestamp"] < current_time_unix_seconds - WEATHER_CACHE_DURATION_SECONDS :
             should_fetch_weather = True
             logging.info(f"{WEATHER_FETCH_HOUR}時台、天気キャッシュ古いため更新試行。")


    if should_fetch_weather:
        condition, description, temp, error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION)
        if not error:
            weather_cache["data"] = {"condition": condition, "description": description, "temp_c": temp, "is_rain": (condition and "rain" in condition.lower())}
            weather_cache["timestamp"] = current_time_unix_seconds
            weather_cache["error"] = None # Clear previous error on success
            weather_fetched_today_g = True # Mark as fetched for today (for this worker)
            logging.info(f"天気情報更新・キャッシュ完了。is_rain: {weather_cache['data']['is_rain']}")
        else:
            weather_cache["error"] = error # Store error, keep old data if any
            # Do not set weather_fetched_today_g to True on error, to allow retry
            logging.error(f"天気情報更新失敗: {error}")
    
    weather_data_to_display = weather_cache.get("data", {}) # Use data if present
    weather_data_to_display["error_message"] = weather_cache.get("error") # Add error message separately


    # --- バス情報の取得とキャッシュ ---
    processed_buses = []
    bus_fetch_error = None
    app_state_message = "監視中"

    is_before_service_hours = current_hour < BUS_SERVICE_START_HOUR or \
                              (current_hour == BUS_SERVICE_START_HOUR and current_dt_tokyo.minute < BUS_SERVICE_START_MINUTE)

    if is_before_service_hours:
        app_state_message = f"始発バス待機中 (～{BUS_SERVICE_START_HOUR:02d}:{BUS_SERVICE_START_MINUTE:02d}目安)"
        # Use cached bus data if available, but recalculate times
        if bus_data_cache.get("data"):
            for bus_info_original in bus_data_cache["data"]:
                bus_info = bus_info_original.copy()
                time_until_str, is_urgent = calculate_and_format_time_until(
                    bus_info.get(KEY_DEPARTURE_TIME, ""),
                    bus_info.get(KEY_STATUS_TEXT, ""),
                    current_dt_tokyo
                )
                bus_info[KEY_TIME_UNTIL] = time_until_str
                bus_info[KEY_IS_URGENT] = is_urgent
                processed_buses.append(bus_info)
        bus_fetch_error = bus_data_cache.get("error")
    
    else: # Service hours
        if current_time_unix_seconds - bus_data_cache.get("timestamp", 0) > BUS_DATA_CACHE_DURATION_SECONDS \
           or not bus_data_cache.get("data") and not bus_data_cache.get("error"): # Fetch if cache expired, or no data and no persistent error
            logging.info("バス情報を更新します (キャッシュ期限切れ、初回、またはエラー後の再試行)。")
            bus_result = fetch_simplified_bus_departure_times(FROM_STOP_NO, TO_STOP_NO)
            # Only update cache if there's new data or a new error. Don't overwrite good data with error if fetch fails.
            if bus_result.get("buses") or bus_result.get("error"):
                 bus_data_cache["data"] = bus_result.get("buses", [])
                 bus_data_cache["error"] = bus_result.get("error")
            bus_data_cache["timestamp"] = current_time_unix_seconds # Always update timestamp
        
        bus_fetch_error = bus_data_cache.get("error")
        if bus_data_cache.get("data"):
            for bus_info_original in bus_data_cache["data"]:
                bus_info = bus_info_original.copy()
                time_until_str, is_urgent = calculate_and_format_time_until(
                    bus_info.get(KEY_DEPARTURE_TIME, ""),
                    bus_info.get(KEY_STATUS_TEXT, ""),
                    current_dt_tokyo
                )
                bus_info[KEY_TIME_UNTIL] = time_until_str
                bus_info[KEY_IS_URGENT] = is_urgent
                processed_buses.append(bus_info)
        
        if not bus_fetch_error and not processed_buses:
            # Changed to match the template's expected message for this scenario
            app_state_message = "運行再開待機中 (終バス後など)" 
        elif bus_fetch_error and not processed_buses:
            app_state_message = "情報取得エラー (再試行中)"


    return render_template('index.html',
                           from_stop=FROM_STOP_NAME,
                           to_stop=TO_STOP_NAME,
                           current_time_unix_milliseconds=int(current_time_unix_seconds * 1000), # For JS Date
                           weather_data=weather_data_to_display,
                           app_state_message=app_state_message,
                           buses_to_display=processed_buses,
                           bus_error_message=bus_fetch_error,
                           bus_last_updated_str=datetime.datetime.fromtimestamp(bus_data_cache.get("timestamp", 0), TOKYO_TZ).strftime('%H:%M:%S') if bus_data_cache.get("timestamp", 0) > 0 else "N/A",
                           page_refresh_interval_seconds=BUS_DATA_CACHE_DURATION_SECONDS # Pass this to template
                           )

if __name__ == '__main__':
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        logging.warning("ローカルテスト: Discord Webhook URL未設定")
    if not OPENWEATHERMAP_API_KEY or OPENWEATHERMAP_API_KEY == "YOUR_OPENWEATHERMAP_API_KEY_HERE":
        logging.warning("ローカルテスト: OpenWeatherMap APIキー未設定")
    # Notify on start for local dev
    # send_discord_notification("🚌 バス情報チェッカースクリプト (ローカルFlask) が起動しました。")
    try:
        app.run(host='127.0.0.1', port=8080, debug=True)
    except KeyboardInterrupt:
        logging.info("スクリプトが手動で終了されました (Ctrl+C)。")
        # send_discord_notification("ℹ️ バス情報チェッカースクリプト (ローカルFlask) が手動で停止されました。")
    except Exception as e:
        logging.critical(f"スクリプト実行中に致命的なエラーが発生し終了します: {e}", exc_info=True)
        # send_discord_notification(f"💥 **緊急停止:** バス情報チェッカースクリプト (ローカルFlask) が予期せぬエラーで停止しました: {e}")
    finally:
        logging.info("ローカルFlaskサーバシャットダウン。")
