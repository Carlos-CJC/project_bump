# 近场配对设备 — 云端 / App 交接文档

> **读者**：负责云平台和 App 的组员  
> **设备端现状**：两块 Arduino Nano RP2040 Connect 已完成红外近场配对、LED 状态显示、白色按钮解除，并通过实测。  
> **相关文档**：
> - [HARDWARE.md](HARDWARE.md)：接线、引脚、电源注意事项
> - [OPERATION.md](OPERATION.md)：设备端操作逻辑、状态机、红外协议、测试记录
> - [firmware/nearfield_pairing/nearfield_pairing.ino](firmware/nearfield_pairing/nearfield_pairing.ino)：当前固件
>
> 本文重点不是重复红外协议，而是说明 **设备端能给云端/App 提供什么数据、还缺什么、云端和 App 建议怎么设计**。

## 目录

- 0. 一句话总结
- 1. 项目现状
- 2. 设备端能力与数据接口
- 3. 红外协议摘要
- 4. 云端 / App 总体架构建议
- 5. 数据模型建议
- 6. API 设计建议
- 7. 关键流程
- 8. 设备端改造建议
- 9. 安全与隐私
- 10. 可选 v2：红外交换完整 UID
- 11. 测试计划与验收标准
- 12. 已知限制与待决策
- 13. 代码与文件索引
- 14. 术语表
- 15. 交接清单

---

## 0. 一句话总结

设备端现在能在两人把设备靠近时完成 **红外三次握手**，拿到对方的 **16 bit 机器码**，并用 LED 显示“搜索 / 握手 / 配对成功”。  
云端/App 需要做的是：

1. 给每台设备建立云端身份（用 **64 bit Chip UID** 作为主键）；
2. 把“设备 A 和机器码 `C371` 配对成功”解析成“设备 A 和设备 B 配对成功”；
3. 创建好友请求 / 好友关系；
4. 在 App 里展示好友、通知、接受/拒绝；
5. 处理未注册、离线、重复上报、机器码碰撞等边界情况。

---

## 1. 项目现状

### 1.1 已完成（设备端）

- 两块板烧同一份固件，机器码由芯片 UID 自动生成，不需要手工配置。
- 红外近场配对：
  - `HELLO`（广播）→ `ACK`（定向）→ `DONE`（定向）三次握手；
  - 48 bit 自定义帧，CRC-8 校验；
  - ACK 重发、DONE 连发 3 次、PAIRED 状态补发、8 s / 120 s 超时；
  - 先听后发 + 随机时隙，避免两块板同时发射。
- LED：
  - `IDLE`：白色常亮（配对关闭）
  - `SEARCH`：蓝色慢闪（正在找对方）
  - `CONFIRM`：青色快闪（已听到对方，正在握手）
  - `PAIRED`：绿色常亮（配对成功）
  - 搜索超时：红色闪 4 下回白色
- 按钮：
  - 蓝色：从待机进入配对；
  - 白色：停止配对 / 解除配对，向对方发 `BYE`。
- 串口（115200）：
  - 开机/首次打开串口时打印完整 64 bit `Chip UID` 和 16 bit 机器码；
  - 配对过程打印 `RX HELLO` / `TX ACK` / `PAIRED` 等日志；
  - 每 5 s 打印状态心跳；
  - 调试命令：`b` = 蓝键、`w` = 白键、`s` = 打印状态。

### 1.2 未完成（本文的主要工作）

- Wi-Fi 配网 / 设备绑定；
- 设备注册、心跳、配对事件上报；
- 云端好友关系、好友请求、通知；
- App 界面；
- 设备端 token 持久化、离线事件队列；
- 可选的“完整 UID 交换”协议（见第 10 节）；
- 安全、隐私、限流、去重。

---

## 2. 设备端能力与数据接口

### 2.1 硬件 / 固件栈

| 项目 | 说明 |
|---|---|
| 主控 | Arduino Nano RP2040 Connect（RP2040 + NINA-W102 Wi-Fi/BLE 模块） |
| 红外发射 | D9，38 kHz 载波，NEC 风格 pulse-distance |
| 红外接收 | D4，HX1838 / VS1838B，输出空闲 HIGH、收到 38 kHz 为 LOW |
| 按钮 | 白色 D2、蓝色 D3，`INPUT_PULLUP`，按下 LOW |
| LED | 板载 RGB，挂在 NINA 模块上，通过 `WiFiNINA` 的 `LEDR/LEDG/LEDB` 控制 |
| 开发环境 | Arduino IDE / `arduino-cli`，核心 `arduino:mbed_nano:nanorp2040connect` |
| 关键库 | IRremote 4.7.1、WiFiNINA 2.1.1 |
| 供电 | USB 5V；红外发射模块目前 3V3，距离不够可改 5V（见 HARDWARE.md） |

> **NINA 是共享资源**：板载 RGB LED 和 Wi-Fi 都走 NINA。现在固件已经用 `WiFiNINA` 初始化了 NINA（第一次 `pinMode(LEDR, OUTPUT)` 会复位 NINA 并等约 0.8 s）。后面加 Wi-Fi 不需要再单独初始化 NINA，但要注意不要在多个线程里同时操作 NINA/SPI。

### 2.2 设备状态机（给云端理解事件用）

```
IDLE ──蓝键──▶ SEARCH ──收到 HELLO──▶ CONFIRM ──收到 ACK/DONE──▶ PAIRED
  ▲               │                      │                         │
  │               │ 白键 / 超时           │ 白键 / BYE              │ 白键 / BYE
  └───────────────┴──────────────────────┴─────────────────────────┘
```

- 只有 `PAIRED` 才是“双方都确认”的状态。
- `CONFIRM` 可能是“我听到对方，但对方还没确认听到我”，App 不应把它当作好友成功。
- 白色按钮是 **结束这次近场配对会话**，不是“删除云端好友”。云端好友关系应独立存在。

### 2.3 设备身份：两个 ID，不要混用

| ID | 长度 | 来源 | 用途 |
|---|---|---|---|
| `uid64` | 64 bit / 16 hex | RP2040 芯片唯一 ID（`flash_get_unique_id`） | **云端主键**、设备注册、账号绑定 |
| `code16` | 16 bit / 4 hex | `uid64` 经 FNV-1a 哈希取 16 bit | **红外帧里的本地机器码**、近场链路标签 |

规则：

- `uid64` 全局唯一、稳定、不可变；云端数据库用它做设备主键。
- `code16` 只用于红外帧，**不能当云端身份凭证**；它可能碰撞（1/65536），也可能被伪造。
- `code16 == 0xFFFF` 保留为广播地址，真实设备不会使用。
- 当前两块测试板：

| 端口 | `uid64` | `code16` |
|---|---|---|
| COM3 | `50351706825E8318` | `0xA30F` |
| COM5 | `50236541C8965F1C` | `0xC371` |

> 设备端开机串口会打印这两项。云端可以做一个“设备注册表”，把 `code16` 作为索引/别名，但唯一性约束应放在 `uid64` 上。

### 2.4 当前固件能直接提供的数据

| 数据 | 代码位置 | 说明 |
|---|---|---|
| 本机 `code16` | `myId` | 全局变量，16 bit |
| 本机 `uid64` | `chipUid[8]` | 8 字节数组，串口打印为 16 hex |
| 对方 `code16` | `peerId` | 进入 `PAIRED` 后有效；`BYE`/白键后清零 |
| 当前状态 | `state` | `ST_IDLE` / `ST_SEARCH` / `ST_CONFIRM` / `ST_PAIRED` |
| 状态进入时间 | `stateSince` | `millis()`，仅用于设备端超时，不是 UTC 时间 |
| 配对成功 | `handleFrame()` 中进入 `PAIRED` 的分支 | 目前只打印日志，没有回调 |
| 解除配对 | `onWhitePress()` / `MSG_BYE` 分支 | 目前只打印日志 |

> 设备没有 RTC，`millis()` 是上电后的毫秒数，不是真实时间。云端事件时间应以 **云端收到请求的时间** 为准；设备可以附带 `uptime_ms` 仅作调试。

### 2.5 建议给云端集成加的固件接口

当前固件是一个 `.ino`，云端同学可以直接加一个 `CloudLink` 模块。建议先加下面这组轻量接口，避免把 Wi-Fi/HTTP 逻辑塞进 `handleFrame()`：

```cpp
// 设备身份
uint16_t getDeviceCode16();          // myId
const uint8_t* getDeviceUid64();     // chipUid[8]

// 配对状态
bool isIrPaired();                   // state == ST_PAIRED
uint16_t getPeerCode16();            // peerId，未配对返回 0

// 事件回调（由 CloudLink 实现）
void onIrPaired(uint16_t peerCode16);    // 进入 PAIRED 时调用一次
void onIrUnpaired(uint16_t peerCode16);  // 白键/BYE 解除时调用
```

在 `handleFrame()` 里进入 `PAIRED` 的位置加：

```cpp
onIrPaired(sender);
```

只在 **从 `SEARCH` / `CONFIRM` 进入 `PAIRED`** 的两个分支调用一次；在 `PAIRED` 状态下收到重复 ACK/HELLO 时的补发分支不要重复触发，否则云端会收到重复事件。

在 `onWhitePress()` 发送 `BYE` 之前加：

```cpp
if (peerId != 0) onIrUnpaired(peerId);
```

**不要在 `onIrPaired()` 里直接做阻塞的 HTTP 请求。** 更稳妥的做法是：

- 回调只把事件放进一个小队列；
- `CloudLink::loop()` 在 `loop()` 里非阻塞地处理队列；
- 队列元素建议包含：`event_id`（随机 UUID/32 bit）、`peer_code16`、`uptime_ms`、`retry_count`。

### 2.6 串口日志与调试命令

串口 115200，日志示例：

```
=== IR Near-Field Pairing ===
Chip UID: 50351706825E8318
My machine code: 0xA30F
Blue = start pairing, White = stop/unpair

Blue: start pairing
TX HELLO -> BCAST
RX HELLO from 0xC371
HELLO peer=0xC371 state=CONFIRM
TX ACK -> 0xC371
RX DONE from 0xC371 to 0xA30F
PAIRED (DONE) peer=0xC371 state=PAIRED
[state=PAIRED my=0xA30F peer=0xC371]
```

调试命令（单字符，不需要回车）：

| 字符 | 作用 |
|---|---|
| `b` | 等同按蓝色按钮 |
| `w` | 等同按白色按钮 |
| `s` | 打印当前状态 |

> `IR other: proto=... bits=...` 是排查日志，表示“收到了但不是本协议的 48 bit 帧”。正常情况下可能偶尔出现，不影响配对。

### 2.7 重要语义（云端/App 必须知道）

1. **白色按钮 = 结束这次近场会话**，不是删除好友。  
   设备端会发 `BYE` 让对方也回 `IDLE`，但云端好友关系应保留。
2. **设备端不保存好友列表**。  
   `peerId` 只在 RAM 里，重启/白键后丢失；云端才是好友关系的唯一事实来源。
3. **设备端不保存配对历史**。  
   如果 Wi-Fi 断开时配对，事件只在 RAM 队列里；断电会丢。云端/App 要能处理“设备说配对过，但云端没收到”的情况（例如让用户重试）。
4. **LED 归配对状态机所有**。  
   云端同学如果要做“Wi-Fi 连接中/同步中”的指示，需要和配对状态机约定优先级，或者用另一个 LED/串口日志，不要直接覆盖 `setLed()`。
5. **`PAIRED` 是双方都确认**。  
   只有 `PAIRED` 才应该上报“配对成功”；`CONFIRM` 不是。

---

## 3. 红外协议摘要（理解事件来源即可）

### 3.1 帧格式

48 bit，LSB first，NEC 风格时序：

| bit | 字段 | 说明 |
|---|---|---|
| 0–3 | `version` | 当前 `0x1` |
| 4–7 | `type` | `1=HELLO`、`2=ACK`、`3=DONE`、`4=BYE` |
| 8–23 | `sender` | 发送方 `code16` |
| 24–39 | `target` | 目标 `code16`；`0xFFFF` = 广播 |
| 40–47 | `CRC-8` | poly `0x07`，覆盖低 40 bit |

物理层：38 kHz，9 ms mark + 4.5 ms space 引导，560 µs mark，0 的 space 560 µs，1 的 space 1690 µs，结束位 560 µs。单帧约 90–120 ms。

### 3.2 三次握手

```
A: HELLO(广播, sender=A)
B: ACK(target=A, sender=B)
A: DONE(target=B, sender=A)  → A 进入 PAIRED
B: 收到 DONE → B 进入 PAIRED
```

- A 收到 ACK ⇒ A 知道 B 收到了 A；
- B 收到 DONE ⇒ B 知道 A 收到了 B；
- 如果 DONE 丢了，B 继续发 ACK，A 在 PAIRED 状态听到 ACK 会补发 DONE。

### 3.3 时序/重试

| 参数 | 值 |
|---|---|
| HELLO 间隔 | 260–700 ms 随机 |
| ACK 重发 | 320 ms + 0–100 ms 随机 |
| DONE 连发 | 3 次，间隔 280 ms |
| CONFIRM 超时 | 8 s → 回 SEARCH |
| SEARCH 超时 | 120 s → 红闪 → IDLE |
| 发射后保护 | 30 ms（等接收头 AGC 恢复） |
| 先听后发 | 3 ms 采样，低电平 >250 µs 视为忙 |

### 3.4 关键限制：红外帧里只有 `code16`，没有 `uid64`

这是云端/App 设计里最重要的一点：

- 配对成功后，设备 A 只知道对方是 `0xC371`，不知道对方的 64 bit `uid64`。
- 云端要把 `0xC371` 解析成“设备 B / 用户 B”，有两个方案：

| 方案 | 做法 | 优点 | 缺点 | 建议 |
|---|---|---|---|---|
| **v1：云端注册表** | 所有设备注册时上报 `{uid64, code16}`；配对事件上报 `peer_code16`，云端查表 | 固件不用改协议，最快落地 | 依赖对方已注册；`code16` 碰撞/伪造风险 | **课程演示推荐** |
| **v2：红外 UID 交换** | 配对成功后，双方用额外的 IR 帧交换完整 `uid64` | 不依赖全局注册表；无碰撞 | 需要改固件协议，增加 4–5 帧 | 产品化再做 |

本文第 4–9 节按 **v1** 设计，第 10 节给出 v2 的可选方案。课程项目先用 v1 即可。

---

## 4. 云端 / App 总体架构建议

### 4.1 架构图

```
        ┌──────────────────────┐        IR 近场（5–30 cm）        ┌──────────────────────┐
        │ Device A             │◀────────────────────────────────▶│ Device B             │
        │ RP2040 + NINA        │   HELLO / ACK / DONE / BYE       │ RP2040 + NINA        │
        │ code16 = A30F        │                                  │ code16 = C371        │
        │ uid64 = 5035...8318  │                                  │ uid64 = 5023...5F1C  │
        └──────────┬───────────┘                                  └──────────┬───────────┘
                   │ Wi-Fi / HTTPS / MQTT                                  │
                   └──────────────────────┬────────────────────────────────┘
                                          ▼
                             ┌─────────────────────────┐
                             │ Cloud backend           │
                             │ - Auth / device registry│
                             │ - Pairing resolution    │
                             │ - Friend requests       │
                             │ - Friend list / events  │
                             └────────────┬────────────┘
                                          │ HTTPS + WebSocket/MQTT
                             ┌────────────▼────────────┐
                             │ Mobile / Web App        │
                             │ - 登录 / 绑定设备        │
                             │ - 好友请求 / 好友列表     │
                             │ - 配对通知               │
                             └─────────────────────────┘
```

### 4.2 身份与绑定

| 概念 | 说明 |
|---|---|
| `uid64` | 设备硬件主键；云端 `devices.uid64` 唯一 |
| `code16` | 近场链路标签；云端作为 `devices.code16` 索引，**不唯一** |
| `user_id` | App 用户账号 |
| `device_token` | 设备注册后云端颁发的长期 token；设备用它调设备端 API |
| `user_jwt` | App 登录后的用户 token；App 用它调用户端 API |
| `claim_code` | 用户把设备绑定到自己账号时用的一次性/短期码 |

绑定流程建议：

1. 设备首次联网，`POST /v1/devices/register` 上报 `{uid64, code16, fw_version}`，云端返回 `device_id` + `device_token`。设备把 token 存到 flash（见 8.4）。
2. 用户在 App 里输入/扫描设备的 `claim_code`（或直接输入设备标签上的 `uid64` 后 6 位），`POST /v1/devices/claim` 把设备绑定到 `user_id`。
3. 设备后续心跳/配对事件都带 `device_token`，云端据此知道是哪台设备，**不依赖设备自报的 `uid64`**。

> 设备没有屏幕。`claim_code` 的来源需要团队决定：
> - 课程演示：串口打印的 `uid64` / `code16`，或贴在设备上的标签；
> - 产品化：NINA BLE 配网时由 App 读取，或设备外壳印 QR 码。
>
> 不要用 `code16` 单独作为绑定凭证，它只有 16 bit，容易被猜到/碰撞。

### 4.3 配对事件流（v1）

```
用户 A          设备 A                云端                  设备 B / App B
  │ 蓝键           │                    │                       │
  │───────────────▶│                    │                       │
  │                │ IR HELLO/ACK/DONE  │                       │
  │                │◀──────────────────▶│                       │
  │                │ PAIRED (peer=C371) │                       │
  │                │ POST /pairings     │                       │
  │                │───────────────────▶│                       │
  │                │                    │ 查 code16=C371         │
  │                │                    │ 找到 B 的 uid64/账号    │
  │                │                    │ 创建 friend_request    │
  │                │                    │──────────────────────▶│
  │                │                    │                       │ 通知 B
  │                │                    │                       │ B 接受
  │                │                    │◀──────────────────────│
  │                │                    │ 建立 friendship        │
  │                │                    │──────────────────────▶│
  │                │                    │                       │ App 显示好友
```

关键点：

- **发起方 A 上报一次就够了**。如果 B 也上报，云端用 `event_id` 去重，并按规范化设备对 `(min(uidA,uidB), max(uidA,uidB))` 合并，避免重复好友请求；优先使用能同时解析出双方 `uid64` 的那条事件。
- 云端解析 `C371` 时，必须检查该 `code16` 是否唯一对应一台已注册设备：
  - 唯一 → `matched`，返回 B 的 `uid64` / 显示名；
  - 没有 → `pending_responder`，先存起来，等 B 注册/绑定后再匹配；
  - 多台 → `ambiguous`，不要自动加好友，让用户重试或走 v2 UID 交换。
- 云端的“配对成功”只是**好友请求的触发条件**，不建议直接自动建立好友关系。建议 B 在 App 里确认“接受/拒绝”，避免骚扰和误配。

### 4.4 边界情况

| 情况 | 建议处理 |
|---|---|
| 对方设备未注册 | 存 `pending_pairing`，按 `code16` 索引；对方注册/绑定后 24 h 内匹配并通知；超时过期 |
| 对方设备已注册但未绑定用户 | 同样存 pending；绑定后再解析 |
| 对方离线 | 云端仍可创建 friend_request；App 推送/下次上线拉取 |
| 本机离线 | 设备端 RAM 队列重试；断电会丢（v1 限制）。App 可显示“上次配对未同步，请靠近再试” |
| `code16` 碰撞 | 云端返回 `ambiguous`，不自动加好友；提示用户重试/换位置；长期方案是 v2 |
| 重复上报 | `event_id` 唯一索引；或 `(initiator_uid64, responder_code16, occurred_at 窗口)` 去重 |
| 伪造 `code16` | 设备 token 鉴权 + 限流 + 对方确认；不要仅凭 `code16` 建立关系 |
| 白键解除 | 只结束近场会话；云端好友关系保留；App 可显示“设备已断开近场”但不删好友 |
| 一方上报、另一方没上报 | 云端以先到者为准；另一方上线后通过好友请求/通知补齐 |
| 配对后设备重启 | 设备端 `peerId` 丢失；云端已有好友关系；App 不受影响 |

### 4.5 选型建议（供讨论）

- **课程项目、团队小、想快**：Supabase（Postgres + Auth + Realtime + REST）或 Firebase（Auth + Firestore + Cloud Functions + FCM）。两者都能覆盖登录、数据库、实时通知。
- **想自己掌控后端/关系型查询**：Node.js/Express 或 FastAPI + Postgres + WebSocket；部署到 Render/Railway/Fly.io。
- **实时通知**：Firebase Cloud Messaging、Supabase Realtime、或自建 WebSocket。
- **不要自己实现密码存储**；用托管 Auth 或成熟的 OAuth。
- 设备端 HTTPS 客户端要考虑 TLS 证书和内存；如果课程时间紧，可以先 HTTP + 内网，但生产必须 TLS。

---

## 5. 数据模型建议

以下用关系型数据库描述；用 Firestore/MongoDB 也可以按同样字段建集合。

### 5.1 `users`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID/PK | 用户 ID |
| `email` / `username` | string unique | 登录名 |
| `display_name` | string | App 显示名 |
| `avatar_url` | string nullable | 头像 |
| `created_at` | timestamp | |
| `last_seen_at` | timestamp | |

### 5.2 `devices`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID/PK | 设备 ID |
| `uid64` | char(16) unique | 芯片 UID，主键 |
| `code16` | char(4) indexed | 红外机器码，**不唯一** |
| `owner_user_id` | FK users nullable | 绑定用户；未绑定为 null |
| `name` | string | 用户给设备起的名字 |
| `firmware_version` | string | |
| `device_token_hash` | string | 只存 token 哈希 |
| `last_seen_at` | timestamp | |
| `status` | enum | `active` / `offline` / `retired` |

索引：

- `unique(uid64)`
- `index(code16)`（非唯一）
- `index(owner_user_id)`

### 5.3 `pairing_events`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID/PK | 事件 ID |
| `event_id` | string unique | 设备生成的幂等 ID |
| `initiator_device_id` | FK devices | 上报的设备 |
| `initiator_uid64` | char(16) | 冗余，便于排查 |
| `responder_code16` | char(4) indexed | 对方机器码 |
| `responder_device_id` | FK devices nullable | 解析成功后填 |
| `status` | enum | `matched` / `pending_responder` / `ambiguous` / `expired` |
| `created_at` | timestamp | 云端时间 |
| `resolved_at` | timestamp nullable | |
| `source` | string | `IR` |
| `raw` | JSON | 设备上报的原始字段，便于审计 |

### 5.4 `friend_requests`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID/PK | |
| `from_user_id` | FK users | 发起方 |
| `to_user_id` | FK users | 接收方 |
| `pairing_event_id` | FK pairing_events | 来源 |
| `status` | enum | `pending` / `accepted` / `declined` / `expired` |
| `created_at` | timestamp | |
| `responded_at` | timestamp nullable | |

约束：同一对用户最多一个 `pending`；已存在 friendship 时不再创建。

### 5.5 `friendships`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID/PK | |
| `user_a_id` | FK users | 规范化：`min(user_id)` |
| `user_b_id` | FK users | 规范化：`max(user_id)` |
| `pair_key` | string unique | `min:max`，防重复 |
| `created_at` | timestamp | |
| `source_pairing_event_id` | FK pairing_events nullable | |
| `status` | enum | `active` / `blocked` |

索引：`unique(pair_key)`，`index(user_a_id)`，`index(user_b_id)`。

### 5.6 `device_claims`（可选）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID/PK | |
| `device_id` | FK devices | |
| `claim_code_hash` | string | 一次性/短期码 |
| `expires_at` | timestamp | |
| `claimed_by_user_id` | FK users nullable | |
| `claimed_at` | timestamp nullable | |

### 5.7 `device_sessions` / token

设备 token 建议只存哈希；设备端保存明文 token。App 用户用 JWT/OAuth，不需要这个表。

---

## 6. API 设计建议

### 6.1 通用约定

- HTTPS + JSON；设备端可用 HTTPS 或 MQTT over TLS。
- 设备端：`Authorization: Bearer <device_token>`。
- App 端：`Authorization: Bearer <user_jwt>`。
- 幂等：设备生成的 `event_id`（UUID 或 32 bit 随机 + 设备 ID）作为唯一键；重复上报返回同一条记录。
- 时间：云端以服务器时间 `created_at` 为准；设备附带的 `uptime_ms` 只作调试。
- 错误码：
  - `400` 参数错误
  - `401` token 无效
  - `403` 无权限
  - `404` 资源不存在
  - `409` 重复/冲突
  - `422` 语义错误（如 `ambiguous`）
  - `429` 限流
  - `503` 服务不可用

### 6.2 设备注册 / 心跳

**注册**（首次联网）：

```http
POST /v1/devices/register
Content-Type: application/json

{
  "uid64": "50351706825E8318",
  "code16": "A30F",
  "firmware_version": "1.0.0",
  "board": "nano-rp2040-connect"
}
```

响应：

```json
{
  "device_id": "dev_123",
  "device_token": "dt_...",
  "registered_at": "2026-11-18T12:00:00Z"
}
```

> 生产环境应加注册凭证/claim code，避免任何人拿 `uid64` 注册。课程项目可以先用“首次注册 + 管理端清理”的简化方案。

**心跳**：

```http
POST /v1/devices/heartbeat
Authorization: Bearer <device_token>

{
  "status": "online",
  "uptime_ms": 123456,
  "firmware_version": "1.0.0"
}
```

### 6.3 配对事件上报

```http
POST /v1/pairings
Authorization: Bearer <device_token>
Content-Type: application/json

{
  "event_id": "evt_9f3a1c2b",
  "initiator_uid64": "50351706825E8318",
  "initiator_code16": "A30F",
  "responder_code16": "C371",
  "occurred_at": "2026-11-18T12:34:56Z",
  "uptime_ms": 456789,
  "firmware_version": "1.0.0"
}
```

响应（已匹配）：

```json
{
  "pairing_id": "pr_123",
  "status": "matched",
  "responder": {
    "uid64": "50236541C8965F1C",
    "display_name": "Alice",
    "user_id": "u_456"
  },
  "friend_request_id": "fr_789"
}
```

响应（对方未注册）：

```json
{
  "pairing_id": "pr_123",
  "status": "pending_responder",
  "responder": null,
  "friend_request_id": null
}
```

响应（机器码碰撞）：

```json
{
  "pairing_id": "pr_123",
  "status": "ambiguous",
  "candidate_count": 2,
  "message": "Multiple devices share code16 C371; please retry or use UID exchange."
}
```

服务端逻辑：

1. 校验 token 对应的设备；忽略请求体里的 `initiator_uid64` 与 token 不一致的情况（或直接拒绝）。
2. 用 `responder_code16` 查 `devices`：
   - 0 条：`pending_responder`，存 `pairing_events`，等对方注册；
   - 1 条：`matched`，填 `responder_device_id`；如果双方都已绑定用户，创建 `friend_request`（或返回给 App 决定）；
   - >1 条：`ambiguous`，不自动加好友。
3. 用 `event_id` 去重。
4. 如果双方已经是好友，返回 `already_friends=true`，不创建重复请求。
5. 解析 `responder_code16` 后：
   - 唯一候选 = 发起方自己 → 拒绝（防自己配自己）；
   - 唯一候选 = 其他设备 → 正常匹配；
   - 多个候选 → `ambiguous`，不要自动加好友；
   - 0 个候选 → `pending_responder`。

### 6.4 好友请求 / 好友列表

```http
GET /v1/friend-requests?status=pending
Authorization: Bearer <user_jwt>
```

```json
{
  "items": [
    {
      "id": "fr_789",
      "from": { "user_id": "u_123", "display_name": "Bob" },
      "pairing": { "pairing_id": "pr_123", "created_at": "..." },
      "status": "pending"
    }
  ]
}
```

```http
POST /v1/friend-requests/fr_789/accept
Authorization: Bearer <user_jwt>
```

```http
POST /v1/friend-requests/fr_789/decline
Authorization: Bearer <user_jwt>
```

```http
GET /v1/friends
Authorization: Bearer <user_jwt>
```

```json
{
  "items": [
    {
      "user_id": "u_456",
      "display_name": "Alice",
      "avatar_url": "...",
      "friends_since": "2026-11-18T12:35:10Z",
      "source": "IR_PAIRING"
    }
  ]
}
```

### 6.5 设备绑定 / 认领

```http
POST /v1/devices/claim
Authorization: Bearer <user_jwt>

{
  "claim_code": "X7K2Q9",
  "uid64": "50351706825E8318"
}
```

响应：

```json
{
  "device_id": "dev_123",
  "uid64": "50351706825E8318",
  "code16": "A30F",
  "owner_user_id": "u_123"
}
```

> `claim_code` 的生成/展示方式待定（标签、串口、BLE、QR）。课程演示可以先用 `uid64` 后 6 位 + 管理端确认，**不要**让用户只凭 `code16` 认领。

### 6.6 实时事件（WebSocket / MQTT）

WebSocket 事件示例：

```json
{ "type": "pairing.matched", "pairing_id": "pr_123", "peer": { "display_name": "Alice" } }
{ "type": "friend_request.created", "request_id": "fr_789", "from": { "display_name": "Bob" } }
{ "type": "friend_request.accepted", "request_id": "fr_789" }
{ "type": "friend.online", "user_id": "u_456" }
```

MQTT 主题（可选）：

```
devices/{uid64}/events
users/{user_id}/friends
users/{user_id}/pairings
```

> 设备端如果只做 HTTPS 轮询，也可以先不上 WebSocket；App 端用 WebSocket 体验更好。

---

## 7. 关键流程

### 7.1 设备首次绑定

```
用户 App               设备                云端
  │  打开 App             │                   │
  │  登录                 │                   │
  │  输入/扫描 claim_code │                   │
  │──────────────────────────────────────────▶│
  │                      │                   │ 校验 claim_code
  │                      │                   │ 绑定 uid64 → user
  │◀──────────────────────────────────────────│
  │  显示设备列表         │                   │
```

设备端需要：

1. 连 Wi-Fi；
2. 注册设备，拿 `device_token`；
3. 把 token 存 flash；
4. 周期性心跳。

### 7.2 两人红外配对并加好友

```
A 按蓝键                B 按蓝键
   │                       │
   └──── 设备靠近，IR 三次握手 ────┐
                                   ▼
                         A、B 都进入 PAIRED（绿灯）
                                   │
                         A 上报 POST /pairings
                                   │
                         云端解析 B 的 code16
                                   │
                         创建 friend_request A→B
                                   │
                         B App 收到通知
                                   │
                         B 接受 → friendship
                                   │
                         A/B App 显示好友
```

### 7.3 对方未注册 / 离线

```
A 配对成功，上报 peer=C371
        │
        ▼
云端 devices 表没有 C371
        │
        ▼
pairing_events.status = pending_responder
        │
        ├─ B 之后注册/绑定 → 匹配 pending → 创建 friend_request → 通知双方
        └─ 24 h 未匹配 → expired
```

### 7.4 白色按钮解除近场会话

```
A 按白键
  │
  ├─ 发 BYE 给 B
  ├─ A → IDLE（白灯）
  └─ B 收到 BYE → IDLE（白灯）
        │
        ▼
云端好友关系不变
App 可显示“设备已结束近场会话”，但不删除好友
```

> 这一点必须在产品文案/App 里说清楚，否则用户会以为按白键就是删好友。

### 7.5 App 界面建议

设备本身只有 LED，没有屏幕，所以 **App 是主要的文字/身份界面**。建议至少有以下页面：

| 页面 | 内容 | 备注 |
|---|---|---|
| 登录/注册 | 邮箱/OAuth | 可用 Firebase Auth / Supabase Auth |
| 设备列表 | 我的设备、在线状态、电量（暂无） | 显示设备名、`code16` 后 4 位、最后在线 |
| 绑定设备 | 输入 claim code 或扫 QR | 设备无屏幕，claim code 需标签/串口/BLE |
| 开始配对 | “请按设备蓝色按钮，并把两台设备面对面靠近” | v1 设备端没有 App 触发配对的命令通道，需物理按键 |
| 配对结果 | “已发现 <昵称>，是否添加好友？” | 对方未注册时显示“已记录，待对方注册” |
| 好友请求 | 来自谁的请求、通过近场配对、接受/拒绝 | 推送/站内通知 |
| 好友列表 | 昵称、头像、好友来源、最后在线 | 支持下拉刷新 |
| 好友详情 | 设备信息、解除好友、拉黑/举报 | 解除好友不影响设备端近场状态 |
| 设置/隐私 | 通知、隐私、账号注销 | |

配对 UX 建议：

```
设备列表 → 点击“开始近场配对”
        → App 提示：按设备蓝键，并把两台设备面对面
        → 设备绿灯（物理确认）
        → App 收到“配对成功”事件
        → 显示对方昵称/请求
        → 接受 → 好友列表
```

- App 不要把 `code16` 当作唯一身份展示给用户；优先显示昵称/头像。
- 对方未注册/未绑定账号时，App 应显示“待对方注册”，不要显示错误。
- `ambiguous` 时提示“附近有多台设备使用相同机器码，请重试或换一个位置”。
- 离线时提示“配对已记录，等待同步”，不要直接失败。

---

## 8. 设备端改造建议（给做云/App 的同学）

### 8.1 Wi-Fi 配网

当前固件没有 Wi-Fi 连接逻辑，只用了 `WiFiNINA` 点 RGB。建议：

- **课程演示**：在 `config.h` 里写死 SSID/密码，或通过串口命令配置；
- **产品化**：BLE 配网（NINA 硬件支持 BLE，需确认 Arduino BLE 库在 Nano RP2040 Connect 上的可用性）或 Wi-Fi AP 配网；
- 连接过程不要阻塞 `loop()` 太久；用非阻塞状态机，或者在配对空闲时连接。

### 8.2 CloudLink 模块

建议新增 `CloudLink` 类，与 IR 状态机解耦：

```cpp
class CloudLink {
public:
  void begin();                       // 读 token、连 Wi-Fi
  void loop();                        // 非阻塞：注册/心跳/上报队列
  void onIrPaired(uint16_t peerCode16);
  void onIrUnpaired(uint16_t peerCode16);
  bool isOnline() const;
};
```

事件队列建议：

```cpp
struct PairingEvent {
  uint32_t eventId;
  uint16_t peerCode16;
  uint32_t uptimeMs;
  uint8_t retries;
};
```

`onIrPaired()` 只入队，`loop()` 里发送；失败指数退避重试。

### 8.3 不要阻塞 IR 状态机

- `sendPulseDistanceWidth()` 本身会阻塞约 100 ms，这是正常的；
- HTTP/TLS 请求可能阻塞几秒，**不要**在 `handleFrame()`/`onIrPaired()` 里直接调用；
- 如果要用多线程，注意 NINA/SPI 是共享资源：RGB LED 和 Wi-Fi 都走 NINA，需要互斥；
- 简单方案：配对成功后再发 HTTP，且设置 3–5 s 超时；配对结束后用户不会马上再按按钮，短暂阻塞可以接受，但要在文档里写明。

### 8.4 token / 事件持久化

- token 建议存在 RP2040 flash（Mbed `KVStore`/`FlashIAP`）或 NINA flash（`WiFiStorage`）；
- 不要每次心跳都写 flash；
- 配对事件队列如果只放 RAM，断电会丢；课程项目可接受，产品化建议存 flash 并限制队列长度。

### 8.5 设备注册/心跳状态机

```
BOOT → 读 token
  ├─ 无 token → 连 Wi-Fi → POST /register → 存 token
  └─ 有 token → 连 Wi-Fi → POST /heartbeat
        │
        ▼
   ONLINE ──配对事件──▶ POST /pairings
        │                   │
        │                   ├─ 2xx：出队
        │                   ├─ 409：已存在，出队
        │                   └─ 5xx/超时：重试
        ▼
   OFFLINE → 定时重连
```

### 8.6 `uid64` / `code16` 的导出

当前固件：

- `chipUid[8]` 是 8 字节 UID；
- `myId` 是 16 bit code；
- `printBanner()` 已经打印 16 hex UID 和 4 hex code。

云端同学可以直接复用：

```cpp
char uidHex[17];
for (int i = 0; i < 8; i++) sprintf(&uidHex[i * 2], "%02X", chipUid[i]);
uidHex[16] = '\0';
```

### 8.7 LED 所有权

配对状态机拥有 RGB：

- `IDLE` 白、`SEARCH` 蓝闪、`CONFIRM` 青闪、`PAIRED` 绿；
- 云端不要直接 `digitalWrite(LEDR, ...)`；
- 如果要在 App 里显示“云同步中”，建议用另一颗 LED、串口日志，或和固件同学约定一个临时覆盖接口（例如 `cloudLedOverride()`，优先级低于配对状态）。

---

## 9. 安全与隐私

1. **IR 配对是“物理接近”的证明，不是身份认证。**  
   任何人拿一个 IR 发射器都能伪造 `code16`。云端应把它当作“介绍/请求”，最终由用户确认。
2. **设备 token 是设备身份的唯一凭证。**  
   不要只凭请求体里的 `uid64` 认设备；用 token 反查设备。
3. **`code16` 不是秘密。**  
   它会在空中广播，也会打印在日志里；不要用它做密码、token 或唯一身份。
4. **防重放。**  
   `event_id` 唯一索引；可选 `nonce` + 时间窗（如 5 min）；重复请求返回同一条记录。
5. **限流。**  
   每台设备每分钟最多 N 次配对上报；每用户每天最多 M 次好友请求。
6. **好友请求需要对方确认。**  
   不建议 IR 一配对就自动互加好友，避免骚扰/误配。
7. **隐私。**  
   App 只展示 `display_name`/头像，不展示 `uid64`；日志/数据库中 `uid64` 属于内部标识。
8. **拉黑/举报。**  
   好友关系支持 `blocked`；被拉黑后不再接收对方的配对请求。
9. **传输安全。**  
   生产环境必须 HTTPS/TLS；设备端校验证书（或使用预置 CA）；不要用明文 HTTP 传 token。

---

## 10. 可选 v2：红外交换完整 UID

> 课程演示不需要；如果团队希望“不依赖全局 `code16` 注册表”，再做这一节。

### 10.1 目标

配对成功后，A、B 各自拿到对方的 64 bit `uid64`，云端不再需要按 `code16` 猜。

### 10.2 建议帧型

当前 48 bit 帧只够放两个 16 bit ID。可以新增一个 64 bit 的 `UID_CHUNK` 帧：

| bit | 字段 | 说明 |
|---|---|---|
| 0–3 | `version` | `0x1` |
| 4–7 | `type` | `5 = UID_CHUNK` |
| 8–23 | `sender` | 发送方 `code16` |
| 24–39 | `target` | 目标 `code16` |
| 40–41 | `chunk_index` | 0–3 |
| 42–57 | `uid_chunk` | 16 bit，4 帧拼 64 bit |
| 58–63 | `CRC-6` | 覆盖前面 58 bit |

> CRC-6 较弱；更稳妥的是把帧扩到 72/80 bit，用 8 bit CRC。课程项目可以不做到这一步，v1 注册表更简单。

### 10.3 流程

```
PAIRED（现有三次握手完成）
  │
  ├─ A → B: UID_CHUNK(index=0..3, A.uid64)
  ├─ B → A: UID_CHUNK(index=0..3, B.uid64)
  ├─ 双方拼出对方 uid64，校验 CRC/重复
  └─ 双方各发一个 UID_DONE（可复用 DONE 帧或新类型）
```

- 每帧可以重发 2–3 次；
- 收到 4 个 chunk 后校验；
- 失败则退回 v1 的 `code16` 注册表方案。

### 10.4 与 v1 的关系

- v1：设备只上报 `{self_uid64, peer_code16}`，云端查表；
- v2：设备上报 `{self_uid64, peer_uid64}`，云端不需要 `code16` 索引；
- 可以两者都支持：优先用 `peer_uid64`，没有则退回 `peer_code16`。

---

## 11. 测试计划与验收标准

### 11.1 云端单元测试

- `code16` 唯一 → `matched`；
- `code16` 不存在 → `pending_responder`；
- 多条同 `code16` → `ambiguous`；
- 重复 `event_id` → 返回同一条记录，不重复创建好友请求；
- 已是好友 → 不重复创建；
- 自己配自己 → 拒绝；
- 对方未绑定用户 → pending，绑定后匹配；
- 24 h 过期 → expired。

### 11.2 端到端测试

1. 两台设备、两个账号；
2. 两台设备各按蓝键，靠近 → 绿灯；
3. 设备上报配对事件 → 云端匹配 → B 收到好友请求；
4. B 接受 → 双方好友列表出现对方；
5. 按白键结束近场会话 → 云端好友仍在；
6. 重新配对 → 不重复创建好友请求；
7. 断开 Wi-Fi 后配对 → 恢复后重试成功（或 App 提示待同步）；
8. 对方未注册 → 注册后 pending 自动匹配；
9. 重放旧事件 → 云端去重；
10. 碰撞模拟（两台设备同 `code16`）→ `ambiguous`，不自动加好友。

### 11.3 验收标准

- [ ] 两台设备靠近后 1–3 s 内都绿灯；
- [ ] 云端在配对后 5 s 内收到事件（Wi-Fi 正常）；
- [ ] App 在 5 s 内收到好友请求通知；
- [ ] 接受后双方好友列表一致；
- [ ] 白键只结束近场会话，不删好友；
- [ ] 重复配对不产生重复好友/请求；
- [ ] 断网/重启不产生脏数据；
- [ ] 日志/数据库能定位一次配对（`pairing_id`、`event_id`、双方 `uid64`）。

### 11.4 演示脚本（建议）

1. 两块设备都是白色常亮（`IDLE`）。
2. 各按一下蓝色按钮 → 蓝色慢闪（`SEARCH`）。
3. 两块设备面对面 5–10 cm → 青色快闪（`CONFIRM`）→ 绿色常亮（`PAIRED`）。
4. 串口展示 `PAIRED peer=0x....`，说明拿到了对方机器码。
5. App 收到配对事件 → 显示好友请求 → 接受 → 好友列表出现对方。
6. 按白色按钮 → 两块设备回到白色常亮；App 好友关系仍然存在。
7. 再次靠近配对 → 不会重复创建好友/请求。

---

## 12. 已知限制与待决策

### 12.1 已知限制

- 红外只有 16 bit `code16`，没有对方 `uid64`（v1 用云端注册表解决）。
- 设备端不持久化好友/配对状态；云端是唯一事实来源。
- 没有 Wi-Fi 配网/claim code 方案；设备无屏幕。
- 没有离线事件持久化；断电会丢队列。
- 红外是视线链路，必须面对面 5–30 cm；外壳/摆放影响很大。
- 设备端 LED 归配对状态机，云端状态指示需要另想办法。
- 16 bit `code16` 有 1/65536 碰撞概率；云端必须处理 `ambiguous`。

### 12.2 待团队决策

1. 云平台：Firebase / Supabase / 自建 / 其他？
2. App：Flutter / React Native / Web / 原生？
3. 配网：写死 Wi-Fi / BLE / AP / QR？
4. 好友请求：自动互加，还是必须对方接受？（建议接受）
5. 是否做 v2 UID 交换？
6. 一个用户是否允许多台设备？
7. `claim_code` 怎么展示（标签/串口/BLE/QR）？
8. 是否需要拉黑/举报/隐私设置？
9. 设备 token 存 RP2040 flash 还是 NINA flash？
10. 云端离线队列保留多久（建议 24 h）？

---

## 13. 代码与文件索引

| 文件/函数 | 作用 |
|---|---|
| `HARDWARE.md` | 接线、引脚、3V3/5V、接收头注意事项 |
| `OPERATION.md` | 设备端状态机、LED、协议、测试记录 |
| `firmware/nearfield_pairing/nearfield_pairing.ino` | 当前固件 |
| `computeMachineId()` | UID64 → code16（FNV-1a） |
| `buildFrame()` / `decodeFrame()` | 48 bit 帧编码/解码、CRC |
| `handleFrame()` | HELLO/ACK/DONE/BYE 状态迁移；**在这里加 `onIrPaired()`** |
| `onWhitePress()` | 白键/`BYE`；**在这里加 `onIrUnpaired()`** |
| `handleSerialCommand()` | `b`/`w`/`s` 调试命令 |
| `printBanner()` | 打印 UID64/code16/说明 |
| `setLed()` | 板载 RGB；注意实测 `HIGH=亮` |
| `sendFrame()` / `trySend()` / `channelBusy()` | 半双工发射、先听后发 |
| `state` / `peerId` / `myId` / `chipUid` | 设备端状态与身份 |

当前实测板：

- COM3：`uid64=50351706825E8318`，`code16=0xA30F`
- COM5：`uid64=50236541C8965F1C`，`code16=0xC371`

---

## 14. 术语表

| 术语 | 含义 |
|---|---|
| `uid64` | RP2040 芯片 64 bit 唯一 ID，云端设备主键 |
| `code16` | 16 bit 红外机器码，近场链路标签 |
| `HELLO` | 广播“我在这里，我的 code16 是 X” |
| `ACK` | 定向“我收到你了，我的 code16 是 Y” |
| `DONE` | 定向“我确认你收到我了，配对完成” |
| `BYE` | 结束近场会话 |
| `PAIRED` | 双方都确认的配对成功状态 |
| `pending_responder` | 云端暂时找不到对方 `code16` 对应的设备 |
| `ambiguous` | 多个设备共用同一个 `code16`，无法唯一解析 |
| `claim_code` | 用户把设备绑定到自己账号的短期码 |
| `device_token` | 设备注册后云端颁发的长期凭证 |
| `friend_request` | 好友请求，需对方接受 |
| `friendship` | 已建立的好友关系（与近场会话独立） |

---

## 15. 交接清单（给云/App 同学）

- [ ] 选定云平台和 App 框架
- [ ] 建 `users` / `devices` / `pairing_events` / `friend_requests` / `friendships`
- [ ] 实现设备注册、心跳、配对上报 API
- [ ] 实现 `code16` 解析：matched / pending / ambiguous
- [ ] 实现好友请求、接受/拒绝、好友列表
- [ ] 实现 App 登录、设备认领、好友请求通知、好友列表
- [ ] 和固件同学确认：Wi-Fi 配网、`CloudLink`、`onIrPaired()`、token 存储
- [ ] 决定是否做 v2 UID 交换
- [ ] 端到端测试：两台设备、两个账号、配对→请求→接受→列表
- [ ] 安全/隐私/限流/去重
- [ ] 把测试结果补回本文档
