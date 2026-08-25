#!/usr/bin/env python3
"""run_pipeline.py — 气象数据端到端流水线。

数据获取 → 指标计算 → 结构化 JSON 输出（可选生成交互式 HTML 面板）。

用法:
    python3 run_pipeline.py --lat 31.2 --lon 121.5 [--days 7] [--output <dir>] [--no-metrics] [--scope <scope>] [--html <面板路径>]
    python3 run_pipeline.py --lat 31.2 --lon 121.5 --scope city
    python3 run_pipeline.py --lat 31.2 --lon 121.5 --html /tmp/panel.html
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FETCH_SCRIPT = os.path.join(SCRIPT_DIR, "fetch_openmeteo.py")
METRICS_SCRIPT = os.path.join(SCRIPT_DIR, "compute_metrics.py")
DASHBOARD_SCRIPT = os.path.join(SCRIPT_DIR, "render_dashboard.py")


def run_cmd(cmd, label=""):
    """运行外部命令，返回 stdout 字符串，失败则抛异常。"""
    print(f"[PIPELINE] {label}: {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"{label} 失败 (exit {result.returncode}): {result.stderr}")
    return result.stdout


def fetch_data(lat, lon, days=7, models=None, past_days=0, scope=None):
    """获取气象数据，返回 (data_dict, method)。"""
    cmd = [sys.executable, FETCH_SCRIPT, "--lat", str(lat), "--lon", str(lon), "--days", str(days)]
    if models:
        cmd += ["--models", models]
    if past_days > 0:
        cmd += ["--past-days", str(past_days)]
    stdout = run_cmd(cmd, label="fetch")
    data = json.loads(stdout)
    return data


def compute_metrics(data):
    """计算气象指标，返回 metrics_dict。"""
    # 写入临时文件供 compute_metrics.py 读取
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        tmp_path = f.name
    try:
        stdout = run_cmd([sys.executable, METRICS_SCRIPT, "--input", tmp_path, "--summary"],
                         label="metrics")
        return json.loads(stdout)
    finally:
        os.unlink(tmp_path)


def render_dashboard(data, metrics_summary, html_path):
    """调用 render_dashboard.py 生成交互式 HTML 面板；失败只告警不中断流水线。"""
    # 写入临时文件供 render_dashboard.py 读取
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        tmp_path = f.name
    try:
        cmd = [sys.executable, DASHBOARD_SCRIPT,
               "--input", tmp_path, "--output", html_path,
               "--title", "气象面板"]
        if metrics_summary is not None:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as fm:
                json.dump(metrics_summary, fm, ensure_ascii=False)
                metrics_tmp = fm.name
            cmd += ["--metrics", metrics_tmp]
        else:
            metrics_tmp = None
        try:
            run_cmd(cmd, label="dashboard")
            print(f"[PIPELINE] 面板已生成: {html_path}", file=sys.stderr)
        except Exception as e:
            print(f"[PIPELINE] 警告: 面板生成失败(不影响流水线): {e}", file=sys.stderr)
        finally:
            if metrics_tmp:
                os.unlink(metrics_tmp)
    finally:
        os.unlink(tmp_path)


def build_extrema(data):
    """从 Open-Meteo 原始数据提取关键极值。"""
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    def extrema(key):
        vals = [v for v in hourly.get(key, []) if v is not None]
        if not vals:
            return None
        return {"min": round(min(vals), 2), "max": round(max(vals), 2)}

    return {
        "pressure_msl_hpa": extrema("pressure_msl"),
        "temperature_2m_c": extrema("temperature_2m"),
        "wind_speed_10m_ms": extrema("wind_speed_10m"),
        "precipitation_mm": extrema("precipitation"),
        "cloud_cover_pct": extrema("cloud_cover"),
    }


def summarize_weather(data, metrics_summary):
    """根据数据和指标生成天气倾向总结。"""
    hourly = data.get("hourly", {})
    weather_codes = hourly.get("weather_code", [])
    precip = hourly.get("precipitation", [])
    cloud = hourly.get("cloud_cover", [])

    # 天气代码出现最多的前3个
    from collections import Counter
    valid_codes = [c for c in weather_codes if c is not None]
    top_codes = Counter(valid_codes).most_common(3) if valid_codes else []

    # 降水统计
    total_precip = sum(p for p in precip if p is not None)

    # 云量统计
    avg_cloud = sum(c for c in cloud if c is not None) / len([c for c in cloud if c is not None]) if any(c is not None for c in cloud) else None

    # 稳定性
    stability = metrics_summary.get("stability_assessment", "数据不足")
    tendency = metrics_summary.get("weather_tendency", "数据不足")

    # WMO 代码映射
    WMO = {
        0: "晴", 1: "大部晴", 2: "多云", 3: "阴",
        45: "雾", 48: "雾凇",
        51: "小毛毛雨", 53: "中毛毛雨", 55: "大毛毛雨",
        61: "小雨", 63: "中雨", 65: "大雨",
        71: "小雪", 73: "中雪", 75: "大雪",
        80: "小阵雨", 81: "中阵雨", 82: "大阵雨",
        95: "雷暴", 96: "雷暴+小冰雹", 99: "雷暴+大冰雹",
    }

    desc_parts = []
    desc_parts.append(f"总降水 {round(total_precip, 1)}mm")
    if avg_cloud is not None:
        desc_parts.append(f"平均云量 {round(avg_cloud)}%")
    desc_parts.append(f"大气{stability}")
    desc_parts.append(f"{tendency}")

    weather_desc = "; ".join(desc_parts)

    return {
        "top_weather_codes": [{"code": c, "label": WMO.get(c, f"未知({c})"), "count": n} for c, n in top_codes],
        "total_precipitation_mm": round(total_precip, 1),
        "average_cloud_cover_pct": round(avg_cloud, 1) if avg_cloud is not None else None,
        "stability": stability,
        "tendency": tendency,
        "summary": weather_desc,
    }


def main():
    parser = argparse.ArgumentParser(description="气象数据端到端流水线")
    parser.add_argument("--lat", type=float, required=True, help="纬度")
    parser.add_argument("--lon", type=float, required=True, help="经度")
    parser.add_argument("--days", type=int, default=7, help="预报天数")
    parser.add_argument("--past-days", type=int, default=0, help="过去天数")
    parser.add_argument("--models", type=str, default=None, help="气象模型")
    parser.add_argument("--output", type=str, default=None, help="输出目录（保存原始JSON）")
    parser.add_argument("--no-metrics", action="store_true", help="跳过指标计算")
    parser.add_argument("--scope", type=str, default=None, help="查询范围（网格模式）")
    parser.add_argument("--html", type=str, default=None,
                        help="生成交互式 HTML 面板到指定路径（失败仅警告，不中断流水线）")
    args = parser.parse_args()

    # 1. 获取数据
    print(f"[PIPELINE] 获取 {args.lat},{args.lon} 数据...", file=sys.stderr)
    data = fetch_data(args.lat, args.lon, args.days, args.models, args.past_days)
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    # 2. 保存原始数据
    if args.output:
        os.makedirs(args.output, exist_ok=True)
        raw_path = os.path.join(args.output, f"weather_{args.lat}_{args.lon}.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print(f"[PIPELINE] 原始数据已保存: {raw_path}", file=sys.stderr)

    # 3. 计算指标（可选）
    metrics_summary = None
    metrics_data = None
    if not args.no_metrics:
        print("[PIPELINE] 计算气象指标...", file=sys.stderr)
        try:
            metrics_summary = compute_metrics(data)
            # compute_metrics --summary 返回摘要
            metrics_data = metrics_summary
        except Exception as e:
            print(f"[PIPELINE] 指标计算失败: {e}", file=sys.stderr)

    # 4. 组装输出
    output = {
        "location": {
            "latitude": data.get("latitude"),
            "longitude": data.get("longitude"),
            "timezone": data.get("timezone"),
            "elevation_m": data.get("elevation"),
        },
        "time_range": {
            "start": times[0] if times else None,
            "end": times[-1] if times else None,
            "count": len(times),
        },
        "extrema": build_extrema(data),
        "metrics": metrics_data if metrics_data else {},
        "weather_summary": summarize_weather(data, metrics_summary) if metrics_summary else {},
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))

    # 5. 生成交互式 HTML 面板（可选，末尾执行；失败只打警告不中断）
    if args.html:
        render_dashboard(data, metrics_summary, args.html)


if __name__ == "__main__":
    main()
