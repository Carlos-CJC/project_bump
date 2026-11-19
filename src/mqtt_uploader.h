#ifndef BUMP_MQTT_UPLOADER_H
#define BUMP_MQTT_UPLOADER_H

#include <stdint.h>

#include "bump_config.h"

#if BUMP_MQTT

// 上传窗口由主调度器在每个发现窗口结束时调用；期间独占 NINA 射频，
// 返回时 BLE 仍处于停止状态，调用方负责重新开始发现窗口。
// 时序与重试策略见 docs/FIRMWARE_MQTT_PLAN.md §7/§9。

void uploaderBegin();

// 有新事件入队时调用，标记下个发现窗口结束立即尝试上传。
void uploaderNotifyEvent();

// 若到期则执行一次上传窗口并返回 true，否则返回 false 且不做任何事。
bool uploaderMaybeUpload(uint32_t now);

#endif  // BUMP_MQTT

#endif  // BUMP_MQTT_UPLOADER_H
