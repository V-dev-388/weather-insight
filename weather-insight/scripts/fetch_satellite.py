#!/usr/bin/env python3
"""fetch_satellite.py — 卫星云图获取引导与云量数据提取。

卫星云图真实图片直链受反爬/动态加载影响难以稳定获取，本脚本提供：
  1. --platform-urls   输出官方卫星云图平台 URL（供浏览器查看真实云图）
  2. --cloud-cover F   从 Open-Meteo JSON 提取分层云量数据（供示意图渲染）
  3. --image-urls      输出可能的云图图片 URL（基于已知格式，可达性不保证）

可视化窗口推荐：用 --cloud-cover 提取的云量数据生成云覆盖示意图（SVG），
同时用 --platform-urls 引导用户查看真实卫星云图。

用法:
    python3 fetch_satellite.py --platform-urls
    python3 fetch_satellite.py --cloud-cover data.json
    python3 fetch_satellite.py --cloud-cover data.json --summary
"""
import argparse
import json
import sys
from datetime import datetime

# 官方卫星云图平台（全球，供浏览器查看真实云图）
PLATFORMS = {
    "nsmc_geofy": {
        "name": "国家卫星气象中心-风云四号天气应用平台",
        "url": "http://rsapp.nsmc.org.cn/geofy/?i18n=zh",
        "desc": "风云四号实时卫星数据，含真彩色/红外/水汽，支持区域选择和时间动画",
        "region": "中国/亚太",
        "note": "最权威的风云四号云图平台，可能需要浏览器访问",
    },
    "nsmc_home": {
        "name": "风云卫星-国家卫星气象中心",
        "url": "https://www.nsmc.org.cn/nsmc/cn/home/index.html",
        "desc": "风云卫星数据门户，含风云二号/三号/四号产品",
        "region": "全球",
    },
    "nmc_satellite": {
        "name": "中央气象台-卫星气象",
        "url": "http://www.nmc.cn/publish/satellite/weather.html",
        "desc": "中央气象台卫星云图产品",
        "region": "中国",
    },
    "qweather_fy4": {
        "name": "和风天气-风云四号卫星云图",
        "url": "https://www.qweather.com/satellite/fengyun4-asia-tc.html",
        "desc": "风云四号亚太真彩色云图，每小时更新",
        "region": "亚太",
    },
    "nmc_typhoon": {
        "name": "中央气象台台风网（含云图叠加）",
        "url": "http://typhoon.nmc.cn/web.html",
        "desc": "台风路径与卫星云图叠加显示",
        "region": "西北太平洋",
    },
    "noaa_goes": {
        "name": "NOAA STAR GOES 卫星云图",
        "url": "https://www.star.nesdis.noaa.gov/GOES/",
        "desc": "GOES-16/17 真彩色/红外云图，覆盖美洲/大西洋",
        "region": "美洲/大西洋",
    },
    "eumetsat": {
        "name": "EUMETSAT 卫星云图",
        "url": "https://www.eumetsat.int/monitoring-clouds",
        "desc": "Meteosat 卫星云图，覆盖欧洲/非洲/大西洋",
        "region": "欧洲/非洲",
    },
    "himawari_nict": {
        "name": "Himawari Monitor (NICT)",
        "url": "https://himawari8.nict.go.jp/",
        "desc": "Himawari-8/9 真彩色实时云图，覆盖亚太/印度洋",
        "region": "亚太/印度洋",
    },
    "nasa_worldview": {
        "name": "NASA Worldview",
        "url": "https://worldview.earthdata.nasa.gov/",
        "desc": "多卫星合成真彩色影像，全球覆盖，支持历史回溯",
        "region": "全球",
    },
}

# 云量变量名（Open-Meteo）
CLOUD_VARS = ["cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"]


def output_platforms():
    """输出官方卫星云图平台 URL 列表。"""
    result = []
    for key, p in PLATFORMS.items():
        result.append({
            "id": key,
            "name": p["name"],
            "url": p["url"],
            "desc": p.get("desc", ""),
            "region": p.get("region", ""),
            "note": p.get("note", ""),
        })
    print(json.dumps({"platforms": result, "count": len(result)}, ensure_ascii=False, indent=2))


def extract_cloud_cover(filepath, summary=False):
    """从 Open-Meteo JSON 提取分层云量数据。"""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if not times:
        print("[ERROR] JSON 中无 hourly.time 数据", file=sys.stderr)
        sys.exit(1)

    # 检查云量变量是否存在
    available = [v for v in CLOUD_VARS if v in hourly]
    if not available:
        print("[ERROR] JSON 中无云量数据（cloud_cover_*），请确认 fetch_openmeteo 获取了云量变量",
              file=sys.stderr)
        sys.exit(1)

    if summary:
        # 摘要模式：各层云量的统计
        def stats(key):
            vals = [v for v in hourly.get(key, []) if v is not None]
            if not vals:
                return None
            avg = sum(vals) / len(vals)
            return {"avg": round(avg, 1), "min": min(vals), "max": max(vals), "unit": "%"}

        result = {
            "location": {
                "latitude": data.get("latitude"),
                "longitude": data.get("longitude"),
            },
            "time_range": {"start": times[0], "end": times[-1], "count": len(times)},
            "cloud_cover_stats": {v: stats(v) for v in available},
            "sky_condition": _classify_sky(hourly.get("cloud_cover", [])),
        }
    else:
        # 完整模式：时间序列
        series = {}
        for v in available:
            series[v] = hourly.get(v, [])
        result = {
            "location": {
                "latitude": data.get("latitude"),
                "longitude": data.get("longitude"),
                "timezone": data.get("timezone"),
            },
            "time": times,
            "cloud_cover": series,
            "variables": available,
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))


def _classify_sky(cloud_cover_vals):
    """根据总云量分类天空状况（供普通人理解）。"""
    valid = [v for v in cloud_cover_vals if v is not None]
    if not valid:
        return "未知"
    avg = sum(valid) / len(valid)
    if avg < 20:
        return "晴朗"
    elif avg < 50:
        return "少云"
    elif avg < 80:
        return "多云"
    else:
        return "阴天"


def main():
    parser = argparse.ArgumentParser(
        description="卫星云图获取引导与云量数据提取"
    )
    parser.add_argument("--platform-urls", action="store_true",
                        help="输出官方卫星云图平台 URL 列表")
    parser.add_argument("--cloud-cover", type=str,
                        help="从 Open-Meteo JSON 文件提取分层云量数据")
    parser.add_argument("--image-urls", action="store_true",
                        help="输出可能的云图图片 URL（可达性不保证）")
    parser.add_argument("--summary", action="store_true",
                        help="仅输出云量摘要统计")
    args = parser.parse_args()

    if args.platform_urls:
        output_platforms()
        return

    if args.cloud_cover:
        extract_cloud_cover(args.cloud_cover, args.summary)
        return

    if args.image_urls:
        # 基于 nmc.cn image 域名的已知格式（可达性不保证，需实际验证）
        today = datetime.utcnow().strftime("%Y/%m/%d")
        urls = {
            "note": "以下 URL 基于已知格式生成，可达性不保证。推荐用 --platform-urls 查看真实云图。",
            "nmc_fy2_blue": f"http://image.nmc.cn/product/{today}/0/SATE_LL_FY2N_SDA_EACH_ACHN_GLB_0000.JPG",
            "nmc_fy4_china": f"http://image.nmc.cn/product/{today}/0/SATE_LL_FY4A_SDA_ACHN_NOM_0000.JPG",
        }
        print(json.dumps(urls, ensure_ascii=False, indent=2))
        return

    # 默认：输出平台 URL
    output_platforms()


if __name__ == "__main__":
    main()
