"""温控核查请求的严格输入校验。

所有非法输入都被拒绝并给出 JSON Pointer 形式的精确定位（如
``/samples/0/entry``），由 FastAPI 以 ``422`` 返回。校验规则覆盖：

- 根对象、必填字段、类型（布尔不冒充整数、NaN/Infinity 不接受）；
- 未知字段一律拒绝（严格模式）；
- 水样 1~12 份、日志 2~100 点、ID 非空且唯一、入箱早于出箱；
- 温度区间下限 ≤ 上限、越界上限非负；
- 日志时间严格递增；
- δ 整数闭区间下界 ≤ 上界；
- 十进制字符串格式（不接受科学计数法）。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from .temperature import LogPoint, WaterSample, parse_decimal

_ROOT_KEYS = {"delta_lo", "delta_hi", "log", "samples"}
_LOG_KEYS = {"time", "temp"}
_SAMPLE_KEYS = {
    "id",
    "entry",
    "exit",
    "lower_temp",
    "upper_temp",
    "max_excursion_seconds",
}
_DECIMAL_RE_PARTS = ("temp", "lower_temp", "upper_temp", "max_excursion_seconds")


class RequestValidationError(Exception):
    """请求不合法；errors 为 (json_pointer, message) 列表。"""

    def __init__(self, errors: list[tuple[str, str]]) -> None:
        if not errors:
            raise ValueError("至少需要一个校验错误")
        self.errors = errors
        super().__init__("; ".join(f"{path}: {msg}" for path, msg in errors))


def _is_int(value: Any) -> bool:
    """严格整数：排除布尔值。"""

    return isinstance(value, int) and not isinstance(value, bool)


def _check_object(value: Any, path: str, allowed: set[str]) -> None:
    if not isinstance(value, dict):
        raise RequestValidationError([(path, "必须是 JSON 对象")])
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise RequestValidationError(
            [(f"{path}/{unknown[0]}", "未知字段，请求不允许出现该字段")]
        )
    missing = sorted(allowed - set(value))
    if missing:
        raise RequestValidationError(
            [(f"{path}/{missing[0]}", "缺少必填字段")]
        )


def _require_int(value: Any, path: str) -> int:
    if not _is_int(value):
        raise RequestValidationError([(path, "必须是整数（UTC 整数秒）")])
    return value


def _require_decimal(value: Any, path: str) -> Fraction:
    if not isinstance(value, str):
        raise RequestValidationError([(path, "必须是十进制字符串，例如 \"8.5\"")])
    try:
        return parse_decimal(value)
    except ValueError as exc:
        raise RequestValidationError([(path, str(exc))]) from exc


def _require_nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RequestValidationError([(path, "必须是非空、非空白字符串")])
    return value


def validate_temperature_request(payload: Any) -> tuple[
    int, int, list[LogPoint], list[WaterSample]
]:
    """完整校验并返回可直接送入算法的精确数值对象。"""

    _check_object(payload, "", _ROOT_KEYS)
    delta_lo = _require_int(payload["delta_lo"], "/delta_lo")
    delta_hi = _require_int(payload["delta_hi"], "/delta_hi")
    if delta_lo > delta_hi:
        raise RequestValidationError(
            [("/delta_lo", "delta_lo 必须小于或等于 delta_hi")]
        )

    log_raw = payload["log"]
    if not isinstance(log_raw, list):
        raise RequestValidationError([("/log", "必须是数组")])
    if not 2 <= len(log_raw) <= 100:
        raise RequestValidationError(
            [("/log", "温度点数量必须在 2 到 100 之间")]
        )

    log: list[LogPoint] = []
    previous_time: int | None = None
    for index, point in enumerate(log_raw):
        path = f"/log/{index}"
        _check_object(point, path, _LOG_KEYS)
        time_value = _require_int(point["time"], f"{path}/time")
        temp_value = _require_decimal(point["temp"], f"{path}/temp")
        if previous_time is not None and time_value <= previous_time:
            raise RequestValidationError(
                [(f"{path}/time", "日志时间必须按时间严格递增")]
            )
        previous_time = time_value
        log.append(LogPoint(time=time_value, temp=temp_value))

    samples_raw = payload["samples"]
    if not isinstance(samples_raw, list):
        raise RequestValidationError([("/samples", "必须是数组")])
    if not 1 <= len(samples_raw) <= 12:
        raise RequestValidationError(
            [("/samples", "水样数量必须在 1 到 12 份之间")]
        )

    samples: list[WaterSample] = []
    seen_ids: set[str] = set()
    for index, sample in enumerate(samples_raw):
        path = f"/samples/{index}"
        _check_object(sample, path, _SAMPLE_KEYS)

        sample_id = _require_nonempty_string(sample["id"], f"{path}/id")
        if sample_id in seen_ids:
            raise RequestValidationError(
                [(f"{path}/id", f"样品 ID 重复：{sample_id}")]
            )
        seen_ids.add(sample_id)

        entry = _require_int(sample["entry"], f"{path}/entry")
        exit_value = _require_int(sample["exit"], f"{path}/exit")
        if entry >= exit_value:
            raise RequestValidationError(
                [(f"{path}/entry", "入箱时间必须严格早于出箱时间")]
            )

        lower = _require_decimal(sample["lower_temp"], f"{path}/lower_temp")
        upper = _require_decimal(sample["upper_temp"], f"{path}/upper_temp")
        if lower > upper:
            raise RequestValidationError(
                [(f"{path}/lower_temp", "下限必须小于或等于上限")]
            )

        limit = _require_decimal(
            sample["max_excursion_seconds"],
            f"{path}/max_excursion_seconds",
        )
        if limit < 0:
            raise RequestValidationError(
                [
                    (
                        f"{path}/max_excursion_seconds",
                        "累计越界时长上限必须是非负数",
                    )
                ]
            )

        samples.append(
            WaterSample(
                sample_id=sample_id,
                entry=entry,
                exit=exit_value,
                lower=lower,
                upper=upper,
                limit=limit,
            )
        )

    return delta_lo, delta_hi, log, samples
