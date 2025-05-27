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

OPENWEATHERMAP_API_KEY = "28482976c81657127a816a47f53cc3d2"
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1375497603466395749/4QtOWTUk-_44xc8-RVmhm3imPatU4yiEuRj1NR1j5PryEkbik98A204uJ3069nye_GNI"

WEATHER_LOCATION = "Isehara,JP"
BASE_URL = "http://real.kanachu.jp/pc/displayapproachinfo"

ROUTE_DEFINITIONS = {
    "sanno_to_station": {
        "id": "sanno_to_station",
        "from_stop_no": "18137",
        "to_stop_no": "18100",
        "from_stop_name_short": "大学",
        "to_stop_name_short": "駅",
        "from_stop_name_full": "産業能率大学",
        "to_stop_name_full": "伊勢原駅北口",
        "group": "to_station_area"
    },
    "ishikura_to_station": {
        "id": "ishikura_to_station",
        "from_stop_no": "18124",
        "to_stop_no": "18100",
        "from_stop_name_short": "石倉",
        "to_stop_name_short": "駅",
        "from_stop_name_full": "石倉",
        "to_stop_name_full": "伊勢原駅北口",
        "group": "to_station_area"
    },
    "station_to_university_ishikura": {
        "id": "station_to_university_ishikura",
        "from_stop_no": "18100", # 伊勢原駅北口の代表番号 (産能大/石倉行き共通の乗り場と仮定)
        "to_stop_no_sanno": "18137",
        "to_stop_no_ishikura": "18124", # 神奈中APIへのtNOとして使用
        "from_stop_name_short": "駅",
        "to_stop_name_short": "大学・石倉", # UIの方向ボタン表示用
        "from_stop_name_full": "伊勢原駅北口",
        "to_stop_name_full": "大学・石倉方面",
        "group": "to_university_area"
    }
}

MAX_BUSES_TO_FETCH = 10
WEATHER_FETCH_HOUR = 9
BUS_SERVICE_START_HOUR = 6
BUS_SERVICE_START_MINUTE = 20

TOKYO_TZ = pytz.timezone('Asia/Tokyo')

KEY_DEPARTURE_TIME = "departure_time"
KEY_STATUS_TEXT = "status_text"
KEY_TIME_UNTIL = "time_until_departure"
KEY_IS_URGENT = "is_urgent"
KEY_DEPARTURE_TIME_ISO = "departure_time_iso"
KEY_SECONDS_UNTIL_DEPARTURE = "seconds_until_departure"
KEY_SYSTEM_ROUTE_NAME = "system_route_name"
KEY_DESTINATION_NAME = "destination_name"
KEY_VIA_INFO = "via_info"
KEY_IS_ISHIKURA_STOP_ONLY = "is_ishikura_stop_only"
KEY_ORIGIN_STOP_NAME_SHORT = "origin_stop_name_short"

app = Flask(__name__)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(funcName)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

weather_cache = {"data": None, "timestamp": 0, "error": None}
bus_data_cache = {}

weather_fetched_today_g = False
last_date_weather_checked_g = None

WEATHER_CACHE_DURATION_SECONDS = 30 * 60
BUS_DATA_CACHE_DURATION_SECONDS = 10

def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL or "YOUR_DISCORD_WEBHOOK_URL_HERE" in DISCORD_WEBHOOK_URL:
        logging.warning("Discord Webhook URLが未設定またはプレースホルダーのままのため、通知は送信されません。")
        return
    # logging.info("Discord Webhook URLが設定されています。") # 本番では不要な場合もある
    payload = {"content": message, "username": "バス情報チェッカー"}
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
    if not api_key or "YOUR_OPENWEATHERMAP_API_KEY_HERE" in api_key:
        logging.warning("OpenWeatherMap APIキーが未設定またはプレースホルダーのままです。")
        return None, None, None, None, "APIキー未設定"
    # logging.info("OpenWeatherMap APIキーが設定されています。") # 本番では不要な場合もある
    api_url = "http://api.openweathermap.org/data/2.5/weather"
    params = {"q": location_query, "appid": api_key, "units": "metric", "lang": "ja"}
    try:
        response = requests.get(api_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("weather") and len(data["weather"]) > 0:
            main_condition = data["weather"][0].get("main")
            description = data["weather"][0].get("description")
            condition_code = data["weather"][0].get("id")
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

def fetch_simplified_bus_departure_times(from_stop_no, to_stop_no_for_request):
    params = {'fNO': from_stop_no}
    if to_stop_no_for_request:
        params['tNO'] = to_stop_no_for_request

    bus_departure_list = []
    try:
        response = requests.get(BASE_URL, params=params, timeout=10)
        response.raise_for_status()
        html_content = response.content.decode('shift_jis', errors='replace')
        soup = BeautifulSoup(html_content, 'html.parser')
        
        bus_wrappers = soup.select('div.inner2.pa01 > div.wrap')

        for wrap_element in bus_wrappers:
            if len(bus_departure_list) >= MAX_BUSES_TO_FETCH:
                break

            col01 = wrap_element.find('div', class_='col01')
            system_route_name = "不明"
            destination_name = "不明"
            via_info = "不明"

            if col01:
                table_rows = col01.select('table.table01 tr')
                for row in table_rows:
                    th_tag = row.find('th')
                    td_tag = row.find('td')
                    if th_tag and td_tag:
                        th_text = th_tag.get_text(strip=True)
                        # td内のspan.pointがあればそのテキストを、なければtd全体のテキストを取得
                        span_point = td_tag.find('span', class_='point')
                        td_text = span_point.get_text(strip=True) if span_point else td_tag.get_text(strip=True)
                        
                        if "系統" in th_text:
                            # "バスルートを表示" のimgタグとその前の空白文字を除去する可能性を考慮
                            if span_point: # span.point があれば、その中のテキストのみを対象とする
                                img_tag = span_point.find('img', alt="バスルートを表示")
                                if img_tag:
                                    # imgタグとその前の空白の可能性があるAタグなどを除去
                                    # より確実なのは、span.point の子要素を調べてテキストノードだけを結合
                                    text_parts = [s for s in span_point.stripped_strings if "バスルートを表示" not in s]
                                    system_route_name = "".join(text_parts).strip()
                                else:
                                    system_route_name = td_text # 通常のテキスト
                            else:
                                system_route_name = td_text
                        elif "行き先" in th_text:
                            destination_name = td_text
                        elif "経由" in th_text:
                            via_info = td_text
            
            col02 = wrap_element.find('div', class_='col02')
            title_text_raw = "情報なし" # 発車時刻・状況
            if col02:
                frameBox03 = col02.find('div', class_='frameBox03')
                if frameBox03:
                    title_element = frameBox03.find('p', class_='title01')
                    if title_element:
                        title_text_raw = title_element.get_text(strip=True)

            departure_time_str = None
            if "まもなく発車します" in title_text_raw or "まもなく到着" in title_text_raw:
                departure_time_str = "まもなく発車します"
            elif "通過しました" in title_text_raw or "出発しました" in title_text_raw:
                departure_time_str = "出発しました"
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
                elif time_part:
                    departure_time_str = f"{time_part}発"
            
            if departure_time_str:
                bus_departure_list.append({
                    KEY_DEPARTURE_TIME: departure_time_str,
                    KEY_STATUS_TEXT: title_text_raw,
                    KEY_SYSTEM_ROUTE_NAME: system_route_name,
                    KEY_DESTINATION_NAME: destination_name,
                    KEY_VIA_INFO: via_info
                })
        return {"buses": bus_departure_list, "error": None}
    except requests.exceptions.Timeout:
        error_msg = "バス情報取得タイムアウト"
        logging.warning(error_msg)
        return {"buses": [], "error": error_msg}
    except requests.exceptions.RequestException as e:
        error_msg = f"バス情報取得リクエストエラー: {e}"
        logging.error(error_msg)
        return {"buses": [], "error": error_msg}
    except Exception as e:
        error_msg = f"バス情報取得中に予期せぬエラー: {e}"
        logging.exception(error_msg)
        return {"buses": [], "error": error_msg}

def calculate_and_format_time_until(departure_str, status_text_raw, current_dt_tokyo):
    is_urgent = False
    time_until_str = ""
    seconds_until = -1
    departure_datetime_tokyo = None
    if "まもなく発車します" in departure_str:
        time_until_str = "まもなく"
        is_urgent = True
        seconds_until = 10
    elif "出発しました" in departure_str:
        time_until_str = "出発済み"
    else:
        match = re.search(r'(\d{1,2}:\d{2})発', departure_str)
        if not match:
            if "(予定通り)" in departure_str: time_until_str = ""
            elif "(遅延可能性あり)" in departure_str: time_until_str = "遅延可能性あり"
            elif "(予定)" in departure_str: time_until_str = "予定"
            else: time_until_str = ""
            return time_until_str, is_urgent, seconds_until, departure_datetime_tokyo
        bus_time_str = match.group(1)
        try:
            bus_hour, bus_minute = map(int, bus_time_str.split(':'))
            bus_dt_today_tokyo = current_dt_tokyo.replace(hour=bus_hour, minute=bus_minute, second=0, microsecond=0)
            departure_datetime_tokyo = bus_dt_today_tokyo
            if bus_dt_today_tokyo < current_dt_tokyo and \
               (current_dt_tokyo.hour >= 20 and bus_hour <= 5):
                bus_dt_today_tokyo += datetime.timedelta(days=1)
                departure_datetime_tokyo = bus_dt_today_tokyo
            if bus_dt_today_tokyo < current_dt_tokyo:
                if "予定通り発車します" not in status_text_raw and \
                   "通過しました" not in status_text_raw and \
                   "出発しました" not in status_text_raw and \
                   "まもなく発車します" not in status_text_raw :
                    time_until_str = "発車済みのおそれあり"
                else:
                    time_until_str = "出発済み"
            else:
                delta = bus_dt_today_tokyo - current_dt_tokyo
                total_seconds = int(delta.total_seconds())
                seconds_until = total_seconds
                if total_seconds <= 15 :
                    time_until_str = "まもなく発車"
                    is_urgent = True
                elif total_seconds <= 180:
                    minutes_until = total_seconds // 60
                    seconds_rem = total_seconds % 60
                    time_until_str = f"あと{minutes_until}分{seconds_rem}秒"
                    is_urgent = True
                else:
                    minutes_until = total_seconds // 60
                    time_until_str = f"あと{minutes_until}分"
        except ValueError:
            time_until_str = f"時刻形式エラー ({departure_str})"
        except Exception:
            time_until_str = "計算エラー"
            logging.exception(f"calculate_and_format_time_untilでエラー: dep={departure_str}, status={status_text_raw}")
    if "遅延可能性あり" in departure_str and time_until_str and "あと" in time_until_str:
        is_urgent = False
    if "まもなく" in time_until_str or "まもなく発車します" in departure_str:
        is_urgent = True
    return time_until_str, is_urgent, seconds_until, departure_datetime_tokyo

@app.route('/')
def index():
    app.config['ACTIVE_DATA_FETCH_INTERVAL'] = BUS_DATA_CACHE_DURATION_SECONDS
    return render_template('index.html', config=app.config)

@app.route('/api/data')
def api_data():
    global weather_cache, bus_data_cache, weather_fetched_today_g, last_date_weather_checked_g
    requested_direction_group = request.args.get('direction_group', 'to_station_area')
    all_routes_bus_data = {}
    current_dt_tokyo = datetime.datetime.now(TOKYO_TZ)
    current_time_unix = time.time()

    if requested_direction_group == 'to_station_area':
        merged_buses_to_station = []
        combined_errors = []
        latest_bus_update_time_station_area = 0
        route_ids_for_station_area = [
            route_id for route_id, route_data in ROUTE_DEFINITIONS.items() 
            if route_data["group"] == 'to_station_area'
        ]
        for route_id in route_ids_for_station_area:
            route_config = ROUTE_DEFINITIONS[route_id]
            from_stop_no_current = route_config["from_stop_no"]
            to_stop_no_current = route_config["to_stop_no"]
            if route_id not in bus_data_cache:
                bus_data_cache[route_id] = {"data": [], "timestamp": 0, "error": None, "data_valid": True}
            active_bus_cache = bus_data_cache[route_id]
            if current_time_unix - active_bus_cache.get("timestamp", 0) > BUS_DATA_CACHE_DURATION_SECONDS \
               or not active_bus_cache.get("data_valid", False):
                logging.info(f"バス情報({route_id})を更新します。")
                bus_result = fetch_simplified_bus_departure_times(from_stop_no_current, to_stop_no_current)
                active_bus_cache["data"] = bus_result.get("buses", [])
                active_bus_cache["error"] = bus_result.get("error")
                active_bus_cache["timestamp"] = current_time_unix
                active_bus_cache["data_valid"] = not bus_result.get("error")
            if active_bus_cache.get("error"):
                combined_errors.append(f"{route_config['from_stop_name_short']}発: {active_bus_cache.get('error')}")
            if active_bus_cache.get("timestamp", 0) > latest_bus_update_time_station_area:
                latest_bus_update_time_station_area = active_bus_cache.get("timestamp", 0)
            if active_bus_cache.get("data"):
                for bus_info_original in active_bus_cache["data"]:
                    bus_info = bus_info_original.copy()
                    time_until_str, is_urgent, seconds_until, departure_dt = calculate_and_format_time_until(
                        bus_info.get(KEY_DEPARTURE_TIME, ""), bus_info.get(KEY_STATUS_TEXT, ""), current_dt_tokyo)
                    bus_info[KEY_TIME_UNTIL] = time_until_str; bus_info[KEY_IS_URGENT] = is_urgent
                    bus_info[KEY_SECONDS_UNTIL_DEPARTURE] = seconds_until
                    bus_info[KEY_DEPARTURE_TIME_ISO] = departure_dt.isoformat() if departure_dt else None
                    bus_info[KEY_ORIGIN_STOP_NAME_SHORT] = route_config["from_stop_name_short"]
                    merged_buses_to_station.append(bus_info)
        merged_buses_to_station.sort(key=lambda b: (b[KEY_SECONDS_UNTIL_DEPARTURE] == -1, b[KEY_SECONDS_UNTIL_DEPARTURE]))
        final_buses_for_station_area = merged_buses_to_station[:MAX_BUSES_TO_FETCH]
        all_routes_bus_data['to_station_combined'] = {
            "from_stop_name": "大学・石倉", 
            "to_stop_name": "駅",
            "buses_to_display": final_buses_for_station_area,
            "bus_error_message": "、".join(combined_errors) if combined_errors else None,
            "bus_last_updated_str": datetime.datetime.fromtimestamp(latest_bus_update_time_station_area, TOKYO_TZ).strftime('%H:%M:%S') if latest_bus_update_time_station_area > 0 else "N/A",
        }
    elif requested_direction_group == 'to_university_area':
        route_id = "station_to_university_ishikura"
        route_config = ROUTE_DEFINITIONS[route_id]
        from_stop_no_current = route_config["from_stop_no"]
        to_stop_no_for_api_request = route_config["to_stop_no_ishikura"]
        if route_id not in bus_data_cache:
            bus_data_cache[route_id] = {"data": [], "timestamp": 0, "error": None, "data_valid": True}
        active_bus_cache = bus_data_cache[route_id]
        processed_buses_for_route = []
        bus_fetch_error_for_route = None
        if current_time_unix - active_bus_cache.get("timestamp", 0) > BUS_DATA_CACHE_DURATION_SECONDS \
           or not active_bus_cache.get("data_valid", False):
            logging.info(f"バス情報({route_id})を更新します。")
            bus_result = fetch_simplified_bus_departure_times(from_stop_no_current, to_stop_no_for_api_request)
            active_bus_cache["data"] = bus_result.get("buses", [])
            active_bus_cache["error"] = bus_result.get("error")
            active_bus_cache["timestamp"] = current_time_unix
            active_bus_cache["data_valid"] = not bus_result.get("error")
        bus_fetch_error_for_route = active_bus_cache.get("error")
        if active_bus_cache.get("data"):
            for bus_info_original in active_bus_cache["data"]:
                bus_info = bus_info_original.copy()
                time_until_str, is_urgent, seconds_until, departure_dt = calculate_and_format_time_until(
                    bus_info.get(KEY_DEPARTURE_TIME, ""), bus_info.get(KEY_STATUS_TEXT, ""), current_dt_tokyo)
                bus_info[KEY_TIME_UNTIL] = time_until_str; bus_info[KEY_IS_URGENT] = is_urgent
                bus_info[KEY_SECONDS_UNTIL_DEPARTURE] = seconds_until
                bus_info[KEY_DEPARTURE_TIME_ISO] = departure_dt.isoformat() if departure_dt else None
                dest_name = bus_info.get(KEY_DESTINATION_NAME, "")
                is_ishikura_stop_only = False
                if "石倉" == dest_name.strip(): # 行き先が「石倉」のみの場合
                    is_ishikura_stop_only = True
                elif "産業能率大学" in dest_name: # 産能大行きなら石倉止まりではない
                    is_ishikura_stop_only = False
                # 他の行き先（例：大山ケーブル）は石倉止まりではない
                elif "大山ケーブル" in dest_name:
                     is_ishikura_stop_only = False

                bus_info[KEY_IS_ISHIKURA_STOP_ONLY] = is_ishikura_stop_only
                processed_buses_for_route.append(bus_info)
        all_routes_bus_data[route_id] = {
            "from_stop_name": route_config["from_stop_name_short"],
            "to_stop_name": route_config["to_stop_name_short"],
            "from_stop_name_full": route_config["from_stop_name_full"],
            "to_stop_name_full": route_config.get("to_stop_name_full", route_config["to_stop_name_short"]),
            "buses_to_display": processed_buses_for_route[:MAX_BUSES_TO_FETCH],
            "bus_error_message": bus_fetch_error_for_route,
            "bus_last_updated_str": datetime.datetime.fromtimestamp(active_bus_cache.get("timestamp", 0), TOKYO_TZ).strftime('%H:%M:%S') if active_bus_cache.get("timestamp", 0) > 0 else "N/A",
        }

    weather_data_to_display = {}
    if last_date_weather_checked_g != current_dt_tokyo.date():
        weather_fetched_today_g = False
        last_date_weather_checked_g = current_dt_tokyo.date()
        logging.info(f"日付変更 ({current_dt_tokyo.date()})。天気取得フラグ解除。")
    if (current_dt_tokyo.hour == WEATHER_FETCH_HOUR and not weather_fetched_today_g) or \
       (not weather_cache.get("data") and not weather_cache.get("error")):
        logging.info(f"{WEATHER_FETCH_HOUR}時台の天気情報更新、または初回取得試行。")
        condition, description, temp, condition_code, error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION)
        weather_cache["data"] = {
            "condition": condition, "description": description, "temp_c": temp,
            "condition_code": condition_code, "is_rain": (condition and "rain" in condition.lower()) if condition else False
        }
        weather_cache["error"] = error
        weather_cache["timestamp"] = current_time_unix
        if not error and condition: weather_fetched_today_g = True
    weather_data_to_display = weather_cache.get("data", {})
    weather_data_to_display["error_message"] = weather_cache.get("error")
    system_healthy = True
    system_warning = False
    active_group_routes = [rid for rid, rdata in ROUTE_DEFINITIONS.items() if rdata["group"] == requested_direction_group]
    if requested_direction_group == 'to_station_area':
        if 'to_station_combined' in all_routes_bus_data and all_routes_bus_data['to_station_combined']["bus_error_message"]:
            system_healthy = False
            logging.warning(f"システム状態: バス情報取得エラー(駅方面) - {all_routes_bus_data['to_station_combined']['bus_error_message']}")
    else: # to_university_area (現在は1ルートのみだが将来的には複数になる可能性)
        for route_id_check in active_group_routes:
             if route_id_check in all_routes_bus_data and all_routes_bus_data[route_id_check]["bus_error_message"]:
                system_healthy = False
                logging.warning(f"システム状態: バス情報取得エラー(大学方面) - {all_routes_bus_data[route_id_check]['bus_error_message']}")
                break
    current_weather_error = weather_data_to_display.get("error_message")
    if current_weather_error:
        if "APIキー" in current_weather_error or "認証エラー" in current_weather_error :
            system_healthy = False
        else:
            system_warning = True
        logging.warning(f"システム状態: 天気情報に問題 - {current_weather_error}")
    return jsonify(
        weather_data=weather_data_to_display,
        routes_bus_data=all_routes_bus_data,
        system_status={'healthy': system_healthy, 'warning': system_warning}
    )

if __name__ == '__main__':
    if "YOUR_OPENWEATHERMAP_API_KEY_HERE" in OPENWEATHERMAP_API_KEY:
        logging.warning("ローカルテスト: OpenWeatherMap APIキーが設定されていません。")
    else:
        logging.info("ローカルテスト: OpenWeatherMap APIキーが設定されています。")
    if "YOUR_DISCORD_WEBHOOK_URL_HERE" in DISCORD_WEBHOOK_URL:
        logging.warning("ローカルテスト: Discord Webhook URLが設定されていません。")
    else:
        logging.info("ローカルテスト: Discord Webhook URLが設定されています。")
    logging.info("アプリケーションを開始します。")
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
