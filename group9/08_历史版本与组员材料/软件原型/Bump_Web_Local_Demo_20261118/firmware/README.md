# Bump 固件框架：无硬件开发版

本目录为 Nano RP2040 Connect 准备固件基础。默认是 `BUMP_MOCK_IO=1`：
动作、广播接收和应用 ACK 由脚本或串口注入；事件固定标记
`source="simulated"`、`time_quality="unknown"`。它不会连接服务器，也不会
自动把串口输出视作服务器已保存。完整的本机 MQTT/TLS/网页演示请按项目根目录
README 运行 Python 模拟器。

## 已实现与边界

| 内容 | 本版本状态 |
|---|---|
| IDLE / DISCOVERY / SYNC / PAUSED | 已写 portable C++ 核心；60 秒窗口不会被重复摇动延长 |
| 事件队列 | 128 条 RAM；满时拒绝新条目并累计 overflow，不覆盖待确认事件 |
| ACK / 重传 | 检查版本、device_id、boot_id、seq、stored/duplicate；未发送或错误 ACK 不删除；先确认订阅，再允许发送 |
| 重试 | 前三次间隔 5 秒，之后 30 秒；SYNC 三次后回待机；待机和暂停时继续处理旧队列 |
| BLE 数据格式 | 已写 29 字节构造及严格解析；错误 UUID、版本、状态、长度、化名及自身广播均拒绝 |
| 候选聚合 | 两设备 MVP；至少 3 样本、停留至少 5 秒、RSSI 中位数至少 −70 dBm；间隔 15 秒另起一段 |
| 摇动识别 | 加速度模长减缓变重力基线；双峰 150–800 ms、2 秒冷却；忽略重复时间戳和无效数值 |
| IMU API | `BUMP_MOCK_IO=0` 使用 Arduino_LSM6DSOX 的真实 begin/available/read API；仅编译核对，传感器采样未实测 |
| 按钮 / 灯光 | 可选 GPIO 输入含 40 ms 消抖，默认不启用按钮；LED_BUILTIN 为简单状态节奏；未接线、未实测 |
| 无线 / 同步时钟 | ArduinoBLE、WiFiNINA、ArduinoMqttClient、NINA 证书、UTC 校时尚未接入 |
| 断电持久化 | 未实现；重新启动 RAM 队列为空 |

`BumpCore.h` 不依赖 Arduino API，主机测试和 Arduino 草图使用同一份逻辑。
`test_core.cpp` 是实际执行的 C++ 回归测试，Python 测试还会把固件产生的 JSON
交给服务器验证器检查。

本次实际验证（2026-10-23）：

| 检查 | 结果 |
|---|---|
| Arduino CLI 1.5.1 / Mbed Nano core 4.6.0 / 默认 mock 模式 | 编译通过；程序 98,641 bytes，静态 RAM 51,148 bytes |
| 同板型 / `BUMP_MOCK_IO=0` / Arduino_LSM6DSOX 1.1.2 | 编译通过；程序 106,418 bytes，静态 RAM 51,944 bytes |
| Zig 0.14.1 编译并实际运行同一 C++ 核心 | 通过广播、ACK/重试、满队列/重启、窗口/暂停、停留/中位数、摇动/时钟溢出 6 组回归断言 |
| 固件 JSON 交给实际服务器验证器 | 通过；保持 simulated、unknown、UTC=null |
| `pytest tests/test_firmware_core.py` | 2 passed；没有跳过 |
| 开发板上传、IMU 实测、BLE、MQTT、TLS、LED/按钮实测 | 均未执行 |

### 有意保留的简化

- 暂停立即停止发现，并丢弃当前尚未冻结的候选段；已经入队的记录继续补传。
  恢复只回待机，需要再次摇动才开窗。
- RSSI 使用完整整数直方图求中位数；偶数样本取两个中间值平均并向零取整。
  `sample_count` 与中位数使用同一批样本。
- 仅接收当前布局恰好 29 字节的广播。不自动容忍无线库增加名字、UUID 列表或
  扫描响应；拿到板后应核对真实报文，再决定是否需要解析通用 AD 列表。
- 核心的身份字符串必须在该次运行中保持不变。串口草图使用显式占位
  `serial-only-no-production-boot-id`，因为它没有真实上行；不能把此值复制进
  生产上传。实际网络适配必须在每次启动生成新的 boot_id，并保持到重启。
- 默认 GPIO 没有指定，避免猜测接线；确认引脚后用 `BUMP_PAUSE_PIN` 设置，
  接线方式为该 GPIO 到 GND，使用 INPUT_PULLUP。
- 这些阈值用于软件初值，尚无实际佩戴、误触、距离或 RSSI 标定证据。

## 编译

板卡：`arduino:mbed_nano:nanorp2040connect`。本次使用 Mbed Nano core 4.6.0；
真实 IMU API 编译另需 Arduino_LSM6DSOX 1.1.2。默认模式不依赖 ArduinoBLE、
WiFiNINA 或 ArduinoMqttClient。

在项目根目录打开 PowerShell，已经安装 Arduino CLI 和板卡包时：

```powershell
.\firmware\compile.ps1
.\firmware\compile.ps1 -RealImu
```

如使用已有隔离工具链，显式提供路径：

```powershell
.\firmware\compile.ps1 `
  -ArduinoCli "完整路径\arduino-cli.exe" `
  -ArduinoConfig "完整路径\arduino-cli.yaml"
```

第二种模式加 `-RealImu`。编译脚本仅生成本项目中的 build、binaries 和日志，
不会上传到开发板；不修改所复用的 Lab 草图或配置文件。真正上传前必须先确认
板型、端口和实际接线。

## 运行核心测试

```powershell
# 先按根目录 README 配置 Python 环境
.\.venv\Scripts\python.exe -m pytest tests/test_firmware_core.py -v
```

需要 C++11 宿主编译器。测试自动寻找 g++ 或 clang++，或读取 `BUMP_CXX`
所指定的可执行文件。Zig 也可用：

```powershell
$env:BUMP_CXX = "完整路径\zig.exe"
.\.venv\Scripts\python.exe -m pytest tests/test_firmware_core.py -v
```

缺少编译器会明确报告 skipped，而不是通过。交付包不包含大型开发工具链。
本次编译及测试证据见 `evidence/firmware/`，不应将编译通过写成实板运行通过。

## 拿到开发板后的串口自检

本次没有实物，因此以下是待执行步骤：

1. 上传默认 mock 草图，串口速率设为 115200；拔开串口监视器后应仍能运行。
2. 发送 `s` 模拟已识别的摇动；发送 `r` 注入一个 B00002 的模拟广播。
   在至少 5 秒内发 3 次或更多 `r`，等待本次 60 秒窗口结束。
3. 观察 `SIMULATED_SEND` 行。相同事件每次重传内容必须一致。
4. `x` 注入错误 boot_id ACK，队列必须保留；`a` 注入 stored ACK，
   或 `d` 注入 duplicate ACK，才能删除已经发送的队首。
5. `o` 切换模拟离线/在线，`p` 暂停/恢复。暂停时 `s` 和 `r` 无效，
   原有队列可继续重传；恢复后需再次 `s`。
6. `i` 注入两峰加速度序列，经过同一 ShakeDetector 开窗。该输入不是传感器证据。
7. 可选真实 IMU 模式只替换加速度来源；无线广播和 ACK 仍是模拟输入，
   记录仍标记 simulated。

默认 LED 节奏：发现每 200 ms 翻转，暂停每 1000 ms 翻转，有积压每 500 ms
翻转，空闲熄灭。它是可编译的初步提示，不代表已经验证 NINA/RGB 共存，
也不会表示对方已经同意联系。

## 广播接口

项目 UUID：`965b0120-8aaa-4e72-9a37-6e208731ba01`。

| 偏移 | 字节 | 含义 |
|---|---|---|
| 0–2 | `02 01 06` | Flags AD |
| 3–4 | `19 21` | 25-byte AD length；128-bit Service Data |
| 5–20 | `01 ba 31 87 20 6e 37 9a 72 4e aa 8a 20 01 5b 96` | UUID，小端 wire order |
| 21 | `01` | 版本 1 |
| 22 | `01` | DISCOVERY；其他状态不广播 |
| 23–28 | 例如 `42 30 30 30 30 32` | 6 个 ASCII 大写字母/数字，示例 B00002 |

两个设备只改变 device_id 和化名 A00001/B00002；会话与服务器映射保持一致。
服务 UUID 是协议标识，不是身份认证。广播化名和 RSSI 无法阻止伪造邻近记录，
不能据此宣称防冒充或精确距离测量。

## 真实硬件接入清单

1. **boot_id / 时间**：实现每次启动的新 ID；NTP 校时并保存 UTC 与 uptime 的
   映射；未知时间保持 null，跨设备不得用各自 uptime 互相比对。
2. **BLE 发送/接收**：接入真实 ArduinoBLE；重复扫描、实际原始字节、广播与
   扫描同时或交替工作、RSSI 和丢包率必须由双机日志证实。
3. **MQTT / TLS**：接入 WiFiSSLClient 与 ArduinoMqttClient；配置 NINA 信任根、
   设备凭据、QoS 1 / retain=false、SUBACK、严格 ACK JSON 解析和超时；
   把完整 ACK 参数传入 `acknowledge`。禁止 PUBACK 直接清队列。
4. **服务器可达性**：本项目服务器当前仅监听 localhost。真实板卡不能直接
   连接本机回环地址；扩展为受控局域网服务时需另核对监听地址、防火墙、
   主机名证书和访问范围。
5. **真实事件来源**：只有 BLE、IMU、时钟与上行都接入后才增加 hardware 事件
   序列化路径；保留模拟与真实数据隔离。
6. **传感器与交互**：记录实际采样率、双峰阈值、冷却时间；采集主动摇动和
   走路/挂包/放桌面负样本，分开调参集与正式测试集；验证暂停响应和按钮接线。
7. **NINA 与显示共存**：记录板卡及 NINA 固件版本；验证发现→上传→重新发现
   20 轮，核对 LED_BUILTIN/RGB 与无线是否冲突。
8. **可靠性与供电**：5 分钟断网、掉 ACK、队列满、重启、未知时钟、无串口启动；
   如实记录 RAM 断电丢失，并实测电流、续航、外壳和佩戴效果。

## 官方接口依据

- [Nano RP2040 Connect 官方硬件页](https://docs.arduino.cc/hardware/nano-rp2040-connect/)
- [Arduino_LSM6DSOX 官方加速度示例](https://github.com/arduino-libraries/Arduino_LSM6DSOX/blob/main/examples/SimpleAccelerometer/SimpleAccelerometer.ino)

本地实际编译使用上述已安装版本；外部最新版本不等于本次验证版本。
