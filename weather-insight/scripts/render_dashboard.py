#!/usr/bin/env python3
"""render_dashboard.py — 自包含交互式 HTML 气象面板生成器。

把 Open-Meteo 单点预报 JSON 变成双击即开、完全离线的单文件交互面板：
CSS/JS/SVG 全部内联，原始数据放在 <script type="application/json" id="dashboard-data">
标签内，由页面内的原生 JS 渲染。零外部依赖（无 CDN、无字体、无图片、产物中不出现任何
外部链接字样，断网双击即可打开）。

用法:
    python3 scripts/render_dashboard.py --input <openmeteo.json> --output <panel.html> \
        [--metrics <metrics.json>] [--grid <grid数组.json>] [--analysis <markdown文件>] \
        [--title <标题>]

页面结构（tab）:
    总览   天气码 + 温度/风/气压等卡片（随时间滑块联动）+ 极值表
    图表   气压趋势折线 / 温度-降水双轴 / 16 方位风玫瑰 / 低中高云量堆叠面积（手写 SVG）
    天气图 仅提供 --grid 时出现：格点气压蓝白红着色 + 风矢量箭头 + 色标图例 + 陆地底图
    指标   仅提供 --metrics 时出现：指标表
    分析   仅提供 --analysis 时出现：Markdown 简易渲染

设计让步顺序：离线可用 > 数据准确 > 交互丰富 > 界面好看。
纯 Python 标准库实现，无第三方依赖。
"""
import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GEOJSON = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "resources", "world_countries.geojson"))

# 地理底图内联预算：裁剪+抽稀后全部环的顶点总数上限（约对应 <55KB 文本）
GEO_POINT_BUDGET = 2600

_URL_RE = re.compile(r"https?://[^\"\\\s]*")

# ---- 以下映射字典与 chart_data.py 保持一致（该脚本为权威定义，此处只读复制）----

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

# 天气码分组图标（unicode 字符，离线可用）
WMO_ICON = [
    (0, 0, "☀"), (1, 1, "🌤"), (2, 2, "⛅"), (3, 3, "☁"),
    (45, 48, "🌫"), (51, 57, "🌦"), (61, 67, "🌧"), (71, 77, "🌨"),
    (80, 82, "🌧"), (85, 86, "🌨"), (95, 99, "⛈"),
]


def wmo_icon(code):
    if code is None:
        return "·"
    for lo, hi, icon in WMO_ICON:
        if lo <= code <= hi:
            return icon
    return "·"


def log(msg):
    print(f"[DASH] {msg}", file=sys.stderr)


def sanitize_text(s):
    """自由文本安全化：移除外部链接形态，保证产物零外链。"""
    if not isinstance(s, str):
        return s
    return _URL_RE.sub("(外链已移除)", s)


# ---------------------------------------------------------------------------
# 输入读取
# ---------------------------------------------------------------------------

def load_json(path, what):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log(f"错误: {what} 文件不存在: {path}")
        raise SystemExit(2)
    except OSError as e:
        log(f"错误: {what} 无法读取: {path} ({e})")
        raise SystemExit(2)
    except json.JSONDecodeError as e:
        log(f"错误: {what} 不是合法 JSON: {path} ({e})")
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# 时间处理
# ---------------------------------------------------------------------------

def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def nearest_index(times, utc_offset_seconds):
    """默认停在最接近生成时刻的位置；无法解析时回退到中间索引。"""
    now_local = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=utc_offset_seconds or 0)
    best, best_diff = None, None
    for i, t in enumerate(times):
        ts = parse_ts(t)
        if ts is None:
            continue
        diff = abs((ts - now_local).total_seconds())
        if best_diff is None or diff < best_diff:
            best, best_diff = i, diff
    if best is None:
        return max(0, len(times) // 2)
    return best


# ---------------------------------------------------------------------------
# 极值统计（None 安全）
# ---------------------------------------------------------------------------

def extrema_of(arr, times, ndigits=1):
    vals = [(i, v) for i, v in enumerate(arr or []) if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not vals:
        return None
    imin = min(vals, key=lambda x: x[1])
    imax = max(vals, key=lambda x: x[1])
    return {
        "min": round(imin[1], ndigits),
        "max": round(imax[1], ndigits),
        "min_time": times[imin[0]] if imin[0] < len(times) else None,
        "max_time": times[imax[0]] if imax[0] < len(times) else None,
    }


# ---------------------------------------------------------------------------
# 地理底图：bbox 裁剪（Sutherland-Hodgman）+ 隔点抽稀
# ---------------------------------------------------------------------------

def _clip_ring(ring, xmin, ymin, xmax, ymax):
    def clip_edge(pts, keep, interp):
        out = []
        n = len(pts)
        if n == 0:
            return out
        for i in range(n):
            cur = pts[i]
            prev = pts[i - 1]
            ck, pk = keep(cur), keep(prev)
            if ck:
                if not pk:
                    out.append(interp(prev, cur))
                out.append(cur)
            elif pk:
                out.append(interp(prev, cur))
        return out

    def ix(p, q, x):
        return (x, p[1] + (q[1] - p[1]) * (x - p[0]) / (q[0] - p[0]))

    def iy(p, q, y):
        return (p[0] + (q[0] - p[0]) * (y - p[1]) / (q[1] - p[1]), y)

    pts = [(float(pt[0]), float(pt[1])) for pt in ring if isinstance(pt, (list, tuple)) and len(pt) >= 2]
    pts = clip_edge(pts, lambda p: p[0] >= xmin, lambda p, q: ix(p, q, xmin))
    pts = clip_edge(pts, lambda p: p[0] <= xmax, lambda p, q: ix(p, q, xmax))
    pts = clip_edge(pts, lambda p: p[1] >= ymin, lambda p, q: iy(p, q, ymin))
    pts = clip_edge(pts, lambda p: p[1] <= ymax, lambda p, q: iy(p, q, ymax))
    return pts


def decimate_rings(rings, budget=GEO_POINT_BUDGET):
    """全局顶点预算下的隔点抽稀；保持各环闭合；坐标保留 2 位小数。"""
    total = sum(len(r) for r in rings)
    if total <= budget:
        stride = 1
    else:
        stride = 2
        while True:
            cnt = sum(max(1, math.ceil(len(r) / stride)) + 1 for r in rings)
            if cnt <= budget or stride >= 64:
                break
            stride += 1
    out = []
    for r in rings:
        rr = r[::stride]
        if rr and rr[0] != rr[-1]:
            rr.append(rr[0])
        if len(rr) >= 4:
            out.append([[round(x, 2), round(y, 2)] for x, y in rr])
    return out


def build_geo_payload(bbox, geojson_path):
    """按 bbox 裁剪世界边界并抽稀，返回 {"rings": [[[lon,lat],...]]} 或 None。"""
    xmin, ymin, xmax, ymax = bbox
    try:
        with open(geojson_path, "r", encoding="utf-8") as f:
            gj = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"警告: 底图 GeoJSON 不可用({e})，天气图将不含陆地底图")
        return None

    features = []
    if isinstance(gj, dict):
        features = gj.get("features") or []
    elif isinstance(gj, list):
        features = gj

    rings = []
    for feat in features:
        geom = (feat or {}).get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        polys = []
        if gtype == "Polygon":
            polys = [coords]
        elif gtype == "MultiPolygon":
            polys = coords or []
        for poly in polys:
            if not poly:
                continue
            ring = poly[0]  # 只取外环，够画底图且省体积
            clipped = _clip_ring(ring, xmin, ymin, xmax, ymax)
            if len(clipped) >= 4:
                rings.append(clipped)
    if not rings:
        log("警告: 底图裁剪后无可见陆地（bbox 可能全为海洋），跳过底图")
        return None
    kept = decimate_rings(rings)
    npts = sum(len(r) for r in kept)
    approx_kb = sum(len(str(pt)) + 3 for r in kept for pt in r) // 1024
    log(f"底图: 裁剪+抽稀后 {len(kept)} 环 / {npts} 顶点（约 {approx_kb}KB 内联）")
    return {"rings": kept}


# ---------------------------------------------------------------------------
# 网格数据提取
# ---------------------------------------------------------------------------

def build_grid_payload(grid_data):
    """输入为 Open-Meteo 点位数组（同 render_weather_map 的网格 JSON）。
    以第一个点的 time 序列为主时间轴，逐点按各自 time 对齐，缺失补 null。"""
    if not isinstance(grid_data, list) or not grid_data:
        log("警告: --grid 内容不是非空数组，忽略网格")
        return None
    master = (grid_data[0].get("hourly") or {}).get("time") or []
    if not master:
        log("警告: --grid 第一个点位缺 hourly.time，忽略网格")
        return None
    master_idx = {t: i for i, t in enumerate(master)}
    n = len(master)
    cols = {"pressure": [], "temperature": [], "windSpeed": [], "windDir": []}
    points = []
    skipped = 0
    for item in grid_data:
        try:
            lat = float(item["latitude"])
            lon = float(item["longitude"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        points.append({"lat": round(lat, 4), "lon": round(lon, 4)})
        hourly = item.get("hourly") or {}
        times = hourly.get("time") or []
        series_out = {}
        for src, dst in (("pressure_msl", "pressure"), ("temperature_2m", "temperature"),
                         ("wind_speed_10m", "windSpeed"), ("wind_direction_10m", "windDir")):
            arr = hourly.get(src) or []
            aligned = [None] * n
            for j, t in enumerate(times):
                k = master_idx.get(t)
                if k is not None and j < len(arr):
                    val = arr[j]
                    aligned[k] = val if isinstance(val, (int, float)) and not isinstance(val, bool) else None
            series_out[dst] = aligned
        for dst, aligned in series_out.items():
            cols[dst].append(aligned)
    if skipped:
        log(f"警告: {skipped} 个格点缺少经纬度，已跳过")
    if not points:
        log("警告: 网格无有效格点，忽略网格")
        return None
    log(f"网格: {len(points)} 格点 × {n} 时次已嵌入")
    return {"times": master, "points": points, **cols}


# ---------------------------------------------------------------------------
# Markdown 简易渲染（不引解析库）
# ---------------------------------------------------------------------------

_MD_LINK_TARGET = re.compile(r"\]\(\s*https?://[^)]*\)")


def _md_inline(text):
    """行内格式：先转义 HTML 并中和链接，再做粗体/斜体/行内代码。"""
    t = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = _MD_LINK_TARGET.sub("](外链已移除)", t)
    t = _URL_RE.sub("(外链已移除)", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*([^*\s][^*]*)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    return t


def render_markdown(md_text):
    """极简 Markdown 渲染：标题/列表/引用/分隔线/表格/围栏代码/段落。"""
    lines = md_text.splitlines()
    out = []
    para = []
    list_tag = None          # 当前打开的列表标签 "ul"/"ol"
    in_code = False
    table_buf = []

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(_md_inline(l) for l in para) + "</p>")
            para.clear()

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def flush_table():
        nonlocal table_buf
        rows, table_buf = table_buf, []
        parsed = []
        for r in rows:
            parsed.append([c.strip() for c in r.strip().strip("|").split("|")])
        if not parsed:
            return
        if len(parsed) >= 2 and all(re.fullmatch(r":?-{2,}:?", c or "---") for c in parsed[1]):
            header, body = parsed[0], parsed[2:]
        else:
            header, body = None, parsed
        out.append("<table>")
        if header:
            out.append("<thead><tr>" + "".join(f"<th>{_md_inline(c)}</th>" for c in header) + "</tr></thead>")
        out.append("<tbody>")
        for r in body:
            out.append("<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in r) + "</tr>")
        out.append("</tbody></table>")

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if in_code:
            if stripped.startswith("```"):
                out.append("</code></pre>")
                in_code = False
            else:
                out.append(line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            continue
        if stripped.startswith("```"):
            flush_table()
            flush_para()
            close_list()
            out.append('<pre><code>')
            in_code = True
            continue
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            flush_para()
            close_list()
            table_buf.append(stripped)
            continue
        flush_table()
        if not stripped:
            flush_para()
            close_list()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            close_list()
            level = min(len(m.group(1)) + 2, 6)  # 面板内标题从 h3 视觉层级起步
            out.append(f"<h{level}>{_md_inline(m.group(2))}</h{level}>")
            continue
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            flush_para()
            close_list()
            out.append("<hr>")
            continue
        m = re.match(r"^>\s?(.*)$", stripped)
        if m:
            flush_para()
            close_list()
            out.append(f"<blockquote>{_md_inline(m.group(1))}</blockquote>")
            continue
        m = re.match(r"^[-*+]\s+(.*)$", stripped)
        if m:
            flush_para()
            if list_tag != "ul":
                close_list()
                out.append("<ul>")
                list_tag = "ul"
            out.append(f"<li>{_md_inline(m.group(1))}</li>")
            continue
        m = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if m:
            flush_para()
            if list_tag != "ol":
                close_list()
                out.append("<ol>")
                list_tag = "ol"
            out.append(f"<li>{_md_inline(m.group(1))}</li>")
            continue
        close_list()
        para.append(stripped)
    if in_code:
        out.append("</code></pre>")
    flush_table()
    flush_para()
    close_list()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 指标表渲染（通用结构，长数组给摘要）
# ---------------------------------------------------------------------------

def _fmt_scalar(v):
    if v is None:
        return "–"
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, float):
        try:
            return f"{v:g}"
        except (ValueError, OverflowError):
            return str(v)
    return str(v)


def _metrics_value_html(key, val):
    del key  # 键名已在行头展示
    if val is None or isinstance(val, (int, float, bool)):
        return f'<span class="mv">{_fmt_scalar(val)}</span>'
    if isinstance(val, str):
        return f'<span class="mv">{_md_inline(sanitize_text(val))}</span>'
    if isinstance(val, list):
        nums = [v for v in val if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if nums and len(nums) == len(val) and len(nums) > 12:
            lo, hi = min(nums), max(nums)
            nn = len([v for v in val if v is not None])
            return (f'<span class="mv">数组×{nn}，范围 {_fmt_scalar(round(lo, 2))}'
                    f" ~ {_fmt_scalar(round(hi, 2))}</span>"
                    f'<span class="muted">（完整数组见数据标签）</span>')
        return ('<span class="mv">'
                + _md_inline(sanitize_text(json.dumps(val, ensure_ascii=False)[:400]))
                + "</span>")
    if isinstance(val, dict):
        parts = []
        for k2, v2 in list(val.items())[:8]:
            parts.append(f"{k2}: {_fmt_scalar(v2) if not isinstance(v2, (dict, list)) else '…'}")
        more = "" if len(val) <= 8 else f" 等{len(val)}项"
        return '<span class="mv">' + _md_inline("; ".join(parts) + more) + "</span>"
    return '<span class="mv">–</span>'


def render_metrics_tables(metrics):
    """metrics.json → 分节表格 HTML（顶层键分节）。完整原始值仍嵌入数据标签。"""
    if not isinstance(metrics, dict) or not metrics:
        return '<p class="empty-hint">指标文件为空或结构不可识别。</p>'
    out = []
    for key, val in metrics.items():
        out.append(f'<h3 class="metric-section">{_md_inline(str(key))}</h3>')
        rows = []
        if isinstance(val, dict):
            for k2, v2 in val.items():
                rows.append((str(k2), _metrics_value_html(k2, v2)))
        elif isinstance(val, list) and val and all(isinstance(v, dict) for v in val):
            keys = []
            for item in val:
                for k2 in item.keys():
                    if k2 not in keys:
                        keys.append(k2)
            for item in val:
                name = "; ".join(f"{k2}={_fmt_scalar(item[k2])}" for k2 in keys if item.get(k2) is not None)
                rows.append((name or "(空项)", ""))
        else:
            rows.append(("值", _metrics_value_html(key, val)))
        out.append("<table><tbody>")
        for name, vhtml in rows:
            out.append(f"<tr><th>{_md_inline(name)}</th><td>{vhtml}</td></tr>")
        out.append("</tbody></table>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 数据载荷组装
# ---------------------------------------------------------------------------

PICK_FIELDS = [
    "temperature_2m", "apparent_temperature", "relative_humidity_2m",
    "precipitation", "pressure_msl", "cloud_cover",
    "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
    "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m", "weather_code",
]


def build_payload(data, metrics, grid, title):
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []

    hourly_out = {f: hourly[f] for f in PICK_FIELDS if f in hourly}

    extrema = {}
    for label, field, nd in (("温度 (°C)", "temperature_2m", 1),
                             ("体感温度 (°C)", "apparent_temperature", 1),
                             ("海平面气压 (hPa)", "pressure_msl", 1),
                             ("风速 10m (m/s)", "wind_speed_10m", 1),
                             ("降水量 (mm)", "precipitation", 2),
                             ("云量 (%)", "cloud_cover", 0)):
        e = extrema_of(hourly.get(field), times, nd)
        if e:
            extrema[field] = {"label": label, **e}

    utc_off = data.get("utc_offset_seconds") or 0
    now_idx = nearest_index(times, utc_off)
    generated_at = (datetime.now(timezone.utc) + timedelta(seconds=utc_off)).strftime("%Y-%m-%dT%H:%M")

    loc = data.get("location") if isinstance(data.get("location"), dict) else None
    lat = data.get("latitude", loc.get("latitude") if loc else None)
    lon = data.get("longitude", loc.get("longitude") if loc else None)

    payload = {
        "title": sanitize_text(title),
        "generatedAt": generated_at,
        "location": {
            "latitude": lat,
            "longitude": lon,
            "elevation": data.get("elevation"),
            "timezone": data.get("timezone"),
        },
        "times": times,
        "nowIndex": now_idx,
        "hourly": hourly_out,
        "extrema": extrema,
        "wmoLabels": {str(k): v for k, v in WMO_LABELS.items()},
    }
    if grid:
        payload["grid"] = grid
    if metrics is not None:
        payload["metrics"] = metrics
    return payload


def dump_payload_json(payload):
    """嵌入 <script type="application/json"> 的安全序列化。
    双保险：先剥离任何外部链接形态，再转义 </ 防止提前闭合脚本标签；
    两步都不影响数值型气象数据的精度。"""
    s = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    s = _URL_RE.sub("(外链已移除)", s)
    s = s.replace("</", "<\\/").replace("<!--", "<\\!--")
    return s


# ---------------------------------------------------------------------------
# 页面模板与装配（%%TOKEN%% 替换，避免与 CSS/JS 花括号冲突）
# ---------------------------------------------------------------------------

OVERVIEW_HTML = """
<div class="ov-top">
  <div class="ov-now">
    <div class="ov-icon" id="ov-icon">·</div>
    <div>
      <div class="ov-desc" id="ov-desc">–</div>
      <div class="ov-time muted" id="ov-time"></div>
    </div>
  </div>
</div>
<div class="cards">
  <div class="card"><div class="card-k">温度 °C</div><div class="card-v" id="ov-temp">–</div><div class="card-s muted" id="ov-temp-s"></div></div>
  <div class="card"><div class="card-k">体感 °C</div><div class="card-v" id="ov-feels">–</div><div class="card-s muted" id="ov-feels-s"></div></div>
  <div class="card"><div class="card-k">海平面气压 hPa</div><div class="card-v" id="ov-pres">–</div><div class="card-s muted" id="ov-pres-s"></div></div>
  <div class="card"><div class="card-k">风速 m/s</div><div class="card-v" id="ov-wind">–</div><div class="card-s muted" id="ov-wind-s"></div></div>
  <div class="card"><div class="card-k">湿度 %</div><div class="card-v" id="ov-hum">–</div><div class="card-s muted" id="ov-hum-s"></div></div>
  <div class="card"><div class="card-k">降水 mm</div><div class="card-v" id="ov-precip">–</div><div class="card-s muted" id="ov-precip-s"></div></div>
  <div class="card"><div class="card-k">云量 %</div><div class="card-v" id="ov-cloud">–</div><div class="card-s muted" id="ov-cloud-s"></div></div>
</div>
<div class="extrema">
  <h3>期间极值</h3>
  <table class="tbl" id="extrema-table"><thead><tr><th>要素</th><th>最小</th><th>出现时刻</th><th>最大</th><th>出现时刻</th></tr></thead><tbody></tbody></table>
</div>
"""

CHARTS_HTML = """
<div class="chart-block">
  <h3>气压趋势（hPa）</h3>
  <div class="chart-box" id="box-pressure"></div>
</div>
<div class="chart-block">
  <h3>温度-降水（左轴 °C / 右轴 mm）</h3>
  <div class="chart-box" id="box-tempprecip"></div>
</div>
<div class="chart-block">
  <h3>风玫瑰（16 方位，统计至滑块时刻）</h3>
  <div class="chart-box" id="box-rose"></div>
</div>
<div class="chart-block">
  <h3>低 / 中 / 高云量堆叠面积（%）</h3>
  <div class="chart-box" id="box-cloud"></div>
</div>
"""

MAP_HTML = """
<div class="chart-box" id="box-map"></div>
<div class="legend" id="map-legend"></div>
<p class="muted legend-note">圆点颜色 = 海平面气压（蓝=低压 → 白=平 → 红=高压）；箭头指向风的去向，长度∝风速。</p>
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%%TITLE%%</title>
<style>
:root{
  --bg:#f6f8fa; --panel:#ffffff; --ink:#1a1a1a; --ink2:#667085;
  --line:#e4e7ec; --accent:#2563eb; --red:#dc2626;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Segoe UI",sans-serif;
  font-size:14px;line-height:1.55}
.wrap{max-width:960px;margin:0 auto;padding:18px 16px 40px}
header h1{font-size:21px;margin:0 0 2px}
header .sub{color:var(--ink2);font-size:12.5px;margin-bottom:14px}
.sliderbar{display:flex;align-items:center;gap:10px;background:var(--panel);
  border:1px solid var(--line);border-radius:10px;padding:9px 14px;margin-bottom:12px}
.sliderbar input[type=range]{flex:1;accent-color:var(--accent)}
.sliderbar .sl-time{font-variant-numeric:tabular-nums;font-size:12.5px;color:var(--ink2);white-space:nowrap}
.tabs{display:flex;gap:6px;border-bottom:2px solid var(--line)}
.tab-btn{appearance:none;border:none;background:none;padding:8px 14px;font-size:14px;color:var(--ink2);
  cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-2px;font-family:inherit}
.tab-btn.active{color:var(--accent);font-weight:600;border-bottom-color:var(--accent)}
.panel{display:none;background:var(--panel);border:1px solid var(--line);border-top:none;
  border-radius:0 0 10px 10px;padding:18px}
.panel.active{display:block}
.muted{color:var(--ink2)}
.ov-top{margin-bottom:14px}
.ov-now{display:flex;align-items:center;gap:12px}
.ov-icon{font-size:44px;line-height:1}
.ov-desc{font-size:19px;font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:18px}
.card{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card-k{font-size:12px;color:var(--ink2)}
.card-v{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums;margin:2px 0}
.card-s{font-size:11.5px;min-height:15px}
table.tbl,.metrics-wrap table,.analysis-wrap table{width:100%;border-collapse:collapse;font-size:13px}
.tbl th,.tbl td,.metrics-wrap table th,.metrics-wrap table td,
.analysis-wrap table th,.analysis-wrap table td{border:1px solid var(--line);padding:6px 9px;text-align:left}
.tbl thead th,.metrics-wrap table thead th{background:var(--bg)}
.extrema h3{margin:16px 0 8px;font-size:15px}
.chart-block{margin-bottom:22px}
.chart-block h3{margin:0 0 6px;font-size:15px}
.chart-box{position:relative;width:100%;background:#fff;border:1px solid var(--line);border-radius:8px}
.chart-box svg{display:block;width:100%;height:auto}
.tip{position:absolute;pointer-events:none;background:rgba(26,26,26,.92);color:#fff;
  padding:5px 9px;border-radius:6px;font-size:12px;white-space:nowrap;z-index:5;display:none}
.legend{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px;color:var(--ink2);
  font-variant-numeric:tabular-nums}
.legend .bar{flex:0 0 auto;display:flex;border-radius:4px;overflow:hidden;border:1px solid var(--line)}
.legend .bar i{display:block;width:9px;height:10px}
.legend-note{font-size:12px;margin:6px 0 0}
.metrics-wrap h3.metric-section{margin:16px 0 6px;font-size:14px;color:var(--ink2)}
.metrics-wrap .mv{font-variant-numeric:tabular-nums;word-break:break-all}
.analysis-wrap h3,.analysis-wrap h4,.analysis-wrap h5,.analysis-wrap h6{margin:16px 0 6px}
.analysis-wrap blockquote{margin:8px 0;padding:6px 12px;border-left:3px solid var(--line);
  background:var(--bg);color:var(--ink2)}
.analysis-wrap code{background:var(--bg);padding:1px 5px;border-radius:4px;font-size:12.5px}
.analysis-wrap pre{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:8px;overflow:auto}
.analysis-wrap pre code{background:none;color:inherit;padding:0}
.empty-hint{color:var(--ink2)}
footer{margin-top:16px;font-size:11.5px;color:var(--ink2);text-align:center}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>%%TITLE%%</h1>
  <div class="sub">%%SUBTITLE%% · 生成于 %%GENERATED%% · 数据源 Open-Meteo</div>
</header>

<div class="sliderbar">
  <span class="muted">时刻</span>
  <input type="range" id="time-slider" min="0" max="1" step="1" value="0"
         aria-label="时间滑块：拖动联动总览卡片、图表游标与天气图时刻">
  <span class="sl-time" id="sl-label">–</span>
</div>

<nav class="tabs" role="tablist">
%%TABS%%
</nav>

%%SECTIONS%%

<footer>自包含离线面板 · 由 weather-insight 技能生成</footer>
</div>

<script type="application/json" id="dashboard-data">%%PAYLOAD%%</script>

<script>
(function(){
"use strict";
var D = JSON.parse(document.getElementById("dashboard-data").textContent);
function $(id){return document.getElementById(id);}
function num(x){return (typeof x==="number" && isFinite(x))?x:null;}
function gv(path,i){var a=D.hourly[path];if(!a)return null;if(i<0||i>=a.length)return null;return num(a[i]);}
function gc(path){return D.hourly[path]||null;}
function fmt(x,d){if(d===undefined)d=1;return x===null?"–":Number(x).toFixed(d);}
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
var TIMES=D.times||[], N=TIMES.length;

/* ---- 映射字典（与 chart_data.py 一致）---- */
var WMO=D.wmoLabels||{};
function wmoLabel(c){if(c===null)return "未知";return WMO[String(Math.round(c))]||("代码 "+c);}
function wmoIcon(c){
  if(c===null)return "·";
  var t=[[0,0,"☀"],[1,1,"🌤"],[2,2,"⛅"],[3,3,"☁"],[45,48,"🌫"],[51,57,"🌦"],
         [61,67,"🌧"],[71,77,"🌨"],[80,82,"🌧"],[85,86,"🌨"],[95,99,"⛈"]];
  for(var i=0;i<t.length;i++){if(c>=t[i][0]&&c<=t[i][1])return t[i][2];}
  return "·";
}
var DIRS16=["北","东北偏北","东北","东北偏东","东","东南偏东","东南","东南偏南",
            "南","西南偏南","西南","西南偏西","西","西北偏西","西北","西北偏北"];
var LEVELS=[[0.0,0.5,"0级 无风"],[0.6,1.5,"1级 软风"],[1.6,3.3,"2级 轻风"],[3.4,5.4,"3级 微风"],
  [5.5,7.9,"4级 和风"],[8.0,10.7,"5级 清风"],[10.8,13.8,"6级 强风"],[13.9,17.1,"7级 疾风"],
  [17.2,20.7,"8级 大风"],[20.8,24.4,"9级 烈风"],[24.5,28.4,"10级 狂风"],[28.5,32.6,"11级 暴风"],
  [32.7,1e9,"12级 飓风"]];
function windLevel(sp){for(var i=0;i<LEVELS.length;i++){if(sp>=LEVELS[i][0]&&sp<=LEVELS[i][1])return LEVELS[i][2];}return "12级 飓风";}
function dirName(deg){if(deg===null)return "未知";return DIRS16[Math.floor(((deg+11.25)%360)/22.5)%16];}

/* ============================================================
 * SVG 组装：全部走字符串拼接 + innerHTML（HTML 解析器原生支持内联
 * SVG，无需命名空间属性，也保证产物零外部引用）
 * ============================================================ */
function mkBox(boxId,w,h){
  var box=$(boxId);
  box.innerHTML='<svg viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="xMidYMid meet"></svg>'+
                '<div class="tip"></div>';
  return {box:box, svg:box.firstChild, tip:box.lastChild, W:w};
}
function S(tag,attrs,content){
  var a="";
  for(var k in attrs){if(attrs[k]!==null&&attrs[k]!==undefined)a+=" "+k+'="'+attrs[k]+'"';}
  return "<"+tag+a+">"+(content===undefined?"":content)+"</"+tag+">";
}
function txt(x,y,s,size,anchor,fill,extra){
  return S("text",{x:x,y:y,"font-size":size||11,"text-anchor":anchor||"middle",
    fill:fill||"#667085"},esc(s));
}
function ln(x1,y1,x2,y2,st,w,dash){
  return S("line",{x1:x1,y1:y1,x2:x2,y2:y2,stroke:st,"stroke-width":w||1}, "");
}
function pth(d,fill,st,w){
  return S("path",{d:d,fill:(fill||fill==="")?fill:"none",
    stroke:st||"none","stroke-width":w||1,
    "stroke-linejoin":"round","stroke-linecap":"round"},"");
}
function circ(cx,cy,r,fill,st,w){
  return S("circle",{cx:cx,cy:cy,r:r,fill:fill||"none",stroke:st||"none","stroke-width":w||1},"");
}

/* ---- 坐标尺度 ---- */
function niceStep(range){
  if(!(range>0))return 1;
  var raw=range/5,pow=Math.pow(10,Math.floor(Math.log(raw)/Math.LN10)),c=raw/pow;
  return (c<1.5?1:c<3.5?2:c<7.5?5:10)*pow;
}
function tickEvery(){return Math.max(1,Math.ceil(N/9));}
function timeTickLabel(i){
  var t=TIMES[i]||"";if(t.length<13)return "";
  var hh=t.slice(11,16);
  if(hh==="00:00"&&t.length>=10)return t.slice(5,10);
  return hh;
}

/* ============================================================
 * 总览
 * ============================================================ */
function fillExtrema(){
  var tb=document.querySelector("#extrema-table tbody");
  if(!tb)return;
  var ex=D.extrema||{},rows=[];
  for(var k in ex){if(Object.prototype.hasOwnProperty.call(ex,k)){
    var e=ex[k];
    rows.push("<tr><th>"+esc(e.label)+"</th><td>"+fmt(e.min)+"</td><td>"+esc(e.min_time||"–")+
      "</td><td>"+fmt(e.max)+"</td><td>"+esc(e.max_time||"–")+"</td></tr>");
  }}
  tb.innerHTML=rows.join("")||'<tr><td colspan="5" class="empty-hint">无极值数据</td></tr>';
}

function updateOverview(i){
  var el=function(id){return $(id);};
  var t=gv("temperature_2m",i),ap=gv("apparent_temperature",i),pr=gv("pressure_msl",i),
      ws=gv("wind_speed_10m",i),wd=gv("wind_direction_10m",i),hu=gv("relative_humidity_2m",i),
      pc=gv("precipitation",i),cl=gv("cloud_cover",i),
      wc=D.hourly.weather_code?num(D.hourly.weather_code[i]):null;
  el("ov-temp").textContent=fmt(t);
  el("ov-feels").textContent=fmt(ap);
  el("ov-pres").textContent=fmt(pr);
  el("ov-wind").textContent=fmt(ws);
  el("ov-hum").textContent=fmt(hu,0);
  el("ov-precip").textContent=fmt(pc,2);
  el("ov-cloud").textContent=fmt(cl,0);
  el("ov-temp-s").textContent=t!==null?(t>=30?"炎热":t>=24?"暖":t>=10?"舒适":"冷"):"";
  el("ov-pres-s").textContent="";
  el("ov-wind-s").textContent=(ws!==null&&wd!==null)?windLevel(ws)+" · "+dirName(wd)+"风":"";
  el("ov-hum-s").textContent=hu!==null?(hu>=80?"潮湿":hu<=35?"干燥":""):"";
  el("ov-precip-s").textContent=(pc!==null&&pc>0)?"有降水":"";
  el("ov-cloud-s").textContent=cl!==null?(cl<=25?"晴好":cl<=75?"多云":"阴"):"";
  $("ov-desc").textContent=wmoLabel(wc);
  $("ov-icon").textContent=wmoIcon(wc);
  $("ov-time").textContent=TIMES[i]||"";
}

/* ============================================================
 * 折线类图表公共件
 * ============================================================ */
var CURSORS={}; // tab 内游标线更新函数注册表
function attachHover(ctx,plot,onPick){
  var hit=S("rect",{x:plot.L,y:plot.T,width:plot.w,height:plot.h,fill:"transparent"},"");
  ctx.svg.insertAdjacentHTML("beforeend",hit);
  var hitEl=ctx.svg.lastChild;
  hitEl.addEventListener("mousemove",function(ev){
    var r=ctx.svg.getBoundingClientRect();
    var sx=(ev.clientX-r.left)/r.width*ctx.W;
    var i=Math.round((sx-plot.L)/(plot.w)*Math.max(1,N-1));
    i=Math.max(0,Math.min(N-1,i));
    var html=onPick(i);
    if(html){
      ctx.tip.innerHTML=html;ctx.tip.style.display="block";
      var bx=ev.clientX-r.left,by=ev.clientY-r.top;
      ctx.tip.style.left=Math.min(bx+14,ctx.box.clientWidth-170)+"px";
      ctx.tip.style.top=Math.max(by-44,2)+"px";
    }else{ctx.tip.style.display="none";}
  });
  hitEl.addEventListener("mouseleave",function(){ctx.tip.style.display="none";});
}
function addCursor(ctx,name,plot,color){
  ctx.svg.insertAdjacentHTML("beforeend",
    S("line",{y1:plot.T,y2:plot.T+plot.h,x1:-9,x2:-9,stroke:color||"#94a3b8",
      "stroke-dasharray":"4 3","stroke-width":"1","pointer-events":"none"},""));
  CURSORS[name]=function(i){
    var l=ctx.svg.querySelector("line[stroke-dasharray='4 3']");
    if(!l)return;
    if(N<2){l.setAttribute("visibility","hidden");return;}
    var x=plot.L+plot.w*i/(N-1);
    l.setAttribute("x1",x);l.setAttribute("x2",x);
    l.setAttribute("visibility","visible");
  };
}

/* ============================================================
 * 图表一：气压趋势折线
 * ============================================================ */
function renderPressure(){
  var P=gc("pressure_msl");
  if(!P)return;
  var ctx=mkBox("box-pressure",680,320);
  var plot={L:58,R:18,T:26,B:34,w:604,h:260};
  var vals=P.filter(function(v){return num(v)!==null;});
  if(!vals.length){
    ctx.svg.insertAdjacentHTML("beforeend",txt(340,160,"暂无气压数据",13));
    CURSORS.pressure=function(){};return;
  }
  var lo=Math.min.apply(null,vals),hi=Math.max.apply(null,vals);
  var pad=Math.max(0.8,(hi-lo)*0.08);lo-=pad;hi+=pad;
  function x(i){return N<2?plot.L+plot.w/2:plot.L+plot.w*i/(N-1);}
  function y(v){return plot.T+plot.h*(hi-v)/(hi-lo);}
  var parts="",step=niceStep(hi-lo),v,g,i;
  for(v=Math.ceil(lo/step)*step;v<=hi;v+=step){
    g=y(v);
    parts+=ln(plot.L,g,plot.L+plot.w,g,"#eef0f4",1);
    parts+=txt(plot.L-6,g+4,String(Math.round(v*10)/10),10.5,"end");
  }
  for(i=0;i<N;i+=tickEvery()){
    var lab=timeTickLabel(i);
    if(lab)parts+=txt(x(i),plot.T+plot.h+18,lab,10.5);
  }
  /* 折线（null 断段）*/
  var d="",pen=false;
  for(i=0;i<N;i++){
    var vv=num(P[i]);
    if(vv===null){pen=false;continue;}
    d+=(pen?"L":"M")+x(i).toFixed(1)+","+y(vv).toFixed(1);pen=true;
  }
  parts+=pth(d,"none","#2563eb",2);
  /* 高低压标注 */
  var imax=-1,imin=-1;
  for(i=0;i<N;i++){
    if(num(P[i])===null)continue;
    if(imax<0||num(P[i])>num(P[imax]))imax=i;
    if(imin<0||num(P[i])<num(P[imin]))imin=i;
  }
  if(imax>=0){
    parts+=circ(x(imax),y(num(P[imax])),3.5,"#dc2626");
    parts+=txt(x(imax),y(num(P[imax]))-9,"H",12,"middle","#dc2626");
  }
  if(imin>=0){
    parts+=circ(x(imin),y(num(P[imin])),3.5,"#2563eb");
    parts+=txt(x(imin),y(num(P[imin]))-9,"L",12,"middle","#2563eb");
  }
  ctx.svg.innerHTML=parts;
  attachHover(ctx,plot,function(j){
    return "<b>"+esc(TIMES[j]||"")+"</b><br>气压 "+fmt(gv("pressure_msl",j))+" hPa";
  });
  addCursor(ctx,"pressure",plot);
}

/* ============================================================
 * 图表二：温度-降水双轴
 * ============================================================ */
function renderTempPrecip(){
  var T=gc("temperature_2m"),PC=gc("precipitation");
  if(!T&&!PC)return;
  var tv=T?T.filter(function(v){return num(v)!==null;}):[];
  var pv=PC?PC.filter(function(v){return num(v)!==null;}):[];
  if(!tv.length&&!pv.length)return;
  var ctx=mkBox("box-tempprecip",680,320);
  var plot={L:52,R:52,T:26,B:34,w:576,h:260};
  var lo=tv.length?Math.min.apply(null,tv):0,hi=tv.length?Math.max.apply(null,tv):1;
  var tpad=Math.max(1,(hi-lo)*0.08);lo-=tpad;hi+=tpad;
  var pmax=0;
  if(PC){for(var i0=0;i0<N;i0++){var v0=num(PC[i0]);if(v0!==null&&v0>pmax)pmax=v0;}}
  pmax=pmax>0?Math.ceil(pmax):1;
  function x(i){return N<2?plot.L+plot.w/2:plot.L+plot.w*i/(N-1);}
  function yt(v){return plot.T+plot.h*(hi-v)/(hi-lo);}
  function yp(v){return plot.T+plot.h*v/pmax;}
  var parts="",step=niceStep(hi-lo),g,v,i;
  for(v=Math.ceil(lo/step)*step;v<=hi;v+=step){
    g=yt(v);
    parts+=ln(plot.L,g,plot.L+plot.w,g,"#eef0f4",1);
    parts+=txt(plot.L-6,g+4,String(Math.round(v*10)/10),10.5,"end","#b91c1c");
  }
  var pstep=niceStep(pmax);
  for(v=0;v<=pmax+1e-9;v+=pstep){
    g=yp(v);
    parts+=txt(plot.L+plot.w+6,g+4,String(Math.round(v*100)/100),10.5,"start","#1d4ed8");
  }
  for(i=0;i<N;i+=tickEvery()){
    var lab=timeTickLabel(i);
    if(lab)parts+=txt(x(i),plot.T+plot.h+18,lab,10.5);
  }
  /* 降水柱（右轴）*/
  if(PC){
    var bw=plot.w/Math.max(N,1)*0.62;
    for(i=0;i<N;i++){
      var dv=num(PC[i]);
      if(dv===null||dv<=0)continue;
      parts+=S("rect",{x:(x(i)-bw/2).toFixed(1),y:yp(dv).toFixed(1),
        width:bw.toFixed(1),height:(plot.T+plot.h-yp(dv)).toFixed(1),
        fill:"#3b82f6",opacity:"0.55"},"");
    }
  }
  /* 温度折线（左轴）*/
  if(tv.length){
    var d="",pen=false;
    for(i=0;i<N;i++){
      var vv=num(T[i]);
      if(vv===null){pen=false;continue;}
      d+=(pen?"L":"M")+x(i).toFixed(1)+","+yt(vv).toFixed(1);pen=true;
    }
    parts+=pth(d,"none","#dc2626",2);
  }
  ctx.svg.innerHTML=parts;
  attachHover(ctx,plot,function(j){
    return "<b>"+esc(TIMES[j]||"")+"</b><br>温度 "+fmt(gv("temperature_2m",j))+" °C<br>降水 "+
      fmt(gv("precipitation",j),2)+" mm";
  });
  addCursor(ctx,"tempprecip",plot);
}

/* ============================================================
 * 图表三：16 方位风玫瑰（统计至滑块时刻）
 * ============================================================ */
var SPEED_COLORS=[[1.6,"#bfdbfe"],[3.4,"#93c5fd"],[5.5,"#60a5fa"],[8.0,"#3b82f6"],[1e9,"#1e40af"]];
function speedColor(sp){
  for(var i=0;i<SPEED_COLORS.length;i++){if(sp<SPEED_COLORS[i][0])return SPEED_COLORS[i][1];}
  return "#1e40af";
}
var ROSE={};
function renderRose(){
  var WS=gc("wind_speed_10m"),WD=gc("wind_direction_10m");
  if(!WS&&!WD)return;
  var ctx=mkBox("box-rose",680,330);
  ROSE={svg:ctx.svg,cx:340,cy:172,R:118,WS:WS,WD:WD};
  updateRose(Math.min(Math.max(0,D.nowIndex||0),Math.max(0,N-1)));
}
function updateRose(upTo){
  if(!ROSE.svg)return;
  var WS=ROSE.WS,WD=ROSE.WD,svg=ROSE.svg,cx=ROSE.cx,cy=ROSE.cy,R=ROSE.R;
  var buckets=[],b,i;
  for(b=0;b<16;b++)buckets.push({n:0,sum:0});
  var total=0;
  if(WS&&WD){
    var lim=Math.min(upTo+1,N);
    for(i=0;i<lim;i++){
      var sp=num(WS[i]),dg=num(WD[i]);
      if(sp===null||dg===null)continue;
      var bi=Math.floor(((dg+11.25)%360)/22.5)%16;
      buckets[bi].n++;buckets[bi].sum+=sp;total++;
    }
  }
  var maxN=0;
  for(b=0;b<16;b++)if(buckets[b].n>maxN)maxN=buckets[b].n;
  var parts="";
  for(var rr=1;rr<=4;rr++){
    parts+=circ(cx,cy,R*rr/4,"none","#eef0f4",1);
    if(rr<4)parts+=txt(cx+4,cy-R*rr/4+3,(rr*25)+"%",9,"start","#9aa3af");
  }
  var dirs=["N","NE","E","SE","S","SW","W","NW"];
  for(b=0;b<8;b++){
    var ang=(b*45-90)*Math.PI/180;
    parts+=txt(cx+Math.cos(ang)*(R+14),cy+Math.sin(ang)*(R+14)+4,dirs[b],11.5,"middle","#475467");
  }
  /* 扇区：气象约定 0°=北在正上方，顺时针增 */
  for(b=0;b<16;b++){
    if(!buckets[b].n)continue;
    var radius=R*(buckets[b].n/maxN),
        a0=((b*22.5)-90-11.25)*Math.PI/180,a1=((b*22.5)-90+11.25)*Math.PI/180;
    var x0=cx+Math.cos(a0)*radius,y0=cy+Math.sin(a0)*radius,
        x1=cx+Math.cos(a1)*radius,y1=cy+Math.sin(a1)*radius;
    var avg=buckets[b].sum/buckets[b].n,col=speedColor(avg);
    var d="M"+cx+","+cy+"L"+x0.toFixed(1)+","+y0.toFixed(1)+
      "A"+radius.toFixed(1)+","+radius.toFixed(1)+" 0 0 1 "+x1.toFixed(1)+","+y1.toFixed(1)+"Z";
    parts+='<path d="'+d+'" fill="'+col+'" opacity="0.78" stroke="#ffffff" stroke-width="0.5"></path>';
    var pct=Math.round(buckets[b].n/Math.max(total,1)*100);
    if(radius>34){
      var lx=cx+Math.cos((a0+a1)/2)*(radius*0.62),
          ly=cy+Math.sin((a0+a1)/2)*(radius*0.62)+4;
      parts+=txt(lx,ly,pct+"%",9.5,"middle","#1e293b");
    }
  }
  var cap=(upTo+1>=N)?"全时段":"至 "+(TIMES[upTo]||"");
  parts+=txt(cx,20,"样本 "+total+" 时次 · "+cap,11,"middle","#667085");
  svg.innerHTML=parts;
}

/* ============================================================
 * 图表四：低中高云量堆叠面积
 * ============================================================ */
function renderCloud(){
  var LO=gc("cloud_cover_low"),MI=gc("cloud_cover_mid"),HI=gc("cloud_cover_high"),ALLC=gc("cloud_cover");
  if(!LO&&!MI&&!HI&&!ALLC)return;
  var ctx=mkBox("box-cloud",680,300);
  var plot={L:52,R:18,T:26,B:34,w:610,h:240};
  function x(i){return N<2?plot.L+plot.w/2:plot.L+plot.w*i/(N-1);}
  function clamp01(v){return Math.max(0,Math.min(100,num(v)||0));}
  function yv(v){return plot.T+plot.h*(100-clamp01(v))/100;}
  var parts="",i;
  for(var pcv=0;pcv<=100;pcv+=25){
    var gy=yv(pcv);
    parts+=ln(plot.L,gy,plot.L+plot.w,gy,pcv===0?"#cbd5e1":"#eef0f4",1);
    parts+=txt(plot.L-6,gy+4,String(pcv),10.5,"end");
  }
  for(i=0;i<N;i+=tickEvery()){
    var lab=timeTickLabel(i);
    if(lab)parts+=txt(x(i),plot.T+plot.h+18,lab,10.5);
  }
  /* 分层可用性：优先用低/中/高分层，全缺则退化为总云量单层 */
  var layers=[];
  if(HI)layers.push(["high","#dbeafe","高云"]);
  if(MI)layers.push(["mid","#93c5fd","中云"]);
  if(LO)layers.push(["low","#3b82f6","低云"]);
  if(!layers.length&&ALLC)layers.push(["all","#93c5fd","总云量"]);
  var base=[];for(i=0;i<N;i++)base.push(0);
  layers.forEach(function(L){
    var top=[],hasAny=false;
    for(var j=0;j<N;j++){
      var cv=null;
      if(L[0]==="all")cv=num(ALLC[j]);
      else cv=L[0]==="low"?num(LO[j]):L[0]==="mid"?num(MI[j]):num(HI[j]);
      if(cv!==null)hasAny=true;
      var eff=cv===null?base[j]:clamp01(base[j]+cv);
      top.push(eff);
    }
    if(!hasAny)return;
    var d="";
    for(j=0;j<N;j++)d+=(j?"L":"M")+x(j).toFixed(1)+","+yv(top[j]).toFixed(1);
    for(j=N-1;j>=0;j--)d+="L"+x(j).toFixed(1)+","+yv(base[j]).toFixed(1);
    d+="Z";
    parts+='<path d="'+d+'" fill="'+L[1]+'" opacity="0.92"></path>';
    for(j=0;j<N;j++)base[j]=top[j];
  });
  parts+=txt(plot.L+plot.w-4,plot.T+12,layers.map(function(L){return "■"+L[2];}).join(" "),10.5,"end","#475467");
  ctx.svg.innerHTML=parts;
  attachHover(ctx,plot,function(j){
    function gg(a){return a?fmt(num(a[j]),0):"–";}
    if(!LO&&!MI&&!HI)return "<b>"+esc(TIMES[j]||"")+"</b><br>总云量 "+gg(ALLC)+"%";
    return "<b>"+esc(TIMES[j]||"")+"</b><br>低 "+gg(LO)+"% · 中 "+gg(MI)+"% · 高 "+gg(HI)+"%";
  });
  addCursor(ctx,"cloud",plot);
}

/* ============================================================
 * 天气图（仅 --grid）：经纬度线性投影
 * ============================================================ */
var MAPST={};
function lerp(a,b,t){return a+(b-a)*t;}
function hex2rgb(h){return [parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)];}
function rgbStr(c){return "rgb("+Math.round(c[0])+","+Math.round(c[1])+","+Math.round(c[2])+")";}
var C_LO=hex2rgb("#2563eb"),C_MID=hex2rgb("#f8fafc"),C_HI=hex2rgb("#dc2626");
function presColor(t){
  var c=t<0.5?[lerp(C_LO[0],C_MID[0],t*2),lerp(C_LO[1],C_MID[1],t*2),lerp(C_LO[2],C_MID[2],t*2)]
             :[lerp(C_MID[0],C_HI[0],t*2-1),lerp(C_MID[1],C_HI[1],t*2-1),lerp(C_MID[2],C_HI[2],t*2-1)];
  return rgbStr(c);
}
function renderMap(){
  var G=D.grid;
  if(!G||!G.points||!G.points.length)return;
  var xs=G.points.map(function(p){return p.lon;}),ys=G.points.map(function(p){return p.lat;});
  var xmin=Math.min.apply(null,xs),xmax=Math.max.apply(null,xs),
      ymin=Math.min.apply(null,ys),ymax=Math.max.apply(null,ys);
  if(xmax-xmin<1e-6){xmin-=0.5;xmax+=0.5;}
  if(ymax-ymin<1e-6){ymin-=0.5;ymax+=0.5;}
  var W=680,H=Math.max(240,Math.min(500,Math.round(W*(ymax-ymin)/(xmax-xmin))));
  var ctx=mkBox("box-map",W,H+8);
  function px(lon){return (lon-xmin)/(xmax-xmin)*W;}
  function py(lat){return H-(lat-ymin)/(ymax-ymin)*H;}
  var parts="";
  /* 陆地底图（bbox 裁剪+抽稀后内联）*/
  if(D.geo&&D.geo.rings&&D.geo.rings.length){
    var d="";
    D.geo.rings.forEach(function(ring){
      ring.forEach(function(pt,k){
        d+=(k?"L":"M")+px(pt[0]).toFixed(1)+","+py(pt[1]).toFixed(1);
      });
      d+="Z";
    });
    parts+=pth(d,"#edf1f5","#cbd5e1",1);
  }
  parts+=ln(0.5,0.5,W-0.5,0.5,"#cbd5e1",1);
  parts+=ln(0.5,H+0.5,W-0.5,H+0.5,"#cbd5e1",1);
  parts+=ln(0.5,0.5,0.5,H+0.5,"#cbd5e1",1);
  parts+=ln(W-0.5,0.5,W-0.5,H+0.5,"#cbd5e1",1);
  parts+='<g id="map-layer"></g>';
  ctx.svg.innerHTML=parts;
  /* 气压全域归一化（滑动时颜色稳定）*/
  var pmin=Infinity,pmax=-Infinity;
  G.points.forEach(function(_,pi){
    (G.pressure[pi]||[]).forEach(function(v){if(typeof v==="number"){if(v<pmin)pmin=v;if(v>pmax)pmax=v;}});
  });
  if(!isFinite(pmin)){pmin=980;pmax=1040;}
  if(pmax-pmin<0.5){pmax=pmin+0.5;}
  MAPST={ctx:ctx,G:G,px:px,py:py,pmin:pmin,pmax:pmax,W:W};
  var lg=$("map-legend");
  if(lg){
    var swatches="";
    for(var s=0;s<24;s++)swatches+='<i style="background:'+presColor(s/23)+'"></i>';
    lg.innerHTML="<span>"+pmin.toFixed(1)+" hPa</span>"+
      '<span class="bar">'+swatches+'</span>'+
      "<span>"+pmax.toFixed(1)+" hPa</span>";
  }
  updateMap(Math.min(Math.max(0,D.nowIndex||0),Math.max(0,N-1)));
}
function updateMap(idx){
  var M=MAPST;
  if(!M.ctx)return;
  var G=M.G,layer=M.ctx.svg.querySelector("#map-layer");
  if(!layer)return;
  var nt=G.times.length?Math.min(idx,G.times.length-1):0;
  /* 每个格点一个 <g>：原生 SVG <title> 做悬浮提示（离线零依赖），
     组内含气压圆点 + 风矢量（气象约定 wd 为来向，箭头指向去向 wd+180，长度∝风速） */
  var html="";
  G.points.forEach(function(p,pi){
    var pr=typeof (G.pressure[pi]||[])[nt]==="number"?G.pressure[pi][nt]:null;
    var te=typeof (G.temperature[pi]||[])[nt]==="number"?G.temperature[pi][nt]:null;
    var ws=typeof (G.windSpeed[pi]||[])[nt]==="number"?G.windSpeed[pi][nt]:null;
    var wd=typeof (G.windDir[pi]||[])[nt]==="number"?G.windDir[pi][nt]:null;
    var x=M.px(p.lon),y=M.py(p.lat);
    html+='<g><title>'+esc(p.lat+", "+p.lon+"｜气压 "+fmt(pr)+" hPa｜温度 "+fmt(te)+
      " °C｜风 "+fmt(ws)+" m/s "+dirName(wd))+'</title>';
    if(pr!==null){
      var t=(pr-M.pmin)/(M.pmax-M.pmin);
      html+=circ(x.toFixed(1),y.toFixed(1),7,presColor(t),"#94a3b8",0.8);
    }else{
      html+=circ(x.toFixed(1),y.toFixed(1),5,"#f1f5f9","#94a3b8",0.8);
    }
    if(ws!==null&&wd!==null&&ws>0.2){
      var toDir=(wd+180)*Math.PI/180,len=7+Math.min(ws,16)/16*20;
      var x2=x+Math.sin(toDir)*len,y2=y-Math.cos(toDir)*len;
      html+=ln(x.toFixed(1),y.toFixed(1),x2.toFixed(1),y2.toFixed(1),"#334155",1.6);
      var ah=Math.atan2(y2-y,x2-x),hl=5.5,hw=2.6;
      var ax1=x2-hl*Math.cos(ah)-hw*Math.sin(ah),ay1=y2-hl*Math.sin(ah)+hw*Math.cos(ah),
          ax2=x2-hl*Math.cos(ah)+hw*Math.sin(ah),ay2=y2-hl*Math.sin(ah)-hw*Math.cos(ah);
      html+='<path d="M'+x2.toFixed(1)+","+y2.toFixed(1)+
        "L"+ax1.toFixed(1)+","+ay1.toFixed(1)+
        "L"+ax2.toFixed(1)+","+ay2.toFixed(1)+'Z" fill="#334155"></path>';
    }
    html+="</g>";
  });
  layer.innerHTML=html;
}

/* ============================================================
 * Tab 切换 + 滑块联动
 * ============================================================ */
function activateTab(tid){
  document.querySelectorAll(".tab-btn").forEach(function(b){
    var on=b.getAttribute("data-tab")===tid;
    b.classList.toggle("active",on);
    b.setAttribute("aria-selected",on?"true":"false");
  });
  document.querySelectorAll(".panel").forEach(function(p){
    p.classList.toggle("active",p.id==="panel-"+tid);
  });
}
function initTabs(){
  document.querySelectorAll(".tab-btn").forEach(function(b){
    b.addEventListener("click",function(){activateTab(b.getAttribute("data-tab"));});
  });
}
function updateAll(i){
  var lab=$("sl-label");
  if(lab)lab.textContent=(TIMES[i]||"–")+"（第 "+(i+1)+"/"+N+" 时次）";
  updateOverview(i);
  updateRose(i);
  updateMap(i);
  ["pressure","tempprecip","cloud"].forEach(function(k){
    if(CURSORS[k])CURSORS[k](i);
  });
}
function initSlider(){
  var sl=$("time-slider");
  if(!sl||!N)return;
  sl.setAttribute("max",String(N-1));
  var start=Math.min(Math.max(0,D.nowIndex||0),N-1);
  sl.value=String(start);
  sl.addEventListener("input",function(){updateAll(+sl.value);});
}

/* ---- 启动 ---- */
initTabs();
fillExtrema();
try{renderPressure();}catch(e){}
try{renderTempPrecip();}catch(e){}
try{renderRose();}catch(e){}
try{renderCloud();}catch(e){}
try{renderMap();}catch(e){}
initSlider();
updateAll(Math.min(Math.max(0,D.nowIndex||0),Math.max(0,N-1)));
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 页面装配
# ---------------------------------------------------------------------------

def make_html(payload, geo, analysis_html, metrics_html):
    tab_defs = [("overview", "总览"), ("charts", "图表")]
    if payload.get("grid"):
        tab_defs.append(("map", "天气图"))
    if payload.get("metrics") is not None:
        tab_defs.append(("metrics", "指标"))
    if analysis_html is not None:
        tab_defs.append(("analysis", "分析"))

    nav_buttons = "\n".join(
        f'<button class="tab-btn" id="tab-{tid}" data-tab="{tid}" role="tab" '
        f'aria-controls="panel-{tid}" aria-selected="{"true" if tid == "overview" else "false"}">'
        f"{label}</button>"
        for tid, label in tab_defs
    )
    sections = []
    for tid, _label in tab_defs:
        if tid == "overview":
            inner = OVERVIEW_HTML
        elif tid == "charts":
            inner = CHARTS_HTML
        elif tid == "map":
            inner = MAP_HTML
        elif tid == "metrics":
            inner = f'<div class="metrics-wrap">{metrics_html}</div>'
        else:
            inner = f'<div class="analysis-wrap">{analysis_html}</div>'
        active = " active" if tid == "overview" else ""
        sections.append(f'<section class="panel{active}" id="panel-{tid}" role="tabpanel">{inner}</section>')

    loc = payload.get("location") or {}
    sub = []
    if loc.get("latitude") is not None:
        sub.append(f'{loc["latitude"]:g}, {loc["longitude"]:g}')
    if loc.get("timezone"):
        sub.append(str(loc["timezone"]))
    if payload.get("times"):
        sub.append(f'{payload["times"][0]} ~ {payload["times"][-1]}')
    subtitle = " · ".join(sub)

    html = TEMPLATE
    for k, v in {
        "%%TITLE%%": payload.get("title") or "气象面板",
        "%%SUBTITLE%%": subtitle,
        "%%GENERATED%%": payload.get("generatedAt", ""),
        "%%TABS%%": nav_buttons,
        "%%SECTIONS%%": "\n".join(sections),
        "%%PAYLOAD%%": dump_payload_json(payload),
    }.items():
        html = html.replace(k, v)
    return html


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="生成自包含离线交互式 HTML 气象面板（可视化主输出）")
    parser.add_argument("--input", required=True, help="Open-Meteo 单点预报 JSON 路径（必选）")
    parser.add_argument("--output", required=True, help="输出 HTML 路径（必选）")
    parser.add_argument("--metrics", default=None, help="compute_metrics 输出的指标 JSON 路径")
    parser.add_argument("--grid", default=None, help="网格模式点位数组 JSON 路径（启用天气图 tab）")
    parser.add_argument("--analysis", default=None, help="Markdown 分析报告路径（启用分析 tab）")
    parser.add_argument("--title", default=None, help="面板标题文字")
    parser.add_argument("--geojson", default=DEFAULT_GEOJSON, help="世界边界 GeoJSON（默认随技能自带）")
    args = parser.parse_args()

    data = load_json(args.input, "输入数据")
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        log("错误: 输入缺少 hourly.time 数组，无法生成面板")
        raise SystemExit(2)
    log(f"输入: {len(times)} 个时次 ({times[0]} ~ {times[-1]})")

    metrics = None
    if args.metrics:
        metrics = load_json(args.metrics, "指标")
        log(f"指标: 已载入（顶层键 {len(metrics) if isinstance(metrics, dict) else '?'} 个）")

    grid = None
    geo = None
    if args.grid:
        grid_raw = load_json(args.grid, "网格")
        grid = build_grid_payload(grid_raw)
    if grid:
        lats = [p["lat"] for p in grid["points"]]
        lons = [p["lon"] for p in grid["points"]]
        mlon = max(1.5, (max(lons) - min(lons)) * 0.25)
        mlat = max(1.5, (max(lats) - min(lats)) * 0.25)
        bbox = (min(lons) - mlon, min(lats) - mlat, max(lons) + mlon, max(lats) + mlat)
        log(f"底图 bbox: lon {bbox[0]:.2f}~{bbox[2]:.2f}, lat {bbox[1]:.2f}~{bbox[3]:.2f}")
        geo = build_geo_payload(bbox, args.geojson)

    analysis_html = None
    if args.analysis:
        md_text = None
        try:
            with open(args.analysis, "r", encoding="utf-8") as f:
                md_text = f.read()
        except OSError as e:
            log(f"警告: 分析文件不可读({e})，分析 tab 将不出现")
        if md_text is not None:
            analysis_html = render_markdown(sanitize_text(md_text))
            log(f"分析: 已渲染 {len(md_text)} 字符 Markdown")

    metrics_html = render_metrics_tables(metrics) if metrics is not None else None

    title = args.title
    if not title:
        title = f'{data.get("latitude", "?")}, {data.get("longitude", "?")} 气象面板'

    payload = build_payload(data, metrics, grid, title)
    if geo is not None:
        payload["geo"] = geo

    html = make_html(payload, geo, analysis_html, metrics_html)

    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    n_tabs = (2 + (1 if grid else 0) + (1 if metrics is not None else 0)
              + (1 if analysis_html is not None else 0))
    log(f"面板已生成: {args.output} ({os.path.getsize(args.output) // 1024}KB, {n_tabs} 个 tab)")
    if grid and geo is None:
        log("提示: 无陆地底图（GeoJSON 不可用或 bbox 全海域），天气图仍显示格点与风场")


if __name__ == "__main__":
    main()
