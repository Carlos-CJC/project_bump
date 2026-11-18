#ifndef BUMP_CONN_TEST_H
#define BUMP_CONN_TEST_H

// 双板 BLE 连接自检入口：由 BUMP_TEST_ROLE 选择角色，仅联调时使用。
void connTestSetup();
void connTestLoop();

#endif  // BUMP_CONN_TEST_H
