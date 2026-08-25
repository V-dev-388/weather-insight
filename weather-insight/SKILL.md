---
name: weather-insight
description: 气象数据可视化与专业分析技能。当用户需要查看、解读或可视化气象数据（气压、温度、风、降水、云量、卫星云图等这类非专业人士难以读懂的原始数据），或需要生成专业气象分析报告时，使用此技能。提供三种模式：可视化模式（用内联图表把原始气象数据变成普通人能懂的图表并配解读）、地图天气图模式（按查询范围自适应格点密度，叠加地理底图展示气压场/风场/天气系统的空间分布）、分析报告模式（基于原始数据做气象建模级深度分析，输出结构化专业报告）。数据源为 Open-Meteo（免费、含 GFS/ECMWF/CMA 多模型预报）与风云四号卫星云图平台。触发场景包括：气象可视化、气象分析、卫星云图、气压分析、温度解读、风场分析、降水分析、天气系统识别、天气图、地图天气、城市天气、区域天气、气象报告等。
agent_created: true
---

# Weather Insight — 气象数据可视化与分析

## 概述

将公开的气象原始数据（气压、温度、风、降水、云量、多层气压面、卫星云图等非专业人士极难读懂的数据）转化为两种能力：

1. **可视化模式（visualize）**：主输出为运行 `scripts/render_dashboard.py` 生成的**自包含交互式 HTML 面板**——完全离线、双击即开，含总览卡片、四类手写 SVG 图表（气压趋势 / 温度-降水双轴 / 风玫瑰 / 云量堆叠）、可选天气图 tab 与时间滑块联动，把原始数据变成普通人能看懂的图。
2. **分析报告模式（analyze）**：基于原始数据 + 计算指标，由运行此技能的 LLM 做气象建模级深度分析，输出结构化专业报告。

两种模式共享同一数据获取层，可联动交互。

## 多 Agent 并行架构

本技能设计为多 agent 并行处理，以大幅提升速度：

**核心原则**：数据获取、可视化渲染、分析报告三阶段尽量并行，减少串行等待。

**并行策略**：
- **地图天气图**：大网格（>20 格点）拆分为 2-4 个批次，用多个 curl 命令并行获取各批次（大 payload 下 WebFetch 可能截断，仅作兜底），最后合并渲染。拆分为 N 批可将数据获取耗时降至 ~1/N。
- **分析报告**：数据获取完成即开始分析，不等待可视化完成。两者共享同一份原始数据。
- **视觉元素**：交互面板由 `render_dashboard.py` 一次性离线生成（零 CDN 依赖），不占用对话轮次与 token，质量稳定；仅无文件系统平台才退回 show_widget 手绘。

**工作流示意**（地图天气图）：
```
curl(批次1) ─┐
curl(批次2) ─┤→ 合并数据 → render_dashboard(天气图/面板渲染)
curl(批次N) ─┘
                    └→ compute_metrics → 分析报告
```

**大网格拆分示例**：8×6=48 格点 → 拆为 2 批各 24 点（上下半区），或 4 批各 12 点（四象限）。每批 URL 由 `--url-only` 分别生成。

## 数据源

- **Open-Meteo**（主数据源）：免费、无需 API Key、全球可访问。提供海平面气压、地面气压、2m温度、多高度风、降水、分层云量、天气码，以及 1000-200hPa 共 8 层气压面数据（温度/湿度/风/位势高度）。内置 GFS/ECMWF IFS/CMA GRAPES 等多模型预报。
- **卫星云图**：风云四号官方平台（引导用户查看真实云图）+ 用 Open-Meteo 云量数据生成云覆盖示意图。

详细 API 参数与备选数据源见 [references/data-sources.md](references/data-sources.md)。

## 数据获取工作流

脚本默认智能获取数据：自动尝试多种网络策略直连 Open-Meteo API，成功则直接输出 JSON；若直连失败，则降级为 curl 直连或 WebFetch 三步法。

### 方式一：直接运行脚本（默认，优先尝试）

```bash
python3 scripts/fetch_openmeteo.py --lat <纬度> --lon <经度> [--days <天数>] [--models <模型>] [--summary]
```

脚本自动尝试标准请求与绕代理直连。成功时输出完整 JSON（或 `--summary` 摘要）；失败时输出 API URL 并提示用 curl 或 WebFetch 获取。

### 方式二：curl 直连（脚本直连失败时，或大网格场景优先）

1. 生成 URL：`python3 scripts/fetch_openmeteo.py --lat <纬度> --lon <经度> --url-only`
2. curl 直连获取完整 JSON：
   ```bash
   curl -s --max-time 60 "<API_URL>" -o /tmp/weather_<lat>_<lon>.json
   ```
3. 校验完整性后再解析：
   ```bash
   python3 scripts/fetch_openmeteo.py --parse /tmp/weather_<lat>_<lon>.json --summary
   ```

> 大网格（>20 格点）必须用本方式：响应 payload 常超 100KB，WebFetch 中转可能截断 JSON；curl 直连无此问题。多批次并行时，用多个并行 Bash 调用分别执行 curl。

### 方式三：WebFetch 三步法（小 payload 适用）

1. 生成 URL：`python3 scripts/fetch_openmeteo.py --lat <纬度> --lon <经度> --url-only`
2. 用 WebFetch 工具访问该 URL（prompt：`Return ONLY the raw JSON data exactly as received.`），获取 JSON 后写入临时文件（如 `/tmp/weather_<lat>_<lon>.json`）
3. 解析：`python3 scripts/fetch_openmeteo.py --parse /tmp/weather_<lat>_<lon>.json --summary`

> 优先尝试方式一，失败则用方式二。单点或小网格（≤20 点）可用方式三；>20 点优先方式二 curl，WebFetch 仅作 curl 也不可达时的兜底，且获取后必须校验 JSON 完整性。

## 坐标解析

用户输入城市名而非坐标时，按以下优先级解析经纬度：

1. 用户直接给坐标则直接使用
2. 用 WebSearch 查询"城市名 经纬度 / coordinates"（适用于全球任意城市，优先方式）
3. 常见城市快捷坐标（中国：北京 39.9,116.4 / 上海 31.2,121.5 / 广州 23.1,113.3 / 深圳 22.5,114.1 / 成都 30.6,104.0 / 杭州 30.2,120.1 / 武汉 30.5,114.3 / 西安 34.2,108.9 / 重庆 29.5,106.5 / 南京 32.0,118.8 / 天津 39.0,117.2 / 哈尔滨 45.7,126.6 / 乌鲁木齐 43.8,87.6 / 拉萨 29.6,91.1 / 海口 20.0,110.3 / 台北 25.0,121.5 / 香港 22.3,114.2 / 澳门 22.2,113.5；国际：东京 35.7,139.7 / 首尔 37.6,127.0 / 新加坡 1.4,103.8 / 纽约 40.7,-74.0 / 伦敦 51.5,-0.1 / 巴黎 48.9,2.4 / 悉尼 -33.9,151.2 / 莫斯科 55.8,37.6 / 迪拜 25.3,55.3 / 孟买 19.1,72.9）

## 模式一：可视化模式（visualize）

**目的**：把难读的原始气象数据变成普通人能懂的图表 + 解读。

**判断触发**：用户说"看看天气""可视化气象数据""气压图""云图""温度变化""风场""降水情况"等。

**流程**：
1. 解析用户输入（城市/坐标 + 时间范围，默认当前至未来 7 天）
2. 按"数据获取工作流"获取 Open-Meteo 数据
3. 运行 `python3 scripts/render_dashboard.py --input <openmeteo.json> --output <panel.html> [--metrics <metrics.json>] [--title <标题>]` 生成交互式 HTML 面板（图表规范见 [references/visualization-guide.md](references/visualization-guide.md)）
4. 面板交付后配一段普通人语言解读（不堆术语，讲清"这图说明什么天气"）；无文件系统平台改走 show_widget 后备路线手绘图表并逐图配解读

**图表方案**（按需选择，不必全部渲染）：

| 图表 | 类型 | 数据要素 | 解读要点 |
|------|------|----------|----------|
| 气压趋势 | 折线图 | 海平面气压随时间 | 标注高/低压系统，说明气压升降意味着什么天气 |
| 温度-降水组合 | 双轴图 | 温度折线 + 降水柱状 | 体感温度与降水配合，是否闷热/凉爽 |
| 风玫瑰 | 极坐标图 | 风向频率 + 风速 | 主导风向、风力等级，用风级描述 |
| 云量分层 | 堆叠面积图 | 低/中/高云量 | 云系结构，晴/多云/阴的时段 |
| 高空风场 | 矢量图 | 850hPa/500hPa 风速风向 | 高空风引导天气系统移动方向 |
| 温度垂直剖面 | 填色图 | 多层气压面温度 | 大气垂直结构，是否稳定 |

**卫星云图处理**：
- 运行 `python3 scripts/fetch_satellite.py --platform-urls` 获取官方云图平台链接，告知用户可查看真实卫星云图
- 用 `--cloud-cover` 从已获取的 Open-Meteo 数据提取云量，在可视化中展示云覆盖示意

**解读原则**：
- 先说结论（"今天晴热，午后可能有雷阵雨"），再解释数据依据
- 用生活化比喻（"气压像这样下降，通常是有低压系统靠近，天气要转坏"）
- 标注关键时间点（"下午2点温度最高36°C""晚上8点后降水概率增大"）

### 无文件系统平台的后备路线：show_widget

面板需要把 HTML 落地到文件系统；当运行环境无法写文件或无法打开本地 HTML 时，才退回用平台专属 `show_widget` 现场手绘内联 SVG（先调用 `read_me` 加载 chart/diagram 模块）。规范：

- 浅色主题适配（light theme）：浅色背景 + 深色文字
- 气压用蓝-红发散色阶；降水用白-蓝渐变；温度用蓝(冷)-红(热)
- viewBox 从 `0 0 680` 开始
- 每个形状都显式设置 fill，避免 fallback 到黑色
- 手写 SVG 模板详见 [references/visualization-guide.md](references/visualization-guide.md) 后备附录

### 地图天气图模式（空间分布可视化）

**目的**：在地理底图上展示气象要素的空间分布。在区域网格上采样，用颜色表示气压、箭头表示风，直观看到天气系统的空间结构（类似经典天气图）。

**触发**：用户说"天气图""地图""气压分布""风场分布""区域天气""空间可视化"等。

**自适应网格密度**：根据用户查询的范围自动调整格点间距——单城市密集（5km 间距），广域稀疏（5°间距）。无需手动指定参数。

| 查询范围 | Scope | 间距 | 网格 | 覆盖范围 | 典型场景 |
|---------|-------|------|------|----------|----------|
| 单城市 | city | 0.05° | 5×5 | ~0.2°×0.2° | 北京/上海/纽约 |
| 小区域（县市级）| local | 0.25° | 5×5 | ~1.2°×1.2° | 北京市域/长三角 |
| 区域（省级） | region | 1° | 7×5 | ~5°×7° | 华东/华北/日本 |
| 国家 | country | 2° | 8×6 | ~12°×16° | 中国/美国/德国 |
| 广域（洲际） | wide | 5° | 8×6 | ~30°×40° | 东亚/欧洲/北美 |
| 全球 | global | 10° | 9×7 | ~70°×90° | 全球天气系统 |

**LLM 推断 scope 的规则**：
- 用户提具体城市名 → **city**（如"北京天气图""上海的气压"）
- 用户提县/区/小镇 → **local**
- 用户提省/州/区域名 → **region**（如"浙江省""关东地区"）
- 用户提国家名 → **country**
- 用户提大区域/洲 → **wide**（如"东亚""欧洲"）
- 用户提全球/世界 → **global**
- 范围不明 → 默认 **region**

**流程**：
1. 根据用户输入推断 scope（单城市密集 → 广域稀疏）
2. 用 `--scope` 自动选密度，生成 API URL：
   ```bash
   # 单城市 - 5km 间距，25 个格点
   python3 scripts/fetch_openmeteo.py --lat 39.9 --lon 116.4 --scope city --url-only
   # 国家级 - 2° 间距，48 个格点
   python3 scripts/fetch_openmeteo.py --lat 35 --lon 115 --scope country --url-only
   ```
3. 对于大网格（>20点），按多 Agent 架构拆分为多个批次，分别生成各批次 URL
4. 并行发起多个 `curl -s --max-time 60 "<批次URL>" -o <批次文件>` 获取各批次 JSON，逐个校验完整性（WebFetch 仅作兜底）
5. 合并数据，提取当前时刻各格点气压、温度、风速、风向
6. 生成天气图面板（首选）：把合并后的网格数组 JSON 落地，连同任一单点 Open-Meteo JSON 一起交给 render_dashboard：
   ```bash
   python3 scripts/render_dashboard.py --input <单点openmeteo.json> --grid <网格数组.json> --output <panel.html>
   ```
   面板「天气图」tab：经纬度线性投影画 SVG + 陆地底图（按 bbox 从 resources/world_countries.geojson 裁剪抽稀内联），格点圆点按气压蓝(低)→白→红(高)着色、箭头表示风向风速、带色标图例；拖时间滑块可逐时刻查看
7. （后备）无文件系统平台用 `show_widget` 手绘精细天气图：
   - 底图：D3.js + world-atlas TopoJSON（Natural Earth），Mercator 投影
   - 格点：圆形标记，填充色=气压（蓝=低→白=中→红=高），标注数值
   - 等压线：在密集格点间连接近似的等压线
   - 风矢量：箭头，方向=风来向，长度∝风速
   - H/L 中心标记：自动识别气压极值点
8. 配文字解读：高/低压系统位置、风场特征、天气趋势

**手动指定**（覆盖自适应）：`--grid ROWSxCOLS --grid-step DEG`
**查看密度配置**：`--show-density`

**解读原则**：
- 气压低 + 风逆时针旋转 = 气旋/低压系统，天气不稳定
- 气压高 + 风顺时针旋转 = 反气旋/高压系统，天气晴好
- 气压梯度大（邻点差>3hPa）= 强风区
- 参考 [references/analysis-methods.md](references/analysis-methods.md) 的天气系统识别规则

## 模式二：分析报告模式（analyze）

**目的**：基于原始数据 + 计算指标，由 LLM 做气象建模级深度分析，输出专业报告。

**判断触发**：用户说"气象分析""分析报告""天气系统""大气稳定性""气压梯度""气象建模"等。

**流程**：
1. 按"数据获取工作流"获取 Open-Meteo 完整数据
2. 运行 `python3 scripts/compute_metrics.py --input <json文件>` 计算气象指标
3. LLM 基于原始数据 + 指标 JSON 做建模级分析：
   - **天气系统识别**：高/低压系统位置与移动、锋面判定、气旋/反气旋
   - **大气稳定性评估**：基于 K指数/Showalter指数/CAPE 近似判断对流潜势
   - **气压梯度与风场关系**：地转风偏差、梯度风、辐合辐散
   - **温度/降水异常检测**：与近期均值的偏差、极端性判断
   - **短期天气趋势推断**：基于气压趋势 + 风场演变 + 稳定性的综合推断
4. 输出结构化 Markdown 专业报告

**报告结构**（参考 [examples/sample-report.md](examples/sample-report.md)）：

```
# 气象分析报告 — <地点> <时间范围>

## 一、数据摘要
- 位置、时间范围、数据源、模型
- 关键要素极值表

## 二、气象指标
- 气压梯度、稳定性指数、降水分级、风切变等指标表

## 三、天气系统分析
- 识别到的气压系统、锋面、气旋
- 系统移动趋势

## 四、大气稳定性分析
- 对流潜势评估
- 层结稳定性

## 五、温度与降水分析
- 温度距平
- 降水特征与强度分级

## 六、短期趋势判断
- 未来天气演变推断
- 不确定性说明

## 七、数据与方法说明
- 数据源、模型、指标算法、局限性
```

**LLM 分析要求**：
- 基于指标数据推理，不做无依据的判断
- 明确区分"数据支撑的结论"与"推断"
- 标注不确定性
- 用气象专业术语，但关键结论附通俗解释

## 脚本说明

### `scripts/fetch_openmeteo.py` — Open-Meteo 数据获取与解析

默认智能获取：自动尝试多种网络策略直连，成功输出 JSON；失败输出 URL 供 WebFetch 获取。
- `--url-only`：仅输出 API URL
- `--parse <file>`：解析已下载的 JSON 文件，`--summary` 输出摘要
- `--summary`：输出数据摘要而非完整 JSON
- `--grid ROWSxCOLS --grid-step DEG`：网格多坐标模式（地图天气图用），生成多坐标 API URL

参数：`--lat --lon --days(默认7) --past-days --models`

### `scripts/fetch_satellite.py` — 卫星云图引导与云量提取

- `--platform-urls`：输出官方卫星云图平台 URL 列表
- `--cloud-cover <file>`：从 Open-Meteo JSON 提取分层云量数据
- `--cloud-cover <file> --summary`：输出云量统计 + 天空状况分类
- `--image-urls`：输出可能的云图图片 URL（可达性不保证）

### `scripts/compute_metrics.py` — 气象指标计算

- `--input <file>`：从 Open-Meteo JSON 计算气象指标
- 输出：气压梯度、K指数、Showalter指数、CAPE近似、降水分级、风切变、温度距平、天气系统倾向评分

详细算法与阈值见 [references/analysis-methods.md](references/analysis-methods.md)。

### `scripts/render_weather_map.py` — 空间天气图渲染

- `--input <file>`：网格气象 JSON 文件路径
- `--geojson <file>`：地理边界 GeoJSON 文件路径
- `--output <file>`：输出 SVG 地图路径
- `--time <timestamp>`：要渲染的预报时刻（例如 2026-07-08T21:00）

根据多点网格自动识别格点尺寸、计算中心，并叠加世界 GeoJSON 边界，在地理底图上进行气压色彩渐变插值、绘制等压线和风场箭头，生成专业的天气图。

### `scripts/render_dashboard.py` — 交互式 HTML 面板生成（可视化主输出）

- `--input <file>`：Open-Meteo 单点预报 JSON 路径（必选）
- `--output <file>`：输出 HTML 面板路径（必选）
- `--metrics <file>`：compute_metrics 输出的指标 JSON（提供时启用「指标」tab）
- `--grid <file>`：网格模式点位数组 JSON，同 render_weather_map 输入格式（提供时启用「天气图」tab）
- `--analysis <file>`：Markdown 分析报告（提供时启用「分析」tab）
- `--title <text>`：面板标题文字

产出单文件自包含面板：CSS/JS/SVG 全部内联、原始数据嵌入 `dashboard-data` 标签、零外部链接、完全离线可用。页面为 tab 结构（总览/图表/天气图/指标/分析，可选 tab 缺参数时自动隐藏），支持 tab 切换、折线悬停查值、时间滑块联动总览卡片与天气图时刻；默认停在最接近生成时刻的位置。

### `scripts/run_pipeline.py` — 端到端流水线

- `--lat --lon [--days]`：获取数据并计算指标，stdout 输出结构化摘要 JSON
- `--scope <范围>`：网格模式（见地图天气图模式）
- `--output <dir>`：保存原始 JSON 到目录
- `--no-metrics`：跳过指标计算
- `--html <path>`：末尾调用 render_dashboard.py 生成交互式面板到指定路径；面板失败只打警告，不中断流水线

## 资源文件

- [resources/world_countries.geojson](resources/world_countries.geojson)：全球低分辨率地理边界数据，保障任意区域的通用绘制能力；render_dashboard.py 生成天气图时也用它作底图（按 bbox 裁剪+隔点抽稀后内联，不整块嵌入）
- [references/data-sources.md](references/data-sources.md)：Open-Meteo API 参数详解、风云四号获取方式、备选数据源
- [references/analysis-methods.md](references/analysis-methods.md)：气象分析算法、判断阈值、指标计算方法
- [references/visualization-guide.md](references/visualization-guide.md)：各类图表的 SVG 渲染规范与模板
- [examples/sample-report.md](examples/sample-report.md)：分析报告模板示例

## 错误处理

- **脚本直连失败**：脚本自动降级输出 URL，改用 curl 直连或 WebFetch 工具获取 JSON 再 `--parse` 解析
- **大网格 JSON 截断**：>20 格点的响应常超 100KB，WebFetch 可能返回截断数据（`--parse` 报解析错误或格点数与请求不符）。改用 `curl -s --max-time 60 "<URL>" -o <文件>` 直连获取完整 JSON
- **WebFetch 返回非 JSON**：检查 URL 是否正确，重试，或用简化参数的 URL
- **卫星云图图片不可达**：用 `--platform-urls` 引导用户查看官方平台，用云量数据生成示意图
- **坐标解析失败**：用 WebSearch 查询，或提示用户提供经纬度
- **API 限流**：Open-Meteo 非商业限制 10000次/天，正常使用不会触发
- **南半球坐标**：纬度为负值，算法通用，无需特殊处理
