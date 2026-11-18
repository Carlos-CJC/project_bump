# 近场配对电子社交挂件

两块 **Arduino Nano RP2040 Connect** 通过红外近场通信互相识别，配对成功后用板载 RGB 显示状态，并把对方的 16 bit 机器码拿到本地。后续接入云平台/App，把“近场配对”升级成“加好友”。

当前状态：**设备端红外配对已完成并实测通过**；云平台和 App 待实现。

## 快速体验

1. 两块板都烧录同一份固件（`firmware/nearfield_pairing`）。
2. 打开串口 115200，记下各自的 `My machine code`。
3. 两块板各按一下 **蓝色按钮**，LED 蓝色慢闪。
4. 两块板 **面对面 5–10 cm**，透明发射头对准对方黑色接收头。
5. 握手成功 → 两块板 **绿色常亮**；串口打印 `PAIRED peer=0x....`。
6. 按任意一块板的 **白色按钮**，两块板回到 **白色常亮**。

> 红外是强方向性的视线链路：并排放在桌上通常收不到，必须面对面。距离不够时按 `HARDWARE.md` 把发射模块 `+` 改接 5V。

## 文档地图

| 文档 | 读者 | 内容 |
|---|---|---|
| [HARDWARE.md](HARDWARE.md) | 硬件/接线 | 引脚分配、接线图、3V3/5V、接收头注意事项 |
| [OPERATION.md](OPERATION.md) | 固件/测试 | 操作逻辑、状态机、LED、48 bit 协议、时序、排查表、实测记录 |
| [HANDOFF_CLOUD_APP.md](HANDOFF_CLOUD_APP.md) | 云端/App | 设备身份（UID64/code16）、数据接口、云端架构、数据模型、API、好友流程、安全、测试计划 |
| [firmware/nearfield_pairing/nearfield_pairing.ino](firmware/nearfield_pairing/nearfield_pairing.ino) | 固件 | 当前完整固件 |

## 当前设备

| 端口 | Chip UID | 16 bit 机器码 |
|---|---|---|
| COM3 | `50351706825E8318` | `0xA30F` |
| COM5 | `50236541C8965F1C` | `0xC371` |

## 固件要点

- 48 bit 自定义帧：`version + type + sender16 + target16 + CRC-8`。
- 三次握手：`HELLO` → `ACK` → `DONE`；ACK 重发、DONE×3、PAIRED 补发。
- 半双工 + 先听后发 + 随机时隙；发射后 30 ms AGC 保护。
- 16 bit 机器码由 RP2040 64 bit 芯片 UID 哈希得到；两块板烧同一份固件，ID 自动不同。
- 串口 115200：开机打印完整 `Chip UID` 和机器码；调试命令 `b`（蓝键）/`w`（白键）/`s`（状态）。
- 依赖：IRremote 4.7.1、WiFiNINA 2.1.1；开发板 `arduino:mbed_nano:nanorp2040connect`。

## LED

| 状态 | LED |
|---|---|
| `IDLE` | 白色常亮 |
| `SEARCH` | 蓝色慢闪 |
| `CONFIRM` | 青色快闪 |
| `PAIRED` | 绿色常亮 |
| 搜索超时 | 红色闪 4 下 → 白色 |

> 板载 RGB 实测：`digitalWrite(LEDx, HIGH)` 亮、`LOW` 灭（`NinaPin` 封装内部会反相，不要按普通共阳直觉写）。

## 下一步

- 云端/App：按 `HANDOFF_CLOUD_APP.md` 实现设备注册、配对事件解析、好友请求/列表。
- 固件：加 Wi-Fi 配网、`CloudLink`、`onIrPaired()` 回调、token/事件队列。
- 可选：红外交换完整 64 bit UID，摆脱对云端 `code16` 注册表的依赖。
