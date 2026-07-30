# 数据源参考文档

## 一、Open-Meteo（主数据源）

### 概述

- **官网**：https://open-meteo.com
- **API**：`https://api.open-meteo.com/v1/forecast`
- **费用**：非商业免费，无需 API Key，每日 < 10,000 次调用
- **覆盖**：全球，内置多模型预报
- **网络获取**：脚本默认智能直连，失败时降级用 WebFetch 工具通道获取（见 SKILL.md 数据获取工作流）

### URL 构造

```
https://api.open-meteo.com/v1/forecast?
  latitude=<纬度>
  &longitude=<经度>
  &hourly=<变量列表,逗号分隔>
  &timezone=auto
  &forecast_days=<天数,默认7>
  &past_days=<过去天数,0-92>
  &models=<模型,可选>
  &wind_speed_unit=ms
  &temperature_unit=celsius
  &precipitation_unit=mm
```

### 地面层变量（hourly）

| 变量 | 单位 | 说明 |
|------|------|------|
| `pressure_msl` | hPa | 海平面气压（折算至平均海平面） |
| `surface_pressure` | hPa | 地面气压（随海拔降低） |
| `temperature_2m` | °C | 2m 气温 |
| `relative_humidity_2m` | % | 2m 相对湿度 |
| `dew_point_2m` | °C | 2m 露点温度 |
| `apparent_temperature` | °C | 体感温度（综合风寒/湿度/辐射） |
| `wind_speed_10m` | m/s | 10m 风速 |
| `wind_direction_10m` | ° | 10m 风向（0=北,90=东） |
| `wind_gusts_10m` | m/s | 10m 阵风 |
| `wind_speed_80m` | m/s | 80m 风速 |
| `wind_direction_80m` | ° | 80m 风向 |
| `precipitation` | mm | 总降水（雨+阵雪+雪） |
| `rain` | mm | 大尺度降雨 |
| `showers` | mm | 对流性阵性降水 |
| `snowfall` | cm | 降雪量 |
| `precipitation_probability` | % | 降水概率（集合预报） |
| `cloud_cover` | % | 总云量 |
| `cloud_cover_low` | % | 低层云（<3km） |
| `cloud_cover_mid` | % | 中层云（3-8km） |
| `cloud_cover_high` | % | 高层云（>8km） |
| `weather_code` | - | WMO 天气代码 |
| `visibility` | m | 能见度 |

### 多层气压面变量

气压层：1000, 925, 850, 700, 500, 300, 250, 200 hPa（覆盖边界层至对流层顶）

每层可用变量（命名格式 `<变量>_<层>hPa`）：

| 变量 | 单位 | 说明 |
|------|------|------|
| `temperature_<L>hPa` | °C | 该层温度 |
| `relative_humidity_<L>hPa` | % | 该层相对湿度 |
| `wind_speed_<L>hPa` | m/s | 该层风速 |
| `wind_direction_<L>hPa` | ° | 该层风向 |
| `geopotential_height_<L>hPa` | m | 位势高度 |

示例：`temperature_850hPa, wind_speed_500hPa, geopotential_height_500hPa`

> 完整 URL 由 `fetch_openmeteo.py --url-only` 自动生成，包含全部地面变量 + 8 层气压面变量。

### 可用模型

| 模型 ID | 提供方 | 国家 | 分辨率 | 预报时长 |
|---------|--------|------|--------|----------|
| `best_match` | 自动选择 | - | - | - |
| `gfs_seamless` | NOAA | 美国 | 3-25km | 16天 |
| `gfs_hrrr` | NOAA | 美国 | 3km | 18小时 |
| `ecmwf_ifs025` | ECMWF | 欧盟 | 9-25km | 15天 |
| `ecmwf_aifs025` | ECMWF(AI) | 欧盟 | 25km | 15天 |
| `cma_grapeseamless` | CMA | 中国 | 15km | 10天 |
| `icon_seamless` | DWD | 德国 | 2-11km | 7.5天 |
| `jma_seamless` | JMA | 日本 | 5-55km | 11天 |
| `meteofrance_seamless` | Météo-France | 法国 | 1-25km | 4天 |
| `ukmo_seamless` | UK Met Office | 英国 | 2-10km | 7天 |
| `kma_seamless` | KMA | 韩国 | 1.5-13km | 12天 |

多模型对比：同一地点用不同 `--models` 参数分别获取，对比预报差异。

### WMO 天气代码对照

| 代码 | 天气 |
|------|------|
| 0 | 晴 |
| 1-3 | 少云/多云 |
| 45,48 | 雾 |
| 51-57 | 毛毛雨 |
| 61-67 | 雨 |
| 71-77 | 雪 |
| 80-82 | 阵雨 |
| 85-86 | 阵雪 |
| 95 | 雷暴 |
| 96,99 | 雷暴伴冰雹 |

## 二、卫星云图

### 官方平台（供浏览器查看真实云图）

| 平台 | URL | 覆盖区域 | 特点 |
|------|-----|----------|------|
| 国家卫星气象中心-风云四号平台 | http://rsapp.nsmc.org.cn/geofy/?i18n=zh | 中国/亚太 | 真彩色/红外/水汽，支持区域选择和时间动画 |
| 风云卫星-NSMC | https://www.nsmc.org.cn/nsmc/cn/home/index.html | 全球 | 风云二号/三号/四号产品门户 |
| 中央气象台-卫星气象 | http://www.nmc.cn/publish/satellite/weather.html | 中国 | 中央气象台云图产品 |
| 和风天气-风云四号 | https://www.qweather.com/satellite/fengyun4-asia-tc.html | 亚太 | 亚太真彩色，每小时更新 |
| 中央气象台台风网 | http://typhoon.nmc.cn/web.html | 西北太平洋 | 台风路径与云图叠加 |
| NOAA STAR GOES | https://www.star.nesdis.noaa.gov/GOES/ | 美洲/大西洋 | GOES-16/17 真彩色/红外云图 |
| EUMETSAT | https://www.eumetsat.int/monitoring-clouds | 欧洲/非洲/大西洋 | Meteosat 卫星云图 |
| Himawari Monitor (NICT) | https://himawari8.nict.go.jp/ | 亚太/印度洋 | Himawari-8/9 真彩色实时云图 |
| NASA Worldview | https://worldview.earthdata.nasa.gov/ | 全球 | 多卫星合成真彩色影像，支持历史回溯 |

> 真实卫星云图图片直链受反爬/动态加载影响难以稳定获取。可视化窗口推荐用 Open-Meteo 云量数据生成云覆盖示意图，同时引导用户访问上述平台查看真实云图。

### 云量数据示意图

用 `fetch_satellite.py --cloud-cover <json> --summary` 从 Open-Meteo 数据提取分层云量，输出：
- 各层云量统计（avg/min/max）
- 天空状况分类（晴朗/少云/多云/阴天）

在 show_widget 中用堆叠面积图渲染低/中/高云量随时间变化。

## 三、备选数据源

### ECMWF CDS（欧洲中期天气预报中心）

- **官网**：https://cds.climate.copernicus.eu
- **特点**：全球最权威再分析/预报数据（ERA5）
- **要求**：需注册 + API Key
- **适用**：长期历史分析、专业研究

### CMA 国家气象数据网（中国气象局）

- **官网**：https://data.cma.cn
- **特点**：国内地面观测密集，历史数据丰富
- **要求**：需注册账号
- **适用**：国内站点历史分析

### NOAA GFS（美国国家海洋和大气管理局）

- **获取**：AWS S3 (`noaa-gfs-bdp-pds`) 或 NCEP FTP
- **特点**：全球预报，原始 GRIB2 格式，需 wgrib2 解码
- **适用**：已通过 Open-Meteo 的 `--models gfs_seamless` 间接获取

### Himawari（日本气象厅卫星）

- **官网**：https://www.eorc.jaxa.jp/ptree/
- **特点**：真彩色/红外云图，亚太覆盖
- **适用**：风云四号的备选卫星云图源

## 四、数据获取注意事项

1. **网络可达性**：脚本默认智能直连，失败时用 WebFetch 工具通道（见 SKILL.md 数据获取工作流）
2. **数据时效**：forecast API 提供未来最多 16 天预报；历史数据用 past_days（最多 92 天）
3. **时间分辨率**：默认逐小时；部分模型支持 15 分钟
4. **时区**：`timezone=auto` 自动使用当地时区
5. **单位**：统一用 `wind_speed_unit=ms`（米/秒）、`temperature_unit=celsius`、`precipitation_unit=mm`
