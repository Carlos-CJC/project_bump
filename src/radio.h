#ifndef BUMP_RADIO_H
#define BUMP_RADIO_H

// 硬复位 NINA-W102（variant 的 D24）。ArduinoBLE 的 BLE.end() 不拉这根线，
// Wi-Fi/BLE 切换或协议栈初始化失败时只能靠脉冲复位清除残留状态。
void bumpNinaReset();

#endif  // BUMP_RADIO_H
