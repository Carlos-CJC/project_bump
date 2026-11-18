# NINA-W102 射频切换问题排查记录

记录日期：2026-11-18
硬件：Arduino Nano RP2040 Connect ×2（NINA-W102，固件 3.0.2）
固件：`mqtt` 环境，`BUMP_FW_VERSION=0.4.0`

本文只记录 BLE / Wi-Fi 共用 NINA 射频时遇到的问题、已落地的缓解措施和
实测结果。接口契约见 [INTERFACES.md](INTERFACES.md)，固件方案见
[FIRMWARE_MQTT_PLAN.md](FIRMWARE_MQTT_PLAN.md)，功能说明见
[FEATURES.md](FEATURES.md)。

## 1. 硬件约束

- RP2040 通过 SPI 访问 NINA-W102 协处理器，BLE 与 Wi-Fi 都由 NINA 固件
  提供且共用射频，**不能同时工作**，只能分时切换。
- WiFiNINA 2.x 的 SPI 驱动忙等协处理器返回 ready 字节，**没有超时**：
  协处理器一旦不响应，RP2040 主循环永久阻塞，USB 串口随之失联，
  只能物理复位（或由看门狗复位）。
- NINA 3.0.2 固件对 BLE 协议栈重启的容错很差：`BLE.begin()` 经常需要
  多次重试；Wi-Fi 使用之后偶发长达数十秒的连续初始化失败。

## 2. 观测到的故障形态

| 现象 | 串口特征 | 根因 |
| --- | --- | --- |
| Wi-Fi 连不上 | `[mqtt] session failed stage=wifi`，`WiFi.status()==255`（WL_NO_MODULE） | BLE 刚关闭，首次 `WiFi.begin()` 停在 WL_IDLE 直至超时 |
| TLS 握手失败 | `[mqtt] connect failed err=-2` | 同一原因，射频/协议栈残留状态导致握手失败 |
| BLE 起不来 | `The initialization of the Bluetooth® Low Energy module failed.` 连续刷屏 | Wi-Fi 使用后 NINA 拒绝初始化 BLE，最长可持续 90s+ |
| 整机卡死 | 串口完全静默，端口 `Device not configured` | SPI 等待无超时的死锁，只能复位 |

触发规律：切换越频繁、Wi-Fi 信号越差，失败概率越高；失败窗口本身还会
触发下一轮切换，形成恶性循环。

## 3. 已落地的缓解措施

| 措施 | 参数 | 说明 |
| --- | --- | --- |
| BLE 关闭后静置 | `delay(3000)` | 给 NINA 释放射频留时间 |
| Wi-Fi 建连重建重试 | 单次等待 `BUMP_WIFI_TIMEOUT_MS/2`（10s），失败后 `WiFi.end()` + 2s 再试一次 | 解决 WL_NO_MODULE；实测第二次通常在 3–8s 连上 |
| MQTT/TLS 建连重试 | 2 次，间隔 1s | BLE 刚释放射频时第一次握手常失败 |
| 硬件看门狗 | `BUMP_WDT_TIMEOUT_MS`，默认 16s | 主循环、BLE 重启循环、Wi-Fi 轮询、上传等待均喂狗；真死锁 16s 后自动复位 |
| BLE 重启失败保护 | 连续 30 次失败 | 记录 `[ble] restart failed 30 times, rebooting` 后 `NVIC_SystemReset()` |
| 启动阶段提前喂狗 | setup 开头 start/kick | RP2040 软复位后看门狗仍在倒计时，提前重置避免复位后约 3s 被二次打断 |

## 4. 公网 broker 与证书

测试目标为公开测试 broker（匿名、无隔离，仅课程演示）：

| Broker | NINA 3.0.2（内置根证书） |
| --- | --- |
| `broker.hivemq.com:8883` | **可用**（Amazon 根证书链） |
| `broker.emqx.io:8883` | 不可用（RapidSSL 链校验失败，`err=-2`） |
| `test.mosquitto.org:8883` | 不可用（自签 CA） |

正式部署应换自有服务器 + Let's Encrypt 证书，并使用每设备独立凭证。

## 5. 实测结果（2026-11-18，oppo2 热点）

- `wifi_test` 扫描：`oppo2` 信道 6、RSSI -35，`WiFi.begin` 3.4s 连接成功
  （证明凭据与 2.4GHz 频段正常）。
- `mqtt` 全链路成功（板 `bump-ec626080e83c`）：
  `wifi ok rssi=-31 → mqtt connected → tx ok → session done queued=0`，
  broker 收到 `status online` → `encounters`（peer 短 ID、rssi_avg、duration）
  → `status offline`，队列清空。
- 监视器实测：BLE 初始化风暴每 1–2 分钟出现一次，持续 30–60s，
  之后由协议栈自愈、30 次失败保护或看门狗恢复，不再永久挂死。

## 6. 已知代价与功能影响

复位会丢失内存状态（事件队列、8 小时冷却、peer 表、短 ID），具体影响：

1. **检测空窗**：风暴与复位期间无广播/扫描，短时擦肩可能漏判。
2. **重复上报**：冷却丢失，同一 peer 复位后可能再次上报；服务端按
   `event_id` 幂等，而复位后 `boot_id` 变化会产生新的 `event_id`，
   库里可能出现重复偶遇记录。
3. **未上传事件丢失**：队列最多 16 条，复位时未上传的偶遇全部丢失。
4. **数据连续性**：`boot_id`/`short_id` 频繁变化、`uptime` 反复归零，
   但不影响单条事件的时间换算与字段契约。

当前决策（MVP 阶段）：保持现状，不做持久化。

## 7. 后续可选优化

- 放长 TDM 周期（如广播 15s / 扫描 12s），减少协议栈重启次数。
- 调大 BLE 重启失败阈值（当前 30 次），减少自动复位频率。
- 将冷却（`lastEventAt`）与队列持久化到 flash/NVS，消除重复上报与丢事件；
  需权衡写寿命与实现复杂度。
- 升级 NINA 固件后重新评估上述问题是否缓解。
