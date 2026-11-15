# 基于 BLE 广播的偶遇社交徽章

---

## 1. 产品背景

### 1.1 要解决的问题

线下认识人的机会很多，但"认识"往往没有发生：

- 展会、课堂、社团、旅行中，和有趣的人聊了几句，却忘了留联系方式；
- 掏出手机扫码、搜索 ID、加好友，会打断当下的对话，也显得刻意；
- 事后想找"刚才那个人"，没有任何线索；
- 主流社交 App 面向"已有关系"或"主动匹配"，并不记录线下真实发生的"偶遇"。

### 1.2 现有方案与不足

| 方案 | 优点 | 不足 |
|---|---|---|
| 加微信 / WhatsApp | 通用、熟人社会 | 必须当面操作；交换的是强身份；事后难整理 |
| AirDrop / 附近共享 | 无需配对 | 只能传文件；不记录历史；平台限制 |
| 社交 App 的"附近的人" | 有发现能力 | 依赖手机定位或持续在线；隐私争议；不是线下偶遇 |
| 二维码名片 | 便宜、直观 | 需要主动出示和扫码；无法自动记录 |

### 1.3 产品机会

BLE 广播功耗低、无需配对，可被附近设备被动发现；NFC 提供"碰一碰"的确定性交互；MQTT 适合低带宽、间歇联网的 IoT 设备。把三者结合，可以做一个**低成本、可穿戴、隐私友好**的"线下偶遇记录器"：设备自动记录，用户事后在网页确认，是否加好友由双方决定。

### 1.4 设计原则

- **线下优先**：只记录真实发生的近距离接触；
- **自愿与透明**：可随时关闭广播；广播内容不含姓名、学号等身份信息；
- **事后确认**：自动记录，但不自动成为好友；
- **最小数据**：只保留时间、信号强度、匿名 ID，不采集位置轨迹。

---

## 2. 产品描述

### 2.1 产品形态

一枚可挂在胸前或书包上的徽章（也可做卡片式外壳），核心是 Arduino Nano RP2040 Connect，配 PN532 NFC 读卡模块、锂电池与充电模块；可选配小屏幕，用于显示头像、二维码或状态。

### 2.2 核心体验（用户旅程）

1. **佩戴**：徽章周期性广播一个UUID，同时扫描附近同类设备。
2. **偶遇**：当另一枚设备持续出现在近距离范围内（RSSI 超过阈值并停留数秒），双方各自记录一条"偶遇"。
3. **同步**：徽章连上 Wi-Fi 后，通过 MQTT 把批量记录上传到服务端；断网时先存本地，恢复后重传。
4. **回看**：用户打开网页 / APP，看到"今天 14:32 与某人偶遇 3 分钟"；只有双方都记录到对方时，才显示对方资料。
5. **加好友**：两人通过设备碰一下，NFC 读取对方标签，直接加入好友。
6. **隐私控制**：隐身模式、暂停广播、清除记录、注销账号。

### 2.3 功能范围（MoSCoW）

**Must have（MVP）**

- 设备与账号绑定；
- BLE 广播自定义服务 + 自定义 UUID；
- BLE 扫描与偶遇判定（RSSI 阈值 + 停留时间 + 去重）；
- 本地离线缓存与批量上传；
- Wi-Fi + MQTT 同步到服务端；
- 服务端设备/用户映射、偶遇记录、去重、互遇判定；
- Web / APP 查看偶遇时间线；
- NFC 碰一碰加好友；
- 隐私开关（隐身 / 暂停广播 / 删除记录）。

**Should have**

- 断网重传与幂等去重；
- 设备注册与每设备 MQTT 凭证；
- 好友列表与好友申请管理；
- 简单的偶遇统计（本周遇见人数等）。

**Could have**

- 小屏幕显示头像 / 二维码 / 状态；
- 板载 IMU 计步；
- 外壳个性化（贴纸、灯效）。

### 2.4 系统组成

- **终端**：开发板（BLE 广播/扫描、NFC 读卡、离线缓存、Wi-Fi 上传、可选显示/计步）。
- **接入**：Wi-Fi → MQTT（Mosquitto broker，TLS + 设备凭证）。
- **服务端**：Python FastAPI 订阅 MQTT、写入 SQLite/PostgreSQL、提供 REST API。
- **客户端**：响应式 Web / APP（偶遇时间线、好友申请、隐私设置）。

---

## 3. 技术方案

### 3.1 硬件

- **主控**：Arduino Nano RP2040 Connect（RP2040 双核 133MHz，264KB SRAM，16MB Flash；板载 NINA-W102，BLE 4.2 + 2.4GHz Wi-Fi）。
- **NFC**：PN532（I2C），读取每枚徽章上的 NTAG213 标签。
- **可选**：1.54" TFT（ST7789，SPI）显示OC/头像/二维码；板载 LSM6DSOX 做计步。
- **外壳**：3D 打印或亚克力激光切割；胸牌/挂绳。


### 3.2 BLE 协议

- 使用自定义 Service UUID 作为过滤条件；原型阶段可用 16-bit 测试 UUID。
- 广播数据（Manufacturer Data）建议字段：

  | 字段 | 长度 | 说明 |
  |---|---|---|
  | `ver` | 1B | 协议版本 |
  | `short_id` | 6B | 当前匿名短 ID（不含姓名/学号） |
  | `flags` | 1B | 隐身、可加好友等状态 |
  | `tx_power` | 1B | 标定发射功率，用于 RSSI 距离估计 |

- 采用**不可连接广播**（`BLE.setConnectable(false)`），只广播不建立连接，降低功耗与复杂度。
- BLE 传统广播包上限 31 字节，因此短 ID 控制在 6 字节内，不塞入头像或长文本。
- 短 ID 定期轮换（如每天一次），服务端保存 `(short_id, 有效期) → user` 映射；离线时沿用旧 ID，联网后换新。
- 扫描端按 Service UUID 过滤，记录 `RSSI`、时间戳、短 ID；不持久化 MAC 地址。

### 3.3 偶遇判定

- **条件**：同一短 ID 在 `T` 秒内出现不少于 `N` 次，且 RSSI ≥ 阈值（例如 -70 dBm，需实测标定）。
- **去重**：同一对设备在冷却窗口（如 8 小时）内只保留一条偶遇，更新 `last_seen` 与持续时长。
- **防抖**：RSSI 取滑动平均或中位数，避免人体遮挡导致瞬时跳变。
- **时间**：开机联网时用 NTP 校时；离线记录使用相对时间，上传时由服务端补全接收时间（可选 DS3231 RTC）。

### 3.4 固件并发策略

- **Wi-Fi + BLE**：更新到 WiFiNINA ≥ 2.0.0、ArduinoBLE ≥ 2.0.0、NINA-W102 固件 ≥ 3.0.1 后，可在同一 sketch 中同时使用（2026-03 起官方支持）。若实验室板子固件较旧，先升级；仍不稳定则在上传时暂停 BLE，上传完成后恢复，记录已在本地缓存，不影响偶遇数据。
- **广播 + 扫描**：ArduinoBLE 对"同一设备同时作为 peripheral 和 central"没有官方保证。第 1 周用双机 demo 验证：能同时开启就直接用；不稳定则采用**时分复用**——例如 600ms 广播 / 400ms 扫描，加随机抖动，避免两枚徽章长期错过彼此。两枚设备在数秒内即可互相发现，对"偶遇"场景足够。
- **Plan B**：若时分复用效果仍不佳，改用 ESP32（NimBLE，原生支持同时广播+扫描、Wi-Fi/BLE 共存），协议与后端不变。
- **内存**：关闭不必要的动态分配，使用定长缓冲区；MQTT 载荷采用紧凑 JSON；TLS 交给 NINA 模块，降低 RP2040 压力。

### 3.5 通信与数据

- **MQTT Topic 设计**：
  - `bump/v1/device/{device_id}/encounters`：设备批量上传偶遇；
  - `bump/v1/device/{device_id}/status`：电量、固件版本、在线状态（可选）。
- **载荷示例**：

```json
{
  "device_id": "dev_7f3a",
  "batch_id": "b_20261017_1432",
  "events": [
    {
      "event_id": "e_...",
      "peer_short_id": "a1b2c3d4e5f6",
      "rssi": -63,
      "seen_at": 1760692320,
      "duration_s": 18
    }
  ]
}
```

- **上传策略**：QoS 1 + 本地保留至 ACK；失败指数退避；`event_id` 幂等，服务端去重。
- **服务端数据表（最小集）**：
  - `users(id, display_name, avatar_url, privacy_mode, created_at)`
  - `devices(id, user_id, short_id, secret_hash, last_seen)`
  - `encounters(id, device_a_id, device_b_id, started_at, ended_at, rssi_min, rssi_max, mutual)`
  - `friend_requests(id, from_user_id, to_user_id, method, status, created_at)`
  - `friendships(user_a_id, user_b_id, created_at)`

### 3.6 NFC 加好友流程

1. 每枚徽章贴一张 NTAG213，写入本机短 ID / 服务端 URL（不含个人信息）。
2. 对方用 PN532 读取标签，拿到短 ID。
3. 徽章通过 MQTT / HTTPS 发送好友申请（带设备凭证）。
4. 服务端把申请推送给目标用户；目标在网页确认后建立好友关系。
5. 防骚扰：申请需确认、频率限制、可拉黑。

### 3.7 前端

- **技术栈**：静态 HTML/CSS/JS，轻量框架或原生均可。
- **页面**：登录/注册、偶遇时间线、偶遇详情、好友申请、好友列表、隐私设置、设备绑定。
- **视觉**：卡片式时间线；用"互遇 / 单向"区分状态；隐私开关放在显眼位置。

---

## 4. 可行性分析

### 4.1 技术可行性

| 模块 | 方案 | 可行性判断 |
|---|---|---|
| BLE 广播/扫描 | ArduinoBLE + 自定义 UUID + Manufacturer Data | 高。官方库支持广播、扫描、RSSI、Manufacturer Data、不可连接广播 |
| Wi-Fi + BLE 并发 | 更新 WiFiNINA / ArduinoBLE / NINA 固件 | 中高。2026-03 起官方支持；需按版本要求升级，旧固件有回退方案 |
| 广播 + 扫描并发 | 双机验证；必要时时分复用 | 中高。数秒级发现对"偶遇"场景足够；Plan B 换 ESP32 |
| MQTT 同步 | WiFiNINA + ArduinoMqttClient / PubSubClient | 高。成熟方案；TLS 交给 NINA 模块 |
| NFC 碰一碰 | PN532 读 NTAG213 | 高。读卡器 + 无源标签稳定、便宜，避免 P2P 兼容性问题 |
| 服务端 | Mosquitto + FastAPI + SQLite | 高。组件成熟，开发量可控 |
| 前端 | Web + APP | 中高。工作量较大 |
| 电源 | 1000mAh LiPo + 占空比 | 中。演示可用 USB 供电；便携续航需实测优化 |

**结论**：核心技术均有成熟库或模块支撑，主要不确定性集中在 BLE 双角色与功耗，均可通过双机验证、时分复用、固件升级和 Plan B 规避。


### 4.2 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| ArduinoBLE 无法同时广播 + 扫描 | 偶遇漏检 | 第 1 周双机验证；时分复用 + 随机抖动；Plan B 换 ESP32/NimBLE |
| Wi-Fi 与 BLE 并发不稳定 | 上传中断、丢记录 | 升级三个组件版本；上传时暂停 BLE、传完恢复；本地缓存 |
| RSSI 波动大 | 误判 / 漏判 | 阈值 + 停留时间 + 滑动平均；实测标定；报告中给出检测率 |
| NFC 读卡距离短 / 对不准 | 演示失败 | 选带大天线的 PN532；外壳标注感应区；保留二维码/手动输入后备 |
| 功耗偏高 | 续航不足 | 降低扫描占空比、批量上传、深睡；演示用 USB 供电 |
| 范围蔓延 | 无法按时完成 | MVP 冻结；屏幕/计步/地图等放到最后 |
| 多人或密集环境干扰 | 记录爆炸 | 按 UUID 过滤、扫描结果上限、去重窗口、服务端限流 |

---

## 5. 里程碑与分工（6 周）

| 周 | 目标 | 交付物 |
|---|---|---|
| W1 | 需求冻结、器件下单、开发环境 | 需求清单、BOM、BLE 广播/扫描 demo |
| W2 | 双机偶遇检测 | RSSI 标定数据、偶遇判定与去重、离线缓存 |
| W3 | Wi-Fi + MQTT 上传 | broker、设备注册、批量上传与重传 |
| W4 | 后端 + Web | 数据库、REST API、偶遇时间线页面 |
| W5 | NFC 加好友 + 隐私 | 碰一碰申请、好友确认、隐身/删除 |
| W6 | 联调、外壳、演示 | 3 机联调、续航/检测率数据、演示视频、报告 |

**分工建议（4 人）**

- A：硬件与固件（BLE、NFC、电源、外壳）
- B：通信与后端（MQTT、broker、数据库、API）
- C：前端与产品（网页、APP、交互、视觉、隐私文案）
- D：集成与测试（偶遇算法调参、联调、演示、文档）

---

## 6. 验收标准与预期成果

**功能验收**

- 两枚设备在 1–2m 内停留 ≥10s，网页在下次同步后显示偶遇；
- 同一对设备若干时间内不重复计数；
- 断网 5 分钟后恢复，记录不丢、不重；
- 碰一碰后更新好友列表；
- 隐身模式下不再产生新记录。


**交付物**：可运行样机 3 枚、服务端与 Web 源码、部署说明、演示视频、项目报告（含架构、协议、测试数据、隐私设计）。

---

## 7. Proposal Form 英文版

**Project name (Product name):** Bump — BLE Encounter Badge

**The current situation:**  
At fairs, classes, and trips, people often meet briefly but fail to exchange contacts. Adding friends manually interrupts the moment and is easy to forget, while existing social apps do not record who you actually passed by. There is no lightweight, privacy-conscious way to capture these serendipitous encounters and confirm them later.

**Project objective(s) and feature(s) / function(s) (≤200 words):**  
Bump is a wearable badge that turns nearby encounters into a private, reviewable social timeline. Each badge broadcasts a rotating pseudonymous ID over BLE and scans for other Bump badges. When two badges stay within a set range for several seconds, each records the encounter locally with time and signal strength. The badge uploads batched records over Wi-Fi/MQTT to a server, which maps pseudonymous IDs to users, removes duplicates, and presents the encounter timeline in a web/APP client. An NFC tap reads the other badge's tag and sends a friend request; the recipient approves it online. Key features: (1) BLE broadcast and scan with RSSI/dwell-time filtering; (2) offline buffering and reliable MQTT upload; (3) server-side mapping, deduplication and mutual-encounter detection; (4) web/APP timeline and friend management; (5) NFC tap-to-add; (6) privacy controls (opt-out, rotating IDs, mutual-only visibility). Optional: a small display for avatars/QR codes, and step counting using the onboard IMU.

**Block diagram:**

```
Badge ×3                              Server side
┌───────────────────────┐            ┌───────────────────┐
│ Nano RP2040 Connect   │            │ Mosquitto broker  │
│  BLE advertise/scan   │──Wi-Fi────▶│  (MQTT)           │
│  Wi-Fi + MQTT         │  MQTT      └─────────┬─────────┘
│  Local event buffer   │                      │ subscribe
├───────────────────────┤            ┌─────────▼─────────┐
│ PN532 NFC reader      │            │ FastAPI backend   │
│ NTAG213 tag           │            │ dedup / mapping   │
├───────────────────────┤            │ SQLite/PostgreSQL │
│ Optional TFT / IMU    │            └─────────┬─────────┘
└───────────────────────┘                      │ REST
                                     ┌─────────▼─────────┐
                                     │ Web / APP client  │
                                     │ timeline/friends  │
                                     └───────────────────┘
```

**Components used:** see the Proposal Form.

---