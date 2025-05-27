from flask import Flask, render_template, jsonify, request
import requests
from bs4 import BeautifulSoup
import json
import re
import datetime
import time
import os
import pytz
import logging

# --- 設定 (環境変数から読み込むことを推奨) ---
OPENWEATHERMAP_API_KEY = os.environ.get("OPENWEATHERMAP_API_KEY", "YOUR_OPENWEATHERMAP_API_KEY_HERE")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "YOUR_DISCORD_WEBHOOK_URL_HERE")
WEATHER_LOCATION = "Isehara,JP"
BASE_URL = "http://real.kanachu.jp/pc/displayapproachinfo"
MAX_BUSES_TO_FETCH = 5
WEATHER_FETCH_HOUR = 9
BUS_SERVICE_START_HOUR = 6
BUS_SERVICE_START_MINUTE = 20

# 運用開始日の設定は削除（運用日数表示削除のため）
# SERVICE_OPERATION_START_DATE = datetime.date(2024, 5, 1)

TOKYO_TZ = pytz.timezone('Asia/Tokyo')

KEY_DEPARTURE_TIME = "departure_time"
KEY_STATUS_TEXT = "status_text"
KEY_TIME_UNTIL = "time_until_departure"
KEY_IS_URGENT = "is_urgent"

# --- ルート情報 ---
SANNODAI_TO_ISEHARA = "sannodai_to_isehara"
FROM_STOP_NO_SANNODAI = "18137" #
TO_STOP_NO_SANNODAI = "18100" #
FROM_STOP_NAME_SANNODAI = "産業能率大学" #
TO_STOP_NAME_SANNODAI = "伊勢原駅北口" #

ISEHARA_TO_SANNODAI = "isehara_to_sannodai"
FROM_STOP_NO_ISEHARA = "18100" #
TO_STOP_NO_ISEHARA = "18137" #
FROM_STOP_NAME_ISEHARA = "伊勢原駅北口" #
TO_STOP_NAME_ISEHARA = "産業能率大学" #

DEFAULT_DIRECTION = SANNODAI_TO_ISEHARA

app = Flask(__name__)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(funcName)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

weather_cache = {"data": None, "timestamp": 0, "error": None}
bus_data_cache_sannodai = {"data": [], "timestamp": 0, "error": None, "data_valid": True}
bus_data_cache_isehara = {"data": [], "timestamp": 0, "error": None, "data_valid": True}
weather_fetched_today_g = False
last_date_weather_checked_g = None

WEATHER_CACHE_DURATION_SECONDS = 30 * 60
BUS_DATA_CACHE_DURATION_SECONDS = 10

def get_current_route_details(direction_param):
    if direction_param == ISEHARA_TO_SANNODAI:
        return {
            "from_stop_no": FROM_STOP_NO_ISEHARA,
            "to_stop_no": TO_STOP_NO_ISEHARA,
            "from_stop_name": FROM_STOP_NAME_ISEHARA,
            "to_stop_name": TO_STOP_NAME_ISEHARA,
            "current_direction": ISEHARA_TO_SANNODAI,
            "bus_cache": bus_data_cache_isehara
        }
    return {
        "from_stop_no": FROM_STOP_NO_SANNODAI,
        "to_stop_no": TO_STOP_NO_SANNODAI,
        "from_stop_name": FROM_STOP_NAME_SANNODAI,
        "to_stop_name": TO_STOP_NAME_SANNODAI,
        "current_direction": SANNODAI_TO_ISEHARA,
        "bus_cache": bus_data_cache_sannodai
    }

def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE": #
        logging.warning("Discord Webhook URLが未設定のため、通知は送信されません。") #
        return
    payload = {"content": message, "username": os.environ.get("DISCORD_USERNAME", "バス情報チェッカー")} #
    headers = {"Content-Type": "application/json"} #
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, data=json.dumps(payload), headers=headers, timeout=5) #
        response.raise_for_status() #
        logging.info(f"Discord通知送信成功: {message[:50]}...") #
    except requests.exceptions.RequestException as e:
        logging.error(f"Discord通知送信失敗: {e}") #
    except Exception as e:
        logging.error(f"Discord通知送信中に予期せぬエラー: {e}") #

def get_weather_info(api_key, location_query):
    global weather_fetched_today_g
    if not api_key or api_key == "YOUR_OPENWEATHERMAP_API_KEY_HERE": #
        logging.warning("OpenWeatherMap APIキーが未設定。") #
        return None, None, None, "APIキー未設定" #
    api_url = "http://api.openweathermap.org/data/2.5/weather" #
    params = {"q": location_query, "appid": api_key, "units": "metric", "lang": "ja"} #
    try:
        response = requests.get(api_url, params=params, timeout=10) #
        response.raise_for_status() #
        data = response.json() #
        if data.get("weather") and len(data["weather"]) > 0: #
            main_condition = data["weather"][0].get("main") #
            description = data["weather"][0].get("description") #
            temp = data.get("main", {}).get("temp") #
            logging.info(f"天気情報取得成功 ({location_query}): {main_condition} ({description}), 気温: {temp}°C") #
            weather_fetched_today_g = True #
            return main_condition, description, temp, None #
        return None, None, None, "APIレスポンス形式不正" #
    except requests.exceptions.Timeout:
        logging.warning(f"天気情報取得タイムアウト ({location_query})") #
        return None, None, None, "タイムアウト" #
    except requests.exceptions.HTTPError as http_err:
        error_message = f"HTTPエラー {http_err.response.status_code}" #
        if http_err.response.status_code == 401: #
             error_message = "APIキーが無効か認証エラーです。" #
        logging.error(f"天気情報取得HTTPエラー ({location_query}): {http_err}") #
        return None, None, None, error_message #
    except Exception as e:
        logging.exception(f"天気情報取得中に予期せぬエラー ({location_query})") #
        return None, None, None, f"予期せぬエラー" #

def fetch_simplified_bus_departure_times(from_stop_no_str, to_stop_no_str):
    params = {'fNO': from_stop_no_str, 'tNO': to_stop_no_str} #
    bus_departure_list = [] #
    try:
        logging.info(f"バス情報取得開始: {from_stop_no_str} -> {to_stop_no_str}")
        response = requests.get(BASE_URL, params=params, timeout=10) #
        response.raise_for_status() #
        html_content = response.content.decode('shift_jis', errors='replace') #
        soup = BeautifulSoup(html_content, 'html.parser') #
        main_content_area = soup.find('div', class_='inner2 pa01') #
        if not main_content_area: #
            logging.warning("バス情報: main_content_area (div.inner2.pa01) が見つかりません。")
            return {"buses": [], "error": "主要コンテンツエリア解析不可"}
        bus_info_headings = main_content_area.find_all('h3', class_='heading3') #
        if not bus_info_headings: #
            logging.info("バス情報: h3.heading3 が見つかりません。この時間帯のバスはない可能性があります。")
            return {"buses": [], "error": None} #
        for heading_tag in bus_info_headings: #
            if len(bus_departure_list) >= MAX_BUSES_TO_FETCH: break #
            hgroup_element = heading_tag.parent #
            bus_wrap_element = None #
            if hgroup_element and hgroup_element.name == 'div' and 'hgroup01' in hgroup_element.get('class', []): #
                bus_wrap_element = hgroup_element.find_next_sibling('div', class_='wrap') #
            if not bus_wrap_element: bus_wrap_element = heading_tag.find_next_sibling('div', class_='wrap') #
            if not bus_wrap_element: continue #
            col02 = bus_wrap_element.find('div', class_='col02') #
            if not col02: continue #
            frame_box_03 = col02.find('div', class_='frameBox03') #
            if not frame_box_03: continue #
            approach_info_title_element = frame_box_03.find('p', class_='title01') #
            if not approach_info_title_element: continue #
            title_text_raw = approach_info_title_element.get_text(strip=True) #
            departure_time_str = None #
            if "まもなく発車します" in title_text_raw or "まもなく到着" in title_text_raw: departure_time_str = "まもなく" #
            elif "通過しました" in title_text_raw or "出発しました" in title_text_raw: departure_time_str = "出発済み" #
            else:
                match_time_candidate = re.search(r'(\d{1,2}:\d{2})発?', title_text_raw) #
                time_part = None #
                if match_time_candidate: time_part = match_time_candidate.group(1) #
                if "予定通り発車します" in title_text_raw: #
                    if time_part: departure_time_str = f"{time_part}発 (予定通り)" #
                    else: departure_time_str = "状態不明 (予定通り情報あり)" #
                elif "頃発車します" in title_text_raw: #
                    if time_part: departure_time_str = f"{time_part}発 (遅延可能性あり)" #
                    else: departure_time_str = "状態不明 (遅延情報あり)" #
                elif "発予定" in title_text_raw: #
                    if time_part: departure_time_str = f"{time_part}発 (予定)" #
                    else: departure_time_str = "状態不明 (予定情報あり)" #
                elif time_part: departure_time_str = f"{time_part}発" #
            if departure_time_str: #
                bus_departure_list.append({KEY_DEPARTURE_TIME: departure_time_str, KEY_STATUS_TEXT: title_text_raw}) #
        return {"buses": bus_departure_list, "error": None} #
    except requests.exceptions.Timeout:
        error_msg = f"バス情報取得タイムアウト ({from_stop_no_str} -> {to_stop_no_str})"
        logging.warning(error_msg) #
        return {"buses": [], "error": error_msg} #
    except requests.exceptions.RequestException as e:
        error_msg = f"バス情報取得リクエストエラー: {e} ({from_stop_no_str} -> {to_stop_no_str})"
        logging.error(error_msg) #
        return {"buses": [], "error": error_msg} #
    except Exception as e:
        error_msg = f"バス情報取得中に予期せぬエラー: {e} ({from_stop_no_str} -> {to_stop_no_str})"
        logging.exception(error_msg) #
        return {"buses": [], "error": error_msg} #

def calculate_and_format_time_until(departure_str, status_text_raw, current_dt_tokyo):
    is_urgent = False #
    time_until_str = "" #
    if "まもなく" in departure_str: #
        time_until_str = "まもなく" #
        is_urgent = True #
    elif "出発済み" in departure_str: #
        time_until_str = "出発済み" #
    else:
        match = re.search(r'(\d{1,2}:\d{2})発', departure_str) #
        if not match: #
            if "(予定通り)" in departure_str: time_until_str = "" #
            elif "(遅延可能性あり)" in departure_str: time_until_str = "遅延可能性あり" #
            elif "(予定)" in departure_str: time_until_str = "予定" #
            else: time_until_str = "" #
            return time_until_str, is_urgent #
        bus_time_str = match.group(1) #
        try:
            bus_hour, bus_minute = map(int, bus_time_str.split(':')) #
            bus_dt_today_tokyo = current_dt_tokyo.replace(hour=bus_hour, minute=bus_minute, second=0, microsecond=0) #
            if bus_dt_today_tokyo < current_dt_tokyo and (current_dt_tokyo.hour >= 20 and bus_hour <= 5): #
                bus_dt_today_tokyo += datetime.timedelta(days=1) #
            if bus_dt_today_tokyo < current_dt_tokyo: #
                if "予定通り発車します" not in status_text_raw and "通過しました" not in status_text_raw and "出発しました" not in status_text_raw: #
                    time_until_str = "発車済みのおそれあり" #
                else: time_until_str = "出発済み" #
            else:
                delta = bus_dt_today_tokyo - current_dt_tokyo #
                total_seconds = int(delta.total_seconds()) #
                if total_seconds <= 15: #
                    time_until_str = "まもなく発車" #
                    is_urgent = True #
                elif total_seconds <= 180: #
                    minutes_until = total_seconds // 60 #
                    seconds_until = total_seconds % 60 #
                    is_urgent = True #
                    if minutes_until > 0: time_until_str = f"あと{minutes_until}分{seconds_until}秒" #
                    else: time_until_str = f"あと{seconds_until}秒" #
                else:
                    minutes_until = total_seconds // 60 #
                    time_until_str = f"あと{minutes_until}分" #
        except ValueError: time_until_str = f"時刻形式エラー ({departure_str})" #
        except Exception: time_until_str = "計算エラー" #
    return time_until_str, is_urgent #

@app.route('/')
def index():
    direction = request.args.get('direction', DEFAULT_DIRECTION)
    route_details = get_current_route_details(direction)

    # 運用日数関連の計算は削除
    # current_dt_tokyo = datetime.datetime.now(TOKYO_TZ)
    # current_date_tokyo = current_dt_tokyo.date()
    # uptime_days = (current_date_tokyo - SERVICE_OPERATION_START_DATE).days + 1

    app.config['ACTIVE_DATA_FETCH_INTERVAL'] = BUS_DATA_CACHE_DURATION_SECONDS #

    other_direction = ISEHARA_TO_SANNODAI if route_details["current_direction"] == SANNODAI_TO_ISEHARA else SANNODAI_TO_ISEHARA
    other_direction_name = TO_STOP_NAME_ISEHARA if route_details["current_direction"] == SANNODAI_TO_ISEHARA else TO_STOP_NAME_SANNODAI # 修正: 正しい反対方向の「行き先名」
    
    return render_template('index.html',
                           from_stop=route_details["from_stop_name"],
                           to_stop=route_details["to_stop_name"],
                           # initial_uptime_days=uptime_days, # 削除
                           config=app.config, #
                           current_direction_param=route_details["current_direction"],
                           other_direction_param=other_direction,
                           other_direction_name=other_direction_name 
                           )

@app.route('/api/data')
def api_data():
    global weather_cache, weather_fetched_today_g, last_date_weather_checked_g
    
    direction_param = request.args.get('direction', DEFAULT_DIRECTION)
    route_details = get_current_route_details(direction_param)
    current_bus_cache = route_details["bus_cache"]

    current_dt_tokyo = datetime.datetime.now(TOKYO_TZ) #
    current_time_unix = time.time() #
    current_hour = current_dt_tokyo.hour #
    current_date = current_dt_tokyo.date() #

    weather_data_to_display = {} #
    if last_date_weather_checked_g != current_date: #
        weather_fetched_today_g = False #
        last_date_weather_checked_g = current_date #
        logging.info(f"日付変更 ({current_date})。天気取得フラグ解除。") #

    if current_hour == WEATHER_FETCH_HOUR and not weather_fetched_today_g: #
        logging.info(f"{WEATHER_FETCH_HOUR}時台、天気情報更新試行。") #
        condition, description, temp, error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION) #
        weather_cache["data"] = {"condition": condition, "description": description, "temp_c": temp, "is_rain": (condition and "rain" in condition.lower())} #
        weather_cache["error"] = error #
        weather_cache["timestamp"] = current_time_unix #
        if not error: #
            weather_fetched_today_g = True #
    
    weather_data_to_display = weather_cache["data"] if weather_cache.get("data") else {} #
    weather_data_to_display["error_message"] = weather_cache.get("error") #

    processed_buses = [] #
    bus_fetch_error = None #
    # app_state_message は削除
    # app_state_message = "監視中" 

    is_before_service_hours = current_hour < BUS_SERVICE_START_HOUR or \
                              (current_hour == BUS_SERVICE_START_HOUR and current_dt_tokyo.minute < BUS_SERVICE_START_MINUTE) #

    # app_state_message の設定ロジックは削除
    # if is_before_service_hours:
    #     app_state_message = f"始発バス待機中 (～{BUS_SERVICE_START_HOUR:02d}:{BUS_SERVICE_START_MINUTE:02d}目安)"
    
    # 早朝でもキャッシュされたバスがあれば表示試行（前日最終など）
    if is_before_service_hours and current_bus_cache.get("data"):
        for bus_info_original in current_bus_cache["data"]: #
            bus_info = bus_info_original.copy() #
            time_until_str, is_urgent = calculate_and_format_time_until(
                bus_info.get(KEY_DEPARTURE_TIME, ""), #
                bus_info.get(KEY_STATUS_TEXT, ""), #
                current_dt_tokyo
            )
            bus_info[KEY_TIME_UNTIL] = time_until_str #
            bus_info[KEY_IS_URGENT] = is_urgent #
            processed_buses.append(bus_info) #
        bus_fetch_error = current_bus_cache.get("error") #
    
    elif not is_before_service_hours and \
         (current_time_unix - current_bus_cache.get("timestamp", 0) > BUS_DATA_CACHE_DURATION_SECONDS \
         or not current_bus_cache.get("data") or not current_bus_cache.get("data_valid", True)): #
        logging.info(f"バス情報を更新します ({route_details['current_direction']}): キャッシュ期限切れまたは初回または無効データ。") #
        bus_result = fetch_simplified_bus_departure_times(route_details["from_stop_no"], route_details["to_stop_no"]) #
        current_bus_cache["data"] = bus_result.get("buses", []) #
        current_bus_cache["error"] = bus_result.get("error") #
        current_bus_cache["timestamp"] = current_time_unix #
        current_bus_cache["data_valid"] = True  #
        if bus_result.get("error"): #
            current_bus_cache["data_valid"] = False #
    
    bus_fetch_error = current_bus_cache.get("error") #
    if current_bus_cache.get("data"): #
        for bus_info_original in current_bus_cache["data"]: #
            bus_info = bus_info_original.copy() #
            time_until_str, is_urgent = calculate_and_format_time_until(
                bus_info.get(KEY_DEPARTURE_TIME, ""), #
                bus_info.get(KEY_STATUS_TEXT, ""), #
                current_dt_tokyo
            )
            bus_info[KEY_TIME_UNTIL] = time_until_str #
            bus_info[KEY_IS_URGENT] = is_urgent #
            processed_buses.append(bus_info) #
    
    # app_state_message の設定ロジックは削除
    # if not is_before_service_hours and not bus_fetch_error and not processed_buses:
    #     app_state_message = "情報なし/運行終了の可能性" #
    
    system_healthy = True #
    system_warning = False #

    if bus_fetch_error: #
        system_healthy = False #
        logging.warning(f"システム状態: バス情報取得エラーのため不健康 - {bus_fetch_error}") #

    if weather_data_to_display.get("error_message"): #
        if "APIキー" in weather_data_to_display["error_message"] or "認証エラー" in weather_data_to_display["error_message"] : #
            system_healthy = False  #
            logging.warning(f"システム状態: 天気APIキー/認証エラーのため不健康 - {weather_data_to_display['error_message']}") #
        else: 
            system_warning = True #
            logging.warning(f"システム状態: 天気情報取得に軽微な問題 - {weather_data_to_display['error_message']}") #
    
    # app_state_message に基づく判定の調整も削除
            
    # 運用日数の計算は削除
    # current_date_tokyo_for_uptime = datetime.datetime.now(TOKYO_TZ).date()
    # uptime_days = (current_date_tokyo_for_uptime - SERVICE_OPERATION_START_DATE).days + 1

    # APIレスポンスから app_state_message と uptime_days を削除
    return jsonify(
        from_stop=route_details["from_stop_name"],
        to_stop=route_details["to_stop_name"],
        weather_data=weather_data_to_display,
        # app_state_message=app_state_message, # 削除
        buses_to_display=processed_buses,
        bus_error_message=bus_fetch_error,
        bus_last_updated_str=datetime.datetime.fromtimestamp(current_bus_cache.get("timestamp", 0), TOKYO_TZ).strftime('%H:%M:%S') if current_bus_cache.get("timestamp", 0) > 0 else "N/A", #
        system_status={'healthy': system_healthy, 'warning': system_warning}, #
        # uptime_days=uptime_days, # 削除
        current_direction=route_details["current_direction"]
    )

if __name__ == '__main__':
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE": #
        logging.warning("ローカルテスト: Discord Webhook URL未設定") #
    if not OPENWEATHERMAP_API_KEY or OPENWEATHERMAP_API_KEY == "YOUR_OPENWEATHERMAP_API_KEY_HERE": #
        logging.warning("ローカルテスト: OpenWeatherMap APIキー未設定") #
    # SERVICE_OPERATION_START_DATE のログは削除
    # logging.info(f"SERVICE_OPERATION_START_DATE: {SERVICE_OPERATION_START_DATE}")
    app.run(host='127.0.0.1', port=8080, debug=True) #
