# 近场配对操作逻辑设计

> 适用硬件：Arduino Nano RP2040 Connect × 2，接线见 [HARDWARE.md](HARDWARE.md)。
> 对应固件：[firmware/nearfield_pairing/nearfield_pairing.ino](firmware/nearfield_pairing/nearfield_pairing.ino)
> 云端/App 交接：[HANDOFF_CLOUD_APP.md](HANDOFF_CLOUD_APP.md)

## 0. 先回答两个关键问题

### Q1：接收器会收到自己的信号，是 PWM 交替收发，还是过滤自身信号就行？

**过滤自身 ID 只能作为最后一道保险，不能作为主要机制。** 正确做法是 **半双工 + 随机时隙**，不是固定占空比的 PWM 交替。

原因有三个：

1. **本地强光会让接收头饱和。** 发射管和接收头在同一块板上，接收头一定会收到直射、反射或外壳漏出的本地红外光。HX1838 内部有 AGC（自动增益控制），本地强信号会把增益压得很低，发射结束后还需要几十毫秒恢复；这段时间对方的弱信号根本听不见。
2. **同时发射会互相破坏。** 两块板同时发 38 kHz 载波时，空间中的两路光叠加/拍频，接收头解调出来的包络会互相干扰，两边的帧都可能解不出来。这不是“收到自己的码”那么简单，而是双方都收不到。
3. **过滤解决不了碰撞。** 即使你把 `senderID == myID` 的帧丢掉，自己的发射仍然占用了空中时间；如果双方同时发，对方的帧在物理层就已经坏了。

所以本设计采用：

- **发之前先听**（listen-before-talk）：检查 D4 上是否有 >250 µs 的低电平（说明空中有 38 kHz 帧），有就随机退避。
- **发完一帧立刻停接收**，等 30 ms 让 AGC 恢复，再重新开接收（`IrReceiver.stop()` / `start()`）。
- **随机时隙**：搜索间隔 260–700 ms 随机；ACK 间隔加 0–100 ms 抖动。避免两块板同时上电或同时按蓝色按钮时一直撞车。
- **ID 过滤**：`sender == myID` 的帧直接丢弃，作为兜底。
- 这里的“PWM”只是 38 kHz 载波，不是双工方式；不要用固定 PWM 占空比去交替收发。
- 也不建议用固定相位的 TDMA（例如 A 在偶数时隙发、B 在奇数时隙发）：两块板独立上电，没有共享时钟和主从角色，很容易长期同相而永远听不到对方。随机时隙不需要同步，更适合这种“两个平等设备”的场景。

> 如果想做真正的全双工，需要两个不同波长 + 滤光片，或者改用射频/有线；当前这颗 38 kHz 接收头做不到。

### Q2：如何确保两边都能收到对方信号？

核心是 **对称状态机 + 三次握手 + 重传 + 随机退避**，而不是“各自发一次，收到就算”。

1. **两块板运行同一份代码**，没有固定主机/从机，谁先听到谁就回 ACK。
2. **HELLO 广播**；**ACK / DONE 带目标机器码**，只有目标板会处理。
3. **三次握手**：
   - A 发 `HELLO`（广播自己的机器码）；
   - B 收到后发 `ACK`（带 B 的机器码，目标 = A）；
   - A 收到 `ACK`：A 确认“B 收到了我”，发 `DONE`（目标 = B），A 进入 PAIRED；
   - B 收到 `DONE`：B 确认“A 收到了我”，B 进入 PAIRED。
4. **重传**：
   - `ACK` 每 320–420 ms 重发一次，直到收到 `DONE`；
   - `DONE` 连发 3 次；
   - 已经 PAIRED 的一方如果又收到针对自己的 `ACK`，说明对方没收到 `DONE`，会补发；
   - `CONFIRM` 8 s 超时回 `SEARCH`，重新握手；
   - `SEARCH` 120 s 无人应答，红色闪 4 下回 `IDLE`。
5. **随机退避**：两边的 HELLO 间隔、ACK 间隔都带随机抖动；撞车后下一轮自然错开。
6. **CRC-8 + 字段校验**：版本、类型、发送者、目标、CRC 全部通过才处理，过滤噪声和自身帧。
7. **用户可见的进度**：蓝色慢闪 = 正在搜索；青色快闪 = 已经听到对方、正在握手；绿色常亮 = 双方都确认。若一块绿、一块青，绿的一方会补发 `DONE`，等 1–2 s 即可；还不行就按白色重来。

---

## 1. 交互与 LED

### 按钮

| 按钮 | 当前状态 | 动作 |
|---|---|---|
| 蓝色 | `IDLE` | 进入近场配对 `SEARCH` |
| 蓝色 | `SEARCH` / `CONFIRM` | 忽略（已经在配对中） |
| 蓝色 | `PAIRED` | 忽略（先按白色解除，再重新配对） |
| 白色 | `IDLE` | 广播一次 `BYE` 做残留清理，保持 `IDLE` |
| 白色 | `SEARCH` / `CONFIRM` | 取消配对，回 `IDLE` |
| 白色 | `PAIRED` | 向对方发 `BYE`（连发 2 次），双方回 `IDLE` |

> 白色键是“关闭近场配对模式”的总开关；配对成功后也用白色键解除。蓝色键只在待机时启动。
>
> 两个按钮都使用 `INPUT_PULLUP`，按下读到 `LOW`；代码里做了 25 ms 软件消抖。

### LED（板载 RGB，代码里已按实测极性处理）

| 状态 | LED | 含义 |
|---|---|---|
| `IDLE` | 白色常亮 | 近场配对关闭，待机 |
| `SEARCH` | 蓝色慢闪（300 ms 亮/灭） | 配对模式已开启，正在发 HELLO |
| `CONFIRM` | 青色快闪（120 ms 亮/灭） | 已收到对方 HELLO，正在握手 |
| `PAIRED` | 绿色常亮 | 配对成功，已拿到对方机器码 |
| 搜索超时 | 红色闪 4 下（1.2 s） | 120 s 没找到对方，回到白色待机 |

> 接线文档里没有外接 LED，所以这里用 Nano RP2040 Connect 的板载 RGB LED。它挂在 NINA 模块上，需要 `WiFiNINA` 库；`LEDR/LEDG/LEDB` 是 `NinaPin`。**实测这块板是 `digitalWrite(LEDx, HIGH)` 亮、`LOW` 灭**（`NinaPin` 封装内部还会反相一次，不要按普通共阳“LOW 亮”的直觉写）。代码里 `setLed()` 做了缓存，只有颜色变化时才写 NINA，避免频繁 SPI。

### 状态图

```
                   蓝色                     收到 HELLO
  IDLE ───────────────────────▶ SEARCH ───────────────────▶ CONFIRM
  (白色常亮)                    (蓝色慢闪)                    (青色快闪)
    ▲                              │                            │
    │ 白色 / 超时                   │ 白色                        │ 收到 ACK / DONE
    │                              ▼                            ▼
    └──────────────────────────── IDLE ◀──────────────────── PAIRED
                                       白色 / 收到 BYE          (绿色常亮)
```

---

## 2. 红外协议

### 2.1 物理层

采用 NEC 风格的 pulse-distance 编码，但数据位扩展到 48 bit：

| 项目 | 值 |
|---|---|
| 载波 | 38 kHz |
| 引导脉冲 | 9 ms mark + 4.5 ms space |
| 位 mark | 560 µs |
| 0 的 space | 560 µs |
| 1 的 space | 1690 µs |
| 数据位 | 48 bit，LSB first |
| 结束位 | 560 µs mark |
| 单帧时长 | 约 90–120 ms（取决于 1 的个数） |

使用 IRremote 的通用接口：

```cpp
IrSender.sendPulseDistanceWidth(38, 9000, 4500,
                                560, 1690,   // 1
                                560, 560,    // 0
                                frame, 48, 0, 0, 0);
```

接收端启用 `DECODE_DISTANCE_WIDTH`，用 `IrReceiver.decodedIRData.decodedRawData` 取 48 bit，再自行解析。

### 2.2 帧格式（48 bit，LSB first）

| bit | 字段 | 说明 |
|---|---|---|
| 0–3 | `version` | 协议版本，当前为 `0x1` |
| 4–7 | `type` | `1=HELLO`、`2=ACK`、`3=DONE`、`4=BYE` |
| 8–23 | `sender` | 发送方 16 bit 机器码 |
| 24–39 | `target` | 目标机器码；`0xFFFF` = 广播 |
| 40–47 | `crc` | CRC-8（poly `0x07`，初值 0），覆盖低 40 bit |

> 16 bit 机器码由 RP2040 的 64 bit 芯片唯一 ID 做 FNV-1a 哈希得到，串口打印为 4 位十六进制（如 `0xA3F7`）。`0x0000` / `0xFFFF` 保留。两块板烧同一份固件即可，ID 自动不同。
>
> 16 bit 有 1/65536 的碰撞概率；对两块板演示足够。以后接云平台时，把串口打印的完整 64 bit `Chip UID` 作为云端设备 ID，16 bit 码只作为近场链路的临时标签。

### 2.3 握手时序

正常情况（A 先被 B 听到）：

```
A (SEARCH)                         B (SEARCH)
    │                                  │
    │── HELLO(sender=A, target=BCAST) ─▶│
    │                                  │ 听到 A，进入 CONFIRM
    │◀─ ACK(sender=B, target=A) ───────│
    │ 听到 ACK，进入 PAIRED             │
    │── DONE(sender=A, target=B) ──────▶│
    │        （连发 3 次）              │ 听到 DONE，进入 PAIRED
    │                                  │
    ▼ 绿色                             ▼ 绿色
```

如果 `DONE` 丢了：

```
A (PAIRED)                         B (CONFIRM)
    │◀─ ACK(sender=B, target=A) ───────│  B 没收到 DONE，继续重发 ACK
    │  A 听到 ACK，补发 DONE            │
    │── DONE(sender=A, target=B) ──────▶│
    │                                  │  B 进入 PAIRED
```

如果双方同时听到对方的 HELLO，会各自发 ACK；可能都进入 PAIRED 并各自发 DONE。`DONE` 是幂等的，收到就直接进入 PAIRED，不会破坏状态。

### 2.4 防碰撞 / 时序

| 参数 | 值 | 作用 |
|---|---|---|
| 搜索 HELLO 间隔 | 260–700 ms 随机 | 避免两边同节奏发射 |
| ACK 重发间隔 | 320 ms + 0–100 ms 随机 | 撞车后下一轮错开 |
| DONE 连发 | 3 次，间隔 280 ms | 对抗单帧丢失 |
| CONFIRM 超时 | 8 s | 对方离开/持续撞车时回 SEARCH |
| SEARCH 超时 | 120 s | 自动退出配对，省电 |
| 发射后保护 | 30 ms | 等接收头 AGC 恢复 |
| 先听后发 | 3 ms 采样，低电平 >250 µs 视为忙 | 避免打断对方正在发的帧 |

> 这些参数都在 `.ino` 顶部的常量区，实测时可以调。比如两块板距离较远、误码率高，可以把 `DONE_REPEATS` 加到 5，或把 `ACK_INTERVAL_MS` 降到 250。

---

## 3. 代码与烧录

### 3.1 文件

- 固件：`firmware/nearfield_pairing/nearfield_pairing.ino`
- 两块板烧 **同一份** 固件，不需要改任何宏；机器码从芯片 UID 自动生成。

### 3.2 依赖库

在 Arduino IDE 的“库管理器”里安装：

- **IRremote** ≥ 4.7（本项目用 4.7.1 验证）
- **WiFiNINA** ≥ 2.1（只用于板载 RGB LED；不连 Wi-Fi 也能用）

命令行（可选）：

```powershell
arduino-cli lib install IRremote
arduino-cli lib install WiFiNINA
```

### 3.3 编译 / 上传

开发板选择 **Arduino Nano RP2040 Connect**，FQBN 为 `arduino:mbed_nano:nanorp2040connect`。

命令行编译：

```powershell
arduino-cli compile --fqbn arduino:mbed_nano:nanorp2040connect firmware\nearfield_pairing
```

上传（COM 号按实际改；本机当前是 COM3）：

```powershell
arduino-cli upload -p COM3 --fqbn arduino:mbed_nano:nanorp2040connect firmware\nearfield_pairing
```

### 3.4 代码里几个关键点

- `#define NO_LED_FEEDBACK_CODE`：D13 同时是 NINA SPI 的 SCK，IRremote 默认会用它做收发指示，必须关掉，否则可能干扰 NINA/RGB。
- `#define IR_SEND_PIN 9`：发射脚 D9。
- `#define DECODE_DISTANCE_WIDTH`：启用通用 pulse-distance 解码器，才能收 48 bit 自定义帧。
- `IrReceiver.begin(4, DISABLE_LED_FEEDBACK)`：接收脚 D4，关闭反馈 LED。
- `sendFrame()` 里 `IrReceiver.stop()` → 发射 → `delay(30)` → `IrReceiver.start()`：半双工，避免自接收和 AGC 饱和。
- `setLed()` 只在颜色变化时写 `LEDR/LEDG/LEDB`，避免每帧都走 NINA SPI。
- 串口 115200：开机（或第一次打开串口监视器）打印完整 `Chip UID` 和 16 bit 机器码；配对成功打印 `PAIRED peer=0xXXXX`；每 5 s 打印一次状态心跳。

---

## 4. 测试步骤

1. 两块板都烧录同一份固件，打开串口监视器（115200），记下各自的 `My machine code: 0x....`。
2. 两块板各按一下 **蓝色按钮**，LED 应蓝色慢闪，串口应周期性打印 `[state=SEARCH ...]`。
3. 让两块板的 **发射头对准对方的接收头**，距离 5–30 cm，尽量面对面，避免阳光直射和日光灯直照接收头。
4. 正常 1–3 s 内两块板都应绿色常亮，串口打印：
   ```
   RX ACK from 0x....
   PAIRED (ACK) peer=0x....
   RX DONE from 0x....
   PAIRED (DONE) peer=0x....
   ```
   其中 `peer` 就是对方的机器码。
5. 按任意一块板的 **白色按钮**，两块板都应回到白色常亮，串口打印 `White: stop pairing` / `Peer pressed white; pairing off`。
6. 如果一块绿、一块青：等 1–2 s，绿的一方会补发 `DONE`。还不行就按白色，重新按蓝色配对。
7. 如果两块都一直蓝闪：见下面的排查表。

### 串口调试命令

不方便按按钮时，可以在 115200 串口监视器里直接发送单个字符（不需要回车）：

| 字符 | 作用 |
|---|---|
| `b` | 等同按一下蓝色按钮 |
| `w` | 等同按一下白色按钮 |
| `s` | 打印当前状态 |

例如两块板都连在电脑上时，可以同时向两个串口发 `b`，观察完整的 `HELLO → ACK → DONE` 日志。

### 排查表

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 两块都蓝闪，永远不青 | 红外链路不通 | 检查 D9/D4、3V3/GND、发射/接收头方向；用手机相机看发射头是否在闪（多数手机能看到红外紫光）；用 38 kHz 遥控器测接收头 |
| 一块青一块蓝 | ACK 丢了 | 靠近、对准、避免遮挡；等 2 s；代码会自动重发 |
| 一块绿一块青 | DONE 丢了 | 等 2 s，绿的一方会补发；不行按白色重来 |
| 刚上电就绿 | 上一次的 BYE 没收到/状态残留 | 按白色解除，再重新配对；代码没有掉电保存配对状态 |
| 两块板的机器码相同 | 克隆板的 flash UID 可能重复 | 串口确认；在 `computeMachineId()` 里给其中一块强制一个不同 ID，或换板 |
| LED 不亮 | WiFiNINA 未安装 / NINA 异常 | 确认包含 `WiFiNINA.h`；串口能否打印；换 USB 线/供电 |
| 发射时板子复位 | USB 供电电流不足 / 电源跌落 | 发射模块 `+` 改 5V（S 仍由 3.3V GPIO 驱动），VCC–GND 加 100 nF，必要时加三极管 |
| 串口没输出 | 没打开串口 / 波特率不对 | 115200；打开后等 5 s 心跳；`if (Serial)` 在没连主机时会跳过打印 |

### 距离/稳定性

- 3 引脚 IR 发射模块在 3V3 下距离通常较短（几厘米到十几厘米）；如果不够，按 HARDWARE.md 把发射模块 `+` 改接 **5V**（信号 S 仍由 3.3V GPIO 驱动），或加 NPN 三极管增大电流。
- 接收头 VCC–GND 就近加 100 nF 去耦；避免阳光、日光灯直射。
- 发射头和接收头不要贴在一起，否则本地强光会让接收头饱和；外壳上最好加一小段黑色隔光墙。

---

## 5. 后续接云平台的接口

当前固件是 **纯本地** 的：WiFiNINA 只用来点板载 RGB，不连 Wi-Fi。以后加云平台时：

1. **设备身份**：用开机打印的 64 bit `Chip UID` 作为云端设备 ID；16 bit 机器码只作为近场链路的临时标签。云平台里加好友时，把 `{selfUid, peerUid, timestamp}` 上报即可。
2. **配对事件**：在 `handleFrame()` 收到 `DONE`/`ACK` 进入 `PAIRED` 的地方加一个 `onPaired(peerId)` 回调；云端逻辑放在回调里，不要阻塞 IR 状态机。
3. **Wi-Fi 初始化**：在 `setup()` 末尾调用 `WiFi.begin(ssid, pass)` 即可；NINA 的 SPI 与 IR 的 D4/D9 不冲突。注意 Wi-Fi 连接过程可能耗时几秒，建议放在后台状态机里，不要阻塞 `loop()`。
4. **对端完整 UID**：当前 48 bit 帧只带 16 bit 机器码，没有空间放对方 64 bit UID。如果云端必须拿到对方完整 UID，可以在配对成功后加一轮“UID 交换”（两帧 48 bit 拼 64 bit，或把帧扩到 64 bit）。现在只要求“收到对方机器码”，16 bit 已够用。
5. **LED 与 Wi-Fi 共用 NINA**：RGB 写入频率已经做了缓存；加入云逻辑后不要在高频循环里直接写 `digitalWrite(LEDR, ...)`，继续走 `setLed()`。

---

## 6. 设计取舍

- **48 bit 帧**：刚好放下两个 16 bit ID + 类型 + CRC，单帧约 90–120 ms；比 32 bit 多约 1.5 倍空中时间，但换来了“谁发给谁”的定向确认和 CRC。对 1–2 s 的配对手感没有影响。
- **16 bit 机器码**：两块板足够；碰撞概率 1/65536。以后云端用 64 bit UID 区分。
- **阻塞式发射**：`sendPulseDistanceWidth()` 会阻塞约 100 ms；按钮在两次发送之间轮询。正常按键 >100 ms，不会漏；如果要做“轻点”交互，需要改中断/队列。
- **板载 RGB**：不用额外接线，但依赖 WiFiNINA/NINA 模块。若以后换成外接 WS2812 或普通 RGB，只需替换 `setLed()`，状态机不变。
- **不自动解除配对**：对方关机/离开后，本机仍保持绿色，直到按白色或收到 `BYE`。这是有意的：近场配对是“这次社交动作”的本地记录，云端好友关系以后由云平台维护。

---

## 7. 实测记录（2026-11-18）

两块 Nano RP2040 Connect 通过 USB 连接到电脑：

| 端口 | Chip UID | 16 bit 机器码 |
|---|---|---|
| COM3 | `50351706825E8318` | `0xA30F` |
| COM5 | `50236541C8965F1C` | `0xC371` |

实测结果：

- 两块板面对面 **5–10 cm**，串口发 `b` 后约 1 s 内完成握手；两块都进入 `PAIRED`，心跳分别显示 `peer=0xC371` / `peer=0xA30F`。
- 串口发 `w` 后，COM3 发出两帧 `BYE`，COM5 收到并回到 `IDLE`，两块板都回到白色待机。
- 再次发 `b`，重新配对成功。
- 说明红外收发、48 bit 解码、CRC、三次握手和 `BYE` 解除链路均已跑通。
- 板载 RGB 实测为 **`digitalWrite(LEDx, HIGH)` 亮、`LOW` 灭**；第一次测试颜色不对（待机不亮、搜索红绿底、配对偏紫），已在 `setLed()` 中修正。修正后已视觉确认：**待机白色常亮、搜索纯蓝慢闪、配对纯绿常亮**；按白色按钮两块板都回到白色常亮。

> 第一次测试时两块板只是并排放在桌上，接收头完全收不到信号（日志没有任何 `RX`）；改成面对面、发射头对准对方接收头后立即成功。这说明 **红外是强方向性的视线链路**，外壳/摆放方式很重要。
