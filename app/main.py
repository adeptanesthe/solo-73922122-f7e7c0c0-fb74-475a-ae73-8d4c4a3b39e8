"""FastAPI 服务。

包含两组能力：

1. 既有：仪器检测记录与送检水样的编号核对（``/api/v1/check``）。
2. 新增：冷藏箱温控核查（``/api/v1/temperature-check``），在未知记录仪
   时钟误差 δ 下，给出每份水样与全箱同时合格的全部实数 δ 区间。
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, model_validator

from .matcher import InstrumentRow, SubmissionRow, analyze_matches
from .temperature import (
    BoxVerdict,
    analyze_box,
    format_fraction,
)
from .validation import RequestValidationError, validate_temperature_request

API_DESCRIPTION = """
环保实验室水样 API，提供两组核查能力。

## 1. 仪器记录核对（/api/v1/check）

将仪器检测记录按样品编号核对到送检水样：忽略大小写的 Levenshtein 距离
不超过 2 才建立候选边，先最大化配对数再最小化总距离，并分析所有最优
方案中的确定映射与候选。

## 2. 冷藏箱温控核查（/api/v1/temperature-check）

收样员用它判断记录仪时钟误差 δ（真实时间 = 日志时间 + δ，整数秒）是否
会改变水样的收样结论：

- 接收同箱 1~12 份水样与 2~100 个时间严格递增的温度点，段内线性插值，
  不外推；日志必须覆盖每份水样的整个在箱区间。
- 逐份返回输入 δ 整数闭区间内**全部**合格的实数 δ 闭区间（端点为最简
  分数，保留孤立可行点），并给出全箱同时合格的交集。
- 逐样标明该 δ 范围内 `all`（全部合格）、`partial`（部分合格）或
  `none`（均不合格），用于决定直接收样还是补查时钟。
- 全程有理数精确计算，温度等于允许区间端点不算越界。
- 非法输入以 `422` 拒绝并通过 JSON Pointer 精确定位字段。
"""

app = FastAPI(
    title="环保实验室水样 API",
    version="1.1.0",
    description=API_DESCRIPTION,
    contact={"name": "环保实验室"},
    docs_url="/docs",
    redoc_url="/redoc",
)


# ---------------------------------------------------------------------------
# 温控核查：响应模型与异常处理
# ---------------------------------------------------------------------------


class FeasibleInterval(BaseModel):
    """一个合格的 δ 闭区间；两端相等时表示孤立可行点。"""

    lower: str = Field(description="区间下端点（最简分数字符串）", examples=["-5/2"])
    upper: str = Field(description="区间上端点（最简分数字符串）", examples=["3"])


class TemperatureSampleResult(BaseModel):
    """单份水样的温控核查结论。"""

    sample_id: str = Field(description="水样 ID")
    verdict: Literal["all", "partial", "none"] = Field(
        description=(
            "输入 δ 范围内的合格情况：all=全部合格，partial=部分合格，"
            "none=均不合格"
        )
    )
    feasible_intervals: list[FeasibleInterval] = Field(
        description="全部合格 δ 闭区间（端点为最简分数）；空列表表示均不合格"
    )


class TemperatureCheckResponse(BaseModel):
    """温控核查响应。"""

    delta_lo: int = Field(description="请求给出的 δ 整数闭区间下界")
    delta_hi: int = Field(description="请求给出的 δ 整数闭区间上界")
    box_verdict: Literal["all", "partial", "none"] = Field(
        description="全箱交集相对输入 δ 范围的合格情况"
    )
    intersection: list[FeasibleInterval] = Field(
        description="全箱所有水样同时合格的 δ 区间交集；空列表表示无法同时合格"
    )
    results: list[TemperatureSampleResult] = Field(
        description="逐份水样的合格区间与结论"
    )


class ValidationErrorItem(BaseModel):
    location: str = Field(description="JSON Pointer 字段定位", examples=["/samples/0/entry"])
    message: str = Field(description="拒绝原因")


class ValidationErrorResponse(BaseModel):
    """非法输入的统一错误体。"""

    error: Literal["validation_error", "malformed_json"]
    detail: list[ValidationErrorItem]


@app.exception_handler(RequestValidationError)
async def _handle_request_validation_error(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "detail": [
                {"location": location, "message": message}
                for location, message in exc.errors
            ],
        },
    )


def _serialize_verdict(verdict: BoxVerdict) -> dict[str, Any]:
    return {
        "delta_lo": verdict.delta_lo,
        "delta_hi": verdict.delta_hi,
        "box_verdict": verdict.verdict,
        "intersection": [
            {"lower": format_fraction(lo), "upper": format_fraction(hi)}
            for lo, hi in verdict.intersection
        ],
        "results": [
            {
                "sample_id": result.sample_id,
                "verdict": result.verdict,
                "feasible_intervals": [
                    {"lower": format_fraction(lo), "upper": format_fraction(hi)}
                    for lo, hi in result.feasible_intervals
                ],
            }
            for result in verdict.results
        ],
    }


# ---------------------------------------------------------------------------
# 既有：仪器记录核对
# ---------------------------------------------------------------------------


class Submission(BaseModel):
    """一条送检清单记录。"""

    id: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9]+$",
        description="送检行独立 ID，仅限英文字母和数字，忽略大小写后不可重复",
        examples=["S01"],
    )
    sample_no: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9]+$",
        description="样品编号，仅限英文字母和数字；允许在不同送检行中重复",
        examples=["WQ1001"],
    )
    sampling_point: str = Field(
        min_length=1,
        description="采样点名称",
        examples=["东岸取水口"],
    )


class InstrumentRecord(BaseModel):
    """一条仪器检测记录。"""

    id: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9]+$",
        description="仪器记录独立 ID，仅限英文字母和数字，忽略大小写后不可重复",
        examples=["I01"],
    )
    sample_no: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9]+$",
        description="仪器上录入的样品编号，仅限英文字母和数字",
        examples=["WQ1002"],
    )
    value: Annotated[
        float,
        Field(allow_inf_nan=False, description="仪器检测值", examples=[7.42]),
    ]


class CheckRequest(BaseModel):
    """核对请求。两个清单分别最多 12 行。"""

    submissions: list[Submission] = Field(
        max_length=12,
        description="送检清单，最多 12 行",
    )
    instruments: list[InstrumentRecord] = Field(
        max_length=12,
        description="仪器记录，最多 12 行",
    )

    @model_validator(mode="after")
    def _ids_must_be_unique_case_insensitively(self) -> "CheckRequest":
        seen: dict[str, str] = {}
        for label, rows in (
            ("送检清单", self.submissions),
            ("仪器记录", self.instruments),
        ):
            for row in rows:
                key = row.id.casefold()
                if key in seen:
                    raise ValueError(
                        f"行 ID 重复（忽略大小写）：{seen[key]} 与 {row.id}"
                    )
                seen[key] = f"{label}/{row.id}"
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "submissions": [
                        {
                            "id": "S01",
                            "sample_no": "WQ1001",
                            "sampling_point": "东岸取水口",
                        },
                        {
                            "id": "S02",
                            "sample_no": "WQ1003",
                            "sampling_point": "西岸取水口",
                        },
                        {
                            "id": "S03",
                            "sample_no": "WQ5555",
                            "sampling_point": "南岸取水口",
                        },
                        {
                            "id": "S04",
                            "sample_no": "WQ1003",
                            "sampling_point": "西岸复采点",
                        },
                    ],
                    "instruments": [
                        {
                            "id": "I01",
                            "sample_no": "WQ1002",
                            "value": 7.42,
                        },
                        {
                            "id": "I02",
                            "sample_no": "WQ1004",
                            "value": 7.18,
                        },
                        {
                            "id": "I03",
                            "sample_no": "WQ1000",
                            "value": 7.51,
                        },
                        {
                            "id": "I04",
                            "sample_no": "WQ5555",
                            "value": 8.03,
                        },
                        {
                            "id": "I05",
                            "sample_no": "ZZZ999",
                            "value": 6.97,
                        },
                        {
                            "id": "I06",
                            "sample_no": "WQ1006",
                            "value": 7.66,
                        },
                    ],
                }
            ]
        }
    }


class MappingCandidate(BaseModel):
    """一个可能的最优归档去向。"""

    submission_id: str = Field(description="送检行 ID")
    sample_no: str = Field(description="送检样品编号")
    sampling_point: str = Field(description="采样点")
    distance: int = Field(description="仪器编号与样品编号之间的 Levenshtein 距离")


class InstrumentResolution(BaseModel):
    """一条仪器记录的核对结论。"""

    instrument_id: str = Field(description="仪器记录 ID")
    recorded_sample_no: str = Field(description="仪器记录的原始样品编号")
    value: float = Field(description="仪器检测值")
    status: Literal["confirmed", "review"] = Field(
        description=(
            "confirmed：所有最优方案中均对应同一条送检记录，可自动归档；"
            "review：存在歧义，需人工核对"
        ),
        examples=["confirmed", "review"],
    )
    confirmed_mapping: MappingCandidate | None = Field(
        default=None,
        description="确定归档映射；仅 status=confirmed 时非空",
    )
    candidate_mappings: list[MappingCandidate] = Field(
        description=(
            "该仪器记录在所有最优方案中出现过的候选送检行；"
            "status=confirmed 时仅有一个元素"
        )
    )
    possible_unmatched: bool = Field(
        description=(
            "是否存在某个最优方案使该记录没有匹配。" "为 true 时，不能直接归档"
        )
    )


class CheckSummary(BaseModel):
    submission_count: int = Field(description="送检清单行数")
    instrument_count: int = Field(description="仪器记录行数")
    max_matched_pairs: int = Field(description="最优方案中的配对数")
    min_total_distance: int = Field(description="最优方案的总编辑距离")
    confirmed_count: int = Field(description="可直接归档的仪器记录数")
    review_count: int = Field(description="需人工核对的仪器记录数")
    possible_unmatched_count: int = Field(description="可能未匹配的仪器记录数")


class CheckResponse(BaseModel):
    summary: CheckSummary
    results: list[InstrumentResolution]


DEMO_PATH = Path(__file__).resolve().parent.parent / "demo" / "check_request.json"
TEMPERATURE_DEMO_PATH = (
    Path(__file__).resolve().parent.parent / "demo" / "temperature_request.json"
)


@lru_cache(maxsize=1)
def _demo_request() -> CheckRequest:
    try:
        payload = json.loads(DEMO_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover
        raise RuntimeError(f"无法读取演示数据 {DEMO_PATH}: {exc}") from exc
    return CheckRequest.model_validate(payload)


@lru_cache(maxsize=1)
def _temperature_demo_payload() -> dict[str, Any]:
    try:
        return json.loads(TEMPERATURE_DEMO_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover
        raise RuntimeError(
            f"无法读取演示数据 {TEMPERATURE_DEMO_PATH}: {exc}"
        ) from exc


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """把浏览器访问根路径的请求转到交互式文档。"""

    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 温控核查路由
# ---------------------------------------------------------------------------


_TEMPERATURE_TAGS = ["温控核查"]


def _reject_constant(value: str) -> None:
    """json.loads 的 parse_constant 钩子：拒绝 NaN/Infinity。"""

    raise RequestValidationError(
        [("", f"JSON 不允许使用 {value}（仅接受有限数值与字符串）")]
    )


@app.post(
    "/api/v1/temperature-check",
    response_model=TemperatureCheckResponse,
    tags=_TEMPERATURE_TAGS,
    summary="核查冷藏箱时钟误差下的水样温控合格 δ 区间",
    response_description="逐样合格 δ 区间、全箱交集与收样结论",
    responses={422: {"model": ValidationErrorResponse, "description": "非法输入"}},
)
async def temperature_check(request: Request) -> dict[str, Any]:
    """在未知整数时差 δ 下求全部合格区间（精确分数）。"""

    raw = await request.body()
    if not raw:
        raise RequestValidationError([("", "请求体不能为空")])
    try:
        payload = json.loads(raw, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "malformed_json",
                "detail": [
                    {
                        "location": "",
                        "message": f"请求体不是合法 JSON（第 {exc.lineno} 行"
                        f"第 {exc.colno} 列）：{exc.msg}",
                    }
                ],
            },
        )
    delta_lo, delta_hi, log, samples = validate_temperature_request(payload)
    verdict = analyze_box(log=log, samples=samples, delta_lo=delta_lo, delta_hi=delta_hi)
    return _serialize_verdict(verdict)


@app.get(
    "/api/v1/temperature-demo",
    tags=_TEMPERATURE_TAGS,
    summary="获取温控核查的演示请求",
)
def temperature_demo_request() -> dict[str, Any]:
    """返回可直接 POST 到温控核查接口的演示请求体。"""

    return _temperature_demo_payload()


@app.get(
    "/api/v1/temperature-demo/expected",
    response_model=TemperatureCheckResponse,
    tags=_TEMPERATURE_TAGS,
    summary="温控演示数据的核查结果",
)
def temperature_demo_expected() -> dict[str, Any]:
    """直接运行内置温控演示数据并返回结果，方便验证服务是否正常。"""

    delta_lo, delta_hi, log, samples = validate_temperature_request(
        _temperature_demo_payload()
    )
    verdict = analyze_box(log=log, samples=samples, delta_lo=delta_lo, delta_hi=delta_hi)
    return _serialize_verdict(verdict)


# ---------------------------------------------------------------------------
# 既有路由
# ---------------------------------------------------------------------------


@app.get(
    "/api/v1/demo",
    response_model=CheckRequest,
    tags=["演示数据"],
    summary="获取可直接提交的演示请求",
)
def demo_request() -> CheckRequest:
    """返回演示请求体；把响应完整复制到 POST /api/v1/check 即可。"""

    return _demo_request()


@app.post(
    "/api/v1/check",
    response_model=CheckResponse,
    tags=["核对"],
    summary="核对仪器记录与送检水样",
    response_description="每条仪器记录的确定映射或候选水样",
)
def check(payload: CheckRequest) -> CheckResponse:
    submissions = [
        SubmissionRow(
            row_id=row.id,
            sample_no=row.sample_no,
            sampling_point=row.sampling_point,
        )
        for row in payload.submissions
    ]
    instruments = [
        InstrumentRow(
            row_id=row.id,
            sample_no=row.sample_no,
            value=row.value,
        )
        for row in payload.instruments
    ]

    analysis = analyze_matches(submissions, instruments)

    results: list[InstrumentResolution] = []
    for resolution in analysis.resolutions:
        candidates = [
            MappingCandidate(
                submission_id=candidate.submission.row_id,
                sample_no=candidate.submission.sample_no,
                sampling_point=candidate.submission.sampling_point,
                distance=candidate.distance,
            )
            for candidate in resolution.candidates
        ]
        confirmed_mapping = (
            candidates[0] if resolution.is_confirmed and len(candidates) == 1 else None
        )
        results.append(
            InstrumentResolution(
                instrument_id=resolution.instrument.row_id,
                recorded_sample_no=resolution.instrument.sample_no,
                value=resolution.instrument.value,
                status="confirmed" if confirmed_mapping else "review",
                confirmed_mapping=confirmed_mapping,
                candidate_mappings=candidates,
                possible_unmatched=resolution.possible_unmatched,
            )
        )

    return CheckResponse(
        summary=CheckSummary(
            submission_count=len(submissions),
            instrument_count=len(instruments),
            max_matched_pairs=analysis.max_pairs,
            min_total_distance=analysis.min_total_distance,
            confirmed_count=sum(1 for row in results if row.status == "confirmed"),
            review_count=sum(1 for row in results if row.status == "review"),
            possible_unmatched_count=sum(
                1 for row in results if row.possible_unmatched
            ),
        ),
        results=results,
    )


@app.get("/api/v1/demo/expected", tags=["演示数据"], summary="演示数据的核对结果")
def demo_expected() -> CheckResponse:
    """直接运行内置演示数据并返回结果，方便验证服务是否正常。"""

    return check(_demo_request())
