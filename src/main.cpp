/**
 * @file main.cpp
 * @brief Bump 偶遇徽章 MVP —— 仅 BLE 广播（发射）与扫描（接收）。
 *
 * 目标硬件：Arduino Nano RP2040 Connect（板载 NINA-W102 蓝牙模块）
 * 依赖库  ：ArduinoBLE >= 2.0（要求 NINA 固件 >= 3.0.1）
 *
 * 工作方式
 *   1. 每台设备生成 6 字节匿名短 ID，周期性以「不可连接广播」发出。
 *   2. 广播内容：
 *        Service UUID : 0xA5B0（原型用 16-bit，正式版可换 128-bit）
 *        Manufacturer : company(2) | ver(1) | short_id(6) | flags(1) | tx_power(1)
 *   3. 同一 NINA 模块无法可靠地同时广播与扫描，因此采用时分复用：
 *        广播 600ms <-> 扫描 400ms，并加 ±100ms 随机抖动，避免两台设备相位锁定。
 *   4. 扫描阶段按 Service UUID 过滤，解析厂商数据；用 RSSI + 停留时间判定偶遇，
 *      命中后向串口输出一行 JSON 事件。
 *
 * 偶遇判定（参数见下方常量）
 *   - 强信号样本（RSSI >= -70dBm）在 10s 窗口内至少 5 次；
 *   - 从首个强信号样本起持续至少 3s；
 *   - 同一 short_id 在 8 小时冷却期内只上报一次。
 *
 * 隐私与 MVP 限制
 *   - 不记录、不打印对端 MAC，仅使用匿名 short_id。
 *   - short_id 目前每次上电随机生成；后续再做按天轮换 + 服务端映射。
 *   - 时间为开机后的相对时间（无 NTP），无本地持久化与上传。
 *
 * 串口输出（115200 波特率）
 *   [boot] ...                      启动信息与运行参数
 *   [status] ...                    每 10s 打印一次状态
 *   [scan] ...                      扫描采样（全局限流 500ms 一条，便于调试）
 *   {"event":"encounter",...}       偶遇事件（JSON，一行一条）
 */

#include <Arduino.h>
#include <ArduinoBLE.h>
#include <stdarg.h>

// ---------------------------------------------------------------------------
// BLE 协议常量
// ---------------------------------------------------------------------------

static const char* SERVICE_UUID = "a5b0";     // 16-bit 原型 UUID
static const uint16_t COMPANY_ID = 0xFFFF;    // 测试用未分配 Company ID

static const uint8_t PROTO_VER = 1;           // 广播协议版本
static const uint8_t FLAG_JOINABLE = 0x01;    // flags bit0：允许加好友
static const int8_t TX_POWER_CAL = -4;        // 标称发射功率(dBm)，尚未逐台校准

// 厂商数据中 Company ID 之后的载荷长度：ver(1) + short_id(6) + flags(1) + tx_power(1)
static const uint8_t MFG_PAYLOAD_LEN = 9;

// ---------------------------------------------------------------------------
// 偶遇判定参数
// ---------------------------------------------------------------------------

static const int RSSI_THRESHOLD = -70;            // dBm；弱于此值的样本不计入
static const uint8_t MIN_SAMPLES = 5;             // 窗口内所需强信号样本数
static const uint32_t DWELL_MS = 3000;            // 最短停留时间
static const uint32_t SAMPLE_WINDOW_MS = 10000;   // 样本计数窗口
static const uint32_t PEER_RESET_MS = 30000;      // 超过此间隔未见则重置该 peer 的窗口
static const uint32_t COOLDOWN_MS = 8UL * 3600UL * 1000UL;  // 同一 peer 去重冷却 8 小时

// 时分复用参数：广播/扫描时长与抖动
static const uint16_t ADV_MS = 600;
static const uint16_t SCAN_MS = 400;
static const int16_t PHASE_JITTER_MS = 100;

static const uint8_t MAX_PEERS = 16;              // 固定容量 peer 表，避免动态内存

// ---------------------------------------------------------------------------
// 状态结构
// ---------------------------------------------------------------------------

enum Phase { PHASE_ADV, PHASE_SCAN };

struct Peer {
  bool used;             // 槽位是否占用
  uint8_t id[6];         // 对端匿名短 ID
  uint8_t flags;         // 对端广播的 flags
  int8_t txPower;        // 对端广播的标称发射功率
  int16_t rssiLast;      // 最近一次 RSSI
  int16_t rssiAvg;       // 强信号样本的指数滑动平均
  uint32_t firstSeen;    // 当前计数窗口内首个强信号样本时间
  uint32_t lastSeen;     // 最近一次收到广播的时间（含弱信号）
  uint32_t windowStart;  // 当前计数窗口起点
  uint16_t samples;      // 当前窗口内强信号样本数
  uint32_t lastEventAt;  // 上次上报偶遇的时间；0 表示从未上报
};

static Peer peers[MAX_PEERS];
static Phase phase;                 // 当前时分复用所处阶段
static uint32_t phaseUntil;         // 当前阶段结束时间（millis）
static uint8_t shortId[6];          // 本机匿名短 ID
static uint32_t lastDebugPrint;     // [scan] 调试输出限流
static uint32_t lastStatusPrint;    // [status] 状态输出限流

// ---------------------------------------------------------------------------
// 通用工具
// ---------------------------------------------------------------------------

/**
 * printf 风格的串口日志。
 * mbed 内核的 arduino::UART 没有 printf，这里用 vsnprintf 统一格式化。
 * 注意：缓冲区 192 字节，超长内容会被截断。
 */
static void logf(const char* fmt, ...) {
  char buf[192];
  va_list args;
  va_start(args, fmt);
  vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  Serial.println(buf);
}

/**
 * 将 6 字节短 ID 转成 12 字符小写十六进制字符串。
 * 调用方缓冲区至少 13 字节（12 个字符 + '\0'）。
 */
static void formatId(const uint8_t* id, char* out) {
  static const char hex[] = "0123456789abcdef";
  for (int i = 0; i < 6; i++) {
    out[i * 2] = hex[id[i] >> 4];
    out[i * 2 + 1] = hex[id[i] & 0x0F];
  }
  out[12] = '\0';
}

/**
 * 生成匿名短 ID。
 * 熵源为 micros() 与两个悬空模拟引脚的噪声，因此每次上电都不同；
 * 这样原型阶段不暴露身份，但也就无法跨重启识别同一设备。
 */
static void makeShortId(uint8_t* id) {
  randomSeed(micros() ^ (analogRead(A0) << 16) ^ analogRead(A1));
  for (int i = 0; i < 6; i++) {
    id[i] = (uint8_t)random(0, 256);
  }
}

// ---------------------------------------------------------------------------
// BLE 广播 / 扫描状态切换
// ---------------------------------------------------------------------------

/**
 * 填充厂商数据并交给 ArduinoBLE。
 *
 * 关键细节：BLEAdvertisingData 保存的是传入的指针而不是拷贝，因此 payload
 * 必须是 static（生命周期贯穿整个程序），否则广播时读到的是已释放的栈内存。
 */
static void applyAdvertisingData() {
  static uint8_t payload[MFG_PAYLOAD_LEN];
  payload[0] = PROTO_VER;
  memcpy(&payload[1], shortId, 6);
  payload[7] = FLAG_JOINABLE;
  payload[8] = (uint8_t)TX_POWER_CAL;
  BLE.setManufacturerData(COMPANY_ID, payload, sizeof(payload));
}

/**
 * 进入广播阶段：先停止扫描（避免双角色冲突），再发布广播。
 * 阶段长度叠加随机抖动，降低两机长期同相、互相错过的概率。
 */
static void enterAdvPhase() {
  BLE.stopScan();
  applyAdvertisingData();
  BLE.advertise();
  phase = PHASE_ADV;
  phaseUntil = millis() + ADV_MS + random(-PHASE_JITTER_MS, PHASE_JITTER_MS + 1);
}

/**
 * 进入扫描阶段：先停止广播，再按 UUID 扫描。
 * withDuplicates = true 表示不去重，这样才能拿到同一设备的多次 RSSI 采样。
 */
static void enterScanPhase() {
  BLE.stopAdvertise();
  BLE.scanForUuid(SERVICE_UUID, true);
  phase = PHASE_SCAN;
  phaseUntil = millis() + SCAN_MS + random(-PHASE_JITTER_MS, PHASE_JITTER_MS + 1);
}

// ---------------------------------------------------------------------------
// Peer 表维护
// ---------------------------------------------------------------------------

/** 按短 ID 查找 peer，线性扫描（MAX_PEERS 很小，无需更复杂结构）。 */
static Peer* findPeer(const uint8_t* id) {
  for (uint8_t i = 0; i < MAX_PEERS; i++) {
    if (peers[i].used && memcmp(peers[i].id, id, 6) == 0) {
      return &peers[i];
    }
  }
  return nullptr;
}

/**
 * 查找 peer；不存在则分配槽位。
 * 槽位用完时淘汰「最久未见」的条目（LRU），密集环境下可能丢失旧 peer 的冷却信息。
 */
static Peer* findOrCreatePeer(const uint8_t* id, uint32_t now) {
  Peer* p = findPeer(id);
  if (p) {
    return p;
  }

  Peer* slot = nullptr;
  for (uint8_t i = 0; i < MAX_PEERS; i++) {
    if (!peers[i].used) {
      slot = &peers[i];
      break;
    }
  }
  if (!slot) {
    slot = &peers[0];
    for (uint8_t i = 1; i < MAX_PEERS; i++) {
      // 无符号时间差转成有符号再比较，以正确处理 millis() 回绕
      if ((int32_t)(peers[i].lastSeen - slot->lastSeen) < 0) {
        slot = &peers[i];
      }
    }
  }

  memset(slot, 0, sizeof(*slot));
  slot->used = true;
  memcpy(slot->id, id, 6);
  slot->firstSeen = now;
  slot->windowStart = now;
  return slot;
}

// ---------------------------------------------------------------------------
// 偶遇判定与上报
// ---------------------------------------------------------------------------

/**
 * 输出一条偶遇事件。
 * duration_s 使用整数秒：newlib-nano 默认不链接浮点 printf，避免 %f 输出为空。
 */
static void emitEncounter(const Peer* p, uint32_t now) {
  char id[13];
  formatId(p->id, id);
  unsigned long durationS = (p->lastSeen - p->firstSeen) / 1000;
  logf("{\"event\":\"encounter\",\"peer\":\"%s\",\"rssi_avg\":%d,\"rssi_last\":%d,"
       "\"duration_s\":%lu,\"samples\":%u,\"flags\":%u,\"uptime_s\":%lu}",
       id, p->rssiAvg, p->rssiLast, durationS, (unsigned)p->samples,
       (unsigned)p->flags, (unsigned long)(now / 1000));
}

/**
 * 处理一次扫描到的广播样本。
 *
 * 判定流程：
 *   1. 弱信号样本只更新 lastSeen，不计入停留与样本数（防止人体遮挡造成跳变）；
 *   2. 若距上次收到超过 PEER_RESET_MS，重置计数窗口；
 *   3. 强信号样本累加计数并更新 RSSI 滑动平均；
 *   4. 样本数、停留时间、平均 RSSI 同时达标，且不在冷却期内，则上报一次。
 *
 * 说明：lastEventAt 在窗口重置时有意保留，使 8 小时去重在短暂断连后仍生效。
 */
static void onSample(const uint8_t* id, int rssi, uint8_t flags, int8_t txPower, uint32_t now) {
  Peer* p = findOrCreatePeer(id, now);
  p->flags = flags;
  p->txPower = txPower;
  p->rssiLast = rssi;

  // 注意：先判断是否超时，再更新 lastSeen
  bool active = (int32_t)(now - p->lastSeen) <= (int32_t)PEER_RESET_MS;
  p->lastSeen = now;
  if (!active) {
    p->firstSeen = now;
    p->windowStart = now;
    p->samples = 0;
    p->rssiAvg = 0;
  }

  if (rssi < RSSI_THRESHOLD) {
    return;
  }

  // 计数窗口过期，或本窗口尚无样本：重新开始统计
  if (p->samples == 0 || (int32_t)(now - p->windowStart) > (int32_t)SAMPLE_WINDOW_MS) {
    p->windowStart = now;
    p->firstSeen = now;
    p->samples = 0;
    p->rssiAvg = 0;
  }

  p->samples++;
  // 指数滑动平均（1/4 新样本），平滑瞬时 RSSI 波动
  p->rssiAvg = (p->samples == 1) ? rssi : (p->rssiAvg * 3 + rssi) / 4;

  uint32_t dwell = now - p->firstSeen;
  bool cooledDown = (p->lastEventAt == 0) ||
                    ((int32_t)(now - p->lastEventAt) >= (int32_t)COOLDOWN_MS);
  if (p->samples >= MIN_SAMPLES && dwell >= DWELL_MS &&
      p->rssiAvg >= RSSI_THRESHOLD && cooledDown) {
    p->lastEventAt = now;
    emitEncounter(p, now);
  }
}

/**
 * 排空 GAP 的扫描结果队列。
 * 只接受本公司 ID + 匹配版本号的厂商数据帧；其余帧（含其他 BLE 设备）直接忽略。
 * [scan] 调试输出做 500ms 全局限流，避免高频广播刷屏拖慢主循环。
 */
static void handleAdvertisements(uint32_t now) {
  for (;;) {
    BLEDevice device = BLE.available();
    if (!device) {
      break;
    }

    // 缓冲区包含 2 字节 Company ID + 载荷
    uint8_t buf[MFG_PAYLOAD_LEN + 2];
    int len = device.manufacturerData(buf, sizeof(buf));
    if (len < (int)sizeof(buf)) {
      continue;
    }
    if (buf[0] != (COMPANY_ID & 0xFF) || buf[1] != (COMPANY_ID >> 8)) {
      continue;
    }
    if (buf[2] != PROTO_VER) {
      continue;
    }

    const uint8_t* peerId = &buf[3];
    uint8_t flags = buf[9];
    int8_t txPower = (int8_t)buf[10];
    int rssi = device.rssi();

    onSample(peerId, rssi, flags, txPower, now);

    if ((int32_t)(now - lastDebugPrint) >= 500) {
      lastDebugPrint = now;
      char id[13];
      formatId(peerId, id);
      Peer* p = findPeer(peerId);
      int avg = (p && p->samples > 0) ? p->rssiAvg : rssi;
      logf("[scan] peer=%s rssi=%d avg=%d n=%u flags=0x%02x tx=%d",
           id, rssi, avg, p ? (unsigned)p->samples : 0u, flags, txPower);
    }
  }
}

/** 每 10s 打印状态：当前阶段、本机短 ID、最近 30s 内活跃的 peer 数、运行时长。 */
static void printStatus(uint32_t now) {
  uint8_t peerCount = 0;
  for (uint8_t i = 0; i < MAX_PEERS; i++) {
    if (peers[i].used &&
        (int32_t)(now - peers[i].lastSeen) <= (int32_t)PEER_RESET_MS) {
      peerCount++;
    }
  }
  char id[13];
  formatId(shortId, id);
  logf("[status] phase=%s self=%s peers=%u uptime=%lus",
       phase == PHASE_ADV ? "adv" : "scan", id, (unsigned)peerCount,
       (unsigned long)(now / 1000));
}

// ---------------------------------------------------------------------------
// Arduino 入口
// ---------------------------------------------------------------------------

void setup() {
  // 等待 USB 串口就绪，但最多 2s，保证脱离电脑也能正常启动
  Serial.begin(115200);
  uint32_t waitStart = millis();
  while (!Serial && (millis() - waitStart) < 2000) {
    delay(10);
  }

  makeShortId(shortId);

  if (!BLE.begin()) {
    Serial.println("[fatal] BLE.begin() failed. Check NINA firmware >= 3.0.1.");
    for (;;) {
      delay(1000);
    }
  }

  BLE.setLocalName("Bump");
  BLE.setAdvertisedServiceUuid(SERVICE_UUID);
  BLE.setConnectable(false);      // 不可连接广播：只广播、不建立连接
  BLE.setAdvertisingInterval(100);

  char id[13];
  formatId(shortId, id);
  logf("[boot] Bump BLE MVP: self=%s uuid=%s rssi_thr=%d min_samples=%u "
       "dwell=%ums adv=%ums scan=%ums",
       id, SERVICE_UUID, RSSI_THRESHOLD, MIN_SAMPLES,
       (unsigned)DWELL_MS, (unsigned)ADV_MS, (unsigned)SCAN_MS);

  enterAdvPhase();
}

void loop() {
  BLE.poll();  // 让库处理 HCI 事件

  uint32_t now = millis();

  // 时分复用：阶段到期后切换角色（无符号差值转有符号，兼容 millis() 回绕）
  if ((int32_t)(now - phaseUntil) >= 0) {
    if (phase == PHASE_ADV) {
      enterScanPhase();
    } else {
      enterAdvPhase();
    }
  }

  // 只有扫描阶段才消费扫描结果
  if (phase == PHASE_SCAN) {
    handleAdvertisements(now);
  }

  if ((int32_t)(now - lastStatusPrint) >= 10000) {
    lastStatusPrint = now;
    printStatus(now);
  }
}
