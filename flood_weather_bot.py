# -*- coding: utf-8 -*-
"""
钉钉防洪天气机器人：精简版
====================================================

功能：
1. 获取涪陵北、丰都、石柱、黄水、凉雾片区天气实况；
2. 获取未来七天天气；
3. 自动判断降雨、大风、防胀高温及综合防洪防风风险；
4. 生成五个片区近七天气温和降雨图示；
5. 通过钉钉自定义机器人推送 Markdown 消息；
6. 适合每天定时推送防洪防风防胀天气日报。

依赖：
pip install requests matplotlib

环境变量：
DINGTALK_TOKEN      钉钉机器人 access_token，或者完整 Webhook
DINGTALK_SECRET     钉钉机器人加签 secret；如果只设置关键词，可以为空
QWEATHER_KEY        和风天气 API Key
QWEATHER_HOST       和风天气控制台里的专属 API Host，例如 https://xxxx.qweatherapi.com
CHART_OUTPUT        七天气温降雨图输出路径，默认 weather_7d_chart.png
CHART_IMAGE_URL     可选，公网图片地址；配置后钉钉 Markdown 内直接显示图示
SEND_DINGTALK       是否推送钉钉，默认 true；设为 false 时只生成内容
REPORT_OUTPUT       可选，保存本次 Markdown 报告文本

PowerShell 本地测试示例：
cd D:\\dev\\flood_weather_bot

$env:DINGTALK_TOKEN="你的钉钉access_token或完整Webhook"
$env:DINGTALK_SECRET=""
$env:QWEATHER_KEY="你的和风天气KEY"
$env:QWEATHER_HOST="https://pj5xmfawjr.re.qweatherapi.com"

D:\\dev\\python\\python3.10.4\\python.exe D:\\dev\\flood_weather_bot\\flood_weather_bot.py
"""

import os
import re
import time
import json
import hmac
import base64
import hashlib
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import requests


# ====================================================
# 一、网络请求 Session
# ====================================================
# 禁用系统代理，避免 ProxyError / SSLError
SESSION = requests.Session()
SESSION.trust_env = False


# ====================================================
# 二、基础配置
# ====================================================

DINGTALK_TOKEN = os.getenv("DINGTALK_TOKEN", "").strip()
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET", "").strip()
QWEATHER_KEY = os.getenv("QWEATHER_KEY", "").strip()

QWEATHER_HOST = os.getenv("QWEATHER_HOST", "").strip().rstrip("/")

# 自动补全 https://
if QWEATHER_HOST and not QWEATHER_HOST.startswith(("http://", "https://")):
    QWEATHER_HOST = "https://" + QWEATHER_HOST

# 如果没有设置，给一个兜底值。
# 注意：新版和风天气一般需要使用控制台专属 API Host。
if not QWEATHER_HOST:
    QWEATHER_HOST = "https://devapi.qweather.com"

# 是否艾特所有人
AT_ALL = False

# 如果要艾特具体人员手机号，在这里填，例如：
# AT_MOBILES = ["13800000000", "13900000000"]
AT_MOBILES: List[str] = []

# 钉钉关键词建议设置为：防洪
REPORT_TITLE = "石柱车间防洪防风防胀日报"

# 请求超时时间
REQUEST_TIMEOUT = 20

# 七天气温降雨图输出路径。GitHub Actions 会上传这个文件为运行产物。
CHART_OUTPUT = os.getenv("CHART_OUTPUT", "weather_7d_chart.png").strip()

# 如果有可公网访问的图片地址，可填这里，钉钉 Markdown 会直接内嵌图片。
# 例如：https://example.com/weather_7d_chart.png
CHART_IMAGE_URL = os.getenv("CHART_IMAGE_URL", "").strip()

# GitHub Actions 可先生成报告和图片，提交图片后再单独推送已保存的报告。
SEND_DINGTALK = os.getenv("SEND_DINGTALK", "true").strip().lower() not in ("0", "false", "no")
REPORT_OUTPUT = os.getenv("REPORT_OUTPUT", "").strip()


@dataclass
class WeatherPoint:
    """
    天气观测点配置

    name:
        推送中显示的片区名称。
    query:
        和风天气查询用的位置。
        可以写城市名、区县名、乡镇名，也可以写经纬度，例如 "108.112,30.000"。
    """

    name: str
    query: str


# ====================================================
# 三、观测片区配置
# ====================================================
# 说明：
# 1. 名称按钉钉日报显示为“片区”；
# 2. 涪陵北、丰都、石柱、黄水当前按行政区域查询；
# 3. 凉雾片区使用凉雾站附近经纬度：经度108.8088306，纬度30.2616833；
# 4. 后续如果掌握涪陵北站、丰都站、石柱站、黄水站精确经纬度，建议全部改为经纬度。

WEATHER_POINTS: List[WeatherPoint] = [
    WeatherPoint(name="涪陵北片区", query="重庆涪陵"),
    WeatherPoint(name="丰都片区", query="重庆丰都"),
    WeatherPoint(name="石柱片区", query="重庆石柱"),
    WeatherPoint(name="黄水片区", query="重庆石柱黄水"),
    WeatherPoint(name="凉雾片区", query="108.8088306,30.2616833"),
]


# ====================================================
# 四、通用工具函数
# ====================================================

def now_str() -> str:
    """返回北京时间字符串。"""
    from datetime import timezone, timedelta
    beijing_time = datetime.now(timezone(timedelta(hours=8)))
    return beijing_time.strftime("%Y-%m-%d %H:%M:%S")


def extract_dingtalk_token(raw: str) -> str:
    """
    支持两种输入：
    1. 只填 access_token；
    2. 填完整 Webhook。

    例如：
    https://oapi.dingtalk.com/robot/send?access_token=xxxx
    会自动提取 xxxx。
    """
    raw = (raw or "").strip()

    if not raw:
        return ""

    if "access_token=" in raw:
        parsed = urllib.parse.urlparse(raw)
        query = urllib.parse.parse_qs(parsed.query)
        token_list = query.get("access_token", [])
        if token_list:
            return token_list[0].strip()

        match = re.search(r"access_token=([^&]+)", raw)
        if match:
            return match.group(1).strip()

    return raw


def require_env() -> None:
    """检查必要环境变量。"""
    missing = []

    if not extract_dingtalk_token(DINGTALK_TOKEN):
        missing.append("DINGTALK_TOKEN")

    if not QWEATHER_KEY:
        missing.append("QWEATHER_KEY")

    if not QWEATHER_HOST:
        missing.append("QWEATHER_HOST")

    if missing:
        raise RuntimeError(
            "缺少必要环境变量：{}\n"
            "请在 PowerShell 或 bat 文件中配置。".format(", ".join(missing))
        )


def safe_float(value, default: float = 0.0) -> float:
    """安全转 float。"""
    try:
        if value is None:
            return default

        value = str(value).strip()

        if value == "":
            return default

        return float(value)

    except Exception:
        return default


def safe_int(value, default: int = 0) -> int:
    """安全转 int。风力可能是 '3-4'，取后面的较大值。"""
    try:
        if value is None:
            return default

        value = str(value).replace("级", "").strip()

        if value == "":
            return default

        if "-" in value:
            return int(value.split("-")[-1])

        return int(value)

    except Exception:
        return default


def md_escape(text: str) -> str:
    """简单处理 Markdown 文本。"""
    if text is None:
        return "-"

    return str(text).replace("\r", "").strip()


def weather_icon(text: str) -> str:
    """根据天气现象返回图标。"""
    text = str(text or "")

    if "雷" in text:
        return "⛈️"
    if "暴雨" in text or "大雨" in text:
        return "🌧️"
    if "雨" in text:
        return "🌦️"
    if "雪" in text:
        return "❄️"
    if "晴" in text:
        return "☀️"
    if "云" in text:
        return "⛅"
    if "阴" in text:
        return "☁️"
    if "雾" in text or "霾" in text:
        return "🌫️"

    return "🌤️"


def rain_icon(level: str) -> str:
    """根据降雨风险返回图标。"""
    if level in ["特高", "高"]:
        return "🔴"
    if level == "较高":
        return "🟠"
    if level == "关注":
        return "🟡"
    if level == "一般":
        return "🔵"
    return "🟢"


def wind_icon(level: str) -> str:
    """根据大风风险返回图标。"""
    if level in ["高", "特高"]:
        return "🌪️🔴"
    if level == "较高":
        return "💨🟠"
    if level == "关注":
        return "💨🟡"
    return "🍃🟢"


def overall_risk_icon(risk: str) -> str:
    """综合风险图标。"""
    if risk in ["特高风险", "高风险"]:
        return "🔴"
    if risk == "较高风险":
        return "🟠"
    if risk == "关注":
        return "🟡"
    if risk == "一般":
        return "🔵"
    return "🟢"


def heat_level_by_temp(temp_max) -> Tuple[str, str, int]:
    """
    按未来 24 小时最高气温判断防胀高温预警。

    口径：超过 35℃发布黄色，超过 37℃发布橙色，超过 40℃发布红色。
    """
    temp = safe_float(temp_max, -999)

    if temp > 40:
        return "红色", "🔴", 3
    if temp > 37:
        return "橙色", "🟠", 2
    if temp > 35:
        return "黄色", "🟡", 1
    return "无预警", "🟢", 0


def heat_alert_text(temp_max) -> str:
    """返回防胀预警简短文本。"""
    level, icon, _ = heat_level_by_temp(temp_max)
    if level == "无预警":
        return "防胀无预警"
    return f"防胀{icon}{level}"


# ====================================================
# 五、钉钉机器人发送
# ====================================================

def build_dingtalk_url() -> str:
    """
    构造钉钉机器人 URL。

    支持：
    1. 只设置关键词，不用加签：DINGTALK_SECRET 为空；
    2. 关键词 + 加签：DINGTALK_SECRET 不为空。
    """
    token = extract_dingtalk_token(DINGTALK_TOKEN)

    if not token:
        raise RuntimeError("DINGTALK_TOKEN 为空或无法识别。")

    if DINGTALK_SECRET:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{DINGTALK_SECRET}"

        hmac_code = hmac.new(
            DINGTALK_SECRET.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()

        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))

        url = (
            "https://oapi.dingtalk.com/robot/send"
            f"?access_token={token}"
            f"&timestamp={timestamp}"
            f"&sign={sign}"
        )
    else:
        url = (
            "https://oapi.dingtalk.com/robot/send"
            f"?access_token={token}"
        )

    return url


def send_dingtalk_markdown(title: str, markdown_text: str) -> None:
    """发送钉钉 Markdown 消息。"""
    url = build_dingtalk_url()

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": markdown_text,
        },
        "at": {
            "atMobiles": AT_MOBILES,
            "isAtAll": AT_ALL,
        },
    }

    headers = {
        "Content-Type": "application/json;charset=utf-8"
    }

    response = SESSION.post(
        url,
        headers=headers,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=REQUEST_TIMEOUT,
    )

    try:
        result = response.json()
    except Exception:
        raise RuntimeError(
            f"钉钉接口返回非 JSON：HTTP {response.status_code}，{response.text}"
        )

    print("[DINGTALK]", result)

    if result.get("errcode") != 0:
        raise RuntimeError(f"钉钉推送失败：{result}")


# ====================================================
# 六、和风天气接口
# ====================================================

def qweather_get(path: str, params: Dict[str, str]) -> Dict:
    """
    请求和风天气 API。

    新版写法：
    1. API Host 使用控制台里的专属 Host；
    2. API KEY 放到请求头 X-QW-Api-Key。
    """
    params = dict(params)

    url = f"{QWEATHER_HOST}{path}"

    headers = {
        "X-QW-Api-Key": QWEATHER_KEY,
        "Accept-Encoding": "gzip",
    }

    response = SESSION.get(
        url,
        params=params,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )

    try:
        data = response.json()
    except Exception:
        raise RuntimeError(
            f"和风天气接口返回非 JSON：HTTP {response.status_code}，{response.text}"
        )

    if "error" in data:
        raise RuntimeError(
            f"和风天气接口异常：path={path}，params={params}，result={data}"
        )

    code = str(data.get("code", ""))

    if code != "200":
        raise RuntimeError(
            f"和风天气接口异常：path={path}，params={params}，result={data}"
        )

    return data


def get_location_id(query: str) -> Tuple[str, str, str]:
    """
    根据地名或经纬度获取 LocationID。

    返回：
    location_id, resolved_name, adm2
    """

    # 如果是经纬度格式，例如 "108.112,30.000"，直接作为 location 使用。
    if "," in query:
        return query, query, ""

    data = qweather_get(
        "/geo/v2/city/lookup",
        {
            "location": query,
            "range": "cn",
            "lang": "zh",
        },
    )

    locations = data.get("location", [])

    if not locations:
        raise RuntimeError(f"未查询到地点：{query}")

    first = locations[0]

    location_id = first.get("id", "")
    resolved_name = first.get("name", query)
    adm2 = first.get("adm2", "")

    if not location_id:
        raise RuntimeError(f"地点缺少 LocationID：{query}，返回：{first}")

    return location_id, resolved_name, adm2


def get_weather_now(location: str) -> Dict:
    """获取天气实况。"""
    return qweather_get(
        "/v7/weather/now",
        {
            "location": location,
            "lang": "zh",
            "unit": "m",
        },
    )


def get_weather_3d(location: str) -> Dict:
    """获取未来 3 天天气。"""
    return qweather_get(
        "/v7/weather/3d",
        {
            "location": location,
            "lang": "zh",
            "unit": "m",
        },
    )


def get_weather_7d(location: str) -> Dict:
    """获取未来 7 天天气。"""
    return qweather_get(
        "/v7/weather/7d",
        {
            "location": location,
            "lang": "zh",
            "unit": "m",
        },
    )


# ====================================================
# 七、风险判断逻辑
# ====================================================

def rain_level_by_daily_precip(precip_mm: float) -> Tuple[str, str]:
    """
    按日降雨量粗略判断风险。
    注意：不是气象部门正式预警标准，只用于防洪值守辅助研判。
    """
    if precip_mm >= 100:
        return "特高", "预计日降雨量达到或超过100mm，需高度警惕强降雨及次生灾害"
    elif precip_mm >= 50:
        return "高", "预计日降雨量达到或超过50mm，需重点盯控防洪重点处所"
    elif precip_mm >= 25:
        return "较高", "预计日降雨量达到或超过25mm，需加强雨前雨中雨后检查"
    elif precip_mm >= 10:
        return "关注", "有明显降雨，需关注排水不良及山区短时强降雨"
    elif precip_mm > 0:
        return "一般", "有弱降雨，保持正常关注"
    else:
        return "低", "暂无明显降雨"


def rain_level_by_now_precip(precip_mm: float) -> Tuple[str, str]:
    """按实况降雨量粗略判断。"""
    if precip_mm >= 20:
        return "高", "当前降雨较强，需立即关注现场排水、边坡、隧道口、桥涵"
    elif precip_mm >= 10:
        return "较高", "当前降雨明显，需加强值守和重点处所检查"
    elif precip_mm >= 2:
        return "关注", "当前有降雨，需关注短时增强"
    elif precip_mm > 0:
        return "一般", "当前有弱降雨"
    else:
        return "低", "当前暂无明显降雨"


def wind_level_by_scale(wind_scale: str) -> Tuple[str, str]:
    """按风力等级判断风险。"""
    scale = safe_int(wind_scale, 0)

    if scale >= 8:
        return "高", "风力达到8级及以上，需关注异物侵限、树木倒伏、临边设施"
    elif scale >= 6:
        return "较高", "风力达到6级及以上，需关注大风影响"
    elif scale >= 4:
        return "关注", "风力较明显，注意现场作业和异物风险"
    else:
        return "低", "暂无明显大风风险"


def risk_score(rain_level: str, wind_level: str) -> int:
    """综合风险分值。"""
    mapping = {
        "低": 0,
        "一般": 1,
        "关注": 2,
        "较高": 3,
        "高": 4,
        "特高": 5,
    }

    return max(mapping.get(rain_level, 0), mapping.get(wind_level, 0))


def risk_label_by_score(score: int) -> str:
    """综合风险文字。"""
    if score >= 5:
        return "特高风险"
    elif score >= 4:
        return "高风险"
    elif score >= 3:
        return "较高风险"
    elif score >= 2:
        return "关注"
    elif score >= 1:
        return "一般"
    else:
        return "低风险"


# ====================================================
# 八、生成单片区天气内容
# ====================================================

def build_point_weather(point: WeatherPoint) -> Dict:
    """获取并整理单个片区天气。"""
    location_id, resolved_name, adm2 = get_location_id(point.query)

    now_data = get_weather_now(location_id)
    daily_data = get_weather_7d(location_id)

    now = now_data.get("now", {})
    daily = daily_data.get("daily", [])

    if not now:
        raise RuntimeError(f"{point.name} 实况天气为空")

    if not daily:
        raise RuntimeError(f"{point.name} 未来七天天气为空")

    # 实况字段
    now_text = md_escape(now.get("text", "-"))
    now_temp = md_escape(now.get("temp", "-"))
    now_feels_like = md_escape(now.get("feelsLike", "-"))
    now_wind_dir = md_escape(now.get("windDir", "-"))
    now_wind_scale = md_escape(now.get("windScale", "-"))
    now_wind_speed = md_escape(now.get("windSpeed", "-"))
    now_precip = safe_float(now.get("precip", 0), 0)
    now_vis = md_escape(now.get("vis", "-"))
    obs_time = md_escape(now.get("obsTime", "-"))

    now_rain_level, now_rain_desc = rain_level_by_now_precip(now_precip)
    now_wind_level, now_wind_desc = wind_level_by_scale(now_wind_scale)

    forecast_list = []
    max_daily_risk_score = 0
    key_risks = []

    if now_rain_level in ["关注", "较高", "高", "特高"]:
        key_risks.append(f"实况降雨{now_precip:.1f}mm，降雨风险{now_rain_level}")

    if now_wind_level in ["关注", "较高", "高"]:
        key_risks.append(f"实况{now_wind_dir}{now_wind_scale}级，风风险{now_wind_level}")

    max_heat_score = 0
    max_heat_day = None

    for day in daily[:7]:
        fx_date = md_escape(day.get("fxDate", "-"))
        text_day = md_escape(day.get("textDay", "-"))
        text_night = md_escape(day.get("textNight", "-"))
        temp_min = md_escape(day.get("tempMin", "-"))
        temp_max = md_escape(day.get("tempMax", "-"))
        precip = safe_float(day.get("precip", 0), 0)
        wind_dir_day = md_escape(day.get("windDirDay", "-"))
        wind_scale_day = md_escape(day.get("windScaleDay", "-"))
        wind_speed_day = md_escape(day.get("windSpeedDay", "-"))

        rain_level, rain_desc = rain_level_by_daily_precip(precip)
        wind_level, wind_desc = wind_level_by_scale(wind_scale_day)
        heat_level, heat_icon, heat_score = heat_level_by_temp(temp_max)
        max_heat_score = max(max_heat_score, heat_score)
        if max_heat_day is None or heat_score > max_heat_day["heatScore"]:
            max_heat_day = {
                "fxDate": fx_date,
                "tempMax": temp_max,
                "heatLevel": heat_level,
                "heatIcon": heat_icon,
                "heatScore": heat_score,
            }

        score = risk_score(rain_level, wind_level)
        max_daily_risk_score = max(max_daily_risk_score, score)

        if rain_level in ["关注", "较高", "高", "特高"]:
            key_risks.append(
                f"{fx_date}预计降雨{precip:.1f}mm，降雨风险{rain_level}"
            )

        if wind_level in ["关注", "较高", "高"]:
            key_risks.append(
                f"{fx_date}{wind_dir_day}{wind_scale_day}级，风风险{wind_level}"
            )

        forecast_list.append(
            {
                "fxDate": fx_date,
                "textDay": text_day,
                "textNight": text_night,
                "tempMin": temp_min,
                "tempMax": temp_max,
                "precip": precip,
                "windDirDay": wind_dir_day,
                "windScaleDay": wind_scale_day,
                "windSpeedDay": wind_speed_day,
                "rainLevel": rain_level,
                "rainDesc": rain_desc,
                "windLevel": wind_level,
                "windDesc": wind_desc,
                "heatLevel": heat_level,
                "heatIcon": heat_icon,
                "heatScore": heat_score,
            }
        )

    now_score = risk_score(now_rain_level, now_wind_level)
    overall_score = max(now_score, max_daily_risk_score)
    overall_risk = risk_label_by_score(overall_score)

    return {
        "name": point.name,
        "query": point.query,
        "locationId": location_id,
        "resolvedName": resolved_name,
        "adm2": adm2,
        "obsTime": obs_time,
        "now": {
            "text": now_text,
            "temp": now_temp,
            "feelsLike": now_feels_like,
            "windDir": now_wind_dir,
            "windScale": now_wind_scale,
            "windSpeed": now_wind_speed,
            "precip": now_precip,
            "vis": now_vis,
            "rainLevel": now_rain_level,
            "rainDesc": now_rain_desc,
            "windLevel": now_wind_level,
            "windDesc": now_wind_desc,
        },
        "forecast": forecast_list,
        "keyRisks": key_risks,
        "overallRisk": overall_risk,
        "overallScore": overall_score,
        "maxHeatScore": max_heat_score,
        "maxHeatDay": max_heat_day,
    }


def generate_weather_chart(results: List[Dict], output_path: str) -> str:
    """生成五个片区未来七天气温和降雨图示。"""
    if not results:
        return ""

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except Exception as exc:
        print(f"[WARN] matplotlib 不可用，跳过图示生成：{exc}")
        return ""

    preferred_fonts = [
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "WenQuanYi Micro Hei",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    available_fonts = {font.name for font in font_manager.fontManager.ttflist}
    for font_name in preferred_fonts:
        if font_name in available_fonts:
            plt.rcParams["font.sans-serif"] = [font_name]
            break
    plt.rcParams["axes.unicode_minus"] = False

    panel_count = len(results)
    fig_height = max(3.0 * panel_count, 5.0)
    fig, axes = plt.subplots(panel_count, 1, figsize=(13, fig_height), sharex=False)
    if panel_count == 1:
        axes = [axes]

    for ax, item in zip(axes, results):
        forecast = item.get("forecast", [])[:7]
        dates = [day["fxDate"][-5:] for day in forecast]
        temp_max = [safe_float(day["tempMax"], 0) for day in forecast]
        temp_min = [safe_float(day["tempMin"], 0) for day in forecast]
        precip = [safe_float(day["precip"], 0) for day in forecast]

        ax.axhspan(35, 37, color="#fff3bf", alpha=0.55, label="黄色 >35℃")
        ax.axhspan(37, 40, color="#ffd8a8", alpha=0.55, label="橙色 >37℃")
        ax.axhspan(40, max(max(temp_max, default=40) + 2, 42), color="#ffc9c9", alpha=0.55, label="红色 >40℃")

        line_max, = ax.plot(dates, temp_max, color="#d9480f", marker="o", linewidth=2.2, label="最高气温")
        line_min, = ax.plot(dates, temp_min, color="#1971c2", marker="o", linewidth=2.0, label="最低气温")
        ax.set_ylabel("气温(℃)")
        ax.set_ylim(min(min(temp_min, default=20) - 2, 20), max(max(temp_max, default=40) + 2, 42))
        ax.grid(axis="y", linestyle="--", linewidth=0.6, alpha=0.35)
        ax.set_title(item["name"], loc="left", fontsize=13, fontweight="bold")

        ax_rain = ax.twinx()
        bars = ax_rain.bar(dates, precip, color="#74c0fc", alpha=0.55, width=0.45, label="降雨量")
        ax_rain.set_ylabel("降雨(mm)")
        ax_rain.set_ylim(0, max(max(precip, default=0) * 1.8, 5))

        for date, high, low, rain in zip(dates, temp_max, temp_min, precip):
            level, _, _ = heat_level_by_temp(high)
            if level != "无预警":
                ax.text(date, high + 0.4, level[0], ha="center", va="bottom", fontsize=11, fontweight="bold", color="#c92a2a")
            if rain > 0:
                ax_rain.text(date, rain + 0.1, f"{rain:.1f}", ha="center", va="bottom", fontsize=8, color="#1864ab")

        ax.legend([line_max, line_min, bars], ["最高气温", "最低气温", "降雨量"], loc="upper right", ncols=3, fontsize=9)

    fig.suptitle("五片区未来七天气温与降雨趋势", fontsize=18, fontweight="bold")
    fig.text(0.5, 0.012, "防胀预警阈值：黄色 >35℃，橙色 >37℃，红色 >40℃", ha="center", fontsize=10, color="#495057")
    fig.tight_layout(rect=[0, 0.025, 1, 0.97])

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(fig)

    return str(output)


# ====================================================
# 九、生成 Markdown 报告
# ====================================================

def build_markdown_report(results: List[Dict], failed: List[Dict]) -> str:
    """
    生成钉钉 Markdown 天气日报：极简版
    保留：
    1. 风险总览
    2. 天气实况
    3. 未来三天
    4. 防洪风险提示
    """

    report_time = now_str()

    lines: List[str] = []

    lines.append(f"#  {REPORT_TITLE}")
    lines.append(f"**发布时间：{report_time}**")
    lines.append("")
    lines.append("> 辅助研判，现场处置以正式预警、调度命令和现场检查为准。")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 一、风险总览
    lines.append("## 一、风险总览")

    if results:
        sorted_results = sorted(results, key=lambda x: x["overallScore"], reverse=True)

        for item in sorted_results:
            now = item["now"]
            icon = overall_risk_icon(item["overallRisk"])
            today = item["forecast"][0] if item.get("forecast") else {}
            today_temp_max = today.get("tempMax", "-")

            lines.append(
                f"- {icon} **{item['name']}**：{item['overallRisk']}，"
                f"{heat_alert_text(today_temp_max)}，"
                f"{weather_icon(now['text'])}{now['text']}，"
                f"{now['temp']}℃，雨{now['precip']:.1f}mm，"
                f"{now['windDir']}{now['windScale']}级"
            )
    else:
        lines.append("- ⚠️ 未获取到有效天气数据。")

    if failed:
        for item in failed:
            lines.append(f"- ⚠️ **{item['name']}**：数据获取失败")

    lines.append("")
    lines.append("---")
    lines.append("")

    # 二、未来三天
    lines.append("## 二、未来三天")

    if results:
        for item in results:
            forecast_texts = []

            for day in item["forecast"][:3]:
                w_icon = weather_icon(day["textDay"] + day["textNight"])
                r_icon = rain_icon(day["rainLevel"])

                forecast_texts.append(
                    f"{day['fxDate'][-5:]} {w_icon}{day['textDay']}/{day['textNight']}，"
                    f"{day['tempMin']}～{day['tempMax']}℃，"
                    f"雨{day['precip']:.1f}mm，"
                    f"{heat_alert_text(day['tempMax'])}"
                )

            lines.append(f"- **{item['name']}**：")
            for txt in forecast_texts:
                lines.append(f"  - {txt}")
    else:
        lines.append("- ⚠️ 未来三天数据获取失败。")

    lines.append("")
    lines.append("---")
    lines.append("")

    # 三、防胀气温预警
    lines.append("## 三、防胀气温预警")

    if results:
        heat_items = [
            x for x in sorted(results, key=lambda item: item.get("maxHeatScore", 0), reverse=True)
            if x.get("maxHeatScore", 0) > 0
        ]

        if heat_items:
            for item in heat_items:
                heat_day = item.get("maxHeatDay") or {}
                lines.append(
                    f"- {heat_day.get('heatIcon', '🟡')} **{item['name']}**："
                    f"未来七天最高{heat_day.get('tempMax', '-')}℃"
                    f"（{heat_day.get('fxDate', '-')[-5:]}），"
                    f"触发防胀{heat_day.get('heatLevel', '黄色')}预警。"
                )
            lines.append("- 建议重点关注 12:00—17:00 高温时段，结合轨温和线路状态现场复核。")
        else:
            lines.append("- 🟢 未来七天最高气温暂未超过 35℃，暂不触发防胀高温预警。")
    else:
        lines.append("- ⚠️ 天气数据获取异常，请人工复核高温防胀风险。")

    lines.append("")
    lines.append("---")
    lines.append("")

    if CHART_IMAGE_URL:
        # 四、未来七天气温降雨图示
        lines.append("## 四、未来七天气温降雨图示")
        lines.append(f"![五片区未来七天气温降雨图]({CHART_IMAGE_URL})")

        lines.append("")
        lines.append("---")
        lines.append("")

    # 防洪防风风险提示
    lines.append("## 防洪防风风险提示")

    if results:
        sorted_results = sorted(results, key=lambda x: x["overallScore"], reverse=True)

        risk_items = [
            x for x in sorted_results
            if x["overallRisk"] in ["特高风险", "高风险", "较高风险", "关注"]
        ]

        if risk_items:
            for item in risk_items:
                icon = overall_risk_icon(item["overallRisk"])

                if item["keyRisks"]:
                    risk_text = "；".join(item["keyRisks"][:3])
                else:
                    risk_text = "需关注局地短时天气变化"

                lines.append(
                    f"- {icon} **{item['name']}**：**{item['overallRisk']}**。{risk_text}。"
                )
        else:
            lines.append("- 🟢 当前及未来三天暂未发现明显强降雨或大风高风险信号。")
    else:
        lines.append("- ⚠️ 天气数据获取异常，请人工查看气象信息。")

    if failed:
        for item in failed:
            lines.append(f"- ⚠️ **{item['name']}**：天气数据获取失败，建议人工复核。")

    lines.append("")
    lines.append("> 本消息为石柱综合维修车间工务防洪防风防胀机器人辅助提醒。")

    return "\n".join(lines)


# ====================================================
# 十、主流程
# ====================================================

def main() -> None:
    """主函数。"""
    require_env()

    print("=" * 80)
    print("钉钉防洪天气机器人启动")
    print("时间：", now_str())
    print("地点数量：", len(WEATHER_POINTS))
    print("QWEATHER_HOST：", QWEATHER_HOST)
    print("=" * 80)

    results: List[Dict] = []
    failed: List[Dict] = []

    for point in WEATHER_POINTS:
        print(f"[INFO] 获取天气：{point.name} -> {point.query}")

        try:
            data = build_point_weather(point)
            results.append(data)

            print(
                f"[OK] {point.name}："
                f"实况{data['now']['text']}，"
                f"降雨{data['now']['precip']:.1f}mm，"
                f"综合风险{data['overallRisk']}"
            )

        except Exception as e:
            error_msg = str(e)
            failed.append(
                {
                    "name": point.name,
                    "query": point.query,
                    "error": error_msg,
                }
            )
            print(f"[ERROR] {point.name} 获取失败：{error_msg}")

    chart_path = generate_weather_chart(results, CHART_OUTPUT)
    if chart_path:
        print(f"[OK] 七天气温降雨图已生成：{chart_path}")

    markdown_text = build_markdown_report(results, failed)

    print("=" * 80)
    print("生成推送内容：")
    print(markdown_text)
    print("=" * 80)

    if REPORT_OUTPUT:
        report_path = Path(REPORT_OUTPUT)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(markdown_text, encoding="utf-8")
        print(f"[OK] Markdown 报告已保存：{report_path}")

    if not SEND_DINGTALK:
        print("[SKIP] SEND_DINGTALK=false，跳过钉钉推送。")
        return

    send_dingtalk_markdown(REPORT_TITLE, markdown_text)

    print("[DONE] 推送完成。")


if __name__ == "__main__":
    main()
