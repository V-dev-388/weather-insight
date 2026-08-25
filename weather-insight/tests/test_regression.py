"""weather-insight 黄金样本回归测试（characterization，锁当前算法行为）。

固定输入：tests/fixtures/openmeteo_shanghai_3d.json
（2026-08-24 抓取的上海 31.23N/121.47E 三天真实预报快照，72 小时，
含 8 层气压面要素）。

期望值全部硬编码：由独立重算脚本（不入库）按各指标公开语义
从原始 fixture 现算后填入，运行时不调用被测脚本的函数生成期望值。
当前输出即标准答案——谁改坏数字，这里立即红。

容差约定：
- 一般浮点指标 0.05；
- 降水分日合计与全序列总和的一致性 0.2。

运行方式（在 weather-insight/ 目录下）：
    python3 -B -m unittest discover tests

纯标准库实现，无第三方依赖。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BASE_DIR / "scripts"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "openmeteo_shanghai_3d.json"

TOL_FLOAT = 0.05  # 浮点容差
TOL_PRECIP = 0.2  # 降水日合计容差

# ---- 黄金期望值（独立重算后硬编码，禁止运行时生成）----
GOLD_PRESSURE_Y_MIN = 1004.7
GOLD_PRESSURE_Y_MAX = 1010.6
GOLD_EXTREMA_HIGH_VALUE = 1010.6
GOLD_EXTREMA_HIGH_TIME = "2026-08-24T23:00"
GOLD_EXTREMA_LOW_VALUE = 1004.7
GOLD_EXTREMA_LOW_TIME = "2026-08-26T16:00"

GOLD_TEMP_RANGE_MIN = 25.4
GOLD_TEMP_RANGE_MAX = 32.3
GOLD_DAILY_PRECIP_MM = {
    "2026-08-24": 0.1,
    "2026-08-25": 0.4,
    "2026-08-26": 0.0,
}

# K 指数独立重算检查点：(小时下标, 期望值)
GOLD_K_INDEX_CHECKPOINTS = [
    (0, 28.49),   # 2026-08-24T00:00
    (35, 17.03),  # 2026-08-25T11:00
    (71, 19.77),  # 2026-08-26T23:00
]

METRIC_KEYS = [
    "k_index",
    "showalter_index_approx",
    "cape_approx_j_kg",
    "wind_shear_850_200_ms",
    "pressure_gradient_3h_hpa",
    "temperature_anomaly_c",
]


def _run_script(script_name, *args):
    """以子进程方式运行被测脚本（不 import、不 mock）。"""
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script_name), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _load_fixture():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        return json.load(f)


def _run_chart(chart_type):
    result = _run_script("chart_data.py", "--input", str(FIXTURE), "--type", chart_type)
    if result.returncode != 0:
        raise AssertionError(f"chart_data --type {chart_type} 退出码 {result.returncode}\n{result.stderr}")
    return json.loads(result.stdout)


class TestChartPressureRegression(unittest.TestCase):
    """chart_data pressure：黄金样本上的极值与最高气压时刻。"""

    def test_y_min_y_max_and_extrema_high_match_hand_calc(self):
        out = _run_chart("pressure")
        self.assertAlmostEqual(out["y_min"], GOLD_PRESSURE_Y_MIN, delta=TOL_FLOAT)
        self.assertAlmostEqual(out["y_max"], GOLD_PRESSURE_Y_MAX, delta=TOL_FLOAT)
        self.assertIsNotNone(out["extrema"])
        self.assertAlmostEqual(
            out["extrema"]["high"]["value"], GOLD_EXTREMA_HIGH_VALUE, delta=TOL_FLOAT
        )
        self.assertEqual(out["extrema"]["high"]["time"], GOLD_EXTREMA_HIGH_TIME)
        # 低值同样锁定，防止 min/max 被改反或时刻取法被改
        self.assertAlmostEqual(
            out["extrema"]["low"]["value"], GOLD_EXTREMA_LOW_VALUE, delta=TOL_FLOAT
        )
        self.assertEqual(out["extrema"]["low"]["time"], GOLD_EXTREMA_LOW_TIME)


class TestChartTempPrecipRegression(unittest.TestCase):
    """chart_data temp_precip：全温区间与降水分日合计。"""

    def test_temp_range_matches_hand_calc(self):
        out = _run_chart("temp_precip")
        self.assertAlmostEqual(out["temp_range"]["min"], GOLD_TEMP_RANGE_MIN, delta=TOL_FLOAT)
        self.assertAlmostEqual(out["temp_range"]["max"], GOLD_TEMP_RANGE_MAX, delta=TOL_FLOAT)

    def test_daily_precipitation_sums_to_series_total(self):
        out = _run_chart("temp_precip")
        daily = out["daily_precipitation"]
        # 各日 total_mm 与黄金值一致（含「无降水日也出现在结果里」的行为）
        self.assertEqual(set(daily.keys()), set(GOLD_DAILY_PRECIP_MM.keys()))
        for day, expect_mm in GOLD_DAILY_PRECIP_MM.items():
            self.assertAlmostEqual(daily[day]["total_mm"], expect_mm, delta=TOL_PRECIP)
        # 各日合计 == 全序列 precipitation 总和 ±0.2（独立从 fixture 求和）
        hourly = _load_fixture()["hourly"]
        series_total = sum(v for v in hourly["precipitation"] if v is not None)
        daily_total = sum(daily[d]["total_mm"] for d in daily)
        self.assertAlmostEqual(daily_total, series_total, delta=TOL_PRECIP)


class TestChartWindRoseRegression(unittest.TestCase):
    """chart_data wind_rose：16 方位 bins 计数守恒。"""

    def test_bins_count_sum_equals_valid_wind_hours(self):
        out = _run_chart("wind_rose")
        bins_total = sum(b["count"] for b in out["bins"])
        hourly = _load_fixture()["hourly"]
        ws = hourly["wind_speed_10m"]
        wd = hourly["wind_direction_10m"]
        n_valid = sum(
            1 for i in range(min(len(ws), len(wd)))
            if ws[i] is not None and wd[i] is not None
        )
        self.assertEqual(bins_total, n_valid)
        self.assertEqual(bins_total, 72)  # 本黄金样本无缺测，全部小时有效


class TestComputeMetricsRegression(unittest.TestCase):
    """compute_metrics：键齐全、值为 number|null、K 指数独立重算一致。"""

    def test_metrics_keys_complete_and_k_index_matches_recompute(self):
        result = _run_script("compute_metrics.py", "--input", str(FIXTURE))
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        metrics = out["metrics"]
        for key in METRIC_KEYS:
            self.assertIn(key, metrics, f"缺少指标键 {key}")
            self.assertIsInstance(metrics[key], list, f"{key} 应为数组")
            for v in metrics[key]:
                self.assertTrue(
                    v is None or isinstance(v, (int, float)),
                    f"{key} 含非法值 {v!r}（应为 number 或 null）",
                )
        for idx, expect_k in GOLD_K_INDEX_CHECKPOINTS:
            got = metrics["k_index"][idx]
            self.assertIsNotNone(got, f"k_index[{idx}] 不应为 null")
            self.assertAlmostEqual(got, expect_k, delta=TOL_FLOAT)

    def test_degraded_input_without_pressure_levels_no_crash(self):
        data = _load_fixture()
        hourly = data.get("hourly", {})
        for name in [k for k in list(hourly) if k.endswith("hPa")]:
            del hourly[name]  # 删掉全部气压面字段
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(data, tmp, ensure_ascii=False)
            tmp_path = tmp.name
        try:
            result = _run_script("compute_metrics.py", "--input", tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0, f"残缺点位输入不应崩溃\n{result.stderr}")
        out = json.loads(result.stdout)  # stdout 仍为完整可解析 JSON
        metrics = out["metrics"]
        for key in METRIC_KEYS:
            self.assertIn(key, metrics)
            self.assertIsInstance(metrics[key], list)
        # 结构完整性：时间范围仍指向原始序列，降水分级与倾向结构仍在
        hourly_orig = _load_fixture()["hourly"]
        self.assertEqual(out["time_range"]["start"], hourly_orig["time"][0])
        self.assertEqual(out["time_range"]["end"], hourly_orig["time"][-1])
        self.assertIsInstance(out.get("precipitation_classification"), dict)
        self.assertIsInstance(out.get("weather_system_tendency"), list)


if __name__ == "__main__":
    unittest.main()
