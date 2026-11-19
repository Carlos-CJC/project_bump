# Bump BLE MVP — 单板 Mock 过程总结

日期：2026-11-15
分支：`caojiecheng/mid-report-mock`
提交：`b535419 feat: add deterministic mock RX demo for single-board reporting`
构建环境：`nanorp2040connect_mock`
配套日志：`tmp/mock_serial_log.txt`

---

## 1. 背景与目标

只有一块 Arduino Nano RP2040 Connect，无法进行真实双机收发。目标是产出一份可用于中期报告的串口输出，覆盖：

- BLE 广播（发射）
- 扫描后的帧解析（接收）
- RSSI 过滤、停留时间判定
- 偶遇事件输出

单板无法收到自己的广播，因此采用「真实发射 + 模拟接收样本注入」的方案。

## 2. Mock 的边界

| 环节 | 是否真实 | 说明 |
|---|---|---|
| BLE 初始化 / 广播 | 真实 | 板子实际以不可连接广播发出 UUID `a5b0` 与厂商数据 |
| `[tx]` 打印的广播帧 | 真实 | 与实际广播内容一致 |
| `[mock] rx` 接收样本 | 模拟 | 固件按场景注入，未经过空口 |
| 帧解析 / 判定 / 事件 | 真实逻辑 | 模拟帧走与真实扫描相同的代码路径 |

模拟数据通过 `[mock]` 前缀和事件中的 `"sim":true` 标记，报告中必须注明。

## 3. 实现步骤

1. 创建分支 `feature/mock-serial-demo`，后改名为 `caojiecheng/mid-report-mock`。
2. 拆分 `platformio.ini`：公共配置放入 `[env]`，新增 `[env:nanorp2040connect_mock]`，通过 `-DBUMP_MOCK=1` 控制。
3. 新增 `buildMfgFrame()`，真实广播与 mock 共用帧布局：
   `Company ID(2) | ver(1) | short_id(6) | flags(1) | tx_power(1)`。
4. 新增 `hexString()`，用于打印完整广播帧。
5. 设计 9 阶段、约 41s 的模拟场景，包含两个虚拟 peer A/B 的靠近、停留、离开。
6. 实现 `mockTick()`：按真实时间推进场景，仅在扫描阶段每 200ms 注入一个样本，RSSI 在阶段内线性插值并叠加固定种子抖动。
7. 将扫描处理重构为共享函数 `ingestFrame()`，真实扫描与 mock 注入都调用同一套校验、解析和 `onSample()` 判定。
8. `emitEncounter()` 在 mock 构建下追加 `"sim":true`，并统计模拟事件数。
9. `setup()` 打印模式、真实广播帧，初始化场景并固定相位抖动随机种子；`loop()` 调用 `mockTick()`。
10. 补充审阅注释：协议字段、判定算法、时间回绕、指针生命周期、真实与模拟区分。
11. 编译验证：真实环境与 mock 环境均成功，RAM 约 16.3%，Flash 约 0.2%。
12. 离线时序仿真确认两次偶遇能在场景内触发（约 8.7s、27.7s）。
13. 实机烧录运行，得到完整串口日志。
14. 将完整日志归档为 `tmp/mock_serial_log.txt`；`tmp/` 已通过 `.git/info/exclude` 排除，不进入版本库。

## 4. 关键参数

| 参数 | 值 |
|---|---|
| Service UUID | `0xA5B0` |
| Company ID | `0xFFFF`（测试用） |
| RSSI 阈值 | -70 dBm |
| 最少强信号样本 | 5 |
| 最短停留 | 3000 ms |
| 样本计数窗口 | 10000 ms |
| peer 失联重置 | 30000 ms |
| 偶遇去重冷却 | 8 小时 |
| 广播 / 扫描 | 600 ms / 400 ms，±100 ms 抖动 |
| mock 采样间隔 | 200 ms（仅扫描阶段） |
| peer 表容量 | 16（LRU 淘汰） |

## 5. 模拟场景时间线

| 阶段 | 时长 | RSSI | 目的 |
|---|---|---|---|
| 静默 | 3000 ms | -95 | 无信号 |
| A 靠近 | 2500 ms | -85 → -72 | 弱信号被过滤 |
| A 停留 | 9000 ms | -68 → -56 | 触发偶遇 |
| A 离开 | 3000 ms | -72 → -90 | 信号衰减 |
| 间隔 | 4000 ms | -95 | — |
| B 靠近 | 2500 ms | -84 → -71 | 弱信号被过滤 |
| B 停留 | 9000 ms | -67 → -55 | 触发偶遇 |
| B 离开 | 3000 ms | -71 → -89 | 信号衰减 |
| 收尾静默 | 5000 ms | -95 | 场景结束 |

虚拟 peer：

- A：`a10000000001`
- B：`b20000000002`

## 6. 实机运行结果

- 约 11s：peer A 触发偶遇，`rssi_avg=-65`、`duration_s=3`、`samples=8`、`sim:true`。
- 约 29s：peer B 触发偶遇，`rssi_avg=-62`、`duration_s=3`、`samples=8`、`sim:true`。
- 约 41s：场景结束，输出 `59 samples, 2 encounter event(s) simulated`。
- 之后 peer 在 30s 失联窗口后从统计中移除，无重复事件，验证了去重与生命周期逻辑。

## 7. 报告使用注意事项

1. 明确写明：广播为真实 BLE 发射，接收样本为固件注入的模拟数据。
2. `duration_s=3` 表示“达到判定阈值时的停留时长”，不是实际靠近总时长。
3. 日志开头 `Disconnected / Reconnecting` 是串口监视器重连提示，不是固件异常。
4. 完整原始输出见 `tmp/mock_serial_log.txt`，该文件未提交、未上传 GitHub。
