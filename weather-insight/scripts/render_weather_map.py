#!/usr/bin/env python3
"""render_pro_weather_map.py — 空间气象场专业渲染脚本（支持全球任意区域）。

从多点网格 Open-Meteo JSON 中自动识别格点尺寸、计算中心坐标，
并叠加世界 GeoJSON 边界，生成色彩平滑、等压线与风矢量清晰的高质量专业天气图。
"""
import json
import math
import os
import sys
import argparse
import numpy as np
import matplotlib
matplotlib.use('Agg') # 非交互式后端
import matplotlib.pyplot as plt
matplotlib.rcParams['font.sans-serif'] = ['Heiti TC', 'PingFang HK', 'Arial Unicode MS', 'sans-serif']
matplotlib.rcParams['axes.unicode_minus'] = False
from matplotlib.patches import Polygon
from matplotlib.collections import PatchCollection

def cluster_coords(coords_all, num_points):
    coords_set = sorted(list(set(coords_all)))
    c_min, c_max = coords_set[0], coords_set[-1]
    if c_max == c_min:
        return [c_min]
    n_est = math.sqrt(num_points)
    threshold = (c_max - c_min) / (2.5 * n_est)
    groups = []
    current_group = [coords_set[0]]
    for c in coords_set[1:]:
        if c - current_group[-1] <= threshold:
            current_group.append(c)
        else:
            groups.append(sum(current_group) / len(current_group))
            current_group = [c]
    groups.append(sum(current_group) / len(current_group))
    return groups

def parse_grid_data(grid_data, target_hour):
    """自动解析网格数据，检测形状并返回重构后的矩阵。"""
    # 提取所有格点的经纬度
    lats_all = []
    lons_all = []
    for item in grid_data:
        lats_all.append(item["latitude"])
        lons_all.append(item["longitude"])
        
    # 用自适应聚类算法确定经纬网格线
    lats_unique = sorted(cluster_coords(lats_all, len(grid_data)), reverse=True)
    lons_unique = sorted(cluster_coords(lons_all, len(grid_data)))
    
    rows = len(lats_unique)
    cols = len(lons_unique)
    
    print(f"[INFO] 自动检测网格尺寸: {rows}行 × {cols}列 = {len(grid_data)}个格点")
    
    # 初始化 2D 矩阵
    lat_grid = np.zeros((rows, cols))
    lon_grid = np.zeros((rows, cols))
    p_grid = np.zeros((rows, cols))
    t_grid = np.zeros((rows, cols))
    u_grid = np.zeros((rows, cols))
    v_grid = np.zeros((rows, cols))
    
    # 填充数据
    for item in grid_data:
        lat = item["latitude"]
        lon = item["longitude"]
        
        # 寻找最接近的行列索引
        r = np.argmin([abs(lat - r_lat) for r_lat in lats_unique])
        c = np.argmin([abs(lon - c_lon) for c_lon in lons_unique])
        
        lat_grid[r, c] = lat
        lon_grid[r, c] = lon
        
        hourly = item["hourly"]
        times = hourly["time"]
        
        # 查找时间索引
        try:
            idx = times.index(target_hour)
        except ValueError:
            # 找不到则用第一个或最近一个
            idx = 0
            
        p = hourly["pressure_msl"][idx]
        t = hourly["temperature_2m"][idx]
        ws = hourly["wind_speed_10m"][idx]
        wd = hourly["wind_direction_10m"][idx]
        
        # 转换风向（风来的方向）到矢量（风去的方向）
        rad = math.radians((wd + 180) % 360)
        u = ws * math.sin(rad)
        v = ws * math.cos(rad)
        
        p_grid[r, c] = p
        t_grid[r, c] = t
        u_grid[r, c] = u
        v_grid[r, c] = v
        
    return lat_grid, lon_grid, p_grid, t_grid, u_grid, v_grid, rows, cols

def find_default_geojson():
    """查找默认 GeoJSON 文件（脚本同目录下的 resources/）。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "resources", "world_countries.geojson"),
        os.path.join(script_dir, "..", "resources", "world_countries.geojson"),
    ]
    for path in candidates:
        if os.path.isfile(path) and os.path.getsize(path) > 5000:
            return os.path.normpath(path)
    return None


def main():
    parser = argparse.ArgumentParser(description="空间气象场专业 SVG 渲染器")
    parser.add_argument("--input", type=str, required=True, help="网格气象 JSON 文件路径")
    parser.add_argument("--geojson", type=str, default=None, help="边界 GeoJSON 文件路径（默认自动查找 resources/world_countries.geojson）")
    parser.add_argument("--output", type=str, required=True, help="输出 SVG 图表路径")
    parser.add_argument("--time", type=str, default=None, help="预报时间点（默认取 hourly.time 中间时刻）")
    args = parser.parse_args()

    # 自动查找 GeoJSON
    geojson_path = args.geojson
    if geojson_path is None:
        geojson_path = find_default_geojson()
        if geojson_path:
            print(f"[INFO] 自动找到 GeoJSON: {geojson_path}", file=sys.stderr)
        else:
            print("[ERROR] 未找到 GeoJSON 文件，请用 --geojson 指定或确认 resources/world_countries.geojson 存在",
                  file=sys.stderr)
            sys.exit(1)
    elif not os.path.isfile(geojson_path):
        print(f"[ERROR] GeoJSON 文件不存在: {geojson_path}", file=sys.stderr)
        sys.exit(1)
    
    # 加载网格气象数据
    with open(args.input, "r", encoding="utf-8") as f:
        grid_data = json.load(f)

    # 自动确定目标时间：取第一个格点的 hourly.time 中间时刻
    sample_hourly = grid_data[0].get("hourly", {}) if grid_data else {}
    sample_times = sample_hourly.get("time", [])
    if args.time:
        target_hour = args.time
    elif sample_times:
        target_hour = sample_times[len(sample_times) // 2]
        print(f"[INFO] 自动选择中间时刻: {target_hour}", file=sys.stderr)
    else:
        print("[ERROR] 无法确定预报时刻，请用 --time 指定", file=sys.stderr)
        sys.exit(1)

    # 解析并转换为 2D 矩阵
    lats, lons, pressures, temps, u_wind, v_wind, rows, cols = parse_grid_data(grid_data, target_hour)
    
    # 极值与中心点计算
    p_min, p_max = pressures.min(), pressures.max()
    p_mid = (p_min + p_max) / 2
    lat_min, lat_max = lats.min(), lats.max()
    lon_min, lon_max = lons.min(), lons.max()
    
    # 初始化 Matplotlib 图形
    # 高清，长宽比与 680x400 SVG 适配
    fig, ax = plt.subplots(figsize=(8.0, 5.0), dpi=150)
    
    # 1. 设置海洋背景颜色（深浅适度，提升界限感）
    ax.set_facecolor('#d9eaf7') # 水体蓝色底色
    
    # 2. 绘制 GeoJSON 陆地边界（填充浅灰/白，形成极为清晰的陆海对比）
    print("[INFO] 加载并绘制地理边界...")
    with open(geojson_path, "r", encoding="utf-8") as f:
        geojson = json.load(f)
        
    patches = []
    for feature in geojson.get("features", []):
        geom = feature.get("geometry", {})
        if not geom:
            continue
        g_type = geom.get("type")
        coordinates = geom.get("coordinates", [])
        
        if g_type == "Polygon":
            polygons = [coordinates]
        elif g_type == "MultiPolygon":
            polygons = coordinates
        else:
            polygons = []
            
        for poly in polygons:
            for ring in poly:
                # GeoJSON 坐标可能嵌套
                # 只保留在显示区域及周边的多边形以优化渲染
                ring_coords = []
                for pt in ring:
                    if isinstance(pt[0], list):
                        continue # 防御嵌套结构
                    ring_coords.append((pt[0], pt[1]))
                if ring_coords:
                    patches.append(Polygon(ring_coords, closed=True))
                    
    # 陆地填充为 #fafafa 极轻灰，边界使用 #475569 石板灰，线条设为 0.8
    # 这样在水体底色上会立刻呈现出极为清晰明显的陆地版图界限！
    pc = PatchCollection(patches, facecolor='#fafafa', edgecolor='#475569', linewidth=0.8, alpha=1.0, zorder=1)
    ax.add_collection(pc)
    
    # 3. 绘制平滑的气压场色彩渐变层 (contourf)
    # 限制气压渲染在陆地上，采用 alpha 半透明，让下层底图清晰可见
    levels_p = np.linspace(p_min, p_max, 40)
    cf = ax.contourf(lons, lats, pressures, levels=levels_p, cmap='coolwarm', alpha=0.60, zorder=2)
    
    # 4. 绘制等压线 (contour) 并标注数值
    isobars = ax.contour(lons, lats, pressures, colors='#1e3a8a', linewidths=0.9, alpha=0.75, zorder=3)
    ax.clabel(isobars, inline=True, fontsize=7.5, fmt='%.1f')
    
    # 5. 绘制风场矢量箭头 (quiver)
    # 限制 zorder 在最上层
    q = ax.quiver(lons, lats, u_wind, v_wind, color='#0f172a', 
                  scale=100, scale_units='height', width=0.0035, headwidth=4.5, headlength=5, zorder=4)
    # 增加风速参考图例
    ax.quiverkey(q, 0.85, 0.04, 5, '5 m/s', labelpos='E', coordinates='figure', fontproperties={'size': 8.5, 'weight': 'bold'})
    
    # 6. 自动检测 H/L 气压中心并标注
    for r in range(1, rows-1):
        for c in range(1, cols-1):
            p = pressures[r, c]
            neighbors = [
                pressures[r-1, c-1], pressures[r-1, c], pressures[r-1, c+1],
                pressures[r, c-1],                      pressures[r, c+1],
                pressures[r+1, c-1], pressures[r+1, c], pressures[r+1, c+1]
            ]
            if p > max(neighbors) and p > p_mid:
                # 高气压中心
                ax.plot(lons[r, c], lats[r, c], 'ro', markersize=3, zorder=5)
                ax.text(lons[r, c], lats[r, c] + 0.15, 'H', color='#dc2626', fontsize=11, fontweight='bold', ha='center', va='bottom', zorder=5)
            elif p < min(neighbors) and p < p_mid:
                # 低气压中心
                ax.plot(lons[r, c], lats[r, c], 'bo', markersize=3, zorder=5)
                ax.text(lons[r, c], lats[r, c] + 0.15, 'L', color='#2563eb', fontsize=11, fontweight='bold', ha='center', va='bottom', zorder=5)
                
    # 7. 设置边界与细节样式
    # 将显示界限裁剪至网格四周的实际范围
    ax.set_xlim(lon_min, lon_max)
    ax.set_ylim(lat_min, lat_max)
    ax.set_xlabel('经度 Longitude (°E)', fontsize=9.5, fontweight='bold', labelpad=4)
    ax.set_ylabel('纬度 Latitude (°N)', fontsize=9.5, fontweight='bold', labelpad=4)
    ax.tick_params(labelsize=8.5)
    ax.grid(True, linestyle=':', color='#64748b', alpha=0.4, zorder=1)
    
    # 添加右侧色标条
    cbar = fig.colorbar(cf, ax=ax, pad=0.03, shrink=0.85)
    cbar.set_label('海平面气压 Sea Level Pressure (hPa)', fontsize=9, fontweight='bold', labelpad=6)
    cbar.ax.tick_params(labelsize=8)
    
    # 绘制气象分析地图标题
    plt.title(f'区域空间气象分析天气图\n时间: {args.time} (UTC/Local)', 
              fontsize=11, fontweight='bold', pad=10)
    
    # 保存为 SVG 文件
    plt.savefig(args.output, format='svg', bbox_inches='tight')
    plt.close()
    
    print(f"[SUCCESS] 空间天气图 SVG 渲染成功: {args.output}")

if __name__ == "__main__":
    main()
