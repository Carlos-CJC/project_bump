#ifndef BUMP_WATCHDOG_H
#define BUMP_WATCHDOG_H

#include <stdint.h>

// 启动硬件看门狗。timeoutMs 会按平台上限封顶；未提供 WDT 的平台为空操作。
void watchdogStart(uint32_t timeoutMs);

// 喂狗。主循环和所有可能长时间阻塞的等待循环都要定期调用。
void watchdogKick();

#endif  // BUMP_WATCHDOG_H
