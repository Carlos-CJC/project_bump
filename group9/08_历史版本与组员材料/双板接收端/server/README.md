# Bump 双板联调接收端

本接收端独立于桌面软件演示。它复用兼容版的 MQTT v1 校验、时间换算、短 ID 匹配和网页会话授权。板端 MQTT 3.1.1 明文 TCP 只绑定指定的临时局域网接口。网页只监听 127.0.0.1。每块板的账号与客户端 ID 都是该板的 device_id，密码各自独立。

## 启动

在项目根目录的 PowerShell 执行这组命令，并将私有目录、两块设备 ID 和地址换成本次记录中的值。

~~~powershell
$python = '.\Bump_EIE568_MQTT_v1_兼容演示\.local-tools\python312-ac9f8f76cdc4\python.exe'
$receiver = '.\2026-11-18_双板联调\server\live_server.py'
$runtime = '.\2026-11-18_双板联调\private\receiver'
$env:PYTHONDONTWRITEBYTECODE = '1'
& $python -X utf8 $receiver init --runtime $runtime --device-a 'bump-<板A的MAC>' --device-b 'bump-<板B的MAC>'
& $python -X utf8 $receiver serve --runtime $runtime --mqtt-host 192.168.137.1
~~~

默认 MQTT 端口为 18883，网页为 [http://127.0.0.1:8875](http://127.0.0.1:8875)。首次初始化后，在当前联调目录的 credentials.txt 读取 Alice、Bob、Charlie 网页密码。live-config.json 中的 mqtt.accounts 为板端编译时配置提供独立秘密。两份文件均留在本地私有目录。初始化默认把 A30F 登记为 Alice 的挂件 A，C371 登记为 Bob 的挂件 B。Charlie 未分配挂件。停用服务时在它的窗口按 Ctrl+C。

### 验收与诊断

- 给板端发布的 status 带上当前开机的 self_short_id。另一端只发在线 status 就可补齐短 ID 映射。缺少注册或同一短 ID 对应多份候选时，网页记为身份待匹配。15 分钟内对向观察互相指向且来自同一传输方式时，显示双方记录已匹配。短连接结束后设备通常会上报离线。
- encounter 与 IR 使用 QoS 1 上行。每块板订阅自己的 bump/v1/device/{device_id}/ack。入库事务结束后产生逐事件回执，重发相同事件得到 duplicate。事件同号但内容冲突时不给应用回执。板端收到的 PUBACK、服务入库、板端应用 ACK 分别观察。
- IR 需要两块注册板在同一种传输方式下于 120 秒内各自上报对方向的代码。服务以第一条到达的事件所属账号为发件人，只生成一份待同意请求。资料在接收方同意后可见，联系撤销后重新隐藏。已有待回应或已接受的关系会沿用。
- 使用 **inspect --runtime** 查看短 ID、设备状态、偶遇、IR、请求及实际传输方式。使用 **ingest --runtime --device --kind** 时，从标准输入接收一条已经确认来自指定板的 JSON，标为 USB 串口中转。它不会向 MQTT 发布应用 ACK。每个事件保存首次提交的传输方式，后续传输重试不更改原记录的来源。
- 本地演练可用同一包内解释器执行 smoke_test.py。它在回环临时端口和新数据库中发送合成 MQTT 报文，覆盖板间主题隔离、入库回执和去重、短 ID 回填、互遇、红外邀请、同意、撤销和第三人隔离。实板与浏览器当次验证由联调记录另行保存。


## 热点关闭后查看已有记录

先停止监听局域网的接收端。在同一个 PowerShell 目录中，沿用先前的解释器、接收端脚本和私有运行目录启动一次。无需重新初始化或打开热点。

~~~powershell
& $python -X utf8 $receiver serve --runtime $runtime --mqtt-host 127.0.0.1
~~~

浏览器仍访问 [http://127.0.0.1:8875](http://127.0.0.1:8875)。用原来的联调账号登录，同一数据库中的相遇、联系邀请和处理状态仍可查看。此时 MQTT 仅接受本机连接，挂件需要待到下一次局域网联调才能继续直传。

## 本机软件验证

2026-11-18 使用兼容演示包内 Python 在临时回环地址运行 smoke_test.py。合成客户端收到 stored 和 duplicate，伪造跨板上报与伪造 ACK 均未通过。单边记录在对端发注册状态后识别对方，等待反向偶遇后标为双方记录。两条反向红外事件只生成一份待回应邀请，接收方同意后资料可见，撤销后资料隐藏。Charlie 看不到或操作不了两块板的记录与邀请。所有样例在临时测试库运行。

同一临时运行目录随后在 127.0.0.1 重新启动，通过 Alice 原密码登录，仍可看见原来的已匹配时间线以及待回应和已撤销的邀请。网页脚本语法和引用的 24 个页面元素均已核对。
