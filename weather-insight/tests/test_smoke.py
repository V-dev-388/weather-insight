"""weather-insight 权威源脚本最小回归冒烟测试。

对 scripts/ 下六个脚本逐一执行 `python3 <script> --help`，
断言退出码为 0，确保脚本可被解释器加载且 argparse 入口完好。

运行方式（在 weather-insight/ 目录下）：
    python3 -m unittest discover tests

纯标准库实现，无第三方依赖。
"""

import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

SCRIPTS = [
    "fetch_openmeteo.py",
    "compute_metrics.py",
    "chart_data.py",
    "fetch_satellite.py",
    "render_weather_map.py",
    "run_pipeline.py",
]


class TestScriptSmoke(unittest.TestCase):
    """六个脚本的 --help 冒烟：加载失败或 argparse 损坏时立即暴露。"""

    def test_scripts_list_matches_directory(self):
        """SCRIPTS 清单与 scripts/ 目录中的实际脚本保持一致。"""
        actual = sorted(p.name for p in SCRIPTS_DIR.glob("*.py"))
        self.assertEqual(actual, sorted(SCRIPTS))


def _make_help_test(script_name):
    def test(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / script_name), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"{script_name} --help 退出码 {result.returncode}\n{result.stderr}",
        )
    return test


for _name in SCRIPTS:
    setattr(
        TestScriptSmoke,
        f"test_help_{_name.removesuffix('.py')}",
        _make_help_test(_name),
    )


if __name__ == "__main__":
    unittest.main()
