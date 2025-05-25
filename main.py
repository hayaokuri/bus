from flask import Flask, render_template
import requests
from bs4 import BeautifulSoup
import json
import re
import datetime
import time # JavaScriptでのリアルタイム更新のため、Python側のtime.sleepは不要になる
import os
import pytz
import logging
import threading # バックグラウンド処理用
# from apscheduler.schedulers.background import BackgroundScheduler # App EngineではCron Serviceを推奨

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

# --- グローバル変数 (App Engineではリクエストごとにリセットされるため、Datastore/Memcache推奨) ---
# この例では、簡略化のためリクエストごとにデータを取得・キャッシュする形を維持
weather_cache = {"data": None, "timestamp": 0, "error": None}
bus_data_cache = {"data": [], "timestamp": 0, "error": None}
weather_fetched_today_g = False # App Engineではリクエスト間で状態を保持しないため、このフラグはCronジョブ側で管理する方が良い
last_date_weather_checked_g = None

WEATHER_CACHE_DURATION_SECONDS = 30 * 60
BUS_DATA_CACHE_DURATION_SECONDS = 10 # バス情報のキャッシュ（兼データ取得間隔）

# (send_discord_notification, get_weather_info, fetch_simplified_bus_departure_times, calculate_and_format_time_until は変更なしのため省略)
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
    global weather_fetched_today_g # このグローバル変数の扱いはApp Engineでは注意
    if not api_key or api_key == "YOUR_OPENWEATHERMAP_API_KEY_HERE":
        logging.warning("OpenWeatherMap APIキーが未設定。")
        return None, None, "APIキー未設定"
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
            weather_fetched_today_g = True # App Engineではリクエストごとにリセットされる
            return main_condition, description, temp, None # 温度も返すように変更
        return None, None, None, "APIレスポンス形式不正"
    except requests.exceptions.Timeout:
        logging.warning(f"天気情報取得タイムアウト ({location_query})")
        return None, None, None, "タイムアウト"
    except requests.exceptions.HTTPError as http_err:
        error_message = f"HTTPエラー {http_err.response.status_code}"
        if http_err.response.status_code == 401:
             error_message = "APIキーが無効か認証エラーです。"
             # send_discord_notification(f"🚨 **天気APIエラー:** {error_message} 確認してください。") # エラー時は通知
        logging.error(f"天気情報取得HTTPエラー ({location_query}): {http_err}")
        return None, None, None, error_message
    except Exception as e:
        logging.exception(f"天気情報取得中に予期せぬエラー ({location_query})")
        # send_discord_notification(f"🚨 **天気API取得中の予期せぬエラー ({location_query}):** {e}") # エラー時は通知
        return None, None, None, f"予期せぬエラー"

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
    except Exception as e: # より広範なエラーキャッチ
        error_msg = f"バス情報取得エラー: {e}"
        logging.error(error_msg)
        # send_discord_notification(f"🛑 **バス情報取得エラー:** {error_msg}") # エラー時は通知
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
            if bus_dt_today_tokyo < current_dt_tokyo and (current_dt_tokyo.hour >= 20 and bus_hour <= 5):
                bus_dt_today_tokyo += datetime.timedelta(days=1)
            if bus_dt_today_tokyo < current_dt_tokyo:
                if "予定通り発車します" not in status_text_raw and "通過しました" not in status_text_raw and "出発しました" not in status_text_raw:
                    time_until_str = "発車済みのおそれあり"
                else: time_until_str = "出発済み"
            else:
                delta = bus_dt_today_tokyo - current_dt_tokyo
                total_seconds = int(delta.total_seconds())
                if total_seconds <= 15:
                    time_until_str = "まもなく発車"
                    is_urgent = True
                elif total_seconds <= 180: 
                    minutes_until = total_seconds // 60
                    seconds_until = total_seconds % 60
                    is_urgent = True 
                    if minutes_until > 0: time_until_str = f"あと{minutes_until}分{seconds_until}秒"
                    else: time_until_str = f"あと{seconds_until}秒"
                else: 
                    minutes_until = total_seconds // 60
                    time_until_str = f"あと{minutes_until}分"
        except ValueError: time_until_str = f"時刻形式エラー ({departure_str})"
        except Exception: time_until_str = "計算エラー"
    return time_until_str, is_urgent
# === ここまで既存の関数群 ===

@app.route('/')
def index():
    global weather_cache, bus_data_cache, weather_fetched_today_g, last_date_weather_checked_g # App Engineでは注意

    current_dt_tokyo = datetime.datetime.now(TOKYO_TZ)
    current_time_unix = time.time()
    current_hour = current_dt_tokyo.hour
    current_date = current_dt_tokyo.date()

    # --- 天気情報の取得とキャッシュ (9時台に1回) ---
    weather_data_to_display = {}
    if last_date_weather_checked_g != current_date: # 日付が変わったらリセット
        weather_fetched_today_g = False
        last_date_weather_checked_g = current_date
        logging.info(f"日付変更 ({current_date})。天気取得フラグ解除。")

    if current_hour == WEATHER_FETCH_HOUR and not weather_fetched_today_g:
        logging.info(f"{WEATHER_FETCH_HOUR}時台、天気情報更新試行。")
        condition, description, temp, error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION)
        weather_cache["data"] = {"condition": condition, "description": description, "temp_c": temp, "is_rain": (condition and condition.lower() == "rain")}
        weather_cache["error"] = error
        weather_cache["timestamp"] = current_time_unix # キャッシュ時刻更新
        if not error:
            weather_fetched_today_g = True # 成功したらフラグを立てる
    
    weather_data_to_display = weather_cache["data"] if weather_cache.get("data") else {}
    weather_data_to_display["error_message"] = weather_cache.get("error")


    # --- バス情報の取得とキャッシュ ---
    processed_buses = []
    bus_fetch_error = None
    app_state_message = "監視中" # デフォルト

    # 運行開始時刻判定
    is_before_service_hours = current_hour < BUS_SERVICE_START_HOUR or \
                              (current_hour == BUS_SERVICE_START_HOUR and current_dt_tokyo.minute < BUS_SERVICE_START_MINUTE)

    if is_before_service_hours:
        app_state_message = f"始発バス待機中 (～{BUS_SERVICE_START_HOUR:02d}:{BUS_SERVICE_START_MINUTE:02d}目安)"
        # 早朝はバス情報を取得しない（キャッシュも更新しない）
        # ただし、表示のために最後に成功したキャッシュを使うことはできる
        if bus_data_cache.get("data"): # 前日の最終情報などが残っていればそれを使う
            for bus_info_original in bus_data_cache["data"]:
                bus_info = bus_info_original.copy()
                # 残り時間は現在の時刻で再計算
                time_until_str, is_urgent = calculate_and_format_time_until(
                    bus_info.get(KEY_DEPARTURE_TIME, ""),
                    bus_info.get(KEY_STATUS_TEXT, ""),
                    current_dt_tokyo
                )
                bus_info[KEY_TIME_UNTIL] = time_until_str
                bus_info[KEY_IS_URGENT] = is_urgent
                processed_buses.append(bus_info)
        bus_fetch_error = bus_data_cache.get("error") # キャッシュされたエラーも表示

    elif current_time_unix - bus_data_cache.get("timestamp", 0) > BUS_DATA_CACHE_DURATION_SECONDS \
         or not bus_data_cache.get("data"): # キャッシュ切れまたはデータなし
        logging.info("バス情報を更新します (キャッシュ期限切れまたは初回)。")
        bus_result = fetch_simplified_bus_departure_times(FROM_STOP_NO, TO_STOP_NO)
        bus_data_cache["data"] = bus_result.get("buses", [])
        bus_data_cache["error"] = bus_result.get("error")
        bus_data_cache["timestamp"] = current_time_unix
    
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
    
    if not is_before_service_hours and not bus_fetch_error and not processed_buses:
        # 運行時間中のはずなのにバス情報がない場合は「終バス後」の可能性
        app_state_message = "情報なし/運行終了の可能性"


    return render_template('index.html',
                           from_stop=FROM_STOP_NAME,
                           to_stop=TO_STOP_NAME,
                           current_time_unix=int(current_time_unix * 1000), # JavaScript用にミリ秒で渡す
                           current_time_zone_offset_minutes=current_dt_tokyo.utcoffset().total_seconds() // 60, # JavaScriptのタイムゾーンオフセット用
                           weather_data=weather_data_to_display,
                           app_state_message=app_state_message,
                           buses_to_display=processed_buses,
                           bus_error_message=bus_fetch_error,
                           bus_last_updated_str=datetime.datetime.fromtimestamp(bus_data_cache["timestamp"], TOKYO_TZ).strftime('%H:%M:%S') if bus_data_cache["timestamp"] > 0 else "N/A"
                           )

if __name__ == '__main__':
    # App Engineデプロイ時はGunicornが起動するため、この部分はローカル開発用
    # ただし、App Engine Standardでは、このファイルが直接実行されるわけではない。
    # entrypoint (例: gunicorn -b :$PORT main:app) が app オブジェクトを見つけて起動する。
    # したがって、バックグラウンドタスクの起動はここではなく、
    # アプリケーションの初期化時や、別のメカニズム (例: Cron Service) で行うのが一般的。
    # このサンプルでは、簡単のためリクエストごとにデータを取得・キャッシュする形にしている。
    # 本格的なバックグラウンド更新はCron Service + Datastore/Memcacheを検討。
    
    # ローカルテスト用
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        logging.warning("ローカルテスト: Discord Webhook URL未設定")
    if not OPENWEATHERMAP_API_KEY or OPENWEATHERMAP_API_KEY == "YOUR_OPENWEATHERMAP_API_KEY_HERE":
        logging.warning("ローカルテスト: OpenWeatherMap APIキー未設定")

    app.run(host='127.0.0.1', port=8080, debug=True)
