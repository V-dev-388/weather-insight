#!/usr/bin/env python3
"""compute_metrics.py — 气象指标计算。

从 Open-Meteo JSON（含多层气压面）计算气象分析所需指标：
- 气压梯度（3小时变压）
- K指数（大气对流稳定性）
- Showalter指数近似（雷暴潜势）
- CAPE近似（对流能量）
- 降水分级（中国气象标准）
- 风切变（850-200hPa 矢量差）
- 温度距平
- 天气系统倾向评分

用法:
    python3 compute_metrics.py --input data.json
    python3 compute_metrics.py --input data.json --summary

指标算法与阈值详见 references/analysis-methods.md
"""
import argparse
import json
import math
import sys


def mean(vals):
    valid = [v for v in vals if v is not None]
    return sum(valid) / len(valid) if valid else None


def dew_point(temp_c, rh):
    """马格努斯公式计算露点温度（°C）。"""
    if temp_c is None or rh is None:
        return None
    a, b = 17.625, 243.04
    gamma = math.log(max(rh, 0.001) / 100) + (a * temp_c) / (b + temp_c)
    return (b * gamma) / (a - gamma)


def wind_components(speed, direction):
    """风向(度,0=北,顺时针)转 u,v 分量。"""
    if speed is None or direction is None:
        return None, None
    rad = math.radians(direction)
    u = -speed * math.sin(rad)
    v = -speed * math.cos(rad)
    return u, v


def compute_pressure_gradient(pressure_msl):
    """3小时变压（hPa/3h）。正值=升压(高压趋近)，负值=降压(低压趋近)。"""
    result = []
    for i in range(len(pressure_msl)):
        if i < 3 or pressure_msl[i] is None or pressure_msl[i - 3] is None:
            result.append(None)
        else:
            result.append(round(pressure_msl[i] - pressure_msl[i - 3], 2))
    return result


def compute_k_index(hourly):
    """K指数 = (T850-T500) + Td850 - (T700-Td700)。
    <20稳定, 20-30中等, 30-40雷暴可能, >40强对流潜势。"""
    t850 = hourly.get("temperature_850hPa", [])
    t500 = hourly.get("temperature_500hPa", [])
    t700 = hourly.get("temperature_700hPa", [])
    rh850 = hourly.get("relative_humidity_850hPa", [])
    rh700 = hourly.get("relative_humidity_700hPa", [])

    n = max(len(t850), len(t500), len(t700)) if t850 else 0
    result = []
    for i in range(n):
        T850 = t850[i] if i < len(t850) else None
        T500 = t500[i] if i < len(t500) else None
        T700 = t700[i] if i < len(t700) else None
        RH850 = rh850[i] if i < len(rh850) else None
        RH700 = rh700[i] if i < len(rh700) else None
        if None in (T850, T500, T700, RH850, RH700):
            result.append(None)
            continue
        Td850 = dew_point(T850, RH850)
        Td700 = dew_point(T700, RH700)
        if Td850 is None or Td700 is None:
            result.append(None)
            continue
        result.append(round((T850 - T500) + Td850 - (T700 - Td700), 2))
    return result


def compute_showalter(k_index_values):
    """Showalter指数近似（基于K指数简化估计）。
    SI = 20 - 0.5*K。>0稳定, <0不稳定, <-3强雷暴潜势。"""
    return [round(20 - 0.5 * k, 2) if k is not None else None for k in k_index_values]


def compute_cape_approx(k_index_values):
    """CAPE近似（J/kg，基于K指数简化估计）。
    CAPE = max(0, (K-25)*80)。>1000强对流。"""
    return [round(max(0, (k - 25) * 80), 1) if k is not None else None for k in k_index_values]


def classify_precipitation(precipitation, times):
    """24小时降水累积分级（默认中国气象标准，国际标准见 references/analysis-methods.md）。"""
    daily = {}
    for i, p in enumerate(precipitation):
        if p is None or i >= len(times):
            continue
        day = times[i][:10]
        daily[day] = daily.get(day, 0) + p

    result = {}
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
        result[day] = {"total_mm": round(total, 1), "level": level}
    return result


def compute_wind_shear(hourly):
    """850hPa与200hPa风矢量差（m/s）。>15强切变(组织化对流环境)。"""
    ws850 = hourly.get("wind_speed_850hPa", [])
    wd850 = hourly.get("wind_direction_850hPa", [])
    ws200 = hourly.get("wind_speed_200hPa", [])
    wd200 = hourly.get("wind_direction_200hPa", [])

    n = max(len(ws850), len(ws200)) if ws850 else 0
    result = []
    for i in range(n):
        s850 = ws850[i] if i < len(ws850) else None
        d850 = wd850[i] if i < len(wd850) else None
        s200 = ws200[i] if i < len(ws200) else None
        d200 = wd200[i] if i < len(wd200) else None
        if None in (s850, d850, s200, d200):
            result.append(None)
            continue
        u850, v850 = wind_components(s850, d850)
        u200, v200 = wind_components(s200, d200)
        du, dv = u200 - u850, v200 - v850
        result.append(round(math.sqrt(du * du + dv * dv), 2))
    return result


def compute_temp_anomaly(temperature_2m):
    """温度距平（与序列均值的偏差，°C）。"""
    avg = mean(temperature_2m)
    if avg is None:
        return [None] * len(temperature_2m)
    return [round(t - avg, 2) if t is not None else None for t in temperature_2m]


def weather_system_tendency(pressure_msl, wind_speed_10m, cloud_cover, pressure_gradient):
    """天气系统倾向评分（综合气压趋势+风+云量）。
    正分=高压控制(晴好), 负分=低压影响(转差)。"""
    result = []
    for i in range(len(pressure_msl)):
        if i >= len(pressure_gradient) or pressure_gradient[i] is None:
            result.append(None)
            continue
        pg = pressure_gradient[i]
        ws = wind_speed_10m[i] if i < len(wind_speed_10m) and wind_speed_10m[i] else 0
        cc = cloud_cover[i] if i < len(cloud_cover) and cloud_cover[i] is not None else 50

        score = 0
        if pg > 1:
            score += 2
        elif pg > 0.5:
            score += 1
        elif pg < -1:
            score -= 2
        elif pg < -0.5:
            score -= 1
        if ws > 8:
            score -= 1
        if cc > 80:
            score -= 1
        elif cc < 30:
            score += 1

        if score >= 2:
            tendency = "高压控制"
        elif score >= 1:
            tendency = "高压趋近"
        elif score == 0:
            tendency = "稳定"
        elif score >= -1:
            tendency = "低压趋近"
        else:
            tendency = "低压影响"
        result.append({"score": score, "tendency": tendency})
    return result


def compute_all(data):
    """计算所有指标。"""
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    pressure_msl = hourly.get("pressure_msl", [])
    temperature_2m = hourly.get("temperature_2m", [])
    wind_speed_10m = hourly.get("wind_speed_10m", [])
    cloud_cover = hourly.get("cloud_cover", [])
    precipitation = hourly.get("precipitation", [])

    pressure_gradient = compute_pressure_gradient(pressure_msl)
    k_index = compute_k_index(hourly)
    showalter = compute_showalter(k_index)
    cape = compute_cape_approx(k_index)
    precip_class = classify_precipitation(precipitation, times)
    wind_shear = compute_wind_shear(hourly)
    temp_anomaly = compute_temp_anomaly(temperature_2m)
    tendency = weather_system_tendency(pressure_msl, wind_speed_10m, cloud_cover, pressure_gradient)

    return {
        "location": {"lat": data.get("latitude"), "lon": data.get("longitude")},
        "time_range": {"start": times[0] if times else None, "end": times[-1] if times else None},
        "metrics": {
            "pressure_gradient_3h_hpa": pressure_gradient,
            "k_index": k_index,
            "showalter_index_approx": showalter,
            "cape_approx_j_kg": cape,
            "wind_shear_850_200_ms": wind_shear,
            "temperature_anomaly_c": temp_anomaly,
        },
        "precipitation_classification": precip_class,
        "weather_system_tendency": tendency,
    }


def summarize_metrics(metrics_data):
    """生成指标摘要（供 LLM 分析参考）。"""
    m = metrics_data["metrics"]

    def stats(vals):
        valid = [v for v in vals if v is not None]
        if not valid:
            return None
        return {"min": round(min(valid), 2), "max": round(max(valid), 2),
                "avg": round(sum(valid) / len(valid), 2)}

    # 稳定性评估
    k_vals = [v for v in m["k_index"] if v is not None]
    stability = "数据不足"
    if k_vals:
        k_avg = sum(k_vals) / len(k_vals)
        if k_avg < 20:
            stability = "稳定（对流潜势低）"
        elif k_avg < 30:
            stability = "中等（有限对流可能）"
        elif k_avg < 40:
            stability = "中等不稳定（雷暴可能）"
        else:
            stability = "很不稳定（强对流潜势）"

    # 风切变评估
    shear_vals = [v for v in m["wind_shear_850_200_ms"] if v is not None]
    shear_assess = "数据不足"
    if shear_vals:
        shear_avg = sum(shear_vals) / len(shear_vals)
        if shear_avg < 10:
            shear_assess = "弱切变"
        elif shear_avg < 20:
            shear_assess = "中等切变"
        else:
            shear_assess = "强切变（组织化对流环境）"

    # 倾向汇总
    tendency_list = metrics_data["weather_system_tendency"]
    valid_t = [t for t in tendency_list if t is not None]
    tendency_summary = "数据不足"
    if valid_t:
        avg_score = sum(t["score"] for t in valid_t) / len(valid_t)
        if avg_score >= 1.5:
            tendency_summary = "总体高压控制，天气晴好"
        elif avg_score >= 0.5:
            tendency_summary = "高压趋近，天气趋稳"
        elif avg_score >= -0.5:
            tendency_summary = "天气平稳"
        elif avg_score >= -1.5:
            tendency_summary = "低压趋近，天气转差"
        else:
            tendency_summary = "低压影响，天气不佳"

    return {
        "location": metrics_data["location"],
        "time_range": metrics_data["time_range"],
        "stability_assessment": stability,
        "wind_shear_assessment": shear_assess,
        "weather_tendency": tendency_summary,
        "metric_stats": {
            "pressure_gradient_3h_hpa": stats(m["pressure_gradient_3h_hpa"]),
            "k_index": stats(m["k_index"]),
            "showalter_index_approx": stats(m["showalter_index_approx"]),
            "cape_approx_j_kg": stats(m["cape_approx_j_kg"]),
            "wind_shear_850_200_ms": stats(m["wind_shear_850_200_ms"]),
            "temperature_anomaly_c": stats(m["temperature_anomaly_c"]),
        },
        "precipitation": metrics_data["precipitation_classification"],
    }


def main():
    parser = argparse.ArgumentParser(description="气象指标计算（气压梯度/K指数/CAPE/风切变等）")
    parser.add_argument("--input", type=str, required=True, help="Open-Meteo JSON 文件路径")
    parser.add_argument("--summary", action="store_true", help="仅输出指标摘要")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    print("[INFO] 计算气象指标...", file=sys.stderr)
    metrics_data = compute_all(data)

    output = summarize_metrics(metrics_data) if args.summary else metrics_data
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
