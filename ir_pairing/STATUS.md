# IR 近场配对模块（独立存放，未集成）

> 这个目录只是把我们这套 **IR 近场配对** 的固件和文档原样存放在 `project_bump` 仓库里，方便朋友拉取后自行适配。  
> **没有改动** `src/`、`platformio.ini` 或 Bump 原有文档；`ir_pairing/` 在 `src/` 外面，PlatformIO 不会编译它。

## 当前状态

- 已在两块 Arduino Nano RP2040 Connect 上实测通过：
  - 红外三次握手 `HELLO → ACK → DONE`；
  - 配对成功绿色常亮、白色待机、蓝色搜索、青色握手；
  - 白色按钮 `BYE` 解除，双方回待机；
  - 串口打印双方 16 bit 机器码。
- 使用 **Arduino Mbed core**（`arduino:mbed_nano:nanorp2040connect`）编译，依赖 IRremote 4.7.1 + WiFiNINA 2.1.1（WiFiNINA 只用于板载 RGB）。
- Bump 项目使用 PlatformIO `platform = raspberrypi`（earlephilhower core）；两者核心不同，**本目录代码不能直接加入 Bump 的 `src/` 编译**，需要朋友适配。

## 目录内容

| 文件 | 内容 |
|---|---|
| `HARDWARE.md` | 接线、引脚、3V3/5V、接收头注意事项 |
| `OPERATION.md` | 设备端操作逻辑、状态机、LED、48 bit 红外协议、测试记录 |
| `HANDOFF_CLOUD_APP.md` | 云端/App 交接：UID64/code16、数据接口、云端架构、数据模型、API、好友流程、安全、测试 |
| `firmware/nearfield_pairing/nearfield_pairing.ino` | 当前完整固件（Arduino Mbed core） |
| `README.md` | 快速体验、文档地图、当前实测板 ID |

## 给 Bump 适配同学的关键提示

1. **产品定位**：Bump 的 BLE 是“被动偶遇时间线”；IR 是“主动碰一碰加好友”。两者互补，IR 可以替代/补充 Bump 计划里的 NFC 碰一碰。
2. **核心差异**：本固件用 Arduino Mbed core；Bump 用 PlatformIO/earlephilhower。移植时 IRremote 在 RP2040 上会走 PIO，API 基本一致；但不要直接用 `WiFiNINA` 的板载 RGB（会和 ArduinoBLE 共用 NINA，容易冲突），建议用外接 RGB/WS2812 或 Bump 的可选 TFT 显示状态。
3. **BLE 会阻塞数秒**：Bump 的 BLE 重启 `BLE.end()/begin()` 会阻塞 2–15 s。建议把 IR 配对做成 **独立模式**：按蓝色按钮 → 暂停 BLE → IR 配对 → 输出 `pair` 事件 → 恢复 BLE；不要一上来就做 BLE+IR 完全并发。
4. **云端 ID 映射**：Bump 用 6 字节 `short_id`（每次上电随机）；IR 用 16 bit `code16`（由 64 bit 芯片 UID 哈希，稳定）。云端 `devices` 表建议增加 `uid64`、`code16`，好友请求 `method` 增加 `IR`。
5. **建议的事件**（串口 JSON，供云端/网关消费）：

   ```json
   {"event":"pair","method":"IR","self_code16":"A30F","peer_code16":"C371","uptime_s":123}
   ```

   云端用 `peer_code16` 查设备 → 用户，创建好友请求；详细流程见 `HANDOFF_CLOUD_APP.md` 第 4–7 节。

6. **本目录没有做的适配**：没有改 Bump 的 `src/`，没有加 PlatformIO env，没有把 IR 代码改成 earlephilhower core。朋友拉取后按 `HANDOFF_CLOUD_APP.md` 和 Bump 的实际云接口自行集成。
