#!/usr/bin/env python3
"""fetch_openmeteo.py — Open-Meteo 气象数据获取与解析。

数据源: Open-Meteo (https://open-meteo.com) 非商业免费、无需 API Key
内置模型: GFS / ECMWF IFS / CMA GRAPES / ICON / JMA 等

=== 单点模式 ===
    python3 fetch_openmeteo.py --lat 39.9 --lon 116.4             # 智能获取
    python3 fetch_openmeteo.py --lat 39.9 --lon 116.4 --summary   # 摘要
    python3 fetch_openmeteo.py --lat 39.9 --lon 116.4 --url-only  # 仅URL
    python3 fetch_openmeteo.py --parse data.json --summary        # 解析

=== 网格模式（用于地图天气图） ===
    python3 fetch_openmeteo.py --lat 35 --lon 115 --grid 7x5 --grid-step 5 --url-only
    # 在中心(35N,115E)生成 7行×5列 间距5°的网格，输出多坐标合并API URL

=== 响应缓存 ===
    默认开启：相同参数 1 小时内重复查询直接读 ~/.cache/weather_insight/ 缓存，
    秒回且不打网络，stderr 提示 [CACHE] hit；--no-cache 绕过读取也不写入；
    --url-only / --parse 不涉及缓存；缓存异常自动静默降级为直连。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse

BASE_URL = "https://api.open-meteo.com/v1/forecast"

# 响应磁盘缓存：相同参数一小时内重复查询直接读缓存，秒回且不打网络。
# 目录固定在用户缓存区（绝不写入技能目录）；TTL 按缓存文件的 mtime 判定；
# 缓存任何异常（权限/磁盘满/损坏）一律静默降级为正常直连，不引入新故障点。
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "weather_insight")
CACHE_TTL_SECONDS = 3600


def _cache_key(lat, lon, days, past_days, models,
               scope=None, grid=None, grid_step=None, grid_vars=None):
    """缓存键 = 规范化参数（lat/lon 四舍五入 4 位）拼接后的 sha1。"""
    parts = [
        f"lat={round(lat or 0.0, 4)}",
        f"lon={round(lon or 0.0, 4)}",
        f"days={days}",
        f"past_days={past_days}",
        f"models={models or ''}",
        f"scope={scope or ''}",
        f"grid={grid or ''}",
        f"grid_step={grid_step}",
        f"grid_vars={grid_vars or ''}",
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _cache_path(key):
    return os.path.join(CACHE_DIR, key + ".json")


def _cache_load(key):
    """命中且未过 TTL 返回缓存的响应数据；过期或损坏则静默删除并返回 None。

    任何异常都静默吞掉并返回 None（调用方随即走正常直连），
    保证缓存永远不会成为新的故障点。
    """
    path = _cache_path(key)
    try:
        if not os.path.isfile(path):
            return None
        if time.time() - os.path.getmtime(path) > CACHE_TTL_SECONDS:
            os.remove(path)
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, (dict, list)):
            raise ValueError("缓存内容不是 Open-Meteo 响应结构")
        return data
    except Exception:
        try:
            if os.path.exists(path):
                os.remove(path)  # 损坏文件静默清除，本次及下次均重新抓取
        except Exception:
            pass
        return None


def _cache_store(key, data):
    """尽力把响应写入缓存；失败静默忽略。

    临时文件 + os.replace 原子替换，并发读方不会读到半截文件。
    """
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _cache_path(key)
        tmp = "%s.tmp.%s" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        pass

# 地面层 hourly 变量（单点完整模式）
SURFACE_VARS = [
    "pressure_msl", "surface_pressure",
    "temperature_2m", "relative_humidity_2m", "dew_point_2m",
    "apparent_temperature",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
    "wind_speed_80m", "wind_direction_80m",
    "precipitation", "rain", "showers", "snowfall",
    "precipitation_probability",
    "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
    "weather_code", "visibility",
]

# 关键气压层
PRESSURE_LEVELS = [1000, 925, 850, 700, 500, 300, 250, 200]
LEVEL_VARS = ["temperature", "relative_humidity", "wind_speed", "wind_direction", "geopotential_height"]

# 网格模式默认变量（精简，适合地图可视化）
DEFAULT_GRID_VARS = "pressure_msl,temperature_2m,wind_speed_10m,wind_direction_10m,cloud_cover,precipitation"

# 可选气象模型
AVAILABLE_MODELS = [
    "best_match", "gfs_seamless", "gfs_hrrr", "ecmwf_ifs025", "ecmwf_aifs025",
    "cma_grapeseamless", "icon_seamless", "icon_global", "jma_seamless",
    "meteofrance_seamless", "ukmo_seamless", "kma_seamless",
]


def build_hourly():
    """构建 hourly 参数列表（地面层 + 多层气压面）。"""
    variables = list(SURFACE_VARS)
    for lv in PRESSURE_LEVELS:
        for v in LEVEL_VARS:
            variables.append(f"{v}_{lv}hPa")
    return variables


def build_params(lat, lon, days=7, models=None, past_days=0):
    """构建 Open-Meteo API 请求参数（单点）。"""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(build_hourly()),
        "timezone": "auto",
        "forecast_days": days,
        "wind_speed_unit": "ms",
        "temperature_unit": "celsius",
        "precipitation_unit": "mm",
    }
    if past_days > 0:
        params["past_days"] = past_days
    if models:
        params["models"] = models
    return params


def build_url(lat, lon, days=7, models=None, past_days=0):
    """生成 Open-Meteo API 完整 URL（单点）。"""
    params = build_params(lat, lon, days, models, past_days)
    return BASE_URL + "?" + urllib.parse.urlencode(params)


def build_grid_coords(lat_center, lon_center, rows, cols, step_deg):
    """生成网格坐标列表。
    rows=行数(纬度方向，-90~+90)，cols=列数(经度方向，-180~+180)
    返回 [(lat, lon), ...] 共 rows×cols 个点。
    """
    coords = []
    lat_start = lat_center + (rows - 1) * step_deg / 2  # 最北的纬度
    lon_start = lon_center - (cols - 1) * step_deg / 2  # 最西的经度
    for r in range(rows):
        lat = round(lat_start - r * step_deg, 4)
        for c in range(cols):
            lon = round(lon_start + c * step_deg, 4)
            coords.append((lat, lon))
    return coords


# 自适应格点密度配置
# scope: 查询范围（单城市/小区域/区域/广域/全球）
# step: 网格间距（度数）
# rows×cols: 推荐网格尺寸
# max_points: 该 scope 的最大格点数（控制 API URL 长度）
ADAPTIVE_DENSITY = {
    "city":     {"step": 0.05, "rows": 5, "cols": 5,  "max_points": 25,
                 "label": "单城市", "desc": "5×5=25点, 0.05°(约5km), 覆盖城市及近郊"},
    "local":    {"step": 0.25, "rows": 5, "cols": 5,  "max_points": 25,
                 "label": "小区域", "desc": "5×5=25点, 0.25°(约25km), 覆盖县市级"},
    "region":   {"step": 1.0,  "rows": 7, "cols": 5,  "max_points": 35,
                 "label": "区域",   "desc": "7×5=35点, 1°, 覆盖省级"},
    "country":  {"step": 2.0,  "rows": 8, "cols": 6,  "max_points": 48,
                 "label": "国家",   "desc": "8×6=48点, 2°, 覆盖国家级"},
    "wide":     {"step": 5.0,  "rows": 8, "cols": 6,  "max_points": 48,
                 "label": "广域",   "desc": "8×6=48点, 5°, 覆盖大区域/洲际"},
    "global":   {"step": 10.0, "rows": 9, "cols": 7,  "max_points": 63,
                 "label": "全球",   "desc": "9×7=63点, 10°, 全球分布"},
}


def adaptive_density_for_scope(scope):
    """根据查询范围返回推荐的网格参数 dict。"""
    return ADAPTIVE_DENSITY.get(scope, ADAPTIVE_DENSITY["region"])


def parse_scope(scope_str):
    """解析用户输入的查询范围（中文/英文）→ ADAPTIVE_DENSITY key。"""
    s = scope_str.lower().strip()
    # 中文
    if s in ("城市", "单城市", "city"):
        return "city"
    if s in ("小区域", "县", "市", "local"):
        return "local"
    if s in ("区域", "省", "region"):
        return "region"
    if s in ("国家", "全国", "country"):
        return "country"
    if s in ("广域", "大区域", "洲", "wide"):
        return "wide"
    if s in ("全球", "世界", "global"):
        return "global"
    return "region"  # 默认


def build_grid_url(lat_center, lon_center, rows, cols, step_deg, days=3,
                   models=None, grid_vars=DEFAULT_GRID_VARS):
    """生成网格多坐标 API URL。
    将 rows×cols 个格点的经纬度合并为一个 API 请求。
    """
    coords = build_grid_coords(lat_center, lon_center, rows, cols, step_deg)
    lats = [str(c[0]) for c in coords]
    lons = [str(c[1]) for c in coords]

    params = {
        "latitude": ",".join(lats),
        "longitude": ",".join(lons),
        "hourly": grid_vars,
        "timezone": "auto",
        "forecast_days": days,
        "wind_speed_unit": "ms",
        "temperature_unit": "celsius",
        "precipitation_unit": "mm",
    }
    if models:
        params["models"] = models

    return BASE_URL + "?" + urllib.parse.urlencode(params)


def fetch_auto(lat, lon, days=7, models=None, past_days=0):
    """智能获取数据：依次尝试多种网络策略，返回 (data, method) 或 (None, None)。"""
    import requests
    params = build_params(lat, lon, days, models, past_days)

    # 第一步：标准 requests 直连
    try:
        resp = requests.get(BASE_URL, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json(), "direct"
    except Exception:
        pass

    # 第二步：绕代理直连
    try:
        session = requests.Session()
        session.trust_env = False
        resp = session.get(BASE_URL, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json(), "direct-no-proxy"
    except Exception:
        pass

    # 第三步：curl 降级（纯标准库 subprocess，无需 requests）
    try:
        url = BASE_URL + "?" + urllib.parse.urlencode(params)
        result = subprocess.run(
            ["curl", "-s", "--max-time", "25", "-H", "Accept: application/json", url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return data, "curl-fallback"
    except Exception:
        pass

    return None, None


def _unit_of(key):
    units = {
        "pressure_msl": "hPa", "surface_pressure": "hPa",
        "temperature_2m": "°C", "wind_speed_10m": "m/s",
        "precipitation": "mm", "cloud_cover": "%",
    }
    return units.get(key, "")


def summarize(data):
    """生成数据摘要。支持单点 dict 和网格数组两种格式。"""
    # 数组格式（网格多坐标返回）
    if isinstance(data, list):
        points = []
        for d in data:
            hourly = d.get("hourly", {})
            times = hourly.get("time", [])
            points.append({
                "lat": d.get("latitude"),
                "lon": d.get("longitude"),
                "timezone": d.get("timezone"),
                "time_count": len(times),
                "time_range": f"{times[0]} ~ {times[-1]}" if times else "N/A",
            })
        return {"type": "grid", "count": len(points), "points": points}

    # 单点 dict
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if not times:
        return {"error": "无 hourly 数据"}

    def extrema(key):
        vals = [v for v in hourly.get(key, []) if v is not None]
        if not vals:
            return None
        return {"min": round(min(vals), 2), "max": round(max(vals), 2), "unit": _unit_of(key)}

    return {
        "location": {"latitude": data.get("latitude"), "longitude": data.get("longitude"),
                      "timezone": data.get("timezone"), "elevation_m": data.get("elevation")},
        "time_range": {"start": times[0], "end": times[-1], "count": len(times)},
        "pressure_levels_hpa": PRESSURE_LEVELS,
        "extrema": {
            "pressure_msl_hpa": extrema("pressure_msl"),
            "temperature_2m_c": extrema("temperature_2m"),
            "wind_speed_10m_ms": extrema("wind_speed_10m"),
            "precipitation_mm": extrema("precipitation"),
            "cloud_cover_pct": extrema("cloud_cover"),
        },
    }


def main():
    parser = argparse.ArgumentParser(
        description="Open-Meteo 气象数据获取（单点/网格模式）"
    )
    # 基础参数
    parser.add_argument("--lat", type=float, help="纬度 (-90~90)")
    parser.add_argument("--lon", type=float, help="经度 (-180~180)")
    parser.add_argument("--days", type=int, default=7, help="预报天数（默认7，网格模式默认3）")
    parser.add_argument("--past-days", type=int, default=0, help="过去天数（默认0）")
    parser.add_argument("--models", type=str, default=None,
                        help=f"气象模型: {', '.join(AVAILABLE_MODELS)}（默认 best_match）")
    # 模式参数
    parser.add_argument("--url-only", action="store_true", help="仅输出 API URL")
    parser.add_argument("--parse", type=str, help="解析已下载的 JSON 文件")
    parser.add_argument("--summary", action="store_true", help="仅输出数据摘要")
    # 网格模式参数
    parser.add_argument("--grid", type=str, help="网格尺寸 ROWSxCOLS（如 7x5），启用网格多坐标模式")
    parser.add_argument("--grid-step", type=float, default=5,
                        help="格点间距（度数，默认5°）")
    parser.add_argument("--grid-vars", type=str, default=DEFAULT_GRID_VARS,
                        help=f"网格变量列表（逗号分隔，默认: {DEFAULT_GRID_VARS}）")
    # 自适应密度参数
    parser.add_argument("--scope", type=str, choices=list(ADAPTIVE_DENSITY.keys()),
                        help=f"查询范围（自适应网格密度）: {', '.join(ADAPTIVE_DENSITY.keys())}")
    parser.add_argument("--show-density", action="store_true",
                        help="显示自适应密度配置表")
    parser.add_argument("--no-cache", action="store_true",
                        help="绕过响应缓存：不读取也不写入（默认启用 1 小时磁盘缓存）")
    args = parser.parse_args()

    # 模式: 显示自适应密度配置
    if args.show_density:
        print("自适应密度配置（按查询范围）:\n")
        print(f"{'Scope':<10} {'Step':<6} {'Grid':<8} {'Points':<8} {'Coverage':<12} {'Description'}")
        print("-" * 80)
        for key, cfg in ADAPTIVE_DENSITY.items():
            print(f"{key:<10} {cfg['step']:>5}°  {cfg['rows']}x{cfg['cols']:<5} "
                  f"{cfg['rows']*cfg['cols']:<8} ~{cfg['step']*cfg['cols']:.1f}°×{cfg['step']*cfg['rows']:.1f}°    {cfg['desc']}")
        return

    # 模式: 解析已有 JSON 文件
    if args.parse:
        with open(args.parse, "r", encoding="utf-8") as f:
            data = json.load(f)
        output = summarize(data) if args.summary else data
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    # 模式: 网格获取（支持 --scope 自适应或 --grid/--grid-step 手动）
    if args.grid or args.scope:
        # 如果指定了 --scope 且没指定 --grid，自动用自适应配置
        if args.scope and not args.grid:
            cfg = adaptive_density_for_scope(args.scope)
            rows, cols, step_deg = cfg["rows"], cfg["cols"], cfg["step"]
            print(f"[INFO] 自适应密度 [{args.scope}]: {rows}×{cols}, 间距 {step_deg}° "
                  f"({cfg['desc']})", file=sys.stderr)
        else:
            parts = args.grid.split("x")
            if len(parts) != 2:
                print("[ERROR] --grid 格式必须为 ROWSxCOLS（如 7x5）", file=sys.stderr); sys.exit(1)
            try:
                rows, cols = int(parts[0]), int(parts[1])
            except ValueError:
                print("[ERROR] --grid 格式必须为整数（如 7x5）", file=sys.stderr); sys.exit(1)
            if rows < 2 or cols < 2 or rows > 20 or cols > 20:
                print("[ERROR] 网格尺寸应在 2~20 之间", file=sys.stderr); sys.exit(1)
            step_deg = args.grid_step

        lat_c = args.lat or 35
        lon_c = args.lon or 115
        if not (-90 <= lat_c <= 90):
            print("[ERROR] 纬度必须在 -90 到 90 之间", file=sys.stderr); sys.exit(1)
        if not (-180 <= lon_c <= 180):
            print("[ERROR] 经度必须在 -180 到 180 之间", file=sys.stderr); sys.exit(1)

        days = args.days if args.days != 7 else 3  # 网格默认3天
        coords = build_grid_coords(lat_c, lon_c, rows, cols, step_deg)
        print(f"[INFO] 网格 {rows}×{cols}={len(coords)} 点, "
              f"中心 ({lat_c},{lon_c}), 间距 {step_deg}°, "
              f"范围: lat [{coords[-1][0]},{coords[0][0]}], "
              f"lon [{min(c[1] for c in coords)},{max(c[1] for c in coords)}]",
              file=sys.stderr)
        url = build_grid_url(lat_c, lon_c, rows, cols, step_deg,
                             days=days, models=args.models, grid_vars=args.grid_vars)
        print(url)
        print("\n[提示] 用 WebFetch 工具访问上述 URL 获取网格 JSON，保存后用 --parse 解析",
              file=sys.stderr)
        return

    # 单点模式：需要 lat/lon
    if args.lat is None or args.lon is None:
        print("[ERROR] 需要 --lat 和 --lon 参数", file=sys.stderr)
        sys.exit(1)
    if not (-90 <= args.lat <= 90):
        print("[ERROR] 纬度必须在 -90 到 90 之间", file=sys.stderr); sys.exit(1)
    if not (-180 <= args.lon <= 180):
        print("[ERROR] 经度必须在 -180 到 180 之间", file=sys.stderr); sys.exit(1)
    if args.models and args.models not in AVAILABLE_MODELS:
        print(f"[ERROR] 未知模型: {args.models}", file=sys.stderr); sys.exit(1)

    if args.url_only:
        print(build_url(args.lat, args.lon, args.days, args.models, args.past_days))
        return

    # 默认: 智能获取（带磁盘缓存；--no-cache 或缓存异常时自动直连）
    ckey = None
    if not args.no_cache:
        ckey = _cache_key(args.lat, args.lon, args.days, args.past_days, args.models,
                          scope=args.scope, grid=args.grid,
                          grid_step=args.grid_step, grid_vars=args.grid_vars)
        cached = _cache_load(ckey)
        if cached is not None:
            print("[CACHE] hit", file=sys.stderr)
            output = summarize(cached) if args.summary else cached
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return
    print(f"[INFO] 获取 {args.lat},{args.lon} 未来{args.days}天气象数据...", file=sys.stderr)
    data, method = fetch_auto(args.lat, args.lon, args.days, args.models, args.past_days)
    if data is not None:
        print(f"[INFO] 获取成功（{method}）", file=sys.stderr)
        if ckey is not None:
            _cache_store(ckey, data)
        output = summarize(data) if args.summary else data
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        url = build_url(args.lat, args.lon, args.days, args.models, args.past_days)
        print(url)
        print("\n[提示] 脚本直连失败。请用 WebFetch 工具访问上述 URL 获取 JSON，"
              "保存为文件后用 --parse 解析", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
