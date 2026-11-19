/*
 * 近场配对（IR Near-Field Pairing）
 * 目标板：Arduino Nano RP2040 Connect
 *
 * 硬件（详见 HARDWARE.md）：
 *   白色按钮  D2  （按下 = LOW）
 *   蓝色按钮  D3  （按下 = LOW）
 *   IR 发射   D9
 *   IR 接收   D4  （HX1838 / VS1838B，输出空闲为 HIGH，收到 38 kHz 时为 LOW）
 *   板载 RGB  LED 通过 NINA 模块控制（LEDR / LEDG / LEDB）
 *
 * 交互：
 *   蓝色按钮：从 IDLE 进入近场配对（蓝色慢闪）
 *   白色按钮：随时停止配对 / 解除配对（白色常亮）
 *   收到对方 HELLO：青色快闪（正在握手）
 *   配对成功：绿色常亮
 *   搜索超时：红色闪 4 下，回到 IDLE
 *
 * 双向握手：
 *   A: HELLO(广播)  ->
 *   B: ACK(目标 A)  ->
 *   A: DONE(目标 B) ->  （A 收到 ACK 后进入 PAIRED，并重复 DONE 3 次）
 *   B 收到 DONE 后进入 PAIRED
 *
 * 如果 DONE 丢失，B 会继续重发 ACK；A 在 PAIRED 状态下听到 ACK 会补发 DONE。
 *
 * 依赖库（Arduino IDE 库管理器）：
 *   - IRremote      >= 4.7
 *   - WiFiNINA      >= 2.1（只用于板载 RGB LED）
 */

#include <WiFiNINA.h>        // 板载 RGB LED 的 NinaPin / digitalWrite
#include <hardware/flash.h>  // flash_get_unique_id()

// ---------------------------------------------------------------------------
// IRremote 编译选项必须在 include 之前定义
// ---------------------------------------------------------------------------
#define NO_LED_FEEDBACK_CODE   // 不要占用 D13（D13 同时是 NINA SPI 的 SCK）
#define DECODE_DISTANCE_WIDTH  // 启用通用 pulse-distance 解码器
#define IR_SEND_PIN 9          // 发射脚 D9
#include <IRremote.hpp>
#include "Nearfield.h"

namespace nearfield {

// ---------------------------------------------------------------------------
// 引脚
// ---------------------------------------------------------------------------
static const uint8_t PIN_BTN_WHITE = 2;
static const uint8_t PIN_BTN_BLUE  = 3;
static const uint8_t PIN_IR_RX     = 4;

// ---------------------------------------------------------------------------
// 协议常量
// ---------------------------------------------------------------------------
static const uint8_t  PROTO_VERSION = 1;
static const uint8_t  MSG_HELLO     = 1;
static const uint8_t  MSG_ACK       = 2;
static const uint8_t  MSG_DONE      = 3;
static const uint8_t  MSG_BYE       = 4;
static const uint16_t ID_BROADCAST  = 0xFFFF;
static const uint8_t  FRAME_BITS    = 48;

// 48 bit 帧布局（LSB first 发送）：
//   bit  0..3   版本号 (1)
//   bit  4..7   消息类型 (HELLO/ACK/DONE/BYE)
//   bit  8..23  发送方机器码 (16 bit)
//   bit 24..39  目标机器码 (16 bit，0xFFFF = 广播)
//   bit 40..47  CRC-8（覆盖低 40 bit）

// NEC 风格时序；HX1838 需要 9 ms 的 AGC 引导脉冲
static const uint16_t IR_HEADER_MARK  = 9000;
static const uint16_t IR_HEADER_SPACE = 4500;
static const uint16_t IR_BIT_MARK     = 560;
static const uint16_t IR_ONE_SPACE    = 1690;
static const uint16_t IR_ZERO_SPACE   = 560;

// ---------------------------------------------------------------------------
// 时间参数（毫秒）
// ---------------------------------------------------------------------------
static const uint32_t DEBOUNCE_MS           = 25;
static const uint32_t SEARCH_GAP_MIN_MS     = 260;
static const uint32_t SEARCH_GAP_MAX_MS     = 700;
static const uint32_t ACK_INTERVAL_MS       = 320;
static const uint32_t CONFIRM_TIMEOUT_MS    = 8000;
static const uint32_t SEARCH_TIMEOUT_MS     = 120000;
static const uint32_t DONE_REPEAT_INTERVAL  = 280;
static const uint8_t  DONE_REPEATS          = 3;
static const uint32_t TX_GUARD_MS           = 30;   // 发射后等 AGC 恢复
static const uint32_t ERROR_BLINK_MS        = 1200;

// ---------------------------------------------------------------------------
// 状态
// ---------------------------------------------------------------------------
enum PairState : uint8_t {
  ST_IDLE,
  ST_SEARCH,
  ST_CONFIRM,
  ST_PAIRED
};

static PairState state = ST_IDLE;
static uint16_t myId = 0;
static uint16_t peerId = 0;
static uint32_t stateSince = 0;
static uint32_t nextTxAt = 0;
static uint8_t  doneLeft = 0;
static uint32_t errorStart = 0;
static uint32_t errorUntil = 0;
static bool     errorActive = false;

// ---------------------------------------------------------------------------
// 按钮
// ---------------------------------------------------------------------------
struct Button {
  uint8_t pin;
  int stable;        // 消抖后的电平：HIGH = 松开，LOW = 按下
  int lastRaw;
  uint32_t lastChange;
  bool pressedEvent;
};

static Button btnWhite = {PIN_BTN_WHITE, HIGH, HIGH, 0, false};
static Button btnBlue  = {PIN_BTN_BLUE,  HIGH, HIGH, 0, false};

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------
static inline bool timeReached(uint32_t t) {
  return (int32_t)(millis() - t) >= 0;
}

static uint8_t crc8(const uint8_t *data, uint8_t len) {
  uint8_t crc = 0x00;
  for (uint8_t i = 0; i < len; ++i) {
    crc ^= data[i];
    for (uint8_t b = 0; b < 8; ++b) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

static const char *stateName(PairState s) {
  switch (s) {
    case ST_IDLE:    return "IDLE";
    case ST_SEARCH:  return "SEARCH";
    case ST_CONFIRM: return "CONFIRM";
    case ST_PAIRED:  return "PAIRED";
  }
  return "?";
}

static const char *typeName(uint8_t t) {
  switch (t) {
    case MSG_HELLO: return "HELLO";
    case MSG_ACK:   return "ACK";
    case MSG_DONE:  return "DONE";
    case MSG_BYE:   return "BYE";
  }
  return "?";
}

static void printHex16(uint16_t v) {
  static const char hex[] = "0123456789ABCDEF";
  Serial.print(hex[(v >> 12) & 0x0F]);
  Serial.print(hex[(v >> 8) & 0x0F]);
  Serial.print(hex[(v >> 4) & 0x0F]);
  Serial.print(hex[v & 0x0F]);
}

static void printHex8(uint8_t v) {
  static const char hex[] = "0123456789ABCDEF";
  Serial.print(hex[(v >> 4) & 0x0F]);
  Serial.print(hex[v & 0x0F]);
}

static void printHex32(uint32_t v) {
  static const char hex[] = "0123456789ABCDEF";
  for (int8_t i = 28; i >= 0; i -= 4) {
    Serial.print(hex[(v >> i) & 0x0F]);
  }
}

static void printHex64(uint64_t v) {
  printHex32((uint32_t)(v >> 32));
  printHex32((uint32_t)v);
}

static void dbg(const char *msg) {
  if (!Serial) return;
  Serial.println(msg);
}

static void dbgTx(uint8_t type, uint16_t target) {
  if (!Serial) return;
  Serial.print("TX ");
  Serial.print(typeName(type));
  Serial.print(" -> ");
  if (target == ID_BROADCAST) {
    Serial.print("BCAST");
  } else {
    Serial.print("0x");
    printHex16(target);
  }
  Serial.println();
}

static void dbgRx(uint8_t type, uint16_t sender, uint16_t target) {
  if (!Serial) return;
  Serial.print("RX ");
  Serial.print(typeName(type));
  Serial.print(" from 0x");
  printHex16(sender);
  if (target != ID_BROADCAST) {
    Serial.print(" to 0x");
    printHex16(target);
  }
  Serial.println();
}

static void dbgPeer(const char *what, uint16_t peer) {
  if (!Serial) return;
  Serial.print(what);
  Serial.print(" peer=0x");
  printHex16(peer);
  Serial.print(" state=");
  Serial.println(stateName(state));
}

// ---------------------------------------------------------------------------
// 板载 RGB LED
// 实测这块 Nano RP2040 Connect：digitalWrite(LEDx, HIGH) = 亮，LOW = 灭。
// WiFiNINA 的 NinaPin 封装内部还会反相一次，所以不要按普通“共阳 LOW 亮”的直觉写。
// ---------------------------------------------------------------------------
static bool ledSuspended = false;
static int8_t lastLed = -1;

static void setLed(bool r, bool g, bool b) {
  if (ledSuspended) return;
  int8_t v = (r ? 1 : 0) | (g ? 2 : 0) | (b ? 4 : 0);
  if (v == lastLed) return;       // 避免频繁走 NINA SPI
  lastLed = v;
  digitalWrite(LEDR, r ? HIGH : LOW);
  digitalWrite(LEDG, g ? HIGH : LOW);
  digitalWrite(LEDB, b ? HIGH : LOW);
}

// ---------------------------------------------------------------------------
// 机器码：由 RP2040 芯片唯一 ID 哈希成 16 bit
// ---------------------------------------------------------------------------
static uint8_t chipUid[8] = {0};
static bool bannerPrinted = false;

static uint16_t computeMachineId() {
  flash_get_unique_id(chipUid);

  uint32_t h = 2166136261u;    // FNV-1a
  for (uint8_t i = 0; i < 8; ++i) {
    h ^= chipUid[i];
    h *= 16777619u;
  }

  uint16_t id = (uint16_t)(h ^ (h >> 16));
  if (id == 0x0000) id = 0x0001;
  if (id == 0xFFFF) id = 0xFFFE;  // 0xFFFF 留给广播
  return id;
}
// ---------------------------------------------------------------------------
// 帧编码 / 解码
// ---------------------------------------------------------------------------
static uint64_t buildFrame(uint8_t type, uint16_t target) {
  uint64_t f = 0;
  f |= (uint64_t)(PROTO_VERSION & 0x0F);
  f |= (uint64_t)(type & 0x0F) << 4;
  f |= (uint64_t)myId << 8;
  f |= (uint64_t)target << 24;

  uint8_t bytes[5];
  for (uint8_t i = 0; i < 5; ++i) {
    bytes[i] = (uint8_t)(f >> (8 * i));
  }
  f |= (uint64_t)crc8(bytes, 5) << 40;
  return f;
}

static bool decodeFrame(uint64_t raw, uint8_t &type, uint16_t &sender, uint16_t &target) {
  if ((raw & 0x0F) != PROTO_VERSION) return false;

  type   = (uint8_t)((raw >> 4) & 0x0F);
  sender = (uint16_t)((raw >> 8) & 0xFFFF);
  target = (uint16_t)((raw >> 24) & 0xFFFF);

  if (type < MSG_HELLO || type > MSG_BYE) return false;
  if (sender == 0 || sender == myId || sender == ID_BROADCAST) return false;  // 过滤自己和非法地址

  uint8_t bytes[5];
  for (uint8_t i = 0; i < 5; ++i) {
    bytes[i] = (uint8_t)(raw >> (8 * i));
  }
  if (((raw >> 40) & 0xFF) != crc8(bytes, 5)) return false;

  return true;
}

// ---------------------------------------------------------------------------
// 收发
// ---------------------------------------------------------------------------
static bool channelBusy() {
  // 在 3 ms 内看到 >=250 us 的低电平，说明空中有 38 kHz 红外帧。
  // 接收头空闲为 HIGH，载波 mark 期间为 LOW。
  uint32_t start = micros();
  uint32_t lowSince = 0;

  while ((uint32_t)(micros() - start) < 3000UL) {
    if (digitalRead(PIN_IR_RX) == LOW) {
      if (lowSince == 0) lowSince = micros();
      if ((uint32_t)(micros() - lowSince) >= 250UL) return true;
    } else {
      lowSince = 0;
    }
  }
  return false;
}

static void sendFrame(uint8_t type, uint16_t target) {
  uint64_t frame = buildFrame(type, target);

  // 半双工：发射时先关接收，避免自己的强信号进入解码器
  IrReceiver.stop();

  IrSender.sendPulseDistanceWidth(38,
                                  IR_HEADER_MARK, IR_HEADER_SPACE,
                                  IR_BIT_MARK, IR_ONE_SPACE,   // 1
                                  IR_BIT_MARK, IR_ZERO_SPACE,  // 0
                                  frame, FRAME_BITS,
                                  0, 0, 0);                    // LSB first, 不重复

  // 等接收头 AGC 从本机强光中恢复，再重新开始接收
  delay(TX_GUARD_MS);
  IrReceiver.start();

  dbgTx(type, target);
}

static bool trySend(uint8_t type, uint16_t target) {
  if (channelBusy()) return false;
  sendFrame(type, target);
  return true;
}

// 让下一次发送不晚于 now + delayMs
static void scheduleEarlier(uint32_t delayMs) {
  uint32_t t = millis() + delayMs;
  if (timeReached(nextTxAt) || (int32_t)(t - nextTxAt) < 0) {
    nextTxAt = t;
  }
}

// ---------------------------------------------------------------------------
// 状态机
// ---------------------------------------------------------------------------
static void enterState(PairState s) {
  state = s;
  stateSince = millis();

  switch (s) {
    case ST_IDLE:
      peerId = 0;
      doneLeft = 0;
      break;

    case ST_SEARCH:
      peerId = 0;
      doneLeft = 0;
      nextTxAt = stateSince + random(80, 350);
      break;

    case ST_CONFIRM:
      nextTxAt = stateSince + random(80, 220);
      break;

    case ST_PAIRED:
      // 调用者负责设置 doneLeft / nextTxAt
      break;
  }
}

static void showError() {
  errorStart = millis();
  errorUntil = errorStart + ERROR_BLINK_MS;
  errorActive = true;
}

static void handleFrame(uint8_t type, uint16_t sender, uint16_t target) {
  // 目标必须是本机或广播
  if (target != myId && target != ID_BROADCAST) return;

  uint32_t now = millis();

  switch (type) {
    case MSG_HELLO:
      if (target != ID_BROADCAST) return;

      if (state == ST_SEARCH) {
        // 第一次听到对方：进入握手
        peerId = sender;
        enterState(ST_CONFIRM);
        nextTxAt = now + random(80, 220);
        dbgPeer("HELLO", sender);

      } else if (state == ST_CONFIRM) {
        // 对方还在发 HELLO，尽快回 ACK
        if (sender == peerId) {
          scheduleEarlier(random(60, 160));
        }

      } else if (state == ST_PAIRED) {
        // 对方可能重启/丢了状态，补一个 DONE
        if (sender == peerId) {
          doneLeft = 1;
          scheduleEarlier(80);
        }
      }
      break;

    case MSG_ACK:
      if (target != myId) return;
      if (state == ST_CONFIRM && sender != peerId) return;

      if (state == ST_SEARCH || state == ST_CONFIRM) {
        // 对方确认收到了我 -> 本机配对成功，发 DONE 让对方也确认
        peerId = sender;
        enterState(ST_PAIRED);
        doneLeft = DONE_REPEATS;
        nextTxAt = now + 50;
        dbgPeer("PAIRED (ACK)", sender);

      } else if (state == ST_PAIRED && sender == peerId) {
        // 对方没收到 DONE，再补一次
        doneLeft = 1;
        scheduleEarlier(80);
      }
      break;

    case MSG_DONE:
      if (target != myId) return;
      if (state == ST_CONFIRM && sender != peerId) return;

      if (state == ST_SEARCH || state == ST_CONFIRM) {
        peerId = sender;
        enterState(ST_PAIRED);
        doneLeft = 0;
        dbgPeer("PAIRED (DONE)", sender);
      }
      break;

    case MSG_BYE:
      if (target != ID_BROADCAST && target != myId) return;
      if (sender == peerId && state != ST_IDLE) {
        errorActive = false;
        enterState(ST_IDLE);
        dbg("Peer pressed white; pairing off");
      }
      break;
  }
}

static void handleIr() {
  if (!IrReceiver.decode()) return;

  auto &d = IrReceiver.decodedIRData;
  static uint32_t lastOtherPrint = 0;

  bool isOurFormat = (d.protocol == PULSE_DISTANCE &&
                      d.numberOfBits == FRAME_BITS &&
                      !(d.flags & IRDATA_FLAGS_WAS_OVERFLOW));

  if (isOurFormat) {
    uint8_t type;
    uint16_t sender, target;

    if (decodeFrame(d.decodedRawData, type, sender, target)) {
      dbgRx(type, sender, target);
      handleFrame(type, sender, target);
    } else if (Serial && (uint32_t)(millis() - lastOtherPrint) > 500) {
      lastOtherPrint = millis();
      Serial.print("IR 48-bit reject raw=0x");
      printHex64(d.decodedRawData);
      Serial.println();
    }
  } else if (Serial && d.numberOfBits > 0 &&
             (uint32_t)(millis() - lastOtherPrint) > 500) {
    // 收到但解码器认成别的格式/位数：用于排查接收头是否真的看到了信号
    lastOtherPrint = millis();
    Serial.print("IR other: proto=");
    Serial.print(d.protocol);
    Serial.print(" bits=");
    Serial.print(d.numberOfBits);
    Serial.print(" raw=0x");
    printHex64(d.decodedRawData);
    Serial.println();
  }

  IrReceiver.resume();
}

static void updateStateMachine() {
  uint32_t now = millis();

  switch (state) {
    case ST_SEARCH:
      if ((uint32_t)(now - stateSince) >= SEARCH_TIMEOUT_MS) {
        dbg("Search timeout");
        showError();
        enterState(ST_IDLE);
        return;
      }

      if (timeReached(nextTxAt)) {
        if (trySend(MSG_HELLO, ID_BROADCAST)) {
          nextTxAt = now + random(SEARCH_GAP_MIN_MS, SEARCH_GAP_MAX_MS);
        } else {
          nextTxAt = now + random(120, 300);
        }
      }
      break;

    case ST_CONFIRM:
      if ((uint32_t)(now - stateSince) >= CONFIRM_TIMEOUT_MS) {
        dbg("Confirm timeout; search again");
        enterState(ST_SEARCH);
        return;
      }

      if (timeReached(nextTxAt)) {
        if (trySend(MSG_ACK, peerId)) {
          nextTxAt = now + ACK_INTERVAL_MS + random(0, 100);
        } else {
          nextTxAt = now + random(100, 220);
        }
      }
      break;

    case ST_PAIRED:
      if (doneLeft > 0 && timeReached(nextTxAt)) {
        if (trySend(MSG_DONE, peerId)) {
          doneLeft--;
          nextTxAt = now + DONE_REPEAT_INTERVAL;
        } else {
          nextTxAt = now + 100;
        }
      }
      break;

    case ST_IDLE:
      break;
  }
}

// ---------------------------------------------------------------------------
// 按钮处理
// ---------------------------------------------------------------------------
static void updateButton(Button &b) {
  int raw = digitalRead(b.pin);
  uint32_t now = millis();

  if (raw != b.lastRaw) {
    b.lastRaw = raw;
    b.lastChange = now;
  }

  if ((uint32_t)(now - b.lastChange) >= DEBOUNCE_MS && raw != b.stable) {
    b.stable = raw;
    if (b.stable == LOW) {
      b.pressedEvent = true;
    }
  }
}

static void onBluePress() {
  if (state == ST_IDLE) {
    dbg("Blue: start pairing");
    errorActive = false;
    enterState(ST_SEARCH);

  } else if (state == ST_PAIRED) {
    dbg("Blue ignored: press white to unpair first");

  } else {
    dbg("Blue ignored: already searching");
  }
}

static void onWhitePress() {
  // 即使本机已经是 IDLE，也发一次 BYE：
  // 如果上一次 BYE 丢了，对方可能还停在绿色，这一下可以把它清回 IDLE。
  uint16_t target = (peerId != 0) ? peerId : ID_BROADCAST;
  dbg("White: stop pairing");
  errorActive = false;

  // 连发两帧 BYE，降低丢帧概率
  for (uint8_t i = 0; i < 2; ++i) {
    if (!channelBusy()) {
      sendFrame(MSG_BYE, target);
    }
    delay(60);
  }

  enterState(ST_IDLE);
}

// ---------------------------------------------------------------------------
// LED
// ---------------------------------------------------------------------------
static void updateLed() {
  uint32_t now = millis();

  // 搜索超时的红色闪烁
  if (errorActive) {
    if ((int32_t)(now - errorUntil) >= 0) {
      errorActive = false;
    } else {
      bool on = ((now - errorStart) / 150) & 1;
      setLed(on, false, false);
      return;
    }
  }

  switch (state) {
    case ST_IDLE:
      setLed(true, true, true);           // 常亮白：配对关闭
      break;

    case ST_SEARCH: {
      bool on = ((now / 300) & 1) == 0;   // 蓝色慢闪
      setLed(false, false, on);
      break;
    }

    case ST_CONFIRM: {
      bool on = ((now / 120) & 1) == 0;   // 青色快闪：已听到对方
      setLed(false, on, on);
      break;
    }

    case ST_PAIRED:
      setLed(false, true, false);         // 常亮绿：配对成功
      break;
  }
}

// ---------------------------------------------------------------------------
// 调试心跳
// ---------------------------------------------------------------------------
static void debugHeartbeat() {
  static uint32_t last = 0;
  uint32_t now = millis();

  if ((uint32_t)(now - last) < 5000) return;
  last = now;

  if (!Serial) return;

  Serial.print("[state=");
  Serial.print(stateName(state));
  Serial.print(" my=0x");
  printHex16(myId);
  if (peerId != 0) {
    Serial.print(" peer=0x");
    printHex16(peerId);
  }
  Serial.println("]");
}

// 串口监视器可能在上电后才打开；第一次连上时补打一次开机信息
static void printBanner() {
  if (!Serial || bannerPrinted) return;
  bannerPrinted = true;

  Serial.println();
  Serial.println("=== IR Near-Field Pairing ===");
  Serial.print("Chip UID: ");
  for (uint8_t i = 0; i < 8; ++i) {
    printHex8(chipUid[i]);
  }
  Serial.println();
  Serial.print("My machine code: 0x");
  printHex16(myId);
  Serial.println();
  Serial.println("Blue = start pairing, White = stop/unpair");
}

void printStatus() {
  if (!Serial) return;
  Serial.print("STATUS state=");
  Serial.print(stateName(state));
  Serial.print(" my=0x");
  printHex16(myId);
  if (peerId != 0) {
    Serial.print(" peer=0x");
    printHex16(peerId);
  }
  Serial.println();
}

void begin() {
  pinMode(PIN_BTN_WHITE, INPUT_PULLUP);
  pinMode(PIN_BTN_BLUE, INPUT_PULLUP);

  btnWhite.lastRaw = btnWhite.stable = digitalRead(PIN_BTN_WHITE);
  btnBlue.lastRaw  = btnBlue.stable  = digitalRead(PIN_BTN_BLUE);
  btnWhite.lastChange = btnBlue.lastChange = millis();

  // 第一次访问 NinaPin 会初始化 NINA 的 SPI，耗时约 0.8 s
  pinMode(LEDR, OUTPUT);
  pinMode(LEDG, OUTPUT);
  pinMode(LEDB, OUTPUT);
  setLed(false, false, false);

  myId = computeMachineId();

  IrReceiver.begin(PIN_IR_RX, DISABLE_LED_FEEDBACK);
  IrSender.begin();

  randomSeed(micros() ^ ((uint32_t)myId << 16) ^ analogRead(A0));

  enterState(ST_IDLE);
  printBanner();
}

void tick() {
  handleIr();
  updateStateMachine();
  updateLed();
  debugHeartbeat();
}

uint8_t buttons() {
  updateButton(btnWhite);
  updateButton(btnBlue);
  uint8_t result = (btnWhite.pressedEvent ? kWhite : 0) |
                   (btnBlue.pressedEvent ? kBlue : 0);
  btnWhite.pressedEvent = false;
  btnBlue.pressedEvent = false;
  return result;
}

void blue() { onBluePress(); }
void white() { onWhitePress(); }
bool active() { return state != ST_IDLE; }
bool paired() { return state == ST_PAIRED; }
bool handshakeBusy() {
  return state == ST_SEARCH || state == ST_CONFIRM ||
         (state == ST_PAIRED && doneLeft > 0);
}
uint16_t selfCode() { return myId; }
uint16_t peerCode() { return peerId; }

void suspendLed() {
  if (!ledSuspended) setLed(false, false, false);
  ledSuspended = true;
}
void restoreLed() {
  // The NINA was reset by the shared transport. Reconfigure GPIO direction.
  pinMode(LEDR, OUTPUT);
  pinMode(LEDG, OUTPUT);
  pinMode(LEDB, OUTPUT);
  ledSuspended = false;
  lastLed = -1;
  updateLed();
}
}  // namespace nearfield