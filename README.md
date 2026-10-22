# surfaace_monitor

**Surface Pro 7 / Intel CPU + iGPU / Hollow Knight.exe** 长期只读性能监控。

Python 主程序采集、关联和保存数据。官方 [LibreHardwareMonitorLib](https://www.nuget.org/packages/LibreHardwareMonitorLib/0.9.6) 通过轻量 C# 桥接进程读取硬件传感器；[Intel PresentMon](https://github.com/GameTechDev/PresentMon) 独立捕获游戏帧时间。采集结果保存到 SQLite，可导出 CSV 并生成 PNG 趋势图。

> 安全边界：本项目 **不修改** PL1/PL2、电压、CPU 缓存、风扇或任何限温参数，不关闭热保护。所有实际缺失的传感器数据保持缺失，**不会**通过频率变化虚构 Thermal Throttling 或 Power Limit Throttling 状态。

## 已实现

- 持续运行 C# 传感器桥接（默认每 1 秒），输出带 UTC 时间戳的 NDJSON；支持断线重启、进程独立、日志记录。
- 捕获 CPU、Intel 核显等设备实际暴露的温度、时钟、功耗、负载和其他传感器。支持传感器标识、硬件类型、单位和原始值保存。
- Python 自动检测 **Hollow Knight.exe**，标记游戏开始/停止以及每次采样的游戏 PID。
- 可选地在游戏运行时使用 PresentMon 按 PID 录帧，并保存原始 CSV；使用 `--date_time` 让帧记录具备可关联的绝对时间。
- SQLite WAL 长期保存，按测试标签（如 `baseline` / `PL2-12W`）分 session；CSV（Excel 可打开）与按分钟聚合的趋势图。
- `doctor` 对设备做只读传感器自检；缺失项会有明确提示。
- Windows **PDH Processor Performance（%）** 随每个传感器样本一起写入数据库。它表示相对于标称处理器性能的百分比，**不是实际频率 MHz**，不能据此直接判定限温降频。
- 可选的 --acpi-zones 在后台每 30 秒读取 Windows ACPI 固件温区温度（摄氏度）；这些值**不是 CPU 核心或封装温度**，有些固件温区可能不反映 CPU 瞬时发热。

## 安装（Windows 10/11 x64）

1. 安装 Python 3.11+，下载此仓库并在仓库根目录打开 PowerShell。
2. 安装 Python 部分：
   ```powershell
   py -m pip install -e ".[charts]"
   ```
3. 从本仓库 **Actions → Build and test → 成功的运行 → Artifacts → SurfaceSensors-win-x64** 下载 C# 自包含 Windows 程序，将 `SurfaceSensors.exe` 放入 `bin\SurfaceSensors.exe`。这是 GitHub 托管 Windows runner 编译的文件，终端电脑无需安装 .NET SDK。也可自行安装 .NET 8 SDK 后执行：
   ```powershell
   dotnet publish bridge/SurfaceSensors/SurfaceSensors.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -o bin
   ```
4. 可选：从 [Intel PresentMon v2.6.0 官方发布页](https://github.com/GameTechDev/PresentMon/releases/tag/v2.6.0) 下载独立命令行版本 PresentMon-2.6.0-x64.exe（**不是大型 GUI MSI**），将其放到 bin 目录。已在 Surface Pro 7 上核对该版本的官方 SHA256 与 Intel Authenticode 签名，程序支持本项目使用的 CLI 参数。缺少 PresentMon 仍能记录硬件，但不会有 FPS 数据。

设备可能需要**以管理员权限**运行 PowerShell 才能让硬件监控驱动或 PresentMon ETW 正常采集。请仅从可信的官方来源获取这两个工具。首次采集先运行 `doctor`，不要预设 Surface Pro 7 一定能暴露独显/核显温度、独立 GPU 功耗或温控原因等指标。

### Surface Pro 7 (i5-1035G4) 实测兼容性

2026-10-22 在已连接的 Surface Pro 7 上完成了管理员权限下的 **只读检测**：

- LibreHardwareMonitorLib 桥接可识别 CPU、Intel 核显、内存的约 35 个活动传感器项，主要为负载数据；CPU 和 Intel 核显的**温度 / 时钟频率均不可用**，CPU / GPU 部分功耗值为 0 W（不能据此断言实际功耗为零）。
- **PawnIO 尚未安装**。LibreHardwareMonitor 0.9.6 的一些 Intel CPU MSR 读取需要 PawnIO；当前程序只会报告这个问题，**不会安装内核驱动**。如愿意自行评估并安装签名驱动，请参考 [PawnIO 项目](https://github.com/namazso/PawnIO) 或 [LibreHardwareMonitor 官方发布页](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/tag/v0.9.6)。安装内核驱动有系统级安全及兼容性影响，需自行决定，最好先备份并确认官方来源；安装后重新执行 doctor。安装 PawnIO 后也**不保证**旧 Intel 核显驱动一定提供温度/频率。
- 已证实 Windows 的 PDH % Processor Performance 会变化，因此作为独立百分比指标被记录；Windows WMI 还能看到 9 个 ACPI **固件**温区，需手动指定 --acpi-zones 才会记录，**不应当解释为 CPU Package 温度**。

如果 doctor 报告 CPU Temperature / Clock 缺失，当前结果**不能证明**真正的温度墙、PL1 或 PL2 限制。完整的过热降频研究需要可验证的 CPU 传感器读数或 HWiNFO 等工具的真实 Throttling 限制标志。不要用 ACPI 温区或 PDH 百分比代替这些证据。

## 测试步骤

```powershell
# 1. 只读硬件检测，列出真实可用传感器
surfaace-monitor doctor --bridge .\bin\SurfaceSensors.exe --presentmon .\bin\PresentMon-2.6.0-x64.exe

# 2. 第一次跑：游戏打开前就开始监控，Ctrl+C 停止；不要修改 PL1/PL2
surfaace-monitor monitor --bridge .\bin\SurfaceSensors.exe --presentmon .\bin\PresentMon-2.6.0-x64.exe --tag baseline --interval 1

# 3. 列出 session ID
surfaace-monitor sessions

# 4. 指定测试进行 30 分钟（秒）。只记录硬件时可不填 --presentmon
surfaace-monitor monitor --bridge .\bin\SurfaceSensors.exe --duration 1800 --tag baseline-30min --acpi-zones

# 5. 输出最新一次的时序数据、完整帧 CSV、事件，以及报告
surfaace-monitor export --out reports\baseline
surfaace-monitor report --out reports\baseline

# 6. 完成两组基线与对照测试后，使用 sessions 里实际列出的两个 session ID
surfaace-monitor compare --baseline BASELINE_SESSION_ID --variant VARIANT_SESSION_ID --out reports\comparison.json
```

默认数据库在 `recordings/surface.db`，PresentMon 原始 CSV 在 `recordings/presentmon/`。CLI 的 `--db` / `--session` 可以操作指定记录；不同测试标签使用同一数据库，以便对照。使用 `python -m surfaace_monitor` 与 `surfaace-monitor` 等效。若未安装 matplotlib，使用 `report --summary-only`。

对照功能仅比较两次测试间**相同传感器 ID** 的实测均值、最小值、最大值与平均 FPS；缺失的 CPU 温度/频率不能被填补，且对照结果并不自动证明某次 PL2 调整造成了变化。相对于标称频率的 Windows PDH 百分比和 ACPI 固件温区有独立说明，不能当成真实 CPU MHz / CPU 封装温度。

**游戏进程名称**需要准确匹配 `Hollow Knight.exe`（忽略大小写）；也可用 `--process` 更改。PresentMon 在检测到游戏进程后启动，结束后尝试合并到 SQLite；若 CSV 不包含可识别的 `CPUStartDateTime`，会保留帧时间/FPS 数据，但不能假装这些帧已与温度对齐。

## 如何判断「调整 L2/PL2 后不降频」

- 先做 **baseline**：固定供电方式、Windows 性能模式、画质/分辨率、室温、游戏场景（例如同一地图和移动路线）、相同持续时间。
- 在 **不关闭设备温控保护** 的前提下，由你自己在设备支持的情况下调整 PL2，再用新的标签记录第二组。同一时刻不要同时调整其他参数。
- 比较温度峰值、长时 CPU/核显时钟及实际 FPS/帧时间，特别关注前 5 分钟和稳定运行阶段。
- **PL2 是短期功耗上限，L2 则是 CPU 二级缓存**；调低 PL2 可能抑制发热峰值，但不保证没有降频。
- **频率下降不等于过热降频**。轻负载、功耗限制、系统策略、硬件温控都可能导致频率变化。如需明确证据，配合 HWiNFO 传感器中的 Thermal Throttling / Power Limit 标志（若硬件支持）；本项目不会据此推断不存在的标志。

## 数据结构与限制

- `sessions`：测试标签、设备运行时间、配置。
- `sensors` / `samples` / `readings`：硬件标识、单位、UTC 时间戳和原始值；`game_pid IS NULL` 表示游戏未运行。
- `WindowsPerfCounter / Performance / %`：PDH **相对处理器性能**，绝非实测 MHz；`AcpiThermalZone / Temperature / C`：**固件温区**，绝非 CPU 核心/封装温度。报告中以独立图表及警告显示两种数据。
- `events`：进程状态、桥接重启、PresentMon 生命周期及异常。
- `captures` / `frames`：PresentMon 原始 CSV、帧间隔、由帧间隔计算出的 FPS、可用的绝对时间戳。
- 统计与 PNG 折线图使用分钟聚合；原始数据**不降采样**。请定期备份/归档数据库，长期运行会持续占用磁盘空间。
- 只有 CPU/GPU *实际暴露* 的指标才会出现在数据库；Intel 旧核显驱动可能不提供 GPU 温度/频率，硬件工具/安全软件也可能影响传感器读取。
- 不对任何硬件修改作自动化处理；监控工具不负责延长温控限制，也不保证 Surface 7 不会降频。

## 开发与构建

```powershell
py -m pip install -e ".[dev]"
py -m pytest -q
```

GitHub Actions 在 Windows runner 上使用 .NET 8 / LibreHardwareMonitorLib **0.9.6** 编译自包含 x64 exe，提供独立 artifact；同时在 Windows/Linux 上测试 Python 部分。构建成功只证明桥接程序可执行，不代表 Surface Pro 7 上所有硬件传感器均可读取。用 `doctor` 在目标设备上校验这一点。
