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
4. 可选：从 PresentMon 官方 GitHub Releases 下载 **standalone console** 版本（不要与只含 GUI 的版本混淆），将可执行文件放到 `C:\Tools\PresentMon.exe`，或使用它原有的完整路径。缺少 PresentMon 仍能记录硬件，但不会有 FPS 数据。

设备可能需要**以管理员权限**运行 PowerShell 才能让硬件监控驱动或 PresentMon ETW 正常采集。请仅从可信的官方来源获取这两个工具。首次采集先运行 `doctor`，不要预设 Surface Pro 7 一定能暴露独显/核显温度、独立 GPU 功耗或温控原因等指标。

## 测试步骤

```powershell
# 1. 只读硬件检测，列出真实可用传感器
surfaace-monitor doctor --bridge .\bin\SurfaceSensors.exe --presentmon C:\Tools\PresentMon.exe

# 2. 第一次跑：游戏打开前就开始监控，Ctrl+C 停止；不要修改 PL1/PL2
surfaace-monitor monitor --bridge .\bin\SurfaceSensors.exe --presentmon C:\Tools\PresentMon.exe --tag baseline --interval 1

# 3. 列出 session ID
surfaace-monitor sessions

# 4. 指定测试进行 30 分钟（秒）。只记录硬件时可不填 --presentmon
surfaace-monitor monitor --bridge .\bin\SurfaceSensors.exe --duration 1800 --tag baseline-30min

# 5. 输出最新一次的时序数据、完整帧 CSV、事件，以及报告
surfaace-monitor export --out reports\baseline
surfaace-monitor report --out reports\baseline
```

默认数据库在 `recordings/surface.db`，PresentMon 原始 CSV 在 `recordings/presentmon/`。CLI 的 `--db` / `--session` 可以操作指定记录；不同测试标签使用同一数据库，以便对照。使用 `python -m surfaace_monitor` 与 `surfaace-monitor` 等效。若未安装 matplotlib，使用 `report --summary-only`。

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
