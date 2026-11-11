"""冷藏箱温控核查的精确算法（全程有理数，无浮点误差）。

约定（与请求字段对应）：

- 记录仪日志按自身时钟给出 ``(time, temp)``，时间严格递增，段内线性插值。
- 真实时间 = 日志时间 + δ；δ 是全箱共用的未知整数秒时差。
- 水样在真实时间 ``[entry, exit]`` 内位于箱中，换到日志坐标为
  ``[entry-δ, exit-δ]``。
- 温度等于允许区间端点不算越界；只有严格低于下限或严格高于上限才计时。
- 越界累计时长 = 越界指示函数（可测函数）在整个在箱区间上的积分。

对一份水样，越界累计时长 ``f(δ)`` 是 δ 的连续分段线性函数，其全部折点
（日志段端点平移、与上下限的交点平移）都是有理数。因此 ``f(δ) ≤ C``
的解集是若干闭区间（含孤立点）的并，可用 Fraction 精确求出，端点直接
是最简分数。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Sequence

__all__ = [
    "LogPoint",
    "WaterSample",
    "SampleResult",
    "BoxVerdict",
    "analyze_box",
    "parse_decimal",
    "format_fraction",
    "intersect_unions",
]


@dataclass(frozen=True)
class LogPoint:
    """一条温度日志（日志时钟，UTC 整数秒）。"""

    time: int
    temp: Fraction


@dataclass(frozen=True)
class WaterSample:
    """一份水样的核查输入。"""

    sample_id: str
    entry: int
    exit: int
    lower: Fraction
    upper: Fraction
    limit: Fraction


@dataclass(frozen=True)
class SampleResult:
    """单份水样的核查结论。"""

    sample_id: str
    verdict: str  # all | partial | none
    feasible_intervals: tuple[tuple[Fraction, Fraction], ...]


@dataclass(frozen=True)
class BoxVerdict:
    """全箱核查结论。"""

    delta_lo: int
    delta_hi: int
    results: tuple[SampleResult, ...]
    intersection: tuple[tuple[Fraction, Fraction], ...]
    verdict: str  # all | partial | none


def parse_decimal(text: str) -> Fraction:
    """把十进制字符串精确解析为 Fraction。

    接受可选符号、整数部分、可选小数点（``12``、``-3.5``、``.25``、
    ``7.`` 均可），不接受科学计数法、空白或其它字符。
    """

    if not isinstance(text, str):
        raise TypeError("decimal 必须是字符串")
    s = text.strip()
    sign = 1
    if s[:1] in ("+", "-"):
        if s[:1] == "-":
            sign = -1
        s = s[1:]
    if not s or s == ".":
        raise ValueError(f"非法十进制字符串: {text!r}")
    if "." in s:
        whole, _, frac = s.partition(".")
        if not whole and not frac:
            raise ValueError(f"非法十进制字符串: {text!r}")
        if whole and not whole.isdigit():
            raise ValueError(f"非法十进制字符串: {text!r}")
        if frac and not frac.isdigit():
            raise ValueError(f"非法十进制字符串: {text!r}")
        whole_value = int(whole) if whole else 0
        value = Fraction(whole_value * 10 ** len(frac) + int(frac or "0"),
                         10 ** len(frac))
    else:
        if not s.isdigit():
            raise ValueError(f"非法十进制字符串: {text!r}")
        value = Fraction(int(s))
    return sign * value


def format_fraction(value: Fraction) -> str:
    """最简分数输出：整数不带分母，其余为 ``p/q``。"""

    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _segment_temp(p0: LogPoint, p1: LogPoint, u: Fraction) -> Fraction:
    """段 [p0.time, p1.time] 内日志时刻 u 的线性插值温度。"""

    span = p1.time - p0.time
    return p0.temp + (p1.temp - p0.temp) * (u - p0.time) / span


def _indicator_intervals(
    log: Sequence[LogPoint],
    bound: Fraction,
    *,
    above: bool,
) -> list[tuple[Fraction, Fraction]]:
    """求整条日志覆盖范围内温度严格越界（>bound 或 <bound）的区间并。

    返回相邻互不重叠的闭区间；取等时刻（测度零，不影响积分）作为端点。
    随后在给定 δ 下，把越界区间与平移后的在箱窗口求交即可积分，裁剪与
    平移共同保证不会外推到日志之外。
    """

    t_first = Fraction(log[0].time)
    t_last = Fraction(log[-1].time)
    breaks: set[Fraction] = {t_first, t_last}
    for p0, p1 in zip(log, log[1:]):
        t0, t1 = Fraction(p0.time), Fraction(p1.time)
        breaks.add(t0)
        breaks.add(t1)
        if p0.temp == p1.temp:
            # 恒温段：没有交点，整段状态一致，靠中点判定即可。
            continue
        # T(u) = bound 的交点 u* = t0 + (bound-T0)*(t1-t0)/(T1-T0)
        u_star = t0 + (bound - p0.temp) * (t1 - t0) / (p1.temp - p0.temp)
        if t0 < u_star < t1:
            breaks.add(u_star)

    ordered = sorted(breaks)
    intervals: list[tuple[Fraction, Fraction]] = []
    run_start: Fraction | None = None
    for left, right in zip(ordered, ordered[1:]):
        mid = (left + right) / 2
        temp = _temperature_at(log, mid)
        out = temp > bound if above else temp < bound
        if out:
            if run_start is None:
                run_start = left
        elif run_start is not None:
            intervals.append((run_start, left))
            run_start = None
    if run_start is not None:
        intervals.append((run_start, ordered[-1]))
    return intervals


def _temperature_at(log: Sequence[LogPoint], u: Fraction) -> Fraction:
    """日志时刻 u 的插值温度；调用方保证 u 落在日志覆盖范围内。"""

    if u == log[-1].time:
        return log[-1].temp
    for p0, p1 in zip(log, log[1:]):
        if p0.time <= u <= p1.time:
            return _segment_temp(p0, p1, u)
    raise ValueError("日志不覆盖该时刻，应被覆盖约束拦截")  # pragma: no cover


def _excursion_breakpoints(
    marked: Sequence[tuple[Fraction, Fraction]],
    w1: Fraction,
    w2: Fraction,
) -> list[Fraction]:
    """由越界区间 [a,b]（日志坐标）得到 f(δ) 的全部折点。

    在箱真实区间 [w1,w2] 对应日志坐标窗口 L(δ)=w1-δ、R(δ)=w2-δ。
    交集长度 ``|[L,R] ∩ [a,b]|`` 的斜率在窗口任一端点越过越界区间任一端点
    时改变，即 δ ∈ {w1-a, w1-b, w2-a, w2-b}。四个事件都纳入，才能保证
    折点之间 f 真正是线性的。
    """

    points: set[Fraction] = set()
    for a, b in marked:
        points.add(w1 - a)
        points.add(w1 - b)
        points.add(w2 - a)
        points.add(w2 - b)
    return sorted(points)


def _evaluate_excursion(
    marked: Sequence[tuple[Fraction, Fraction]],
    w1: Fraction,
    w2: Fraction,
    delta: Fraction,
) -> Fraction:
    """直接计算给定 δ 下的越界累计时长（积分指示函数）。

    在箱窗口的日志坐标为 [w1-δ, w2-δ]，与各越界区间求交后累加长度。
    """

    total = Fraction(0)
    window_left = w1 - delta
    window_right = w2 - delta
    for a, b in marked:
        left = max(a, window_left)
        right = min(b, window_right)
        if right > left:
            total += right - left
    return total


def _sublevel_intervals(
    breakpoints: Sequence[Fraction],
    values: Sequence[Fraction],
    limit: Fraction,
) -> list[tuple[Fraction, Fraction]]:
    """连续分段线性函数 f 的 {δ : f(δ) ≤ limit}，折点处函数值已知。"""

    # 可行域退化为单点（如覆盖约束只允许一个整数 δ）且该点达标。
    if len(breakpoints) == 1:
        if values[0] <= limit:
            return [(breakpoints[0], breakpoints[0])]
        return []

    feasible: list[tuple[Fraction, Fraction]] = []
    for i in range(len(breakpoints) - 1):
        x0, x1 = breakpoints[i], breakpoints[i + 1]
        f0, f1 = values[i], values[i + 1]
        if f0 <= limit and f1 <= limit:
            feasible.append((x0, x1))
        elif f0 <= limit < f1:
            # 0 = f0 + (f1-f0)*t/（x1-x0)，求 f=C 的交点
            root = x0 + (limit - f0) * (x1 - x0) / (f1 - f0)
            feasible.append((x0, root))
        elif f1 <= limit < f0:
            root = x0 + (limit - f0) * (x1 - x0) / (f1 - f0)
            feasible.append((root, x1))
        # 两端都超限：线性段内部严格更大，无解
    return merge_intervals(feasible)


def merge_intervals(
    intervals: Sequence[tuple[Fraction, Fraction]],
) -> list[tuple[Fraction, Fraction]]:
    """合并闭区间并；端点相接（含孤立点）即合并，孤立点 [x,x] 保留。"""

    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[tuple[Fraction, Fraction]] = [ordered[0]]
    for left, right in ordered[1:]:
        last_left, last_right = merged[-1]
        if left <= last_right:  # 闭区间：相接即可合并
            merged[-1] = (last_left, max(last_right, right))
        else:
            merged.append((left, right))
    return merged


def intersect_unions(
    left: Sequence[tuple[Fraction, Fraction]],
    right: Sequence[tuple[Fraction, Fraction]],
) -> list[tuple[Fraction, Fraction]]:
    """两个闭区间并的交集（双指针），保留孤立可行点。"""

    result: list[tuple[Fraction, Fraction]] = []
    i = j = 0
    while i < len(left) and j < len(right):
        lo = max(left[i][0], right[j][0])
        hi = min(left[i][1], right[j][1])
        if lo <= hi:
            result.append((lo, hi))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return merge_intervals(result)


def _feasible_for_sample(
    log: Sequence[LogPoint],
    sample: WaterSample,
    delta_lo: int,
    delta_hi: int,
) -> list[tuple[Fraction, Fraction]]:
    """单份水样在请求 δ 范围内的全部合格 δ 闭区间。"""

    w1, w2 = Fraction(sample.entry), Fraction(sample.exit)

    # 覆盖约束（真实时间 = 日志时间 + δ，在箱真实区间 [w1,w2]）：
    #   entry-δ ≥ t0  ⇔  δ ≤ w1 - t0
    #   exit-δ  ≤ tm  ⇔  δ ≥ w2 - tm
    # 即 δ ∈ [w2-tm, w1-t0]，日志必须覆盖全程且不外推。
    cover_lo = w2 - Fraction(log[-1].time)
    cover_hi = w1 - Fraction(log[0].time)
    domain_lo = max(Fraction(delta_lo), cover_lo)
    domain_hi = min(Fraction(delta_hi), cover_hi)
    if domain_lo > domain_hi:
        return []

    high_marked = _indicator_intervals(log, sample.upper, above=True)
    low_marked = _indicator_intervals(log, sample.lower, above=False)

    # 约束是高、低越界“累计总时长” ≤ 上限，而不是各自独立达标。因此把两个
    # 方向的折点合并后对总时长 f(δ)=f_high(δ)+f_low(δ) 求一次子水平集。
    raw_breaks = set(_excursion_breakpoints(high_marked, w1, w2))
    raw_breaks.update(_excursion_breakpoints(low_marked, w1, w2))
    deduped: list[Fraction] = []
    for x in sorted([domain_lo, *raw_breaks, domain_hi]):
        if not deduped or x != deduped[-1]:
            deduped.append(x)

    values = [
        _evaluate_excursion(high_marked, w1, w2, x)
        + _evaluate_excursion(low_marked, w1, w2, x)
        for x in deduped
    ]
    sublevel = _sublevel_intervals(deduped, values, sample.limit)
    return _clip_intervals(sublevel, domain_lo, domain_hi)


def _clip_intervals(
    intervals: Sequence[tuple[Fraction, Fraction]],
    lo: Fraction,
    hi: Fraction,
) -> list[tuple[Fraction, Fraction]]:
    """把闭区间并裁剪到 [lo, hi]。"""

    clipped: list[tuple[Fraction, Fraction]] = []
    for left, right in intervals:
        new_left = max(left, lo)
        new_right = min(right, hi)
        if new_left <= new_right:
            clipped.append((new_left, new_right))
    return merge_intervals(clipped)


def _verdict(
    intervals: Sequence[tuple[Fraction, Fraction]],
    delta_lo: int,
    delta_hi: int,
) -> str:
    """按输入时差整数闭区间判定 all / partial / none。"""

    if not intervals:
        return "none"
    lo = Fraction(delta_lo)
    hi = Fraction(delta_hi)
    if len(intervals) == 1 and intervals[0] == (lo, hi):
        return "all"
    return "partial"


def analyze_box(
    log: Sequence[LogPoint],
    samples: Sequence[WaterSample],
    delta_lo: int,
    delta_hi: int,
) -> BoxVerdict:
    """核查整箱：逐样求合格 δ 区间，并求全箱同时合格的交集。"""

    results: list[SampleResult] = []
    intersection: list[tuple[Fraction, Fraction]] | None = None
    for sample in samples:
        feasible = _feasible_for_sample(log, sample, delta_lo, delta_hi)
        verdict = _verdict(feasible, delta_lo, delta_hi)
        results.append(
            SampleResult(
                sample_id=sample.sample_id,
                verdict=verdict,
                feasible_intervals=tuple(feasible),
            )
        )
        intersection = (
            feasible
            if intersection is None
            else intersect_unions(intersection, feasible)
        )

    final_intersection = intersection or []
    return BoxVerdict(
        delta_lo=delta_lo,
        delta_hi=delta_hi,
        results=tuple(results),
        intersection=tuple(final_intersection),
        verdict=_verdict(final_intersection, delta_lo, delta_hi),
    )
