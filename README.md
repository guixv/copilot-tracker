# Copilot Activity Tracker

一个用于观察 Copilot 会话动态、VS Code Copilot 调试日志和工作区文件变化的 Python 脚本。默认会在屏幕右下角打开置顶窗口，可切换「动态日志」和「核桃动画」两个页面。

动画是实时绘制的像素风狸花猫「核桃」，参考了核桃的实拍照片：灰棕色虎斑、白胸口和白袜腿，醒着时有圆圆的黄绿色眼睛；小憩时会把脸埋向前爪，身体侧向蜷成舒展的牛角包姿势。Copilot 开始处理时核桃沿用猫砂动画的体型低头吃饭；调用工具时核桃在高楼窗边看路上的车；工具调用完成时核桃去猫砂盆。其他状态还包括敲键盘、收到消息时抬头和结束后洗脸休息。当前 Copilot 活动会以半透明浮层叠在动画图像顶部，不占用主图布局空间。
窗口初始大小会根据屏幕尺寸调整，最小约为 220×170，也可以拖动窗口边缘缩放；动画会随可用区域自适应。
窗口采用无边框样式，关闭、最小化、活动摘要显示/隐藏和页面切换按钮默认隐藏；鼠标移到窗口上时显示。“隐藏摘要”按钮只切换动画顶部的活动摘要，不会隐藏整个窗口；再次点击可恢复显示。悬停工具栏左侧标题区域可拖动窗口。
窗口和 Qt 应用显示名为「核桃 Copilot Tracker」，并使用核桃像素猫图标。macOS 直接通过 `uv run` 启动时，Dock 中的进程标识仍可能受 Python 宿主影响；若要稳定显示为独立 Dock 应用，需要进一步打包成 `.app`。

## 环境安装

需要 Python 3.10 或更高版本。悬浮窗口使用 PySide6。

安装 [uv](https://docs.astral.sh/uv/)，然后在项目目录同步依赖并创建虚拟环境：

```bash
uv sync
```

## 用法

使用 uv 启动脚本（会自动使用项目虚拟环境）：

```bash
uv run python copilot_activity_tracker.py
```

窗口默认置顶显示在屏幕右下角，每秒检查一次。关闭窗口即可停止监视。
也可以先激活虚拟环境再运行：

```bash
source .venv/bin/activate
python copilot_activity_tracker.py
```

### 参数

| 参数 | 说明 |
| --- | --- |
| `--workspace PATH` | 指定工作区目录。用于扫描文件变化，并匹配该工作区的 Copilot 会话和 VS Code Copilot 日志。默认优先选择最近更新的 Copilot 会话工作区，其次选择最近使用的 VS Code Copilot 工作区；无法检测时回退到脚本所在目录。 |
| `--debug-log PATH` | 指定要读取的 Copilot `main.jsonl` 日志文件。默认只在目标工作区对应的 VS Code `workspaceStorage` 中选择最新日志，不会混入其他工作区日志。 |
| `--session-log PATH` | 指定 Copilot 会话 `events.jsonl` 文件。默认从本机 Copilot 会话记录中选择与工作区匹配且最近更新的文件。 |
| `--interval SECONDS` | 设置轮询间隔，必须大于 0。默认 `1` 秒。 |
| `--no-files` | 不扫描工作区文件，只观察 Copilot 调试日志。 |
| `--console` | 仅输出到终端，不创建悬浮窗口。 |
| `-h`, `--help` | 显示帮助信息。 |

同时打开多个工作区或 Copilot 会话时，建议用 `--workspace` 明确指定工作区；自动检测依据最近更新的会话事件文件，不能保证等同于当前前台窗口。

示例：

```bash
# 监视指定工作区
uv run python copilot_activity_tracker.py --workspace ~/code/my-project

# 监视指定工作区，并每半秒检查一次
uv run python copilot_activity_tracker.py --workspace ~/code/my-project --interval 0.5

# 监视另一个工作区及其文件和 Copilot 会话动态
uv run python copilot_activity_tracker.py --workspace ~/code/my-project

# 只在终端显示日志事件
uv run python copilot_activity_tracker.py --no-files --console
```

## 显示内容与限制

- Copilot 会话动态显示用户消息、Copilot 处理状态和工具调用名称；消息正文、内部思考、工具参数及结果不会显示。
- VS Code Copilot `main.jsonl` 日志只显示事件类型、名称、状态和耗时等元数据，不显示日志正文。
- VS Code Copilot 日志按 `workspaceStorage/workspace.json` 与目标工作区匹配。日志格式和记录内容由 Copilot 扩展决定；若扩展只写入 `session_start` 等启动信息，脚本无法从中还原内嵌聊天的处理过程。
- 启动时从当前日志末尾开始读取，因此不会回放启动前已有的日志记录。
- 会话动态从本机 `~/.copilot/session-state/` 读取，并按会话记录中的工作区路径匹配；同一工作区有多个会话时选择最近更新的一个。也可以通过 `--session-log` 固定来源。
- 工作区文件变化根据文件名、修改时间和大小判断；脚本无法确认变化由 Copilot 引起，也无法可靠识别文件读取。
- 脚本不会读取 Copilot 的内部思考，也无法观察未记录到本机会话事件或调试日志中的活动。
