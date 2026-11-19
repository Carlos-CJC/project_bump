# 两套网页入口

| 想做什么 | 打开 | 页面与数据 |
|---|---|---|
| 查看两枚实板的相遇、红外配对与邀请 | [实验电脑实物网页](双板实物/Open-Hardware.cmd) · [双板实物说明](双板实物/README.md) | [双板网页源码](双板实物/web)。实验电脑 8875 使用现场数据库。另机可启动独立接收端。 |
| 没有主板，体验相遇邀请或运行 MQTT v1 双端软件实验 | [启动兼容演示](Bump_EIE568_MQTT_v1_兼容演示/Start-Bump.cmd) · [模拟演示说明](Bump_EIE568_MQTT_v1_兼容演示/COMPAT_V1_GUIDE.md) | [兼容网页源码](Bump_EIE568_MQTT_v1_兼容演示/web)。账号、演示记录在兼容版自己的 `.runtime` 中。 |

Windows 可从文件资源管理器双击对应的 CMD 入口；macOS / Linux 在 `group9` 目录执行 `./run.sh demo` 启动兼容演示，实板接收端用 `./run.sh server serve ...`。两端首次使用都先运行 `setup`。进入登录页后，使用该服务自己的账号文件。
