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

# --- アプリケーション設定 ---
OPENWEATHERMAP_API_KEY = "28482976c81657127a816a47f53cc3d2"
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1375497603466395749/4QtOWTUk-_44xc8-RVmhm3imPatU4yiEuRj1NR1j5PryEkbik98A204uJ3069nye_GNI"

WEATHER_LOCATION = "Isehara,JP"
BASE_URL = "http://real.kanachu.jp/pc/displayapproachinfo"

ROUTE_DEFINITIONS = {
    "sanno_to_station": {
        "id": "sanno_to_station", "from_stop_no": "18137", "to_stop_no": "18100",
        "from_stop_name_short": "大学", "to_stop_name_short": "駅",
        "from_stop_name_full": "産業能率大学", "to_stop_name_full": "伊勢原駅北口",
        "group": "to_station_area"
    },
    "ishikura_to_station": {
        "id": "ishikura_to_station", "from_stop_no": "18124", "to_stop_no": "18100",
        "from_stop_name_short": "石倉", "to_stop_name_short": "駅",
        "from_stop_name_full": "石倉", "to_stop_name_full": "伊勢原駅北口",
        "group": "to_station_area"
    },
    "station_to_university_ishikura": {
        "id": "station_to_university_ishikura", "from_stop_no": "18100",
        "to_stop_no_sanno": "18137",
        "to_stop_no_ishikura": "18124",
        "from_stop_name_short": "駅", "to_stop_name_short": "大学方面",
        "from_stop_name_full": "伊勢原駅北口", "to_stop_name_full": "大学・石倉方面",
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
KEY_IS_OYAMA_FOR_ISHIKURA = "is_oyama_for_ishikura"
KEY_ORIGIN_STOP_NAME_SHORT = "origin_stop_name_short"
KEY_VEHICLE_NO = "vehicle_no"
KEY_DURATION = "duration_text"
KEY_DELAY_INFO = "delay_info"

app = Flask(__name__)

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(funcName)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

weather_cache = {"data": None, "timestamp": 0, "error": None}
bus_data_cache = {}
weather_fetched_today_g = False
last_date_weather_checked_g = None
WEATHER_CACHE_DURATION_SECONDS = 30 * 60
BUS_DATA_CACHE_DURATION_SECONDS = 10

def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL or "YOUR_DISCORD_WEBHOOK_URL_HERE" in DISCORD_WEBHOOK_URL:
        logging.warning("Discord Webhook URLが未設定またはプレースホルダーのため、通知は送信されません。")
        return
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
        logging.warning("OpenWeatherMap APIキーが未設定またはプレースホルダーのため、天気情報は取得できません。")
        return None, None, None, None, "APIキー未設定"
    api_url = "http://api.openweathermap.org/data/2.5/weather"
    params = {"q": location_query, "appid": api_key, "units": "metric", "lang": "ja"}
    try:
        response = requests.get(api_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("weather") and len(data["weather"]) > 0:
            main_condition = data["weather"][0].get("main"); description = data["weather"][0].get("description")
            condition_code = data["weather"][0].get("id"); temp = data.get("main", {}).get("temp")
            logging.info(f"天気情報取得成功 ({location_query}): {main_condition} ({description}), 気温: {temp}°C, Code: {condition_code}")
            weather_fetched_today_g = True
            return main_condition, description, temp, condition_code, None
        logging.warning(f"天気APIからのレスポンス形式が不正です: {data}")
        return None, None, None, None, "APIレスポンス形式不正"
    except requests.exceptions.Timeout:
        logging.warning(f"天気情報取得タイムアウト ({location_query})")
        return None, None, None, None, "タイムアウト"
    except requests.exceptions.HTTPError as http_err:
        error_message = f"HTTPエラー {http_err.response.status_code}"
        if http_err.response.status_code == 401: error_message = "天気APIキーが無効か認証エラーです。"
        logging.error(f"天気情報取得HTTPエラー ({location_query}): {http_err}")
        return None, None, None, None, error_message
    except Exception as e:
        logging.exception(f"天気情報取得中に予期せぬエラー ({location_query})")
        return None, None, None, None, "予期せぬエラー"

def parse_bus_info_from_html(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    bus_departure_list = []
    bus_wrappers = soup.select('div.inner2.pa01 > div.wrap')
    for wrap_element in bus_wrappers:
        col01 = wrap_element.find('div', class_='col01')
        system_route_name = "不明"; destination_name = "不明"; via_info = "不明"; vehicle_no = None; duration_text = "不明"
        if col01:
            table_rows = col01.select('table.table01 tr')
            for row in table_rows:
                th_tag = row.find('th'); td_tag = row.find('td')
                if th_tag and td_tag:
                    th_text = th_tag.get_text(strip=True); span_point = td_tag.find('span', class_='point')
                    td_text = span_point.get_text(strip=True) if span_point else td_tag.get_text(strip=True)
                    if "系統" in th_text: system_route_name = "".join([s for s in span_point.stripped_strings if "バスルートを表示" not in s]).strip() if span_point else td_text
                    elif "行き先" in th_text: destination_name = td_text
                    elif "経由" in th_text: via_info = td_text
                    elif "車両番号" in th_text: vm = re.search(r'([いす盛おつひ平やまた])\s*(\d+)', td_text); vehicle_no = vm.group(0).strip().replace(" ","") if vm else td_text.split("※")[0].split("★")[0].split("Ｔ")[0].strip()
                    elif "所要時分" in th_text: duration_text = td_text.replace("（通常）","").strip()
        col02 = wrap_element.find('div', class_='col02')
        departure_time_display = "情報なし"; raw_status_text = ""; parsed_delay_info = None
        if col02:
            frameBox03 = col02.find('div', class_='frameBox03')
            if frameBox03:
                title01_element = frameBox03.find('p', class_='title01')
                if title01_element:
                    raw_status_text = title01_element.get_text(strip=True)
                    delay_match = re.search(r'(?:約)?(\d+\s*分(?:程度)?遅れ)', raw_status_text)
                    if delay_match: parsed_delay_info = delay_match.group(1)
                    elif "遅れて到着する見込み" in raw_status_text or "遅延が見込まれます" in raw_status_text:
                         if not parsed_delay_info : parsed_delay_info = "遅延"
                departure_stop_area = frameBox03.find('div', class_='placeArea01 departure')
                stop_specific_time = None
                if departure_stop_area:
                    notes_span = departure_stop_area.find('span', class_='notes')
                    if notes_span:
                        notes_text = notes_span.get_text(strip=True)
                        time_match_notes = re.search(r'（(\d{1,2}:\d{2})(?:着予定|発予定|通過予定)?）', notes_text)
                        if time_match_notes: stop_specific_time = time_match_notes.group(1); departure_time_display = f"{stop_specific_time}発"
                if not stop_specific_time and raw_status_text:
                    time_match_title = re.search(r'(\d{1,2}:\d{2})(?:発予定|ごろ発車|に発車|に通過)', raw_status_text)
                    if time_match_title: departure_time_display = f"{time_match_title.group(1)}発"
                    elif "まもなく発車" in raw_status_text or "まもなく到着" in raw_status_text: departure_time_display = "まもなく"
                    elif "出発しました" in raw_status_text or "通過しました" in raw_status_text: departure_time_display = "出発済み"
                    else: departure_time_display = "時刻不明"
                elif not stop_specific_time and not raw_status_text: departure_time_display = "時刻不明"
        bus_departure_list.append({
            KEY_DEPARTURE_TIME: departure_time_display.strip(), KEY_STATUS_TEXT: raw_status_text,
            KEY_SYSTEM_ROUTE_NAME: system_route_name, KEY_DESTINATION_NAME: destination_name,
            KEY_VIA_INFO: via_info, KEY_VEHICLE_NO: vehicle_no, KEY_DURATION: duration_text,
            KEY_DELAY_INFO: parsed_delay_info
        })
    return bus_departure_list

def calculate_and_format_time_until(departure_display_str, raw_status_text, current_dt_tokyo, delay_info=None):
    is_urgent = False; time_until_str_for_js_fallback = ""; seconds_until_departure = -1; departure_datetime_tokyo = None
    if raw_status_text:
        if "まもなく発車します" in raw_status_text or "まもなく到着" in raw_status_text:
            is_urgent = True; seconds_until_departure = 10
            time_match_soon = re.search(r'(\d{1,2}:\d{2})', departure_display_str)
            if time_match_soon:
                try:
                    h, m = map(int, time_match_soon.group(1).split(':'))
                    departure_datetime_tokyo = current_dt_tokyo.replace(hour=h, minute=m, second=0, microsecond=0)
                    if departure_datetime_tokyo < current_dt_tokyo and (current_dt_tokyo.hour >=20 and h <=5) : departure_datetime_tokyo += datetime.timedelta(days=1)
                except: pass
            return time_until_str_for_js_fallback, is_urgent, seconds_until_departure, departure_datetime_tokyo
        elif "出発しました" in raw_status_text or "通過しました" in raw_status_text or "発車済みの恐れあり" in departure_display_str :
            time_match_departed = re.search(r'(\d{1,2}:\d{2})', departure_display_str)
            if time_match_departed:
                 try: h,m = map(int, time_match_departed.group(1).split(':')); departure_datetime_tokyo = current_dt_tokyo.replace(hour=h, minute=m, second=0, microsecond=0)
                 except: pass
            return time_until_str_for_js_fallback, is_urgent, seconds_until_departure, departure_datetime_tokyo
    time_match_countdown = re.search(r'(\d{1,2}:\d{2})発', departure_display_str)
    if time_match_countdown:
        bus_time_str = time_match_countdown.group(1)
        try:
            bus_hour, bus_minute = map(int, bus_time_str.split(':'))
            departure_datetime_tokyo = current_dt_tokyo.replace(hour=bus_hour, minute=bus_minute, second=0, microsecond=0)
            if departure_datetime_tokyo < current_dt_tokyo and (current_dt_tokyo.hour >= 20 and bus_hour <= 5): departure_datetime_tokyo += datetime.timedelta(days=1)
            if departure_datetime_tokyo >= current_dt_tokyo:
                delta = departure_datetime_tokyo - current_dt_tokyo; seconds_until_departure = int(delta.total_seconds())
                if seconds_until_departure <= 180 : is_urgent = True
                if delay_info and not ("まもなく発車します" in raw_status_text or "まもなく到着" in raw_status_text) : is_urgent = False
        except ValueError: logging.warning(f"時刻パースエラー: {bus_time_str} from {departure_display_str}")
        except Exception as e: logging.exception(f"カウントダウン計算エラー: {e}")
    return time_until_str_for_js_fallback, is_urgent, seconds_until_departure, departure_datetime_tokyo

def fetch_and_cache_bus_data(route_id, from_stop_no, to_stop_no_for_request, current_time_unix):
    if route_id not in bus_data_cache: bus_data_cache[route_id] = {"data": [], "timestamp": 0, "error": None, "data_valid": True}
    active_bus_cache = bus_data_cache[route_id]
    if current_time_unix - active_bus_cache.get("timestamp", 0) > BUS_DATA_CACHE_DURATION_SECONDS or not active_bus_cache.get("data_valid", False):
        logging.info(f"バス情報({route_id})を神奈中サイトから更新します。")
        params = {'fNO': from_stop_no};
        if to_stop_no_for_request: params['tNO'] = to_stop_no_for_request
        try:
            response = requests.get(BASE_URL, params=params, timeout=10); response.raise_for_status()
            html_content = response.content.decode('shift_jis', errors='replace'); parsed_buses = parse_bus_info_from_html(html_content)
            active_bus_cache["data"] = parsed_buses; active_bus_cache["error"] = None
        except requests.exceptions.Timeout:
            active_bus_cache["error"] = "バス情報取得タイムアウト"; active_bus_cache["data"] = []
            logging.warning(f"バス情報取得タイムアウト ({route_id})"); send_discord_notification(f"警告: バス情報取得がタイムアウトしました (ルートID: {route_id})")
        except requests.exceptions.RequestException as e:
            active_bus_cache["error"] = f"バス情報取得リクエストエラー: {e}"; active_bus_cache["data"] = []
            logging.error(f"バス情報取得リクエストエラー ({route_id}): {e}"); send_discord_notification(f"エラー: バス情報取得でリクエストエラーが発生しました (ルートID: {route_id}): {e}")
        except Exception as e:
            active_bus_cache["error"] = f"バス情報処理中に予期せぬエラー: {e}"; active_bus_cache["data"] = []
            logging.exception(f"バス情報処理エラー ({route_id})"); send_discord_notification(f"重大エラー: バス情報処理中に予期せぬエラーが発生しました (ルートID: {route_id}): {e}")
        active_bus_cache["timestamp"] = current_time_unix; active_bus_cache["data_valid"] = not active_bus_cache.get("error")
    return active_bus_cache.get("data", []), active_bus_cache.get("error")

@app.route('/')
def index():
    app.config['ACTIVE_DATA_FETCH_INTERVAL'] = BUS_DATA_CACHE_DURATION_SECONDS
    return render_template('index.html', config=app.config)

@app.route('/api/data')
def api_data():
    global weather_cache, weather_fetched_today_g, last_date_weather_checked_g
    current_dt_tokyo_for_api = datetime.datetime.now(TOKYO_TZ)
    try:
        requested_direction_group = request.args.get('direction_group', 'to_station_area')
        all_routes_bus_data = {}; current_time_unix = time.time()
        combined_errors_for_group_api = []

        if requested_direction_group == 'to_station_area':
            processed_buses_for_display_group = []
            latest_bus_update_time_for_group = 0
            sanno_vehicle_numbers = set()

            sanno_buses_raw, sanno_error = fetch_and_cache_bus_data("sanno_to_station", ROUTE_DEFINITIONS["sanno_to_station"]["from_stop_no"], ROUTE_DEFINITIONS["sanno_to_station"]["to_stop_no"], current_time_unix)
            if sanno_error: combined_errors_for_group_api.append(f"大学発: {sanno_error}")
            latest_bus_update_time_for_group = max(latest_bus_update_time_for_group, bus_data_cache.get("sanno_to_station", {}).get("timestamp", 0))

            for bus_info_original in sanno_buses_raw:
                bus_info = bus_info_original.copy()
                time_until_str, is_urgent, seconds_until, departure_dt = calculate_and_format_time_until(bus_info.get(KEY_DEPARTURE_TIME, "情報なし"), bus_info.get(KEY_STATUS_TEXT, ""), current_dt_tokyo_for_api, bus_info.get(KEY_DELAY_INFO))
                bus_info.update({ KEY_TIME_UNTIL: time_until_str, KEY_IS_URGENT: is_urgent, KEY_SECONDS_UNTIL_DEPARTURE: seconds_until, KEY_DEPARTURE_TIME_ISO: departure_dt.isoformat() if departure_dt else None, KEY_ORIGIN_STOP_NAME_SHORT: ROUTE_DEFINITIONS["sanno_to_station"]["from_stop_name_short"]})
                processed_buses_for_display_group.append(bus_info)
                if bus_info.get(KEY_VEHICLE_NO): sanno_vehicle_numbers.add(bus_info.get(KEY_VEHICLE_NO))

            ishikura_buses_raw, ishikura_error = fetch_and_cache_bus_data("ishikura_to_station", ROUTE_DEFINITIONS["ishikura_to_station"]["from_stop_no"], ROUTE_DEFINITIONS["ishikura_to_station"]["to_stop_no"], current_time_unix)
            if ishikura_error: combined_errors_for_group_api.append(f"石倉発: {ishikura_error}")
            latest_bus_update_time_for_group = max(latest_bus_update_time_for_group, bus_data_cache.get("ishikura_to_station", {}).get("timestamp", 0))

            for bus_info_original in ishikura_buses_raw:
                vehicle_no = bus_info_original.get(KEY_VEHICLE_NO)
                if vehicle_no and vehicle_no in sanno_vehicle_numbers:
                    logging.info(f"車両番号 {vehicle_no} は大学発と重複するため、石倉発の情報としてはスキップします。")
                    continue
                bus_info = bus_info_original.copy()
                time_until_str, is_urgent, seconds_until, departure_dt = calculate_and_format_time_until(bus_info.get(KEY_DEPARTURE_TIME, "情報なし"), bus_info.get(KEY_STATUS_TEXT, ""), current_dt_tokyo_for_api, bus_info.get(KEY_DELAY_INFO))
                bus_info.update({KEY_TIME_UNTIL: time_until_str, KEY_IS_URGENT: is_urgent, KEY_SECONDS_UNTIL_DEPARTURE: seconds_until, KEY_DEPARTURE_TIME_ISO: departure_dt.isoformat() if departure_dt else None, KEY_ORIGIN_STOP_NAME_SHORT: ROUTE_DEFINITIONS["ishikura_to_station"]["from_stop_name_short"]})
                processed_buses_for_display_group.append(bus_info)
            
            processed_buses_for_display_group.sort(key=lambda b: (b[KEY_SECONDS_UNTIL_DEPARTURE] == -1, b[KEY_SECONDS_UNTIL_DEPARTURE] if b[KEY_SECONDS_UNTIL_DEPARTURE] != -1 else float('inf')))
            all_routes_bus_data['to_station_combined'] = {
                "from_stop_name": "大学・石倉発", "to_stop_name": "駅",
                "buses_to_display": processed_buses_for_display_group[:MAX_BUSES_TO_FETCH],
                "bus_error_message": "、".join(combined_errors_for_group_api) if combined_errors_for_group_api else None,
                "bus_last_updated_str": datetime.datetime.fromtimestamp(latest_bus_update_time_for_group, TOKYO_TZ).strftime('%H:%M:%S') if latest_bus_update_time_for_group > 0 else "N/A",
            }
        elif requested_direction_group == 'to_university_area':
            route_id = "station_to_university_ishikura"; route_config = ROUTE_DEFINITIONS[route_id]; processed_buses_for_display_group = []; latest_bus_update_time_for_group = 0
            buses_raw, error = fetch_and_cache_bus_data(route_id, route_config["from_stop_no"], route_config["to_stop_no_ishikura"], current_time_unix)
            if error: combined_errors_for_group_api.append(f"駅発 ({route_config['from_stop_name_full']}): {error}")
            latest_bus_update_time_for_group = bus_data_cache.get(route_id, {}).get("timestamp", 0)
            for bus_info_original in buses_raw:
                bus_info = bus_info_original.copy()
                time_until_str, is_urgent, seconds_until, departure_dt = calculate_and_format_time_until(bus_info.get(KEY_DEPARTURE_TIME, "情報なし"), bus_info.get(KEY_STATUS_TEXT, ""), current_dt_tokyo_for_api, bus_info.get(KEY_DELAY_INFO))
                bus_info.update({KEY_TIME_UNTIL: time_until_str, KEY_IS_URGENT: is_urgent, KEY_SECONDS_UNTIL_DEPARTURE: seconds_until, KEY_DEPARTURE_TIME_ISO: departure_dt.isoformat() if departure_dt else None, KEY_ORIGIN_STOP_NAME_SHORT: route_config["from_stop_name_short"]})
                dest_name = bus_info.get(KEY_DESTINATION_NAME, "").strip(); is_ishikura_stop_only = False; is_oyama_for_ishikura = False
                if dest_name == "石倉": is_ishikura_stop_only = True
                elif "大山ケーブル" in dest_name: is_oyama_for_ishikura = True
                bus_info[KEY_IS_ISHIKURA_STOP_ONLY] = is_ishikura_stop_only; bus_info[KEY_IS_OYAMA_FOR_ISHIKURA] = is_oyama_for_ishikura
                processed_buses_for_display_group.append(bus_info)
            all_routes_bus_data[route_id] = {
                "from_stop_name": route_config["from_stop_name_short"], "to_stop_name": route_config["to_stop_name_short"],
                "buses_to_display": processed_buses_for_display_group[:MAX_BUSES_TO_FETCH],
                "bus_error_message": "、".join(combined_errors_for_group_api) if combined_errors_for_group_api else None,
                "bus_last_updated_str": datetime.datetime.fromtimestamp(latest_bus_update_time_for_group, TOKYO_TZ).strftime('%H:%M:%S') if latest_bus_update_time_for_group > 0 else "N/A",
            }
        
        weather_data_to_display = {}
        try:
            if last_date_weather_checked_g != current_dt_tokyo_for_api.date(): weather_fetched_today_g = False; last_date_weather_checked_g = current_dt_tokyo_for_api.date(); logging.info(f"日付変更 ({current_dt_tokyo_for_api.date()})。天気取得フラグ解除。")
            if (current_dt_tokyo_for_api.hour == WEATHER_FETCH_HOUR and not weather_fetched_today_g) or (not weather_cache.get("data") and not weather_cache.get("error")):
                logging.info(f"{WEATHER_FETCH_HOUR}時台の天気情報更新、または初回取得試行。")
                condition, description, temp, condition_code, error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION)
                if error: send_discord_notification(f"警告: 天気情報の取得に失敗しました - {error}")
                weather_cache["data"] = {"condition": condition, "description": description, "temp_c": temp, "condition_code": condition_code, "is_rain": (condition and "rain" in condition.lower()) if condition else False}
                weather_cache["error"] = error; weather_cache["timestamp"] = current_time_unix
                if not error and condition: weather_fetched_today_g = True
            weather_data_to_display = weather_cache.get("data", {}); weather_data_to_display["error_message"] = weather_cache.get("error")
        except Exception as e:
            logging.exception("天気情報処理中に予期せぬエラー (api_data内)"); send_discord_notification(f"重大エラー: 天気情報処理中に予期せぬエラーが発生しました (api_data内): {e}")
            weather_data_to_display = {"error_message": "天気情報処理エラー"}

        system_healthy = True; system_warning = False
        if combined_errors_for_group_api: system_healthy = False; logging.warning(f"システム状態: バス情報取得エラーのため不健康 - {', '.join(combined_errors_for_group_api)}")
        current_weather_error = weather_data_to_display.get("error_message")
        if current_weather_error:
            if "APIキー" in current_weather_error or "認証エラー" in current_weather_error : system_healthy = False
            else: system_warning = True
            logging.warning(f"システム状態: 天気情報に問題 - {current_weather_error}")
        return jsonify(weather_data=weather_data_to_display, routes_bus_data=all_routes_bus_data, system_status={'healthy': system_healthy, 'warning': system_warning})
    except Exception as e:
        logging.exception("APIデータ処理中に致命的なエラーが発生しました。")
        send_discord_notification(f"致命的エラー: /api/data エンドポイントで予期せぬエラーが発生しました: {e}")
        return jsonify(error="サーバー内部エラーが発生しました。", weather_data={"error_message": "サーバーエラー"}, routes_bus_data={}, system_status={'healthy': False, 'warning': True}), 500

if __name__ == '__main__':
    send_discord_notification(f"バス接近情報アプリケーションが起動しました。(環境: {os.environ.get('K_SERVICE', 'ローカル')})")
    if "YOUR_OPENWEATHERMAP_API_KEY_HERE" in OPENWEATHERMAP_API_KEY:
        logging.warning("OpenWeatherMap APIキーが設定されていません。"); send_discord_notification("警告: OpenWeatherMap APIキーが設定されていません。天気情報が利用できません。")
    else:
        logging.info("OpenWeatherMap APIキーが設定されています。")
        _, _, _, _, weather_init_error = get_weather_info(OPENWEATHERMAP_API_KEY, WEATHER_LOCATION)
        if weather_init_error and ("APIキーが無効" in weather_init_error or "認証エラー" in weather_init_error):
            send_discord_notification(f"重大エラー: 設定されているOpenWeatherMap APIキーが無効です: {weather_init_error}")
    if "YOUR_DISCORD_WEBHOOK_URL_HERE" in DISCORD_WEBHOOK_URL: logging.warning("Discord Webhook URLが設定されていません。")
    else: logging.info("Discord Webhook URLが設定されています。")
    logging.info("アプリケーションを開始します。")
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
