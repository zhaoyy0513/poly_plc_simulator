# 项目长期记忆：poly_plc_simulator

## 产品定位（v0.2 方向，2026-10-22 定型）

一个**统一管理平台**（不是每台服务器各部署一套），管理若干台真实服务器上的 PLC 模拟器实例，同时能作为 client 连接别人已有的 PLC server。

- **site = 一台真实服务器**，持有 SSH 访问信息（host / 端口 / 账号 / 密码密文 / 部署目录）。`host` 为空表示「本机」，走内嵌 Supervisor。
- **实例有两种身份**（`kind`）：
  - `sim` = 我们部署并运行在目标机上的 server
  - `link` = 平台作为 client 连到别人的 server（不部署、不占端口）
- 两种实例在页面上共用「查看 / 编辑 / BIN-DEC-HEX」逻辑，差异只在数据来源。
- 详见 `docs/plans/2026-10-22-remote-site-server-client-design.md`。

## 已定下的技术选择（不要反复改）

- 部署：**SSH 推送 native 脚本**，目标机不常驻 agent；进程管理纯 SSH 按需查询，连接按 site 复用。docker 只保留 `runtime` 字段口子。
- 远程 runtime 控制端口只绑目标机 127.0.0.1，平台经 SSH channel 访问。
- 核心抽象 `DataSource`：LocalRuntime / SshRuntime / ModbusClient / OpcUaClient 四个实现。
- 单人多环境，**不做登录与权限**（正交，后加）。
- link 协议先 Modbus + OPC UA；S7 client 因 Windows 端需要 snap7.dll 后置。
- **Modbus 实例无标签表、无仿真值**，只有四个原始数据区（0–65535），值只由 client 或控制台写入产生；实例的 tab 只有「数据区 / 日志」。

## 工程约束

- 无 Alembic：`create_all()` 不会给旧表加列，schema 变更要手写 `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` 的幂等迁移。
- 前端是 React + Vite + zustand，类型手写；重新构建 dist 后刷新浏览器即可（不必重启 control-plane，但 dist 必须在启动时存在）。
- 主题默认深色，`poly.theme` 存 localStorage，浅色是 `:root[data-theme='light']` 重定义 token。

## 与用户的协作方式

技术选型与架构由我拍板，用户只确认业务意图（他说「我不太懂这些」）。给方案时**必须带推荐项 + 理由**，别抛一堆选项让他权衡。用户偏好一次只问一个问题、选项化提问。
