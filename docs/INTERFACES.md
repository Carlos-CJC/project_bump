# Bump 偶遇徽章 · 接口文档（MQTT + Web REST）

> 状态：v1 草案（原型阶段）
> 适用固件：偶遇 MVP（BLE 广播/扫描 + Wi-Fi 上报）
> 关联文档：[FEATURES.md](FEATURES.md)（固件行为）、`doc/plan_polished.md`
> （产品与数据模型）

本文定义徽章、MQTT broker、后端与 Web 端之间的**对外契约**。固件如何实现
（模块划分、库选择、上传时序）见 [FIRMWARE_MQTT_PLAN.md](FIRMWARE_MQTT_PLAN.md)。

---

## 0. 总体架构

```
Badge A/B ──WiFi / MQTT over TLS 8883──▶ Mosquitto broker
                                              │ subscribe（持久会话）
                                              ▼
                                         FastAPI 后端 ──SQLite/PostgreSQL
                                              │ REST（Bearer token）
                                              ▼
                                          Web 客户端
```

- 徽章只做上行：偶遇事件与状态。
- 后端订阅全部设备上行，负责身份映射、去重、互遇判定与入库。
- Web 只调 REST，不直连 broker。

设备端约束（影响协议设计）：

- NINA-W102 的 Wi-Fi 与 BLE 共用射频，**不能同时工作**；每次上报会短暂
  中断 BLE 偶遇检测（见固件方案）。
- 设备没有 RTC，重启后 `millis()` 从 0 开始；不上电持久化。
- 内存受限：载荷为手写紧凑 JSON，单条批次上限 8 个事件、2 KB。

---

## 1. 设备身份与连接

### 1.1 device_id

| 项 | 值 |
| --- | --- |
| 格式 | `bump-` + 12 位小写十六进制（Wi-Fi MAC 去分隔符） |
| 示例 | `bump-ec626080e83c` |
| 来源 | 固件启动时读取 `WiFi.macAddress()`（不联网也可读） |
| 稳定性 | 每块板唯一且重启不变，无需 Flash 持久化 |

> 原型阶段直接用 MAC 派生 ID，会暴露在主题、broker 日志中；正式版应改为
> 工厂预置的不透明 ID（见 §5）。

### 1.2 认证与 ACL

- 每台设备在 broker 上使用**独立账号**：
  - `username = device_id`
  - `password` = 每板随机密钥，固件编译时注入（不进仓库）
- broker 建议 ACL（Mosquitto `pattern`，`%u` 即 username）：

```conf
# 设备：只能写自己的上行主题，只能读自己的下行主题
pattern write bump/v1/device/%u/encounters
pattern write bump/v1/device/%u/status
pattern read  bump/v1/device/%u/config
pattern read  bump/v1/device/%u/ack

# 后端账号：读全部设备上行，写全部设备下行
user bump_backend
topic read  bump/v1/device/+/encounters
topic read  bump/v1/device/+/status
topic write bump/v1/device/+/config
topic write bump/v1/device/+/ack
```

- `allow_anonymous false`；设备与后端都必须通过用户名密码认证。

### 1.3 MQTT 连接参数

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| 协议 | MQTT 3.1.1 | |
| 传输 | TLS 1.2+，TCP 8883 | 禁止明文 1883（测试环境除外，见 §5） |
| `client_id` | `device_id` | 每设备唯一 |
| Clean Session | `true` | 设备只发布；离线队列由固件内存队列负责 |
| Keep Alive | 60 s | 上传窗口只持续数秒 |
| Socket timeout | 10 s | 覆盖 TLS 握手 + 等待 PUBACK |
| LWT | `.../status`，QoS 1，retain，`state:"offline"` | 掉电时由 broker 代发 |

- 设备**每次上传窗口建立一条短连接**，发完即断开；不维持长连接。
- 后端订阅者必须使用 `clean_session=false` + 固定 `client_id`
  （如 `bump-backend-1`），这样后端重启期间 QoS 1 消息由 broker 排队。

---

## 2. MQTT 主题与载荷

主题前缀固定：`bump/v1/device/{device_id}/`

### 2.1 主题总表

| 主题 | 方向 | QoS | Retain | 状态 |
| --- | --- | --- | --- | --- |
| `.../encounters` | 设备 → broker | 1 | 否 | MVP |
| `.../status` | 设备 → broker | 1 | 是 | MVP |
| `.../config` | 后端 → 设备 | 1 | 是 | 可选/后续 |
| `.../ack` | 后端 → 设备 | 1 | 否 | 后续 |

### 2.2 encounters：偶遇批量上报

一次发布携带 1–8 个事件。立即上报模式下通常为 1 条；队列补发时可能多条。
载荷始终是批形状，未来加入本地持久化时**无需改动线上格式**。

信封字段：

| 字段 | 类型 | 必填 | 约束 / 语义 |
| --- | --- | --- | --- |
| `schema` | int | 是 | 固定 `1`；未知值服务端拒收 |
| `batch_id` | string | 是 | `bt_<dev12>_<boot8>_<firstseq8>_<count>`，≤40 字符；重试复用 |
| `device_id` | string | 是 | `^bump-[0-9a-f]{12}$`，必须与主题一致 |
| `boot_id` | string | 是 | 每次上电随机 8 位 hex |
| `self_short_id` | string | 是 | 本机当前广播的 12 位短 ID，服务端据此建立映射 |
| `fw` | string | 是 | 固件版本，≤32 字符，如 `"0.4.0"` |
| `uptime_ms` | uint32 | 是 | 发布时的 `millis()` |
| `time_source` | string | 是 | `"uptime"` 或 `"ntp"` |
| `events` | array | 是 | 1–8 个事件对象 |

事件字段：

| 字段 | 类型 | 必填 | 约束 / 语义 |
| --- | --- | --- | --- |
| `event_id` | string | 是 | `ev_<dev12>_<boot8>_<seq8>`，≤36 字符；入队时生成，重试不变 |
| `peer_short_id` | string | 是 | `^[0-9a-f]{12}$` |
| `rssi_avg` | int | 是 | −127..0 dBm（强信号样本指数滑动平均） |
| `rssi_last` | int | 是 | −127..0 dBm |
| `duration_s` | uint32 | 是 | 0..86400 |
| `samples` | uint16 | 是 | 1..65535 |
| `flags` | uint8 | 是 | 0..255；bit0 = 允许加好友 |
| `peer_tx_power` | int8 | 否 | 对端广播的标称发射功率 |
| `occurred_uptime_ms` | uint32 | 是 | 偶遇判定成立时的 `millis()` |
| `occurred_at` | string | 否 | RFC3339 UTC；仅 `time_source="ntp"` 时出现 |

示例（一条队列补发的批次，含 2 个事件）：

```json
{
  "schema": 1,
  "batch_id": "bt_a1b2c3d4e5f6_9f3c1a02_00000007_0002",
  "device_id": "bump-a1b2c3d4e5f6",
  "boot_id": "9f3c1a02",
  "self_short_id": "3f9a01c47b2e",
  "fw": "0.4.0",
  "uptime_ms": 287431,
  "time_source": "uptime",
  "events": [
    {
      "event_id": "ev_a1b2c3d4e5f6_9f3c1a02_00000007",
      "peer_short_id": "8c21d0a9e4f5",
      "rssi_avg": -65,
      "rssi_last": -63,
      "duration_s": 7,
      "samples": 2,
      "flags": 1,
      "peer_tx_power": -4,
      "occurred_uptime_ms": 214003
    },
    {
      "event_id": "ev_a1b2c3d4e5f6_9f3c1a02_00000008",
      "peer_short_id": "0d4e88aa12c3",
      "rssi_avg": -58,
      "rssi_last": -56,
      "duration_s": 13,
      "samples": 4,
      "flags": 1,
      "peer_tx_power": -4,
      "occurred_uptime_ms": 286905
    }
  ]
}
```

### 2.3 status：设备状态

每次上传窗口开始时发布 `state:"online"`，正常结束前发布 `state:"offline"`；
LWT 使用相同结构（`offline`）。Retained，因此 Web/后端总能看到最后状态。

| 字段 | 类型 | 必填 | 约束 / 语义 |
| --- | --- | --- | --- |
| `schema` | int | 是 | `1` |
| `device_id` | string | 是 | 与主题一致 |
| `boot_id` | string | 是 | 8 位 hex |
| `state` | string | 是 | `online` / `offline` |
| `fw` | string | 是 | ≤32 字符 |
| `uptime_s` | uint32 | 是 | 开机秒数 |
| `queue_depth` | uint8 | 是 | 发布时待上传事件数 |
| `events_dropped` | uint32 | 是 | 本次开机累计因队列满丢弃的事件数 |
| `wifi_rssi` | int | 否 | dBm |
| `battery_mv` | int | 否 | 预留，暂未接线 |

示例：

```json
{"schema":1,"device_id":"bump-a1b2c3d4e5f6","boot_id":"9f3c1a02","state":"online","fw":"0.4.0","uptime_s":287,"queue_depth":2,"events_dropped":0,"wifi_rssi":-58}
```

> 设备大部分时间离线。后端应以 `last_message_at` + retained `state` 判断在线，
> 不要把 retained `online` 当作实时在线。

### 2.4 config：下行配置（可选/后续）

设备在 `BUMP_MQTT_CONFIG=1` 时于上传窗口内订阅并应用（仅存 RAM）。
Retained，后端在用户修改隐私设置时发布。

| 字段 | 类型 | 必填 | 约束 / 语义 |
| --- | --- | --- | --- |
| `schema` | int | 是 | `1` |
| `revision` | uint32 | 是 | 单调递增；设备忽略不大于已应用值的消息 |
| `broadcast_enabled` | bool | 否 | false = 隐身：停止广播、继续扫描 |
| `joinable` | bool | 否 | 映射到广播 flags bit0 |
| `adv_ms` | uint16 | 否 | 2000..30000 |
| `scan_ms` | uint16 | 否 | 2000..30000 |
| `tx_power` | int8 | 否 | 广播中的标称发射功率 |

### 2.5 ack：应用层确认（后续）

MVP 不实现。QoS 1 的 PUBACK 只表示 broker 已收到，不表示后端已入库；
若后续需要端到端确认，后端在入库后发布：

```json
{"schema":1,"batch_id":"bt_...","accepted":true,"duplicate":false,"error":null}
```

---

## 3. 服务端处理约定

### 3.1 时间语义

- 服务端为每条消息记录权威的 `received_at`（UTC）。
- 设备无 NTP 时（`time_source="uptime"`）：
  ```
  occurred_at_est = received_at − (batch.uptime_ms − event.occurred_uptime_ms)
  ```
  结果限制在 `[received_at − 24h, received_at + 5min]`。
- 有 NTP 时（`time_source="ntp"` 且事件带 `occurred_at`）直接采用，但仍保存
  `received_at`；比服务器时间超前 10 分钟以上的事件拒绝入库。
- 上报是立即触发的，`received_at` 与事件时刻通常只差数十秒，反推足够准确。

### 3.2 幂等与去重

- `event_id` 是幂等键：`INSERT ... ON CONFLICT(event_id) DO NOTHING`。
- QoS 1 为至少一次投递，重复消息是正常现象。
- `batch_id` 用于整批短路（记录 `mqtt_batches`），但事件级去重才是权威。
- 固件重试时 `event_id` / `batch_id` 保持不变。

### 3.3 short_id 映射

- 每个入站批次都会 upsert：
  `short_id_registry(short_id=self_short_id, boot_id, device_id, first_seen_at, last_seen_at)`。
- `peer_short_id` 解析：在该短 ID 最后出现时间 ±24h 内匹配注册表；
  解析不到则 `device_b_id = NULL`，并标记 `resolution="pending"`。
- 后到批次补注册短 ID 时，对新注册记录 ±24h 内的 pending 偶遇做回填。

### 3.4 互遇判定

- 事件表示「观察者 A 在时刻 t 看到短 ID S_B」。
- 若存在反向事件「观察者 B 在时刻 t' 看到 A 当时的短 ID」，且
  `|t' − t| ≤ 900 s`，则两条记录都置 `mutual = true`。
- 时间线按**无序设备对**聚合：同一对设备 8 小时内的事件合并为一条展示项
  （对应固件冷却期语义；由于短 ID 每次重启变化，服务端聚合才是真正的去重）。
- 单向事件照常展示，但对方资料不可见（见 §4.5）。

### 3.5 失败与重试（设备侧行为约定）

| 情况 | 行为 |
| --- | --- |
| 收到 PUBACK | 从内存队列移除该批 |
| 10s 内无 PUBACK / socket 错误 | 批次保留，结束本次上传窗口 |
| MQTT CONNACK 失败 | 记录原因码，结束窗口，按退避重试 |
| 重复投递 | 服务端按 `event_id` 去重 |
| 队列满 | 丢弃最旧事件并累计 `events_dropped`（status 中上报） |

退避：`10s × 2^(n−1)`，上限 5 分钟，附加 0–5s 随机抖动；成功后清零。
内存队列重启即丢，属当前原型已知限制。

---

## 4. Web REST API

### 4.1 通用约定

| 项 | 约定 |
| --- | --- |
| Base path | `/api/v1` |
| 编码 | JSON，UTF-8 |
| 鉴权 | `Authorization: Bearer <token>`；`/health` 免鉴权 |
| Token | 原型为静态用户 token；服务端存 SHA-256，常量时间比较 |
| 时间 | RFC3339 UTC，如 `2026-11-18T11:32:10Z` |
| ID | 字符串 |
| 成功单条 | `{"data": {...}}` |
| 成功列表 | `{"items": [...], "page": {"next_cursor": "...", "has_more": false}}` |
| 分页 | `limit` 1–100（默认 20）+ 不透明游标 `cursor` |

错误响应（所有非 2xx）：

```json
{"error":{"code":"invalid_token","message":"Missing or invalid bearer token","request_id":"req_01J..."}}
```

状态码与错误码：

| HTTP | code | 说明 |
| --- | --- | --- |
| 400 | `invalid_request` | 参数格式错误 |
| 401 | `invalid_token` | 缺少/无效/已撤销 token |
| 403 | `forbidden` / `not_mutual` | 无权访问；非互遇不允许加好友 |
| 404 | `not_found` | 资源不存在或不属于当前用户 |
| 409 | `conflict` / `device_already_bound` / `claim_code_invalid` | 状态冲突 |
| 422 | `validation_error` | 字段校验失败 |
| 429 | `rate_limited` | 请求过频 |
| 500 | `internal` | 服务端错误 |

### 4.2 数据模型

与 `doc/plan_polished.md` §3.5 对齐，补充短 ID 映射与幂等所需字段：

| 表 | 主要字段 |
| --- | --- |
| `users` | `id`, `display_name`, `avatar_url`, `privacy_mode`(`visible`/`mutual_only`/`hidden`), `created_at` |
| `devices` | `id`(=device_id), `user_id`(nullable), `claim_code_hash`, `label`, `firmware`, `last_boot_id`, `last_seen_at`, `created_at`, `bound_at` |
| `short_id_registry` | `id`, `short_id`, `boot_id`, `device_id`, `first_seen_at`, `last_seen_at`, `UNIQUE(short_id, boot_id)` |
| `mqtt_batches` | `batch_id`(PK), `device_id`, `received_at`, `event_count` |
| `encounters` | `id`, `event_id`(UNIQUE), `batch_id`, `device_a_id`(观察者), `device_b_id`(对端，可空), `peer_short_id`, `started_at`, `ended_at`, `duration_s`, `rssi_avg`, `rssi_last`, `rssi_min`, `rssi_max`, `samples`, `flags`, `mutual`, `received_at`, `created_at` |
| `friend_requests` | `id`, `from_user_id`, `to_user_id`, `method`(`nfc`/`manual`), `status`, `created_at`, `responded_at` |
| `friendships` | `id`, `user_a_id`, `user_b_id`, `created_at`, `UNIQUE(user_a_id,user_b_id)`，`CHECK(user_a_id < user_b_id)` |
| `api_tokens` | `id`, `user_id`, `token_hash`, `created_at`, `revoked_at` |

### 4.3 端点总表

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 存活与依赖状态（免鉴权） |
| GET | `/users/me` | 当前用户资料 |
| GET | `/encounters` | 偶遇时间线（分页 + 过滤） |
| GET | `/encounters/{id}` | 偶遇详情 |
| DELETE | `/encounters/{id}` | 删除一条自己的观察记录 |
| GET | `/devices` | 我的设备列表 |
| POST | `/devices/bind` | 绑定设备（claim code） |
| PATCH | `/devices/{device_id}` | 修改标签 |
| POST | `/devices/{device_id}/unbind` | 解绑 |
| GET | `/users/me/privacy` | 读取隐私设置 |
| PATCH | `/users/me/privacy` | 修改隐私设置（发布 config 下行） |
| GET | `/friends` | 好友列表 |
| DELETE | `/friends/{user_id}` | 删除好友 |
| GET | `/friend-requests` | 好友申请列表（方向/状态过滤） |
| POST | `/friend-requests` | 发起申请 |
| POST | `/friend-requests/{id}/accept` | 同意 |
| POST | `/friend-requests/{id}/decline` | 拒绝 |
| POST | `/friend-requests/{id}/cancel` | 撤回自己发出的申请 |

### 4.4 关键端点详情

#### GET /health

```json
{"status":"ok","time":"2026-11-18T12:00:00Z","db":"ok",
 "mqtt":{"connected":true,"last_message_at":"2026-11-18T11:59:58Z"},
 "version":"0.1.0"}
```

#### GET /encounters

查询参数：`limit`、`cursor`、`since`、`until`、`mutual`(`true`/`false`)、
`device_id`（必须属于当前用户，否则 403）。

```json
{
  "items": [{
    "id": "enc_123",
    "occurred_at": "2026-11-18T11:32:10Z",
    "started_at": "2026-11-18T11:27:10Z",
    "ended_at": "2026-11-18T11:32:10Z",
    "duration_s": 300,
    "rssi_avg": -61,
    "rssi_last": -59,
    "samples": 4,
    "mutual": true,
    "observer_device_id": "bump-a1b2c3d4e5f6",
    "peer": {
      "short_id": "8c21d0a9e4f5",
      "device_id": "bump-112233445566",
      "user_id": "usr_9f8e",
      "display_name": "Rin",
      "avatar_url": "https://.../a.png",
      "profile_visible": true
    }
  }],
  "page": {"next_cursor": "eyJ0cyI6...", "has_more": false}
}
```

#### GET /encounters/{id}

在列表字段基础上增加 `event_id`、`batch_id`、`peer_device_id`、
`resolution`（`resolved`/`pending`）、`counterpart_encounter_id`。
不属于当前用户返回 404。

#### GET /devices

```json
{"items":[{"id":"bump-ec626080e83c","label":"Badge A","firmware":"0.4.0",
 "online":true,"last_seen_at":"2026-11-18T11:59:58Z","bound_at":"...","created_at":"..."}]}
```

`online` 判定：最近一次 retained `state="online"` 且 `last_seen_at` 在 10 分钟内。

#### POST /devices/bind

```json
{"device_id":"bump-ec626080e83c","claim_code":"123456","label":"Badge A"}
```

`201` 返回绑定结果；设备从未上报 → 404 `not_found`；已被绑定 →
409 `device_already_bound`；claim code 错误 → 409 `claim_code_invalid`。
claim code 由生产时预置并打印在启动日志中，与 MQTT 密码相互独立。

#### GET /users/me/privacy 与 PATCH

```json
{"data":{"privacy_mode":"mutual_only","discoverable":true,
 "broadcast_enabled":true,"history_retention_days":90,"hidden_until":null}}
```

PATCH 接受任意子集；`broadcast_enabled`/`discoverable` 变化会递增配置
revision，并向该用户所有设备发布 retained `.../config`。

#### 好友接口

- `POST /friend-requests`：`{"to_user_id":"usr_abcd","method":"manual"}`。
  非互遇且 `method!="nfc"` → 403 `not_mutual`；已有 pending 申请 → 409 `conflict`。
- `POST /friend-requests/{id}/accept` 仅 `to_user_id` 可调用，成功后创建
  `friendships` 记录；`decline`/`cancel` 返回 204。
- `GET /friends` 返回好友资料与最近一次偶遇时间。

### 4.5 时间线隐私规则

- `profile_visible = mutual && peer.privacy_mode != "hidden"`。
- 不可见时 `user_id`、`display_name`、`avatar_url` 为 `null`，
  `short_id` 保留（仅用于本人时间线内的关联）。
- 删除偶遇（`DELETE /encounters/{id}`）只删除自己的观察记录，不删除对方记录；
  双方均为本人的数据（互遇两侧）时按产品规则另行处理（当前版本保留对方记录）。

---

## 5. 安全、版本与隐私

### 5.1 TLS 要求

- NINA-W102 的 TLS 由模块固件终止；WiFiNINA 库**不提供** `setCACert`/
  `setInsecure`，校验使用模块内置根证书包。
- 因此 broker 证书必须链到公共根证书（推荐 Let's Encrypt / ISRG Root X1）。
  **自签或私有 CA 目前无法在设备侧通过校验**。
- 设备必须用证书中的 DNS 主机名连接，不能填 IP。
- 结论：云端 broker 使用 8883 + 公共 CA 证书；测试环境如需明文 1883，
  必须显式开启编译开关并在文档标记为不安全。

### 5.2 身份与隐私

- `device_id` 由 MAC 派生，会出现在 MQTT 主题、broker 日志与 API 中；
  原型阶段接受此折衷，后续改为工厂预置不透明 ID。
- 广播中只有随机 `short_id`，不含任何个人身份信息；服务端负责
  `short_id → device_id → user` 的映射。
- 设备端永远不上报对端 MAC 地址。

### 5.3 版本策略

- 主题固定带 `v1`；不兼容变更使用 `v2` 前缀并行过渡。
- 载荷内 `schema` 字段用于同主题内的字段扩展；服务端遇到未知 `schema`
  拒收并记录告警，不猜测解析。

---

## 6. 附录：调试命令

```bash
# 后端等价订阅（持久会话，收到所有设备上行）
mosquitto_sub -h mqtt.example.com -p 8883 --cafile ca.pem \
  -u bump_backend -P "$BACKEND_PW" -i bump-backend-test -c -q 1 -v \
  -t 'bump/v1/device/+/encounters' -t 'bump/v1/device/+/status'

# 模拟设备上报（ACL：只能写自己的主题）
mosquitto_pub -h mqtt.example.com -p 8883 --cafile ca.pem \
  -u bump-a1b2c3d4e5f6 -P "$DEV_PW" -q 1 \
  -t 'bump/v1/device/bump-a1b2c3d4e5f6/encounters' \
  -m '{"schema":1,"batch_id":"bt_a1b2c3d4e5f6_9f3c1a02_00000001_0001","device_id":"bump-a1b2c3d4e5f6","boot_id":"9f3c1a02","self_short_id":"3f9a01c47b2e","fw":"0.4.0","uptime_ms":1000,"time_source":"uptime","events":[{"event_id":"ev_a1b2c3d4e5f6_9f3c1a02_00000001","peer_short_id":"8c21d0a9e4f5","rssi_avg":-65,"rssi_last":-63,"duration_s":7,"samples":2,"flags":1,"occurred_uptime_ms":900}]}'

# REST 示例
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/v1/encounters?limit=20"
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/v1/encounters?mutual=true"
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/v1/devices"
```
