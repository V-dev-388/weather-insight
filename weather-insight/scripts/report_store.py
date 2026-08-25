#!/usr/bin/env python3
"""report_store.py — 气象面板/报告产物归档器。

把生成的 HTML 面板/报告按日期归档留底：

    <store-dir>/YYYY-MM-DD/REPORT_YYYYMMDD_HHMM[_标签].html

同时维护：
  - <store-dir>/latest.html  指向最新一份的相对软链（环境不支持时降级为复制并注明）
  - <store-dir>/README.md    索引（每次归档后整体重建，新→旧列出 时间/标签/大小/相对路径）

目录布局与索引思路沿用滴水湖气象分析系统的成熟先例（该系统已归档，
此处仅借鉴行为约定，不 import 其任何代码）。纯标准库实现。

用法（在 weather-insight/ 目录或任意工作目录下）：
    python3 scripts/report_store.py --file <report.html> [--label 标签] [--store-dir reports]

死规矩：
  - 只 copy 不 mv：用户原文件必须原地保留；
  - 已有同名归档绝不覆盖，自动让位为 _1/_2… 递增后缀；
  - README.md 每次从磁盘全量重建，绝不做增量追加。

退出码：成功 0；--file 不存在/非常规文件时报错退出 2。
"""

import argparse
import os
import re
import shutil
import sys
from datetime import datetime

# 归档文件名约定：REPORT_YYYYMMDD_HHMM[_标签].html（标签段不含路径分隔符）
NAME_RE = re.compile(r"^REPORT_(\d{8})_(\d{4})(?:_(.+))?\.html$")
LABEL_MAX = 48


def sanitize_label(raw):
    """把任意文字压成可安全进文件名的标签；压完为空则视为无标签。"""
    label = re.sub(r"[^\w.\-]+", "-", raw.strip())  # \w 含 Unicode 字母数字下划线
    label = label.strip("-.")
    if len(label) > LABEL_MAX:
        label = label[:LABEL_MAX].rstrip("-.")
    return label or None


def human_size(num):
    """字节数转人类可读大小。"""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0


def unique_destination(day_dir, base_name):
    """挑一个不冲突的目标名：已有同名一律让位（_1、_2…），永不覆盖。"""
    name = base_name + ".html"
    bump = 0
    while os.path.lexists(os.path.join(day_dir, name)):  # lexists 连坏软链也算占用
        bump += 1
        name = f"{base_name}_{bump}.html"
    return name, bump


def refresh_latest(store_dir, archive_rel):
    """刷新 latest.html 指向最新归档。优先相对软链；失败则降级为复制。"""
    latest_path = os.path.join(store_dir, "latest.html")
    if os.path.lexists(latest_path):
        os.remove(latest_path)  # 旧软链或旧降级副本一并清掉再重建
    try:
        os.symlink(archive_rel, latest_path)
        return "relative-symlink"
    except (OSError, NotImplementedError):
        # 典型场景：无权限、Windows 未开发者模式等
        shutil.copy2(os.path.join(store_dir, archive_rel), latest_path)
        return "copied-fallback"


def scan_archives(store_dir):
    """递归扫描全部归档，返回 [(相对路径, 文件名)]，新→旧排序。

    排序主键是文件名内的时间戳；同分钟的多份归档（如带不同标签）
    再按落盘时刻 st_ctime_ns 降序区分先后——不能直接对路径做字典序，
    否则「甲/乙」这类标签会因码位大小排出假的新旧关系。
    """
    found = []
    for root, dirs, files in os.walk(store_dir):
        dirs.sort()
        for fn in sorted(files):
            if fn == "latest.html":  # latest 是入口不是归档本体
                continue
            m = NAME_RE.match(fn)
            if not m:
                continue
            rel = os.path.relpath(os.path.join(root, fn), store_dir)
            try:
                tie = os.stat(os.path.join(root, fn)).st_ctime_ns
            except OSError:
                tie = 0
            found.append((m.group(1), m.group(2), tie, rel, fn))
    found.sort(key=lambda e: (e[0], e[1], e[2]), reverse=True)
    return [(rel, fn) for _d8, _t4, _tie, rel, fn in found]


def rebuild_index(store_dir):
    """整体重建 README.md 索引（从磁盘扫描，不是追加）。"""
    rows = scan_archives(store_dir)
    lines = [
        "# 气象报告归档索引",
        "",
        f"- 归档总数：{len(rows)} 份（由 scripts/report_store.py 每次归档时全量重建）",
    ]
    if rows:
        lines.append(
            f"- 最新一份：[{rows[0][1]}]({rows[0][0]})（入口见 latest.html）"
        )
    else:
        lines.append("- 暂无归档")
    lines += [
        "",
        "## 报告清单（新 → 旧）",
        "",
        "| # | 时间 | 标签 | 大小 | 相对路径 |",
        "|---|------|------|------|----------|",
    ]
    for i, (rel, fn) in enumerate(rows, 1):
        m = NAME_RE.match(fn)
        if m:
            d8, t4, label = m.group(1), m.group(2), m.group(3)
            pretty = f"{d8[:4]}-{d8[4:6]}-{d8[6:8]} {t4[:2]}:{t4[2:]}"
        else:
            pretty, label = "-", None
        try:
            size = human_size(os.path.getsize(os.path.join(store_dir, rel)))
        except OSError:
            size = "?"
        mark = " ⭐最新" if i == 1 else ""
        lines.append(
            f"| {i}{mark} | {pretty} | {label or '-'} | {size} | [{fn}]({rel}) |"
        )
    lines.append("")
    index_path = os.path.join(store_dir, "README.md")
    with open(index_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return index_path, len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "归档气象面板/报告 HTML：<store-dir>/YYYY-MM-DD/REPORT_时间戳[_标签].html，"
            "维护 latest.html 入口与 README.md 索引（只拷贝不移动，永不覆盖旧归档）"
        )
    )
    parser.add_argument(
        "--file", required=True,
        help="要归档的 HTML 文件路径（必选；只 copy，原文件原地保留）",
    )
    parser.add_argument(
        "--label", default=None,
        help="可选文字标签，进入文件名：REPORT_YYYYMMDD_HHMM_<标签>.html",
    )
    parser.add_argument(
        "--store-dir", default="reports",
        help="归档根目录（默认 reports/，相对当前工作目录）",
    )
    args = parser.parse_args(argv)

    src = args.file
    if not os.path.isfile(src):
        print(f"错误：--file 不存在或不是常规文件：{src}", file=sys.stderr)
        return 2

    store_dir = args.store_dir
    now = datetime.now()
    day_dir = os.path.join(store_dir, now.strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)

    base_name = f"REPORT_{now.strftime('%Y%m%d_%H%M')}"
    label = sanitize_label(args.label) if args.label else None
    if label:
        base_name += f"_{label}"

    name, bump = unique_destination(day_dir, base_name)
    dest = os.path.join(day_dir, name)
    shutil.copy2(src, dest)  # 只拷贝保原件，绝不 mv
    archive_rel = os.path.relpath(dest, store_dir)

    mode = refresh_latest(store_dir, archive_rel)
    index_path, total = rebuild_index(store_dir)

    print(f"已归档: {dest}")
    print(f"原件保留(copy 非 mv): {os.path.abspath(src)}")
    if bump:
        print(f"注意: 同分钟同名归档已存在，自动让位为 {name}，未覆盖任何旧文件")
    if mode == "relative-symlink":
        print(f"latest.html -> {archive_rel}（相对软链）")
    else:
        print(f"latest.html -> {archive_rel}（本环境不支持软链，已降级为复制并在此注明）")
    print(f"索引已重建: {index_path}（共 {total} 条，新→旧）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
