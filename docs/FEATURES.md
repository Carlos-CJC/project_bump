# Bump 偶遇徽章 · 功能说明

> 目标硬件：Arduino Nano RP2040 Connect（RP2040 + NINA-W102 蓝牙模块）
> 依赖库：ArduinoBLE ≥ 2.1.0
> 固件前置：NINA-W102 固件 ≥ 3.0.2（3.0.1 能初始化 BLE，但实测两块板
> 互相发现失败；升级 3.0.2 后双板连接自检稳定通过）

本项目是一个「偶遇」（bump）徽章原型：两台同型号设备在近距离时通过 BLE
广播互相发现，满足信号强度与停留时间条件后上报一次偶遇事件。MVP 阶段
**只做发现、判定和输出**，不建立连接、不交换业务数据、不做持久化。

## 1. 代码结构与构建环境

| 文件 | 职责 |
| --- | --- |
| `platformio.ini` | 构建环境、库依赖、串口监视配置 |
| `src/main.cpp` | 默认固件：偶遇广播 / 扫描 / 判定 |
| `src/conn_test.h` | 连接自检入口声明 |
| `src/conn_test.cpp` | 双板连接自检（外设 / 中心两种角色） |
| `docs/FEATURES.md` | 本文件 |

提供三个 PlatformIO 环境：

| 环境 | 角色 | 用途 |
| --- | --- | --- |
| `nanorp2040connect` | 默认 | 偶遇 MVP，两块板刷同一份固件 |
| `nano_a` | BLE 外设 | 连接自检，可连接广播 + GATT 服务 |
| `nano_b` | BLE 中心 | 连接自检，扫描、建连、读特征 |

---

## 2. BLE 空中协议

默认固件与连接自检的外设都会使用 16-bit Service UUID **`a5b0`**。

### 2.1 厂商数据格式

广播中的 Manufacturer Specific Data（默认固件）：

| 偏移 | 字段 | 长度 | 取值 | 说明 |
| --- | --- | --- | --- | --- |
| 0–1 | Company ID | 2 | `0xFFFF` | 测试用未分配 ID，小端存放为 `FF FF` |
| 2 | 协议版本 | 1 | `1` | 版本不匹配的帧直接丢弃 |
| 3–8 | short_id | 6 | 每次上电随机 | 匿名设备短 ID |
| 9 | flags | 1 | bit0 = 1 | bit0 表示允许加好友 |
| 10 | tx_power | 1 | `-4` | 标称发射功率 dBm（int8），尚未逐台校准 |

总计 11 字节（Company ID 2 字节 + 载荷 9 字节）。扫描端以
`Company ID + 协议版本` 双重过滤，其他 BLE 设备一律忽略。

### 2.2 匿名短 ID

- 6 字节，格式化为 12 位小写十六进制字符串（如 `3f9a01c47b2e`）。
- 熵源：`micros()` 与两个悬空模拟引脚 `A0`/`A1` 的噪声。
- **每次上电都会变化**，因此原型阶段无法跨重启识别同一设备。
- 默认固件不记录、不打印对端 MAC 地址。

---

## 3. 默认固件：偶遇检测

### 3.1 输入

| 来源 | 内容 | 用途 |
| --- | --- | --- |
| BLE 无线 | 周围同协议广播（UUID `a5b0` + 厂商数据） | 解析对端 short_id / flags / tx_power |
| BLE 控制器 | 每个广播包的 RSSI | 强弱信号判定、滑动平均 |
| 时间 | `millis()` | 时分复用、计数窗口、冷却期、运行时长 |
| USB 串口 | 无业务输入，仅用于监视 | 调试观察 |
| 电源 / 复位 | 上电或复位事件 | 生成新的 short_id，打印启动信息 |

> 注意：默认固件串口只输出日志，不接受任何控制命令。

### 3.2 输出

| 输出 | 内容 |
| --- | --- |
| BLE 广播 | 不可连接广播（`setConnectable(false)`），包含 Service UUID `a5b0` 与 §2.1 厂商数据 |
| USB 串口 | `[boot]`、`[status]`、`[scan]` 调试日志，以及偶遇事件 JSON |

### 3.3 工作流程

1. **启动**：等待 USB 串口最多 2s；生成匿名 short_id；初始化 BLE；
   设置本地名 `Bump`、广播 Service UUID `a5b0`、不可连接、广播间隔 100
   （HCI 单位 0.625ms，约 62.5ms）；打印启动参数；再随机延时 0–15s
   （一个完整广播+扫描周期）错开初始相位，然后进入广播阶段。两块板
   同时上电且周期相近时，相位会长期锁定、互相听不到，必须随机错开。
2. **时分复用**：NINA 模块无法稳定地同时广播与扫描（扫描期间调用
   `advertise()` 会返回成功但实际不发射），因此 **广播 6s ↔ 扫描 5s**
   交替，每次切换附加 ±1.5s 随机抖动，避免两台设备长期同相而互相错过。
   两个方向的切换都必须重启协议栈：扫描→广播时直接重开会卡死（SPI 层
   无超时）；广播→扫描时 `stopAdvertise()` 后直接扫描虽然返回成功，
   控制器却几乎不上报对端广播。切换时 `BLE.end()`、重试 `BLE.begin()`
   （首次通常失败，需带延时重试）并重新配置广播参数；单次重启 2–15s
   不等，这段时间既不广播也不扫描。
3. **扫描阶段**：扫描全部设备（不使用库的 UUID 过滤——实测 NINA 3.0.2
   下 `scanForUuid` 会漏掉对端广播）。不可连接广播（本机广播类型）
   通过 `BLEDiscovered` 事件回调逐条接收——GAP 对该类型会绕过按地址
   去重的发现队列，这是实测唯一能持续收到对端的方式；同时排空
   `BLE.available()` 队列以兼容可连接广播。两种来源都按 Company ID
   与协议版本过滤，解析 short_id / flags / tx_power，连同 RSSI 交给
   偶遇判定。
4. **偶遇判定**：见 §3.4。
5. **状态输出**：每 10s 打印一次 `[status]`。

### 3.4 偶遇判定规则

- 只统计**强信号样本**（RSSI ≥ -70 dBm），弱信号只更新最近可见时间，
  避免人体遮挡造成的瞬时跳变。
- 距上次收到该 peer 超过 **300s** 则重置其计数窗口。
- 从首个强信号样本起 **180s 窗口**内：
  - 强信号样本数 ≥ **2**（NINA 对端广播的实际到达率极低，双板互测
    中同一 peer 约每 80–90s 才被看到一次，所以必须跨扫描会话、跨分钟
    累积样本）；
  - 停留时间 ≥ **3s**；
  - 平均 RSSI ≥ **-70 dBm**；
  - 同一 peer 处于 **8 小时冷却期**之外。
- 四个条件同时满足时上报一次偶遇事件，并刷新冷却时间。

参数集中在 `src/main.cpp` 顶部，可直接调整：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `RSSI_THRESHOLD` | -70 dBm | 强信号门限 |
| `MIN_SAMPLES` | 2 | 窗口内所需样本数 |
| `DWELL_MS` | 3000 ms | 最短停留时间 |
| `SAMPLE_WINDOW_MS` | 180000 ms | 样本计数窗口 |
| `PEER_RESET_MS` | 300000 ms | 超过该间隔未见则重置 |
| `COOLDOWN_MS` | 8 小时 | 同一 peer 去重冷却 |
| `ADV_MS` | 6000 ms | 每轮广播时长 |
| `SCAN_MS` | 5000 ms | 每轮扫描时长 |
| `PHASE_JITTER_MS` | ±1500 ms | 阶段随机抖动 |
| `MAX_PEERS` | 16 | 固定容量 peer 表（LRU 淘汰） |

### 3.5 串口输出格式

启动信息：

```
[boot] Bump BLE MVP: self=3f9a01c47b2e uuid=a5b0 rssi_thr=-70 min_samples=2 dwell=3000ms adv=6000ms scan=5000ms
```

周期性状态（每 10s）：

```
[status] phase=adv self=3f9a01c47b2e peers=1 uptime=42s
```

扫描调试（全局限流 500ms 一条）：

```
[scan] peer=8c21d0a9e4f5 rssi=-63 avg=-65 n=1 flags=0x01 tx=-4
```

偶遇事件（一行一条 JSON）：

```
{"event":"encounter","peer":"8c21d0a9e4f5","rssi_avg":-65,"rssi_last":-63,"duration_s":7,"samples":2,"flags":1,"uptime_s":57}
```

| JSON 字段 | 含义 |
| --- | --- |
| `event` | 固定为 `encounter` |
| `peer` | 对端 12 位十六进制短 ID |
| `rssi_avg` | 强信号样本的指数滑动平均 |
| `rssi_last` | 最近一次样本 RSSI |
| `duration_s` | 本次停留时长（整数秒） |
| `samples` | 当前窗口内强信号样本数 |
| `flags` | 对端广播的 flags |
| `uptime_s` | 本机开机后的相对时间（秒） |

### 3.6 Peer 表

固定 16 个槽位，按 short_id 线性查找；槽位满时淘汰「最久未见」的条目。
偶遇上报时间 `lastEventAt` 在窗口重置时有意保留，使 8 小时去重在短暂
断连后仍然生效。

---

## 4. 双板连接自检

偶遇 MVP 使用不可连接广播，无法回答「两块板能否建立连接」。自检模式
单独验证 **BLE 建连 → 服务发现 → 特征读取** 这条链路，需要给两块板
烧录不同角色的固件。

两端约定：

- Service UUID：`a5b0`
- Characteristic UUID：`a5b1`（读写字符串，初始值 `BUMP`，容量 16 字节）

### 4.1 `nano_a`：可连接外设

**输入**
- BLE 连接请求（中心发起）
- GATT 特征写入（可选，手机调试也会触发）

**输出**
- 可连接广播：本地名 `Bump-Test-A`，广播 Service UUID `a5b0`
- GATT 服务 `a5b0` / 特征 `a5b1`
- 串口日志：

```
[test] role=peripheral name=Bump-Test-A service=a5b0 char=a5b1 waiting for central
[test] central connected
[test] characteristic written, value='...' len=N
[test] central disconnected, advertising again
```

中心断开后 ArduinoBLE 不会自动恢复广播，固件会显式重新调用
`BLE.advertise()`。

### 4.2 `nano_b`：中心

**输入**
- 周围广播包（按 Service UUID `a5b0` 过滤）
- 连接后的 GATT 属性与特征值

**输出**
- BLE 连接、属性发现、特征读取
- 串口日志与最终判定：

```
[test] role=central scanning service=a5b0 char=a5b1
[test] scanning for a5b0 ...
[test] found name='Bump-Test-A' rssi=-52
[test] connected, discovering attributes ...
[test] read len=4 value='BUMP'
[test] disconnected
[test] RESULT=PASS, retry in 5s
```

中心的行为循环：重启 BLE 栈 → 扫描（单会话 4s）→ 发现目标后再次
重启协议栈 → 连接 → 发现属性 → 读取 `a5b1` → 保持连接 1s → 主动断开 →
**每 5s 重试一次**。单会话未发现目标时同样重启协议栈重新扫描。

之所以每轮扫描前都重启协议栈：NINA 在一个扫描会话内对同一地址只上报
一次，丢失的 scan response 不会补报；而扫描结束后直接调用
`BLE.stopScan()` 会让 SPI 命令永远等不到响应。重启协议栈（
`BLE.end()` + 重试 `BLE.begin()`，首次通常失败）同时解决这两个问题。
`BLEDevice` 内部保存的是地址副本，重启后仍然可以发起连接。

### 4.3 `RESULT=PASS` 判定

以下全部成立才输出 `PASS`，否则 `FAIL`：

1. 成功建立 BLE 连接；
2. 属性发现成功；
3. 找到特征 `a5b1` 且可读；
4. 读到的值长度为 4 且内容为 `BUMP`。

---

## 5. 构建、烧录与运行

```bash
# 查看已连接的开发板串口
pio device list

# 默认偶遇固件（两块板都刷这份）
pio run -e nanorp2040connect -t upload --upload-port /dev/cu.usbmodemXXXX

# 双板连接自检：A 板刷外设，B 板刷中心
pio run -e nano_a -t upload --upload-port /dev/cu.usbmodemXXXX
pio run -e nano_b -t upload --upload-port /dev/cu.usbmodemYYYY

# 串口监视（115200，带时间戳）
pio device monitor -p /dev/cu.usbmodemXXXX
pio device monitor -p /dev/cu.usbmodemYYYY
```

两块板同时接在电脑上时必须用 `--upload-port` 明确指定目标板，避免烧错。

若 `BLE.begin()` 报错，先确认 NINA-W102 固件版本（需 ≥ 3.0.2），
可用 Arduino IDE 的 WiFiNINA 固件更新器升级。

---

## 6. 已知限制与后续方向

- **MVP 不建立连接**：偶遇只依赖广播，降低复杂度与功耗；正式协议若需
  交换数据，可复用自检模式的 GATT 通道。
- **无持久化、无上传、无 NTP**：时间均为开机后的相对时间，重启后清零。
- **short_id 每次上电随机**：无法跨重启识别设备；后续再做按天轮换 +
  服务端映射。
- **NINA 无法同时广播与扫描**：扫描期间调用 `advertise()` 返回成功，
  控制器却根本不发射广播。因此只能时分复用（6s/5s，±1.5s 抖动），且
  两个方向的切换都要重启 BLE 栈；单次重启 2–15s 不等，这段时间既不
  广播也不扫描，两台设备可能连续错过数秒。周期再缩短会令重启失败，
  不做进一步压缩。
- **扫描上报稀疏且去重**：GAP 发现队列在每个扫描会话内对同一地址至多
  上报一次，而 NINA 对对端广播的实际接收率本就很低（双板互测中同一
  peer 约每 80–90s 才上报 1 条）。默认固件用不可连接广播，因此走
  `BLEDiscovered` 事件回调——GAP 对不可连接广播（report type 0x03）
  会逐条回调、绕过去重队列，这是实测唯一能持续收到对端的方式；队列
  轮询保留用于兼容可连接广播。即便如此样本仍稀少，判定阈值相应降为 2，
  计数窗口放宽到 180s。
- **Peer 表容量 16**：密集环境下 LRU 淘汰可能丢失旧 peer 的冷却信息。
- **连接自检与默认固件共用 UUID `a5b0`**：只有可连接的外设（`nano_a`）
  能完成自检；默认固件的广播不可连接。
- **NINA 固件关系（3.0.1 → 3.0.2）**：
  - 3.0.1 下两块板相距约 1m、双向角色互换时双方都收不到对方广播，
    而电脑 BLE 适配器能稳定收到两块板（各板也能扫到周围其余 18 个
    BLE 设备），排除距离与单板故障，判断为固件扫描/上报缺陷。
  - 升级 3.0.2 后恢复，双板连接自检可稳定输出 `RESULT=PASS`；
    `BLE.begin()` 重启后首次调用常失败并打印 "initialization ... failed"，
    属库自身的重试提示，不是版本问题。
  - 遗留：`withDuplicates` 在每个扫描会话内对同一地址至多上报一次，
    发现过程仍间歇（部分 4s 会话完全发现不到目标），因此自检平均要
    几十秒才进入一次连接；软件侧对策是每轮扫描前重启协议栈（见 §4.2）。
  - `scanForUuid` 的 UUID 过滤同样不可靠：启用过滤后扫描窗口内一个设备
    都收不到，去掉过滤后同一窗口能看到 11–19 个设备。因此默认固件改为
    扫描全部设备、在应用层校验厂商数据。
- **隐私**：MVP 不打印对端 MAC，仅使用匿名 short_id。
