# HTML 面板规范（主输出）

可视化模式的主输出是 `scripts/render_dashboard.py` 生成的**自包含交互式 HTML 面板**：一条命令把 Open-Meteo JSON 变成双击即开、完全离线的单文件页面。本节定义其输出契约；原 show_widget 手写 SVG 规范降为文末后备附录，仅无文件系统平台使用。

## 命令与参数

```bash
python3 scripts/render_dashboard.py --input <openmeteo.json> --output <panel.html> \
    [--metrics <metrics.json>] [--grid <grid数组.json>] [--analysis <markdown>] [--title <标题>]
```

- `--input` / `--output` 必选；其余可选 tab（天气图/指标/分析）在对应参数缺省时整块隐藏，不留空壳。
- 端到端可用 `run_pipeline.py --html <路径>` 一次跑完；面板生成失败只警告、不中断流水线。

## 输出契约

1. **零外部依赖（硬要求）**：CSS/JS/SVG 全部内联，产物中不得出现任何外部链接字样；断网双击即可打开。
2. **数据嵌入**：原始数据放 `<script type="application/json" id="dashboard-data">` 标签内，数值不做重舍入（验收会比对数组长度与极值）；`</` 转义防提前闭合脚本标签。
3. **tab 结构**：总览（天气码+要素卡片+极值表）/ 图表（气压趋势折线、温度-降水双轴、16 方位风玫瑰、低中高云量堆叠面积，全部手写 SVG）/ 天气图（仅 --grid）/ 指标（仅 --metrics）/ 分析（仅 --analysis）。
4. **交互底线**：tab 切换；折线悬停显示该时刻数值；时间滑块拖动联动总览卡片与天气图时刻，默认停在最接近生成时刻的位置。
5. **健壮性**：Open-Meteo 数组含 null 时 JS 必须跳过、不得抛错；分层云量缺失时退化为总云量单层。
6. **天气图底图**：从 resources/world_countries.geojson 按 bbox 裁剪 + 隔点抽稀后内联（目标 <60KB，禁止整块内联 592KB 全量），经纬度线性投影画 SVG，不上 Leaflet/D3。
7. **配色沿用附录的浅色主题方案**（气压蓝→红发散、温度冷热渐变、降水白蓝等），保证面板与后备路线视觉一致。

---

# 后备附录：show_widget 手写 SVG 渲染规范（无文件系统平台用）

以下规范仅在运行环境无文件系统/无法打开本地 HTML 时，用平台专属 `show_widget` 现场手绘内联 SVG 使用；有文件系统时一律优先走上方 render_dashboard.py 面板路线。

## 一、show_widget 调用流程

每次渲染图表前，先调用 `read_me` 加载对应模块：

```
read_me(modules: ["chart"])  // 柱状图/折线图/面积图
read_me(modules: ["diagram"])  // 矢量图/剖面图/风玫瑰
```

然后调用 `show_widget`，参数：
- `title`：图表标识（中文，下划线分隔，如"北京气压趋势"）
- `widget_code`：SVG 代码片段（viewBox 从 `0 0 680` 开始）
- `loading_messages`：加载提示（中文，1-4条）

## 二、通用规范

### 尺寸与坐标系
- viewBox：`0 0 680 400`（宽680，高400，标准）
- 剖面图可用 `0 0 680 500`（更高）
- 图表绘制区：左边距60（Y轴标签）、右边距30、上边距50（标题）、下边距60（X轴标签）

### 浅色主题配色（light theme）
- 背景：`#ffffff` 或 `#fafbfc`
- 文字：`#1a1a1a`（主）/ `#666666`（次）
- 网格线：`#e8e8e8`
- 坐标轴：`#cccccc`

### 气象配色方案

| 要素 | 配色 | 说明 |
|------|------|------|
| 气压 | 低`#3b82f6`蓝 → 高`#ef4444`红 | 发散色阶，标注高低压 |
| 温度 | 冷`#3b82f6`蓝 → 暖`#ef4444`红 | 渐变 |
| 降水 | `#ffffff`白 → `#1e40af`深蓝 | 白蓝渐变 |
| 云量 | `#ffffff`白 → `#6b7280`灰 | 白灰渐变 |
| 风 | `#10b981`绿（弱）→ `#dc2626`红（强） | 风力等级 |

> 每个形状都必须显式设置 fill 属性，避免 fallback 到黑色。

## 三、各类图表规范

### 1. 气压趋势折线图

**数据**：`hourly.pressure_msl`（海平面气压，hPa）

**SVG 结构**：
```
<svg viewBox="0 0 680 400">
  <!-- 标题 -->
  <text x="340" y="30" text-anchor="middle" font-size="18" fill="#1a1a1a">海平面气压趋势 (hPa)</text>
  <!-- 网格 + Y轴 -->
  <!-- Y轴范围：气压min-5 ~ max+5，每2hPa一格 -->
  <!-- 折线 path：M x0,y0 L x1,y1 ... -->
  <path d="..." fill="none" stroke="#3b82f6" stroke-width="2"/>
  <!-- 高低压标注：气压极值点画圆 + 文字标注 H/L -->
  <!-- X轴：时间标签（每6或12小时一个） -->
  <!-- Y轴：气压值标签 -->
</svg>
```

**数据处理**：
- 72小时数据点太多时，每3小时取一点（24个点）
- Y轴范围：`[min-5, max+5]`，确保波动可见
- 标注气压极值点（最高标"H"红色，最低标"L"蓝色）

**解读模板**：
- 气压 > 1020 hPa 且稳定/上升 → 高压系统，晴好天气
- 气压 < 1000 hPa 且下降 → 低压系统靠近，天气转坏
- 3小时变压 > 3 hPa → 气压系统快速移动，天气变化剧烈
- 气压平稳（日变化<3hPa）→ 天气稳定

### 2. 温度-降水组合图

**数据**：`hourly.temperature_2m` + `hourly.precipitation`

**结构**：双轴图
- 左Y轴：温度（°C），折线，红色
- 右Y轴：降水（mm），柱状，蓝色
- X轴：时间

**解读**：温度高+降水=闷热雷阵雨；温度骤降+降水=冷锋过境

### 3. 风玫瑰图

**数据**：`hourly.wind_direction_10m` + `hourly.wind_speed_10m`

**结构**：极坐标
- 16方位（每22.5°）
- 每方位的频率用扇形大小表示
- 风速用颜色深浅表示

**解读**：主导风向指示天气系统移向；强风对应大风预警

### 4. 云量分层面积图

**数据**：`cloud_cover_low` + `cloud_cover_mid` + `cloud_cover_high`

**结构**：堆叠面积图
- X轴：时间
- Y轴：0-100%
- 三层堆叠：低云（底）+ 中云 + 高云（顶）

**解读**：低云厚=阴雨；高云为主=晴好但有卷云；三层都厚=系统性降水

### 5. 温度垂直剖面图

**数据**：多层气压面温度 `temperature_1000hPa` 至 `temperature_200hPa`

**结构**：填色图
- X轴：时间
- Y轴：气压（1000hPa底，200hPa顶，上小下大）
- 填色：温度色阶（蓝-红）

**解读**：逆温层（下层冷上层暖）= 稳定层结；递减率大= 不稳定

### 6. 高空风矢量图

**数据**：`wind_speed_850hPa` + `wind_direction_850hPa`

**结构**：矢量箭头图
- X轴：时间
- 箭头方向=风向，箭头长度=风速
- 可叠加 500hPa 风场对比

**解读**：高空风引导天气系统移动；850hPa低空急流=强降水潜势

## 四、数据提取

从 Open-Meteo JSON（`--parse` 输出）提取图表数据：

```python
data = json.load(open(filepath))
hourly = data["hourly"]
times = hourly["time"]           # ["2026-07-08T00:00", ...]
pressure = hourly["pressure_msl"] # [998.7, 998.6, ...]
temp = hourly["temperature_2m"]
```

> 数据点过多时降采样：72点→24点（每3小时），或取每天4个关键时点。

## 五、多图组合

复杂场景可连续调用多个 `show_widget`，每张图之间写一段解读文字。不要把所有数据塞进一张图。

示例流程：
1. `read_me(["chart"])`
2. `show_widget` 气压趋势图 + 解读
3. `show_widget` 温度降水图 + 解读
4. `show_widget` 云量图 + 解读
5. 综合小结

## 六、地图天气图（空间分布可视化）

### 技术栈
- **D3.js v7**：`https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js`
- **TopoJSON v3**：`https://cdnjs.cloudflare.com/ajax/libs/topojson/3.0.2/topojson.min.js`
- **底图数据**：`https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json`（Natural Earth 110m）

### 底图规范

```javascript
const projection = d3.geoMercator()
  .center([中心经度, 中心纬度])  // 东亚: [115, 35], 欧洲: [15, 50], 北美: [-95, 40]
  .scale(600)                      // 调整缩放
  .translate([W/2, H/2 + 15]);
const path = d3.geoPath(projection);
```

- 海洋：`#dce6f0`（浅色） / `#16213e`（深色）
- 陆地：`#eaedf2`（浅色） / `#2d2d44`（深色）
- 国界：`#bbb`（浅色） / `#444`（深色），线宽 0.3-0.5
- 中国轮廓：`#d43`，线宽 1.2

### 格点数据获取

```bash
python3 scripts/fetch_openmeteo.py --lat 中心纬度 --lon 中心经度 \
  --grid 行x列 --grid-step 间距 --url-only
# → WebFetch 获取 JSON
# → 提取当前时刻数据
```

网格模式返回数组，每个元素 `{latitude, longitude, hourly: {time:[], pressure_msl:[], wind_speed_10m:[], wind_direction_10m:[]}}`

### 格点渲染

- 圆形标记：半径 10-12，`fill` = 气压色标，`stroke` = `#333`（线宽 0.8）
- 气压数值：圆上方 16px，`text-anchor: middle`，字号 11，`font-weight: 500`
- 风速标注：圆下方 22px，字号 9，灰色

### 气压色标

蓝（`#3b82f6`）→ 白（`#ffffff`）→ 红（`#ef4444`）
```javascript
function pressureColor(p, pMin, pMax) {
  const t = (p - pMin) / (pMax - pMin);
  if (t < 0.5) return d3.interpolateRgb('#3b82f6', '#ffffff')(t * 2);
  return d3.interpolateRgb('#ffffff', '#ef4444')((t - 0.5) * 2);
}
```

### 风矢量

箭头从格点中心指向风去向（wd-180°转换），长度 ∝ 风速：
```javascript
const rad = (wd - 180) * Math.PI / 180;
const len = Math.max(ws * 2, 5);  // 风速×2，最小5px
// 箭头三角形：顶点 + 两个后角
```

### 色标图例

右侧纵向梯度条 + 刻度标签，覆盖 `[pMin, pMax]`。

### 完整示例

参考之前渲染的"中国东部天气图 · 7月8日 21:00"演示——东亚底图 + 3×3 格点（真实 Open-Meteo 数据）+ 气压色标 + 风矢量。
