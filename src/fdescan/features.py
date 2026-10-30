"""形态特征（设计 §3.3）：在本机由至多 N 个值算出类别标记，原值只在内存里停留，不输出、不落盘。"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

_PHONE = re.compile(r"^\+?(86)?1[3-9]\d{9}$")
# 国际 / 固话格式：至少 7 位数字，且带分隔符或括号或加号（纯数字的编号不算）
_PHONE_INTL = re.compile(r"^(?=(?:\D*\d){7,15}\D*$)\+?[\d\s\-().]+$")
_SENSITIVE_NAME = re.compile(r"(phone|mobile|fax|tel(ephone)?|e_?mail|ssn|id_?card|passport|birth|身份证|手机|电话|邮箱|生日)", re.I)
_ID = re.compile(r"^\d{17}[\dXx]$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DATE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
_DATETIME = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}")
_CODE = re.compile(r"^[A-Za-z0-9_\-./]{1,32}$")
_PCT = re.compile(r"^-?\d+(\.\d+)?%$")
_STATUS_WORDS = {"active", "inactive", "open", "closed", "paid", "unpaid", "shipped", "pending", "done", "draft",
                 "cancelled", "canceled", "approved", "rejected", "new", "完成", "已付", "未付", "已发货", "待审",
                 "草稿", "作废", "有效", "无效", "y", "n", "yes", "no", "true", "false", "0", "1"}


def _majority(flags: list[bool], ratio: float = 0.8) -> bool:
    return bool(flags) and sum(flags) / len(flags) >= ratio


def shape_of(values: list[Any], declared_type: str) -> str | None:
    t = declared_type.lower()
    if not values:
        if any(k in t for k in ("datetime", "timestamp")):
            return "datetime"
        if "date" in t:
            return "date"
        if any(k in t for k in ("int", "serial")):
            return "integer"
        if any(k in t for k in ("dec", "num", "money", "real", "float", "double")):
            return "decimal"
        if "bool" in t or t == "bit":
            return "boolean"
        if "json" in t:
            return "json"
        if "uuid" in t:
            return "uuid"
        return None
    if all(isinstance(v, bool) for v in values):
        return "boolean"
    if all(isinstance(v, datetime) for v in values):
        return "datetime"
    if all(isinstance(v, date) for v in values):
        return "date"
    if all(isinstance(v, int) for v in values):
        return "integer"
    if all(isinstance(v, (int, float, Decimal)) for v in values):
        return "decimal"
    s = [str(v) for v in values]
    if _majority([bool(_DATETIME.match(x)) for x in s]):
        return "datetime"
    if _majority([bool(_DATE.match(x)) for x in s]):
        return "date"
    if _majority([bool(_UUID.match(x)) for x in s]):
        return "uuid"
    if _majority([x.strip().startswith(("{", "[")) for x in s]):
        return "json"
    if _majority([bool(_CODE.match(x)) and not x.isalpha() for x in s]):
        return "code"
    return "text_long" if sum(len(x) for x in s) / len(s) > 60 else "text_short"


def looks_like(values: list[Any]) -> str | None:
    s = [str(v).strip() for v in values if v is not None]
    if not s:
        return None
    for label, rx in (("phone", _PHONE), ("id_card", _ID), ("email", _EMAIL), ("percent", _PCT)):
        if _majority([bool(rx.match(x)) for x in s], 0.6):
            return label
    # 日期 / 时间（如 1996-07-04）也满足"7 位以上数字 ＋ 分隔符"，必须先排除
    datelike = [bool(_DATE.match(x) or _DATETIME.match(x)) for x in s]
    if not _majority(datelike, 0.3) and \
            _majority([bool(_PHONE_INTL.match(x)) and any(ch in x for ch in " -()+.") for x in s], 0.6):
        return "phone"
    distinct = {x.lower() for x in s}
    if len(distinct) <= 20 and _majority([x.lower() in _STATUS_WORDS or (x.isupper() and x.isalpha()) for x in s], 0.6):
        return "status_word"
    return None


def monotonic(values: list[Any]) -> bool | None:
    try:
        return len(values) >= 3 and all(a <= b for a, b in zip(values, values[1:]))
    except TypeError:
        return None


def to_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(v[:19], fmt)
            except ValueError:
                continue
    return None


def span_days(lo: Any, hi: Any) -> int | None:
    """由最早、最晚两个值算出天数；两个值本身不输出（K58 E-3）。"""
    a, b = to_dt(lo), to_dt(hi)
    if a is None or b is None:
        return None
    return max(0, (b - a).days)


def sensitive_name(name: str) -> bool:
    """按列名判断是否疑似个人信息列（电话、邮箱、证件、生日等）；与按取值判断互为补充。"""
    return bool(_SENSITIVE_NAME.search(name or ""))
