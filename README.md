# 环保实验室水样 API

服务基于 FastAPI，提供两组面向收样环节的核查能力：

1. **仪器记录核对**：把仪器上录错的样品编号核对回送检水样。
2. **冷藏箱温控核查**：在记录仪时钟存在未知误差 δ 时，判断每份水样的
   收样结论是否会被时钟误差改变。

## 快速启动

需要 Docker 与 Docker Compose 插件：

```bash
docker compose up --build
```

启动后：

| 地址 | 用途 |
| --- | --- |
| <http://localhost:8000/docs> | Swagger UI 交互式 API 文档 |
| <http://localhost:8000/redoc> | ReDoc 文档 |
| <http://localhost:8000/health> | 健康检查 |
| <http://localhost:8000/api/v1/temperature-demo> | 温控核查演示请求 |
| <http://localhost:8000/api/v1/temperature-demo/expected> | 温控演示数据的核查结果 |
| <http://localhost:8000/api/v1/demo> | 编号核对演示请求 |
| <http://localhost:8000/api/v1/demo/expected> | 编号核对演示结果 |

端口可通过环境变量调整，例如 `API_PORT=9000 docker compose up --build`。

---

# 一、冷藏箱温控核查

收样员据此判断记录仪时钟误差会不会改变水样的收样结论。

## 时间与温度约定

- 记录仪日志按自身时钟记录 `(time, temp)`，时间为 UTC 整数秒且严格递增，
  相邻温度点之间**线性插值**，不外推。
- 全箱共用一个未知整数秒时差 δ，满足 **真实时间 = 日志时间 + δ**。
- 每份水样给出真实的入箱 `entry`、出箱 `exit`（UTC 整数秒，入箱严格早于
  出箱）。换算到日志坐标，在箱窗口为 `[entry-δ, exit-δ]`。
- 日志必须覆盖每份水样的整个在箱窗口，否则该 δ 直接判为不可行。
- 温度等于允许区间端点**不算越界**；只有严格低于下限或严格高于上限才计时。
- 越界累计时长是越界指示函数在整个在箱窗口上的积分，须不超过给定的
  非负上限（秒）。

## 请求格式

```json
{
  "delta_lo": -20,
  "delta_hi": 40,
  "log": [
    {"time": 0, "temp": "6"},
    {"time": 20, "temp": "7"},
    {"time": 40, "temp": "8"},
    {"time": 60, "temp": "9"}
  ],
  "samples": [
    {
      "id": "W-001",
      "entry": 40,
      "exit": 120,
      "lower_temp": "2",
      "upper_temp": "8",
      "max_excursion_seconds": "75.5"
    }
  ]
}
```

- `delta_lo`、`delta_hi`：请求给出的 δ 整数闭区间，下界 ≤ 上界。
- `log`：2～100 个温度点，`time` 严格递增；`temp` 为十进制**字符串**。
- `samples`：同箱 1～12 份水样；`id` 非空且唯一。
- `lower_temp`、`upper_temp`、`max_excursion_seconds` 均为十进制字符串，
  要求下限 ≤ 上限、越界上限非负。十进制不接受科学计数法。
- 任何非法输入返回 `422`，并在 `detail[].location` 中以 JSON Pointer
  精确定位字段（如 `/samples/0/entry`）；请求体不是合法 JSON 时返回 `400`。

## 响应格式

```json
{
  "delta_lo": -20,
  "delta_hi": 40,
  "box_verdict": "partial",
  "intersection": [{"lower": "40", "upper": "40"}],
  "results": [
    {
      "sample_id": "W-001",
      "verdict": "partial",
      "feasible_intervals": [
        {"lower": "-20", "upper": "-9/2"},
        {"lower": "9/2", "upper": "40"}
      ]
    }
  ]
}
```

- `feasible_intervals`：该水样在请求 δ 范围内**全部**合格的实数 δ 闭区间，
  端点是最简分数字符串；`lower == upper` 表示孤立可行点并会被保留。
- 逐样 `verdict`：
  - `all`：输入 δ 范围内全部整数/实数 δ 都合格；
  - `partial`：仅部分 δ 合格（含只剩孤立可行点的情况）；
  - `none`：范围内没有任何合格 δ（含日志始终无法覆盖全程）。
- `intersection`：全箱所有水样**同时合格**的 δ 区间交集；`box_verdict`
  按同一口径给出全箱结论。交集为空时返回 `[]` 且 `box_verdict=none`，
  提示需要补查时钟。

## 判定如何支撑收样决策

- 某水样 `all`：无论时钟在给定范围内如何偏差，温控结论都不变，可直接收样。
- 仅 `partial`：结论依赖真实 δ，应先补查/校准时钟再决定。
- `none` 或全箱交集为空：现有信息下无法保证合格，需补查时钟或复检。
- 交集收敛为孤立点（如 `[40,40]`）：只有唯一时差能让全箱同时合格，
  应重点核实记录仪是否正好存在该偏差。

## 精确算法

全程使用 `Fraction` 有理数运算，温度与时长不经过浮点，因此端点直接是
精确的最简分数，不会出现 `0.1+0.2` 类误差。对每份水样：

1. 在整条日志上求出温度严格高于上限、严格低于下限的越界区间并；
2. 越界累计时长 `f(δ)` 是 δ 的连续分段线性函数，其折点来自在箱窗口端点
   与各越界区间端点的相对滑动（每个越界区间贡献四个事件折点），并纳入
   覆盖域边界；
3. 在折点处精确求 `f(δ)`，对每个线性段解 `f(δ) ≤ 上限`，合并得到全部
   合格闭区间（保留孤立点）；
4. 全箱交集由各水样区间并做双指针求交得到。

测试包含一个独立的解析积分参考实现，在 60 组随机输入上与上述算法逐 δ
精确对照，并覆盖覆盖约束、端点取等、孤立可行点与分数边界。

运行演示：

```bash
curl -s -X POST http://localhost:8000/api/v1/temperature-check \
  -H 'Content-Type: application/json' \
  --data @demo/temperature_request.json
```

仓库还附带 `demo/temperature_request_no_intersection.json`，演示全箱
交集为空（含一份任何 δ 都不合格的水样）的情形。

---

# 二、仪器记录编号核对

把仪器上录错的样品编号核对到送检水样，避免检测值归到错误的采样点。

服务使用 FastAPI，两个清单各最多 12 行。除了给出一个最优匹配，它还会分析
**所有**最优方案：只有一条仪器记录在全部最优方案中都对应同一条送检记录时，
才返回可自动归档的确定映射；否则返回候选水样并提示是否需要人工核对。


## 请求格式

```json
{
  "submissions": [
    {"id": "S01", "sample_no": "WQ1001", "sampling_point": "东岸取水口"},
    {"id": "S02", "sample_no": "WQ1003", "sampling_point": "西岸取水口"},
    {"id": "S03", "sample_no": "WQ5555", "sampling_point": "南岸取水口"},
    {"id": "S04", "sample_no": "WQ1003", "sampling_point": "西岸复采点"}
  ],
  "instruments": [
    {"id": "I01", "sample_no": "WQ1002", "value": 7.42},
    {"id": "I02", "sample_no": "WQ1004", "value": 7.18},
    {"id": "I03", "sample_no": "WQ1000", "value": 7.51},
    {"id": "I04", "sample_no": "WQ5555", "value": 8.03},
    {"id": "I05", "sample_no": "ZZZ999", "value": 6.97},
    {"id": "I06", "sample_no": "WQ1006", "value": 7.66}
  ]
}
```

- 两个清单均不能超过 12 行，可以为空。
- `id`、`sample_no` 只允许英文字母和数字。
- `id` 是行标识，在两类记录中忽略大小写后都必须唯一。
- `sample_no` 允许重复，因为同一水样可能在多个采样点或容器中送检。
- `sampling_point` 不能为空，接受中文等任意文本。
- `value` 必须是有限数字（不允许 NaN/Infinity）。

请求中任何不符合上述约束的内容都会由 FastAPI 返回 `422`。

## 响应格式

```json
{
  "summary": {
    "submission_count": 4,
    "instrument_count": 6,
    "max_matched_pairs": 4,
    "min_total_distance": 3,
    "confirmed_count": 1,
    "review_count": 5,
    "possible_unmatched_count": 5
  },
  "results": [
    {
      "instrument_id": "I01",
      "recorded_sample_no": "WQ1002",
      "value": 7.42,
      "status": "review",
      "confirmed_mapping": null,
      "candidate_mappings": [
        {
          "submission_id": "S01",
          "sample_no": "WQ1001",
          "sampling_point": "东岸取水口",
          "distance": 1
        },
        {
          "submission_id": "S02",
          "sample_no": "WQ1003",
          "sampling_point": "西岸取水口",
          "distance": 1
        },
        {
          "submission_id": "S04",
          "sample_no": "WQ1003",
          "sampling_point": "西岸复采点",
          "distance": 1
        }
      ],
      "possible_unmatched": true
    },
    {
      "instrument_id": "I04",
      "recorded_sample_no": "WQ5555",
      "value": 8.03,
      "status": "confirmed",
      "confirmed_mapping": {
        "submission_id": "S03",
        "sample_no": "WQ5555",
        "sampling_point": "南岸取水口",
        "distance": 0
      },
      "candidate_mappings": [
        {
          "submission_id": "S03",
          "sample_no": "WQ5555",
          "sampling_point": "南岸取水口",
          "distance": 0
        }
      ],
      "possible_unmatched": false
    }
  ]
}
```

每条仪器记录的结论：

- `status=confirmed`：在全部最优方案中，该记录始终对应同一条送检记录。
  `confirmed_mapping` 可直接用于归档。
- `status=review`：存在多个候选送检记录，或存在“未匹配”的最优方案。
  `confirmed_mapping` 为 `null`，需人工核对。
- `candidate_mappings`：该记录在**所有最优方案中出现过**的全部候选送检行。
  即使只有一个候选，只要 `possible_unmatched=true`，也不能自动归档。
- `possible_unmatched`：是否有某个最优方案使该记录没有匹配。

候选与确定映射都以 `submission_id` 精确到送检行，而不是只到样品编号。
因此，同一个 `sample_no` 出现在不同采样点时，不会误把检测值归到另一个
采样点。

## 核对规则

1. **候选边**：样品编号忽略大小写后计算 Levenshtein 距离，距离不超过 2
   才允许配对。编号本身只含英文字母和数字。
2. **一对一**：每条送检记录和每条仪器记录最多使用一次。
3. **两级最优**：先最大化配对数，再在配对数相同的方案中最小化总距离。
   即使存在“只配一对、总距离更小”的方案，也会优先选择配对数更多的方案。
4. **稳定性判断**：对每条候选边，强制使用该边后重新求解；若仍能得到同一个
   全局最优（配对数与总距离都不变），该边就出现在某个最优方案中。
   对每条仪器记录，去掉它后若仍能达到同一个全局最优，则存在它未匹配的
   最优方案。

内部使用最小费用最大流求两级最优解；最大规模仅 12×12，因此可以逐条边做
强制约束验证，而不必枚举可能数量巨大的所有最优匹配。单次请求最多约 157 次
小规模流计算。

## 内置演示数据说明了什么

`demo/check_request.json` 与 `demo/expected_response.json` 中包含：

- `I04` 的 `WQ5555` 精确命中 `S03`，是唯一确定映射。
- `I01`、`I02`、`I03`、`I06` 都可对应 `S01`、`S02`、`S04`；这些行中
  `S02`、`S04` 的样品编号相同但采样点不同。
- 上述四条记录都可能在某些最优方案中未匹配，因此全部为 `review`。
- `I05` 的 `ZZZ999` 与任何编号距离都超过 2，没有候选，但仍可能未匹配。

演示数据的全局最优为 4 对、总距离 3。

## 本地开发与测试

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

测试：

```bash
pytest -q
```

测试包含随机小规模输入与独立穷举程序（编号核对）、独立解析积分参考实现
（温控核查）的对照，验证配对稳定性与温控合格区间的精确性。

## 目录

```text
app/main.py          FastAPI 路由、校验模型、响应组装（两组 API）
app/matcher.py       Levenshtein、最小费用流、最优方案稳定性分析
app/temperature.py   温控核查：有理数越界积分、分段线性子水平集、区间交集
app/validation.py    温控请求的严格校验与 JSON Pointer 错误定位
demo/                可直接提交的演示请求与预期响应
tests/               API 与算法测试
Dockerfile           API 镜像
docker-compose.yml   一键启动配置
```
