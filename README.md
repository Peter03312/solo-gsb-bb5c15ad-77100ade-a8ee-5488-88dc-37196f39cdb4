# 公开档案敏感片段遮盖 API

基于 Python + FastAPI 的无状态服务：提交一段原文和若干候选敏感片段，服务在
**互不重叠**的前提下选出最优片段组合，并用等长的 `█`（U+2588 FULL BLOCK）
遮盖原文。片段之间允许重叠（提交时无需预处理），相邻片段可同时入选。
另提供 `POST /redact/revise`：先按码点区间编辑修订原文，自动作废旧片段、
迁移保留片段，再完成选择与遮盖；以及 `POST /redact/budget`：在给定的
遮盖码点总预算内选择最优组合。

`POST /redact/publish` 可把脱敏结果作为**发布凭证**持久化：服务只保存
遮盖正文、入选区间与带随机盐的原文 SHA-256 摘要（**不保存原文**），
每条凭证带单调递增编号与哈希链值；凭证经 SQLite 持久化到挂载的
数据卷，重启不丢失。客户端凭发布时返回的链值，可随时通过
`GET /redact/publish/{id}/verify` 重放哈希链，核验凭证是否被篡改。

## 一条命令启动

需要 Docker（含 Compose 插件，Docker Desktop 自带），**无需任何外部账号或密钥**：

```bash
docker compose up --build
```

服务地址与端口：

- API 地址：`http://localhost:8000`
- 健康检查：`GET http://localhost:8000/health`
- 交互文档（Swagger UI）：`http://localhost:8000/docs`
- OpenAPI 描述：`http://localhost:8000/openapi.json`

后台运行可加 `-d`，停止使用 `docker compose down`。

发布凭证保存在 Docker 命名卷 `publish-data`（容器内 `/data/publish.db`），
执行 `docker compose down` 后仍保留；只有显式
`docker compose down -v` 才会删除。数据库路径可用环境变量
`REDACT_PUBLISH_DB` 覆盖（非容器化开发时默认为 `./data/publish.db`）。

## API

### `POST /redact`

请求体（`Content-Type: application/json`）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `text` | string | 待处理原文 |
| `fragments` | array | 候选片段列表，允许重叠 |
| `fragments[].id` | integer 或 string | 片段唯一 ID；一次请求内所有 ID 必须同为整数或同为字符串 |
| `fragments[].weight` | integer | 权重，**正整数**（`>= 1`） |
| `fragments[].start` | integer | 起始位置，**左闭**，按 Unicode 码点计数，从 0 开始 |
| `fragments[].end` | integer | 结束位置，**右开**；区间为 `[start, end)` |

返回体：

| 字段 | 说明 |
| --- | --- |
| `selected_ids` | 入选片段 ID，按升序排列 |
| `rejected_ids` | 落选片段 ID，按升序排列 |
| `masked_text` | 入选片段覆盖的每个码点替换为一个 `█` 后的正文（长度与原文一致） |

#### 选择目标（按优先级）

1. **权重总和最大**；
2. 权重总和并列时，**遮盖字符数（区间长度之和）最多**；
3. 仍并列时，取**入选 ID 升序列表字典序最小**的组合
   （Python 序列字典序：逐位比较，前缀较短者更小）。

相邻区间（如 `[0,3)` 与 `[3,5)`）不重叠，可以同时入选。
算法为带权区间调度动态规划，时间复杂度 O(n log n)。

#### 示例

```bash
curl -s http://localhost:8000/redact \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "你好，世界！",
    "fragments": [
      {"id": 1, "weight": 5, "start": 0, "end": 3},
      {"id": 2, "weight": 3, "start": 0, "end": 2},
      {"id": 3, "weight": 1, "start": 2, "end": 5}
    ]
  }'
```

响应：

```json
{
  "selected_ids": [1],
  "rejected_ids": [2, 3],
  "masked_text": "███世界！"
}
```

片段 1 与片段 2、3 都重叠；片段 2+3 虽能相邻组合且遮盖字符更多，但权重总和
4 < 5，因此入选片段 1。

相邻片段同时入选的例子：

```bash
curl -s http://localhost:8000/redact \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "abcdef",
    "fragments": [
      {"id": 1, "weight": 1, "start": 0, "end": 3},
      {"id": 2, "weight": 1, "start": 3, "end": 6}
    ]
  }'
# {"selected_ids":[1,2],"rejected_ids":[],"masked_text":"██████"}
```

#### 校验规则（不合法请求返回 HTTP 400）

以下情况一律拒绝，响应形如 `{"detail": "中文错误说明"}`：

- 缺少 `text` / `fragments`，或片段缺少 `id` / `weight` / `start` / `end`；
- 类型错误：如权重或位置不是整数（不接受 `1.0`、`true`、`"1"`），ID
  不是整数或字符串；
- 权重不是正整数（`<= 0`）；
- 区间越界（`start < 0` 或 `end > 原文码点长度`）；
- 空片段或反向区间（`start >= end`）；
- 重复 ID；
- 整数 ID 与字符串 ID 混用；
- 请求体不是合法 JSON。

### `POST /redact/budget`

与 `POST /redact` 使用相同的 `text`、`fragments` 校验与等长 `█` 遮盖规则，
另接收非负整数 `max_masked_chars`（遮盖码点总预算）。只在**互不重叠且
遮盖码点总数不超过预算**的组合中，按与 `POST /redact` 相同的优先级
（权重总和 → 遮盖码点数 → 入选 ID 升序列表字典序）选出结果，相邻区间
仍可同时入选。返回结果额外包含实际遮盖码点数 `used_chars`。

请求体（`Content-Type: application/json`）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `text` | string | 待处理原文（同 `POST /redact`） |
| `fragments` | array | 候选片段列表，允许重叠（同 `POST /redact`） |
| `max_masked_chars` | integer | 遮盖码点总预算，**非负整数**；为 `0` 时不遮盖任何码点，直接返回原文 |

返回体：

| 字段 | 说明 |
| --- | --- |
| `selected_ids` | 入选片段 ID，按升序排列 |
| `rejected_ids` | 落选片段 ID，按升序排列 |
| `masked_text` | 入选片段覆盖的每个码点替换为一个 `█` 后的正文（长度与原文一致） |
| `used_chars` | 入选片段实际遮盖的码点总数（区间长度之和，`<= max_masked_chars`） |

#### 示例

```bash
curl -s http://localhost:8000/redact/budget \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "abcdef",
    "fragments": [
      {"id": 1, "weight": 5, "start": 0, "end": 4},
      {"id": 2, "weight": 3, "start": 0, "end": 2}
    ],
    "max_masked_chars": 3
  }'
```

响应：

```json
{
  "selected_ids": [2],
  "rejected_ids": [1],
  "masked_text": "██cdef",
  "used_chars": 2
}
```

片段 1（`[0,4)`）需遮盖 4 个码点，超出预算 3，即使权重更高也不能入选；
片段 2（`[0,2)`）只需 2 个码点，在预算内取得最高权重总和。

预算为零时返回未遮盖原文：

```bash
curl -s http://localhost:8000/redact/budget \
  -H 'Content-Type: application/json' \
  -d '{"text": "你好，世界！", "fragments": [
    {"id": 1, "weight": 5, "start": 0, "end": 3}
  ], "max_masked_chars": 0}'
# {"selected_ids":[],"rejected_ids":[1],"masked_text":"你好，世界！","used_chars":0}
```

#### 校验规则（不合法请求返回 HTTP 400）

- `max_masked_chars` 缺失，或不是非负整数
  （不接受 `-1`、`1.0`、`true`、`"3"`、`null`；注意布尔值是整数子类也被拒绝）；
- `text` / `fragments` 的校验规则与 `POST /redact` 完全一致；
- 请求体不是合法 JSON。

### `POST /redact/revise`

先把一批编辑一次性应用到原文，再对**未被编辑波及**的片段做最优选择与遮盖。
片段与编辑区间均按**修订前原文**的 Unicode 码点定位。

请求体（`Content-Type: application/json`）：`text` 与 `fragments` 字段同
`POST /redact`，另需 `edits` 数组：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `edits` | array | 编辑列表，按原文码点定位，一次性应用 |
| `edits[].start` | integer | 编辑起始位置，**左闭**，`>= 0` |
| `edits[].end` | integer | 编辑结束位置，**右开**；`start == end` 表示纯插入 |
| `edits[].replacement` | string | 替换文本；空字符串表示删除 `[start, end)` |

返回体：

| 字段 | 说明 |
| --- | --- |
| `revised_text` | 应用全部编辑后的原文 |
| `invalidated_ids` | 被编辑波及而作废的片段 ID，按升序排列 |
| `selected_ids` | 保留片段中入选的 ID，按升序排列 |
| `rejected_ids` | 保留片段中落选的 ID，按升序排列（**不含**作废 ID） |
| `masked_text` | 修订后正文中入选片段以等长 `█` 遮盖的结果（长度与 `revised_text` 一致） |

#### 片段作废与迁移规则

对每个旧片段：

- 删除/替换区间与片段**有交集**，或插入点**严格落在片段内部**
  （`start < p < end`）：片段**作废**，进入 `invalidated_ids`；
- 插入在片段**起点**：整段右移，插入内容不并入片段；
- 插入在片段**终点**：不扩展片段，区间不变；
- 其余保留片段长度不变，按前方编辑的**累计码点位移**整体平移区间，
  再参与与 `POST /redact` 相同的最优选择与遮盖。

#### 编辑校验规则（不合法请求返回 HTTP 400）

- 缺少 `edits`，或编辑缺少 `start` / `end` / `replacement`；
- 类型错误：位置不是整数（不接受 `1.0`、`true`），`replacement` 不是字符串；
- 区间越界（`start < 0` 或 `end > 原文码点长度`）或逆序（`start > end`）；
- 编辑区间彼此重叠（相邻，即前一区间 `end ==` 后一区间 `start`，不算重叠）；
- 同一位置的多次插入；
- `text` / `fragments` 的校验规则与 `POST /redact` 一致。

#### 示例

```bash
curl -s http://localhost:8000/redact/revise \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "你好，世界！",
    "fragments": [
      {"id": 1, "weight": 5, "start": 0, "end": 2},
      {"id": 2, "weight": 3, "start": 3, "end": 5}
    ],
    "edits": [
      {"start": 2, "end": 3, "replacement": "！"},
      {"start": 6, "end": 6, "replacement": "再见"}
    ]
  }'
```

响应：

```json
{
  "revised_text": "你好！世界！再见",
  "invalidated_ids": [],
  "selected_ids": [1, 2],
  "rejected_ids": [],
  "masked_text": "██！██！再见"
}
```

编辑与两个片段均只是相邻而非相交，插入在文末，因此没有片段作废，两个片段
区间不变、同时入选。

删除与片段相交、其余片段随位移迁移的例子：

```bash
curl -s http://localhost:8000/redact/revise \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "abcdef",
    "fragments": [
      {"id": 1, "weight": 1, "start": 1, "end": 3},
      {"id": 2, "weight": 1, "start": 4, "end": 6}
    ],
    "edits": [
      {"start": 2, "end": 4, "replacement": ""}
    ]
  }'
```

响应：

```json
{
  "revised_text": "abef",
  "invalidated_ids": [1],
  "selected_ids": [2],
  "rejected_ids": [],
  "masked_text": "ab██"
}
```

删除区间 `[2,4)` 与片段 1（`[1,3)`）相交，片段 1 作废；片段 2（`[4,6)`）
在删除之后，整体左移 2 个码点至 `[2,4)`，遮盖修订后文本中的 `"ef"`。

### `POST /redact/publish`

按与 `POST /redact` **完全相同的 `text`、`fragments` 校验与选段规则**
生成遮盖结果，并把它作为一条脱敏发布凭证持久化（SQLite）。

**服务端不保存原文**，每条凭证只持久化：

- 遮盖正文 `masked_text`；
- 入选区间（每条的 `id` / `start` / `end`）；
- 带 16 字节随机盐的原文 SHA-256 摘要 `text_digest` 及盐 `salt`
  （仅持有原文的客户端可自行复算比对，服务端无法据此还原原文）。

每条凭证带：

- 单调递增编号 `id`（从 1 开始）；
- 链值 `chain_value`：以固定创世链值为起点，
  `chain_n = SHA256(chain_{n-1} ‖ 规范化凭证内容_n)`；
  规范化内容包含编号、遮盖正文、入选区间、盐与摘要。

编号分配与链值计算在同一个 SQLite `BEGIN IMMEDIATE` 写事务内完成，
并发发布请求会被写锁串行化，编号严格连续、链顺序严格一致。

请求体：与 `POST /redact` 相同（仅 `text` 与 `fragments`）。

返回体：

| 字段 | 说明 |
| --- | --- |
| `id` | 凭证编号（库内单调递增，从 1 开始） |
| `masked_text` | 遮盖正文（已持久化的公开版本） |
| `selected_ids` | 入选片段 ID，按升序排列 |
| `rejected_ids` | 落选片段 ID，按升序排列 |
| `text_digest` | 带随机盐的原文 SHA-256 摘要（64 位十六进制） |
| `salt` | 本次摘要的随机盐（32 位十六进制） |
| `chain_value` | **本凭证链值，客户端必须留存**（64 位十六进制） |
| `previous_chain_value` | 前一凭证链值（首条为固定创世链值） |

#### 示例

```bash
curl -s http://localhost:8000/redact/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "你好，世界！",
    "fragments": [
      {"id": 1, "weight": 5, "start": 0, "end": 3},
      {"id": 2, "weight": 3, "start": 0, "end": 2},
      {"id": 3, "weight": 1, "start": 2, "end": 5}
    ]
  }'
```

响应（链值与摘要每次不同）：

```json
{
  "id": 1,
  "masked_text": "███世界！",
  "selected_ids": [1],
  "rejected_ids": [2, 3],
  "text_digest": "9d2f…（64 位十六进制）",
  "salt": "1a7c…（32 位十六进制）",
  "chain_value": "e3b0…（64 位十六进制，请留存）",
  "previous_chain_value": "36f33a9f…（创世链值）"
}
```

#### 凭证留存方式

链值是日后核验的唯一凭据，服务端不会主动告知某条凭证“应当”对应哪个
链值之外的信息。请在发布方（客户端）至少留存：

1. **`id`（凭证编号）与 `chain_value`（链值）** —— 核验接口的两个必需参数；
2. 需要时留存 `salt` 与 `text_digest`，以便日后用原文自行复算摘要；
3. 原文请按自身策略妥善保管，服务端不落盘、无法找回。

可简单存入一行记录，例如：

```text
id=1  chain=e3b0c44298fc1c149afbf4c8996fb924…  salt=1a7c…  digest=9d2f…
```

### `GET /redact/publish/{id}/verify`

从固定创世链值出发，**重算截至编号 `id` 的全部凭证**，与库中链值逐一
比对，并与客户端留存的链值比较。查询参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `chain_value` | string | **必填**，发布时客户端留存的 64 位十六进制链值 |

返回体：

| 字段 | 说明 |
| --- | --- |
| `id` | 核验目标凭证编号 |
| `valid` | 链重算全部一致且与 `chain_value` 相符时为 `true` |
| `first_break_id` | **首个链值断裂的凭证编号**；无断裂为 `null` |
| `recomputed_chain_value` | 由库中记录重算得到的截至该凭证的链值 |
| `reason` | 中文核验结论 |

目标编号不存在返回 HTTP 404；`chain_value` 缺失或不是 64 位小写
十六进制 SHA-256 返回 HTTP 400。

#### 示例

```bash
curl -s 'http://localhost:8000/redact/publish/1/verify?chain_value=e3b0…'
```

链完整且与留存值一致：

```json
{
  "id": 1,
  "valid": true,
  "first_break_id": null,
  "recomputed_chain_value": "e3b0…",
  "reason": "核验通过：凭证 1 及其之前的记录完整，链值与客户端留存一致"
}
```

若第 2 条凭证（或其之前任何记录）被篡改，再核验到第 3 条：

```json
{
  "id": 3,
  "valid": false,
  "first_break_id": 2,
  "recomputed_chain_value": "…",
  "reason": "核验失败：库中记录与哈希链在凭证 2 处首次断裂，该凭证或更早记录可能被篡改"
}
```

若库内链完整但与客户端留存链值不符（例如留存值来自另一条链或已过期），
`first_break_id` 为 `null`、`valid` 为 `false`，`reason` 会说明
“与客户端留存的链值不符”。

## 码点计数说明

位置与遮盖均以 **Unicode 码点**为单位（即 Python 3 中 `str` 的索引与
`len()`），不是字节，也不是用户感知字符（grapheme cluster）。例如
`"a😀b"` 长度为 3，遮盖 emoji `[1,2)` 得到 `"a█b"`。

## 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

uvicorn app.main:app --reload --port 8000   # 启动开发服务器
pytest -q                                    # 运行测试
```

测试包含核心算法（选择、预算选择、遮盖、编辑应用、片段迁移）单元测试、
五个 API 端点的端到端测试（含发布凭证的哈希链重放、篡改检测、
30 路并发下编号连续性与链自洽性），以及各 300 组随机用例下
动态规划结果与 2^n 暴力枚举（含带预算版本）的对照校验。

非容器化开发时凭证库默认位于 `./data/publish.db`，可用
`REDACT_PUBLISH_DB` 环境变量指向其他路径。

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由、请求校验
│   ├── redaction.py     # 区间调度 DP 与遮盖算法（与框架解耦）
│   └── publish.py       # 发布凭证：盐摘要、哈希链、SQLite 事务存储
├── tests/               # pytest 测试
├── Dockerfile
├── docker-compose.yml   # 命名卷 publish-data → /data（凭证持久化）
├── requirements.txt
└── requirements-dev.txt
```
