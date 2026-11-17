# 修复/清理设计：重置数据区 + 清理测试残留进程

日期：2026-11-17
状态：设计待用户复查

## 1. 问题与目标

外部项目（tps-forklift-simulator）通过本平台的 Modbus 实例做寄存器模拟，发现两类问题：

1. **脏数据**：runtime 的寄存器表是进程内存，不自动清零；控制面重启还会 adopt 旧进程保留数据。多个写者（不同地址基）先后写同一张表 → 混合沉积。
2. **孤儿进程**：pytest 夹具用 `terminate()` 收控制面，Windows 下不触发 shutdown，留下 `poly_sim_runtime` 孤儿（config 在 `pytest-of-*` 临时目录）。它们不在当前控制面清单里，`reap_unwanted` 清不到。

目标：
- 提供「重置数据区」：**保持 client 连接**，只把该实例的数据面清零（Modbus 四区 / S7 字节区 / OPC UA 标签初值）。
- 提供「清理残留」：列出 `poly_sim_runtime` 进程，**只允许杀测试残留**，确认后杀。
- 修测试夹具根因，不再产生新孤儿。

**最高约束：不影响其他正在运行的业务服务。** 正在服务的实例（无论属于哪个控制面）一律不杀；只杀 config 位于测试/临时目录的孤儿。

## 2. 范围

**做**：
- 实例级 `reset`（sim only，保持连接，审计）。
- 本机 `poly_sim_runtime` 进程枚举 + 仅测试残留的确认清理（审计）。
- 修测试夹具 teardown。

**不做**：
- 远程机器进程清理（psutil 只能枚举本机；远程孤儿由各自控制面管理）。
- 杀「非测试」的 unowned 进程（可能是别的控制面的业务实例）——一律不杀。
- 自动启动时杀进程（有误杀风险）。
- link 实例的 reset（数据在对方设备，清零等于写别人设备）。

## 3. 后端设计

### 3.1 重置数据区

- `packages/plc-core/poly_core/adapter.py`：`ProtocolAdapter.reset()` 默认抛 `NotImplementedError`。
- `poly_modbus.server.ModbusTcpAdapter.reset()`：在 `_lock` 下把 `_holding/_input` 置 0、`_coils/_discrete` 置 False（按 `area_size` 长度）。**不关闭 socket、不动 `_conn_records`/`clients`**。
- `poly_s7.server.S7Adapter.reset()`：在 `_lock` 下把各 `_buffers` 清零。
- `poly_opcua.server.OpcUaAdapter.reset()`：把每个标签节点写回 `coerce(seed_value(tag), tag.data_type)`。
- `apps/sim-runtime/poly_sim_runtime/control.py`：`POST /reset` → `runner.adapter.reset()`；adapter None → 404；`NotImplementedError` → 400。
- `apps/agent/poly_agent/supervisor.py`：`reset(server_id)`，未运行抛 `RuntimeError`，HTTP≥400 抛 `ValueError`（同 `write_area`）。
- `apps/control-plane/poly_control_plane/deploy.py`：`RemoteSupervisor.reset(server_id)` → `_post(server_id, "/reset", {})`。
- `apps/control-plane/poly_control_plane/app.py`：`POST /api/servers/{id}/reset`：
  - `row.is_link` → 400「连接实例的数据在对方设备，不能重置」；
  - `not row.enabled` → 409；
  - `supervisor.reset(...)` 包 `except (KeyError, ValueError, RuntimeError)` → 400；
  - 审计 `server.reset`，params `{"protocol"}`；返回 `{"ok": True}`。

### 3.2 清理残留进程（仅测试残留）

- 新增 `apps/control-plane/poly_control_plane/maintenance.py`：
  - `list_runtimes(manifest_records) -> list[dict]`：psutil 遍历进程，cmdline 含 `poly_sim_runtime` 的，解析 `--config` 与 `--control-port`；返回 `{pid, create_time, config, control_port, owned, is_test}`。
    - `owned`：`(pid, create_time)` 与当前 Supervisor 清单匹配（复用 `_process_alive` 的身份判定思路）。
    - `is_test`：config 路径含 `pytest-of` 或位于 `tempfile.gettempdir()` 下。
  - `kill_test_runtimes(pids, manifest_records) -> list[int]`：对每个 pid 重新校验「是 `poly_sim_runtime` 且 `is_test` 且 `not owned`」，才 `terminate`/`kill`；返回实际杀掉的 pid。任何不满足的 pid 拒绝（抛 `ValueError`）。
- `app.py`：
  - `GET /api/maintenance/runtimes` → `{"runtimes": list_runtimes(...)}`（管理员？登录即可，只读）。
  - `POST /api/maintenance/kill` body `{"pids": [int]}` → 调 `kill_test_runtimes`；`ValueError` → 400；审计 `maintenance.kill`，params `{"pids": killed}`；返回 `{"killed": [...]}`。
- 安全：`owned=True` 的进程绝不出现在可杀集合；非 `is_test` 的 unowned 进程也不杀。

### 3.3 修测试夹具（根因）

- `tests/test_client_connections.py`、`tests/test_creation_defaults_api.py` 等的 `api` fixture：teardown 时除了 `process.terminate()`，还要确保 runtime 子进程被停掉。最简：给控制面进程发停止信号后，调用其 `stop_all` 不可行（跨进程），改为在 fixture 里记录 spawned runtime pids 并 terminate；或让控制面以 `--stop-on-exit` 启动并在 teardown 用 `process.terminate()` 触发其 shutdown（Windows 下 terminate 不触发）。
  - 采用：fixture 启动控制面时加 `--stop-on-exit`，teardown 先尝试优雅停止（`process.send_signal(CTRL_BREAK_EVENT)` 仅 Windows 可行则用），否则 `terminate()` 后再用 psutil 杀掉该 `POLY_DATA_DIR` 下的 `poly_sim_runtime` 子进程。
  - 目标：测试跑完不留孤儿。

## 4. 前端设计

- `apps/web/src/api.ts`：`resetServer(id)` → `POST /api/servers/{id}/reset`；`maintenanceRuntimes()` → `GET /api/maintenance/runtimes`；`killRuntimes(pids)` → `POST /api/maintenance/kill`。
- `ServerPage.tsx`：head-actions 加「重置数据区」按钮（仅 sim），`useConfirm({danger:true})`，成功后 `refreshValues`/重载 area。
- 新组件 `MaintenanceModal.tsx`：列出 runtimes（pid/config/owned/is_test），checkbox 默认勾选 `is_test && !owned`；`owned` 行禁用 checkbox；「清理所选」走 `useConfirm`，调 `killRuntimes`，刷新列表。入口放在侧边栏或系统页（管理员可见）。
- `styles.css`：复用现有表格/弹窗样式。

## 5. 错误处理

- reset：link→400、停用→409、supervisor 异常→400。
- kill：任何 owned / 非 is_test 的 pid → 400（整批拒绝或跳过？采用整批校验，任一不合法即 400，避免误杀）。
- 枚举时 psutil `AccessDenied`/`NoSuchProcess` 跳过该进程。

## 6. 测试

- adapter reset：Modbus 写入后 reset → 全 0 且 `clients` 不变、连接仍在；S7/OPC UA reset 回初值。
- runtime `/reset` 状态码。
- 控制面 reset：sim 200、link 400、停用 409、审计 `server.reset`。
- maintenance：构造一个假 `poly_sim_runtime` 进程（config 在 temp）→ 列表含 is_test；kill 只杀它；owned 进程不被杀；非 test unowned 不被杀。
- 夹具：跑完无孤儿（可用 psutil 断言）。
- 前端 typecheck/build。

## 7. 开放问题

- 无。清理范围已收紧为「仅测试残留」，满足「不影响其他业务」。
