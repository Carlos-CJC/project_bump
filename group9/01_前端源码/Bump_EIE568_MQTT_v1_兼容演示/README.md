# MQTT v1 本地兼容版

双击 `Start-Bump.cmd` 启动，登录后展开 **MQTT v1 协议实验**。先读 [新版操作与协议边界](COMPAT_V1_GUIDE.md)；以下保留原版软件说明，旧版测试数不是本版测试数。

---

# Bump 无硬件开发与交接包

EIE568 Group 9 · 软件版本 0.1.0 · 2026-10-23

这份代码用于四位组员在没有开发板时共同推进项目。已提供两枚模拟挂件、真实本机
MQTT/TLS 服务、SQLite 数据库、网页，以及可编译的 Nano 固件核心。模拟事件经过
实际的网络连接与数据库，网页没有写死相遇记录。

**当前没有实物验证。** Python 模拟器生成 `source="simulated"` 的事件，60 秒窗口
由场景程序快进；距离、误触率、耗电、无线共存和板端网络接入都不属于已验证结果。
完整四人共享手册见 `docs/`，测试证据见 `evidence/`。

## 1 先运行起来

本次验证环境为 Windows 64 位、Python 3.12.14。首次安装需要联网。其他操作系统
未做完整复现验证；组员先使用 Python 3.12 和锁定依赖。

在解压后的 `Bump_Starter` 文件夹打开 PowerShell：

```powershell
# 第一次：创建隔离环境、安装依赖、生成本机演示账号和证书
powershell -NoProfile -ExecutionPolicy Bypass -File .\Setup.ps1

# 窗口 1：启动服务，保留此窗口
powershell -NoProfile -ExecutionPolicy Bypass -File .\Start.ps1
```

`ExecutionPolicy Bypass` 只作用于这次脚本进程，不修改整台电脑的执行策略。
脚本会使用 Python Launcher 的 `py -3.12`；如果安装在自定义位置：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Setup.ps1 `
  -PythonPath "C:\你的Python312目录\python.exe"
```

安装 Python 可使用 [Python 官方 Windows 下载页](https://www.python.org/downloads/windows/)。
如已有隔离环境，重复执行 Setup 会保留 `.runtime` 中的账号和数据库。

启动成功后打开 [Bump 本机网页](http://127.0.0.1:8765)。另开窗口 2，仍进入本文件夹：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run-Demo.ps1
```

看到 `"ok": true`、两个 `queue_size` 都为 `0`，表示两条模拟事件已经获得应用确认。
打开 `.runtime/credentials.txt`，使用生成的 Alice 密码登录网页；时间线应出现
`B00002`，并标为“模拟数据”“双方记录已匹配”。

密码只保存在自己的电脑上。转发源码包时**不要附带 `.runtime` 或 `.venv`**。
每位组员各自运行 Setup，生成独立的密码、证书和数据库。

停止服务：在窗口 1 按 `Ctrl+C`。数据库会保留；模拟挂件的 RAM 队列不跨进程保存。

## 2 检查完整的网页流程

三个测试账号都是虚构身份，不是四位组员的正式账号。四位组员可以各自在自己的
电脑上复现同一流程。

1. Alice 对应挂件 A，Bob 对应挂件 B，Charlie 是无关第三人。
2. 运行正常模拟后，Alice、Bob 各能看到自己的相遇记录。此时资料与联系方式隐藏。
3. Alice 发出邀请；退出后登录 Bob，可以选择同意或拒绝。
4. Bob 同意后，双方才能看到虚构资料。任一方可撤销联系，资料会重新隐藏。
5. 登录 Charlie：不应看到 A 与 B 的记录或邀请。自动测试还会直接尝试越权接口。
6. “移出我的记录”只隐藏本人时间线，保留去重证据；重复上传不能使记录复活。
   它不删除对方的观察，也不撤销已有联系。联系需单独撤销。

同一观察的邀请被拒绝/撤销后，再次点击会返回原状态。要再次邀请，请产生新的相遇；
不能靠重复点击恢复旧关系。撤销也无法收回对方此前已看见或自行保存的内容。

## 3 运行不同场景

先让 Start 持续运行。三个场景都采用真实的本机 MQTT/TLS 传输：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run-Demo.ps1 -Scenario normal
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run-Demo.ps1 -Scenario offline
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run-Demo.ps1 -Scenario retry
```

| 场景 | 实际操作 | 应观察到 |
|---|---|---|
| normal | 生成双方各一条模拟观察，经 MQTT 入库 | 两条唯一记录；两端收到 stored；队列清空 |
| offline | 两端先连接再断开，在未连接时保留队列，暂停后重新连接 | `connected_before_outage` 和 `offline_pending` 均有值；暂停中补传成功 |
| retry | A 收到第一次成功 ACK 时故意丢弃，重传同一事件 | A 的 `dropped_ack_count=1`，最终收到 duplicate；数据库不增重复行 |

各次运行会生成新的 boot_id，因此会新增一组相遇。这是同一 session 下多次相遇，
不是去重失败。每台模拟挂件同时只开一个实例；同一 MQTT client_id 会挤掉先前连接。

`offline` 不会关闭电脑 Wi-Fi，也不是“五分钟断网”的硬件试验。自动测试另包含
真实停止并重启本机 broker、一次写库失败和一次 ACK 丢失后的恢复。

## 4 自动测试和证据

关闭正在运行的 Run-Demo 后执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Test.ps1
```

测试使用临时目录和随机端口，不改自己的演示数据库。输出保存在
`evidence/latest-tests.xml`；本次交接时的固定证据是 `evidence/software-tests.xml`、
`evidence/software-tests.txt` 和 `evidence/validation_summary.json`。

本次固定测试结果为 **76 passed，0 skipped**。另在全新文件夹复现了安装、初始化、
启动和 normal 场景：40 项锁定依赖一致，8 组真实 HTTP 检查通过，两个设备收到应用
ACK 后队列归零。详情见 `evidence/fresh-install.txt`；该新环境没有重复运行完整测试套件。

| 测试范围 | 实际验证方式 |
|---|---|
| 事件、时间、去重、事务 | SQLite 与协议回归测试，包括并发重复及提交失败 |
| 登录和资料权限 | FastAPI TestClient，检查真实应用响应、cookie、CSRF 和对象权限 |
| MQTT 集成 | 真实 TLS socket；三个场景、服务中断、错误认证、主题 ACL、错误 CA |
| 网页 | 浏览器实际登录、相遇、邀请与批准；撤销、第三人权限有API自动测试。UI逐项状态见 `evidence/browser/verification.json` |
| 前端会话竞争 | Node 执行异步回归测试，防止退出/换号后旧响应覆盖新账号页面 |
| 固件核心 | 编译并执行同一份 C++ 核心，再验证它产生的 JSON 与服务器兼容 |
| Nano 板卡目标 | Arduino CLI 对 Nano RP2040 Connect 编译；默认模拟模式和真实 IMU API 分支分别检查 |

网页回归测试需要 Node；固件核心测试需要 g++、clang++ 或 Zig。没有这些工具时会
明确显示 `skipped`，不等于测试通过。可用环境变量 `BUMP_NODE` 和 `BUMP_CXX`
指向本机工具；大型工具链不在交付包里。Nano 编译另按 `firmware/README.md`。
本次所有最终测试的实际通过/跳过数量，以 `validation_summary.json` 为准。

## 5 文件与模块交接

| 路径 | 内容 |
|---|---|
| `bump/device.py`、`simulator.py` | 模拟状态、有限队列、完整 ACK 键、正常/断线/重传场景 |
| `bump/broker.py`、`worker.py` | 本机 TLS broker、设备认证与 ACL、校验入库后发应用 ACK |
| `bump/protocol.py`、`store.py` | 严格消息合同、SQLite 数据、互遇、会话与联系权限 |
| `bump/api.py`、`web/` | 登录和授权 API、响应式网页 |
| `bump/config.py`、`__main__.py` | 本地初始化与统一启动入口 |
| `firmware/` | Nano 草图、portable C++ 核心、广播字节格式、编译和接线待验说明 |
| `docs/CONTRACT.md` | 两端统一字段、topic、ACK、HTTP 接口与广播布局 |
| `docs/CHANGELOG.md` | 实施调整及原因 |
| `docs/` 中的完整交接手册 | 全组共用的一份项目范围、任务、期限、分工、验收与当前进度 |
| `tests/`、`evidence/` | 可重新执行的测试与本次已完成的证据 |

## 6 当前实现的约定

- 事件唯一键为 `(device_id, boot_id, seq)`；同键相同内容返回 duplicate，不新增记录；
  同键改内容是冲突。worker 必须等事务提交完成后才发送 stored。
- `QoS 1` 的 PUBACK 不能清除挂件队列；应用 ACK 必须匹配完整事件键，状态为
  stored/duplicate，且来自已验证的连接与该设备自己的 ACK topic。
- 两条反向观察只有同 session、同 source、可信 UTC 区间重叠才标为互遇。
  未校时数据保留，但不拿两块板各自的开机毫秒强行匹配。
- 模拟器队列默认 128 条，满后拒绝新事件并累计溢出数，不覆盖旧记录。
  仅在当前进程持续存在时保留；不是断电恢复方案。
- MQTT 只监听 `127.0.0.1:18883`，网页只监听 `127.0.0.1:8765`。MQTT 校验本机
  开发 CA 和主机名；每个设备有独立凭据，绑定 client_id，topic 权限分方向。
- 开发 broker 只接受 `clean_session=True`，新连接必须先认证，再处理旧会话。
  应用队列负责未确认事件重传；不依赖 MQTT 持久会话保存离线消息。
- 网页为本机 HTTP，使用 HttpOnly、SameSite=Strict cookie 和逐会话 CSRF；
  它没有公网 HTTPS、正式账号注册、密码找回或生产部署运维。
- 固定演示映射为 demo-001 / A00001 / B00002；没有自动轮换化名、24 小时自动清理、
  人脸/定位或真实联系方式采集功能。

## 7 拿到硬件后的接续顺序

1. 登记两块板、NINA 固件、Arduino 库、供电与按钮引脚，保留原 Lab 文件。
2. 烧录并检查真实 LSM6DSOX 采样、无串口冷启动、摇动识别、暂停和 LED。
3. 接入 ArduinoBLE，核对双方实际收到的 29 字节包、重复采样和 RSSI。
4. 接入 WiFiNINA / ArduinoMqttClient、每启动唯一 boot_id、UTC 校时及完整 ACK。
   当前 Nano 草图的无线仍是串口模拟；Python 网络实现不能直接烧录到 Nano。
5. 为受控局域网配置服务器可达地址、TLS 证书主机名与设备信任根，再验证设备认证和
   ACL。真实板卡无法连接本机的 localhost；不能只改 IP 就称为完成。
6. 重做“真实摇动 → 真实双机 BLE → MQTT → 入库 ACK → 网页 → 双方同意”全流程。
7. 按完整手册采集手势、负样本、距离/方向、5 分钟断网和 20 轮稳定性数据。

固件默认模式永远标记 simulated；即使编译了真实 IMU API 分支，只要无线仍是模拟，
整条相遇记录仍不能写成 hardware 结果。真实数据路径接通后再添加 hardware 序列化。

## 8 常见问题

| 现象 | 检查方法 |
|---|---|
| 找不到 Python 3.12 | 安装 3.12 或给 Setup 传 `-PythonPath`；不要直接复用别人复制来的 .venv |
| 安装依赖失败 | 首次安装需要网络；保留报错，确认 Python 版本、代理/网络后重试 |
| 端口被占用 | 先关掉之前启动的 Bump 服务；也可用下面的新 runtime 指定其他端口 |
| 模拟返回 ok=false | 确认窗口 1 仍在运行，且两个窗口用的是同一 runtime；不能并行开启同设备模拟器 |
| 网页暂时没有记录 | 点击刷新；确认模拟 ok=true、账号正确；Charlie 无记录是预期行为 |
| 证书不可信或过期 | 不要关闭校验；确认共用同一 runtime，或用新目录初始化独立环境 |
| 出现 skipped | 查看缺失的是 Node 还是 C++ 编译器，补齐后重跑对应测试 |

需要一套全新演示环境时，用新目录初始化，不必删除旧证据：

```powershell
.\.venv\Scripts\python.exe -m bump --runtime .runtime-new init --web-port 8766 --mqtt-port 18884
.\.venv\Scripts\python.exe -m bump --runtime .runtime-new serve
# 另一个窗口
.\.venv\Scripts\python.exe -m bump --runtime .runtime-new simulate --scenario normal
```

## 官方技术依据

- [aMQTT broker 配置](https://amqtt.readthedocs.io/en/latest/references/broker_config/)
- [Eclipse Paho Python client API](https://eclipse.dev/paho/files/paho.mqtt.python/html/client.html)
- [FastAPI 测试说明](https://fastapi.tiangolo.com/tutorial/testing/)
- [OASIS MQTT 3.1.1](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html)

当前依赖版本见 requirements.txt；源码、运行结果和硬件证据分别核对。
