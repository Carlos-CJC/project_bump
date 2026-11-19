# EIE568 Group 9 · Bump 智能社交挂件

本包以两枚 Arduino Nano RP2040 Connect 实际安装并读回核验的 **0.5.8-dual** 为主版本。网页使用本机 FastAPI、MQTT 接收端和 SQLite。两枚挂件的 BLE 相遇在双方网页匹配后，接收方仍须同意联系申请才会显示资料。

| 查找内容 | 入口 |
|---|---|
| 实板网页与软件模拟网页 | [两套网页入口](01_前端源码/README.md)。每套分别附启动器、前端资源、账号和数据位置说明。 |
| 后端、MQTT、数据库、对时与 API | [02_后端源码](02_后端源码)。入口为 [live_server.py](02_后端源码/live_server.py)，模块在 bump/。 |
| 设备源码、无线库、可刷固件 | [0.5.8 设备工程](03_固件与构建/0.5.8-dual/BumpDualBadge)、[已读回核验的 UF2](03_固件与构建/0.5.8-dual/build/BumpDualBadge.ino.uf2)、[构建版本与散列](03_固件与构建/0.5.8-dual/build-manifest.json)。 |
| 测试如何执行 | [04_测试程序](04_测试程序)。服务端回环与故障测试、双板采集和核验脚本、单板阶段实验、源代码级用例分别归档。硬件实验脚本保留当时的板号和工具配置。 |
| 测试产生的输入及原始观测 | [05_测试数据](05_测试数据)。双板串口与分轮记录、单板实物与注入、模拟输入分别存放。 |
| 实测结论、失败轮次、网页截图 | [验收结果索引](06_测试结果/验收结果索引.md)。截图与对应事件核对 JSON 均可离线打开。 |
| 提案、进度报告、接线和操作说明 | [07_课程与硬件文档](07_课程与硬件文档)。含原始课程 Word/PDF、交接手册和可选 A4 双卡。课程原件保留小组署名。 |
| 历史版本及组员代码 | [版本对照](08_历史版本与组员材料/版本对照.md)。含早期软件原型、单板、历史双板版本、GitHub 主分支与红外交接材料。 |
| 跨平台启动器 | [run.py](run.py) 与 [run.sh](run.sh)、[run.ps1](run.ps1)。统一用 pyenv 选定的 Python 3.12 创建项目内 `.venv`。 |
| 合并说明 | [MERGE_NOTES.md](MERGE_NOTES.md)。记录并入主仓库时的目录取舍、凭据清理和双端适配改动。 |

> 同学原始交付包里的 `09_离线运行依赖`（Windows x64 便携 Python）未纳入仓库。现在两端都改用 pyenv + `.venv`，见下节。

## 跨平台快速开始（macOS / Windows）

需要 Python 3.12；推荐用 pyenv 管理，仓库根目录的 `.python-version` 固定为 `3.12.14`。

~~~bash
# macOS / Linux
pyenv install 3.12.14        # 只需一次
cd group9
./run.sh setup               # 创建 .venv 并安装依赖
./run.sh doctor              # 查看解释器、依赖和路径
./run.sh demo                # 无主板 MQTT v1 演示，浏览器自动打开
~~~

~~~powershell
# Windows PowerShell
pyenv install 3.12.14        # pyenv-win，只需一次
cd group9
.\run.ps1 setup
.\run.ps1 doctor
.\run.ps1 demo
~~~

常用子命令：

| 子命令 | 作用 |
|---|---|
| `setup` | 创建 `group9/.venv` 并安装 `02_后端源码/requirements.txt` |
| `demo` | 启动 MQTT v1 兼容演示；`--self-check` 只做环境自检，`--web-port`/`--mqtt-port` 可改端口 |
| `simulate --scenario normal\|offline\|retry` | 向已启动的兼容演示注入模拟偶遇 |
| `prepare` | 把后端、`web/` 与服务测试组装到 `.run/server` |
| `server init\|serve\|inspect\|ingest ...` | 透传参数给 `live_server.py`，首次使用前自动准备目录 |
| `bridge --runtime DIR ...` | 订阅公开 HiveMQ 的 v1 上报并写入该 runtime 的数据库 |
| `test --suite all\|compat\|compat-v1\|server\|node` | 兼容演示 pytest、服务端回环测试、可选前端 Node 用例 |

示例：初始化并启动独立的实板接收端。示例设备 ID 与端口同原交付说明；首次初始化会生成新的测试口令。启动后打开 [独立接收端](http://127.0.0.1:8876)，账号见 `.run/state/hardware/credentials.txt`（或自定义的 `--runtime` 目录）。

~~~bash
./run.sh server init --runtime .run/state/hardware \
  --device-a bump-349454276354 --device-b bump-c8c9a3e65ba4 \
  --web-port 8876 --mqtt-port 18884
./run.sh server serve --runtime .run/state/hardware --mqtt-host 127.0.0.1
~~~

~~~powershell
.\run.ps1 server init --runtime .run\state\hardware `
  --device-a bump-349454276354 --device-b bump-c8c9a3e65ba4 `
  --web-port 8876 --mqtt-port 18884
.\run.ps1 server serve --runtime .run\state\hardware --mqtt-host 127.0.0.1
~~~

**查看两枚实板的记录**，在实验电脑打开 [Open-Hardware.cmd](01_前端源码/双板实物/Open-Hardware.cmd)（Windows）或直接访问已在运行的 8875 接收端；其他电脑可用 `.\run.ps1 server ...` 初始化 8876 独立库。[实板网页说明](01_前端源码/双板实物/README.md)

接入两枚实板时，按[双板联调手册](07_课程与硬件文档/实物联调/双板联调手册.md)和[固件串口命令](07_课程与硬件文档/实物联调/实板固件说明.md)将接收端绑定到该次测试热点网卡的 IPv4，将相同地址、端口和新生成的网络配置写入设备。上面的回环模式用于本机浏览和离线自检。既有历史日志的机器路径指向当时的实验环境。

**走公开 HiveMQ 的固件**（`platformio.ini` 的 `mqtt` 环境）不会连到上面的本地接收端，它只把消息发到 `broker.hivemq.com:8883`；网页也不会自己订阅 HiveMQ。要让网页出现真板数据，接收端和桥接进程一起运行：

~~~bash
./run.sh server init --runtime .run/state/hardware-live \
  --device-a <板A的device_id> --device-b <板B的device_id>
./run.sh server serve --runtime .run/state/hardware-live --mqtt-host 127.0.0.1
./run.sh bridge --runtime .run/state/hardware-live
~~~

桥接日志里的“收到未登记设备 …”表示板上报的 `device_id` 与 `init` 时的两枚不一致，用日志中的 ID 重建 runtime 即可。`device_id` 可在串口输入 `status` 读取。公网主题没有访问控制，桥接只接受已登记 ID 的消息。

原有 Windows 启动脚本（`Start-Bump.cmd`、`Setup.ps1`、`Test.ps1` 等）保留文件名，内部改为调用根目录的 `run.ps1`，两种入口行为一致。

## 设备版本与材料边界

- `03_固件与构建/libraries/` 提供构建使用的六份 Arduino 库源码，包括与清单摘要吻合的项目版 ArduinoBLE。开发板平台是 `arduino:mbed_nano@4.6.0`，目标是 `arduino:mbed_nano:nanorp2040connect`。已具备 Arduino CLI 与该平台时，可从包根目录构建。
- `build/` 内的 UF2 和 BIN 来自最终安装制品，ELF 用于调试。对板读回记录见[最终安装核验](06_测试结果/0.5.8_最终实板/设备/installed_0.5.8-dual.json)。本包的制作与自检没有改动板上固件。
- 组员仓库固定在 [Carlos-CJC/project_bump 的 4f64bda](https://github.com/Carlos-CJC/project_bump/tree/4f64bda24074a90135c1ccea760b050f5a0a59b6)。`硬件部分.7z` 的红外交接另存；独立提供的较早接线说明保留原版，最终接线同时参考[红外交接 HARDWARE.md](08_历史版本与组员材料/组员红外交接/HARDWARE.md)。
- 实体外壳与卡片试装按当时决定跳过，包内有可打印设计。多人佩戴、距离与方向准确率没有现场统计。原始用户数据库、会话、实际网络口令、证书私钥、原板整片备份和工具缓存留在原机。组员配置文件中已有的 Wi-Fi 口令等已替换为占位符。
- [文件清单](文件清单.csv)逐项列出包内位置、来源、原件与交付副本的 SHA-256、字节数及调整说明，覆盖清单自身以外的所有文件。指定目录链接已为交付布局调整。历史材料中的原实验命令仍以各自版本的工作目录和环境为准。

~~~powershell
arduino-cli compile --fqbn arduino:mbed_nano:nanorp2040connect --libraries '.\03_固件与构建\libraries' '.\03_固件与构建\0.5.8-dual\BumpDualBadge'
~~~
