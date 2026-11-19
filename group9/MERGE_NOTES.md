# 合并说明 · 组 9 成果并入 project_bump

## 来源

- 交付包：`EIE568_Group9_Bump_项目汇总_0.5.8-dual_2026-11-19.zip`（仅本地保存，未入库）。
- 包内自述的主版本：双板固件 **0.5.8-dual**，两枚 Arduino Nano RP2040 Connect 实装读回核验。
- 同学声明其工作基于 `Carlos-CJC/project_bump` 的固定提交 `4f64bda24074a90135c1ccea760b050f5a0a59b6`，与主仓库的协议常量、BLE 帧格式和 MQTT v1 契约一致。

## 目录取舍

保留交付包的中文目录名与相对链接，整体放在 `group9/` 下：

| 保留 | 说明 |
|---|---|
| `01_前端源码` | 两套网页：实板 8875/8876 与 MQTT v1 兼容演示 8765/18883 |
| `02_后端源码` | FastAPI + aMQTT + SQLite，入口 `live_server.py` |
| `03_固件与构建` | 0.5.8-dual 源码、无线库源码、UF2/BIN/ELF 与构建清单 |
| `04_测试程序` | 服务端回环测试、双板/单板实验脚本 |
| `05_测试数据` | 串口与模拟输入原始数据 |
| `06_测试结果` | 结论、截图、事件核对 JSON |
| `07_课程与硬件文档` | 课程原件、接线、联调与验收说明 |
| `08_历史版本与组员材料` | 早期原型、单板/双板历史固件、红外与 docs.zip 交接 |

未纳入仓库：

- `09_离线运行依赖`：约几十 MB 的 Windows x64 便携 Python；仓库改用 pyenv + `.venv`。
- `08_.../组员GitHub_project_bump`、`08_.../组员红外交接/project_bump`：与主仓库重复的源码树。
- `07/提案与优化`、`07/四人交接`、`07/单板课程进度报告`：与合并目标无关的过程材料。
- `.DS_Store` 等系统文件。

`文件清单.csv` 保留原样，用于对照交付包内文件；被剔除目录的条目仍可据此追溯。清单生成于合并之前，不包含本分支新增的 `run.py`、`run.sh`、`run.ps1`、`MERGE_NOTES.md`，也不包含在其之后被改写的启动脚本。

## 凭据处理

- 交付包本身已把 Wi-Fi 口令等替换为占位符。
- 主仓库 `platformio.ini` 中曾硬编码真实口令。本分支改为读取环境变量：

  ```ini
  -D BUMP_WIFI_SSID=\"${sysenv.BUMP_WIFI_SSID}\"
  -D BUMP_WIFI_PASS=\"${sysenv.BUMP_WIFI_PASS}\"
  ```

  macOS/Linux 用 `export`，Windows 用 `$env:` 注入；未设置时宏为空，普通编译不受影响。
- **注意**：旧提交历史里仍可检索到原口令。若仓库公开，应在平台上轮换该 Wi-Fi 密码，必要时重写历史。

## macOS / Windows 双端适配

- 根目录与 `group9/` 均添加 `.python-version`（`3.12.14`），供 pyenv / pyenv-win 解析。
- `group9/run.py`：仅用标准库的启动器，负责创建 `.venv`、安装依赖、组装服务目录、透传 `live_server.py` 参数、运行各测试套件。
- `group9/run.sh`、`group9/run.ps1`：两端入口，依次尝试 `.venv`、`pyenv exec python`、系统 Python。
- 原有 Windows 启动脚本保留文件名，改为转发到统一入口：
  `Start-Bump.cmd`、`Start-LocalWeb.ps1`、`Setup.ps1`、`Start.ps1`、`Run-Demo.ps1`、`Test.ps1`、`Test-V1.ps1`、`双板实物/Start-Hardware.ps1`、`Prepare-Local.ps1`。
- `.gitattributes` 固定脚本换行，避免两端互相改写；二进制制品标记为 binary。
- `.gitignore` 忽略 `.venv/`、`.run/`、运行时数据库/口令/证书与交付压缩包。

## 与交付清单的对照

按 `文件清单.csv` 逐个复核：

- 840 个文件与交付哈希完全一致；
- 13 个文件为双端适配有意改写：两套 README、`COMPAT_V1_GUIDE.md`、`LOCAL_WEB_GUIDE.md`、`Prepare-Local.ps1`、`Start-Hardware.ps1`，以及兼容演示下的 `Setup.ps1`、`Start.ps1`、`Start-Bump.cmd`、`Start-LocalWeb.ps1`、`Run-Demo.ps1`、`Test.ps1`、`Test-V1.ps1`；
- 53 条清单记录指向未纳入仓库的目录（`09_离线运行依赖`、重复源码树、过程材料等）。

## 本机验证（macOS，Python 3.12.14）

| 项目 | 结果 |
|---|---|
| `./run.sh setup` | `.venv` 创建成功，依赖安装完成 |
| `./run.sh test --suite compat` | 200 passed，2 skipped（缺 Node） |
| `./run.sh test --suite server` | smoke / matching / connection_stages / upload_recovery / sync_status 全部通过 |
| `./run.sh demo --self-check` | `{"ok": true, ...}` |
| 兼容演示实际启动 | 首页 HTTP 200 |
| `server init` + `server serve` | `/api/health` 返回 `ready=true` |

未验证：Windows 实机、Node 前端用例（本机无 Node）、Arduino 固件重新编译与实板烧录。
