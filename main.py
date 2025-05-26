from flask import Flask, render_template, request
import requests
from bs4 import BeautifulSoup
import json
import re
import datetime
import time
import os
import pytz
import logging

# --- 設定 ---
OPENWEATHERMAP_API_KEY = os.environ.get("OPENWEATHERMAP_API_KEY", "YOUR_OPENWEATHERMAP_API_KEY_HERE")
OPENWEATHERMAP_API_KEY = "28482976c81657127a816a47f53cc3d2"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "YOUR_DISCORD_WEBHOOK_URL_HERE")
WEATHER_LOCATION = "Isehara,JP"
BASE_URL = "http://real.kanachu.jp/pc/displayapproachinfo"

# 経路情報を定数化
ROUTE_SANNODAI_TO_ISEHARA = {
    "from_stop_no": "18137",
    "to_stop_no": "18100",
    "from_stop_name": "産業能率大学",
    "to_stop_name": "伊勢原駅北口",
    "id": "sannodai_to_isehara"
}
ROUTE_ISEHARA_TO_SANNODAI = {
    "from_stop_no": "18100",
    "to_stop_no": "18137",
    "from_stop_name": "伊勢原駅北口",
    "to_stop_name": "産業能率大学",
    "id": "isehara_to_sannodai"
}

MAX_BUSES_TO_FETCH = 10 # 取得件数を10件に変更
BUS_SERVICE_START_HOUR = 6
BUS_SERVICE_START_MINUTE = 20

TOKYO_TZ = pytz.timezone('Asia/Tokyo')

KEY_DEPARTURE_TIME = "departure_time"
KEY_STATUS_TEXT = "status_text"
KEY_TIME_UNTIL = "time_until_departure"
KEY_IS_URGENT = "is_urgent"

app = Flask(__name__)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(funcName)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

# --- グローバル変数 ---
weather_cache = {
    "data": None,
    "timestamp": 0,
    "error": None,
    "has_rained_before_3pm_today": False, # この機能は前回の会話で「取得時に雨が降っている場合のみ」に変更
    "last_rain_check_date": None
}
# バス情報のキャッシュを経路ごとに持つように変更
bus_data_cache_by_route = {
    ROUTE_SANNODAI_TO_ISEHARA["id"]: {"data": [], "timestamp": 0, "error": None},
    ROUTE_ISEHARA_TO_SANNODAI["id"]: {"data": [], "timestamp": 0, "error": None}
}

HOURLY_WEATHER_FETCH_INTERVAL = 60 * 60  # 1時間 (秒)
BUS_DATA_CACHE_DURATION_SECONDS = 30

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
    if not api_key or api_key == "YOUR_OPENWEATHERMAP_API_KEY_HERE": #
        logging.warning("OpenWeatherMap APIキーが未設定。") #
        return None, None, None, "APIキー未設定" #
    api_url = "http://api.openweathermap.org/data/2.5/weather"
    params = {"q": location_query, "appid": api_key, "units": "metric", "lang": "ja"}
    temp = None
    try:
        response = requests.get(api_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("main"): temp = data.get("main", {}).get("temp")
        if data.get("weather") and len(data["weather"]) > 0:
            main_condition = data["weather"][0].get("main")
            description = data["weather"][0].get("description")
            logging.info(f"天気情報取得成功 ({location_query}): {main_condition} ({description}), 気温: {temp}°C")
            return main_condition, description, temp, None
        return None, None, temp, "APIレスポンス形式不正"
    except requests.exceptions.Timeout:
        logging.warning(f"天気情報取得タイムアウト ({location_query})")
        return None, None, temp, "タイムアウト"
    except requests.exceptions.HTTPError as http_err:
        error_message = f"HTTPエラー {http_err.response.status_code}"
        if http_err.response.status_code == 401:
             error_message = "APIキーが無効か認証エラーです。"
        logging.error(f"天気情報取得HTTPエラー ({location_query}): {http_err}")
        return None, None, temp, error_message
    except Exception as e:
        logging.exception(f"天気情報取得中に予期せぬエラー ({location_query})")
        return None, None, temp, f"予期せぬエラー"

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
            if len(bus_departure_list) >= MAX_BUSES_TO_FETCH: break #
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
            match_time_candidate = re.search(r'(\d{1,2}:\d{2})発', title_text_raw)
            if match_time_candidate: time_part = match_time_candidate.group(1)

            if "まもなく発車します" in title_text_raw or "まもなく到着" in title_text_raw:
                departure_time_str = "まもなく"
            elif "通過しました" in title_text_raw or "出発しました" in title_text_raw:
                departure_time_str = "出発済み"
            elif "予定通り発車します" in title_text_raw:
                if time_part: departure_time_str = f"{time_part}発 (予定通り)"
                else: departure_time_str = "状態不明 (予定通り情報あり)"
            elif "頃発車します" in title_text_raw:
                if time_part: departure_time_str = f"{time_part}発 (遅延可能性あり)"
                else: departure_time_str = "状態不明 (遅延情報あり)"
            elif "発予定" in title_text_raw:
                if time_part: departure_time_str = f"{time_part}発 (予定)"
                else: departure_time_str = "状態不明 (予定情報あり)"
            elif time_part:
                departure_time_str = f"{time_part}発"

            if departure_time_str:
                bus_departure_list.append({
                    KEY_DEPARTURE_TIME: departure_time_str,
                    KEY_STATUS_TEXT: title_text_raw
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
        logging.exception(error_msg)
        return {"buses": [], "error": error_msg}

@app.route('/')
def index():
    global weather_cache, bus_data_cache_by_route

    # 経路の選択
    direction_id = request.args.get('direction', ROUTE_SANNODAI_TO_ISEHARA["id"])
    current_route_info = {}
    if direction_id == ROUTE_ISEHARA_TO_SANNODAI["id"]:
        current_route_info = ROUTE_ISEHARA_TO_SANNODAI
    else: # デフォルトまたはIDが一致しない場合は産能大→伊勢原駅
        current_route_info = ROUTE_SANNODAI_TO_ISEHARA
        direction_id = current_route_info["id"] # 明示的にIDを再設定

    from_stop_no_to_fetch = current_route_info["from_stop_no"]
    to_stop_no_to_fetch = current_route_info["to_stop_no"]
    from_stop_name_display = current_route_info["from_stop_name"]
    to_stop_name_display = current_route_info["to_stop_name"]

    current_dt_tokyo = datetime.datetime.now(TOKYO_TZ)
    current_time_unix = time.time()
    current_date = current_dt_tokyo.date()
    current_date_str = current_date.isoformat()
    current_hour = current_dt_tokyo.hour

    # --- 「15時までに雨が降ったか」フラグの日次リセット (この機能は「現在雨が降っている場合のみ」に変更されたため、has_rained_before_3pm_todayは直接使わない)
    if weather_cache.get("last_rain_check_date") != current_date_str:
        logging.info(f"日付が {current_date_str} に変わりました。")
        weather_cache["last_rain_check_date"] = current_date_str
        weather_cache["has_rained_before_3pm_today"] = False # 念のためリセット
        weather_cache["timestamp"] = 0

    # --- 天気情報の取得とキャッシュ (約1時間ごと) ---
    if current_time_unix - weather_cache.get("timestamp", 0) > HOURLY_WEATHER_FETCH_INTERVAL \
       or weather_cache.get("data") is None:
        logging.info(f"天気情報キャッシュ期限切れまたはデータなし。天気情報を更新します。最終取得: {weather_cache.get('timestamp', 0)}")
        condition, description, temp, error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION) #
        current_is_rain = (condition and condition.lower() == "rain" and not error)
        weather_cache["data"] = {"condition": condition, "description": description, "temp_c": temp, "is_rain": current_is_rain}
        weather_cache["error"] = error
        weather_cache["timestamp"] = current_time_unix
        # if not error and current_is_rain and current_dt_tokyo.hour < 15: # このロジックは不要になった
        #     weather_cache["has_rained_before_3pm_today"] = True
        logging.info(f"天気情報を更新。キャッシュ時刻: {weather_cache['timestamp']}")

    weather_data_to_display = weather_cache["data"].copy() if weather_cache.get("data") else {}
    weather_data_to_display["error_message"] = weather_cache.get("error")
    
    show_umbrella_warning = False
    if weather_data_to_display.get("is_rain"): # 現在雨が降っている場合のみ警告
        show_umbrella_warning = True
        logging.debug("傘警告表示: ON (理由: 現在雨が降っているため)")
    weather_data_to_display["show_umbrella_warning"] = show_umbrella_warning
    
    if weather_cache["timestamp"] > 0:
         weather_data_to_display["last_updated_readable"] = datetime.datetime.fromtimestamp(weather_cache["timestamp"], TOKYO_TZ).strftime('%Y-%m-%d %H:%M:%S')

    # --- バス情報の取得とキャッシュ (経路ごと) ---
    processed_buses = []
    bus_fetch_error = None
    app_state_message = "作動中"
    
    # 現在の経路用のバス情報キャッシュを取得
    current_bus_cache = bus_data_cache_by_route[direction_id]

    is_before_service_hours = current_hour < BUS_SERVICE_START_HOUR or \
                              (current_hour == BUS_SERVICE_START_HOUR and current_dt_tokyo.minute < BUS_SERVICE_START_MINUTE)

    if is_before_service_hours:
        app_state_message = f"始発バス待機中 (～{BUS_SERVICE_START_HOUR:02d}:{BUS_SERVICE_START_MINUTE:02d}目安)"
        raw_buses_from_cache = current_bus_cache.get("data", [])
        bus_fetch_error = current_bus_cache.get("error")
    elif current_time_unix - current_bus_cache.get("timestamp", 0) > BUS_DATA_CACHE_DURATION_SECONDS \
         or not current_bus_cache.get("data") and not current_bus_cache.get("error"):
        logging.info(f"バス情報 ({from_stop_name_display} -> {to_stop_name_display}) を更新します。")
        bus_result = fetch_simplified_bus_departure_times(from_stop_no_to_fetch, to_stop_no_to_fetch)
        current_bus_cache["data"] = bus_result.get("buses", [])
        current_bus_cache["error"] = bus_result.get("error")
        current_bus_cache["timestamp"] = current_time_unix
        raw_buses_from_cache = current_bus_cache["data"]
        bus_fetch_error = current_bus_cache["error"]
        if bus_fetch_error:
            send_discord_notification(f"🚨 バス情報取得エラー発生 ({direction_id}): {bus_fetch_error}")
    else:
        raw_buses_from_cache = current_bus_cache.get("data", [])
        bus_fetch_error = current_bus_cache.get("error")
        logging.info(f"バス情報 ({from_stop_name_display} -> {to_stop_name_display}) をキャッシュから使用 (最終取得: {current_bus_cache.get('timestamp', 0)})")

    if isinstance(raw_buses_from_cache, list):
        for bus_info_original in raw_buses_from_cache:
            bus_info = bus_info_original.copy()
            departure_time_display_str = bus_info_original.get(KEY_DEPARTURE_TIME, "")
            status_text_from_site = bus_info_original.get(KEY_STATUS_TEXT, "")
            time_until_str = ""
            is_urgent = False
            departure_timestamp_utc = None

            if "まもなく" in departure_time_display_str:
                time_until_str = "まもなく発車"
                is_urgent = True
                departure_timestamp_utc = int((current_dt_tokyo + datetime.timedelta(seconds=30)).timestamp())
            elif "出発済み" in departure_time_display_str or "通過しました" in status_text_from_site:
                time_until_str = "出発済み"
                is_urgent = False
            else:
                match = re.search(r'(\d{1,2}:\d{2})発', departure_time_display_str)
                if not match:
                    match = re.search(r'(\d{1,2}:\d{2})(?:頃発車します|発予定)', status_text_from_site)
                if match:
                    bus_hour, bus_minute = map(int, match.group(1).split(':'))
                    try:
                        bus_dt_candidate = current_dt_tokyo.replace(hour=bus_hour, minute=bus_minute, second=0, microsecond=0)
                        if bus_dt_candidate < current_dt_tokyo and \
                           (current_dt_tokyo.hour >= 20 and bus_hour <= 5):
                            bus_dt_candidate += datetime.timedelta(days=1)
                        departure_timestamp_utc = int(bus_dt_candidate.timestamp())
                        if bus_dt_candidate < current_dt_tokyo:
                            if "予定通り発車します" not in status_text_from_site and \
                               "通過しました" not in status_text_from_site and \
                               "出発しました" not in status_text_from_site:
                                time_until_str = "発車済みのおそれあり"
                            else: time_until_str = "出発済み"
                            is_urgent = False
                        else:
                            delta = bus_dt_candidate - current_dt_tokyo
                            total_seconds = int(delta.total_seconds())
                            if total_seconds <= 15:
                                time_until_str = "まもなく発車"; is_urgent = True
                            elif total_seconds <= 180:
                                minutes_until = total_seconds // 60
                                seconds_until = total_seconds % 60
                                time_until_str = f"あと{minutes_until}分{seconds_until}秒" if minutes_until > 0 else f"あと{seconds_until}秒"
                                is_urgent = True
                            else:
                                minutes_until = total_seconds // 60
                                time_until_str = f"あと{minutes_until}分"
                                if "遅延可能性あり" in departure_time_display_str or "頃発車します" in status_text_from_site:
                                    is_urgent = True
                    except ValueError:
                        time_until_str = "時刻解析エラー"; departure_timestamp_utc = None
                else:
                    if "遅延可能性あり" in departure_time_display_str or "頃発車します" in status_text_from_site :
                        time_until_str = "遅延可能性あり"; is_urgent = True
                    elif "予定" in departure_time_display_str: time_until_str = "時間未定"
            bus_info[KEY_TIME_UNTIL] = time_until_str
            bus_info[KEY_IS_URGENT] = is_urgent
            bus_info['departure_timestamp_utc'] = departure_timestamp_utc
            bus_info['raw_departure_text'] = departure_time_display_str
            processed_buses.append(bus_info)
    else:
        logging.error(f"raw_buses_from_cache for {direction_id} is not a list. Skipping processing.")
        bus_fetch_error = bus_fetch_error or "内部データエラー"

    if not is_before_service_hours and not bus_fetch_error and not processed_buses:
        if current_bus_cache["timestamp"] > 0 and (current_time_unix - current_bus_cache["timestamp"] < BUS_DATA_CACHE_DURATION_SECONDS * 3) :
             app_state_message = "情報なし/周辺に運行中のバスなし"

    return render_template('index.html',
                           from_stop=from_stop_name_display,
                           to_stop=to_stop_name_display,
                           weather_data=weather_data_to_display,
                           app_state_message=app_state_message,
                           buses_to_display=processed_buses,
                           bus_error_message=bus_fetch_error,
                           bus_last_updated_str=datetime.datetime.fromtimestamp(current_bus_cache["timestamp"], TOKYO_TZ).strftime('%H:%M:%S') if current_bus_cache["timestamp"] > 0 else "N/A",
                           current_direction_id=direction_id,
                           route_sannodai_id=ROUTE_SANNODAI_TO_ISEHARA["id"],
                           route_isehara_id=ROUTE_ISEHARA_TO_SANNODAI["id"],
                           config={'ACTIVE_DATA_FETCH_INTERVAL': BUS_DATA_CACHE_DURATION_SECONDS}
                           )

if __name__ == '__main__':
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        logging.warning("ローカルテスト: Discord Webhook URL未設定")
    if not OPENWEATHERMAP_API_KEY or OPENWEATHERMAP_API_KEY == "YOUR_OPENWEATHERMAP_API_KEY_HERE":
        logging.warning("ローカルテスト: OpenWeatherMap APIキー未設定")
    app.run(host='127.0.0.1', port=8080, debug=True)
