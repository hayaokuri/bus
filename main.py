from flask import Flask, render_template, jsonify # jsonify を追加
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
OPENWEATHERMAP_API_KEY = "28482976c81657127a816a47f53cc3d2"
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1375497603466395749/4QtOWTUk-_44xc8-RVmhm3imPatU4yiEuRj1NR1j5PryEkbik98A204uJ3069nye_GNI"
WEATHER_LOCATION = "Isehara,JP" #
BASE_URL = "http://real.kanachu.jp/pc/displayapproachinfo" #
FROM_STOP_NO = "18137" #
TO_STOP_NO = "18100" #
FROM_STOP_NAME = "産業能率大学" #
TO_STOP_NAME = "伊勢原駅北口" #
MAX_BUSES_TO_FETCH = 5 #
WEATHER_FETCH_HOUR = 9 #
BUS_SERVICE_START_HOUR = 6 #
BUS_SERVICE_START_MINUTE = 20 #

SERVICE_OPERATION_START_DATE = datetime.date(2024, 5, 1) #

TOKYO_TZ = pytz.timezone('Asia/Tokyo') #

KEY_DEPARTURE_TIME = "departure_time" #
KEY_STATUS_TEXT = "status_text" #
KEY_TIME_UNTIL = "time_until_departure" #
KEY_IS_URGENT = "is_urgent" #

app = Flask(__name__) #

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(funcName)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S') #

weather_cache = {"data": None, "timestamp": 0, "error": None} #
bus_data_cache = {"data": [], "timestamp": 0, "error": None, "data_valid": True} # data_valid を初期値に追加
weather_fetched_today_g = False #
last_date_weather_checked_g = None #

WEATHER_CACHE_DURATION_SECONDS = 30 * 60 #
BUS_DATA_CACHE_DURATION_SECONDS = 10 #

def send_discord_notification(message): #
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

def get_weather_info(api_key, location_query): #
    global weather_fetched_today_g
    if not api_key or api_key == "YOUR_OPENWEATHERMAP_API_KEY_HERE":
        logging.warning("OpenWeatherMap APIキーが未設定。")
        return None, None, None, None, "APIキー未設定"
    api_url = "http://api.openweathermap.org/data/2.5/weather"
    params = {"q": location_query, "appid": api_key, "units": "metric", "lang": "ja"}
    try:
        response = requests.get(api_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("weather") and len(data["weather"]) > 0:
            main_condition = data["weather"][0].get("main")
            description = data["weather"][0].get("description")
            condition_code = data["weather"][0].get("id") # 天候IDを取得
            temp = data.get("main", {}).get("temp")
            logging.info(f"天気情報取得成功 ({location_query}): {main_condition} ({description}), 気温: {temp}°C, Code: {condition_code}")
            weather_fetched_today_g = True
            return main_condition, description, temp, condition_code, None
        return None, None, None, None, "APIレスポンス形式不正"
    except requests.exceptions.Timeout:
        logging.warning(f"天気情報取得タイムアウト ({location_query})")
        return None, None, None, None, "タイムアウト"
    except requests.exceptions.HTTPError as http_err:
        error_message = f"HTTPエラー {http_err.response.status_code}"
        if http_err.response.status_code == 401:
             error_message = "APIキーが無効か認証エラーです。"
        logging.error(f"天気情報取得HTTPエラー ({location_query}): {http_err}")
        return None, None, None, None, error_message
    except Exception as e:
        logging.exception(f"天気情報取得中に予期せぬエラー ({location_query})")
        return None, None, None, None, f"予期せぬエラー"

def fetch_simplified_bus_departure_times(from_stop_no, to_stop_no): #
    params = {'fNO': from_stop_no, 'tNO': to_stop_no}
    bus_departure_list = []
    try:
        response = requests.get(BASE_URL, params=params, timeout=10)
        response.raise_for_status()
        html_content = response.content.decode('shift_jis', errors='replace')
        soup = BeautifulSoup(html_content, 'html.parser')
        main_content_area = soup.find('div', class_='inner2 pa01')
        if not main_content_area: return {"buses": [], "error": None} # エラーなしの場合は空リスト
        bus_info_headings = main_content_area.find_all('h3', class_='heading3')
        if not bus_info_headings: return {"buses": [], "error": None} # エラーなしの場合は空リスト

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

            # 時刻情報の抽出ロジックを改善
            if "まもなく発車します" in title_text_raw or "まもなく到着" in title_text_raw: # 「まもなく到着」も「まもなく」として扱う
                departure_time_str = "まもなく発車します" # フロントでの処理と合わせる
            elif "通過しました" in title_text_raw or "出発しました" in title_text_raw:
                departure_time_str = "出発しました" # フロントでの処理と合わせる
            else:
                match_time_candidate = re.search(r'(\d{1,2}:\d{2})発?', title_text_raw)
                time_part = None
                if match_time_candidate: time_part = match_time_candidate.group(1)

                if "予定通り発車します" in title_text_raw:
                    if time_part: departure_time_str = f"{time_part}発 (予定通り)"
                    else: departure_time_str = "状態不明 (予定通り情報あり)"
                elif "頃発車します" in title_text_raw: # 「頃」がある場合は遅延の可能性
                    if time_part: departure_time_str = f"{time_part}発 (遅延可能性あり)"
                    else: departure_time_str = "状態不明 (遅延情報あり)"
                elif "発予定" in title_text_raw: # 「発予定」はそのまま
                    if time_part: departure_time_str = f"{time_part}発 (予定)"
                    else: departure_time_str = "状態不明 (予定情報あり)"
                elif time_part: # 時刻のみの場合はそのまま
                    departure_time_str = f"{time_part}発"
                # else: departure_time_str = title_text_raw # 未知のパターンはそのまま保持（デバッグ用）

            if departure_time_str:
                bus_departure_list.append({KEY_DEPARTURE_TIME: departure_time_str, KEY_STATUS_TEXT: title_text_raw})
        return {"buses": bus_departure_list, "error": None}
    except requests.exceptions.Timeout:
        error_msg = "バス情報取得タイムアウト"
        logging.warning(error_msg)
        return {"buses": [], "error": error_msg}
    except requests.exceptions.RequestException as e: # より広範なリクエストエラーをキャッチ
        error_msg = f"バス情報取得リクエストエラー: {e}"
        logging.error(error_msg)
        return {"buses": [], "error": error_msg}
    except Exception as e:
        error_msg = f"バス情報取得中に予期せぬエラー: {e}"
        logging.exception(error_msg) # スタックトレースをログに出力
        return {"buses": [], "error": error_msg}

def calculate_and_format_time_until(departure_str, status_text_raw, current_dt_tokyo): #
    is_urgent = False
    time_until_str = ""

    if "まもなく発車します" in departure_str: # フロントの表記と合わせる
        time_until_str = "まもなく" # フロントではこれのみ表示
        is_urgent = True
    elif "出発しました" in departure_str: # フロントの表記と合わせる
        time_until_str = "出発済み"
    else:
        match = re.search(r'(\d{1,2}:\d{2})発', departure_str)
        if not match: # 時刻情報がない場合
            if "(予定通り)" in departure_str: time_until_str = "" # 「予定通り」などの補足情報のみ
            elif "(遅延可能性あり)" in departure_str: time_until_str = "遅延可能性あり"
            elif "(予定)" in departure_str: time_until_str = "予定"
            else: time_until_str = "" # 不明な場合は空
            return time_until_str, is_urgent # is_urgent は False のまま

        bus_time_str = match.group(1)
        try:
            bus_hour, bus_minute = map(int, bus_time_str.split(':'))
            # 現在の日付でバスの時刻オブジェクトを作成
            bus_dt_today_tokyo = current_dt_tokyo.replace(hour=bus_hour, minute=bus_minute, second=0, microsecond=0)

            # 深夜バスなど、日付をまたぐ場合の考慮 (例: 現在23時でバス時刻が0時台なら翌日扱い)
            if bus_dt_today_tokyo < current_dt_tokyo and \
               (current_dt_tokyo.hour >= 20 and bus_hour <= 5): # 夜8時以降でバス時刻が朝5時以前
                bus_dt_today_tokyo += datetime.timedelta(days=1)


            if bus_dt_today_tokyo < current_dt_tokyo:
                # 過去の時刻だが、「予定通り発車します」などがなければ「発車済みのおそれ」
                if "予定通り発車します" not in status_text_raw and \
                   "通過しました" not in status_text_raw and \
                   "出発しました" not in status_text_raw and \
                   "まもなく発車します" not in status_text_raw : # まもなくも除外
                    time_until_str = "発車済みのおそれあり"
                else:
                    time_until_str = "出発済み" # それ以外は出発済み
            else:
                delta = bus_dt_today_tokyo - current_dt_tokyo
                total_seconds = int(delta.total_seconds())

                if total_seconds <= 15 : # 15秒以内は「まもなく」
                    time_until_str = "まもなく発車" # フロントはこれのみ表示するので、ここでは詳細でも良い
                    is_urgent = True
                elif total_seconds <= 180: # 3分以内は緊急扱い
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
        except Exception: # より広範なエラーをキャッチ
            time_until_str = "計算エラー"
            logging.exception(f"calculate_and_format_time_untilでエラー: dep={departure_str}, status={status_text_raw}")


    # 緊急フラグの最終調整: "遅延可能性あり" の場合は緊急度を下げる (is_urgent=False)
    if "遅延可能性あり" in departure_str and time_until_str and "あと" in time_until_str: # 「あとX分」の形式で遅延の場合
        is_urgent = False # 遅延の場合は緊急ではない

    # 「まもなく」の場合は常に緊急
    if "まもなく" in time_until_str or "まもなく発車します" in departure_str:
        is_urgent = True

    return time_until_str, is_urgent

@app.route('/') #
def index():
    current_dt_tokyo = datetime.datetime.now(TOKYO_TZ)
    current_date_tokyo = current_dt_tokyo.date()
    uptime_days = (current_date_tokyo - SERVICE_OPERATION_START_DATE).days + 1

    app.config['ACTIVE_DATA_FETCH_INTERVAL'] = BUS_DATA_CACHE_DURATION_SECONDS

    return render_template('index.html',
                           from_stop=FROM_STOP_NAME,
                           to_stop=TO_STOP_NAME,
                           initial_uptime_days=uptime_days,
                           config=app.config
                           )

@app.route('/api/data') #
def api_data():
    global weather_cache, bus_data_cache, weather_fetched_today_g, last_date_weather_checked_g

    current_dt_tokyo = datetime.datetime.now(TOKYO_TZ)
    current_time_unix = time.time()
    current_hour = current_dt_tokyo.hour
    current_date = current_dt_tokyo.date()

    weather_data_to_display = {}
    if last_date_weather_checked_g != current_date:
        weather_fetched_today_g = False
        last_date_weather_checked_g = current_date
        logging.info(f"日付変更 ({current_date})。天気取得フラグ解除。")

    if (current_hour == WEATHER_FETCH_HOUR and not weather_fetched_today_g) or \
       (not weather_cache.get("data") and not weather_cache.get("error")): # データもエラーもない初回時も取得
        logging.info(f"{WEATHER_FETCH_HOUR}時台の天気情報更新、または初回取得試行。")
        condition, description, temp, condition_code, error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION)
        weather_cache["data"] = {
            "condition": condition,
            "description": description,
            "temp_c": temp,
            "condition_code": condition_code,
            "is_rain": (condition and "rain" in condition.lower()) if condition else False # conditionがNoneの場合を考慮
        }
        weather_cache["error"] = error
        weather_cache["timestamp"] = current_time_unix
        if not error and condition: # エラーがなく、conditionが得られた場合
            weather_fetched_today_g = True

    weather_data_to_display = weather_cache.get("data", {}) # dataがNoneの場合も考慮して空辞書
    # error_message はキャッシュの error を優先
    weather_data_to_display["error_message"] = weather_cache.get("error")


    processed_buses = []
    bus_fetch_error = None
    app_state_message = "監視中"

    is_before_service_hours = current_hour < BUS_SERVICE_START_HOUR or \
                              (current_hour == BUS_SERVICE_START_HOUR and current_dt_tokyo.minute < BUS_SERVICE_START_MINUTE)

    if is_before_service_hours:
        app_state_message = f"始発バス待機中 (～{BUS_SERVICE_START_HOUR:02d}:{BUS_SERVICE_START_MINUTE:02d}目安)"
        # サービス時間外でも、キャッシュされたバスデータを加工して表示する
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
        bus_fetch_error = bus_data_cache.get("error") # キャッシュのエラーも引き継ぐ
    # サービス時間内、またはキャッシュが切れているかデータが無効な場合
    elif current_time_unix - bus_data_cache.get("timestamp", 0) > BUS_DATA_CACHE_DURATION_SECONDS \
         or not bus_data_cache.get("data_valid", False): # data_valid が False なら更新
        logging.info("バス情報を更新します (キャッシュ期限切れまたは無効データ)。")
        bus_result = fetch_simplified_bus_departure_times(FROM_STOP_NO, TO_STOP_NO)
        bus_data_cache["data"] = bus_result.get("buses", [])
        bus_data_cache["error"] = bus_result.get("error")
        bus_data_cache["timestamp"] = current_time_unix
        bus_data_cache["data_valid"] = not bus_result.get("error") # エラーがなければ有効

    # キャッシュデータを使用する場合 (上記条件に当てはまらない場合)
    # この時点で bus_data_cache["data"] には最新かキャッシュされたデータが入っている
    bus_fetch_error = bus_data_cache.get("error") # 最新のエラー状態を反映
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
        # 深夜など、運行が終了している可能性がある時間帯のメッセージ
        if current_hour >= 23 or current_hour < BUS_SERVICE_START_HOUR -1 : # 23時以降、または始発1時間前まで
             app_state_message = "本日の運行は終了したか、まもなく開始です。"
        else:
             app_state_message = "現在表示できるバス情報はありません。"


    system_healthy = True
    system_warning = False

    if bus_fetch_error:
        system_healthy = False # バス情報取得エラーは主要機能のエラー
        logging.warning(f"システム状態: バス情報取得エラーのため不健康 - {bus_fetch_error}")

    current_weather_error = weather_data_to_display.get("error_message")
    if current_weather_error:
        if "APIキー" in current_weather_error or "認証エラー" in current_weather_error :
            system_healthy = False # APIキーエラーも主要機能の障害とみなす
            logging.warning(f"システム状態: 天気APIキー/認証エラーのため不健康 - {current_weather_error}")
        else: # それ以外の天気エラーは警告レベル
            system_warning = True
            logging.warning(f"システム状態: 天気情報取得に軽微な問題 - {current_weather_error}")

    # app_state_message に基づく健全性判断は、エラーが主要因であれば上記で判定済み
    # ここでは、エラーがないが特殊な状態（始発待機など）は正常範囲内とする
    if not system_healthy and app_state_message.startswith("始発バス待機中"):
        # 始発待機中のバスエラーは、まだサービスが開始していない可能性があるので警告レベルに留めることも検討
        # ただし、現状はバスエラーは unhealthy としているので、このまま
        pass


    current_date_tokyo_for_uptime = datetime.datetime.now(TOKYO_TZ).date()
    uptime_days = (current_date_tokyo_for_uptime - SERVICE_OPERATION_START_DATE).days + 1

    return jsonify(
        weather_data=weather_data_to_display,
        app_state_message=app_state_message,
        buses_to_display=processed_buses,
        bus_error_message=bus_fetch_error,
        bus_last_updated_str=datetime.datetime.fromtimestamp(bus_data_cache.get("timestamp", 0), TOKYO_TZ).strftime('%H:%M:%S') if bus_data_cache.get("timestamp", 0) > 0 else "N/A",
        system_status={'healthy': system_healthy, 'warning': system_warning},
        uptime_days=uptime_days,
        from_stop=FROM_STOP_NAME, #
        to_stop=TO_STOP_NAME #
    )

if __name__ == '__main__': #
    # 環境変数のチェック
    if not OPENWEATHERMAP_API_KEY or OPENWEATHERMAP_API_KEY == "YOUR_OPENWEATHERMAP_API_KEY_HERE":
        logging.warning("ローカルテスト: OpenWeatherMap APIキーが設定されていません。環境変数 'OPENWEATHERMAP_API_KEY' を設定してください。")
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        logging.warning("ローカルテスト: Discord Webhook URLが設定されていません。環境変数 'DISCORD_WEBHOOK_URL' を設定してください。")
    
    logging.info(f"SERVICE_OPERATION_START_DATE: {SERVICE_OPERATION_START_DATE}")
    # Gunicorn などで実行する場合は、app.run は呼び出されない想定
    # ローカルでの直接実行用
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=True) # host='0.0.0.0' で外部からのアクセスも許可（開発時）
