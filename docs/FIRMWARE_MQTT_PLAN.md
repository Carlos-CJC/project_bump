# Bump 固件 MQTT 接入方案

> 状态：实施计划（本轮不写代码）
> 接口契约见 [INTERFACES.md](INTERFACES.md) §1–§3
> 现有固件行为见 [FEATURES.md](FEATURES.md) §3

本文描述如何把偶遇事件从串口 JSON 扩展到 Wi-Fi + MQTT 上报。协议字段、
主题与错误处理已经由接口文档固定；本文只讲固件怎么做。

---

## 1. 目标与范围

- 每次偶遇判定成立后，把事件放入内存队列，并在下一个 TDM 阶段边界尝试上传。
- 上传成功后从队列移除；失败保留并退避重试（重启丢失待上传事件，当前可接受）。
- 保留现有串口 `[boot]`/`[status]`/`[scan]`/偶遇 JSON 输出不变，便于既有工具对照。
- 不在本轮实现：本地持久化、NTP 校时、config 下行、应用层 ack。

## 2. 关键约束

| 约束 | 影响 |
| --- | --- |
| NINA 模块 Wi-Fi 与 BLE 共用射频，不能同时工作 | 每次上传必须 `BLE.end()` → 上传 → 恢复 BLE；上传期间无法发现对端 |
| BLE 栈重启需 2–15 s，`BLE.begin()` 首次常失败 | 上传窗口结束后复用现有 `enterAdvPhase()` 重试循环 |
| 设备无 RTC / NTP | 只上报 `uptime_ms` 相对时间，服务端反推绝对时间 |
| RP2040 RAM 有限，newlib-nano 不链接 `%f` | 手写 JSON、固定缓冲，只用 `%d/%u/%lu/%s` |
| WiFiNINA 无 `setCACert`/`setInsecure` | 云端证书必须链到模块内置公共根证书（Let's Encrypt） |

## 3. 构建环境与依赖

在 `platformio.ini` 新增：

```ini
[env:mqtt]
extends = env:nanorp2040connect
lib_deps =
  arduino-libraries/ArduinoBLE@^2.1.0
  arduino-libraries/WiFiNINA@^2.0.0   ; 1.9.x 与 Arduino_SpiNINA 重复定义 spi_drv
  arduino-libraries/ArduinoMqttClient@^0.1.8
build_src_filter = +<*> -<wifi_test.cpp>
build_flags =
  -D BUMP_MQTT=1
  -D BUMP_FW_VERSION=\"0.4.0\"
  -D BUMP_WIFI_SSID=\"...\"
  -D BUMP_WIFI_PASS=\"...\"
  -D BUMP_MQTT_HOST=\"mqtt.example.com\"
  -D BUMP_MQTT_PORT=8883
  -D BUMP_MQTT_PASS=\"per-board-secret\"
```

- `client_id` 与 MQTT 用户名**不是**构建参数：运行时由 `device_id`（MAC 派生）
  得出；每板只需独立 `BUMP_MQTT_PASS`。
- 两块板联调时用 `badge_a` / `badge_b` 两个 env 继承 `[env:mqtt]` 并覆盖密码。
- `build_src_filter` 排除 `wifi_test.cpp`，避免 WiFiNINA 测试入口重复定义
  `setup/loop`（该文件仅在 `[env:wifi_test]` 编译）。
- `src/conn_test.cpp` 在 `[env:mqtt]` 中仍会编译；它只在 `BUMP_TEST_ROLE`
  非零时参与 `setup/loop` 分发，与 MQTT 互斥但可共存编译。

## 4. 库选择：ArduinoMqttClient

| 维度 | ArduinoMqttClient ^0.1.8 | PubSubClient ^2.8 |
| --- | --- | --- |
| QoS 1 发布 | 是，阻塞等待 PUBACK，可设超时 | 是 |
| 载荷缓冲 | 流式 `beginMessage/write/endMessage`，无固定包缓冲 | 固定缓冲，默认 256B，主题+载荷共用 |
| LWT / clean session / keepalive | 均有 API | 均有 |
| 与 `WiFiSSLClient` | `MQTTClient(Client&)` 直接支持 | `setClient` 支持 |

批次载荷 1–2 KB，PubSubClient 需手动扩缓冲且容易踩包长上限，故选
ArduinoMqttClient。TLS 由 NINA 模块完成，RP2040 不做加解密。

## 5. 模块与文件设计

| 新文件 | 职责 |
| --- | --- |
| `src/bump_config.h` | 所有编译开关与默认参数（见 §10） |
| `src/device_identity.h/.cpp` | `deviceId`（MAC→`bump-xxxx`）、`bootId`、`nextEventId()`、`nextBatchId()` |
| `src/event_queue.h/.cpp` | 定长事件队列：入队、取批、出队、丢弃计数 |
| `src/payload_writer.h/.cpp` | 手写 JSON：偶遇批次、status 上下线 |
| `src/mqtt_uploader.h/.cpp` | 上传窗口：Wi-Fi/TLS/MQTT 连接、发布、退避调度 |

### 5.1 事件结构

```c
struct EncEvent {
  uint32_t seq;               // 入队序号，用于 event_id
  uint32_t occurredUptimeMs;  // millis()
  uint8_t  peerId[6];
  int16_t  rssiAvg, rssiLast;
  uint32_t durationS;
  uint16_t samples;
  uint8_t  flags;
  int8_t   peerTxPower;
};  // 约 28 字节

static EncEvent queue[BUMP_MQTT_QUEUE_CAP];  // 默认 16
```

- 队满时丢弃最旧一条并 `eventsDropped++`（新数据更有价值，且丢弃在 status 上报）。
- 只在收到 PUBACK 后出队；失败不动队列，重试沿用原 `event_id`。

### 5.2 身份

- `setup()` 中读取 `WiFi.macAddress(mac)`：注意这需要 `WiFi.status()` 能访问
  模块，但**不需要连上 AP**。若读 MAC 时模块未就绪，可先 `WiFi.begin()` 一次
  或使用 `WiFi.macAddress()` 重试（参考 `src/wifi_test.cpp` 的失败处理）。
- `bootId` 用与 `makeShortId()` 相同的熵源生成 32 位随机值（8 位 hex）。
- `event_id` / `batch_id` 用 `snprintf` 拼接，禁止动态分配。

## 6. 与主程序的衔接

改动点（都在 `src/main.cpp`）：

1. `setup()`：
   - `identityInit()`（MAC、bootId）。
   - 启动日志追加 `dev=... boot=... fw=...`。
2. `emitEncounter()`：保持现有串口 JSON 输出不变，在其后调用
   `queuePush(...)` 把同一份字段入队。
3. `loop()` 的 TDM 切换处（现有 `enterScanPhase()` / `enterAdvPhase()` 调用
   前后）：在阶段边界调用 `uploadMaybe(now)`。**不在扫描或广播中途上传**，
   避免额外的栈重启。
4. `printStatus()`：追加 `q=<队列深度> drop=<丢弃计数>`。

时序含义：偶遇在扫描阶段被发现，最迟约 `SCAN_MS + jitter`（≤6.5 s）后的
边界开始上传，符合「立即上报」语义，同时不打断当前阶段。

## 7. 上传窗口时序

```text
uploadMaybe():
  if (!uploadDue || now < nextUploadAt) return
  uploadDue = false
  runUploadWindow()

runUploadWindow():
  BLE.end(); delay(500)
  if (!wifiConnect(SSID, PASS, BUMP_WIFI_TIMEOUT_MS))      -> fail("wifi")
  WiFiSSLClient ssl;
  MQTTClient mqtt(ssl);
  mqtt.setID(deviceId);
  mqtt.setCleanSession(true);
  mqtt.setKeepAlive(60);
  mqtt.setTimeout(BUMP_MQTT_TIMEOUT_MS);       // 10 s
  mqtt.setWill(statusTopic, offlinePayload(), true, 1);
  if (!mqtt.begin(HOST, PORT, deviceId, PASS))             -> fail("mqtt")
  publishStatus(mqtt, "online");
  while (queueCount() > 0 && withinBudget()) {
    n = min(queueCount(), BATCH_MAX);
    len = buildEncounterBatch(buf, sizeof(buf), identity, events, n);
    if (!len)                                              -> fail("payload")
    if (!mqtt.publish(encTopic, buf, false, 1))            -> fail("publish")
    queueDrop(n);                                           // 仅在 PUBACK 后
  }
  publishStatus(mqtt, "offline");
  mqtt.stop();
  WiFi.end(); delay(500)
  enterAdvPhase();                              // 现有 BLE 恢复路径
  return

fail(stage):
  记录日志，按 §9 设置退避
  WiFi.end(); delay(500)
  enterAdvPhase()
```

- 整窗预算 `BUMP_UPLOAD_BUDGET_MS`（默认 40 s），超时立即结束窗口。
- 每次窗口只尝试一次连接，不做内部重连，保证检测黑洞有上界。
- 上传结束恢复到**广播阶段**（现有已验证的重启路径），随后 TDM 正常继续。
- `mqtt.publish()` 返回 true 表示已收到 PUBACK（broker 已接收，不代表后端
  入库）；接口文档已将重复投递定义为正常情况。

## 8. JSON 生成

- `payload_writer.cpp` 提供：

```c
size_t buildEncounterBatch(char* buf, size_t cap,
                           const BatchMeta& meta,
                           const EncEvent* ev, size_t n);
size_t buildStatus(char* buf, size_t cap, const char* state, uint32_t uptimeS);
```

- 固定 `char payload[1792]`；`snprintf` 返回值达到或超过 `cap` 视为溢出，
  拒绝发布并记录 `[mqtt] payload overflow`。
- 所有字段均为受控数字或已格式化 hex，无需 JSON 转义。
- 不使用 ArduinoJson（省 10–20 KB Flash 与动态分配风险）。

## 9. 重试与退避

- `consecutiveFailures` 失败计数，退避：
  `nextUploadAt = now + min(300000, 10000 × 2^(failures−1)) + random(0, 5000)`。
- 成功清零计数与 `nextUploadAt`。
- 队列非空但无新事件时，也按 `nextUploadAt` 定期重试（成功后退避为 10 分钟）。
- Wi-Fi 连接失败、TLS 握手失败、CONNACK 失败、发布超时、载荷溢出走同一路径，
  日志区分 `stage=`。

## 10. 配置与密钥

| 宏 | 默认值 | 说明 |
| --- | --- | --- |
| `BUMP_MQTT` | 0 | 编译 MQTT 集成 |
| `BUMP_FW_VERSION` | `"dev"` | status / 批次中的 `fw` |
| `BUMP_WIFI_SSID/PASS` | 无 | Wi-Fi 凭据 |
| `BUMP_MQTT_HOST/PORT` | 无 / 8883 | broker 地址 |
| `BUMP_MQTT_PASS` | 无 | 每设备密码 |
| `BUMP_MQTT_QUEUE_CAP` | 16 | 队列容量 |
| `BUMP_MQTT_BATCH_MAX` | 8 | 单批事件数 |
| `BUMP_UPLOAD_BUDGET_MS` | 40000 | 上传窗口上限 |
| `BUMP_WIFI_TIMEOUT_MS` | 20000 | Wi-Fi 连接上限 |
| `BUMP_MQTT_TIMEOUT_MS` | 10000 | MQTT socket 超时 |
| `BUMP_UPLOAD_MIN_INTERVAL_MS` | 0 | 0 = 立即；>0 = 合并窗口 |
| `BUMP_NTP` | 0 | 预留：上传窗口内 SNTP |
| `BUMP_MQTT_CONFIG` | 0 | 预留：订阅 config |

密钥管理：

- 真实凭据不提交仓库。新增 `.gitignore` 条目 `secrets.ini`，提交
  `secrets.ini.example`；可选 `scripts/load_secrets.py` 作为 PlatformIO
  pre-script，把 `secrets.ini` 的键值注入 `-D` 构建参数。
- 原型阶段也可用 `badge_a` / `badge_b` 两个 env 直接覆盖密码。
- Wi-Fi 密码同理；凭据一旦进 git 历史需要轮换。

## 11. 日志

新增 `[mqtt]` 前缀（沿用 `logf`）：

```text
[mqtt] session reason=encounter queued=1 fails=0
[mqtt] wifi ok ip=172.20.10.9 rssi=-58
[mqtt] mqtt connected
[mqtt] tx batch=bt_..._00000007_0001 n=1 bytes=421
[mqtt] tx ok batch=... rtt=183ms
[mqtt] session failed stage=tls queued=1 backoff=10s
[mqtt] queue full, dropped oldest (total=3)
[mqtt] payload overflow cap=1792 n=8
```

`[status]` 行追加 `q=` / `drop=`。串口偶遇 JSON 行保持不变。

## 12. 实施步骤

1. `[env:mqtt]` + 密钥注入方式；先编译通过，确认 RAM/Flash 占用。
2. `device_identity`：打印 `dev`/`boot`，验证与 WiFi MAC 一致。
3. `event_queue` + `payload_writer`：单元式验证，用串口打印生成的 JSON，
   与 `mosquitto_pub` 手工消息对照。
4. `mqtt_uploader`：先只发 status，再发单事件批次。
5. 接入 TDM 边界与 BLE 恢复；验证上传后偶遇检测能继续工作。
6. 双板联调：A/B 互遇 → 两台上报 → 服务端互遇判定。
7. 异常演练：broker 停止、Wi-Fi 密码错误、队列溢出、掉电重启。

## 13. 验证要点

- 设备串口按 §11 顺序出现 `wifi ok` → `mqtt connected` → `tx ok` → `ble resume`。
- `mosquitto_sub` 收到 `encounters` 与 `status`，`self_short_id` 与对端
  `peer_short_id` 对应。
- 重复发布同一批次两次，后端只入库一次（`event_id` 去重）。
- 断网 5 分钟后恢复：队列保留、退避重试、最终补发。
- 上传期间另一块板持续广播，上传结束后本板能重新发现它（TDM 恢复）。
- 每次上传导致 10–55 s 检测黑洞：双板测试期间需接受偶遇率下降；
  密集场景建议将 `BUMP_UPLOAD_MIN_INTERVAL_MS` 调到 30000 做合并上报。

## 14. 风险与后续

- **检测黑洞**：BLE 重启 2–15 s + 上传 5–40 s，每次上报期间无法发现对端。
  缓解方式：合并窗口（`BUMP_UPLOAD_MIN_INTERVAL_MS`）、缩短 TLS 会话、
  后续考虑 Wi-Fi/BLE 并发（需实测 NINA 3.0.2 是否支持）。
- **PUBACK ≠ 入库**：后端需持久订阅；设备侧无法感知后端故障。
- **TLS 根证书**：私签证书无法通过 NINA 校验；部署前用
  `openssl s_client` 验证证书链。
- **身份隐私**：MAC 派生 ID 可被关联；后续换工厂 ID。
- **无持久化**：重启丢队列事件；需要时引入 LittleFS/Flash 环形队列。
- **时间精度**：依赖服务端反推；NTP 作为可选增强。
