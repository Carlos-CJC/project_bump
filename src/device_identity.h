#ifndef BUMP_DEVICE_IDENTITY_H
#define BUMP_DEVICE_IDENTITY_H

#include <stddef.h>
#include <stdint.h>

// 设备稳定身份（device_id）与每次开机的会话身份（boot_id）。
// device_id 由 Wi-Fi MAC 派生，重启不变；boot_id 每次上电随机。

void identityInit();

const char* identityDeviceId();   // "bump-xxxxxxxxxxxx"
const char* identityBootId();     // 8 位小写 hex
const char* identityShortIdDev(); // device_id 去掉 "bump-" 前缀，12 位 hex

uint32_t identityNextEventSeq();

// event_id = ev_<dev12>_<boot8>_<seq8>
void identityFormatEventId(char* out, size_t cap, uint32_t seq);
// batch_id = bt_<dev12>_<boot8>_<firstseq8>_<count>
void identityFormatBatchId(char* out, size_t cap, uint32_t firstSeq, uint8_t count);

#endif  // BUMP_DEVICE_IDENTITY_H
