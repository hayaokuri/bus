import requests
from bs4 import BeautifulSoup
import json
import re # 正規表現モジュール
import datetime
import time
import os
import pytz # タイムゾーン対応のため追加
import logging # エラーログ記録のため追加
from flask import Flask, render_template # Flask関連をインポート
import threading # バックグラウンド処理用 (App EngineではCron推奨だが、ローカル/一部環境用)

# --- 設定項目 ---
OPENWEATHERMAP_API_KEY = os.environ.get("OPENWEATHERMAP_API_KEY", "YOUR_OPENWEATHERMAP_API_KEY_HERE")
WEATHER_LOCATION = "Isehara,JP"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "YOUR_DISCORD_WEBHOOK_URL_HERE")
DISCORD_USERNAME = "バス情報チェッカー"
BASE_URL = "http://real.kanachu.jp/pc/displayapproachinfo"
FROM_STOP_NO = "18137"
TO_STOP_NO = "18100"
FROM_STOP_NAME = "産業能率大学"
TO_STOP_NAME = "伊勢原駅北口"
MAX_BUSES_TO_FETCH = 5

# --- 動作制御関連の定数 ---
STATE_INITIALIZING = -1
STATE_WAITING_FOR_FIRST_BUS = 0
STATE_ACTIVE_MONITORING = 1
STATE_WAITING_FOR_SERVICE_RESUME = 2

ACTIVE_DATA_FETCH_INTERVAL = 10
WAITING_DATA_FETCH_INTERVAL = 15 * 60
RETRY_DATA_FETCH_INTERVAL = 1 * 60
# DISPLAY_REFRESH_INTERVAL_SECONDS はHTML側のmeta refreshやJSで制御
NO_BUS_INFO_THRESHOLD = 3
WEATHER_FETCH_HOUR = 9
BUS_SERVICE_START_HOUR = 6
BUS_SERVICE_START_MINUTE = 20

# --- 内部処理用のキー名 ---
KEY_DEPARTURE_TIME = "departure_time"
KEY_STATUS_TEXT = "status_text"
KEY_TIME_UNTIL = "time_until_departure"
KEY_IS_URGENT = "is_urgent"

# --- グローバル変数 (App Engine/Cloud Runではリクエストごとに状態がリセットされるため、Datastoreなどを検討) ---
data_lock = threading.Lock() # グローバル変数アクセス時のロック用
weather_cache = {"data": None, "timestamp": 0, "error": None}
bus_data_cache = {"data": [], "timestamp": 0, "error": None}
weather_fetched_today_g = False
last_date_weather_checked_g = None

TOKYO_TZ = pytz.timezone('Asia/Tokyo')

# --- ロギング設定 ---
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(funcName)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

app = Flask(__name__) # Flaskアプリケーションインスタンスの作成
app.logger.setLevel(logging.INFO) # FlaskのロガーもINFOレベルに

# --- 関数定義 ---
def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        logging.warning("Discord Webhook URLが未設定のため、通知は送信されません。")
        return
    payload = {"content": message, "username": DISCORD_USERNAME}
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
        logging.warning("OpenWeatherMap APIキーが未設定。天気取得不可。")
        return None, None, None, "APIキー未設定" # main, description, temp, error

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
            weather_fetched_today_g = True
            return main_condition, description, temp, None # 成功
        else:
            logging.warning(f"天気情報取得失敗 ({location_query}): APIレスポンス形式不正 {data}")
            return None, None, None, "APIレスポンス形式不正"
    except requests.exceptions.Timeout:
        logging.warning(f"天気情報取得タイムアウト ({location_query})")
        return None, None, None, "タイムアウト"
    except requests.exceptions.HTTPError as http_err:
        error_message_detail = f"HTTPエラー {http_err.response.status_code}"
        if http_err.response.status_code == 401:
             error_message_detail = "APIキーが無効か認証エラーです。"
             send_discord_notification(f"🚨 **天気APIエラー:** {error_message_detail} 確認してください。")
        else:
             send_discord_notification(f"🛑 **天気API HTTPエラー ({location_query}):** ステータスコード {http_err.response.status_code}")
        logging.error(f"天気情報取得HTTPエラー ({location_query}): {http_err}")
        return None, None, None, error_message_detail
    except Exception as e:
        error_msg_exception = f"予期せぬエラー: {e}"
        logging.exception(f"天気情報取得中に予期せぬエラーが発生しました ({location_query})。")
        send_discord_notification(f"🚨 **天気API取得中の予期せぬエラー ({location_query}):** {e}")
        return None, None, None, error_msg_exception

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
            logging.info("バス情報が含まれる主要エリアが見つかりませんでした。")
            return {"buses": [], "error": None}
        bus_info_headings = main_content_area.find_all('h3', class_='heading3')
        if not bus_info_headings:
            logging.info("バス情報ブロックのヘッダーが見つかりませんでした。")
            return {"buses": [], "error": None}
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
    except requests.exceptions.Timeout:
        error_msg = "バス情報サイトへのリクエストがタイムアウトしました。"
        logging.warning(error_msg)
        send_discord_notification(f"⚠️ **バス情報取得エラー:** {error_msg}")
        return {"error": error_msg, "buses": []}
    except requests.exceptions.RequestException as e:
        error_msg = f"バス情報サイトへのHTTPリクエストエラー: {e}"
        logging.error(error_msg)
        send_discord_notification(f"🛑 **バス情報取得エラー:** {error_msg}")
        return {"error": error_msg, "buses": []}
    except Exception as e:
        error_msg = f"バス情報取得・解析中に予期せぬエラー: {e}"
        logging.exception("バス情報取得・解析中に予期せぬエラーが発生しました。")
        send_discord_notification(f"🚨 **バス情報取得中の予期せぬエラー:** {error_msg}")
        return {"error": error_msg, "buses": []}

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
        except ValueError:
            time_until_str = f"時刻形式エラー ({departure_str})"
            logging.warning(f"残り時間計算中の時刻形式エラー: {departure_str}")
        except Exception as e:
            time_until_str = "計算エラー"
            logging.error(f"残り時間計算中に予期せぬエラー: {e} (入力: {departure_str})")
    return time_until_str, is_urgent

@app.route('/')
def index():
    # グローバル変数を参照・更新する可能性があるので宣言
    global weather_cache, bus_data_cache, weather_fetched_today_g, last_date_weather_checked_g
    global current_state, no_bus_info_consecutive_count, next_data_fetch_due_time
    global last_fetch_error, last_fetched_buses # 状態を保持するグローバル変数

    current_dt_tokyo = datetime.datetime.now(TOKYO_TZ)
    current_time_unix = time.time()
    current_hour = current_dt_tokyo.hour
    current_minute = current_dt_tokyo.minute
    current_date = current_dt_tokyo.date()

    # --- 日付変更時の天気情報取得フラグリセット ---
    if last_date_weather_checked_g != current_date:
        with data_lock: # グローバル変数アクセスはロック
            weather_fetched_today_g = False
            last_date_weather_checked_g = current_date
        logging.info(f"日付変更 ({current_date})。天気取得フラグ解除。")

    # --- 天気情報取得ロジック (9時台に1回のみ、またはキャッシュが古い場合) ---
    # App Engine Standard 環境では、リクエストごとにこのコードが実行されるため、
    # 天気情報のキャッシュが有効に働く。
    # 9時台、かつ本日未取得、またはキャッシュが古い場合に更新。
    should_fetch_weather = False
    if current_hour == WEATHER_FETCH_HOUR and not weather_fetched_today_g:
        should_fetch_weather = True
    
    # キャッシュの有効期限も考慮 (例: 30分)
    # この部分は、Cronジョブで定期的に天気情報を取得しDatastoreに保存する方が堅牢
    # ここではリクエストベースのキャッシュ
    if current_time_unix - weather_cache.get("timestamp", 0) > (30 * 60): # 30分キャッシュ
         if not (current_hour == WEATHER_FETCH_HOUR and weather_fetched_today_g) : # 9時台に取得済みなら再取得しない
             logging.info("天気情報キャッシュ期限切れ、または9時台でないため、天気情報を試行。")
             should_fetch_weather = True # 9時台以外でもキャッシュが切れていたら取得試行

    if should_fetch_weather:
        logging.info(f"天気情報更新試行。現在{current_hour}時、本日取得済:{weather_fetched_today_g}")
        condition, description, temp, weather_error_msg = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION)
        with data_lock:
            weather_cache["data"] = {"condition": condition, "description": description, "temp_c": temp, "is_rain": (condition and condition.lower() == "rain")}
            weather_cache["error"] = weather_error_msg
            weather_cache["timestamp"] = current_time_unix
            if not weather_error_msg and current_hour == WEATHER_FETCH_HOUR : # 9時台の成功時のみフラグを立てる
                weather_fetched_today_g = True
    
    weather_data_to_display = weather_cache.get("data", {})
    weather_data_to_display["error_message"] = weather_cache.get("error")


    # --- バス情報取得ロジック (状態とキャッシュに基づいて) ---
    # App Engine Standard はステートレスなので、current_state や last_fetched_buses は
    # リクエスト間で保持されない。これらをDatastore等で永続化し、Cronで更新するのが理想。
    # ここでは、リクエストごとにバス情報を取得する（キャッシュは短時間のみ有効）。
    
    current_app_state_display = STATE_ACTIVE_MONITORING # デフォルト
    
    is_before_service_hours_now = current_hour < BUS_SERVICE_START_HOUR or \
                                 (current_hour == BUS_SERVICE_START_HOUR and current_minute < BUS_SERVICE_START_MINUTE)

    if is_before_service_hours_now:
        current_app_state_display = STATE_WAITING_FOR_FIRST_BUS
        # 早朝はバス情報を強制的に取得しない (キャッシュされた前日の最終情報を使うことはあり得る)
        # last_fetched_buses はそのまま (前回のキャッシュ)
        # last_fetch_error もそのまま
    elif current_time_unix - bus_data_cache.get("timestamp", 0) > BUS_DATA_CACHE_DURATION_SECONDS \
         or not bus_data_cache.get("data") or bus_data_cache.get("error"): # キャッシュ切れ、データなし、または前回エラー
        logging.info("バス情報更新 (キャッシュ期限切れ/データなし/前回エラー)。")
        bus_result = fetch_simplified_bus_departure_times(FROM_STOP_NO, TO_STOP_NO)
        with data_lock:
            bus_data_cache["data"] = bus_result.get("buses", [])
            bus_data_cache["error"] = bus_result.get("error")
            bus_data_cache["timestamp"] = current_time_unix
            
            # 状態管理の簡略化 (リクエストベース)
            last_fetched_buses = bus_data_cache["data"]
            last_fetch_error = bus_data_cache["error"]
            if last_fetch_error:
                no_bus_info_consecutive_count +=1 # カウントはするが、永続化しないと意味が薄い
            elif not last_fetched_buses:
                no_bus_info_consecutive_count +=1
            else:
                no_bus_info_consecutive_count = 0

            if no_bus_info_consecutive_count >= NO_BUS_INFO_THRESHOLD:
                current_app_state_display = STATE_WAITING_FOR_SERVICE_RESUME
            else:
                current_app_state_display = STATE_ACTIVE_MONITORING

    else: # キャッシュを使う
        with data_lock:
            last_fetched_buses = bus_data_cache.get("data", [])
            last_fetch_error = bus_data_cache.get("error")
        # current_app_state_display は ACTIVE のまま (キャッシュヒットなので)
        # ただし、キャッシュされたデータが空だった場合の判定は別途必要
        if not last_fetched_buses and not last_fetch_error : # キャッシュが空でエラーもなかった
             current_app_state_display = STATE_WAITING_FOR_SERVICE_RESUME # または適切な状態


    # --- 表示用バス情報処理 ---
    processed_buses_for_display = []
    if last_fetched_buses:
        for bus_info_original in last_fetched_buses:
            bus_info = bus_info_original.copy()
            time_until_str, is_urgent_flag = calculate_and_format_time_until(
                bus_info.get(KEY_DEPARTURE_TIME, ""),
                bus_info.get(KEY_STATUS_TEXT, ""),
                current_dt_tokyo
            )
            bus_info[KEY_TIME_UNTIL] = time_until_str
            bus_info[KEY_IS_URGENT] = is_urgent_flag
            processed_buses_for_display.append(bus_info)

    # --- アプリケーション状態メッセージの決定 (最終版) ---
    app_state_message_display = "監視中"
    if current_app_state_display == STATE_WAITING_FOR_FIRST_BUS:
        app_state_message_display = f"始発バス待機中 (～{BUS_SERVICE_START_HOUR:02d}:{BUS_SERVICE_START_MINUTE:02d}目安)"
    elif current_app_state_display == STATE_WAITING_FOR_SERVICE_RESUME:
        app_state_message_display = "運行再開待機中 (終バス後など)"
    elif last_fetch_error:
        app_state_message_display = "エラー発生中"
    elif not processed_buses_for_display and not is_before_service_hours_now : # 運行時間中のはずなのにバスがない
        app_state_message_display = "情報なし/運行終了の可能性"


    return render_template('index.html',
                           from_stop=FROM_STOP_NAME,
                           to_stop=TO_STOP_NAME,
                           current_time_str=current_dt_tokyo.strftime('%Y-%m-%d %H:%M:%S %Z'),
                           weather_data=weather_data_to_display,
                           app_state_message=app_state_message_display,
                           buses_to_display=processed_buses_for_display,
                           bus_error_message=last_fetch_error,
                           bus_last_updated_str=datetime.datetime.fromtimestamp(bus_data_cache.get("timestamp",0), TOKYO_TZ).strftime('%H:%M:%S') if bus_data_cache.get("timestamp",0) > 0 else "N/A",
                           config_active_data_fetch_interval=ACTIVE_DATA_FETCH_INTERVAL 
                           )

if __name__ == '__main__':
    logging.info("ローカル開発サーバーを起動します。")
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        logging.warning("ローカル: Discord Webhook URL未設定")
    if not OPENWEATHERMAP_API_KEY or OPENWEATHERMAP_API_KEY == "YOUR_OPENWEATHERMAP_API_KEY_HERE":
        logging.warning("ローカル: OpenWeatherMap APIキー未設定")
    
    port = int(os.environ.get("PORT", 8080))
    # 本番環境 (App Engine, Cloud Run) ではGunicornなどがこのappを起動する
    # 開発時は Flask の開発サーバーを使用
    app.run(host='0.0.0.0', port=port, debug=True) # debug=True は開発中のみ
