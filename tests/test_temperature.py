"""温控核查精确算法的单元测试。

包含一个刻意独立的浮点参考实现（不使用被测的折点/区间代码），在大量
随机输入上对照合格判定，确保有理数算法与语义定义一致。
"""

from __future__ import annotations

import random

import pytest

from app.temperature import (
    LogPoint,
    WaterSample,
    analyze_box,
    format_fraction,
    intersect_unions,
    merge_intervals,
    parse_decimal,
)
from fractions import Fraction


# --------------------------------------------------------------------------
# 十进制解析与分数输出
# --------------------------------------------------------------------------


def test_parse_decimal_basic_and_signs():
    assert parse_decimal("8") == Fraction(8)
    assert parse_decimal("8.5") == Fraction(17, 2)
    assert parse_decimal("-3.25") == Fraction(-13, 4)
    assert parse_decimal("+0.00") == Fraction(0)
    assert parse_decimal(".5") == Fraction(1, 2)
    assert parse_decimal("7.") == Fraction(7)
    assert parse_decimal("0") == Fraction(0)
    # 精确：典型浮点无法精确表示的值
    assert parse_decimal("0.1") + parse_decimal("0.2") == parse_decimal("0.3")


@pytest.mark.parametrize("bad", ["", " ", ".", "1e3", "0x10", "1.2.3", "NaN", "inf", "abc"])
def test_parse_decimal_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_decimal(bad)


def test_format_fraction_lowest_terms():
    assert format_fraction(Fraction(4)) == "4"
    assert format_fraction(Fraction(-9, 2)) == "-9/2"
    assert format_fraction(Fraction(6, 4)) == "3/2"
    assert format_fraction(Fraction(0)) == "0"


# --------------------------------------------------------------------------
# 区间工具
# --------------------------------------------------------------------------


def test_merge_and_intersect_keep_singletons():
    merged = merge_intervals([(Fraction(1), Fraction(1)), (Fraction(1), Fraction(2))])
    assert merged == [(Fraction(1), Fraction(2))]
    # 相接的闭区间合并；分离的保留
    assert merge_intervals([(Fraction(0), Fraction(1)), (Fraction(1), Fraction(2))]) == [
        (Fraction(0), Fraction(2))
    ]
    assert merge_intervals([(Fraction(0), Fraction(1)), (Fraction(3), Fraction(4))]) == [
        (Fraction(0), Fraction(1)),
        (Fraction(3), Fraction(4)),
    ]
    # 交集保留孤立点
    assert intersect_unions(
        [(Fraction(0), Fraction(40))],
        [(Fraction(40), Fraction(40))],
    ) == [(Fraction(40), Fraction(40))]
    assert intersect_unions([(Fraction(0), Fraction(1))], [(Fraction(2), Fraction(3))]) == []


# --------------------------------------------------------------------------
# 手工可核对的小例子
# --------------------------------------------------------------------------


def _constant_ramp_log():
    # 0..10 秒温度从 2 线性升到 12；允许区间 [4,8]
    return [LogPoint(0, Fraction(2)), LogPoint(10, Fraction(12))]


def test_ramp_known_geometry_zero_tolerance():
    log = _constant_ramp_log()
    # 温度在 [4,8] 内的日志时长是 4 秒（u∈[2,6]）。取在箱窗口长度 4，
    # 零越界容忍要求日志坐标窗口 [entry-δ, exit-δ]=[-δ,4-δ] 完全落在 [2,6]：
    # -δ ≥ 2 → δ ≤ -2；4-δ ≤ 6 → δ ≥ -2，故唯一 δ = -2（孤立可行点）。
    sample = WaterSample("S", entry=0, exit=4, lower=Fraction(4), upper=Fraction(8), limit=Fraction(0))
    verdict = analyze_box(log, [sample], delta_lo=-100, delta_hi=100)
    result = verdict.results[0]
    assert result.feasible_intervals == ((Fraction(-2), Fraction(-2)),)
    assert result.verdict == "partial"


def test_ramp_tolerance_gives_fractional_endpoints():
    log = _constant_ramp_log()
    # 在箱窗口 [0,6]，覆盖域 δ∈[exit-10, entry]=[-4,0]。
    # 越界总时长在域内依次为 4,3,2,2,2 秒；取上限 2.5 秒时，仅在更负的
    # 一侧达标，合格边界 δ=-5/2 是非整数分数端点。
    sample = WaterSample(
        "S", entry=0, exit=6, lower=Fraction(4), upper=Fraction(8), limit=Fraction(5, 2)
    )
    verdict = analyze_box(log, [sample], delta_lo=-50, delta_hi=50)
    intervals = verdict.results[0].feasible_intervals
    endpoints = [p for pair in intervals for p in pair]
    assert any(p.denominator != 1 for p in endpoints)
    assert intervals == ((Fraction(-5, 2), Fraction(0)),)
    for a, b in intervals:
        assert Fraction(-4) <= a <= b <= Fraction(0)


def test_zero_tolerance_window_longer_than_safe_band_has_no_solution():
    log = _constant_ramp_log()
    # 安全段（温度 ∈ [4,8]）的日志时长只有 4 秒；在箱窗口长度为 8，
    # 任何 δ 下窗口都无法完全落入安全段，零容忍必然无解。
    sample = WaterSample(
        "S", entry=0, exit=8, lower=Fraction(4), upper=Fraction(8), limit=Fraction(0)
    )
    verdict = analyze_box(log, [sample], delta_lo=-50, delta_hi=50)
    assert verdict.results[0].feasible_intervals == ()
    assert verdict.results[0].verdict == "none"


def test_boundary_equality_is_not_an_excursion():
    # 恒温恰好等于上限：任何覆盖到的 δ 都不应计时。
    # 日志比在箱窗口更宽，使整个 δ 请求范围都满足覆盖约束，从而判定 all。
    log = [LogPoint(-20, Fraction(8)), LogPoint(20, Fraction(8))]
    sample = WaterSample(
        "S", entry=0, exit=10, lower=Fraction(2), upper=Fraction(8), limit=Fraction(0)
    )
    verdict = analyze_box(log, [sample], delta_lo=-5, delta_hi=5)
    assert verdict.results[0].verdict == "all"
    assert verdict.results[0].feasible_intervals == (
        (Fraction(-5), Fraction(5)),
    )


def test_coverage_constraint_excludes_non_covering_delta():
    log = [LogPoint(100, Fraction(5)), LogPoint(200, Fraction(5))]
    sample = WaterSample(
        "S", entry=0, exit=50, lower=Fraction(0), upper=Fraction(10), limit=Fraction(0)
    )
    # 覆盖要求 δ ∈ [exit-tm, entry-t0] = [50-200, 0-100] = [-150,-100]
    verdict = analyze_box(log, [sample], delta_lo=-200, delta_hi=0)
    iv = verdict.results[0].feasible_intervals
    assert iv == ((Fraction(-150), Fraction(-100)),)


def test_window_equal_to_full_log_span_collapses_to_single_delta():
    # 在箱窗口跨度恰等于日志跨度时，覆盖约束把 δ 压成单点 0。
    log = [LogPoint(0, Fraction(5)), LogPoint(10, Fraction(5))]
    sample = WaterSample("S", 0, 10, 0, 10, 0)
    verdict = analyze_box(log, [sample], delta_lo=-50, delta_hi=50)
    assert verdict.results[0].feasible_intervals == ((Fraction(0), Fraction(0)),)
    assert verdict.results[0].verdict == "partial"


def test_request_range_without_any_covering_delta_returns_none():
    log = [LogPoint(100, Fraction(5)), LogPoint(200, Fraction(5))]
    sample = WaterSample("S", 0, 50, 0, 10, 0)
    # 覆盖域是 [-150,-100]，请求范围 [0, 50] 与之不相交。
    verdict = analyze_box(log, [sample], delta_lo=0, delta_hi=50)
    assert verdict.results[0].feasible_intervals == ()
    assert verdict.results[0].verdict == "none"
    assert verdict.verdict == "none"


def test_verdict_classification_all_partial_none():
    # 日志比在箱窗口更宽，确保整个 δ 请求范围都满足覆盖约束。
    log = [LogPoint(-20, Fraction(5)), LogPoint(20, Fraction(5))]
    ok = WaterSample("ok", 0, 10, 0, 10, 0)
    bad = WaterSample("bad", 0, 10, 6, 10, 0)  # 恒温 5 低于下限 6，全程越界
    verdict = analyze_box(log, [ok, bad], delta_lo=-3, delta_hi=3)
    assert verdict.results[0].verdict == "all"
    assert verdict.results[1].verdict == "none"
    assert verdict.verdict == "none"
    assert verdict.intersection == ()


# --------------------------------------------------------------------------
# 独立精确参考实现 + 随机输入对照
#
# 参考实现不使用被测的折点/子水平集代码，而是把在箱窗口与每个日志段求交，
# 对每段用一次函数的解析交点精确积分越界指示函数，得到 f(δ) 的精确值。
# --------------------------------------------------------------------------


def _ref_segment_outside_length(p0, p1, low, high, seg_lo, seg_hi):
    """日志段 [p0,p1] 与窗口交集 [seg_lo,seg_hi] 内温度严格越界的精确时长。"""

    span = Fraction(p1.time - p0.time)

    def temp(u):
        return p0.temp + (p1.temp - p0.temp) * (u - p0.time) / span

    cuts = {seg_lo, seg_hi}
    for bound in (low, high):
        if p0.temp == p1.temp:
            continue
        u = p0.time + (bound - p0.temp) * span / (p1.temp - p0.temp)
        if seg_lo < u < seg_hi:
            cuts.add(u)
    ordered = sorted(cuts)
    total = Fraction(0)
    for a, b in zip(ordered, ordered[1:]):
        mid = temp((a + b) / 2)
        if mid < low or mid > high:
            total += b - a
    return total


def _ref_excursion_exact(log, sample, delta):
    """精确计算给定 δ 的越界总时长；日志不覆盖时返回 None。"""

    u0 = Fraction(sample.entry) - delta
    u1 = Fraction(sample.exit) - delta
    if u0 < log[0].time or u1 > log[-1].time:
        return None
    total = Fraction(0)
    for p0, p1 in zip(log, log[1:]):
        seg_lo = max(Fraction(p0.time), u0)
        seg_hi = min(Fraction(p1.time), u1)
        if seg_hi <= seg_lo:
            continue
        total += _ref_segment_outside_length(
            p0, p1, sample.lower, sample.upper, seg_lo, seg_hi
        )
    return total


def _in_unions(x, unions):
    return any(float(a) <= x <= float(b) for a, b in unions)


def test_random_inputs_match_reference():
    rng = random.Random(20261111)
    for case in range(60):
        n_points = rng.randint(2, 12)
        times = sorted(rng.sample(range(0, 400), n_points))
        log = [
            LogPoint(t, Fraction(rng.randint(-20, 40)) + Fraction(rng.randint(0, 9), 10))
            for t in times
        ]
        n_samples = rng.randint(1, 4)
        samples = []
        for i in range(n_samples):
            span = rng.randint(5, 120)
            entry = rng.randint(log[0].time, max(log[0].time, log[-1].time - 1))
            exit_ = min(entry + span, log[-1].time)
            if exit_ <= entry:
                entry, exit_ = log[0].time, log[-1].time
            low = Fraction(rng.randint(-15, 10))
            high = low + Fraction(rng.randint(1, 20))
            limit = Fraction(rng.randint(0, 60))
            samples.append(WaterSample(f"S{i}", entry, exit_, low, high, limit))

        d_lo, d_hi = rng.randint(-150, -50), rng.randint(50, 150)
        verdict = analyze_box(log, samples, d_lo, d_hi)

        # 整数 δ 网格 + 若干半整数点；精确参考，无需容差
        probes = [Fraction(d) for d in range(d_lo, d_hi + 1, 11)]
        probes += [
            Fraction(d_lo) + Fraction(k, 2)
            for k in range(0, (d_hi - d_lo) * 2, 37)
        ]
        for result, sample in zip(verdict.results, samples):
            for delta in probes:
                ref = _ref_excursion_exact(log, sample, delta)
                got = _in_unions(float(delta), result.feasible_intervals)
                if ref is None:
                    assert got is False, (
                        f"case {case} {sample.sample_id} δ={float(delta)} 覆盖外却判合格"
                    )
                    continue
                expect = ref <= sample.limit
                assert got == expect, (
                    f"case {case} {sample.sample_id} δ={float(delta)} "
                    f"ref={ref} limit={sample.limit} got={got}"
                )

        # 全箱交集（精确）
        for delta in probes:
            refs = [_ref_excursion_exact(log, s, delta) for s in samples]
            got = _in_unions(float(delta), verdict.intersection)
            if any(f is None for f in refs):
                assert got is False, f"box case {case} δ={float(delta)} 覆盖外却判合格"
                continue
            expect = all(f <= s.limit for f, s in zip(refs, samples))
            assert got == expect, (
                f"box mismatch case {case} δ={float(delta)} refs={refs}"
            )


def test_endpoints_satisfy_limit_and_interiors_are_feasible():
    log = [
        LogPoint(0, Fraction(3)),
        LogPoint(20, Fraction(9)),
        LogPoint(50, Fraction(9)),
        LogPoint(80, Fraction(2)),
    ]
    sample = WaterSample("S", 10, 70, Fraction(4), Fraction(8), Fraction(40))
    verdict = analyze_box(log, [sample], -80, 80)
    assert verdict.results[0].feasible_intervals  # 至少存在合格区间
    for a, b in verdict.results[0].feasible_intervals:
        fa = _ref_excursion_exact(log, sample, a)
        fb = _ref_excursion_exact(log, sample, b)
        fm = _ref_excursion_exact(log, sample, (a + b) / 2)
        assert fa is not None and fb is not None and fm is not None
        # 端点恰好达标（或贴覆盖边界），区间内部严格不超限
        assert fa <= sample.limit
        assert fb <= sample.limit
        assert fm <= sample.limit


def test_max_size_request_is_fast_and_consistent():
    # 上限规模：100 个温度点、12 份水样、宽 δ 范围。
    rng = random.Random(7)
    log = [LogPoint(t, Fraction(rng.randint(0, 100), 10)) for t in range(0, 1000, 10)]
    samples = [
        WaterSample(
            f"S{i:02d}",
            entry=100 + 5 * i,
            exit=800 - 5 * i,
            lower=Fraction(2),
            upper=Fraction(8),
            limit=Fraction(50 + i),
        )
        for i in range(12)
    ]
    import time

    start = time.perf_counter()
    verdict = analyze_box(log, samples, delta_lo=-200, delta_hi=200)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0  # 上限规模应在 2 秒内完成

    # 与精确参考在若干 δ 上对照，并校验区间端点排序正确
    for result, sample in zip(verdict.results, samples):
        for a, b in result.feasible_intervals:
            assert a <= b
            fa = _ref_excursion_exact(log, sample, a)
            fb = _ref_excursion_exact(log, sample, b)
            assert fa is not None and fb is not None
            assert fa <= sample.limit and fb <= sample.limit
        for delta in (Fraction(-200), Fraction(-100), Fraction(0), Fraction(100), Fraction(200)):
            ref = _ref_excursion_exact(log, sample, delta)
            got = _in_unions(float(delta), result.feasible_intervals)
            if ref is not None:
                assert got == (ref <= sample.limit)
