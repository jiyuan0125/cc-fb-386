#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compose.py —— 用运营覆盖合并主配置，生成生效权益配置。

输入：
  benefits.json            主配置（权益平台维护，按权益码升序，额度记分）
  overrides.csv            运营覆盖（一行一条：权益组,领域,字段,覆盖值，额度记元）

输出：
  effective.json           生效配置（格式同 benefits.json，按权益码升序）
  published/effective-<指纹>.json  当次生效内容的定版
  published/发布记录.csv    追加一行发布记录（重跑不加行）
  核对.txt                  计数与未匹配覆盖的核对清单
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BENEFITS_PATH = ROOT / "benefits.json"
OVERRIDES_PATH = ROOT / "overrides.csv"
EFFECTIVE_PATH = ROOT / "effective.json"
CHECK_PATH = ROOT / "核对.txt"
PUBLISHED_DIR = ROOT / "published"
RECORD_PATH = PUBLISHED_DIR / "发布记录.csv"

# 权益记录的字段（顺序即输出顺序）
FIELDS = ("权益码", "名称", "额度", "生效位")
# 发布记录的列名
RECORD_HEADER = ("时间", "生效权益数", "被覆盖字段数", "指纹")


# --------------------------------------------------------------------------- #
# 读取输入
# --------------------------------------------------------------------------- #
def read_benefits(path: Path) -> list[dict]:
    """读取主配置。"""
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"{path.name} 应为权益数组")
    return [dict(item) for item in data]


def read_overrides(path: Path) -> list[dict]:
    """读取运营覆盖，同时保留每行的原始文本（核对需逐行照抄）。"""
    text = path.read_text(encoding="utf-8-sig")
    raw_lines = text.splitlines()
    parsed = list(csv.reader(raw_lines))
    if not raw_lines:
        return []

    header = [cell.strip() for cell in parsed[0]]
    rows: list[dict] = []
    for index in range(1, len(raw_lines)):
        raw = raw_lines[index]
        if not raw.strip():  # 跳过空行
            continue
        cells = parsed[index] if index < len(parsed) else []
        values = {
            name: (cells[i].strip() if i < len(cells) else "")
            for i, name in enumerate(header)
        }
        rows.append(
            {
                "权益组": values.get("权益组", ""),
                "领域": values.get("领域", ""),
                "字段": values.get("字段", ""),
                "覆盖值": values.get("覆盖值", ""),
                "原文": raw.rstrip("\r"),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #
def group_matches(code: str, group: str) -> bool:
    """权益组是否匹配权益码（权益码形如 BAS01，组一般为前缀 BAS）。"""
    if not group:
        return False
    if code == group:  # 组直接写成完整权益码
        return True
    return code.startswith(group) and code[len(group):].isdigit()


def yuan_to_fen(text: str) -> int:
    """覆盖值以元计（可到厘），换成主配置的「分」并按四舍五入取整。"""
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"无法解析额度覆盖值：{text!r}") from None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def build_effective(
    benefits: list[dict], overrides: list[dict]
) -> tuple[list[dict], dict[tuple[str, str], object], list[dict]]:
    """主配置合并覆盖，返回生效记录、已覆盖字段、未匹配权益组的覆盖。"""
    applied: dict[tuple[str, str], object] = {}
    unmatched: list[dict] = []

    for override in overrides:
        group = override["权益组"]
        if not any(group_matches(item["权益码"], group) for item in benefits):
            unmatched.append(override)
            continue

        field = override["字段"]
        if field not in FIELDS:  # 不认识的字段无法落到记录里，忽略
            continue

        raw_value = override["覆盖值"]
        if raw_value == "":  # 留空的声明不构成覆盖，不抹掉同组已有的值
            continue

        value = yuan_to_fen(raw_value) if field == "额度" else raw_value
        # 同一组同一字段被先后声明时，后声明的为准
        applied[(group, field)] = value

    effective = []
    for benefit in benefits:
        record = dict(benefit)
        for (group, field), value in applied.items():
            if group_matches(record["权益码"], group):
                record[field] = value
        effective.append(record)

    # 保持按权益码升序
    effective.sort(key=lambda item: item["权益码"])
    return effective, applied, unmatched


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def serialize(records: list[dict]) -> bytes:
    """与 benefits.json 相同的记录格式。"""
    return (json.dumps(records, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def fingerprint(payload: bytes) -> str:
    """生效内容的指纹（MD5 前 12 位十六进制）。"""
    return hashlib.md5(payload).hexdigest()[:12]


def last_record_fingerprint(path: Path) -> str | None:
    """发布记录中最后一行的指纹，没有记录时返回 None。"""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8-sig")
    rows = list(csv.reader(text.splitlines()))
    for row in reversed(rows):
        if len(row) >= len(RECORD_HEADER):
            value = row[3].strip()
            if value:
                return value
    return None


def append_record(when: str, benefit_count: int, covered_count: int, digest: str) -> bool:
    """追加一行发布记录；内容与最近一次发布相同时不加行。"""
    if last_record_fingerprint(RECORD_PATH) == digest:
        return False

    record = [when, str(benefit_count), str(covered_count), digest]
    RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not RECORD_PATH.exists() or RECORD_PATH.stat().st_size == 0:
        with RECORD_PATH.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(RECORD_HEADER)
            writer.writerow(record)
        return True

    ends_with_newline = RECORD_PATH.read_bytes()[-1:] == b"\n"
    with RECORD_PATH.open("a", encoding="utf-8", newline="") as fh:
        if not ends_with_newline:
            fh.write("\n")
        csv.writer(fh, lineterminator="\n").writerow(record)
    return True


def write_check(
    benefits: list[dict],
    overrides: list[dict],
    effective: list[dict],
    unmatched: list[dict],
) -> None:
    """重写核对.txt：三行计数、空行、未匹配覆盖的清单。"""
    lines = [
        f"主配置条数：{len(benefits)}",
        f"覆盖条数：{len(overrides)}",
        f"生效条数：{len(effective)}",
        "",
        "匹配不到权益组的覆盖：",
    ]
    if unmatched:
        lines.extend(item["原文"] for item in unmatched)
    else:
        lines.append("无")
    CHECK_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
def main() -> int:
    benefits = read_benefits(BENEFITS_PATH)
    overrides = read_overrides(OVERRIDES_PATH)

    effective, applied, unmatched = build_effective(benefits, overrides)

    payload = serialize(effective)
    digest = fingerprint(payload)

    EFFECTIVE_PATH.write_bytes(payload)
    PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = PUBLISHED_DIR / f"effective-{digest}.json"
    snapshot.write_bytes(payload)

    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    appended = append_record(when, len(effective), len(applied), digest)
    write_check(benefits, overrides, effective, unmatched)

    print(f"主配置 {len(benefits)} 条，覆盖 {len(overrides)} 条，"
          f"生效 {len(effective)} 条，被覆盖字段 {len(applied)} 个")
    print(f"指纹 {digest}，定版 published/{snapshot.name}")
    print("发布记录：" + ("已追加一行" if appended else "内容未变，未追加"))
    print(f"匹配不到权益组的覆盖：{len(unmatched)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
