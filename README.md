# project_bump

Arduino Nano RP2040 Connect 偶遇徽章实验项目。固件通过 BLE 扫描发现同型号挂件，记录相遇并可选通过 MQTT 上报；配套文档描述协议、NINA 无线限制与后端计划。

## 目录

| 路径 | 内容 |
|---|---|
| `src/` | PlatformIO 固件源码 |
| `platformio.ini` | 固件构建环境；无线口令由 `BUMP_WIFI_SSID` / `BUMP_WIFI_PASS` 环境变量注入 |
| `docs/` | 接口、功能、MQTT 计划与 NINA 无线笔记 |
| `doc/` | 早期计划草稿 |
| `include/`、`lib/`、`test/` | PlatformIO 默认目录，当前仅含占位 README |
| `group9/` | 组 9 同学在固定提交 `4f64bda` 基础上完成的 0.5.8-dual 成果、后端、网页、测试与材料 |

## 组 9 成果与双端运行

`group9/` 内是同学交付的完整项目汇总（双板固件、FastAPI/aMQTT 后端、两套网页、测试证据）。入口：

```bash
# macOS / Linux
pyenv install 3.12.14        # 只需一次；仓库 .python-version 已固定
cd group9
./run.sh setup
./run.sh demo                # 无主板 MQTT v1 演示
./run.sh test                # 兼容演示 + 服务端测试
```

```powershell
# Windows PowerShell（pyenv-win）
pyenv install 3.12.14
cd group9
.\run.ps1 setup
.\run.ps1 demo
.\run.ps1 test
```

详细子命令、实板接收端启动与材料边界见 [group9/README.md](group9/README.md) 和 [group9/MERGE_NOTES.md](group9/MERGE_NOTES.md)。

## 固件构建

```bash
export BUMP_WIFI_SSID=你的热点
export BUMP_WIFI_PASS=你的口令
~/.platformio/penv/bin/pio run -e mqtt
```

`pio` 不在 PATH 时使用 `~/.platformio/penv/bin/pio`。
