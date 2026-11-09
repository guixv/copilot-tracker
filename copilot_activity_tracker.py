#!/usr/bin/env python3
"""Show Copilot session activity, debug-log metadata, and workspace-file changes."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

DEFAULT_WORKSPACE = Path(__file__).resolve().parent
COPILOT_SESSION_STATE = Path.home() / ".copilot" / "session-state"
VSCODE_STORAGE = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Code"
    / "User"
    / "workspaceStorage"
)
IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


def timestamp(value: Any = None) -> str:
    if isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(value / 1000).astimezone()
    else:
        moment = datetime.now().astimezone()
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def emit(
    message: str,
    display: Callable[[str], None] | None = None,
    event_timestamp: Any = None,
) -> None:
    formatted = f"[{timestamp(event_timestamp)}] {message}"
    print(formatted, flush=True)
    if display is not None:
        display(formatted)


def workspace_from_storage(storage_directory: Path) -> Path | None:
    metadata = storage_directory / "workspace.json"
    try:
        data = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    folder_uri = data.get("folder")
    if not isinstance(folder_uri, str):
        return None
    parsed = urlparse(folder_uri)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if parsed.netloc:
        path = f"//{parsed.netloc}{path}"
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return None


def workspace_storage_directories(workspace: Path) -> list[Path]:
    if not VSCODE_STORAGE.is_dir():
        return []
    try:
        resolved_workspace = workspace.resolve()
    except OSError:
        return []
    return [
        storage
        for storage in VSCODE_STORAGE.iterdir()
        if storage.is_dir() and workspace_from_storage(storage) == resolved_workspace
    ]


def find_latest_vscode_workspace() -> Path | None:
    if not VSCODE_STORAGE.is_dir():
        return None

    logged_workspaces: list[tuple[int, Path]] = []
    other_workspaces: list[tuple[int, Path]] = []
    for storage in VSCODE_STORAGE.iterdir():
        workspace = workspace_from_storage(storage)
        if workspace is None or not workspace.is_dir():
            continue
        logs = (
            storage
            / "GitHub.copilot-chat"
            / "debug-logs"
        ).glob("*/main.jsonl")
        log_times: list[int] = []
        for path in logs:
            try:
                log_times.append(path.stat().st_mtime_ns)
            except OSError:
                continue
        try:
            if log_times:
                logged_workspaces.append((max(log_times), workspace))
            else:
                metadata_time = (storage / "workspace.json").stat().st_mtime_ns
                other_workspaces.append((metadata_time, workspace))
        except OSError:
            continue
    candidates = logged_workspaces or other_workspaces
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def find_latest_debug_log(workspace: Path) -> Path | None:
    candidates = (
        log
        for storage in workspace_storage_directories(workspace)
        for log in (
            storage / "GitHub.copilot-chat" / "debug-logs"
        ).glob("*/main.jsonl")
    )
    existing: list[tuple[int, Path]] = []
    for path in candidates:
        try:
            existing.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    if not existing:
        return None
    return max(existing, key=lambda candidate: candidate[0])[1]


def session_workspace(session_directory: Path) -> Path | None:
    metadata = session_directory / "workspace.yaml"
    try:
        with metadata.open(encoding="utf-8") as file:
            for line in file:
                key, separator, value = line.partition(":")
                if separator and key.strip() == "cwd":
                    value = value.strip()
                    if value.startswith('"') and value.endswith('"'):
                        value = json.loads(value)
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1].replace("''", "'")
                    return Path(value).expanduser().resolve()
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return None


def find_latest_session_log(workspace: Path) -> Path | None:
    if not COPILOT_SESSION_STATE.is_dir():
        return None

    candidates: list[tuple[int, Path]] = []
    for path in COPILOT_SESSION_STATE.glob("*/events.jsonl"):
        if session_workspace(path.parent) != workspace:
            continue
        try:
            candidates.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def find_latest_session_workspace() -> Path | None:
    if not COPILOT_SESSION_STATE.is_dir():
        return None

    candidates: list[tuple[int, Path]] = []
    for path in COPILOT_SESSION_STATE.glob("*/events.jsonl"):
        workspace = session_workspace(path.parent)
        if workspace is None or not workspace.is_dir():
            continue
        try:
            candidates.append((path.stat().st_mtime_ns, workspace))
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def file_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [
            name
            for name in directories
            if name not in IGNORED_DIRECTORIES
        ]
        current_path = Path(current)
        for filename in filenames:
            path = current_path / filename
            try:
                info = path.stat(follow_symlinks=False)
                if path.is_symlink() or not path.is_file():
                    continue
                relative_path = path.relative_to(root).as_posix()
                snapshot[relative_path] = (info.st_mtime_ns, info.st_size)
            except OSError:
                continue
    return snapshot


def report_file_changes(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
    display: Callable[[str], None] | None = None,
) -> None:
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    changed = sorted(
        name
        for name in before.keys() & after.keys()
        if before[name] != after[name]
    )
    changes = [("新增", name) for name in added]
    changes.extend(("修改", name) for name in changed)
    changes.extend(("删除", name) for name in removed)

    for action, name in changes[:50]:
        emit(f"工作区文件{action}（来源无法判定）: {name}", display)
    if len(changes) > 50:
        emit(f"另有 {len(changes) - 50} 项文件变化未展开", display)


def read_log_records(path: Path, offset: int) -> tuple[int, list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("rb") as log_file:
            log_file.seek(offset)
            data = log_file.read()
    except OSError:
        return offset, records

    last_newline = data.rfind(b"\n")
    if last_newline < 0:
        return offset, records
    complete_data = data[: last_newline + 1]
    complete_lines = complete_data.split(b"\n")

    for line in complete_lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)

    return offset + len(complete_data), records


def safe_label(value: Any) -> str | None:
    if not isinstance(value, str) or not value.isascii() or not 0 < len(value) <= 80:
        return None
    if any(not (character.isalnum() or character in "_-.:/") for character in value):
        return None
    return value


def describe_log_record(record: dict[str, Any]) -> str:
    event_type = safe_label(record.get("type")) or "unknown"
    name = safe_label(record.get("name"))
    status = safe_label(record.get("status"))
    duration = record.get("dur")
    parts = [f"type={event_type}"]
    if name:
        parts.append(f"name={name}")
    if status:
        parts.append(f"status={status}")
    if isinstance(duration, (int, float)):
        parts.append(f"duration={duration}ms")
    return "VS Code Copilot 调试事件（仅元数据）: " + " ".join(parts)


def debug_log_activity(record: dict[str, Any]) -> tuple[str, str] | None:
    event_type = safe_label(record.get("type")) or ""
    name = safe_label(record.get("name")) or ""
    if event_type == "session_start":
        return None

    label = name or event_type
    normalized = f"{event_type} {name}".casefold()
    if any(token in normalized for token in ("tool", "command", "function")):
        return "window", f"VS Code Copilot 日志显示正在执行 {label}"
    if any(
        token in normalized
        for token in ("request", "response", "assistant", "chat", "completion")
    ):
        return "writing", f"VS Code Copilot 日志显示正在处理 {label}"
    return None


def describe_session_record(
    record: dict[str, Any],
    tool_names: dict[str, str],
) -> str | None:
    event_type = safe_label(record.get("type"))
    data = record.get("data")
    if event_type is None or not isinstance(data, dict):
        return None

    if event_type == "user.message":
        return "Copilot 会话动态（正文已隐藏）: 收到用户消息"
    if event_type == "assistant.message":
        return "Copilot 会话动态（正文已隐藏）: Copilot 发送消息"
    if event_type == "assistant.turn_start":
        return "Copilot 会话动态: Copilot 开始处理"
    if event_type == "assistant.turn_end":
        return "Copilot 会话动态: Copilot 完成回复"
    if event_type == "tool.execution_start":
        tool_name = safe_label(data.get("toolName")) or "未知工具"
        tool_call_id = data.get("toolCallId")
        if isinstance(tool_call_id, str):
            tool_names[tool_call_id] = tool_name
        return f"Copilot 会话动态: 开始调用工具 {tool_name}"
    if event_type == "tool.execution_complete":
        tool_call_id = data.get("toolCallId")
        tool_name = (
            tool_names.pop(tool_call_id, "工具")
            if isinstance(tool_call_id, str)
            else "工具"
        )
        if isinstance(data.get("success"), bool):
            result = "成功" if data["success"] else "失败"
            return f"Copilot 会话动态: {tool_name}调用{result}"
        return f"Copilot 会话动态: {tool_name}调用完成"
    return None


def session_activity(record: dict[str, Any]) -> tuple[str, str] | None:
    event_type = safe_label(record.get("type"))
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    if event_type == "user.message":
        return "alert", "收到新请求，等待 Copilot 处理"
    if event_type == "assistant.turn_start":
        return "eating", "Copilot 正在处理当前请求，核桃低头吃饭"
    if event_type == "assistant.turn_end":
        return "groom", "Copilot 已完成本轮处理"
    if event_type == "assistant.message":
        return "writing", "Copilot 正在生成回复"
    if event_type == "tool.execution_start":
        tool_name = safe_label(data.get("toolName")) or "未知工具"
        tool_actions = {
            "functions.apply_patch": "编辑文件",
            "functions.bash": "运行终端命令",
            "functions.glob": "查找文件",
            "functions.read_bash": "读取命令输出",
            "functions.rg": "搜索代码",
            "functions.view": "查看文件",
        }
        action = tool_actions.get(tool_name, f"调用工具 {tool_name}")
        return "window", f"Copilot 正在{action}"
    if event_type == "tool.execution_complete":
        if data.get("success") is True:
            return "toilet", "工具执行完成，核桃去猫砂盆"
        if data.get("success") is False:
            return "toilet", "工具执行失败，核桃去猫砂盆"
        return "toilet", "工具调用结束，核桃去猫砂盆"
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "观察 Copilot 会话动态、调试日志元数据和工作区文件变化。"
            "此脚本不会显示对话正文、内部思考或工具参数和结果。"
        )
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        help="要观察的工作区目录（默认：最近的 Copilot 会话工作区）",
    )
    parser.add_argument(
        "--debug-log",
        type=Path,
        help="指定 Copilot main.jsonl；默认仅选择此工作区对应的最新日志",
    )
    parser.add_argument(
        "--session-log",
        type=Path,
        help="指定 Copilot 会话 events.jsonl；默认按工作区自动选择",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="轮询间隔秒数（默认：1）",
    )
    parser.add_argument(
        "--no-files",
        action="store_true",
        help="只观察 Copilot 调试日志，不扫描工作区文件",
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="仅输出到终端，不创建悬浮窗口",
    )
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval 必须大于 0")
    return args


def create_cat_animation_widget(
    QWidget: Any,
    QLabel: Any,
    Qt: Any,
    QColor: Any,
    QPainter: Any,
    QTimer: Any,
    QFont: Any,
) -> Any:
    class PixelCatScene(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.mood = "idle"
            self.frame = 0
            self.started_at = time.monotonic()
            self.pending_moods: list[str] = []
            self.summary_visible = True
            self.summary_overlay = QLabel("等待 Copilot 活动", self)
            self.summary_overlay.setWordWrap(True)
            self.summary_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.summary_overlay.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents
            )
            self.summary_overlay.setAttribute(
                Qt.WidgetAttribute.WA_StyledBackground
            )
            self.summary_overlay.setStyleSheet(
                "color: #fffaf0; background-color: rgba(42, 42, 57, 190); "
                "border: 1px solid rgba(255, 245, 232, 90); "
                "border-radius: 8px; padding: 5px 10px; font-size: 13px;"
            )
            self.summary_overlay.show()
            self.setMinimumSize(120, 82)
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.animate)
            self.timer.start(180)
            self.position_summary_overlay()

        def set_summary(self, summary: str) -> None:
            self.summary_overlay.setText(summary)
            self.position_summary_overlay()
            if self.summary_visible:
                self.summary_overlay.show()

        def set_summary_visible(self, visible: bool) -> None:
            self.summary_visible = visible
            self.summary_overlay.setVisible(visible)
            if visible:
                self.position_summary_overlay()

        def position_summary_overlay(self) -> None:
            if self.width() <= 0 or self.height() <= 0:
                return
            margin = min(12, max(6, self.width() // 10))
            self.summary_overlay.setMaximumWidth(self.width() - 2 * margin)
            self.summary_overlay.setMaximumHeight(max(32, self.height() // 3))
            self.summary_overlay.adjustSize()
            self.summary_overlay.move(
                max(0, (self.width() - self.summary_overlay.width()) // 2),
                8,
            )
            self.summary_overlay.raise_()

        def resizeEvent(self, event: Any) -> None:
            super().resizeEvent(event)
            self.position_summary_overlay()

        def queue_activity(self, mood: str) -> None:
            if mood == self.mood and not self.pending_moods:
                return
            if self.pending_moods and self.pending_moods[-1] == mood:
                return
            if len(self.pending_moods) < 20:
                self.pending_moods.append(mood)
            self.update()

        def animate(self) -> None:
            self.frame += 1
            now = time.monotonic()
            if self.pending_moods and now - self.started_at >= 2:
                self.mood = self.pending_moods.pop(0)
                self.started_at = now
            elif (
                not self.pending_moods
                and self.mood
                in {"alert", "writing", "groom", "celebrate", "surprised"}
                and now - self.started_at > 3
            ):
                self.mood = "idle"
                self.started_at = now
            self.update()

        @staticmethod
        def fill_rect(painter: Any, x: int, y: int, width: int, height: int, color: str) -> None:
            painter.fillRect(x, y, width, height, QColor(color))

        def draw_cat(self, painter: Any, x: int, y: int, mood: str) -> None:
            fur = "#70716d"
            dark = "#2d2d32"
            stripe = "#464448"
            wiggle = (self.frame % 3) * 2
            self.fill_rect(painter, x, y + 26, 8, 6, dark)
            self.fill_rect(painter, x - 2 + wiggle, y + 20, 6, 8, dark)
            self.fill_rect(painter, x - 4 + wiggle, y + 14, 6, 8, fur)
            self.fill_rect(painter, x - 2 + wiggle, y + 14, 4, 4, stripe)

            self.fill_rect(painter, x + 8, y, 8, 12, dark)
            self.fill_rect(painter, x + 28, y, 8, 12, dark)
            self.fill_rect(painter, x + 10, y + 4, 4, 6, "#a8947e")
            self.fill_rect(painter, x + 30, y + 4, 4, 6, "#a8947e")
            self.fill_rect(painter, x + 6, y + 8, 32, 18, dark)
            self.fill_rect(painter, x + 8, y + 10, 28, 14, fur)
            self.fill_rect(painter, x + 12, y + 8, 4, 4, stripe)
            self.fill_rect(painter, x + 20, y + 8, 4, 7, stripe)
            self.fill_rect(painter, x + 30, y + 12, 4, 4, stripe)
            self.fill_rect(painter, x + 13, y + 14, 7, 6, "#b8b66c")
            self.fill_rect(painter, x + 26, y + 14, 7, 6, "#b8b66c")
            self.fill_rect(painter, x + 16, y + 14, 3, 6, dark)
            self.fill_rect(painter, x + 28, y + 14, 3, 6, dark)
            self.fill_rect(painter, x + 14, y + 19, 16, 5, "#f2eee6")
            self.fill_rect(painter, x + 20, y + 19, 4, 3, "#d88780")
            self.fill_rect(painter, x + 8, y + 21, 6, 1, "#f2eee6")
            self.fill_rect(painter, x + 30, y + 21, 6, 1, "#f2eee6")

            self.fill_rect(painter, x + 4, y + 22, 36, 28, dark)
            self.fill_rect(painter, x + 6, y + 24, 32, 24, fur)
            for stripe_x, stripe_y, width, height in (
                (8, 28, 6, 5),
                (17, 26, 5, 7),
                (28, 27, 7, 5),
                (12, 36, 5, 4),
                (25, 34, 6, 5),
            ):
                self.fill_rect(
                    painter, x + stripe_x, y + stripe_y, width, height, stripe
                )

            self.fill_rect(painter, x + 8, y + 22, 28, 4, "#fff5e8")
            self.fill_rect(painter, x + 12, y + 26, 20, 4, "#fff5e8")
            self.fill_rect(painter, x + 18, y + 30, 8, 4, "#fff5e8")
            self.fill_rect(painter, x + 20, y + 34, 4, 4, "#fff5e8")

            self.fill_rect(painter, x + 8, y + 42, 10, 10, dark)
            self.fill_rect(painter, x + 26, y + 42, 10, 10, dark)
            self.fill_rect(painter, x + 10, y + 42, 6, 8, "#fff5e8")
            self.fill_rect(painter, x + 28, y + 42, 6, 8, "#fff5e8")
            if mood == "celebrate":
                self.fill_rect(painter, x + 38, y + 30, 8, 8, dark)
                self.fill_rect(painter, x + 40, y + 26, 6, 8, "#fff5e8")
            elif mood == "surprised":
                self.fill_rect(painter, x + 42, y + 8, 4, 12, "#ffe88a")

        def draw_window_scene(self, painter: Any) -> None:
            self.fill_rect(painter, 0, 0, 160, 110, "#292943")
            self.fill_rect(painter, 12, 8, 136, 70, "#bb8055")
            self.fill_rect(painter, 16, 12, 128, 62, "#172c4d")
            for x, y in ((28, 20), (52, 16), (119, 22), (135, 18), (92, 26)):
                if (self.frame // 3 + x) % 4:
                    self.fill_rect(painter, x, y, 2, 2, "#f5df9a")
            buildings = (
                (20, 36, 18, 25),
                (42, 28, 22, 33),
                (70, 38, 17, 23),
                (92, 24, 25, 37),
                (122, 34, 18, 27),
            )
            for bx, by, width, height in buildings:
                self.fill_rect(painter, bx, by, width, height, "#293d62")
                for wx in range(bx + 4, bx + width - 2, 6):
                    for wy in range(by + 4, by + height - 2, 7):
                        if (wx + wy) % 3:
                            self.fill_rect(painter, wx, wy, 2, 2, "#f1c96f")

            self.fill_rect(painter, 18, 56, 124, 15, "#33384b")
            self.fill_rect(painter, 18, 63, 124, 2, "#d3d4c7")
            for index in range(3):
                car_x = (self.frame * 3 + index * 49) % 142 - 8
                car_y = 57 + (index % 2) * 7
                car_color = ("#e68759", "#62b8bd", "#edcb72")[index]
                self.fill_rect(painter, car_x, car_y, 12, 4, car_color)
                self.fill_rect(painter, car_x + 2, car_y - 2, 7, 3, car_color)
                self.fill_rect(painter, car_x + 2, car_y + 3, 3, 2, "#24273a")
                self.fill_rect(painter, car_x + 9, car_y + 3, 3, 2, "#24273a")

            self.fill_rect(painter, 8, 74, 144, 6, "#8c583f")
            self.fill_rect(painter, 12, 80, 136, 30, "#473541")
            self.draw_cat(painter, 62, 28, self.mood)
            if self.mood == "alert":
                self.fill_rect(painter, 111, 38, 4, 4, "#ffe88a")
                self.fill_rect(painter, 119, 32, 4, 4, "#ffe88a")

        def draw_nap_scene(self, painter: Any) -> None:
            self.fill_rect(painter, 0, 0, 160, 110, "#3c3650")
            self.fill_rect(painter, 12, 12, 48, 36, "#bb8055")
            self.fill_rect(painter, 16, 16, 40, 28, "#172c4d")
            self.fill_rect(painter, 42, 19, 8, 8, "#f5df9a")
            self.fill_rect(painter, 52, 18, 2, 2, "#f5df9a")
            self.fill_rect(painter, 28, 27, 2, 2, "#f5df9a")
            self.fill_rect(painter, 0, 76, 160, 34, "#715448")
            self.fill_rect(painter, 37, 67, 86, 27, "#382b34")
            self.fill_rect(painter, 41, 63, 78, 28, "#d4b35e")
            self.fill_rect(painter, 47, 66, 66, 20, "#eed7b8")

            breath = (self.frame // 5) % 2
            for x, y, width, height in (
                (72, 40, 32, 8),
                (60, 44, 56, 8),
                (50, 52, 76, 10),
                (44, 62, 88, 12),
                (48, 74, 80, 10),
                (58, 84, 62, 8),
                (72, 90, 36, 5),
            ):
                self.fill_rect(painter, x, y + breath, width, height, "#2d2d32")
            for x, y, width, height in (
                (76, 42, 24, 4),
                (64, 46, 48, 5),
                (54, 54, 68, 6),
                (50, 64, 76, 8),
                (54, 74, 68, 6),
                (62, 82, 52, 6),
                (76, 88, 28, 4),
            ):
                self.fill_rect(painter, x, y + breath, width, height, "#70716d")
            for x, y, width, height in (
                (65, 48, 5, 7),
                (78, 46, 4, 7),
                (94, 50, 5, 7),
                (109, 55, 4, 7),
                (54, 62, 5, 7),
                (73, 60, 4, 6),
                (96, 63, 5, 7),
                (115, 67, 4, 6),
                (63, 75, 5, 6),
                (85, 74, 4, 6),
            ):
                self.fill_rect(painter, x, y + breath, width, height, "#464448")

            tail_wiggle = (self.frame // 4) % 2
            for x, y, width, height in (
                (106, 68, 14, 8),
                (112, 74, 14, 8),
                (106, 82, 18, 8),
                (92, 87, 22, 6),
            ):
                self.fill_rect(
                    painter, x, y + tail_wiggle, width, height, "#2d2d32"
                )
            for x, y, width, height in (
                (108, 70, 10, 4),
                (116, 76, 8, 4),
                (109, 84, 11, 4),
                (96, 88, 14, 3),
            ):
                self.fill_rect(
                    painter, x, y + tail_wiggle, width, height, "#70716d"
                )
            self.fill_rect(painter, 112, 74 + tail_wiggle, 4, 4, "#464448")
            self.fill_rect(painter, 116, 82 + tail_wiggle, 5, 4, "#464448")

            head_y = 56 + breath
            self.fill_rect(painter, 42, head_y - 9, 11, 13, "#2d2d32")
            self.fill_rect(painter, 60, head_y - 9, 11, 13, "#2d2d32")
            self.fill_rect(painter, 45, head_y - 6, 5, 7, "#a8947e")
            self.fill_rect(painter, 63, head_y - 6, 5, 7, "#a8947e")
            self.fill_rect(painter, 38, head_y, 37, 25, "#2d2d32")
            self.fill_rect(painter, 42, head_y + 3, 29, 19, "#70716d")
            self.fill_rect(painter, 47, head_y + 2, 5, 8, "#464448")
            self.fill_rect(painter, 57, head_y, 5, 10, "#464448")
            self.fill_rect(painter, 66, head_y + 4, 4, 7, "#464448")
            for x in (47, 60):
                self.fill_rect(painter, x + 1, head_y + 10, 2, 2, "#2d2d32")
                self.fill_rect(painter, x + 3, head_y + 12, 3, 2, "#2d2d32")
                self.fill_rect(painter, x + 6, head_y + 10, 2, 2, "#2d2d32")
            self.fill_rect(painter, 44, head_y + 14, 20, 7, "#f5f2ea")
            self.fill_rect(painter, 45, head_y + 14, 4, 3, "#d88780")

            for x, y, width, height in (
                (64, 75, 24, 8),
                (80, 82, 18, 7),
                (102, 84, 14, 7),
            ):
                self.fill_rect(
                    painter, x, y + breath, width, height, "#2d2d32"
                )
            for x, y, width, height in (
                (66, 76, 20, 5),
                (82, 83, 14, 4),
                (104, 85, 10, 4),
            ):
                self.fill_rect(
                    painter, x, y + breath, width, height, "#f5f2ea"
                )

            painter.setPen(QColor("#f5df9a"))
            painter.setFont(QFont("Menlo", 7))
            painter.drawText(35, 44 - (self.frame // 5) % 5, "z")
            painter.drawText(45, 37 - (self.frame // 5) % 5, "z")

        def draw_crouched_cat(
            self,
            painter: Any,
            x: int,
            y: int,
            head_offset: int = 0,
            head_bob: int = 0,
            looking_down: bool = False,
        ) -> None:
            wiggle = (self.frame % 3) * 2
            self.fill_rect(painter, x + 53, y + 56, 12, 10, "#2d2d32")
            self.fill_rect(painter, x + 49 - wiggle, y + 52, 10, 8, "#70716d")
            self.fill_rect(painter, x + 56, y + 54, 38, 24, "#2d2d32")
            self.fill_rect(painter, x + 58, y + 56, 34, 20, "#70716d")
            self.fill_rect(painter, x + 53, y + 58, 10, 8, "#2d2d32")
            self.fill_rect(painter, x + 54, y + 60, 7, 4, "#70716d")
            for stripe_x, stripe_y, width, height in (
                (62, 59, 5, 7),
                (73, 56, 4, 8),
                (83, 59, 5, 7),
                (67, 68, 5, 6),
                (80, 68, 4, 6),
            ):
                self.fill_rect(
                    painter,
                    x + stripe_x,
                    y + stripe_y,
                    width,
                    height,
                    "#464448",
                )
            self.fill_rect(painter, x + 61, y + 58, 3, 4, "#a49b8b")
            self.fill_rect(painter, x + 77, y + 57, 3, 4, "#a49b8b")

            head_x = x + 88 + head_offset
            head_y = y + 52 + head_bob
            self.fill_rect(painter, head_x, head_y, 24, 21, "#2d2d32")
            self.fill_rect(painter, head_x + 2, head_y + 2, 20, 17, "#70716d")
            self.fill_rect(painter, head_x + 2, head_y - 3, 9, 9, "#2d2d32")
            self.fill_rect(painter, head_x + 16, head_y - 3, 9, 9, "#2d2d32")
            self.fill_rect(painter, head_x + 4, head_y, 5, 4, "#a8947e")
            self.fill_rect(painter, head_x + 18, head_y, 5, 4, "#a8947e")
            self.fill_rect(painter, head_x + 8, head_y + 2, 4, 6, "#464448")
            self.fill_rect(painter, head_x + 13, head_y + 1, 4, 7, "#464448")
            eye_y = head_y + (9 if looking_down else 7)
            eye_height = 3 if looking_down else 5
            self.fill_rect(
                painter, head_x + 4, eye_y, 6, eye_height, "#b8b66c"
            )
            self.fill_rect(
                painter, head_x + 15, eye_y, 6, eye_height, "#b8b66c"
            )
            pupil_y = eye_y + (1 if looking_down else 0)
            pupil_height = 2 if looking_down else 5
            self.fill_rect(
                painter, head_x + 6, pupil_y, 2, pupil_height, "#2d2d32"
            )
            self.fill_rect(
                painter, head_x + 17, pupil_y, 2, pupil_height, "#2d2d32"
            )
            self.fill_rect(painter, head_x + 9, head_y + 12, 10, 5, "#f5f2ea")
            self.fill_rect(painter, head_x + 12, head_y + 11, 4, 3, "#d88780")
            self.fill_rect(painter, head_x + 3, head_y + 15, 20, 5, "#f5f2ea")

            for paw_x in (x + 64, x + 79):
                self.fill_rect(painter, paw_x, y + 72, 9, 8, "#2d2d32")
                self.fill_rect(
                    painter, paw_x + 2, y + 72, 5, 6, "#f5f2ea"
                )

        def draw_litter_scene(self, painter: Any) -> None:
            self.fill_rect(painter, 0, 0, 160, 110, "#e7d7bb")
            self.fill_rect(painter, 0, 70, 160, 40, "#c7a982")
            self.fill_rect(painter, 0, 68, 160, 4, "#9d7657")
            for x in range(8, 160, 20):
                self.fill_rect(painter, x, 72, 2, 38, "#b8956e")
            self.fill_rect(painter, 22, 18, 24, 22, "#f2e7d5")
            self.fill_rect(painter, 26, 22, 16, 14, "#d7e5df")
            self.fill_rect(painter, 31, 26, 6, 6, "#78a6a0")
            self.fill_rect(painter, 48, 77, 66, 20, "#6d5260")
            self.fill_rect(painter, 52, 79, 58, 14, "#d9c394")
            for index, sand_x in enumerate((58, 68, 80, 94, 103)):
                sand_y = 82 + ((self.frame + index * 2) % 4) * 2
                self.fill_rect(painter, sand_x, sand_y, 2, 2, "#f5e4bd")

            self.draw_crouched_cat(painter, 0, 0)

        def draw_eating_scene(self, painter: Any) -> None:
            self.fill_rect(painter, 0, 0, 160, 110, "#e7d7bb")
            self.fill_rect(painter, 0, 78, 160, 32, "#c7a982")
            self.fill_rect(painter, 79, 82, 76, 20, "#d8c69b")
            self.fill_rect(painter, 100, 71, 51, 21, "#2d2d32")
            self.fill_rect(painter, 103, 74, 45, 14, "#709294")
            self.fill_rect(painter, 103, 72, 45, 4, "#f5f2ea")
            self.fill_rect(painter, 115, 77, 22, 6, "#d69b58")
            for x, y in ((118, 78), (126, 80), (134, 77)):
                self.fill_rect(painter, x, y, 2, 2, "#f2d18a")

            head_bob = 8 + (self.frame // 3) % 2
            self.draw_crouched_cat(
                painter,
                0,
                0,
                head_offset=6,
                head_bob=head_bob,
                looking_down=True,
            )

        def draw_writing_scene(self, painter: Any) -> None:
            self.fill_rect(painter, 0, 0, 160, 110, "#d8c8b3")
            self.fill_rect(painter, 0, 72, 160, 38, "#816049")
            self.fill_rect(painter, 14, 68, 132, 7, "#b77d52")
            self.fill_rect(painter, 96, 42, 34, 26, "#382b34")
            self.fill_rect(painter, 100, 44, 26, 18, "#6ca9b3")
            self.fill_rect(painter, 94, 64, 38, 4, "#49384a")
            self.draw_cat(painter, 50, 24, self.mood)
            self.fill_rect(painter, 82, 64, 12, 4, "#fff5e8")

        def paintEvent(self, event: Any) -> None:
            del event
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            background = {
                "idle": "#3c3650",
                "toilet": "#e7d7bb",
                "eating": "#e7d7bb",
                "writing": "#d8c8b3",
            }.get(self.mood, "#292943")
            painter.fillRect(self.rect(), QColor(background))
            scale = max(0.1, min(self.width() / 160, self.height() / 110))
            painter.translate(
                (self.width() - 160 * scale) / 2,
                (self.height() - 110 * scale) / 2,
            )
            painter.scale(scale, scale)
            if self.mood == "idle":
                self.draw_nap_scene(painter)
            elif self.mood == "toilet":
                self.draw_litter_scene(painter)
            elif self.mood == "eating":
                self.draw_eating_scene(painter)
            elif self.mood == "writing":
                self.draw_writing_scene(painter)
            else:
                self.draw_window_scene(painter)

            painter.setPen(QColor("#fff5e8"))
            painter.setFont(QFont("Menlo", 6))
            captions = {
                "idle": "核桃 · 猫窝里睡成牛角包",
                "alert": "核桃 · 有新消息",
                "toilet": "核桃 · 猫砂时间",
                "eating": "核桃 · 低头干饭",
                "window": "核桃 · 看车车",
                "writing": "核桃 · 敲键盘",
                "groom": "核桃 · 洗脸休息",
                "celebrate": "核桃 · 工具完成",
                "surprised": "核桃 · 受到惊吓",
            }
            painter.drawText(8, 105, captions.get(self.mood, "核桃"))

    return PixelCatScene()


def create_application_icon(
    QColor: Any,
    QIcon: Any,
    QPainter: Any,
    QPixmap: Any,
) -> Any:
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor("#3c3650"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def pixel(x: int, y: int, width: int, height: int, color: str) -> None:
        painter.fillRect(x, y, width, height, QColor(color))

    dark = "#2d2d32"
    fur = "#70716d"
    stripe = "#464448"
    white = "#fff5e8"

    pixel(12, 8, 16, 16, dark)
    pixel(36, 8, 16, 16, dark)
    pixel(16, 12, 8, 8, "#a8947e")
    pixel(40, 12, 8, 8, "#a8947e")
    pixel(8, 16, 48, 40, dark)
    pixel(12, 20, 40, 32, fur)
    pixel(18, 20, 8, 8, stripe)
    pixel(28, 16, 8, 12, stripe)
    pixel(38, 20, 8, 8, stripe)
    pixel(18, 30, 10, 8, "#b8b66c")
    pixel(36, 30, 10, 8, "#b8b66c")
    pixel(21, 30, 4, 8, dark)
    pixel(39, 30, 4, 8, dark)
    pixel(20, 38, 24, 8, white)
    pixel(30, 36, 4, 4, "#e5bc91")
    pixel(16, 40, 8, 4, "#a49b8b")
    pixel(40, 40, 8, 4, "#a49b8b")
    pixel(18, 48, 28, 8, white)
    pixel(28, 56, 8, 8, white)
    painter.end()
    return QIcon(pixmap)


class ActivityWindow:
    def __init__(self) -> None:
        try:
            from PySide6.QtCore import QTimer, Qt
            from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
            from PySide6.QtWidgets import (
                QApplication,
                QHBoxLayout,
                QLabel,
                QMainWindow,
                QPlainTextEdit,
                QPushButton,
                QStackedWidget,
                QVBoxLayout,
                QWidget,
            )
        except ImportError as error:
            raise RuntimeError(
                "未安装 PySide6；请先运行 uv sync，或使用 --console"
            ) from error

        self.app = QApplication.instance() or QApplication(sys.argv[:1])
        application_name = "核桃 Copilot Tracker"
        self.app.setApplicationName(application_name)
        self.app.setApplicationDisplayName(application_name)
        application_icon = create_application_icon(
            QColor, QIcon, QPainter, QPixmap
        )
        self.app.setWindowIcon(application_icon)
        self.root = QMainWindow()
        self.root.setWindowTitle(application_name)
        self.root.setWindowIcon(application_icon)
        self.root.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        screen = self.app.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            minimum_width = min(220, available.width())
            minimum_height = min(170, available.height())
            width = min(480, max(minimum_width, int(available.width() * 0.3)))
            height = min(360, max(minimum_height, int(available.height() * 0.35)))
            self.root.setMinimumSize(minimum_width, minimum_height)
            self.root.resize(width, height)
        else:
            self.root.setMinimumSize(220, 170)
            self.root.resize(420, 320)

        output = QPlainTextEdit()
        output.setReadOnly(True)
        output.document().setMaximumBlockCount(500)
        self.output = output

        self.pages = QStackedWidget()
        self.pages.addWidget(output)
        self.cat_scene = create_cat_animation_widget(
            QWidget, QLabel, Qt, QColor, QPainter, QTimer, QFont
        )

        self.log_button = QPushButton("日志")
        self.cat_button = QPushButton("动画")
        self.log_button.setCheckable(True)
        self.cat_button.setCheckable(True)
        self.log_button.setChecked(True)
        self.log_button.clicked.connect(lambda _checked=False: self.show_page(0))
        self.cat_button.clicked.connect(lambda _checked=False: self.show_page(1))
        self.minimize_button = QPushButton("−")
        self.close_button = QPushButton("×")
        self.summary_button = QPushButton("隐藏摘要")
        self.summary_button.setToolTip("隐藏动画顶部的活动摘要")
        self.summary_button.clicked.connect(self.toggle_activity_summary)
        self.minimize_button.setToolTip("最小化")
        self.close_button.setToolTip("关闭")
        self.minimize_button.clicked.connect(self.root.showMinimized)
        self.close_button.clicked.connect(self.root.close)
        self.minimize_button.setFixedSize(26, 24)
        self.close_button.setFixedSize(26, 24)

        class DragLabel(QLabel):
            def __init__(self, parent: Any) -> None:
                super().__init__("核桃", parent)
                self.drag_offset: Any = None
                self.setCursor(Qt.CursorShape.SizeAllCursor)

            def mousePressEvent(self, event: Any) -> None:
                if event.button() == Qt.MouseButton.LeftButton:
                    self.drag_offset = (
                        event.globalPosition().toPoint()
                        - self.window().frameGeometry().topLeft()
                    )
                    event.accept()
                    return
                super().mousePressEvent(event)

            def mouseMoveEvent(self, event: Any) -> None:
                if (
                    self.drag_offset is not None
                    and event.buttons() & Qt.MouseButton.LeftButton
                ):
                    self.window().move(
                        event.globalPosition().toPoint() - self.drag_offset
                    )
                    event.accept()
                    return
                super().mouseMoveEvent(event)

            def mouseReleaseEvent(self, event: Any) -> None:
                self.drag_offset = None
                super().mouseReleaseEvent(event)

        toolbar = QWidget()
        toolbar.setObjectName("hoverToolbar")
        toolbar.setFixedHeight(36)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(8, 4, 6, 4)
        toolbar_layout.setSpacing(5)
        toolbar_layout.addWidget(DragLabel(toolbar), stretch=1)
        toolbar_layout.addWidget(self.log_button)
        toolbar_layout.addWidget(self.cat_button)
        toolbar_layout.addWidget(self.summary_button)
        toolbar_layout.addWidget(self.minimize_button)
        toolbar_layout.addWidget(self.close_button)
        toolbar.setStyleSheet(
            "QWidget#hoverToolbar { background: #eee3d2; "
            "border-bottom: 1px solid #c8b49a; }"
            "QPushButton { background: #f8f1e7; color: #382b34; "
            "border: 1px solid #bba58c; border-radius: 5px; padding: 3px 8px; }"
            "QPushButton:checked { background: #d7b48c; }"
            "QPushButton#closeButton { background: #d97b69; color: white; }"
        )
        self.close_button.setObjectName("closeButton")
        self.toolbar = toolbar

        content = QWidget()
        content.setObjectName("windowContent")
        content.setStyleSheet(
            "#windowContent { background: #f3eadc; "
            "border: 1px solid #9c8068; border-radius: 8px; }"
        )
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(toolbar)
        cat_page = QWidget()
        cat_layout = QVBoxLayout(cat_page)
        cat_layout.setContentsMargins(2, 2, 2, 2)
        cat_layout.setSpacing(0)
        cat_layout.addWidget(self.cat_scene, stretch=1)
        self.cat_scene.set_summary("等待 Copilot 活动")
        self.pages.addWidget(cat_page)
        layout.addWidget(self.pages)
        self.root.setCentralWidget(content)

        screen = self.app.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            x = max(available.left(), available.right() - self.root.width() - 16)
            y = max(available.top(), available.bottom() - self.root.height() - 16)
            self.root.move(x, y)
        self.toolbar.hide()
        self.last_hover = 0.0
        self.hover_timer = QTimer(self.root)
        self.hover_timer.timeout.connect(self.update_hover_controls)
        self.hover_timer.start(100)
        self.root.show()

    def update_hover_controls(self) -> None:
        from PySide6.QtGui import QCursor

        if self.root.frameGeometry().contains(QCursor.pos()):
            self.last_hover = time.monotonic()
            self.toolbar.show()
        elif (
            self.toolbar.isVisible()
            and time.monotonic() - self.last_hover > 0.6
        ):
            self.toolbar.hide()

    def show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.log_button.setChecked(index == 0)
        self.cat_button.setChecked(index == 1)

    def toggle_activity_summary(self) -> None:
        visible = not self.cat_scene.summary_visible
        self.cat_scene.set_summary_visible(visible)
        self.summary_button.setText("隐藏摘要" if visible else "显示摘要")
        action = "隐藏" if visible else "显示"
        self.summary_button.setToolTip(f"{action}动画顶部的活动摘要")

    def append(self, message: str) -> None:
        self.output.appendPlainText(message)

    def set_activity(self, mood: str, summary: str) -> None:
        self.cat_scene.queue_activity(mood)
        self.cat_scene.set_summary(summary)

    def run(self, tick: Callable[[], None], interval_ms: int) -> None:
        from PySide6.QtCore import QTimer

        timer = QTimer(self.root)
        timer.timeout.connect(tick)
        timer.start(interval_ms)
        tick()
        self.app.exec()


@dataclass
class MonitorState:
    current_log: Path | None
    log_offset: int
    current_session_log: Path | None
    session_log_offset: int
    tool_names: dict[str, str]
    previous_files: dict[str, tuple[int, int]]


def initialize_monitor(
    args: argparse.Namespace,
    workspace: Path,
    display: Callable[[str], None] | None = None,
    activity: Callable[[str, str], None] | None = None,
) -> MonitorState:
    current_log = (
        args.debug_log.expanduser().resolve()
        if args.debug_log
        else find_latest_debug_log(workspace)
    )
    log_offset = 0
    if current_log is not None:
        try:
            log_offset = current_log.stat().st_size
        except OSError:
            log_offset = 0

    current_session_log = (
        args.session_log.expanduser().resolve()
        if args.session_log
        else find_latest_session_log(workspace)
    )
    session_log_offset = 0
    if current_session_log is not None:
        try:
            session_log_offset = current_session_log.stat().st_size
        except OSError:
            session_log_offset = 0

    previous_files = file_snapshot(workspace) if not args.no_files else {}
    emit(
        f"开始监视；工作区: {workspace if not args.no_files else '未启用'}",
        display,
    )
    if current_log:
        emit(f"此工作区的 VS Code Copilot 日志: {current_log}", display)
    else:
        emit(
            "尚未发现此工作区的 VS Code Copilot 调试日志；等待日志出现",
            display,
        )
    if current_session_log:
        emit(f"Copilot 会话事件: {current_session_log}", display)
    else:
        emit("尚未发现此工作区的 Copilot 会话事件；等待新会话", display)
    emit(
        "消息正文、内部思考、工具参数和结果不会显示；文件变化来源也无法确认",
        display,
    )
    if activity is not None:
        activity("idle", "等待 Copilot 活动")
    return MonitorState(
        current_log,
        log_offset,
        current_session_log,
        session_log_offset,
        {},
        previous_files,
    )


def poll_monitor(
    args: argparse.Namespace,
    workspace: Path,
    state: MonitorState,
    display: Callable[[str], None] | None = None,
    activity: Callable[[str, str], None] | None = None,
) -> None:
    candidate = (
        args.debug_log.expanduser().resolve()
        if args.debug_log
        else find_latest_debug_log(workspace)
    )
    if candidate != state.current_log:
        state.current_log = candidate
        state.log_offset = 0
        if state.current_log:
            emit(
                f"切换到此工作区的 VS Code Copilot 日志: {state.current_log}",
                display,
            )

    if state.current_log:
        try:
            current_size = state.current_log.stat().st_size
            if current_size < state.log_offset:
                state.log_offset = 0
            state.log_offset, records = read_log_records(
                state.current_log, state.log_offset
            )
            for record in records:
                emit(describe_log_record(record), display, record.get("ts"))
                status = debug_log_activity(record)
                if status is not None and activity is not None:
                    activity(*status)
        except OSError:
            pass

    session_candidate = (
        args.session_log.expanduser().resolve()
        if args.session_log
        else find_latest_session_log(workspace)
    )
    if session_candidate != state.current_session_log:
        state.current_session_log = session_candidate
        state.tool_names.clear()
        state.session_log_offset = 0
        if session_candidate:
            emit(f"切换到 Copilot 会话事件: {session_candidate}", display)

    if state.current_session_log:
        try:
            current_size = state.current_session_log.stat().st_size
            if current_size < state.session_log_offset:
                state.session_log_offset = 0
                state.tool_names.clear()
            state.session_log_offset, records = read_log_records(
                state.current_session_log, state.session_log_offset
            )
            for record in records:
                description = describe_session_record(record, state.tool_names)
                if description is not None:
                    emit(description, display)
                status = session_activity(record)
                if status is not None and activity is not None:
                    activity(*status)
        except OSError:
            pass

    if not args.no_files:
        current_files = file_snapshot(workspace)
        if current_files != state.previous_files and activity is not None:
            activity("alert", "检测到工作区文件变化（来源无法确认）")
        report_file_changes(state.previous_files, current_files, display)
        state.previous_files = current_files


def main() -> int:
    args = parse_args()
    workspace = (
        args.workspace.expanduser().resolve()
        if args.workspace
        else (
            find_latest_session_workspace()
            or find_latest_vscode_workspace()
            or DEFAULT_WORKSPACE
        )
    )
    if not args.no_files and not workspace.is_dir():
        print(f"工作区目录不存在: {workspace}", file=sys.stderr)
        return 2

    window = None if args.console else ActivityWindow()
    display = window.append if window is not None else None
    activity = window.set_activity if window is not None else None
    state = initialize_monitor(args, workspace, display, activity)

    try:
        if window is not None:
            window.run(
                lambda: poll_monitor(args, workspace, state, display, activity),
                max(1, int(args.interval * 1000)),
            )
        else:
            while True:
                poll_monitor(args, workspace, state)
                time.sleep(args.interval)
    except KeyboardInterrupt:
        emit("监视已停止", display)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
