# Bump 双板联调固件

BumpDualBadge 0.5.0-dual 用于本次两枚 Nano RP2040 Connect 和本机接收端。真人摇动使用 11 月 15 日单板固件的双峰判定，开启 60 秒窗口。两端交替广播 A5B0 和扫描，每个窗口每个对端最多产生一条观察。用 −70 dBm、至少 2 个采样、跨度至少 3 秒判定。诊断开窗也使用实体收发，串口记录标明开窗来源。

## 串口与按键

串口用 115200，命令以换行结束。

| 命令或输入 | 行为 |
|---|---|
| status | 显示 device_id、boot_id、实际广播 self_short_id、红外机器码、动作、队列和网络配置状态 |
| i / q | 打开或关闭每秒状态与 IMU 采样输出 |
| discover | 诊断开启 60 秒广播与扫描，来源记为 serial_diagnostic |
| SET ble_role a / SET ble_role b | 下一窗先扫描或先广播。A30F 默认先扫描，C371 默认先广播 |
| 蓝键 / b | 开始红外搜索。若正在 BLE 发现则取消该窗 |
| 白键 / w | 结束当前红外会话。红外空闲时切换 BLE 发现暂停 |
| pause | 仅切换 BLE 发现暂停 |
| SET ssid、SET wifi_password、SET broker_host、SET broker_port、SET mqtt_password | 按「SET 键 值」逐行配置。broker_host 只接受私有 IPv4，默认端口 18883。MQTT 用户名使用状态所示 device_id |
| sync | 在发现窗口及红外握手结束后安排直连发送。完整配置也会触发首次 presence |
| dump | 逐条输出 presence 和当前待发事件，含 topic 与完整 JSON |
| SET transport serial_bridge | 切换为 USB 导出后中转。中转入库后用 ACK 加空格和原样 JSON；只处理已由 dump 导出的队首 |
| SET transport wifi_mqtt | 恢复板端本地 MQTT TCP 发送 |
| r / t | 重启 / 进入 USB 启动盘。重启、掉电或重刷会清除 RAM 队列、网络参数和本次启动短 ID |

直接 MQTT 与 USB 中转应在日志中区分。每次网络会话先发在线 presence，随后发待确认项并等待本设备应用回执，最后发离线状态。broker PUBACK 和服务确认分开记录，回执必须匹配事件编号且 status 为 stored 或 duplicate。断网时保留队列并在设备继续供电时每 15 秒重试。传输使用局域网认证 TCP。两个设备应分别设置各自的 MQTT 密码。

红外协议、引脚、物理按键和四态灯色复用组员附件。NINA 同时参与 RGB 和无线通信，BLE 或 Wi-Fi 占用期间关闭板载 RGB，释放无线资源后按红外状态恢复 LED。配对成功进入绿灯，握手补发结束后才允许网络上传。

## 重建

使用此前单板安装的 Arduino CLI 与 Arduino Mbed Nano 4.6.0，目标 arduino:mbed_nano:nanorp2040connect。现有库版本为 ArduinoBLE 2.1.0、Arduino_SpiNINA 0.0.2、WiFiNINA 2.1.1、ArduinoMqttClient 0.1.8、Arduino_LSM6DSOX 1.1.2。IRremote 4.7.1 来自 Arduino 库索引并核对 SHA-256，保存在 vendor/libraries。

在本目录用 PowerShell 执行：

~~~powershell
$tool = 'E:\港理课程\网站维护\任务工作记录\2026-10-23_eie568_lab1\toolchain'
$env:ARDUINO_BUILD_CACHE_PATH = Join-Path (Get-Location) 'cache'
& "$tool\bin\arduino-cli.exe" compile --config-file "$tool\arduino-cli.yaml" --fqbn arduino:mbed_nano:nanorp2040connect --libraries "$PWD\vendor\libraries" --build-path "$PWD\build" "$PWD\BumpDualBadge"
~~~

生成 build/BumpDualBadge.ino.uf2。组员原版的独立构建保存在 baseline-build，原板闪存备份由实板测试步骤单独核验。
