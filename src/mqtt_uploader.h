#ifndef BUMP_MQTT_UPLOADER_H
#define BUMP_MQTT_UPLOADER_H

#include <stdint.h>

#include "bump_config.h"

#if BUMP_MQTT

// 上传窗口：在 TDM 阶段边界调用，暂停 BLE → 连 Wi-Fi/TLS/MQTT → 发布 →
// 恢复广播阶段。BLE 与 Wi-Fi 共用 NINA 模块，不能同时工作。
// 时序与重试策略见 docs/FIRMWARE_MQTT_PLAN.md §7/§9。

typedef void (*UploaderResumeFn)();

// resume：上传结束后恢复 BLE 的回调（主程序传入 enterAdvPhase）。
void uploaderBegin(UploaderResumeFn resume);

// 有新事件入队时调用，标记下个阶段边界立即尝试上传。
void uploaderNotifyEvent();

// 若到期则执行一次上传窗口并返回 true（BLE 已恢复到广播阶段）；
// 否则返回 false，调用方按原逻辑切换阶段。
bool uploaderMaybeUpload(uint32_t now);

#endif  // BUMP_MQTT

#endif  // BUMP_MQTT_UPLOADER_H
