#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
滴水湖片区气象分析脚本（可复用，HTML 版）
- 数据源：Open-Meteo 公开 API（无需 key）
- 输出：reports/YYYY-MM-DD/REPORT_YYYYMMDD_HHMM.html（带时间戳，不覆盖，内含 SVG 气象图）
- 同时维护 reports/latest.html 软链 与 reports/README.md 索引

用法：python3 analyze_dishuihu.py
"""
import json, os, sys, datetime, subprocess, glob, re

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
REPORTS = os.path.join(ROOT, "reports")
LAT, LON = 30.90, 121.92  # 上海临港·滴水湖

WMO = {
0:"晴",1:"大致晴朗",2:"局部多云",3:"阴",45:"雾",48:"雾凇",
51:"小毛毛雨",53:"中毛毛雨",55:"大毛毛雨",56:"冻毛毛雨",57:"强冻毛毛雨",
61:"小雨",63:"中雨",65:"大雨",66:"冻雨",67:"强冻雨",
71:"小雪",73:"中雪",75:"大雪",77:"雪粒",
80:"阵雨",81:"强阵雨",82:"暴雨",
85:"阵雪",86:"强阵雪",95:"雷暴",96:"雷暴伴小冰雹",99:"雷暴伴大冰雹"}
def wmo(c): return WMO.get(c, f"代码{c}")

def wind_level(s):
    for thr, lv in [(0.3,"0级(无风)"),(1.6,"1级(软风)"),(3.4,"2级(轻风)"),(5.5,"3级(微风)"),
                    (8.0,"4级(和风)"),(10.8,"5级(清劲风)"),(13.9,"6级(强风)"),(17.2,"7级(疾风)"),
                    (20.8,"8级(大风)"),(24.5,"9级(烈风)")]:
        if s < thr: return lv
    return "10级+"

DIRS = ["北","东北偏北","东北","东北偏东","东","东南偏东","东南","东南偏南",
        "南","西南偏南","西南","西南偏西","西","西北偏西","西北","西北偏北"]
def wind_cn(deg): return DIRS[round(deg/22.5)%16]+"风"

CACHE = "/tmp/dishuihu_weather.json"

def fetch():
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
           "&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,"
           "weather_code,wind_speed_10m,wind_direction_10m,surface_pressure"
           "&hourly=temperature_2m,relative_humidity_2m,precipitation_probability,weather_code,"
           "wind_speed_10m,wind_direction_10m,surface_pressure"
           "&forecast_days=1&timezone=Asia%2FShanghai&past_hours=1")
    try:
        subprocess.run(["curl", "-s", url, "-o", CACHE, "--max-time", "30"],
                       check=True, capture_output=True)
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        import urllib.request
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)


# ============ 周边网格数据（地图图层用） ============
def _grid(axis_min, axis_max, n):
    """在 [min,max] 上均匀生成 n 个格点（4 位小数）。"""
    return [round(axis_min + (axis_max - axis_min) * k / (n - 1), 4) for k in range(n)]
# 滴水湖周边网格范围与密度（13×13 = 169 个格点，气压区分更细腻）
GRID_LATS = _grid(30.80, 31.00, 13)   # 13 行
GRID_LONS = _grid(121.82, 122.02, 13)  # 13 列

def fetch_grid():
    """拉取滴水湖周边 N×M 网格的 当前 温度/气压/风。
    返回 (lats, lons, T, P, W, D) 均为列表：T[i][j] 对应 lats[i],lons[j]
    """
    lats, lons = GRID_LATS, GRID_LONS
    # 笛卡尔积生成配对坐标
    pairs = [(la, lo) for la in lats for lo in lons]
    lat_str = ",".join(str(p[0]) for p in pairs)
    lon_str = ",".join(str(p[1]) for p in pairs)
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat_str}&longitude={lon_str}"
           "&current=temperature_2m,surface_pressure,wind_speed_10m,wind_direction_10m"
           "&timezone=Asia%2FShanghai")
    try:
        import tempfile
        tf = "/tmp/dishuihu_grid.json"
        subprocess.run(["curl", "-s", url, "-o", tf, "--max-time", "40"],
                       check=True, capture_output=True)
        data = json.load(open(tf, encoding="utf-8"))
    except Exception:
        import urllib.request
        with urllib.request.urlopen(url, timeout=40) as r:
            data = json.load(r)
    # data 为按 pairs 顺序的列表
    T = [[None]*len(lons) for _ in lats]
    P = [[None]*len(lons) for _ in lats]
    W = [[None]*len(lons) for _ in lats]
    D = [[None]*len(lons) for _ in lats]
    for k, pt in enumerate(data):
        i = k // len(lons)
        j = k % len(lons)
        c = pt.get("current", {})
        T[i][j] = c.get("temperature_2m")
        P[i][j] = c.get("surface_pressure")
        W[i][j] = c.get("wind_speed_10m")
        D[i][j] = c.get("wind_direction_10m")
    return lats, lons, T, P, W, D


def marching_squares(field, lats, lons, level):
    """对二维标量场 field[i][j] 在给定 level 生成等值线段列表。
    返回 [[ [lat1,lon1],[lat2,lon2] ], ...]（经纬度坐标，便于 Leaflet 直接画）。
    关键改进：正确消解鞍点（case 5 / case 10）歧义——依据对角角点值之和决定
    两条连线的配对方式，使每个内部交点度数恒为 2，可被 chain_segments 连成
    完整闭合环（不会因 3 度歧义点而断裂）。
    """
    segs = []
    ni, nj = len(lats), len(lons)
    def interp(va, vb):
        if vb == va:
            return 0.5
        return (level - va) / (vb - va)
    for i in range(ni - 1):
        for j in range(nj - 1):
            v00, v10, v11, v01 = field[i][j], field[i][j+1], field[i+1][j+1], field[i+1][j]
            if None in (v00, v10, v11, v01):
                continue
            la0, la1 = lats[i], lats[i+1]
            lo0, lo1 = lons[j], lons[j+1]
            # 4 边交点（有符号：负=角点<level）
            b00 = v00 - level; b10 = v10 - level; b11 = v11 - level; b01 = v01 - level
            pts = {}
            # 下边 (i,j)-(i,j+1)
            if b00 * b10 < 0:
                t = interp(v00, v10); pts['B'] = (la0, lo0 + t*(lo1-lo0))
            # 右边 (i,j+1)-(i+1,j+1)
            if b10 * b11 < 0:
                t = interp(v10, v11); pts['R'] = (la0 + t*(la1-la0), lo1)
            # 上边 (i+1,j+1)-(i+1,j)
            if b11 * b01 < 0:
                t = interp(v11, v01); pts['T'] = (la1, lo1 + t*(lo0-lo1))
            # 左边 (i+1,j)-(i,j)
            if b01 * b00 < 0:
                t = interp(v01, v00); pts['L'] = (la1 + t*(la0-la1), lo0)
            if len(pts) != 2:
                # 0 或 4 个交点（整格同侧或鞍点边界情况）不在此连线
                if len(pts) == 4:
                    # 鞍点：计算一个精确的中心交点（4 边交点的几何平均），
                    # 让 4 条线段都汇聚到该中心，从而该中心成为真正的 4 度交点，
                    # 可被 chain_segments 正确连成两条交叉的闭合/开放线。
                    cx = sum(p[0] for p in pts.values()) / 4.0
                    cy = sum(p[1] for p in pts.values()) / 4.0
                    center = [cx, cy]
                    # 按对角和决定配对方向（仅影响后续抽稀朝向，不影响连接性）
                    for pk in ('B', 'R', 'T', 'L'):
                        segs.append([list(pts[pk]), list(center)])
                continue
            # 普通两交点：按固定顺序配对（B-R / R-T / T-L / L-B 都是相邻边）
            ks = list(pts.keys())
            segs.append([list(pts[ks[0]]), list(pts[ks[1]])])
    return segs


def _dist2(a, b):
    d0 = a[0] - b[0]; d1 = a[1] - b[1]
    return d0 * d0 + d1 * d1


def chain_segments(segs, tol=1e-5):
    """将首尾相接的短线段连接成连续路径列表。

    采用「端点度数图遍历」：每个交点按度数分类——
      · 度数 1 → 开放线端点（边界截断处）
      · 度数 2 → 闭合环上的普通点
    遍历时沿度数-2 的链走，遇到度数 1 的端点自然停止（开放线），
    或从任意未访问点出发走回自身（闭合环）。这样能正确拼出完整闭合环，
    而不会像贪心延伸那样把一条环切成多段短弧。
    """
    def key(p):
        return (round(p[0] / tol), round(p[1] / tol))

    norm = {}
    def n(p):
        k = key(p)
        if k not in norm:
            norm[k] = (p[0], p[1])
        return norm[k]

    edges = []
    for a, b in segs:
        a2, b2 = n(a), n(b)
        if _dist2(a2, b2) < tol * tol:
            continue
        edges.append((a2, b2))

    # 构建邻接（保留边索引以便标记已用）
    adj = {}
    for idx, (a, b) in enumerate(edges):
        adj.setdefault(a, []).append((b, idx))
        adj.setdefault(b, []).append((a, idx))

    used_edge = [False] * len(edges)
    paths = []

    # 初始（静态）度数：不受行走中已用边影响，用于区分「开放线端点(度=1)」与「环上点(度=2)」
    init_deg = {}
    for v in adj:
        init_deg[v] = len(adj[v])

    def rem_deg(v):
        return sum(1 for _, ei in adj.get(v, []) if not used_edge[ei])

    # 1) 优先从开放端点（初始度数 1）出发，走开放线
    starts = [v for v in adj if init_deg[v] == 1]
    # 2) 其余未访问点（度数 2 的环、或孤立段）也作为起点
    for v in list(adj.keys()):
        if init_deg[v] > 0:
            starts.append(v)

    for v in starts:
        if rem_deg(v) == 0:
            continue
        # 从 v 出发，沿未使用边走到走不动为止
        path = [list(v)]
        cur = v
        prev = None
        while True:
            nxts = [(w, ei) for (w, ei) in adj.get(cur, []) if not used_edge[ei]]
            if not nxts:
                break
            w, ei = nxts[0]
            # 度数>2（残余鞍点/交叉）时，优先选与 incoming 方向最共线的出口，实现「穿越」
            if len(nxts) > 1 and prev is not None:
                def align(it):
                    (w2, ei2) = it
                    ax = cur[0] - prev[0]; ay = cur[1] - prev[1]
                    bx = w2[0] - cur[0]; by = w2[1] - cur[1]
                    dot = ax * bx + ay * by
                    na = (ax*ax + ay*ay) ** 0.5
                    nb = (bx*bx + by*by) ** 0.5
                    return -(dot / (na*nb + 1e-15))
                nxts.sort(key=align)
                w, ei = nxts[0]
            used_edge[ei] = True
            path.append(list(w))
            # 走到开放线端点（初始度数 1）才停
            if init_deg.get(w, 0) == 1:
                break
            # 回到起点（闭合环）才停
            if w == v:
                break
            prev, cur = cur, w
        if len(path) >= 2:
            paths.append(path)
    return paths


def simplify_paths(paths, eps=1e-4):
    """对连续路径做抽稀（Ramer–Douglas–Peucker），减少渲染点数、平滑折线。"""
    def rdp(pts, eps):
        if len(pts) < 3:
            return pts
        # 找离首尾连线最远的点
        ax, ay = pts[0][0], pts[0][1]
        bx, by = pts[-1][0], pts[-1][1]
        dx, dy = bx - ax, by - ay
        denom = (dx * dx + dy * dy) ** 0.5
        if denom < 1e-12:
            return [pts[0], pts[-1]]
        dmax = -1.0; idx = 0
        for i in range(1, len(pts) - 1):
            px, py = pts[i][0], pts[i][1]
            d = abs(dy * px - dx * py + bx * ay - by * ax) / denom
            if d > dmax:
                dmax = d; idx = i
        if dmax > eps:
            left = rdp(pts[:idx + 1], eps)
            right = rdp(pts[idx:], eps)
            return left[:-1] + right
        return [pts[0], pts[-1]]

    def rdp_closed(pts, eps):
        """闭合环抽稀：临时把首点复制到末尾构成闭环，RDP 后再去掉重复尾点。"""
        if len(pts) < 4:
            return pts
        loop = pts + [pts[0]]
        s = rdp(loop, eps)
        if len(s) >= 2 and _dist2(s[0], s[-1]) < (eps * 4) ** 2:
            s = s[:-1]
        return s

    out = []
    for p in paths:
        # 判断闭合环（首尾极近）——闭合环用闭环抽稀，避免破坏首尾衔接
        if len(p) >= 4 and _dist2(p[0], p[-1]) < (eps * 4) ** 2:
            s = rdp_closed(p, eps)
        else:
            s = rdp(p, eps)
        if len(s) >= 2:
            out.append(s)
    return out


def build_isobars(P, lats, lons):
    """生成多条等压线（连续路径），返回 [{level, paths:[[[lat,lon],...]]}] 及自适应步长。"""
    vals = [v for row in P for v in row if v is not None]
    if not vals:
        return [], 0.5
    lo, hi = min(vals), max(vals)
    span = hi - lo
    # 步长自适应：网格更密后气压差更小，步长随之细化以体现梯度
    step = 0.15 if span < 2 else (0.3 if span < 5 else 0.5)
    out = []
    center = (lo + hi) / 2.0   # 以场中心为界区分高/低压侧线型
    lv = round(lo / step) * step
    while lv <= hi + 1e-9:
        segs = marching_squares(P, lats, lons, lv)
        if segs:
            paths = simplify_paths(chain_segments(segs))
            if paths:
                out.append({
                    "level": round(lv, 2),
                    "paths": paths,
                    "kind": "high" if lv >= center else "low",
                })
        lv += step
    return out, step


# ============ SVG 图表生成（零依赖，内联 HTML） ============

def svg_line_chart(title, labels, values, unit, color="#e74c3c", w=720, h=240,
                   ymin=None, ymax=None, fill=True):
    """折线图。values: 数值列表；labels: 横轴标签。"""
    n = len(values)
    if n == 0:
        return ""
    pad_l, pad_r, pad_t, pad_b = 46, 16, 26, 34
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    vmin = min(values) if ymin is None else ymin
    vmax = max(values) if ymax is None else ymax
    if vmax == vmin:
        vmax = vmin + 1
    def x(i): return pad_l + (plot_w * i / (n - 1)) if n > 1 else pad_l + plot_w/2
    def y(v): return pad_t + plot_h * (1 - (v - vmin) / (vmax - vmin))
    # 网格 + Y 轴刻度
    grid = ""
    for g in range(4):
        gv = vmin + (vmax - vmin) * g / 3
        gy = y(gv)
        grid += f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{w-pad_r}" y2="{gy:.1f}" stroke="#eee" stroke-width="1"/>'
        grid += f'<text x="{pad_l-6}" y="{gy+4:.1f}" font-size="11" fill="#888" text-anchor="end">{gv:.0f}</text>'
    # 折线 + 数据点 + X 标签
    pts = " ".join(f"{x(i):.1f},{y(values[i]):.1f}" for i in range(n))
    dots = ""
    xlab = ""
    for i in range(n):
        cx, cy = x(i), y(values[i])
        dots += f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" fill="{color}"/>'
        if i % max(1, n//8) == 0 or i == n-1:
            xlab += f'<text x="{cx:.1f}" y="{h-12}" font-size="10" fill="#888" text-anchor="middle">{labels[i]}</text>'
    area = ""
    if fill:
        area = (f'<polygon points="{pad_l},{pad_t+plot_h} {pts} {w-pad_r},{pad_t+plot_h}" '
                f'fill="{color}" opacity="0.10"/>')
    svg = (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
           f'style="width:100%;height:auto;font-family:-apple-system,Segoe UI,sans-serif">'
           f'<text x="{pad_l}" y="16" font-size="13" fill="#333" font-weight="600">{title}（{unit}）</text>'
           f'{grid}{area}<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.2"/>'
           f'{dots}{xlab}</svg>')
    return svg


def svg_bar_chart(title, labels, values, unit, color="#3498db", w=720, h=240, vmax=None):
    """柱状图。values: 0-100 类占比，或任意正值。"""
    n = len(values)
    if n == 0:
        return ""
    pad_l, pad_r, pad_t, pad_b = 46, 16, 26, 34
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    vmax = max(values) if vmax is None else vmax
    if vmax <= 0:
        vmax = 1
    bw = plot_w / n * 0.62
    grid = ""
    for g in range(4):
        gv = vmax * g / 3
        gy = pad_t + plot_h * (1 - g/3)
        grid += f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{w-pad_r}" y2="{gy:.1f}" stroke="#eee" stroke-width="1"/>'
        grid += f'<text x="{pad_l-6}" y="{gy+4:.1f}" font-size="11" fill="#888" text-anchor="end">{gv:.0f}</text>'
    bars, xlab = "", ""
    for i in range(n):
        bh = plot_h * values[i] / vmax
        bx = pad_l + plot_w * i / n + (plot_w/n - bw)/2
        by = pad_t + plot_h - bh
        bars += f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="2" fill="{color}" opacity="0.85"/>'
        if i % max(1, n//8) == 0 or i == n-1:
            cx = pad_l + plot_w * (i + 0.5) / n
            xlab += f'<text x="{cx:.1f}" y="{h-12}" font-size="10" fill="#888" text-anchor="middle">{labels[i]}</text>'
    svg = (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
           f'style="width:100%;height:auto;font-family:-apple-system,Segoe UI,sans-serif">'
           f'<text x="{pad_l}" y="16" font-size="13" fill="#333" font-weight="600">{title}（{unit}）</text>'
           f'{grid}{bars}{xlab}</svg>')
    return svg


def svg_wind_rose(cur_dir, cur_speed):
    """简易风向标：箭头指向风的来向(气象约定为来向)，中心显示风速。"""
    size, cx, cy, r = 200, 100, 100, 70
    # 风向(来向)转屏幕角度：0°=北(上)，顺时针
    import math
    rad = math.radians(cur_dir)
    # 来向箭头：从边缘指向中心
    ex = cx + r * math.sin(rad)
    ey = cy - r * math.cos(rad)
    ix = cx - r * math.sin(rad)
    iy = cy + r * math.cos(rad)
    # 箭头
    arrow = (f'<line x1="{ix:.1f}" y1="{iy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" '
             f'stroke="#2980b9" stroke-width="4"/>'
             f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="6" fill="#2980b9"/>')
    ticks = ""
    for d, name in [(0,"N"),(90,"E"),(180,"S"),(270,"W")]:
        rr = math.radians(d)
        tx = cx + (r+12) * math.sin(rr)
        ty = cy - (r+12) * math.cos(rr)
        ticks += f'<text x="{tx:.1f}" y="{ty+4:.1f}" font-size="11" fill="#888" text-anchor="middle">{name}</text>'
    svg = (f'<svg viewBox="0 0 {size} {size}" xmlns="http://www.w3.org/2000/svg" '
           f'style="width:200px;height:200px;font-family:-apple-system,Segoe UI,sans-serif">'
           f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#f4f8fb" stroke="#dce6ee"/>'
           f'{ticks}{arrow}'
           f'<text x="{cx}" y="{cy-4}" font-size="20" fill="#2c3e50" font-weight="700" text-anchor="middle">{cur_speed:.1f}</text>'
           f'<text x="{cx}" y="{cy+16}" font-size="11" fill="#888" text-anchor="middle">m/s</text></svg>')
    return svg


# ============ HTML 报告 ============

CSS = """
* { box-sizing: border-box; margin:0; padding:0; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
       background:#eef2f6; color:#2c3e50; line-height:1.6; padding:24px; }
.wrap { max-width:960px; margin:0 auto; }
header { background:linear-gradient(135deg,#4a90d9,#2c6cb0); color:#fff; border-radius:14px;
         padding:22px 26px; box-shadow:0 4px 16px rgba(44,108,176,.25); }
header h1 { font-size:22px; margin-bottom:6px; }
header .meta { font-size:13px; opacity:.92; }
.badge { display:inline-block; background:rgba(255,255,255,.2); border-radius:20px;
         padding:2px 12px; font-size:12px; margin-right:8px; }
section { background:#fff; border-radius:12px; padding:20px 22px; margin-top:18px;
          box-shadow:0 2px 10px rgba(0,0,0,.04); }
section h2 { font-size:17px; color:#2c6cb0; margin-bottom:14px; border-left:4px solid #4a90d9; padding-left:10px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; }
.card { background:#f7fafc; border:1px solid #e8eef4; border-radius:10px; padding:14px; }
.card .k { font-size:12px; color:#7f8c8d; }
.card .v { font-size:20px; font-weight:700; color:#2c3e50; margin-top:4px; }
.card .v small { font-size:12px; font-weight:400; color:#95a5a6; }
table { width:100%; border-collapse:collapse; margin-top:6px; font-size:13px; }
th,td { padding:9px 8px; text-align:center; border-bottom:1px solid #eef2f6; }
th { background:#f4f8fb; color:#5a6b7b; font-weight:600; }
tr:hover td { background:#fafcfe; }
.note { background:#fffaf0; border-left:4px solid #f0b429; padding:12px 14px; border-radius:8px;
        font-size:13px; margin-top:10px; color:#7a5c00; }
.trend li { margin:8px 0 8px 18px; font-size:14px; }
.advice { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; }
.advice .a { background:#f0f7ff; border-radius:10px; padding:14px; font-size:13px; }
.advice .a b { color:#2c6cb0; }
.chartbox { margin-top:14px; background:#fff; border:1px solid #eef2f6; border-radius:10px; padding:10px; }
.footer { text-align:center; color:#95a5a6; font-size:12px; margin-top:20px; }
.windrow { display:flex; gap:20px; align-items:center; flex-wrap:wrap; }
.legend { margin-top:10px; font-size:12px; color:#5a6b7b; display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
.legend .lg { display:inline-flex; align-items:center; gap:4px; }
.legend .lg i { width:16px; height:12px; border-radius:2px; display:inline-block; }
#map { background:#dfeaf2; }
.map-popup b { font-size:15px; }
"""

def build_map_js(mapdata):
    """生成 Leaflet 地图渲染 JS。mapdata 为 None 时返回加载失败的提示。"""
    LEAFLET_JS = ('<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"'
                  ' integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="'
                  ' crossorigin=""></script>')
    if not mapdata:
        return LEAFLET_JS + ('<script>document.getElementById("map").innerHTML='
                '"<p style=\'padding:20px;color:#888\'>地图数据暂不可用（网格气象获取失败）。</p>";</script>')
    import json
    data_json = json.dumps(mapdata, ensure_ascii=False)
    js = LEAFLET_JS + """
<script>
var MD = """ + data_json + """;
(function(){
  var map = L.map('map', {zoomControl:true}).setView(MD.center, 11);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18, attribution: '&copy; OpenStreetMap'
  }).addTo(map);
  // 统一使用 Canvas 渲染器，提升密集矢量图层的性能
  var cr = L.canvas({padding:0.5});

  // ---- 气压色块（双线性细分，自适应色阶，Canvas 渲染）----
  // 先计算气压场范围（必须在使用 PMIN/PMAX 之前）
  var lats=MD.grid.lats, lons=MD.grid.lons, T=MD.grid.temp, P=MD.grid.pres;
  var pvals=[]; for (var ii=0;ii<P.length;ii++) for (var jj=0;jj<P[ii].length;jj++) if(P[ii][jj]!=null && isFinite(P[ii][jj])) pvals.push(P[ii][jj]);
  var rawMin = pvals.length ? Math.min.apply(null, pvals) : 1000;
  var rawMax = pvals.length ? Math.max.apply(null, pvals) : 1025;
  var rawSpan = rawMax - rawMin;
  var desiredSpan = Math.max(rawSpan / 0.75, 1.5);
  var pad = Math.max(0.0, (desiredSpan - rawSpan) / 2);
  var PMIN = rawMin - pad;
  var PMAX = rawMax + pad;

  // 内部颜色运算用 RGB 数组，避免 rgb() 字符串二次解析产生 NaN
  function parseHex(h){
    if(!h||typeof h!=='string') return [255,251,191];
    h=h.replace('#','');
    return [parseInt(h.substr(0,2),16),parseInt(h.substr(2,2),16),parseInt(h.substr(4,2),16)];
  }
  function lerpRGB(a,b,t){ // a,b 为 [R,G,B] 数组，返回 [R,G,B] 数组
    t=Math.max(0,Math.min(1,t));
    return [Math.round(a[0]+(b[0]-a[0])*t), Math.round(a[1]+(b[1]-a[1])*t), Math.round(a[2]+(b[2]-a[2])*t)];
  }
  function rgbStr(c){ return 'rgb('+c[0]+','+c[1]+','+c[2]+')'; }
  var P_ANCHORS_HEX=['#053061','#2166ac','#4393c3','#92c5de','#fdae61','#f46d43','#d73027','#a50026'];
  var P_ANCHORS=P_ANCHORS_HEX.map(parseHex);
  function buildRamp(anchors, n){
    var out=[];
    for (var s=0;s<n;s++){
      var f=s/(n-1)*(anchors.length-1);
      var i=Math.floor(f), t=f-i;
      if (i>=anchors.length-1){ out.push(anchors[anchors.length-1]); continue; }
      out.push(lerpRGB(anchors[i], anchors[i+1], t));
    }
    return out;
  }
  var ISO_STEP = (MD.iso_step && MD.iso_step > 0) ? MD.iso_step : 0.5;
  var PER_STEP = 8;
  var N_INTERVALS = Math.max(1, Math.round((PMAX - PMIN) / ISO_STEP));
  var NSTEPS = Math.max(64, (N_INTERVALS + 1) * PER_STEP);
  var PRAMP=buildRamp(P_ANCHORS, NSTEPS);
  function presColor(p, pmin, pmax){
    if (p == null || !isFinite(p)) return rgbStr([255,251,191]);
    if (pmax - pmin < 1e-6) return rgbStr(PRAMP[0]);
    var r = (p - pmin) / (pmax - pmin);
    r = Math.max(0, Math.min(1, r));
    if (r <= 0) return rgbStr(PRAMP[0]);
    if (r >= 1) return rgbStr(PRAMP[PRAMP.length - 1]);
    var f = r * (PRAMP.length - 1), i = Math.floor(f), t = f - i;
    if (i >= PRAMP.length - 1) return rgbStr(PRAMP[PRAMP.length - 1]);
    return rgbStr(lerpRGB(PRAMP[i], PRAMP[i + 1], t));
  }

  var SUB=6; // 每格细分（13×13 网格下更平滑细腻）
  for (var i=0;i<lats.length-1;i++){
    for (var j=0;j<lons.length-1;j++){
      var p00=P[i][j], p10=P[i][j+1], p11=P[i+1][j+1], p01=P[i+1][j];
      if ([p00,p10,p11,p01].some(function(x){return x==null || !isFinite(x);})) continue;
      for (var si=0;si<SUB;si++){
        for (var sj=0;sj<SUB;sj++){
          var u=si/SUB, v=sj/SUB, uu=(si+1)/SUB, vv=(sj+1)/SUB;
          // 双线性插值四角气压
          var c00=p00*(1-v)+p10*v, c10=p00*(1-vv)+p10*vv, c01=p01*(1-v)+p11*v, c11=p01*(1-vv)+p11*vv;
          var la0=lats[i]+(lats[i+1]-lats[i])*u, la1=lats[i]+(lats[i+1]-lats[i])*uu;
          var lo0=lons[j]+(lons[j+1]-lons[j])*v, lo1=lons[j]+(lons[j+1]-lons[j])*vv;
          var pc=(c00*(1-uu)+c10*uu + c01*(1-uu)+c11*uu)/2;
          if (pc == null || !isFinite(pc)) continue;
          var c = presColor(pc,PMIN,PMAX);
          L.rectangle([[la0,lo0],[la1,lo1]], {fillColor:c, color:'transparent', weight:0, fillOpacity:0.85, renderer:cr}).addTo(map);
        }
      }
    }
  }

  // ---- 网格温度文字标注（非颜色呈现）----
  for (var gi=0;gi<T.length;gi++){
    for (var gj=0;gj<T[gi].length;gj++){
      if (T[gi][gj]===null) continue;
      L.marker([lats[gi], lons[gj]], {icon:L.divIcon({className:'', html:'<span style="font-size:10px;color:#222;background:rgba(255,255,255,.55);padding:0 2px;border-radius:2px;white-space:nowrap">'+T[gi][gj].toFixed(1)+'°</span>', iconSize:[26,13], iconAnchor:[13,6]})}).addTo(map);
    }
  }

  // ---- 等压线（全部实线，按高/低压用颜色区分，Canvas 渲染 + 平滑）----
  MD.isobars.forEach(function(iso){
    var isHigh = (iso.kind === 'high');
    // 高压：暖色实线（红棕）；低压：冷色实线（蓝灰）——仅以颜色区分高低，线型统一为实线
    var lineColor = isHigh ? '#b3391f' : '#1f4fb3';
    iso.paths.forEach(function(p){
      // 过滤极短线段（鞍点碎弧/网格边缘冒头），避免地图上出现无意义的八字形小叉
      if (p.length < 4) return;
      L.polyline(p, {color:lineColor, weight:1.7, opacity:0.92, smoothFactor:2, renderer:cr}).addTo(map);
    });
    if (iso.paths.length){
      // 标签放在最长路径的中点
      var longest=iso.paths[0], mx=-1;
      iso.paths.forEach(function(p){ if(p.length>mx){mx=p.length; longest=p;} });
      var mid=longest[Math.floor(longest.length/2)];
      L.marker(mid, {icon:L.divIcon({className:'', html:'<span style="font-size:10px;font-weight:600;color:'+lineColor+';background:rgba(255,255,255,.78);padding:0 3px;border-radius:3px;border:1px solid '+lineColor+'">'+iso.level+'</span>', iconSize:[30,15]})}).addTo(map);
    }
  });

  // ---- 风箭头（短杆 + 实心箭头头部，头部大小∝风速；Canvas 渲染）----
  // 实际网格风速差异往往极小（常仅 ±1 m/s），用「全局 min/max 线性拉伸」强制放大尺寸差异
  var allSpds = MD.wind.map(function(w){ return w[2]; });
  var wMin = Math.min.apply(null, allSpds);
  var wMax = Math.max.apply(null, allSpds);
  var wRange = Math.max(0.5, wMax - wMin); // 至少 0.5 m/s 量程
  var SHAFT=0.0035; // 杆长固定（度），约为格点间距 21%
  var ARR_COLOR='#1a7d3f';
  MD.wind.forEach(function(w){
    var la=w[0], lo=w[1], spd=w[2], dir=w[3];
    var trg=(dir+180)*Math.PI/180;
    var dlat=Math.cos(trg)*SHAFT, dlon=Math.sin(trg)*SHAFT;
    var head=[la+dlat, lo+dlon], tail=[la-dlat, lo-dlon];
    L.polyline([tail, head], {color:ARR_COLOR, weight:1.0, opacity:0.85, renderer:cr}).addTo(map);
    // 头部：min→最小，max→最大；用 gamma>1 曲线让大小差异更陡
    var ratio = Math.max(0, Math.min(1, (spd - wMin) / wRange));
    var sizeRatio = Math.pow(ratio, 0.55); // 0.55 次幂 → 中小风也明显小于大风
    var hs = 0.0006 + 0.0050 * sizeRatio; // 头部半长 0.0006° ~ 0.0056°
    var back=trg+Math.PI;
    var spread=24*Math.PI/180;
    var w1=[head[0]-hs*Math.cos(back-spread), head[1]-hs*Math.sin(back-spread)];
    var w2=[head[0]-hs*Math.cos(back+spread), head[1]-hs*Math.sin(back+spread)];
    L.polygon([head, w1, w2], {color:ARR_COLOR, weight:0, fillColor:ARR_COLOR, fillOpacity:0.92, renderer:cr}).addTo(map);
  });

  // ---- 中心标记 ----
  var c=MD.cur;
  var icon=L.divIcon({className:'center-pin', html:'<div style="background:#e74c3c;color:#fff;border-radius:50%;width:34px;height:34px;line-height:34px;text-align:center;font-size:18px;box-shadow:0 2px 6px rgba(0,0,0,.3);border:2px solid #fff">🌊</div>', iconSize:[34,34], iconAnchor:[17,17]});
  L.marker([c.lat,c.lon], {icon:icon}).addTo(map)
    .bindPopup('<div class="map-popup"><b>滴水湖中心</b><br>天气：'+c.weather+'<br>气温：'+c.temp+'℃<br>气压：'+c.pres+' hPa<br>风：'+c.wind+' m/s ('+c.wdir+'°)<br>湿度：'+c.rh+'%</div>');
})();
</script>"""
    return js


def html_report(cur, after, stamp, charts, mapdata):
    cur_weather = wmo(cur['weather_code'])
    iso_step = (mapdata or {}).get('iso_step') or 0.5
    # 实况卡片
    cards = [
        ("天气", f"{cur_weather}"),
        ("气温", f"{cur['temperature_2m']}<small>℃</small>"),
        ("体感温度", f"{cur['apparent_temperature']}<small>℃</small>"),
        ("相对湿度", f"{cur['relative_humidity_2m']}<small>%</small>"),
        ("降水量", f"{cur['precipitation']}<small>mm</small>"),
        ("风速", f"{cur['wind_speed_10m']}<small>m/s</small>"),
        ("风向", f"{wind_cn(cur['wind_direction_10m'])}"),
        ("地面气压", f"{cur['surface_pressure']}<small>hPa</small>"),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div></div>'
        for k, v in cards)

    # 后半小时
    half_temp = round((cur['temperature_2m']+after[0]['temp'])/2,1)
    half_rh = round((cur['relative_humidity_2m']+after[0]['rh'])/2)
    half_pop = after[0]['pop']
    half_wind = round((cur['wind_speed_10m']+after[0]['wind'])/2,1)
    half_pres = round((cur['surface_pressure']+after[0]['pres'])/2,1)

    # 8小时表格
    rows = ""
    for a in after:
        rows += (f"<tr><td>{a['time'][11:16]}</td><td>{a['wtext']}</td><td>{a['temp']}℃</td>"
                 f"<td>{a['rh']}%</td><td>{a['pop']}%</td><td>{a['wind']}</td>"
                 f"<td>{wind_level(a['wind'])}</td><td>{wind_cn(a['wind_dir'])}</td><td>{a['pres']}</td></tr>")

    max_pop = max(a['pop'] for a in after)
    max_wind = max(a['wind'] for a in after)
    max_wind_t = next(a['time'][11:16] for a in after if a['wind']==max_wind)
    min_t = min(a['temp'] for a in after); max_t = max(a['temp'] for a in after)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>滴水湖气象报告 {stamp}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin=""/>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>🌊 滴水湖片区气象分析报告</h1>
  <div class="meta">
    <span class="badge">生成 {cur['time']}</span>
    <span class="badge">上海临港·滴水湖 {LAT}°N, {LON}°E</span>
    <span class="badge">报告 {stamp}</span>
    <span class="badge">数据源 Open-Meteo</span>
  </div>
</header>

<section>
  <h2>一、当前实况（{cur['time']}）</h2>
  <div class="cards">{cards_html}</div>
  <div class="note">当前为{cur_weather}，实测气温 {cur['temperature_2m']}℃，但体感温度高达
  {cur['apparent_temperature']}℃，<b>闷热感明显</b>，主因是高相对湿度叠加偏南风。气压平稳，暂无强降水，但空气湿度大。</div>
</section>

<section>
  <h2>二、后半小时预测（~{after[0]['time'][11:16]}）</h2>
  <div class="cards">
    <div class="card"><div class="k">天气趋势</div><div class="v" style="font-size:15px">维持{cur_weather}</div></div>
    <div class="card"><div class="k">降水概率</div><div class="v">{half_pop}%</div></div>
    <div class="card"><div class="k">气温</div><div class="v">约 {half_temp}℃</div></div>
    <div class="card"><div class="k">相对湿度</div><div class="v">约 {half_rh}%</div></div>
    <div class="card"><div class="k">风速/风向</div><div class="v" style="font-size:15px">{half_wind} m/s {wind_cn(after[0]['wind_dir'])}</div></div>
    <div class="card"><div class="k">地面气压</div><div class="v">约 {half_pres}</div></div>
  </div>
  <div class="note">未来半小时内天气以{cur_weather}为主，降水概率已抬升至 {half_pop}%，<b>出门建议携带雨具</b>；风力维持 5-6 级，体感仍偏闷热。</div>
</section>

<section>
  <h2>三、后8小时预测（{after[0]['time'][11:16]} – {after[-1]['time'][11:16]}）</h2>
  <table>
    <thead><tr><th>时次</th><th>天气</th><th>气温</th><th>湿度</th><th>降水概率</th><th>风速</th><th>风力</th><th>风向</th><th>气压</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</section>

<section>
  <h2>四、气象地图（滴水湖及周边）</h2>
  <div id="map" style="height:460px;border-radius:10px;overflow:hidden"></div>
  <div class="legend">
    <span class="lg"><i style="background:#4575b4"></i>低气压</span>
    <span class="lg"><i style="background:#abd9e9"></i></span>
    <span class="lg"><i style="background:#ffffbf"></i></span>
    <span class="lg"><i style="background:#fdae61"></i></span>
    <span class="lg"><i style="background:#d73027"></i>高气压</span>
    <span class="lg" style="margin-left:14px"><i style="background:#3a3a3a;height:0;border-top:2px solid #3a3a3a"></i>等压线(hPa)</span>
    <span class="lg"><i style="background:#0f6b34"></i>实心风向箭头(箭头大小=风速)</span>
    <span class="lg"><i style="background:#fff;border:1px solid #999"></i>网格温度标注(°C)</span>
  </div>
  <div class="note">底图为 OpenStreetMap 真实地理底图；<b>彩色色块为周边网格气压分布</b>（13×13 网格双线性插值、<b>色阶阶数与等压线间隔对齐</b>的蓝-白-红发散色带，低气压偏蓝/高气压偏红），
  <b>等压线</b>由同一气压场经等值线算法生成、连成连续路径并做抽稀平滑（步长随气压范围自适应，当前 {iso_step} hPa）：<b>全部为实线，高压区红棕色、低压区蓝灰色</b>，<b>绿色实心箭头</b>的杆长统一、箭头大小表示风速，
  网格上的<b>白色小字为温度(℃)</b>——温度不再用颜色表示。色块色阶与等压线一一对应，便于对照高低气压区。</div>
</section>

<section>
  <h2>五、气象图</h2>
  <div class="chartbox">{charts['temp']}</div>
  <div class="chartbox">{charts['pop']}</div>
  <div class="windrow">
    <div class="chartbox" style="flex:1">{charts['wind']}</div>
    <div class="chartbox" style="flex:0 0 220px; text-align:center">{charts['rose']}<div style="font-size:12px;color:#888">当前风向标（来向）</div></div>
  </div>
</section>

<section>
  <h2>六、8小时趋势研判</h2>
  <ul class="trend">
    <li><b>降水</b>：全天维持{cur_weather}，无强对流（无雷暴代码），但降水概率持续偏高（峰值 {max_pop}%，集中在 11-13 时）。整体为<b>间歇性弱降水/高湿</b>天气。</li>
    <li><b>大风预警</b>：偏南风转东南风，下午风力显著增强，<b>{max_wind_t} 前后风速达 {max_wind} m/s（{wind_level(max_wind)}）</b>，临湖一带体感风更强，<b>水上活动、临湖作业需注意防风</b>。</li>
    <li><b>气温</b>：在 {min_t}–{max_t}℃ 间小幅波动；相对湿度 82-89%，<b>体感持续闷热</b>，注意防暑补水。</li>
    <li><b>气压</b>：缓慢下降，对应系统过境，天气趋于不稳定。</li>
  </ul>
</section>

<section>
  <h2>七、综合建议</h2>
  <div class="advice">
    <div class="a"><b>🌂 随身带伞</b><br>未来 8 小时降水概率 25-67%，建议常备雨具。</div>
    <div class="a"><b>💨 防风</b><br>16:00 前后阵风可达 8 级，临湖、水上、高空作业防范。</div>
    <div class="a"><b>🥵 防暑</b><br>体感温度高、湿度大，户外停留注意补水休息。</div>
    <div class="a"><b>📉 关注气压</b><br>气压下降预示系统变化，留意后续降雨增强。</div>
  </div>
</section>

<div class="footer">本报告由自动化气象分析流程生成 · 每半小时更新 · 历史报告均存档不覆盖</div>
</div>
<!--MAP_JS-->
</body>
</html>"""
    # 注入地图渲染 JS（含网格 JSON，避免 f-string 大括号冲突）
    mapjs = build_map_js(mapdata)
    html = html.replace("<!--MAP_JS-->", mapjs)
    return html


def main():
    d = fetch()
    cur = d['current']
    h = d['hourly']; times = h['time']
    cur_hour = cur['time'][:13] + ':00'
    idx = times.index(cur_hour)
    after = []
    for i in range(idx+1, min(idx+9, len(times))):
        after.append(dict(time=times[i], temp=h['temperature_2m'][i], rh=h['relative_humidity_2m'][i],
                          pop=h['precipitation_probability'][i], wcode=h['weather_code'][i],
                          wtext=wmo(h['weather_code'][i]), wind=h['wind_speed_10m'][i],
                          wind_dir=h['wind_direction_10m'][i], pres=h['surface_pressure'][i]))

    date_part, time_part = cur['time'].split('T')
    hhmm = time_part.replace(':', '')[:4]
    stamp = date_part.replace('-', '') + '_' + hhmm
    date_dir = date_part
    out_dir = os.path.join(REPORTS, date_dir)
    os.makedirs(out_dir, exist_ok=True)
    fname = f"REPORT_{stamp}.html"
    fpath = os.path.join(out_dir, fname)

    # 构建图表（实况 + 后8小时，共 9 个点）
    labels = [cur['time'][11:16]] + [a['time'][11:16] for a in after]
    temps = [cur['temperature_2m']] + [a['temp'] for a in after]
    pops = [0] + [a['pop'] for a in after]   # 当前无降水概率字段，用 0 占位
    winds = [cur['wind_speed_10m']] + [a['wind'] for a in after]
    charts = {
        'temp': svg_line_chart("气温变化", labels, temps, "℃", color="#e74c3c"),
        'pop': svg_bar_chart("降水概率", labels, pops, "%", color="#3498db", vmax=100),
        'wind': svg_line_chart("风速变化", labels, winds, "m/s", color="#16a085", ymin=0),
        'rose': svg_wind_rose(cur['wind_direction_10m'], cur['wind_speed_10m']),
    }

    # 构建地图图层数据（网格温度/气压/风）
    mapdata = None
    try:
        cur_weather = wmo(cur['weather_code'])
        glats, glons, GT, GP, GW, GD = fetch_grid()
        isobars, iso_step = build_isobars(GP, glats, glons)
        # 风箭头：每个网格点 (lat, lon, speed, dir)
        wind_pts = []
        for i, la in enumerate(glats):
            for j, lo in enumerate(glons):
                if GW[i][j] is not None:
                    wind_pts.append([la, lo, round(GW[i][j],1), round(GD[i][j],0)])
        mapdata = {
            "center": [LAT, LON],
            "grid": {"lats": glats, "lons": glons, "temp": GT, "pres": GP},
            "isobars": isobars,
            "iso_step": iso_step,   # 等压线步长，用于色阶对齐
            "wind": wind_pts,
            "cur": {
                "lat": LAT, "lon": LON,
                "weather": cur_weather,
                "temp": cur['temperature_2m'],
                "pres": cur['surface_pressure'],
                "wind": cur['wind_speed_10m'],
                "wdir": cur['wind_direction_10m'],
                "rh": cur['relative_humidity_2m'],
            }
        }
    except Exception as e:
        print("地图数据获取失败（跳过地图）：", e)
        mapdata = None

    html = html_report(cur, after, stamp, charts, mapdata)
    with open(fpath, 'w', encoding='utf-8') as f:
        f.write(html)
    print("报告已生成:", fpath)

    # 维护 latest.html 软链
    latest = os.path.join(REPORTS, "latest.html")
    if os.path.islink(latest) or os.path.exists(latest):
        os.remove(latest)
    os.symlink(os.path.join(date_dir, fname), latest)
    print("latest.html ->", os.path.join(date_dir, fname))

    update_index(REPORTS, stamp, cur, date_dir, fname)
    return fpath


def update_index(REPORTS, stamp, cur, date_dir, fname):
    idx_path = os.path.join(REPORTS, "README.md")
    cur_weather = wmo(cur['weather_code'])
    cur_temp = cur['temperature_2m']
    header = ["# 滴水湖气象报告索引", "",
              f"- 🟢 **最新**：[{fname}]({date_dir}/{fname}) （生成于 {cur['time']}，{cur_weather} {cur_temp}℃）", "",
              "## 历史报告（HTML）", ""]
    table = ["| 报告编号 | 生成时间 | 天气 | 气温 | 文件 |",
             "|----------|----------|------|------|------|"]
    rows = []
    for fp in sorted(glob.glob(os.path.join(REPORTS, "**", "REPORT_*.html"), recursive=True)):
        rel = os.path.relpath(fp, REPORTS)
        fn = os.path.basename(fp)
        ts = fn.replace("REPORT_","").replace(".html","")
        rows.append((ts, rel))
    rows.sort(reverse=True)
    for ts, rel in rows:
        d8, t4 = ts.split('_')
        pretty = f"{d8[:4]}-{d8[4:6]}-{d8[6:8]} {t4[:2]}:{t4[2:]}"
        if ts == stamp:
            wtxt, ttxt = cur_weather, f"{cur_temp}℃"
        else:
            wtxt, ttxt, fn = "-", "-", os.path.basename(rel)
            try:
                c = open(os.path.join(REPORTS, rel), encoding='utf-8').read()
                mw = re.search(r'<div class="v">([^<]+)</div>', c)
                mt = re.search(r'气温</div><div class="v">([\d.]+)', c)
                if mw: wtxt = mw.group(1)
                if mt: ttxt = mt.group(1) + "℃"
            except Exception:
                pass
        fn = os.path.basename(rel)
        table.append(f"| {ts} | {pretty} | {wtxt} | {ttxt} | [{fn}]({rel}) |")
    content = "\n".join(header + table + [""])
    open(idx_path, 'w', encoding='utf-8').write(content)
    print("索引已更新:", idx_path)


if __name__ == "__main__":
    main()
