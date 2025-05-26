from flask import Flask, render_template
import requests
from bs4 import BeautifulSoup
import json
import re
import datetime
import pytz
import os
import logging
import time

# --- 設定 (環境変数から読み込むことを推奨) ---
OPENWEATHERMAP_API_KEY = os.environ.get("OPENWEATHERMAP_API_KEY", "YOUR_OPENWEATHERMAP_API_KEY_HERE")
# DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "YOUR_DISCORD_WEBHOOK_URL_HERE") # 必要に応じて
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

# (キー名、ANSIエスケープシーケンスはコンソール版から流用、ただしHTMLではCSSで色付け)
KEY_DEPARTURE_TIME = "departure_time"
KEY_STATUS_TEXT = "status_text"
KEY_TIME_UNTIL = "time_until_departure"
KEY_IS_URGENT = "is_urgent"

app = Flask(__name__)

# --- ロギング設定 (App Engineは標準でロギングを提供) ---
# logging.basicConfig(level=logging.INFO) # ローカルテスト用
# App Engine上では print() や logging が Cloud Logging に出力されます。

# === ここに既存の関数群を移植 ===
# get_weather_info, fetch_simplified_bus_departure_times, 
# calculate_and_format_time_until をコピー＆ペースト。
# print() は app.logger.info() や logging.info() に置き換える。
# Discord通知関数も必要なら移植。

# (関数の内容は長いため、前の回答を参照してここに配置してください)
# 例:
def get_weather_info(api_key, location_query):
    # ... (前のコードから get_weather_info の内容をここに) ...
    # 戻り値は (main_condition, description, error_message) とする
    if not api_key or api_key == "YOUR_OPENWEATHERMAP_API_KEY_HERE":
        logging.warning("OpenWeatherMap APIキーが未設定。")
        return None, None, "APIキー未設定"
    # ... (実際のAPI呼び出しとエラー処理) ...
    # ダミーデータ
    # return "Clear", "快晴", None
    # 実際のAPI呼び出し
    api_url = "http://api.openweathermap.org/data/2.5/weather"
    params = {"q": location_query, "appid": api_key, "units": "metric", "lang": "ja"}
    try:
        response = requests.get(api_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("weather") and len(data["weather"]) > 0:
            main_condition = data["weather"][0].get("main")
            description = data["weather"][0].get("description")
            # temp = data.get("main", {}).get("temp") # 必要なら温度も
            return main_condition, description, None
        return None, None, "APIレスポンス形式不正"
    except Exception as e:
        logging.error(f"天気情報取得エラー: {e}")
        return None, None, str(e)


def fetch_simplified_bus_departure_times(from_stop_no, to_stop_no):
    # ... (前のコードから fetch_simplified_bus_departure_times の内容をここに) ...
    # 戻り値は {"buses": list, "error": str_or_none} とする
    # ダミーデータ
    # return {"buses": [{"departure_time": "10:00発 (予定通り)", "status_text": "10:00発予定 予定通り発車します"}], "error": None}
    # (実際の処理をここに記述)
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
        logging.error(f"バス情報取得エラー: {e}")
        return {"buses": [], "error": str(e)}


def calculate_and_format_time_until(departure_str, status_text_raw, current_dt_tokyo):
    # ... (前のコードから calculate_and_format_time_until の内容をここに) ...
    # 戻り値は (time_until_str, is_urgent) とする
    # ダミーデータ
    # return "あと10分", False
    # (実際の処理をここに記述)
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


# --- グローバル変数 (App Engineではリクエストごとに状態がリセットされるため、Datastoreなどを検討) ---
# 今回は簡略化のため、リクエストごとに天気とバス情報を取得する形にします。
# より高度な実装では、これらの情報をDatastoreにキャッシュし、Cronジョブで更新します。
weather_cache = {"data": None, "timestamp": 0, "error": None}
bus_data_cache = {"data": [], "timestamp": 0, "error": None}

WEATHER_CACHE_DURATION_SECONDS = 30 * 60 # 天気情報のキャッシュ期間 (30分)
BUS_DATA_CACHE_DURATION_SECONDS = 10    # バス情報のキャッシュ期間 (10秒)


@app.route('/')
def index():
    global weather_cache, bus_data_cache # キャッシュを読み書きするため

    current_dt_tokyo = datetime.datetime.now(TOKYO_TZ)
    current_time_unix = time.time()

    # --- 天気情報の取得とキャッシュ ---
    weather_data_to_display = {}
    # 9時台にキャッシュを更新するロジック (またはキャッシュが古い場合)
    if current_dt_tokyo.hour == WEATHER_FETCH_HOUR:
        if current_time_unix - weather_cache["timestamp"] > WEATHER_CACHE_DURATION_SECONDS or not weather_cache["data"]:
            logging.info("天気情報を更新します (9時台またはキャッシュ期限切れ)。")
            condition, description, error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION)
            weather_cache["data"] = {"condition": condition, "description": description, "is_rain": (condition and condition.lower() == "rain")}
            weather_cache["error"] = error
            weather_cache["timestamp"] = current_time_unix
    
    weather_data_to_display = weather_cache["data"] if weather_cache["data"] else {}
    weather_data_to_display["error_message"] = weather_cache["error"]


    # --- バス情報の取得とキャッシュ ---
    # App Engine Standardでは、リクエストごとに処理が走るため、
    # 毎リクエストでバス情報を取得するか、キャッシュを利用します。
    # ここでは簡単な時間ベースのキャッシュを実装します。
    # より堅牢な実装には Datastore + Cron を使います。
    
    processed_buses = []
    bus_fetch_error = None

    if current_time_unix - bus_data_cache["timestamp"] > BUS_DATA_CACHE_DURATION_SECONDS or not bus_data_cache["data"]:
        logging.info("バス情報を更新します (キャッシュ期限切れまたは初回)。")
        bus_result = fetch_simplified_bus_departure_times(FROM_STOP_NO, TO_STOP_NO)
        bus_data_cache["data"] = bus_result.get("buses", [])
        bus_data_cache["error"] = bus_result.get("error")
        bus_data_cache["timestamp"] = current_time_unix

    bus_fetch_error = bus_data_cache["error"]
    if bus_data_cache["data"]:
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

    # --- アプリケーションの状態判定 (簡略版) ---
    app_state_message = "監視中"
    current_hour = current_dt_tokyo.hour
    current_minute = current_dt_tokyo.minute

    if current_hour < BUS_SERVICE_START_HOUR or \
       (current_hour == BUS_SERVICE_START_HOUR and current_minute < BUS_SERVICE_START_MINUTE):
        app_state_message = f"始発バス待機中 (～{BUS_SERVICE_START_HOUR:02d}:{BUS_SERVICE_START_MINUTE:02d}目安)"
        if not bus_fetch_error and not processed_buses: # 早朝でまだバスがない場合
             pass # processed_buses が空なのでHTML側で「情報なし」と表示される
    elif bus_fetch_error:
        app_state_message = "エラー発生中"
    elif not processed_buses:
        # 終バス後の可能性。より正確には、fetchで情報が取れなかった回数をカウントするが、
        # App Engine Standardではリクエストごとに状態がリセットされるため難しい。
        # ここでは単純に「情報なし」とする。Cronで状態を管理するのが望ましい。
        app_state_message = "情報なし/運行終了の可能性"


    return render_template('index.html',
                           from_stop=FROM_STOP_NAME,
                           to_stop=TO_STOP_NAME,
                           current_time_str=current_dt_tokyo.strftime('%Y-%m-%d %H:%M:%S %Z'),
                           weather_data=weather_data_to_display,
                           app_state_message=app_state_message,
                           buses_to_display=processed_buses,
                           bus_error_message=bus_fetch_error,
                           bus_last_updated_str=datetime.datetime.fromtimestamp(bus_data_cache["timestamp"], TOKYO_TZ).strftime('%H:%M:%S') if bus_data_cache["timestamp"] > 0 else "N/A"
                           )

if __name__ == '__main__':
    # ローカルでの開発サーバー起動 (App Engineデプロイ時はGunicornなどが使われる)
    app.run(host='127.0.0.1', port=8080, debug=True)
