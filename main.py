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

# --- 設定 ---
# APIキーとWebhook URLを直接記述
OPENWEATHERMAP_API_KEY = "28482976c81657127a816a47f53cc3d2" # ユーザー指定の実際のキー
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1375497603466395749/4QtOWTUk-_44xc8-RVmhm3imPatU4yiEuRj1NR1j5PryEkbik98A204uJ3069nye_GNI" # ユーザー指定の実際のURL
WEATHER_LOCATION = "Isehara,JP"
BASE_URL = "http://real.kanachu.jp/pc/displayapproachinfo"

# 産業能率大学 -> 伊勢原駅北口
FROM_STOP_NO_SANNODAI = "18137"
TO_STOP_NO_SANNODAI = "18100"
FROM_STOP_NAME_SANNODAI = "産業能率大学"
TO_STOP_NAME_SANNODAI = "伊勢原駅北口"

# 伊勢原駅北口 -> 産業能率大学 (バス停番号は要確認)
FROM_STOP_NO_ISEHARA = "18100"
TO_STOP_NO_ISEHARA = "18137"
FROM_STOP_NAME_ISEHARA = "伊勢原駅北口"
TO_STOP_NAME_ISEHARA = "産業能率大学"

MAX_BUSES_TO_FETCH = 10
WEATHER_FETCH_HOUR = 9 # 天気情報を取得する時間帯（時）
BUS_SERVICE_START_HOUR = 6
BUS_SERVICE_START_MINUTE = 20

# SERVICE_OPERATION_START_DATE は index.html で使用されなくなったためコメントアウト
# SERVICE_OPERATION_START_DATE = datetime.date(2024, 5, 1)

TOKYO_TZ = pytz.timezone('Asia/Tokyo')

KEY_DEPARTURE_TIME = "departure_time"
KEY_STATUS_TEXT = "status_text"
KEY_TIME_UNTIL = "time_until_departure"
KEY_IS_URGENT = "is_urgent"

app = Flask(__name__)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(funcName)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

weather_cache = {"data": None, "timestamp": 0, "error": None}
bus_data_cache = {} # 方向ごとのキャッシュを格納
weather_fetched_today_g = False # 天気情報をその日に取得済みかのフラグ
last_date_weather_checked_g = None # 天気情報取得の是非を判断するために最後に確認した日付

WEATHER_CACHE_DURATION_SECONDS = 30 * 60 # 天気キャッシュの有効期間 (30分)
BUS_DATA_CACHE_DURATION_SECONDS = 10    # バス情報キャッシュの有効期間 (10秒)

def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE": # プレースホルダーチェック
        logging.warning("Discord Webhook URLが未設定またはプレースホルダーのため、通知は送信されません。")
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
    if not api_key or api_key == "YOUR_OPENWEATHERMAP_API_KEY_HERE": # プレースホルダーチェック
        logging.warning("OpenWeatherMap APIキーが未設定またはプレースホルダーです。")
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
            weather_fetched_today_g = True # 取得成功フラグ
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
        if not main_content_area: return {"buses": [], "error": None}
        bus_info_headings = main_content_area.find_all('h3', class_='heading3')
        if not bus_info_headings: return {"buses": [], "error": None}
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
            if "まもなく発車します" in title_text_raw or "まもなく到着" in title_text_raw: departure_time_str = "まもなく"
            elif "通過しました" in title_text_raw or "出発しました" in title_text_raw: departure_time_str = "出発済み"
            else:
                match_time_candidate = re.search(r'(\d{1,2}:\d{2})発?', title_text_raw)
                time_part = None
                if match_time_candidate: time_part = match_time_candidate.group(1)
                if "予定通り発車します" in title_text_raw:
                    if time_part: departure_time_str = f"{time_part}発 (予定通り)"
                    else: departure_time_str = "状態不明 (予定通り情報あり)"
                elif "頃発車します" in title_text_raw:
                    if time_part: departure_time_str = f"{time_part}発 (遅延可能性あり)"
                    else: departure_time_str = "状態不明 (遅延情報あり)"
                elif "発予定" in title_text_raw:
                    if time_part: departure_time_str = f"{time_part}発 (予定)"
                    else: departure_time_str = "状態不明 (予定情報あり)"
                elif time_part: departure_time_str = f"{time_part}発"
            if departure_time_str:
                bus_departure_list.append({KEY_DEPARTURE_TIME: departure_time_str, KEY_STATUS_TEXT: title_text_raw})
        return {"buses": bus_departure_list, "error": None}
    except Exception as e:
        error_msg = f"バス情報取得エラー: {e}"
        logging.error(error_msg)
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
            else: time_until_str = ""
            return time_until_str, is_urgent
        bus_time_str = match.group(1)
        try:
            bus_hour, bus_minute = map(int, bus_time_str.split(':'))
            bus_dt_today_tokyo = current_dt_tokyo.replace(hour=bus_hour, minute=bus_minute, second=0, microsecond=0)
            if bus_dt_today_tokyo < current_dt_tokyo and (current_dt_tokyo.hour >= 20 and bus_hour <= 5): # 深夜帯の翌日判定
                bus_dt_today_tokyo += datetime.timedelta(days=1)
            if bus_dt_today_tokyo < current_dt_tokyo: # 過去時刻の場合
                if "予定通り発車します" not in status_text_raw and "通過しました" not in status_text_raw and "出発しました" not in status_text_raw:
                    time_until_str = "発車済みのおそれあり"
                else: time_until_str = "出発済み"
            else:
                delta = bus_dt_today_tokyo - current_dt_tokyo
                total_seconds = int(delta.total_seconds())
                if total_seconds <= 15:
                    time_until_str = "まもなく発車"
                    is_urgent = True
                elif total_seconds <= 180: # 3分以内
                    minutes_until = total_seconds // 60
                    seconds_until = total_seconds % 60
                    is_urgent = True
                    if minutes_until > 0: time_until_str = f"あと{minutes_until}分{seconds_until}秒"
                    else: time_until_str = f"あと{seconds_until}秒"
                else:
                    minutes_until = total_seconds // 60
                    time_until_str = f"あと{minutes_until}分"
        except ValueError: time_until_str = f"時刻形式エラー ({departure_str})"
        except Exception: time_until_str = "計算エラー" # 広範なエラーキャッチ
    return time_until_str, is_urgent

@app.route('/')
def index():
    # initial_uptime_days は index.html で使用されなくなった
    app.config['ACTIVE_DATA_FETCH_INTERVAL'] = BUS_DATA_CACHE_DURATION_SECONDS

    direction = request.args.get('direction', 'sannodai_to_isehara') # デフォルトは産能大→伊勢原駅

    from_stop_display_name = FROM_STOP_NAME_SANNODAI
    to_stop_display_name = TO_STOP_NAME_SANNODAI
    if direction == 'isehara_to_sannodai':
        from_stop_display_name = FROM_STOP_NAME_ISEHARA
        to_stop_display_name = TO_STOP_NAME_ISEHARA

    return render_template('index.html',
                           from_stop=from_stop_display_name,
                           to_stop=to_stop_display_name,
                           # initial_uptime_days は削除
                           config=app.config,
                           current_direction=direction # 現在の方向をテンプレートに渡す
                           )

@app.route('/api/data')
def api_data():
    global weather_cache, bus_data_cache, weather_fetched_today_g, last_date_weather_checked_g

    direction = request.args.get('direction', 'sannodai_to_isehara') # デフォルトは産能大→伊勢原駅

    if direction == 'isehara_to_sannodai':
        current_from_stop_no = FROM_STOP_NO_ISEHARA
        current_to_stop_no = TO_STOP_NO_ISEHARA
        current_from_stop_name = FROM_STOP_NAME_ISEHARA
        current_to_stop_name = TO_STOP_NAME_ISEHARA
    else: # sannodai_to_isehara or default
        current_from_stop_no = FROM_STOP_NO_SANNODAI
        current_to_stop_no = TO_STOP_NO_SANNODAI
        current_from_stop_name = FROM_STOP_NAME_SANNODAI
        current_to_stop_name = TO_STOP_NAME_SANNODAI

    current_dt_tokyo = datetime.datetime.now(TOKYO_TZ)
    current_time_unix = time.time()
    current_hour = current_dt_tokyo.hour
    current_date = current_dt_tokyo.date()

    # --- 天気情報の取得とキャッシュ管理 ---
    weather_data_to_display = {}
    # 日付が変わったら、その日の天気情報をまだ取得していないことにする
    if last_date_weather_checked_g != current_date:
        weather_fetched_today_g = False
        last_date_weather_checked_g = current_date
        logging.info(f"日付変更 ({current_date})。天気取得フラグ解除。")

    # APIキーがプレースホルダーでない場合のみ天気情報を取得試行
    should_fetch_weather_now = (OPENWEATHERMAP_API_KEY and
                                OPENWEATHERMAP_API_KEY != "YOUR_OPENWEATHERMAP_API_KEY_HERE")

    # 特定の時刻になったら、その日の天気情報をまだ取得していなければ取得する
    if current_hour == WEATHER_FETCH_HOUR and not weather_fetched_today_g:
        if should_fetch_weather_now:
            logging.info(f"{WEATHER_FETCH_HOUR}時台、天気情報更新試行。")
            condition, description, temp, error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION)
            weather_cache["data"] = {"condition": condition, "description": description, "temp_c": temp, "is_rain": (condition and "rain" in condition.lower())}
            weather_cache["error"] = error
            weather_cache["timestamp"] = current_time_unix
            # if not error: weather_fetched_today_g = True # get_weather_info内で設定
        else:
            logging.warning(f"{WEATHER_FETCH_HOUR}時台だが、OpenWeatherMap APIキーが未設定のため天気情報取得せず。")
            if weather_cache.get("error") != "APIキー未設定": # まだエラーが記録されていなければ記録
                 weather_cache["error"] = "APIキー未設定"
                 weather_cache["data"] = None
                 weather_cache["timestamp"] = current_time_unix # 更新時刻は記録

    # キャッシュが古い場合、またはデータがない場合に再取得を試みる (APIキーエラーの場合を除く)
    elif (current_time_unix - weather_cache.get("timestamp", 0) > WEATHER_CACHE_DURATION_SECONDS or not weather_cache.get("data")) and \
         weather_cache.get("error") != "APIキー未設定":
        if should_fetch_weather_now:
            logging.info("天気情報を更新します (キャッシュ期限切れまたはデータなし)。")
            condition, description, temp, error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION)
            weather_cache["data"] = {"condition": condition, "description": description, "temp_c": temp, "is_rain": (condition and "rain" in condition.lower())}
            weather_cache["error"] = error
            weather_cache["timestamp"] = current_time_unix
        elif not weather_cache.get("error"): # まだエラーが記録されていなければAPIキー未設定エラーを記録
            logging.warning("OpenWeatherMap APIキーが未設定のため、天気情報取得せず（キャッシュ切れ）。")
            weather_cache["error"] = "APIキー未設定"
            weather_cache["data"] = None
            weather_cache["timestamp"] = current_time_unix


    weather_data_to_display = weather_cache.get("data") if weather_cache.get("data") else {}
    # エラーメッセージはキャッシュから直接取得する（取得試行時に設定されるため）
    weather_data_to_display["error_message"] = weather_cache.get("error")


    # 天気情報の最終更新時刻文字列を生成（index.html用）
    weather_last_updated_timestamp = weather_cache.get("timestamp", 0)
    weather_last_updated_str = "N/A"
    if weather_last_updated_timestamp > 0:
        try:
            weather_last_updated_str = datetime.datetime.fromtimestamp(weather_last_updated_timestamp, TOKYO_TZ).strftime('%H:%M:%S')
        except Exception as e:
            logging.error(f"天気最終更新時刻のフォーマットエラー: {e}")
            weather_last_updated_str = "時刻エラー" # エラー時も何かしら表示

    # --- バス情報の取得とキャッシュ ---
    processed_buses = []
    bus_fetch_error = None
    # app_state_message は index.html で使用されなくなった

    is_before_service_hours = current_hour < BUS_SERVICE_START_HOUR or \
                              (current_hour == BUS_SERVICE_START_HOUR and current_dt_tokyo.minute < BUS_SERVICE_START_MINUTE)

    cache_key_suffix = f"_{current_from_stop_no}_{current_to_stop_no}" # 方向ごとのキャッシュキー
    current_bus_data_cache = bus_data_cache.get(cache_key_suffix, {"data": [], "timestamp": 0, "error": None, "data_valid": True})

    if is_before_service_hours:
        # app_state_message は使用しない
        if current_bus_data_cache.get("data"): # 既存キャッシュのデータを加工して表示
            for bus_info_original in current_bus_data_cache["data"]:
                bus_info = bus_info_original.copy()
                time_until_str, is_urgent = calculate_and_format_time_until(
                    bus_info.get(KEY_DEPARTURE_TIME, ""),
                    bus_info.get(KEY_STATUS_TEXT, ""),
                    current_dt_tokyo
                )
                bus_info[KEY_TIME_UNTIL] = time_until_str
                bus_info[KEY_IS_URGENT] = is_urgent
                processed_buses.append(bus_info)
        bus_fetch_error = current_bus_data_cache.get("error")

    elif current_time_unix - current_bus_data_cache.get("timestamp", 0) > BUS_DATA_CACHE_DURATION_SECONDS \
         or not current_bus_data_cache.get("data") or not current_bus_data_cache.get("data_valid", True): # data_validフラグもチェック
        logging.info(f"バス情報を更新します (キャッシュ期限切れまたは初回または無効データ) - 区間: {current_from_stop_name} -> {current_to_stop_name}")
        bus_result = fetch_simplified_bus_departure_times(current_from_stop_no, current_to_stop_no)
        
        bus_data_cache[cache_key_suffix] = {
            "data": bus_result.get("buses", []),
            "error": bus_result.get("error"),
            "timestamp": current_time_unix,
            "data_valid": not bool(bus_result.get("error")) # エラーがなければTrue
        }
        current_bus_data_cache = bus_data_cache[cache_key_suffix] # 更新後のキャッシュを再設定

    bus_fetch_error = current_bus_data_cache.get("error")
    if current_bus_data_cache.get("data"):
        for bus_info_original in current_bus_data_cache["data"]:
            bus_info = bus_info_original.copy()
            time_until_str, is_urgent = calculate_and_format_time_until(
                bus_info.get(KEY_DEPARTURE_TIME, ""),
                bus_info.get(KEY_STATUS_TEXT, ""),
                current_dt_tokyo
            )
            bus_info[KEY_TIME_UNTIL] = time_until_str
            bus_info[KEY_IS_URGENT] = is_urgent
            processed_buses.append(bus_info)
    
    # app_state_message は使用しないため、関連する '情報なし/運行終了の可能性' の判定も削除
    
    # --- システム状態 ---
    system_healthy = True
    system_warning = False

    if bus_fetch_error:
        system_healthy = False
        logging.warning(f"システム状態: バス情報取得エラーのため不健康 - {bus_fetch_error}")

    # 天気APIキー未設定は警告、認証エラー等は不健康
    if weather_data_to_display.get("error_message"):
        if "APIキー未設定" == weather_data_to_display.get("error_message"):
            system_warning = True # APIキー未設定は警告のみ
            logging.warning(f"システム状態: 天気APIキー未設定のため警告 - {weather_data_to_display['error_message']}")
        elif "APIキーが無効か認証エラーです。" in weather_data_to_display.get("error_message"):
            system_healthy = False # 認証エラーは不健康
            logging.warning(f"システム状態: 天気APIキー/認証エラーのため不健康 - {weather_data_to_display['error_message']}")
        else: # その他の天気エラーは警告
            system_warning = True
            logging.warning(f"システム状態: 天気情報取得に軽微な問題 - {weather_data_to_display['error_message']}")
    
    # app_state_message に基づく判定は削除
    
    # uptime_days は index.html で使用されなくなった
    
    bus_last_updated_timestamp = current_bus_data_cache.get("timestamp", 0)

    return jsonify(
        weather_data=weather_data_to_display,
        weather_last_updated_str=weather_last_updated_str,
        # app_state_message は削除
        buses_to_display=processed_buses,
        bus_error_message=bus_fetch_error,
        bus_last_updated_str=datetime.datetime.fromtimestamp(bus_last_updated_timestamp, TOKYO_TZ).strftime('%H:%M:%S') if bus_last_updated_timestamp > 0 else "N/A",
        system_status={'healthy': system_healthy, 'warning': system_warning},
        # uptime_days は削除
        from_stop=current_from_stop_name, # HTMLでの表示とタイトル更新に必要
        to_stop=current_to_stop_name,     # HTMLでの表示とタイトル更新に必要
        current_direction=direction       # HTMLでのリンクのアクティブ状態制御に必要
    )

if __name__ == '__main__':
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        logging.warning("ローカルテスト: Discord Webhook URLが未設定またはプレースホルダーのままです。")
    if not OPENWEATHERMAP_API_KEY or OPENWEATHERMAP_API_KEY == "YOUR_OPENWEATHERMAP_API_KEY_HERE":
        logging.warning("ローカルテスト: OpenWeatherMap APIキーが未設定またはプレースホルダーのままです。")
    # SERVICE_OPERATION_START_DATE を削除したため関連ログも削除
    app.run(host='127.0.0.1', port=8080, debug=True)
