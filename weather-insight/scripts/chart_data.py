#!/usr/bin/env python3
"""chart_data.py — 从 Open-Meteo JSON 提取并降采样图表数据。

输出精简 JSON，供 LLM 直接生成 SVG 图表或写入可视化工具。

用法:
    python3 chart_data.py --input data.json --type pressure
    python3 chart_data.py --input data.json --type temp_precip
    python3 chart_data.py --input data.json --type wind_rose
"""
import argparse
import json
import math
import os
import sys
from collections import Counter


# WMO 天气代码映射
WMO_LABELS = {
    0: "晴", 1: "大部晴", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "中毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨(小)", 57: "冻毛毛雨(大)",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨(小)", 67: "冻雨(大)",
    71: "小雪", 73: "中雪", 75: "大雪",
    77: "雪粒",
    80: "小阵雨", 81: "中阵雨", 82: "大阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷暴", 96: "雷暴+小冰雹", 99: "雷暴+大冰雹",
}

# 风向中文方位
DIRS = ["北", "东北偏北", "东北", "东北偏东", "东", "东南偏东", "东南", "东南偏南",
        "南", "西南偏南", "西南", "西南偏西", "西", "西北偏西", "西北", "西北偏北"]

# 风力等级 (Beaufort)
WIND_LEVELS = [
    (0.0, 0.5, "0级 无风"), (0.6, 1.5, "1级 软风"), (1.6, 3.3, "2级 轻风"),
    (3.4, 5.4, "3级 微风"), (5.5, 7.9, "4级 和风"), (8.0, 10.7, "5级 清风"),
    (10.8, 13.8, "6级 强风"), (13.9, 17.1, "7级 疾风"), (17.2, 20.7, "8级 大风"),
    (20.8, 24.4, "9级 烈风"), (24.5, 28.4, "10级 狂风"), (28.5, 32.6, "11级 暴风"),
    (32.7, 100.0, "12级 飓风"),
]


def wind_level_label(speed):
    for lo, hi, label in WIND_LEVELS:
        if lo <= speed <= hi:
            return label
    return "12级 飓风"


def wind_direction_label(deg):
    """风向(度, 0=北) → 中文方位 + 角度。"""
    if deg is None:
        return "未知"
    idx = int((deg + 11.25) % 360 / 22.5)
    return DIRS[idx]


def downsample(times, values, max_points=48):
    """等间隔降采样，保留首尾。返回 (new_times, new_values)。"""
    if len(times) <= max_points:
        return times, values
    step = len(times) / max_points
    indices = [0] + [int(i * step) for i in range(1, max_points - 1)] + [len(times) - 1]
    seen = set()
    unique_indices = []
    for i in indices:
        if i not in seen:
            seen.add(i)
            unique_indices.append(i)
    return [times[i] for i in unique_indices], [values[i] for i in unique_indices]


def format_time_label(iso_str):
    """ISO 时间 → 简短标签（如 '29日14时'）。"""
    if not iso_str:
        return ""
    # 2026-07-29T14:00 → "29日14时"
    try:
        date_part = iso_str.split("T")[0]
        time_part = iso_str.split("T")[1][:2]
        day = date_part.split("-")[2].lstrip("0") or "0"
        return f"{day}日{time_part}时"
    except Exception:
        return iso_str


def type_pressure(data):
    """气压趋势图数据。"""
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    pressure = hourly.get("pressure_msl", [])

    times_ds, pressure_ds = downsample(times, pressure, max_points=48)
    labels = [format_time_label(t) for t in times_ds]

    valid = [v for v in pressure if v is not None]
    return {
        "title": "海平面气压趋势",
        "unit": "hPa",
        "x": labels,
        "y": pressure_ds,
        "y_min": round(min(valid), 1) if valid else None,
        "y_max": round(max(valid), 1) if valid else None,
        "extrema": {
            "high": {"value": round(max(valid), 1), "time": times[pressure.index(max(valid))] if valid and max(valid) in pressure else None},
            "low": {"value": round(min(valid), 1), "time": times[pressure.index(min(valid))] if valid and min(valid) in pressure else None},
        } if valid else None,
    }


def type_temp_precip(data):
    """温度-降水组合图数据。"""
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temp = hourly.get("temperature_2m", [])
    precip = hourly.get("precipitation", [])

    times_ds, temp_ds = downsample(times, temp, max_points=48)
    _, precip_ds = downsample(times, precip, max_points=48)
    labels = [format_time_label(t) for t in times_ds]

    valid_temp = [v for v in temp if v is not None]
    valid_precip = [v for v in precip if v is not None]

    # 24h 降水分级
    daily = {}
    for i, p in enumerate(precip):
        if p is None or i >= len(times):
            continue
        day = times[i][:10]
        daily[day] = daily.get(day, 0) + p

    precip_levels = {}
    for day, total in sorted(daily.items()):
        if total < 0.1:
            level = "无降水"
        elif total < 10:
            level = "小雨"
        elif total < 25:
            level = "中雨"
        elif total < 50:
            level = "大雨"
        elif total < 100:
            level = "暴雨"
        else:
            level = "大暴雨"
        precip_levels[day] = {"total_mm": round(total, 1), "level": level}

    return {
        "title": "温度与降水",
        "temp": {"label": "温度 (°C)", "unit": "°C", "x": labels, "y": temp_ds},
        "precip": {"label": "降水 (mm)", "unit": "mm", "x": labels, "y": precip_ds},
        "temp_range": {
            "min": round(min(valid_temp), 1) if valid_temp else None,
            "max": round(max(valid_temp), 1) if valid_temp else None,
        },
        "daily_precipitation": precip_levels,
    }


def type_wind_rose(data):
    """风玫瑰图数据（按16方位汇总）。"""
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    ws = hourly.get("wind_speed_10m", [])
    wd = hourly.get("wind_direction_10m", [])

    n = min(len(ws), len(wd), len(times)) if ws and wd else 0
    dir_buckets = {d: {"count": 0, "speed_sum": 0.0, "level_counts": Counter()} for d in DIRS}

    for i in range(n):
        speed = ws[i]
        direction = wd[i]
        if speed is None or direction is None:
            continue
        label = wind_direction_label(direction)
        dir_buckets[label]["count"] += 1
        dir_buckets[label]["speed_sum"] += speed
        lv = wind_level_label(speed)
        dir_buckets[label]["level_counts"][lv] += 1

    result = []
    for d in DIRS:
        bucket = dir_buckets[d]
        if bucket["count"] == 0:
            continue
        avg_speed = round(bucket["speed_sum"] / bucket["count"], 1)
        result.append({
            "dir": d,
            "count": bucket["count"],
            "avg_speed": avg_speed,
            "dominant_level": bucket["level_counts"].most_common(1)[0][0],
        })

    return {
        "title": "风玫瑰（16方位）",
        "unit": "m/s",
        "bins": result,
    }


def main():
    parser = argparse.ArgumentParser(description="图表数据提取与降采样")
    parser.add_argument("--input", type=str, required=True, help="Open-Meteo JSON 文件路径")
    parser.add_argument("--type", type=str, required=True,
                        choices=["pressure", "temp_precip", "wind_rose"],
                        help="图表类型: pressure / temp_precip / wind_rose")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"[ERROR] 文件不存在: {args.input}", file=sys.stderr)
        sys.exit(1)

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    dispatch = {
        "pressure": type_pressure,
        "temp_precip": type_temp_precip,
        "wind_rose": type_wind_rose,
    }
    output = dispatch[args.type](data)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
