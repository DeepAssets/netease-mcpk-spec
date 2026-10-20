"""Chinese Windows GUI for the MCPK tool, authored by nmbb."""

from __future__ import annotations

import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from backend import repack, unpack


APP_TITLE = "nmbb · MCPK 解包封包工具"
VERSION = "1.1.1"


def _input_path(value: str) -> Path:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if not value:
        raise ValueError("请先填写源路径和输出路径。")
    return Path(value).expanduser().absolute()


def _unused_path(path: Path) -> Path:
    """Suggest a new path without reserving or modifying it."""
    if not path.exists():
        return path
    index = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


class MCPKApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.running = False
        self.controls: list[ttk.Widget] = []
        self.sources = {"unpack": tk.StringVar(), "repack": tk.StringVar()}
        self.outputs = {"unpack": tk.StringVar(), "repack": tk.StringVar()}
        self.suggested = {"unpack": "", "repack": ""}
        self.output_location: Path | None = None
        self.report_path: Path | None = None
        self.started_at = 0.0
        self.last_directory = Path.cwd()
        self.status = tk.StringVar(value="准备就绪")
        self.percent = tk.StringVar(value="0%")
        self.result = tk.StringVar(value="选择源文件或目录后，指定新的输出路径。")
        self.root.title(APP_TITLE)
        self.root.geometry("920x730")
        self.root.minsize(820, 620)
        self.root.configure(background="#F4F6FA")
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._configure_styles()
        self._build()
        self.root.after(75, self._poll_events)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        font = ("Microsoft YaHei UI", 10)
        self.root.option_add("*Font", font)
        style.configure(".", font=font, foreground="#1D2939")
        style.configure("TFrame", background="#F4F6FA")
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure("TLabel", background="#F4F6FA")
        style.configure("Card.TLabel", background="#FFFFFF")
        style.configure("Muted.TLabel", foreground="#667085")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 21, "bold"), foreground="#172B4D")
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Tip.TLabel", background="#EEF4FF", foreground="#344F7B", padding=(13, 10))
        style.configure("TNotebook", background="#F4F6FA", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(24, 10), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", "#FFFFFF"), ("!selected", "#E8EDF5")])
        style.configure("TEntry", padding=7, fieldbackground="#FFFFFF", bordercolor="#CBD5E1")
        style.configure("TButton", padding=(12, 7), background="#EDF1F7", bordercolor="#CBD5E1")
        style.map("TButton", background=[("active", "#E0E7F0"), ("disabled", "#F0F2F5")])
        style.configure("Accent.TButton", background="#2463D6", foreground="#FFFFFF", bordercolor="#2463D6", font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Accent.TButton", background=[("disabled", "#A8BCE1"), ("active", "#1D4FB0")], foreground=[("disabled", "#F8FAFF")])
        style.configure("Horizontal.TProgressbar", background="#2463D6", troughcolor="#E4EAF2", borderwidth=0)

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=(24, 20, 24, 16))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        ttk.Label(header, text="MCPK 解包 / 封包", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text=f"作者：nmbb    ·    版本 {VERSION}", style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

        self.notebook = ttk.Notebook(outer)
        self.notebook.grid(row=1, column=0, sticky="ew")
        self._build_operation("unpack", "解包", "提取 MCPK 中的文件；同时保存 .mcpk_tool 原包副本，供后续回封使用。")
        self._build_operation("repack", "封包", "保留原结构，支持修改已有文件；新增、删除或改名会明确报错。\n请选择本工具解出的完整目录，并保留其中的 .mcpk_tool。")

        progress_area = ttk.Frame(outer)
        progress_area.grid(row=2, column=0, sticky="ew", pady=(18, 12))
        progress_area.columnconfigure(0, weight=1)
        ttk.Label(progress_area, textvariable=self.status, style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(progress_area, textvariable=self.percent, style="Muted.TLabel").grid(row=0, column=1, sticky="e")
        self.progress_bar = ttk.Progressbar(progress_area, maximum=100, mode="determinate")
        self.progress_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 9))
        self.result_label = ttk.Label(progress_area, textvariable=self.result, style="Muted.TLabel", wraplength=820, justify="left")
        self.result_label.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.result_label.bind("<Configure>", lambda event: self.result_label.configure(wraplength=event.width) if event.width > 200 else None)

        log_area = ttk.Frame(outer)
        log_area.grid(row=3, column=0, sticky="nsew")
        log_area.columnconfigure(0, weight=1)
        log_area.rowconfigure(1, weight=1)
        ttk.Label(log_area, text="处理日志", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 7))
        self.log_text = ScrolledText(log_area, height=7, wrap="word", state="disabled", background="#FFFFFF", foreground="#344054", relief="flat", borderwidth=0, padx=12, pady=10, font=("Microsoft YaHei UI", 9))
        self.log_text.grid(row=1, column=0, sticky="nsew")

        footer = ttk.Frame(outer)
        footer.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, text="封包结构校验不代表游戏实测。", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.open_output_button = ttk.Button(footer, text="打开输出位置", state="disabled", command=self._open_output)
        self.open_output_button.grid(row=0, column=1, padx=(8, 0))
        self.open_report_button = ttk.Button(footer, text="打开报告", state="disabled", command=self._open_report)
        self.open_report_button.grid(row=0, column=2, padx=(8, 0))

    def _build_operation(self, operation: str, title: str, tip: str) -> None:
        panel = ttk.Frame(self.notebook, style="Card.TFrame", padding=(18, 15))
        panel.columnconfigure(0, weight=1)
        self.notebook.add(panel, text=f"  {title}  ")
        info = ttk.Label(panel, text=tip, style="Tip.TLabel", wraplength=760, justify="left")
        info.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 13))
        info.bind("<Configure>", lambda event, label=info: label.configure(wraplength=event.width - 26) if event.width > 226 else None)
        source_label = "源 MCPK 文件" if operation == "unpack" else "本工具解出的目录"
        output_label = "输出新目录（必须尚不存在）" if operation == "unpack" else "输出 MCPK 文件（必须尚不存在）"
        ttk.Label(panel, text=source_label, style="Card.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 5))
        source_entry = ttk.Entry(panel, textvariable=self.sources[operation])
        source_entry.grid(row=2, column=0, sticky="ew", padx=(0, 9))
        source_entry.bind("<FocusOut>", lambda _event, op=operation: self._suggest_output(op))
        source_button = ttk.Button(panel, text="选择文件…" if operation == "unpack" else "选择目录…", command=lambda op=operation: self._browse_source(op))
        source_button.grid(row=2, column=1, sticky="ew")
        ttk.Label(panel, text=output_label, style="Card.TLabel").grid(row=3, column=0, columnspan=2, sticky="w", pady=(11, 5))
        output_entry = ttk.Entry(panel, textvariable=self.outputs[operation])
        output_entry.grid(row=4, column=0, sticky="ew", padx=(0, 9))
        output_button = ttk.Button(panel, text="选择位置…", command=lambda op=operation: self._browse_output(op))
        output_button.grid(row=4, column=1, sticky="ew")
        action_button = ttk.Button(panel, text=f"开始{title}", style="Accent.TButton", command=lambda op=operation: self._start(op))
        action_button.grid(row=5, column=1, sticky="ew", pady=(14, 0))
        self.controls.extend([source_entry, source_button, output_entry, output_button, action_button])

    def _suggest_output(self, operation: str) -> None:
        raw_source = self.sources[operation].get().strip()
        if not raw_source:
            return
        current = self.outputs[operation].get().strip()
        if current and current != self.suggested[operation]:
            return
        try:
            source = _input_path(raw_source)
            name = f"{source.stem}_解包" if operation == "unpack" else f"{source.name}_回封.mcpk"
            destination = _unused_path(source.parent / name)
        except (OSError, ValueError):
            return
        self.suggested[operation] = str(destination)
        self.outputs[operation].set(str(destination))

    def _browse_source(self, operation: str) -> None:
        if operation == "unpack":
            selected = filedialog.askopenfilename(parent=self.root, title="选择 MCPK 封包文件", initialdir=str(self.last_directory), filetypes=[("所有文件", "*.*"), ("MCPK 封包", "*.mcpk *.MCPK")])
        else:
            selected = filedialog.askdirectory(parent=self.root, title="选择本工具解出的完整目录", initialdir=str(self.last_directory), mustexist=True)
        if selected:
            self.sources[operation].set(selected)
            self.last_directory = Path(selected).parent
            self._suggest_output(operation)

    def _browse_output(self, operation: str) -> None:
        self._suggest_output(operation)
        try:
            current = _input_path(self.outputs[operation].get())
        except ValueError:
            current = self.last_directory / ("MCPK_解包" if operation == "unpack" else "MCPK_回封.mcpk")
        initial_directory = current.parent if current.parent.is_dir() else self.last_directory
        if operation == "unpack":
            selected = filedialog.askdirectory(parent=self.root, title="选择输出位置（将在此创建新的解包目录）", initialdir=str(initial_directory), mustexist=True)
            if selected:
                destination = _unused_path(Path(selected) / current.name)
                self.outputs[operation].set(str(destination))
                self.last_directory = Path(selected)
        else:
            selected = filedialog.asksaveasfilename(parent=self.root, title="指定新的 MCPK 输出文件", initialdir=str(initial_directory), initialfile=current.name, defaultextension=".mcpk", confirmoverwrite=False, filetypes=[("MCPK 封包", "*.mcpk"), ("所有文件", "*.*")])
            if selected:
                self.outputs[operation].set(selected)
                self.last_directory = Path(selected).parent

    def _validate(self, operation: str) -> tuple[Path, Path]:
        source = _input_path(self.sources[operation].get()).resolve()
        output = _input_path(self.outputs[operation].get()).resolve()
        if operation == "unpack" and not source.is_file():
            raise ValueError("源 MCPK 文件不存在，或所选路径不是文件。")
        if operation == "repack" and not source.is_dir():
            raise ValueError("待封包目录不存在，或所选路径不是目录。")
        if output.exists():
            raise ValueError("输出路径已存在。工具不会覆盖已有文件或目录，请换一个新的名称。")
        if not output.parent.is_dir():
            raise ValueError("输出路径的父目录不存在，请先选择有效的输出位置。")
        if operation == "repack" and output.is_relative_to(source):
            raise ValueError("请将封包结果保存到待封包目录之外，避免改变原目录结构。")
        return source, output

    def _start(self, operation: str) -> None:
        if self.running:
            return
        self._suggest_output(operation)
        try:
            source, output = self._validate(operation)
        except (OSError, ValueError) as error:
            messagebox.showerror("路径需要调整", str(error), parent=self.root)
            return
        self.running = True
        self.started_at = time.monotonic()
        self.output_location = None
        self.report_path = None
        for control in self.controls:
            control.configure(state="disabled")
        self.open_output_button.configure(state="disabled")
        self.open_report_button.configure(state="disabled")
        self.progress_bar["value"] = 0
        self.percent.set("0%")
        verb = "解包" if operation == "unpack" else "封包"
        self.status.set(f"正在{verb}…")
        self.result.set(f"输出：{output}")
        self._append_log(f"开始{verb}：{source}")
        self._append_log(f"输出：{output}")
        threading.Thread(target=self._worker, args=(operation, source, output), name=f"mcpk-{operation}", daemon=True).start()

    def _worker(self, operation: str, source: Path, output: Path) -> None:
        def log(message: str) -> None:
            self.events.put(("log", str(message)))

        def progress(value: float) -> None:
            self.events.put(("progress", value))

        try:
            function = unpack if operation == "unpack" else repack
            result = function(source, output, log=log, progress=progress)
        except Exception as error:
            self.events.put(("error", (operation, str(error) or error.__class__.__name__)))
        else:
            self.events.put(("done", (operation, output, result)))

    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message.rstrip()}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_events(self) -> None:
        try:
            for _ in range(300):
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "progress":
                    try:
                        value = float(payload)
                        value = value * 100 if value <= 1 else value
                        value = max(0.0, min(100.0, value))
                    except (TypeError, ValueError):
                        continue
                    self.progress_bar["value"] = value
                    self.percent.set(f"{value:.0f}%")
                elif kind == "done":
                    self._complete(*payload)
                elif kind == "error":
                    self._fail(*payload)
        except queue.Empty:
            pass
        self.root.after(75, self._poll_events)

    def _restore_controls(self) -> None:
        self.running = False
        for control in self.controls:
            control.configure(state="normal")

    def _complete(self, operation: str, output: Path, result: object) -> None:
        self._restore_controls()
        verb = "解包" if operation == "unpack" else "封包"
        elapsed = time.monotonic() - self.started_at
        self.progress_bar["value"] = 100
        self.percent.set("100%")
        self.status.set(f"{verb}完成 · {elapsed:.1f} 秒")
        details = result if isinstance(result, dict) else {}
        self.output_location = output if output.is_dir() else output.parent
        report = details.get("report_path") or details.get("report")
        if isinstance(report, (str, Path)) and str(report):
            self.report_path = Path(report)
        count = details.get("file_count", details.get("files_count", details.get("count")))
        count_text = f" · {count} 个文件" if isinstance(count, int) else ""
        lines = [f"{verb}完成{count_text}", f"输出：{output}"]
        if self.report_path is not None:
            lines.append(f"报告：{self.report_path}")
        self.result.set("\n".join(lines))
        self._append_log(f"{verb}完成，用时 {elapsed:.1f} 秒。")
        if self.report_path is not None:
            self._append_log(f"报告：{self.report_path}")
        if self.output_location.exists():
            self.open_output_button.configure(state="normal")
        if self.report_path is not None and self.report_path.is_file():
            self.open_report_button.configure(state="normal")

    def _fail(self, operation: str, error: str) -> None:
        self._restore_controls()
        verb = "解包" if operation == "unpack" else "封包"
        self.status.set(f"{verb}未完成")
        self.result.set(error)
        self._append_log(f"{verb}失败：{error}")
        messagebox.showerror(f"{verb}失败", error, parent=self.root)

    def _open_output(self) -> None:
        if self.output_location is not None:
            self._open_path(self.output_location)

    def _open_report(self) -> None:
        if self.report_path is not None:
            self._open_path(self.report_path)

    def _open_path(self, path: Path) -> None:
        try:
            os.startfile(str(path))
        except OSError as error:
            messagebox.showerror("无法打开", f"{path}\n\n{error}", parent=self.root)

    def _close(self) -> None:
        if self.running:
            messagebox.showinfo("任务正在处理", "请等待当前任务完成后再关闭窗口，以免中断文件写入。", parent=self.root)
            return
        self.root.destroy()


def run_gui() -> None:
    root = tk.Tk()
    MCPKApp(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
