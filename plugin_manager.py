import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from plugin_control import (
    PLUGIN_DEFINITIONS,
    get_plugin_config_path,
    load_plugin_config,
    parse_bool_setting,
    save_plugin_config,
)


APP_DIR = Path(__file__).resolve().parent


class PluginManagerApp(tk.Tk):
    BG = "#f6f9fe"
    SURFACE = "#ffffff"
    TEXT = "#0f172a"
    MUTED = "#64748b"
    BORDER = "#dbe7f5"
    SOFT_BLUE = "#eff6ff"
    PRIMARY = "#2563eb"
    PRIMARY_HOVER = "#1d4ed8"

    def __init__(self) -> None:
        super().__init__()
        self.title("QQ Bot 插件控制台")
        self.geometry("920x660")
        self.minsize(820, 580)
        self.configure(bg=self.BG)

        self.plugin_vars: dict[str, tk.BooleanVar] = {}
        self.setting_vars: dict[str, dict[str, tk.StringVar | tk.BooleanVar]] = {}
        self.status_var = tk.StringVar(value="准备就绪")
        self.docker_status_var = tk.StringVar(value="未刷新")
        self.enabled_summary_var = tk.StringVar(value="")
        self.config_path = get_plugin_config_path()

        self._build_styles()
        self._build_layout()
        self._load_config()
        self._refresh_docker_status()

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Root.TFrame", background=self.BG)
        style.configure("Surface.TFrame", background=self.SURFACE)
        style.configure("List.TFrame", background=self.SURFACE)
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("Subtitle.TLabel", background=self.BG, foreground=self.MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Section.TLabel", background=self.SURFACE, foreground=self.TEXT, font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Body.TLabel", background=self.SURFACE, foreground=self.MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("RowTitle.TLabel", background=self.SURFACE, foreground=self.TEXT, font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("RowMeta.TLabel", background=self.SURFACE, foreground=self.MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Metric.TLabel", background=self.SOFT_BLUE, foreground=self.PRIMARY, font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("MetricCaption.TLabel", background=self.SOFT_BLUE, foreground=self.MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Status.TLabel", background=self.BG, foreground=self.MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Switch.TCheckbutton", background=self.SURFACE, foreground=self.PRIMARY, font=("Microsoft YaHei UI", 10, "bold"))

    def _build_layout(self) -> None:
        root = ttk.Frame(self, style="Root.TFrame", padding=(28, 24))
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        header = ttk.Frame(root, style="Root.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 20))
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text="QQ Bot 插件控制台", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=f"配置文件  {self.config_path}", style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._button(header, "刷新状态", self._refresh_docker_status, "secondary").grid(row=0, column=1, rowspan=2, sticky="e")

        shell = ttk.Frame(root, style="Root.TFrame")
        shell.grid(row=1, column=0, sticky="nsew")
        shell.columnconfigure(0, weight=1)
        shell.columnconfigure(1, minsize=248)
        shell.rowconfigure(0, weight=1)

        panel = tk.Frame(shell, bg=self.SURFACE, highlightbackground=self.BORDER, highlightthickness=1)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)

        panel_head = ttk.Frame(panel, style="Surface.TFrame", padding=(18, 16, 18, 10))
        panel_head.grid(row=0, column=0, sticky="ew")
        panel_head.columnconfigure(0, weight=1)
        ttk.Label(panel_head, text="插件", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(panel_head, text="关闭插件后，保存并重启 bot 生效。", style="Body.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 0))

        quick_actions = ttk.Frame(panel_head, style="Surface.TFrame")
        quick_actions.grid(row=0, column=1, rowspan=2, sticky="e")
        self._button(quick_actions, "全部启用", self._enable_all, "ghost").pack(side="left", padx=(0, 8))
        self._button(quick_actions, "全部关闭", self._disable_all, "ghost").pack(side="left")

        list_wrap = ttk.Frame(panel, style="List.TFrame")
        list_wrap.grid(row=1, column=0, sticky="nsew")
        list_wrap.columnconfigure(0, weight=1)
        list_wrap.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(list_wrap, bg=self.SURFACE, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_wrap, orient="vertical", command=self.canvas.yview)
        self.rows_frame = ttk.Frame(self.canvas, style="List.TFrame")
        self.rows_frame.columnconfigure(0, weight=1)

        self.canvas_window = self.canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.rows_frame.bind("<Configure>", self._sync_scroll_region)
        self.canvas.bind("<Configure>", self._sync_canvas_width)

        for index, plugin in enumerate(PLUGIN_DEFINITIONS):
            self._add_plugin_row(plugin.plugin_id, index)

        sidebar = ttk.Frame(shell, style="Root.TFrame")
        sidebar.grid(row=0, column=1, sticky="nsew")
        sidebar.columnconfigure(0, weight=1)

        self._metric(sidebar, "已启用插件", self.enabled_summary_var).grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self._metric(sidebar, "Docker 状态", self.docker_status_var).grid(row=1, column=0, sticky="ew", pady=(0, 12))

        operations = tk.Frame(sidebar, bg=self.SURFACE, highlightbackground=self.BORDER, highlightthickness=1)
        operations.grid(row=2, column=0, sticky="ew")
        operations.columnconfigure(0, weight=1)
        tk.Label(
            operations,
            text="操作",
            bg=self.SURFACE,
            fg=self.TEXT,
            font=("Microsoft YaHei UI", 12, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(16, 10))
        self._button(operations, "保存配置", self._save_config, "primary").grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._button(operations, "保存并应用", self._save_and_restart, "primary").grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._button(operations, "启动/更新", lambda: self._run_docker_command(("up", "-d", "--build")), "secondary").grid(
            row=3,
            column=0,
            sticky="ew",
            padx=16,
            pady=(0, 8),
        )
        self._button(operations, "停止", lambda: self._run_docker_command(("stop", "qq-bot")), "secondary").grid(
            row=4,
            column=0,
            sticky="ew",
            padx=16,
            pady=(0, 16),
        )

        footer = ttk.Frame(root, style="Root.TFrame")
        footer.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=0, sticky="w")

    def _button(self, parent: tk.Misc, text: str, command: Callable[[], None], kind: str) -> tk.Button:
        if kind == "primary":
            bg = self.PRIMARY
            fg = "#ffffff"
            active_bg = self.PRIMARY_HOVER
            border = self.PRIMARY
        elif kind == "ghost":
            bg = self.SURFACE
            fg = self.PRIMARY
            active_bg = self.SOFT_BLUE
            border = self.SURFACE
        else:
            bg = self.SURFACE
            fg = self.TEXT
            active_bg = self.SOFT_BLUE
            border = self.BORDER

        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=active_bg,
            activeforeground=fg,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=border,
            padx=14,
            pady=9,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold" if kind == "primary" else "normal"),
        )

    def _metric(self, parent: tk.Misc, caption: str, value_var: tk.StringVar) -> tk.Frame:
        frame = tk.Frame(parent, bg=self.SOFT_BLUE, highlightbackground="#d7e7fb", highlightthickness=1)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, textvariable=value_var, style="Metric.TLabel").grid(row=0, column=0, sticky="w", padx=16, pady=(14, 0))
        ttk.Label(frame, text=caption, style="MetricCaption.TLabel").grid(row=1, column=0, sticky="w", padx=16, pady=(0, 14))
        return frame

    def _add_plugin_row(self, plugin_id: str, index: int) -> None:
        plugin = next(item for item in PLUGIN_DEFINITIONS if item.plugin_id == plugin_id)
        var = tk.BooleanVar(master=self, value=True)
        var.trace_add("write", lambda *_args: self._update_enabled_summary())
        self.plugin_vars[plugin.plugin_id] = var

        row = tk.Frame(self.rows_frame, bg=self.SURFACE)
        row.grid(row=index, column=0, sticky="ew")
        row.columnconfigure(0, weight=1)

        content = ttk.Frame(row, style="Surface.TFrame", padding=(18, 14))
        content.grid(row=0, column=0, sticky="ew")
        content.columnconfigure(0, weight=1)

        ttk.Label(content, text=plugin.name, style="RowTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(content, text=plugin.description, style="RowMeta.TLabel", wraplength=460).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(content, text="  ".join(plugin.commands), style="RowMeta.TLabel", wraplength=460).grid(row=2, column=0, sticky="w", pady=(5, 0))
        ttk.Checkbutton(content, text="启用", variable=var, style="Switch.TCheckbutton").grid(row=0, column=1, rowspan=3, sticky="e", padx=(16, 0))

        if plugin.plugin_id == "auto_emoji":
            settings_frame = ttk.Frame(content, style="Surface.TFrame")
            settings_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
            settings_frame.columnconfigure(1, weight=1)
            settings_frame.columnconfigure(3, weight=1)

            target_qq_var = tk.StringVar()
            emoji_ids_var = tk.StringVar(value="127852,12951")
            self.setting_vars[plugin.plugin_id] = {
                "target_qq": target_qq_var,
                "emoji_ids": emoji_ids_var,
            }

            ttk.Label(settings_frame, text="目标 QQ", style="RowMeta.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
            ttk.Entry(settings_frame, textvariable=target_qq_var, width=18).grid(row=0, column=1, sticky="ew", padx=(0, 14))
            ttk.Label(settings_frame, text="表情 ID 列表", style="RowMeta.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 8))
            ttk.Entry(settings_frame, textvariable=emoji_ids_var, width=18).grid(row=0, column=3, sticky="ew")

        if plugin.plugin_id == "daily_wife":
            settings_frame = ttk.Frame(content, style="Surface.TFrame")
            settings_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0), padx=(12, 0))
            settings_frame.columnconfigure(0, weight=1)

            marriage_enabled_var = tk.BooleanVar(master=self, value=True)
            self.setting_vars[plugin.plugin_id] = {"marriage_enabled": marriage_enabled_var}
            marriage_switch = ttk.Checkbutton(
                settings_frame,
                text="启用结芬子功能",
                variable=marriage_enabled_var,
                style="Switch.TCheckbutton",
            )
            marriage_switch.grid(row=0, column=0, sticky="w")
            var.trace_add("write", lambda *_args: marriage_switch.state(["!disabled" if var.get() else "disabled"]))
            ttk.Label(
                settings_frame,
                text="关闭后仅保留每日抽取，不处理 /结芬 及其回应指令。",
                style="RowMeta.TLabel",
                wraplength=420,
            ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        if plugin.plugin_id == "timetable":
            settings_frame = ttk.Frame(content, style="Surface.TFrame")
            settings_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0), padx=(12, 0))
            settings_frame.columnconfigure(0, weight=1)

            isolation_var = tk.BooleanVar(master=self, value=True)
            self.setting_vars[plugin.plugin_id] = {"group_isolation_enabled": isolation_var}
            isolation_switch = ttk.Checkbutton(
                settings_frame,
                text="启用群聊隔离",
                variable=isolation_var,
                style="Switch.TCheckbutton",
            )
            isolation_switch.grid(row=0, column=0, sticky="w")
            var.trace_add("write", lambda *_args: isolation_switch.state(["!disabled" if var.get() else "disabled"]))
            ttk.Label(
                settings_frame,
                text=("默认开启：各群独立课表。关闭后同一 QQ 跨群共享最近更新的一份课表，"
                      "其他共同群成员也可通过 /课ing 查阅。点击“保存并应用”后生效。"),
                style="RowMeta.TLabel",
                wraplength=420,
            ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        if plugin.plugin_id == "summary":
            settings_frame = ttk.Frame(content, style="Surface.TFrame")
            settings_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
            settings_frame.columnconfigure(1, weight=1)

            api_key_var = tk.StringVar()
            base_url_var = tk.StringVar(value="https://api.deepseek.com")
            model_var = tk.StringVar(value="deepseek-chat")
            self.setting_vars[plugin.plugin_id] = {
                "deepseek_api_key": api_key_var,
                "deepseek_api_base_url": base_url_var,
                "deepseek_model": model_var,
            }

            ttk.Label(settings_frame, text="API Key", style="RowMeta.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
            ttk.Entry(settings_frame, textvariable=api_key_var, show="*", width=44).grid(row=0, column=1, sticky="ew", pady=(0, 6))
            ttk.Label(settings_frame, text="API 地址", style="RowMeta.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
            ttk.Entry(settings_frame, textvariable=base_url_var, width=44).grid(row=1, column=1, sticky="ew", pady=(0, 6))
            ttk.Label(settings_frame, text="模型", style="RowMeta.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 8))
            ttk.Entry(settings_frame, textvariable=model_var, width=44).grid(row=2, column=1, sticky="ew")

        separator = tk.Frame(row, bg=self.BORDER, height=1)
        separator.grid(row=1, column=0, sticky="ew", padx=18)

    def _sync_scroll_region(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_canvas_width(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=event.width)

    def _load_config(self) -> None:
        plugin_config = load_plugin_config(self.config_path)
        enabled = set(plugin_config.enabled_plugin_ids)
        for plugin_id, var in self.plugin_vars.items():
            var.set(plugin_id in enabled)
        for plugin_id, plugin_setting_vars in self.setting_vars.items():
            settings = plugin_config.plugin_settings.get(plugin_id, {})
            for key, setting_var in plugin_setting_vars.items():
                if isinstance(setting_var, tk.BooleanVar):
                    setting_var.set(parse_bool_setting(settings.get(key), default=True))
                else:
                    setting_var.set(settings.get(key, setting_var.get()))
        self._update_enabled_summary()
        self.status_var.set(f"已加载配置：启用 {len(enabled)} / {len(PLUGIN_DEFINITIONS)} 个插件")

    def _selected_plugin_ids(self) -> list[str]:
        return [plugin.plugin_id for plugin in PLUGIN_DEFINITIONS if self.plugin_vars[plugin.plugin_id].get()]

    def _selected_plugin_settings(self) -> dict[str, dict[str, str]]:
        return {
            plugin_id: {
                key: ("true" if var.get() else "false") if isinstance(var, tk.BooleanVar) else var.get().strip()
                for key, var in plugin_setting_vars.items()
            }
            for plugin_id, plugin_setting_vars in self.setting_vars.items()
        }

    def _update_enabled_summary(self) -> None:
        self.enabled_summary_var.set(f"{len(self._selected_plugin_ids())} / {len(PLUGIN_DEFINITIONS)}")

    def _save_config(self) -> bool:
        selected_plugin_ids = self._selected_plugin_ids()
        path = save_plugin_config(selected_plugin_ids, self._selected_plugin_settings(), self.config_path)
        self._update_enabled_summary()
        self.status_var.set(f"已保存到 {path}，当前启用 {len(selected_plugin_ids)} 个插件")
        return True

    def _save_and_restart(self) -> None:
        if self._save_config():
            self._run_docker_command(("up", "-d", "--build", "--force-recreate", "qq-bot"))

    def _enable_all(self) -> None:
        for var in self.plugin_vars.values():
            var.set(True)
        self.status_var.set("已选择全部插件，点击保存后生效")

    def _disable_all(self) -> None:
        for var in self.plugin_vars.values():
            var.set(False)
        self.status_var.set("已关闭全部插件，点击保存后生效")

    def _refresh_docker_status(self) -> None:
        self._run_background("刷新 Docker 状态", ("compose", "ps", "qq-bot"), self._set_docker_status)

    def _run_docker_command(self, args: tuple[str, ...]) -> None:
        self._run_background("执行 Docker 命令", ("compose", *args), self._show_docker_result)

    def _run_background(
        self,
        label: str,
        args: tuple[str, ...],
        callback: Callable[[subprocess.CompletedProcess[str]], None],
    ) -> None:
        self.status_var.set(f"{label}中...")

        def worker() -> None:
            try:
                result = subprocess.run(
                    ("docker", *args),
                    cwd=APP_DIR,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=180,
                    check=False,
                )
            except FileNotFoundError:
                self.after(0, lambda: messagebox.showwarning("Docker 未找到", "没有找到 docker 命令，请确认 Docker Desktop 已安装并启动。"))
                self.after(0, lambda: self.status_var.set("Docker 命令不可用"))
                return
            except subprocess.TimeoutExpired:
                self.after(0, lambda: self.status_var.set("Docker 命令超时"))
                return

            self.after(0, lambda: callback(result))

        threading.Thread(target=worker, daemon=True).start()

    def _set_docker_status(self, result: subprocess.CompletedProcess[str]) -> None:
        output = (result.stdout or result.stderr or "").strip()
        if result.returncode != 0:
            self.docker_status_var.set("获取失败")
            self.status_var.set(output or "Docker 状态获取失败")
            return

        lowered = output.lower()
        if "running" in lowered or "up" in lowered:
            self.docker_status_var.set("运行中")
        elif output:
            self.docker_status_var.set("已停止")
        else:
            self.docker_status_var.set("无容器")
        self.status_var.set("状态已刷新")

    def _show_docker_result(self, result: subprocess.CompletedProcess[str]) -> None:
        output = (result.stdout or result.stderr or "").strip()
        if result.returncode == 0:
            self.status_var.set(output or "Docker 命令执行成功")
            self._refresh_docker_status()
            return
        messagebox.showerror("Docker 命令失败", output or f"退出码：{result.returncode}")
        self.status_var.set("Docker 命令执行失败")


def main() -> None:
    app = PluginManagerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
