# -*- coding: utf-8 -*-
"""
app.py — HoI4 音乐播放列表编辑器（GUI）

功能：
  - 选择 music 文件夹，展示/排序/搜索全部歌曲
  - 添加：多选 ogg → 复制入夹 → 设置 song name / 音量 / 播放逻辑；拖放文件到窗口直接添加
  - 批量添加：单独 UI 一次添加多个文件（统一音量/播放逻辑）
  - 编辑：修改 song name、音量、播放逻辑
  - 删除：单选/多选移除条目，可选删除 ogg 文件
  - 全局默认音量：新添加歌曲自动预填，单曲可覆盖
  - 保存：备份原文件后原子写回 music.asset 与 _songs.txt
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from tkinterdnd2 import DND_FILES, TkinterDnD

from PIL import Image, ImageTk

import music_lib
from music_lib import MusicFolder, summarize_conditions

VERSION = "1.6.2"
APP_TITLE = f"HoI4 音乐播放列表编辑器 v{VERSION}"
AUTHOR_NAME = "BulkCooper"
QQ_GROUP = "QQ交流群：1076344041"
DEFAULT_VOLUME = 0.65
DEFAULT_FACTOR = 1.0
ICON_FILE = "logo.ico"
CACHE_DIR_NAME = "hoi4music_cache"


def commit_pending_files(folder: str, pending: dict) -> dict:
    """
    把 pending {目标文件名: cache 路径} 中的文件复制进 folder，处理重名。
    返回 {原目标文件名: 实际复制后的文件名}（未变化的项也包含，值为原名）。
    """
    committed = {}
    for target, src in pending.items():
        if not os.path.exists(src):
            continue
        dest = target
        if os.path.exists(os.path.join(folder, dest)):
            base, ext = os.path.splitext(dest)
            i = 2
            while os.path.exists(os.path.join(folder, f"{base}_{i}{ext}")):
                i += 1
            dest = f"{base}_{i}{ext}"
        shutil.copy2(src, os.path.join(folder, dest))
        committed[target] = dest
    return committed

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                          "HoI4MusicEditor")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "app.log")


def log(msg: str) -> None:
    """追加诊断日志（%APPDATA%\\HoI4MusicEditor\\app.log），失败静默。"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def resource_path(name: str) -> str:
    """打包后从 _MEIPASS 取资源；开发时用脚本所在目录。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def load_config() -> dict:
    """读取用户配置文件；损坏或不存在时返回空字典。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    """写入用户配置文件；失败时静默忽略（不影响主功能）。"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _monitor_rect(win):
    """返回 win 所在显示器工作区 (x, y, w, h)；失败返回 None（用主屏）。"""
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        hwnd = win.winfo_id()
        if not hwnd:
            return None
        monitor = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcWork
            return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:
        pass
    return None


def _explorer_select(path: str) -> bool:
    """
    用 SHOpenFolderAndSelectItems 在资源管理器中打开文件夹并选中目标文件。
    比 `explorer /select` 可靠（后者在 explorer 已运行时可能忽略参数）。
    成功返回 True。
    """
    try:
        import ctypes
        from ctypes import wintypes, POINTER, byref, c_void_p

        path = os.path.normpath(path)  # 确保反斜杠路径（Shell API 对正斜杠解析可能失败）

        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED

        shell32 = ctypes.windll.shell32
        shell32.SHParseDisplayName.argtypes = [
            wintypes.LPCWSTR, c_void_p, POINTER(c_void_p),
            wintypes.DWORD, POINTER(wintypes.DWORD)]
        shell32.SHParseDisplayName.restype = ctypes.c_long
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            c_void_p, wintypes.UINT, POINTER(c_void_p), wintypes.DWORD]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long

        folder = os.path.dirname(path)
        pidl_folder = c_void_p()
        pidl_file = c_void_p()
        attr = wintypes.DWORD()
        if shell32.SHParseDisplayName(folder, None, byref(pidl_folder), 0, byref(attr)) != 0:
            return False
        if shell32.SHParseDisplayName(path, None, byref(pidl_file), 0, byref(attr)) != 0:
            return False
        hr = shell32.SHOpenFolderAndSelectItems(pidl_folder, 1, byref(pidl_file), 0)
        return hr == 0
    except Exception:
        return False


def center_window(win, parent=None):
    """
    将窗口居中于父窗口所在显示器（无 parent 则主屏）。
    小屏保护：窗口尺寸超过显示器时自动缩小到显示器尺寸。
    """
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()

    area = _monitor_rect(parent) if parent is not None else None
    if area is None:
        area = (0, 0, win.winfo_screenwidth(), win.winfo_screenheight())
    ax, ay, aw, ah = area

    # 小屏保护：限制窗口尺寸
    if w > aw:
        w = aw
    if h > ah:
        h = ah
    if w <= 0 or h <= 0:
        return
    win.geometry(f"{w}x{h}")

    x = ax + (aw - w) // 2
    y = ay + (ah - h) // 2
    x = max(x, ax)
    y = max(y, ay)
    win.geometry(f"+{x}+{y}")


class SongDialog(tk.Toplevel):
    """添加/编辑歌曲对话框。result() 返回 dict 或 None（取消）。"""

    def __init__(self, parent, title: str,
                 name: str = "", volume: float = DEFAULT_VOLUME,
                 factor: float = DEFAULT_FACTOR,
                 war: str = "任意", faction: str = "通用",
                 conditions=None, advanced: bool = False):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.geometry("512x449")  # 显式尺寸，避免 wait_visibility 阶段窗口未布局导致裁切
        self._result = None
        conditions = conditions or []

        self._name_var = tk.StringVar(value=name)
        self._name_cache = name          # song name 输入缓存（切主菜单前的内容）
        self._was_main_menu = False      # 是否刚从主菜单切回
        self._volume_var = tk.DoubleVar(value=volume)
        self._factor_var = tk.DoubleVar(value=factor)
        self._war_var = tk.StringVar(value=war)
        self._faction_var = tk.StringVar(value=faction)
        self._advanced_var = tk.BooleanVar(value=advanced)

        pad = {"padx": 10, "pady": 4}
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)

        # ---- 基本信息 ----
        row = ttk.Frame(body)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="song name：", width=12).pack(side="left")
        self._name_entry = ttk.Entry(row, textvariable=self._name_var, width=32)
        self._name_entry.pack(side="left", fill="x", expand=True)

        # 主菜单提示行（单独一行，避免与输入框挤占被截断）
        self._name_hint = ttk.Label(body, text="", foreground="#c0392b")
        self._name_hint.pack(anchor="w", padx=(22, 0))

        row = ttk.Frame(body)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="音量：", width=12).pack(side="left")
        vol_label = ttk.Label(row, text="", width=5, font=("Consolas", 10, "bold"))
        vol_label.pack(side="left")

        def _show_vol(_=None):
            vol_label.config(text=f"{self._volume_var.get():.2f}")

        tk.Scale(row, from_=0.0, to=2.0, resolution=0.05, orient="horizontal",
                 length=130, variable=self._volume_var, command=_show_vol).pack(side="left", padx=4)
        _show_vol()

        ttk.Label(row, text="factor：", width=9).pack(side="left", padx=(10, 0))
        fac_label = ttk.Label(row, text="", width=5, font=("Consolas", 10, "bold"))
        fac_label.pack(side="left")

        def _show_fac(_=None):
            fac_label.config(text=f"{self._factor_var.get():.2f}")

        tk.Scale(row, from_=0.1, to=10.0, resolution=0.1, orient="horizontal",
                 length=130, variable=self._factor_var, command=_show_fac).pack(side="left", padx=4)
        _show_fac()

        # ---- 播放逻辑（预设） ----
        ttk.Label(body, text="播放逻辑（预设组合）：").pack(anchor="w", **pad)
        row = ttk.Frame(body)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="状态：", width=12).pack(side="left")
        for k in music_lib.PRESET_WAR:
            ttk.Radiobutton(row, text=k, value=k, variable=self._war_var).pack(side="left", padx=2)
        ttk.Radiobutton(row, text="主菜单", value="主菜单",
                        variable=self._war_var).pack(side="left", padx=2)
        row = ttk.Frame(body)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="阵营：", width=12).pack(side="left")
        self._faction_radios = []
        for k in music_lib.PRESET_FACTION:
            rb = ttk.Radiobutton(row, text=k, value=k, variable=self._faction_var)
            rb.pack(side="left", padx=2)
            self._faction_radios.append(rb)
        ttk.Label(row, text="（选「主菜单」时禁用）", foreground="gray").pack(side="left", padx=4)
        self._war_var.trace_add("write", lambda *a: self._on_war_changed())

        # ---- 高级自定义 ----
        row = ttk.Frame(body)
        row.pack(fill="x", **pad)
        self._adv_check = ttk.Checkbutton(
            row, text="高级自定义（直接编辑条件）", variable=self._advanced_var,
            command=self._on_advanced_toggle)
        self._adv_check.pack(side="left")
        self._adv_hint = ttk.Label(row, text="  每行一条，如 has_war = yes", foreground="gray")
        self._adv_hint.pack(side="left")

        self._cond_text = tk.Text(body, height=5, width=52, font=("Consolas", 10))
        self._cond_text.pack(fill="x", **pad)
        self._cond_text.insert("1.0", "\n".join(conditions))
        self._update_advanced_state()

        # ---- 按钮 ----
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="确定", command=self._ok).pack(side="right", padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right")

        self.transient(parent)
        self.grab_set()
        self.wait_visibility()
        center_window(self, parent)  # 居中于主窗口所在显示器
        # song name 输入实时缓存（主菜单状态下不覆盖缓存）
        self._name_var.trace_add("write", self._on_name_changed)
        self._on_war_changed()  # 初始化主菜单联动状态（UI 已全部构建）

    # -- 内部 --------------------------------------------------------------

    def _on_name_changed(self, *_a):
        """song name 输入缓存：非主菜单状态时每次修改覆盖缓存。"""
        if self._war_var.get() != "主菜单":
            self._name_cache = self._name_var.get()

    def _on_war_changed(self):
        """选「主菜单」：缓存当前 song name 并自动改为 maintheme，禁用阵营与高级自定义；
        切回其他状态：恢复缓存内容。"""
        is_main = self._war_var.get() == "主菜单"
        if is_main:
            if not self._was_main_menu:
                self._name_cache = self._name_var.get()
                self._name_var.set("maintheme")
            self._was_main_menu = True
        else:
            if self._was_main_menu and self._name_cache is not None:
                self._name_var.set(self._name_cache)
            self._was_main_menu = False
        state = "disabled" if is_main else "normal"
        for rb in self._faction_radios:
            rb.configure(state=state)
        adv_check = getattr(self, "_adv_check", None)
        if adv_check is not None:
            adv_check.configure(state=state)
        name_entry = getattr(self, "_name_entry", None)
        if name_entry is not None:
            # 主菜单锁定 song name（必须为 maintheme，不可手动改）
            name_entry.configure(state=state)
        name_hint = getattr(self, "_name_hint", None)
        if name_hint is not None:
            # 主菜单时在名称后加注释说明
            name_hint.config(
                text="主菜单音乐：名称已锁定为 maintheme（引擎固定播放这首歌）" if is_main else "")
        self._update_advanced_state()

    def _on_advanced_toggle(self):
        self._update_advanced_state()

    def _update_advanced_state(self):
        adv = self._advanced_var.get()
        state = "normal" if adv else "disabled"
        self._cond_text.configure(state=state)
        if not adv:
            # 用预设生成预览
            if self._war_var.get() == "主菜单":
                conds: list = []
            else:
                conds = music_lib.conditions_from_preset(
                    self._war_var.get(), self._faction_var.get())
            self._cond_text.configure(state="normal")
            self._cond_text.delete("1.0", "end")
            self._cond_text.insert("1.0", "\n".join(conds))
            self._cond_text.configure(state="disabled")

    def _parse_conditions(self) -> list:
        if self._war_var.get() == "主菜单":
            # 主菜单音乐：引擎固定播放名为 maintheme 的歌曲；写入哨兵注释以便持久化识别
            return [music_lib.MAIN_THEME_COND]
        if not self._advanced_var.get():
            return music_lib.conditions_from_preset(
                self._war_var.get(), self._faction_var.get())
        raw = self._cond_text.get("1.0", "end")
        conds = []
        for line in raw.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                conds.append(s)
        return conds

    def _ok(self):
        name = self._name_var.get().strip()
        if not name:
            messagebox.showerror("错误", "song name 不能为空", parent=self)
            return
        if self._war_var.get() == "主菜单" and name != "maintheme":
            if not messagebox.askyesno(
                    "主菜单音乐",
                    "主菜单只播放名为 maintheme 的歌曲（引擎写死）。\n"
                    f"当前 song name 为「{name}」，游戏主菜单不会播放它。\n"
                    "仍以主菜单逻辑保存吗？（可稍后在编辑中改名为 maintheme）",
                    parent=self):
                return
        try:
            volume = float(self._volume_var.get())
            factor = float(self._factor_var.get())
        except ValueError:
            messagebox.showerror("错误", "音量与 factor 必须是数字", parent=self)
            return
        if not (0.0 <= volume <= 2.0):
            messagebox.showerror("错误", "音量范围 0.0 – 2.0", parent=self)
            return
        self._result = {
            "name": name,
            "volume": volume,
            "factor": factor,
            "conditions": self._parse_conditions(),
            "advanced": self._advanced_var.get(),
        }
        self.destroy()

    def result(self):
        return self._result


class BatchAddDialog(tk.Toplevel):
    """批量添加对话框：一次添加多个文件，统一音量与播放逻辑。"""

    def __init__(self, parent, volume: float = DEFAULT_VOLUME,
                 factor: float = DEFAULT_FACTOR):
        super().__init__(parent)
        self.title("批量添加歌曲")
        self.resizable(False, False)
        self.geometry("580x600")  # 显式尺寸，避免 wait_visibility 阶段窗口未布局导致裁切
        self._result = None
        self._files: list = []

        self._volume_var = tk.DoubleVar(value=volume)
        self._factor_var = tk.DoubleVar(value=factor)
        self._war_var = tk.StringVar(value="任意")
        self._faction_var = tk.StringVar(value="通用")
        self._advanced_var = tk.BooleanVar(value=False)

        pad = {"padx": 10, "pady": 4}
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)

        # ---- 文件列表 ----
        ttk.Label(body, text="要添加的音频文件（可继续添加/移除）：").pack(anchor="w", **pad)
        lf = ttk.Frame(body)
        lf.pack(fill="both", expand=True, **pad)
        self._listbox = tk.Listbox(lf, selectmode="extended", height=8)
        sb = ttk.Scrollbar(lf, orient="vertical", command=self._listbox.yview)
        self._listbox.configure(yscrollcommand=sb.set)
        self._listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        fbtns = ttk.Frame(body)
        fbtns.pack(fill="x", **pad)
        ttk.Button(fbtns, text="添加文件...", command=self._pick_files).pack(side="left")
        ttk.Button(fbtns, text="移除选中", command=self._remove_selected).pack(side="left", padx=6)
        ttk.Button(fbtns, text="清空", command=self._clear_files).pack(side="left")
        self._count_var = tk.StringVar(value="共 0 个文件")
        ttk.Label(fbtns, textvariable=self._count_var, foreground="gray").pack(side="right")

        # ---- 统一设置 ----
        ttk.Separator(body).pack(fill="x", pady=6)
        ttk.Label(body, text="统一设置（应用到所有文件，song name 自动按文件名生成）：").pack(anchor="w", **pad)

        row = ttk.Frame(body)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="音量：", width=12).pack(side="left")
        vol_label = ttk.Label(row, text="", width=5, font=("Consolas", 10, "bold"))
        vol_label.pack(side="left")

        def _show_vol(_=None):
            vol_label.config(text=f"{self._volume_var.get():.2f}")

        tk.Scale(row, from_=0.0, to=2.0, resolution=0.05, orient="horizontal",
                 length=140, variable=self._volume_var, command=_show_vol).pack(side="left", padx=4)
        _show_vol()

        ttk.Label(row, text="factor：", width=9).pack(side="left", padx=(10, 0))
        fac_label = ttk.Label(row, text="", width=5, font=("Consolas", 10, "bold"))
        fac_label.pack(side="left")

        def _show_fac(_=None):
            fac_label.config(text=f"{self._factor_var.get():.2f}")

        tk.Scale(row, from_=0.1, to=10.0, resolution=0.1, orient="horizontal",
                 length=140, variable=self._factor_var, command=_show_fac).pack(side="left", padx=4)
        _show_fac()

        # 播放逻辑（预设）
        ttk.Label(body, text="播放逻辑（预设组合）：").pack(anchor="w", **pad)
        row = ttk.Frame(body)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="状态：", width=12).pack(side="left")
        for k in music_lib.PRESET_WAR:
            ttk.Radiobutton(row, text=k, value=k, variable=self._war_var).pack(side="left", padx=2)
        row = ttk.Frame(body)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="阵营：", width=12).pack(side="left")
        for k in music_lib.PRESET_FACTION:
            ttk.Radiobutton(row, text=k, value=k, variable=self._faction_var).pack(side="left", padx=2)

        # 高级自定义
        row = ttk.Frame(body)
        row.pack(fill="x", **pad)
        self._adv_check = ttk.Checkbutton(
            row, text="高级自定义（直接编辑条件）", variable=self._advanced_var,
            command=self._on_advanced_toggle)
        self._adv_check.pack(side="left")
        self._adv_hint = ttk.Label(row, text="  每行一条，如 has_war = yes", foreground="gray")
        self._adv_hint.pack(side="left")
        self._adv_text = tk.Text(body, height=3, state="disabled")
        self._adv_text.pack(fill="x", padx=10, pady=(0, 6))

        # 底部按钮
        btm = ttk.Frame(body)
        btm.pack(fill="x", pady=(8, 0))
        ttk.Button(btm, text="确定添加", command=self._ok).pack(side="right")
        ttk.Button(btm, text="取消", command=self.destroy).pack(side="right", padx=6)

        self.transient(parent)
        self.grab_set()
        self.wait_visibility()
        self.update_idletasks()
        center_window(self, parent)

    def _on_advanced_toggle(self):
        if self._advanced_var.get():
            self._adv_text.config(state="normal")
            self._adv_hint.config(text="  预设条件会自动填入，可在此基础上修改")
        else:
            self._adv_text.config(state="disabled")
            self._adv_hint.config(text="  每行一条，如 has_war = yes")

    def _pick_files(self):
        files = filedialog.askopenfilenames(
            title="选择音频文件（可多选）",
            filetypes=[("音频文件", "*.ogg *.mp3 *.flac *.wav *.m4a *.aac"),
                       ("所有文件", "*.*")])
        for f in files:
            if f not in self._files:
                self._files.append(f)
        self._update_list()

    def _remove_selected(self):
        for i in reversed(self._listbox.curselection()):
            self._files.pop(i)
        self._update_list()

    def _clear_files(self):
        self._files = []
        self._update_list()

    def _update_list(self):
        self._listbox.delete(0, "end")
        for f in self._files:
            self._listbox.insert("end", os.path.basename(f))
        self._count_var.set(f"共 {len(self._files)} 个文件")

    def _ok(self):
        if not self._files:
            messagebox.showinfo("提示", "请先添加要批量添加的文件", parent=self)
            return
        if self._advanced_var.get():
            conditions = [l.strip() for l in self._adv_text.get("1.0", "end").splitlines() if l.strip()]
            war, faction = "任意", "通用"
        else:
            conditions = music_lib.conditions_from_preset(
                self._war_var.get(), self._faction_var.get())
            war, faction = self._war_var.get(), self._faction_var.get()
        self._result = {
            "files": list(self._files),
            "volume": round(self._volume_var.get(), 2),
            "factor": round(self._factor_var.get(), 2),
            "war": war, "faction": faction,
            "conditions": conditions,
            "advanced": self._advanced_var.get(),
        }
        self.destroy()

    def result(self):
        return self._result


class ImageCropDialog(tk.Toplevel):
    """电台图标裁剪编辑器：选图 → 拖拽选区（锁定 152:120 比例）→ 两帧预览 → 生成图标。
    result() 返回 True（已生成）或 None（取消）。"""

    RATIO_W, RATIO_H = 152, 120   # 游戏单帧比例（原版 base_game_album_art.dds 权威尺寸）
    CANVAS_W, CANVAS_H = 560, 440
    PREVIEW_W, PREVIEW_H = 114, 90  # 预览缩略图尺寸（152x120 的 75%）

    def __init__(self, parent, out_path: str, src_path: str = ""):
        super().__init__(parent)
        self.title("裁剪电台图标")
        self.resizable(False, False)
        self.geometry("880x580")
        self._result = None
        self.out_path = out_path

        self._img: Image.Image = None       # 原图
        self._canvas_photo = None           # 画布底图（保持引用）
        self._prev_photo1 = None
        self._prev_photo2 = None
        self._scale = 1.0                   # 画布 → 图像 缩放
        self._ox = self._oy = 0.0           # 图像显示区域左上角（画布坐标）
        self._dw = self._dh = 0             # 图像显示区域宽高
        self._sel = None                    # 选区（画布坐标 x,y,w,h）
        self._mode = None                   # None | draw | move | handle
        self._drag_anchor = None            # 拖动锚点（画布坐标）
        self._fixed_corner = None           # handle/draw 固定角（画布坐标）
        self._src_label_text = tk.StringVar(value="未选择图片")

        # ---- 布局 ----
        pad = {"padx": 10, "pady": 4}
        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)

        top = ttk.Frame(body)
        top.pack(fill="x", **pad)
        ttk.Button(top, text="选择图片…", command=self._pick_image).pack(side="left")
        ttk.Label(top, textvariable=self._src_label_text, foreground="gray").pack(
            side="left", padx=8)

        main = ttk.Frame(body)
        main.pack(fill="both", expand=True, **pad)

        # 左侧画布
        self._canvas = tk.Canvas(main, width=self.CANVAS_W, height=self.CANVAS_H,
                                 bg="#1e1e1e", highlightthickness=1,
                                 highlightbackground="#444444")
        self._canvas.pack(side="left")
        self._canvas.bind("<Button-1>", self._on_press)
        self._canvas.bind("<B1-Motion>", self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)

        # 右侧面板
        side = ttk.Frame(main, width=250)
        side.pack(side="left", fill="y", padx=(12, 0))
        side.pack_propagate(False)
        ttk.Label(side, text="裁剪预览（锁定比例 152:120）",
                  font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        ttk.Label(side, text="未选中帧：").pack(anchor="w", pady=(8, 0))
        self._prev1 = ttk.Label(side, relief="solid", borderwidth=1)
        self._prev1.pack(anchor="w")
        ttk.Label(side, text="选中帧（高亮）：").pack(anchor="w", pady=(8, 0))
        self._prev2 = ttk.Label(side, relief="solid", borderwidth=1)
        self._prev2.pack(anchor="w")
        ttk.Label(side, text="提示：拖拽画框选裁剪区；\n框内拖动可移动；拖动四角\n调整大小；点「自动适配」\n恢复整图居中裁剪。",
                  foreground="gray", justify="left").pack(anchor="w", pady=(12, 0))

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="自动适配", command=self._auto_fit).pack(side="left")
        ttk.Button(btns, text="确定", command=self._ok).pack(side="right", padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right")

        self.transient(parent)
        self.grab_set()
        self.wait_visibility()
        center_window(self, parent)

        if src_path:
            self._load_image(src_path)

    # -- 图片加载 ----------------------------------------------------------

    def _pick_image(self):
        path = filedialog.askopenfilename(
            parent=self, title="选择电台图标图片",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.bmp *.webp *.gif"),
                       ("所有文件", "*.*")])
        if path:
            try:
                self._load_image(path)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("错误", f"无法打开图片：\n{e}", parent=self)

    def _load_image(self, path: str):
        with Image.open(path) as im:
            im.load()
            self._img = im.copy()
        self._src_label_text.set(os.path.basename(path))
        self._auto_fit()

    # -- 选区几何 ----------------------------------------------------------

    def _auto_fit(self):
        """默认选区：图像内按比例 152:120 的最大内接框，居中。"""
        if self._img is None:
            return
        W, H = self._img.size
        # 画布可用区（留 20px 边距）
        cw, ch = self.CANVAS_W - 20, self.CANVAS_H - 20
        self._scale = min(cw / W, ch / H)
        self._dw, self._dh = max(1, round(W * self._scale)), max(1, round(H * self._scale))
        self._ox, self._oy = (self.CANVAS_W - self._dw) / 2, (self.CANVAS_H - self._dh) / 2
        r = self.RATIO_W / self.RATIO_H
        if self._dw / self._dh > r:          # 图偏宽 → 高度撑满，宽按比例
            h = self._dh
            w = h * r
        else:                                 # 图偏高 → 宽度撑满，高按比例
            w = self._dw
            h = w / r
        x = self._ox + (self._dw - w) / 2
        y = self._oy + (self._dh - h) / 2
        self._sel = [x, y, w, h]
        self._redraw()

    def _clamp_sel(self, sel, anchor=None):
        """将选区约束在图像区域内并保持比例；anchor 为固定角（画布坐标）。"""
        x, y, w, h = sel
        left, top = self._ox, self._oy
        right, bottom = left + self._dw, top + self._dh
        r = self.RATIO_W / self.RATIO_H
        # 先约束宽
        w = max(10.0, min(w, right - left))
        h = w / r
        if h > bottom - top:
            h = max(10.0, min(h, bottom - top))
            w = h * r
            w = max(10.0, min(w, right - left))
        # 固定角存在时，从固定角反向推导位置；否则保持左上角
        if anchor is not None:
            ax, ay = anchor
            if ax <= x:    # 固定角在左侧
                x = ax
            else:
                x = ax - w
            if ay <= y:
                y = ay
            else:
                y = ay - h
        x = max(left, min(x, right - w))
        y = max(top, min(y, bottom - h))
        return [x, y, w, h]

    # -- 鼠标交互 ----------------------------------------------------------

    def _hit_handle(self, cx, cy):
        """命中四角手柄 → 返回角名；否则 None。"""
        if self._sel is None:
            return None
        x, y, w, h = self._sel
        size = 8
        for name, (hx, hy) in (("tl", (x, y)), ("tr", (x + w, y)),
                               ("bl", (x, y + h)), ("br", (x + w, y + h))):
            if abs(cx - hx) <= size and abs(cy - hy) <= size:
                return name
        return None

    def _on_press(self, e):
        if self._img is None:
            return
        cx, cy = e.x, e.y
        handle = self._hit_handle(cx, cy)
        if handle:
            self._mode = "handle"
            # 固定角 = 对角
            x, y, w, h = self._sel
            corners = {"tl": (x + w, y + h), "tr": (x, y + h),
                       "bl": (x + w, y), "br": (x, y)}
            self._fixed_corner = corners[handle]
            self._drag_anchor = (cx, cy)
        elif self._sel and (self._sel[0] <= cx <= self._sel[0] + self._sel[2]
                            and self._sel[1] <= cy <= self._sel[1] + self._sel[3]):
            self._mode = "move"
            self._drag_anchor = (cx - self._sel[0], cy - self._sel[1])
        else:
            self._mode = "draw"
            self._fixed_corner = (cx, cy)
            self._drag_anchor = (cx, cy)
            self._sel = self._clamp_sel([cx, cy, 10, 10 / (self.RATIO_W / self.RATIO_H)],
                                        anchor=(cx, cy))

    def _on_drag(self, e):
        if self._mode is None or self._img is None:
            return
        cx, cy = e.x, e.y
        if self._mode == "move":
            x = cx - self._drag_anchor[0]
            y = cy - self._drag_anchor[1]
            self._sel = self._clamp_sel([x, y, self._sel[2], self._sel[3]])
        elif self._mode in ("draw", "handle"):
            # 从固定角按比例扩展
            ax, ay = self._fixed_corner
            dx, dy = cx - ax, cy - ay
            r = self.RATIO_W / self.RATIO_H
            w = max(abs(dx), abs(dy) * r)
            x = ax if dx >= 0 else ax - w
            y = ay if dy >= 0 else ay - w / r
            self._sel = self._clamp_sel([x, y, w, w / r], anchor=(ax, ay))
        self._redraw()

    def _on_release(self, _e):
        self._mode = None
        self._drag_anchor = None

    # -- 绘制与预览 --------------------------------------------------------

    def _redraw(self):
        c = self._canvas
        c.delete("all")
        if self._img is None:
            c.create_text(self.CANVAS_W // 2, self.CANVAS_H // 2,
                          text="请先选择一张图片", fill="#888888")
            return
        # 底图
        disp = self._img.resize((self._dw, self._dh), Image.LANCZOS)
        self._canvas_photo = ImageTk.PhotoImage(disp)
        c.create_image(self._ox, self._oy, anchor="nw", image=self._canvas_photo)
        # 选区
        if self._sel:
            x, y, w, h = self._sel
            c.create_rectangle(x, y, x + w, y + h, outline="#ffd54f", width=2)
            for hx, hy in ((x, y), (x + w, y), (x, y + h), (x + w, y + h)):
                c.create_rectangle(hx - 4, hy - 4, hx + 4, hy + 4,
                                   fill="#ffd54f", outline="")
        self._update_preview()

    def _update_preview(self):
        if self._img is None or self._sel is None:
            return
        x, y, w, h = self._sel
        ix = int((x - self._ox) / self._scale)
        iy = int((y - self._oy) / self._scale)
        iw = max(1, int(w / self._scale))
        ih = max(1, int(h / self._scale))
        crop = self._img.crop((ix, iy, ix + iw, iy + ih))
        frame = crop.resize((self.RATIO_W, self.RATIO_H), Image.LANCZOS)
        from PIL import ImageEnhance
        sel_frame = ImageEnhance.Brightness(frame).enhance(1.18)
        size = (self.PREVIEW_W, self.PREVIEW_H)
        self._prev_photo1 = ImageTk.PhotoImage(frame.resize(size))
        self._prev_photo2 = ImageTk.PhotoImage(sel_frame.resize(size))
        self._prev1.configure(image=self._prev_photo1)
        self._prev2.configure(image=self._prev_photo2)

    # -- 完成 --------------------------------------------------------------

    def _ok(self):
        if self._img is None or self._sel is None:
            messagebox.showerror("错误", "请先选择图片", parent=self)
            return
        x, y, w, h = self._sel
        ix = int((x - self._ox) / self._scale)
        iy = int((y - self._oy) / self._scale)
        iw = max(1, int(w / self._scale))
        ih = max(1, int(h / self._scale))
        crop = self._img.crop((ix, iy, ix + iw, iy + ih))
        try:
            music_lib.compose_album_art(
                crop.resize((self.RATIO_W, self.RATIO_H), Image.LANCZOS),
                self.out_path, with_border=True)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"生成图标失败：\n{e}", parent=self)
            return
        self._result = True
        self.destroy()

    def result(self):
        return self._result


def default_mod_dir() -> str:
    """HoI4 本地 mod 默认目录：文档\\Paradox Interactive\\Hearts of Iron IV\\mod\\"""
    docs = os.path.expanduser("~/Documents")
    return os.path.join(docs, "Paradox Interactive", "Hearts of Iron IV", "mod")


def _shorten(text: str, maxlen: int = 46) -> str:
    """超长文本中间省略，防止工具栏被撑爆（Tk pack 会 unmap 同排其他控件）。"""
    if len(text) <= maxlen:
        return text
    half = (maxlen - 1) // 2
    return text[:half] + "…" + text[-half:]


class RadioModDialog(tk.Toplevel):
    """新建本地电台 MOD 对话框。result() 返回 dict 或 None（取消）。"""

    def __init__(self, parent, mod_dir: str = ""):
        super().__init__(parent)
        self.title("新建本地电台 MOD")
        self.resizable(False, False)
        self.geometry("520x300")
        self._result = None

        self._id_var = tk.StringVar(value="radio_my")
        self._name_var = tk.StringVar(value="我的电台")
        self._ver_var = tk.StringVar(value=music_lib.DEFAULT_SUPPORTED_VERSION)
        self._dir_var = tk.StringVar(value=mod_dir or default_mod_dir())

        pad = {"padx": 10, "pady": 4}
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)

        row = ttk.Frame(body)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="电台 ID：", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self._id_var, width=30).pack(side="left")
        ttk.Label(row, text="字母开头，字母/数字/下划线，≤32 字符",
                  foreground="gray").pack(side="left", padx=6)

        row = ttk.Frame(body)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="显示名：", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self._name_var, width=30).pack(side="left")
        ttk.Label(row, text="游戏播放器里显示的名字，可中文", foreground="gray").pack(side="left", padx=6)

        row = ttk.Frame(body)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="游戏版本：", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self._ver_var, width=14).pack(side="left")
        ttk.Label(row, text="supported_version，默认 1.* 通配", foreground="gray").pack(side="left", padx=6)

        row = ttk.Frame(body)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="目标 mod 目录：", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self._dir_var, width=34).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…", command=self._browse).pack(side="left", padx=4)

        ttk.Label(body, text="生成后自动加载，可立即添加歌曲；产物为完整独立 MOD，"
                             "不修改游戏本体与已有 mod。\n注意：歌曲 name 全局唯一，"
                             "请勿与游戏本体/其他 mod 重名（重名歌曲会被游戏忽略）。",
                  foreground="gray", wraplength=480, justify="left").pack(anchor="w", **pad)

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="确定", command=self._ok).pack(side="right", padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right")

        self.transient(parent)
        self.grab_set()
        self.wait_visibility()
        center_window(self, parent)
        self._id_var.trace_add("write", lambda *a: None)  # 保持引用

    def _browse(self):
        path = filedialog.askdirectory(parent=self, title="选择 mod 目录",
                                       initialdir=self._dir_var.get() or None)
        if path:
            self._dir_var.set(path)

    def _ok(self):
        radio_id = self._id_var.get().strip()
        if not music_lib.is_valid_radio_id(radio_id):
            messagebox.showerror("错误",
                                 "电台 ID 不合法：需字母开头，仅含字母/数字/下划线，长度≤32",
                                 parent=self)
            return
        display = self._name_var.get().strip() or radio_id
        version = self._ver_var.get().strip() or music_lib.DEFAULT_SUPPORTED_VERSION
        mod_dir = self._dir_var.get().strip()
        if not mod_dir:
            messagebox.showerror("错误", "请选择目标 mod 目录", parent=self)
            return
        if not os.path.isdir(mod_dir):
            if not messagebox.askyesno("目录不存在",
                                       f"目录不存在：\n{mod_dir}\n\n将自动创建该目录。是否继续？",
                                       parent=self):
                return
        self._result = {"radio_id": radio_id, "display": display,
                        "version": version, "mod_dir": mod_dir}
        self.destroy()

    def result(self):
        return self._result


class RadioModOpenDialog(tk.Toplevel):
    """打开本地电台 MOD：合并显示索引电台（任意目录）+ 默认目录扫描。
    支持移除索引、编辑索引位置。result() 返回 dict(radio_id, mod_dir) 或 None。"""

    def __init__(self, parent, mod_dir: str = ""):
        super().__init__(parent)
        self.title("打开本地电台 MOD")
        self.resizable(False, False)
        self.geometry("560x400")
        self._result = None
        self._mod_dir = mod_dir or default_mod_dir()
        self._entries = []   # [(radio_id, display_name, folder, is_indexed)]

        pad = {"padx": 10, "pady": 4}
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)

        top = ttk.Frame(body)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="扫描目录：").pack(side="left")
        self._dir_label = ttk.Label(top, text=self._mod_dir, foreground="gray")
        self._dir_label.pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="浏览…", command=self._browse).pack(side="left", padx=4)

        self._listbox = tk.Listbox(body, height=9, font=("Microsoft YaHei UI", 10))
        self._listbox.pack(fill="both", expand=True, **pad)
        self._listbox.bind("<Double-1>", lambda e: self._ok())
        ttk.Label(body, text="[索引] 条目来自索引（可移除/编辑位置）；其余为当前目录扫描。",
                  foreground="gray").pack(anchor="w", **pad)

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="移除索引", command=self._remove_index).pack(side="left")
        ttk.Button(btns, text="编辑位置…", command=self._edit_position).pack(side="left", padx=4)
        ttk.Button(btns, text="确定", command=self._ok).pack(side="right", padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right")

        self._scan()
        self.transient(parent)
        self.grab_set()
        self.wait_visibility()
        center_window(self, parent)

    def _scan(self):
        self._listbox.delete(0, "end")
        self._entries = []
        seen = set()
        # 索引条目（任意目录创建的电台）
        for e in music_lib.load_radio_index():
            rid = e.get("radio_id", "")
            disp = e.get("display_name") or rid
            folder = e.get("mod_dir", "")
            if not rid or not folder:
                continue
            key = (rid, os.path.normpath(folder).lower())
            if key in seen:
                continue
            seen.add(key)
            exists = os.path.isdir(os.path.join(folder, rid))
            tag = "" if exists else "（位置失效，可编辑位置修正）"
            self._entries.append((rid, disp, folder, True))
            self._listbox.insert(
                "end", f"[索引] {disp}（{rid}）→ {os.path.basename(folder)}{tag}")
        # 默认目录扫描
        for rid, disp, folder in music_lib.find_radio_mods(self._mod_dir):
            key = (rid, os.path.normpath(folder).lower())
            if key in seen:
                continue
            seen.add(key)
            self._entries.append((rid, disp, folder, False))
            self._listbox.insert(
                "end", f"{disp}（{rid}）→ {os.path.basename(folder)}")
        if not self._entries:
            self._listbox.insert("end", "（未找到本地电台 MOD）")
            self._listbox.itemconfig("end", foreground="gray")

    def _browse(self):
        path = filedialog.askdirectory(parent=self, title="选择 mod 目录",
                                       initialdir=self._mod_dir)
        if path:
            self._mod_dir = path
            self._dir_label.config(text=path)
            self._scan()

    def _selected(self):
        sel = self._listbox.curselection()
        if not sel or not self._entries or sel[0] >= len(self._entries):
            return None
        return self._entries[sel[0]]

    def _ok(self):
        item = self._selected()
        if not item:
            messagebox.showinfo("提示", "请先选择要打开的电台", parent=self)
            return
        rid, _disp, folder, _is_idx = item
        self._result = {"radio_id": rid, "mod_dir": folder}
        self.destroy()

    def _remove_index(self):
        item = self._selected()
        if not item:
            return
        rid, disp, folder, is_idx = item
        if not is_idx:
            messagebox.showinfo("提示", "该电台来自目录扫描，无索引可移除", parent=self)
            return
        if messagebox.askyesno(
                "移除索引",
                f"将移除电台「{disp}（{rid}）」的索引记录。\n"
                "不会删除任何电台文件。是否继续？", parent=self):
            music_lib.index_remove_radio(rid, folder)
            self._scan()

    def _edit_position(self):
        item = self._selected()
        if not item:
            return
        rid, disp, folder, is_idx = item
        if not is_idx:
            messagebox.showinfo("提示", "该电台来自目录扫描，可直接浏览到对应目录",
                                parent=self)
            return
        path = filedialog.askdirectory(
            parent=self,
            title=f"选择「{disp}」的新位置（包含电台文件夹 {rid}\\ 的目录）",
            initialdir=folder if os.path.isdir(folder) else None)
        if path and os.path.normpath(path) != os.path.normpath(folder):
            music_lib.index_update_radio(rid, folder, path)
            self._scan()

    def result(self):
        return self._result


class StartGameDialog(tk.Toplevel):
    """开局音效对话框：选音频 → 转 WAV 探测时长 → 裁剪区间（≤15s）→ 音量 → 自动安装。
    result() 返回 True（已安装）或 None（取消）。"""

    MAX_SECONDS = music_lib.START_GAME_MAX_SECONDS

    def __init__(self, parent, radio_mod):
        super().__init__(parent)
        self.title("开局音效（start_game_01）")
        self.resizable(False, False)
        self.geometry("580x450")
        self._result = None
        self._rm = radio_mod
        self._src = ""
        self._total = 0.0
        self._busy = False
        self._q = queue.Queue()
        self._probe = os.path.join(tempfile.gettempdir(), CACHE_DIR_NAME,
                                   "startgame_probe.wav")
        os.makedirs(os.path.dirname(self._probe), exist_ok=True)

        self._file_var = tk.StringVar(value="未选择音频文件")
        self._status_var = tk.StringVar(
            value="请选择音频文件（mp3 / flac / wav / m4a / aac / ogg）")
        self._sg_status_var = tk.StringVar(value="")
        self._start_var = tk.DoubleVar(value=0.0)
        self._end_var = tk.DoubleVar(value=0.0)
        self._vol_var = tk.DoubleVar(value=0.1)
        self._fade_var = tk.BooleanVar(value=False)
        self._fade_sec_var = tk.DoubleVar(value=2.0)
        self._len_var = tk.StringVar(value="")
        self._drag_handle = None     # 区间条拖拽中：None | "start" | "end"
        self._playing = False        # 试听中
        self._preview_path = os.path.join(tempfile.gettempdir(), CACHE_DIR_NAME,
                                          "startgame_preview.wav")

        pad = {"padx": 10, "pady": 4}
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)

        # 文件选择
        row = ttk.Frame(body)
        row.pack(fill="x", **pad)
        ttk.Button(row, text="选择音频文件…", command=self._pick_file).pack(side="left")
        ttk.Label(row, textvariable=self._file_var, foreground="gray").pack(
            side="left", padx=8)
        ttk.Label(body, textvariable=self._status_var, foreground="#1a7f37").pack(
            anchor="w", **pad)
        # 当前开局音效状态（已安装 / 未安装 + 时长与音量）
        self._sg_status_label = ttk.Label(body, textvariable=self._sg_status_var,
                                          foreground="#1a5fb4", font=("Microsoft YaHei UI", 9, "bold"))
        self._sg_status_label.pack(anchor="w", **pad)

        # 裁剪区间：单条双端区间条（左手柄=开头，右手柄=结尾，中间为保留区间）
        ttk.Label(body, text="裁剪区间（点击开始游戏 → 加载界面播放，时长 ≤ 12 秒）：").pack(
            anchor="w", **pad)
        self._range_canvas = tk.Canvas(body, width=560, height=44, bg="#1e1e1e",
                                       highlightthickness=0)
        self._range_canvas.pack(fill="x", **pad)
        self._range_canvas.bind("<Button-1>", self._on_bar_press)
        self._range_canvas.bind("<B1-Motion>", self._on_bar_drag)
        self._range_canvas.bind("<ButtonRelease-1>", self._on_bar_release)
        self.BAR_X0, self.BAR_X1 = 16, 544   # 轨道左右边界（画布坐标）
        self.BAR_Y = 20                       # 轨道中线

        # 数值显示 + 试听
        frm = ttk.Frame(body)
        frm.pack(fill="x", **pad)
        self._len_label = ttk.Label(frm, textvariable=self._len_var, foreground="#1a7f37")
        self._len_label.pack(side="left")
        self._preview_btn = ttk.Button(frm, text="试听", command=self._toggle_preview,
                                       state="disabled")
        self._preview_btn.pack(side="right")
        self._range_info = tk.StringVar(value="开始 0.0s / 结束 0.0s")
        self._range_info_label = ttk.Label(frm, textvariable=self._range_info,
                                           foreground="gray")
        self._range_info_label.pack(side="left", padx=(14, 0))

        # 音量
        frm = ttk.Frame(body)
        frm.pack(fill="x", **pad)
        ttk.Label(frm, text="音量：", width=8).pack(side="left")
        vol_label = ttk.Label(frm, text="", width=5, font=("Consolas", 10, "bold"))
        vol_label.pack(side="left")

        def _show_vol(_=None):
            vol_label.config(text=f"{self._vol_var.get():.2f}")

        tk.Scale(frm, from_=0.0, to=1.0, resolution=0.05, orient="horizontal",
                 length=240, variable=self._vol_var, command=_show_vol).pack(
            side="left", padx=4)
        _show_vol()
        ttk.Label(frm, text="原版默认 0.1，音量过大请调低", foreground="gray").pack(
            side="left", padx=6)

        # 淡出选项（可勾选：最后几秒音量渐降到 0，时长可设置）
        frm = ttk.Frame(body)
        frm.pack(fill="x", **pad)
        self._fade_check = ttk.Checkbutton(frm, text="最后几秒淡出",
                                           variable=self._fade_var)
        self._fade_check.pack(side="left")
        ttk.Label(frm, text="淡出时长：", foreground="gray").pack(side="left", padx=(10, 0))
        ttk.Spinbox(frm, from_=0.5, to=8.0, increment=0.5,
                    textvariable=self._fade_sec_var, width=5).pack(side="left")
        ttk.Label(frm, text="秒（结尾音量渐降到 0）", foreground="gray").pack(
            side="left", padx=6)

        # 说明
        ttk.Label(body, text="安装方式：写入本电台 mod 的 sound/zz_<电台>_sounds.asset（追加定义，"
                             "不顶掉原版音效），start_game_01（点开始游戏）与 start_game_02"
                             "（进入游戏世界）都替换为同一段音频。",
                  foreground="gray", wraplength=540, justify="left").pack(anchor="w", **pad)

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        self._disable_btn = ttk.Button(btns, text="禁用开局音效", command=self._disable)
        self._disable_btn.pack(side="left")
        ttk.Button(btns, text="安装", command=self._install).pack(side="right", padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right")

        self.transient(parent)
        self.grab_set()
        self.wait_visibility()
        center_window(self, parent)
        self._refresh_status()

    # -- 状态与禁用 ----------------------------------------------------------

    def _refresh_status(self):
        installed, desc = self._rm.start_game_status()
        self._sg_status_var.set(f"当前开局音效：{desc}")
        self._disable_btn.configure(state="normal" if installed else "disabled")

    def _disable(self):
        installed, desc = self._rm.start_game_status()
        if not installed:
            return
        if not messagebox.askyesno(
                "禁用开局音效",
                f"当前音效：{desc}\n\n"
                "将删除本电台 mod 的 sound/zz_<电台>_sounds.asset 与 sound/menu/start_game_01.wav，\n"
                "恢复游戏默认开局音效（不动游戏本体）。是否继续？",
                parent=self):
            return
        removed = self._rm.remove_start_game()
        self._refresh_status()
        self._status_var.set("开局音效已禁用")
        messagebox.showinfo("已禁用",
                            "已删除：\n" + "\n".join(f"· {r}" for r in removed)
                            + "\n\n游戏将使用默认开局音效。", parent=self)

    # -- 文件选择与转换探测 --------------------------------------------------

    def _pick_file(self):
        path = filedialog.askopenfilename(
            parent=self, title="选择音频文件",
            filetypes=[("音频文件", "*.mp3 *.flac *.wav *.m4a *.aac *.ogg *.opus *.wma"),
                       ("所有文件", "*.*")])
        if not path:
            return
        self._stop_preview()
        self._src = path
        self._file_var.set(os.path.basename(path))
        self._status_var.set("正在转换并读取时长，请稍候…")
        self._busy = True
        threading.Thread(target=self._probe_worker, args=(path,), daemon=True).start()
        self.after(100, self._poll)

    def _probe_worker(self, path):
        try:
            music_lib.convert_to_wav(path, self._probe)
            dur = music_lib.wav_duration(self._probe)
            self._q.put(("ok", dur))
        except Exception as e:  # noqa: BLE001
            self._q.put(("err", str(e)))

    def _poll(self):
        if not self._busy:
            return
        try:
            kind, payload = self._q.get_nowait()
        except queue.Empty:
            self.after(100, self._poll)
            return
        self._busy = False
        if kind == "ok":
            self._total = payload
            if self._total < 0.5:
                self._status_var.set(f"音频太短：{self._total:.1f} 秒，请选择更长的音频")
                return
            self._status_var.set(f"总时长：{self._total:.1f} 秒，可裁剪任意区间（≤ {self.MAX_SECONDS:g} 秒）")
            self._start_var.set(0.0)
            self._end_var.set(min(self.MAX_SECONDS, self._total))
            self._preview_btn.configure(state="normal")
            self._draw_range()
            self._update_len()
        else:
            self._status_var.set("转换失败")
            messagebox.showerror("转换失败",
                                 f"无法读取该音频文件：\n{payload}", parent=self)

    # -- 区间条（单条双端：左手柄=开头、右手柄=结尾） -----------------------

    def _t_to_x(self, t: float) -> float:
        span = max(self._total, 0.001)
        return self.BAR_X0 + t / span * (self.BAR_X1 - self.BAR_X0)

    def _x_to_t(self, x: float) -> float:
        span = max(self._total, 0.001)
        t = (x - self.BAR_X0) / (self.BAR_X1 - self.BAR_X0) * self._total
        return max(0.0, min(self._total, t))

    def _draw_range(self):
        c = self._range_canvas
        c.delete("all")
        if self._total <= 0:
            return
        start, end = self._start_var.get(), self._end_var.get()
        sx, ex = self._t_to_x(start), self._t_to_x(end)
        over = (end - start) > self.MAX_SECONDS + 0.05
        # 轨道
        c.create_rectangle(self.BAR_X0, self.BAR_Y - 4, self.BAR_X1, self.BAR_Y + 4,
                           fill="#3a3a3a", outline="#555555")
        # 选中区间（正常绿色，超限红色）
        c.create_rectangle(sx, self.BAR_Y - 4, ex, self.BAR_Y + 4,
                           fill="#c0392b" if over else "#2e8b57", outline="")
        # 两端手柄（开始=红，结束=绿）
        for hx, color in ((sx, "#ff5555"), (ex, "#55ff55")):
            c.create_rectangle(hx - 5, self.BAR_Y - 11, hx + 5, self.BAR_Y + 11,
                               fill=color, outline="")
        # 时间刻度
        for t in (0.0, self._total / 2, self._total):
            x = self._t_to_x(t)
            c.create_text(x, self.BAR_Y + 16, text=f"{t:.1f}", fill="#888888",
                          font=("Consolas", 8))
        c.create_text(sx, self.BAR_Y - 14, text=f"{start:.1f}s", fill="#ff8888",
                      font=("Consolas", 8))
        c.create_text(ex, self.BAR_Y - 14, text=f"{end:.1f}s", fill="#88ff88",
                      font=("Consolas", 8))

    def _on_bar_press(self, e):
        if self._total <= 0:
            return
        sx = self._t_to_x(self._start_var.get())
        ex = self._t_to_x(self._end_var.get())
        if abs(e.x - sx) <= 12:          # 加宽命中区，方便抓取手柄
            self._drag_handle = "start"
        elif abs(e.x - ex) <= 12:
            self._drag_handle = "end"
        else:
            self._drag_handle = None

    def _on_bar_drag(self, e):
        if self._drag_handle is None or self._total <= 0:
            return
        t = self._x_to_t(e.x)
        start, end = self._start_var.get(), self._end_var.get()
        if self._drag_handle == "start":
            # 只保留最小间隔 0.5s，不再受 12s 上限限制
            start = max(0.0, min(t, end - 0.5))
            self._start_var.set(round(start, 1))
        elif self._drag_handle == "end":
            # 允许拖到音频结尾（可超过 12s），超出后红色警示、安装时拦截
            end = max(start + 0.5, min(t, self._total))
            self._end_var.set(round(end, 1))
        self._draw_range()
        self._update_len()
        self._stop_preview()

    def _on_bar_release(self, _e):
        self._drag_handle = None

    def _update_len(self):
        length = max(0.0, self._end_var.get() - self._start_var.get())
        over = length > self.MAX_SECONDS + 0.05
        color = "#c0392b" if over else "#1a7f37"
        text = f"裁剪时长：{length:.1f} 秒（上限 {self.MAX_SECONDS:g} 秒）"
        if over:
            text += "  —— 超出上限，将无法安装！"
        self._len_var.set(text)
        for w in (self._len_label, self._range_info_label):
            if w is not None:
                w.configure(foreground=color)
        self._range_info.set(
            f"开始 {self._start_var.get():.1f}s / 结束 {self._end_var.get():.1f}s")

    # -- 试听 --------------------------------------------------------------

    def _make_final(self, start: float, end: float, dst: str) -> None:
        """从探测 WAV 裁剪出最终片段（勾选淡出时对末尾做渐降），写入 dst。"""
        tmp = dst + ".tmp"
        music_lib.crop_wav(self._probe, tmp, start, end)
        if self._fade_var.get():
            music_lib.apply_fade_out(tmp, dst, float(self._fade_sec_var.get() or 0))
        else:
            shutil.copy2(tmp, dst)
        try:
            os.remove(tmp)
        except OSError:
            pass

    def _stop_preview(self):
        if not self._playing:
            return
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except OSError:
            pass
        self._playing = False
        self._preview_btn.config(text="试听")

    def _toggle_preview(self):
        if self._playing:
            self._stop_preview()
            return
        if not self._src or self._total <= 0:
            return
        start, end = self._start_var.get(), self._end_var.get()
        if end <= start:
            return
        try:
            # 快速无延迟：从探测时已转换好的 44.1k WAV 直接切片（纯 Python，毫秒级），
            # 不再每次调 ffmpeg；勾选淡出时一并应用
            self._make_final(start, end, self._preview_path)
            import winsound
            winsound.PlaySound(self._preview_path,
                               winsound.SND_FILENAME | winsound.SND_ASYNC)
            self._playing = True
            self._preview_btn.config(text="停止试听")
            self._status_var.set(f"试听中（{end - start:.1f} 秒）…")
            # 播放结束后恢复按钮
            ms = int((end - start) * 1000) + 600
            self.after(ms, self._on_preview_finished)
        except Exception as e:  # noqa: BLE001
            self._status_var.set("试听失败")
            messagebox.showerror("试听失败", str(e), parent=self)

    def _on_preview_finished(self):
        self._playing = False
        self._preview_btn.config(text="试听")

    # -- 安装 --------------------------------------------------------------

    def _install(self):
        self._stop_preview()
        if not self._src:
            messagebox.showerror("错误", "请先选择音频文件", parent=self)
            return
        start = self._start_var.get()
        end = self._end_var.get()
        if end <= start:
            messagebox.showerror("错误", "结束时间必须大于开始时间", parent=self)
            return
        if end - start > self.MAX_SECONDS + 0.05:
            messagebox.showerror(
                "超出时长限制",
                f"裁剪时长 {end - start:.1f} 秒超过上限 {self.MAX_SECONDS:g} 秒，无法安装。\n"
                "请向左拖动绿色手柄（结尾）缩小区间。", parent=self)
            return
        try:
            dst = os.path.join(os.path.dirname(self._probe), "start_game_out.wav")
            # 从探测 WAV 直接切片（快速且规格正确：44.1k/16bit/立体声），可选淡出
            self._make_final(start, end, dst)
            self._rm.install_start_game(dst, round(self._vol_var.get(), 2))
        except OSError as e:
            messagebox.showerror("安装失败", str(e), parent=self)
            return
        self._result = True
        self.destroy()

    def result(self):
        return self._result


class App(TkinterDnD.Tk):
    """主窗口。"""

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("900x520")
        self.minsize(720, 400)

        self.config = load_config()
        self.mf: MusicFolder | None = None
        self.folder = ""
        self.dirty = False
        self.radio_mod: music_lib.RadioMod | None = None
        self._mode: str | None = None     # None=一级页 | "classic" | "radio"
        self.default_volume = float(self.config.get("default_volume", DEFAULT_VOLUME))
        self._sort_key = self.config.get("sort_key", "name")
        self._sort_rev = bool(self.config.get("sort_rev", False))
        # 转换暂存：目标文件名 -> cache 中的 ogg 路径（保存时才复制进目标文件夹）
        self._pending_files: dict = {}
        self._cache_dir = os.path.join(tempfile.gettempdir(), CACHE_DIR_NAME)
        shutil.rmtree(self._cache_dir, ignore_errors=True)  # 清理上次异常退出残留
        os.makedirs(self._cache_dir, exist_ok=True)

        self._build_mode_select()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 窗口图标（logo.ico）
        icon = resource_path(ICON_FILE)
        if os.path.exists(icon):
            try:
                self.iconbitmap(icon)
            except tk.TclError:
                pass
        center_window(self)  # 主窗口屏幕居中
        # 拖放支持：把音乐文件拖入窗口即可添加
        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<Drop>>", self._on_drop)
        self._update_headers()
        self._show_mode_select()  # 启动进入一级页：模式选择

    # -- UI 构建 ------------------------------------------------------------

    def _build_ui(self):
        # 二级页容器（编辑器）：一级页模式选择后进入
        self._editor_frame = ttk.Frame(self)
        # 工具栏
        bar = ttk.Frame(self._editor_frame, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="← 切换模式", command=self._on_mode_switch).pack(side="left")
        self._btn_choose_folder = ttk.Button(bar, text="选择 music 文件夹",
                                             command=self._choose_folder)
        self._btn_choose_folder.pack(side="left", padx=(6, 0))
        self._btn_new_radio = ttk.Button(bar, text="新建电台 MOD", command=self._new_radio_mod)
        self._btn_open_radio = ttk.Button(bar, text="打开电台 MOD", command=self._open_radio_mod)
        self._btn_change_icon = ttk.Button(bar, text="更换图标", command=self._change_radio_icon)
        self._btn_start_game = ttk.Button(bar, text="开局音效", command=self._start_game_sound)
        ttk.Button(bar, text="保存", command=self._save).pack(side="left", padx=6)
        ttk.Button(bar, text="修复格式", command=self._repair_format).pack(side="left", padx=6)

        # 路径行（独立一行：长文本会撑爆工具栏导致同排按钮被 Tk pack unmap/压缩）
        prow = ttk.Frame(self._editor_frame, padding=(8, 0, 8, 4))
        prow.pack(fill="x")
        # 当前编辑模式指示（放在路径行，不占用工具栏按钮空间）
        self._mode_label = ttk.Label(prow, text="", font=("Microsoft YaHei UI", 9, "bold"),
                                     foreground="#1a7f37")
        self._mode_label.pack(side="left")
        self._folder_var = tk.StringVar(value="未选择文件夹")
        ttk.Label(prow, textvariable=self._folder_var, foreground="gray").pack(
            side="left", padx=(12, 0))

        # 搜索
        srow = ttk.Frame(self._editor_frame, padding=(8, 0))
        srow.pack(fill="x")
        ttk.Label(srow, text="搜索：").pack(side="left")
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *a: self._refresh_tree())
        ttk.Entry(srow, textvariable=self._search_var, width=40).pack(side="left")

        # 歌曲列表
        cols = ("name", "file", "volume", "logic")
        self.tree = ttk.Treeview(self._editor_frame, columns=cols, show="headings",
                                 selectmode="extended")
        self._header_base = {
            "name": "song name",
            "file": "文件",
            "volume": "音量",
            "logic": "播放逻辑",
        }
        widths = {"name": 200, "file": 260, "volume": 70, "logic": 240}
        for cid in cols:
            self.tree.heading(cid, text=self._header_base[cid],
                              command=lambda c=cid: self._sort_by(c))
            self.tree.column(cid, width=widths[cid], anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8, pady=6)

        vsb = ttk.Scrollbar(self._editor_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.place(relx=1.0, rely=0.5, anchor="e", relheight=0.6)

        # 操作按钮
        btns = ttk.Frame(self._editor_frame, padding=(8, 0, 8, 8))
        btns.pack(fill="x")
        ttk.Button(btns, text="添加歌曲", command=self._add_songs).pack(side="left")
        self._edit_btn = ttk.Button(btns, text="编辑选中", command=self._edit_selected)
        self._edit_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="删除选中", command=self._delete_selected).pack(side="left")
        ttk.Button(btns, text="打开所在文件夹", command=self._open_in_explorer).pack(side="left", padx=6)
        self._status_var = tk.StringVar(value="")
        ttk.Label(btns, textvariable=self._status_var, foreground="gray").pack(side="right")

        self.tree.bind("<Double-1>", lambda e: self._edit_selected())
        self.tree.bind("<Delete>", lambda e: self._delete_selected())
        self.tree.bind("<<TreeviewSelect>>", self._on_select_change)
        # 右键菜单
        self._context_menu = tk.Menu(self, tearoff=0)
        self._context_menu.add_command(label="在资源管理器中显示", command=self._open_in_explorer)
        self._context_menu.add_command(label="编辑", command=self._edit_selected)
        self._context_menu.add_command(label="删除", command=self._delete_selected)
        self.tree.bind("<Button-3>", self._show_context_menu)

    # -- 一级页：模式选择 ----------------------------------------------------

    def _build_mode_select(self):
        self._mode_frame = ttk.Frame(self)
        ttk.Label(self._mode_frame, text="HoI4 音乐播放列表编辑器",
                  font=("Microsoft YaHei UI", 20, "bold")).pack(pady=(60, 2))
        ttk.Label(self._mode_frame, text=f"v{VERSION} · {AUTHOR_NAME} · {QQ_GROUP}",
                  font=("Microsoft YaHei UI", 9), foreground="gray").pack()
        ttk.Label(self._mode_frame, text="请选择编辑模式",
                  font=("Microsoft YaHei UI", 12)).pack(pady=(36, 18))

        cards = ttk.Frame(self._mode_frame)
        cards.pack()
        self._make_mode_card(cards, "本地电台编辑",
                             "新建或编辑独立的本地电台 MOD\n产物为完整 MOD，不修改游戏本体与已有 mod",
                             self._enter_radio_mode)
        self._make_mode_card(cards, "已有电台编辑",
                             "直接编辑游戏 mod 或游戏本体的 music 文件夹\n（music.asset / _songs.txt）",
                             self._enter_classic_mode)
        ttk.Label(self._mode_frame, text="提示：编辑中途可随时点「← 切换模式」返回本页",
                  foreground="gray").pack(pady=(26, 0))

        # 左下角设置入口（齿轮）：集成全局默认音量 与 关于
        bottom = ttk.Frame(self._mode_frame)
        bottom.pack(side="bottom", fill="x", padx=12, pady=10)
        self._settings_btn = ttk.Button(bottom, text="⚙ 设置")
        self._settings_btn.pack(side="left")
        self._settings_btn.bind("<Button-1>", self._open_settings)
        self._settings_menu = tk.Menu(self, tearoff=0)
        self._settings_menu.add_command(label="全局默认音量…",
                                        command=self._set_default_volume)
        self._settings_menu.add_command(label="关于…", command=self._show_about)
        self._settings_menu.add_separator()
        self._settings_menu.add_command(label="删除用户数据…",
                                        command=self._reset_user_data)

    def _open_settings(self, event):
        """一级 UI 左下角「⚙ 设置」弹菜单：全局默认音量 / 关于 / 删除用户数据。"""
        try:
            self._settings_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._settings_menu.grab_release()

    def _reset_user_data(self):
        """删除软件自身用户数据（config.json）：上次文件夹/默认音量/排序等。
        不影响任何 mod、游戏文件或电台 MOD。"""
        if not messagebox.askyesno(
                "删除用户数据",
                "将删除软件自身的用户数据（config.json）：\n"
                "· 上次使用的文件夹\n"
                "· 全局默认音量\n"
                "· 列表排序状态\n\n"
                "不会删除任何 mod / 游戏文件，也不会删除电台 MOD。\n是否继续？",
                parent=self):
            return
        try:
            os.remove(CONFIG_PATH)
            log("删除用户数据: config.json 已删除")
        except OSError:
            log("删除用户数据: config.json 不存在或删除失败")
        # 重置当前会话
        self.config = {}
        self.default_volume = DEFAULT_VOLUME
        self._sort_key = "name"
        self._sort_rev = False
        self.mf = None
        self.radio_mod = None
        self.folder = ""
        self.dirty = False
        if hasattr(self, "_folder_var"):
            self._folder_var.set("未选择文件夹")
        if hasattr(self, "tree"):
            self.tree.delete(*self.tree.get_children())
        messagebox.showinfo(
            "已完成",
            "用户数据已删除，软件已恢复默认设置。\n"
            "下次启动不再自动加载上次文件夹，默认音量恢复为 0.65。", parent=self)

    @staticmethod
    def _make_mode_card(parent, title, subtitle, command):
        card = tk.Frame(parent, width=300, height=170, bg="#333333",
                        highlightthickness=1, highlightbackground="#555555",
                        cursor="hand2")
        card.pack_propagate(False)
        card.pack(side="left", padx=18)
        ttk.Label(card, text=title, font=("Microsoft YaHei UI", 15, "bold"),
                  background="#333333", foreground="#ffffff").pack(pady=(38, 8))
        ttk.Label(card, text=subtitle, font=("Microsoft YaHei UI", 9),
                  background="#333333", foreground="#bbbbbb",
                  wraplength=270, justify="center").pack()

        def _bind(w):
            w.bind("<Button-1>", lambda e: command())
            w.bind("<Enter>", lambda e: card.configure(bg="#3d3d3d"))
            w.bind("<Leave>", lambda e: card.configure(bg="#333333"))
            for ch in w.winfo_children():
                _bind(ch)

        _bind(card)
        return card

    # -- 模式切换 -------------------------------------------------------------

    def _show_mode_select(self):
        self._mode = None
        self.title(APP_TITLE)
        if hasattr(self, "_mode_label"):
            self._mode_label.config(text="")
        # 彻底清空编辑状态：切换模式后不再残留上一个模式的 music 数据
        self.mf = None
        self.radio_mod = None
        self.folder = ""
        if hasattr(self, "_folder_var"):
            self._folder_var.set("未选择文件夹")
        if hasattr(self, "tree"):
            self.tree.delete(*self.tree.get_children())
        self._editor_frame.pack_forget()
        self._mode_frame.pack(fill="both", expand=True)

    def _enter_classic_mode(self):
        self._mode = "classic"
        self.title(f"{APP_TITLE} — 编辑模式：已有电台编辑")
        self._mode_label.config(text="编辑模式：已有电台编辑")
        self._mode_frame.pack_forget()
        self._editor_frame.pack(fill="both", expand=True)
        # 工具栏：显示「选择 music 文件夹」，隐藏电台按钮
        self._btn_choose_folder.pack(side="left", padx=(6, 0))
        self._btn_new_radio.pack_forget()
        self._btn_open_radio.pack_forget()
        self._btn_change_icon.pack_forget()
        self._btn_start_game.pack_forget()
        # 首次进入时自动加载上次使用的文件夹
        if self.mf is None:
            last = self.config.get("last_folder", "")
            if last and os.path.isdir(last):
                self.after(100, lambda: self._load_folder(last, prompt_empty=False))

    def _enter_radio_mode(self):
        self._mode = "radio"
        self.title(f"{APP_TITLE} — 编辑模式：本地电台编辑")
        self._mode_label.config(text="编辑模式：本地电台编辑")
        self._mode_frame.pack_forget()
        self._editor_frame.pack(fill="both", expand=True)
        # 工具栏：隐藏「选择 music 文件夹」，显示电台按钮
        self._btn_choose_folder.pack_forget()
        self._btn_new_radio.pack(side="left", padx=(6, 0))
        self._btn_open_radio.pack(side="left", padx=6)
        self._btn_change_icon.pack(side="left", padx=6)
        self._btn_start_game.pack(side="left", padx=6)
        if self.radio_mod is None:
            self._status_var.set("电台模式：请先「新建电台 MOD」或「打开电台 MOD」")
            # 记忆分开：自动恢复上次打开的电台（仅电台模式自己的记忆）
            mod_dir = self.config.get("last_radio_mod_dir", "")
            radio_id = self.config.get("last_radio_id", "")
            if mod_dir and radio_id and os.path.isdir(
                    os.path.join(mod_dir, radio_id)):
                self.after(100, lambda: self._open_radio_by(mod_dir, radio_id))

    def _open_radio_by(self, mod_dir: str, radio_id: str):
        try:
            rm = music_lib.RadioMod.open(mod_dir, radio_id)
        except ValueError:
            return
        self._load_radio_mod(rm)

    def _on_mode_switch(self):
        if self.dirty and not messagebox.askyesno(
                "未保存", "有未保存的修改，切换模式将丢弃。是否继续？", parent=self):
            return
        self._show_mode_select()

    # -- 电台 MOD -------------------------------------------------------------

    def _new_radio_mod(self):
        dlg = RadioModDialog(self)
        self.wait_window(dlg)
        r = dlg.result()
        if not r:
            return
        try:
            rm = music_lib.RadioMod(r["radio_id"], r["display"], r["mod_dir"], r["version"])
        except ValueError as e:
            messagebox.showerror("错误", str(e), parent=self)
            return
        if os.path.exists(rm.mod_folder) or os.path.exists(rm.reg_file):
            if not messagebox.askyesno(
                    "电台已存在",
                    f"电台「{r['radio_id']}」已存在。\n"
                    "重新创建将重建模板文件（已添加的歌曲与配置保留）。是否继续？",
                    parent=self):
                return
        try:
            rm.create()
        except OSError as e:
            messagebox.showerror("错误", f"创建电台失败：\n{e}", parent=self)
            return
        music_lib.index_add_radio(rm.radio_id, rm.display_name, rm.mod_dir)
        self._load_radio_mod(rm)

    def _open_radio_mod(self):
        dlg = RadioModOpenDialog(self)
        self.wait_window(dlg)
        r = dlg.result()
        if not r:
            return
        try:
            rm = music_lib.RadioMod.open(r["mod_dir"], r["radio_id"])
        except ValueError as e:
            messagebox.showerror("错误", str(e), parent=self)
            return
        self._load_radio_mod(rm)

    def _load_radio_mod(self, rm):
        mf = rm.music_folder()
        mf.load()
        if not mf.is_loaded():
            messagebox.showinfo(
                "未找到音乐文件",
                f"电台「{rm.radio_id}」的 music 目录中没有找到音乐文件：\n"
                f"{mf.asset_path}\n{mf.songs_path}\n\n"
                "可能原因：该电台不是本程序创建的，或音乐文件位于其他位置。\n"
                "仍将打开空列表，添加歌曲并保存后会自动创建音乐文件。",
                parent=self)
        self.mf = mf
        self.folder = rm.music_dir
        self.radio_mod = rm
        self.dirty = False
        # 只显示简短电台信息；完整路径会撑爆工具栏导致按钮被 unmap
        self._folder_var.set(f"电台 [{rm.radio_id}] {_shorten(rm.display_name, 20)}")
        # 电台模式记忆分开存储（不再写入 last_folder，避免与已有模式互相污染）
        self.config["last_radio_mod_dir"] = rm.mod_dir
        self.config["last_radio_id"] = rm.radio_id
        save_config(self.config)
        # 登记到索引（确保任意目录创建的电台后续都能被找到）
        music_lib.index_add_radio(rm.radio_id, rm.display_name, rm.mod_dir)
        self._enter_radio_mode()
        self._refresh_tree()
        # 自动修复 Paradox 启动器重写造成的 BOM 丢失（不弹问题框，状态栏提示）
        auto_fixed = []
        for path, writer in ((rm.reg_file, rm.write_reg_file),
                             (rm.descriptor_path, rm.write_descriptor)):
            if os.path.exists(path):
                with open(path, "rb") as f:
                    if not f.read(3).startswith(b"\xef\xbb\xbf"):
                        try:
                            writer()
                            auto_fixed.append(os.path.basename(path))
                        except OSError:
                            pass
        if auto_fixed:
            self._status_var.set(f"已自动补回 {len(auto_fixed)} 个文件的 UTF-8 BOM（启动器重写导致）")
        # 排查电台模板问题（gui 引用非原版资源 / supported_version / 图标 / BOM）
        issues = rm.check_issues()
        if issues:
            self._status_var.set(f"检测到 {len(issues)} 个电台问题，点「修复格式」一键修复")
            messagebox.showwarning(
                "检测到电台问题",
                "该电台存在以下问题，可能导致游戏内显示异常：\n\n"
                + "\n".join(f"· {i}" for i in issues)
                + "\n\n点「修复格式」可一键修复（自动备份原文件）。",
                parent=self)

    def _change_radio_icon(self):
        if not self.radio_mod:
            messagebox.showinfo("提示", "请先新建或打开一个电台", parent=self)
            return
        dlg = ImageCropDialog(self, self.radio_mod.album_art_path)
        self.wait_window(dlg)
        if dlg.result():
            self._status_var.set("电台图标已更新")

    def _start_game_sound(self):
        """开局音效：选择音频 → 裁剪区间 → 自动安装进电台 mod。"""
        if not self.radio_mod:
            messagebox.showinfo("提示", "请先新建或打开一个电台", parent=self)
            return
        dlg = StartGameDialog(self, self.radio_mod)
        self.wait_window(dlg)
        if dlg.result():
            self._status_var.set("开局音效已安装（sound/start_game_01）")

    def _show_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            try:
                self._context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._context_menu.grab_release()

    def _open_in_explorer(self):
        """在资源管理器中定位选中的音乐文件。"""
        if not self.mf:
            return
        entry = self._selected_entry()
        if entry is None:
            return
        if entry.file in self._pending_files:
            # 转换文件还在缓存中（未保存）：定位缓存文件
            path = self._pending_files[entry.file]
            if os.path.exists(path):
                _explorer_select(path)
                return
            path = ""
        else:
            path = os.path.join(self.folder, entry.file)
        if path and os.path.exists(path):
            if not _explorer_select(path):
                # 兜底：explorer /select（字符串形式）
                subprocess.Popen(f'explorer /select,"{path}"')
        else:
            messagebox.showinfo("文件不存在", f"文件不存在：{entry.file}\n将打开所在文件夹。")
            subprocess.Popen(["explorer", self.folder])

    # -- 文件夹 --------------------------------------------------------------

    def _choose_folder(self):
        if self.dirty:
            if not messagebox.askyesno("未保存", "有未保存的修改，继续选择文件夹将丢弃。是否继续？"):
                return
        folder = filedialog.askdirectory(title="选择 music 文件夹")
        if not folder:
            return
        self._load_folder(folder)

    def _choose_music_group(self, folder: str):
        """
        确定要编辑的音乐文件组，返回 (asset_filename, songs_filename)。
        优先使用上次选择；多组且无记忆时弹选择框；取消返回 None。
        """
        groups = music_lib.find_music_groups(folder)
        if not groups:
            return music_lib.ASSET_FILENAME, music_lib.SONGS_FILENAME
        prev_a = self.config.get("last_asset")
        prev_s = self.config.get("last_songs")
        for _label, a, s in groups:
            if (a or music_lib.ASSET_FILENAME) == prev_a and (s or music_lib.SONGS_FILENAME) == prev_s:
                return prev_a, prev_s
        if len(groups) == 1:
            _label, a, s = groups[0]
            return a or music_lib.ASSET_FILENAME, s or music_lib.SONGS_FILENAME
        # 多组：弹选择框
        result = []
        dlg = tk.Toplevel(self)
        dlg.title("选择音乐文件组")
        dlg.resizable(False, False)
        dlg.geometry("480x320")
        dlg.transient(self)
        dlg.grab_set()
        dlg.wait_visibility()
        center_window(dlg, self)
        ttk.Label(dlg, padding=12, text="该文件夹下有多个音乐文件组，选择要编辑的一组：").pack(anchor="w")
        lb = tk.Listbox(dlg, height=8, font=("Consolas", 10))
        lb.pack(fill="both", expand=True, padx=12)
        for label, a, s in groups:
            lb.insert("end", f"  {label}   [{a or '无'} / {s or '无'}]")
        lb.selection_set(0)

        def ok():
            sel = lb.curselection()
            if sel:
                _label, a, s = groups[sel[0]]
                result.append((a or music_lib.ASSET_FILENAME, s or music_lib.SONGS_FILENAME))
            dlg.destroy()

        btns = ttk.Frame(dlg, padding=(12, 8))
        btns.pack(fill="x")
        ttk.Button(btns, text="确定", command=ok).pack(side="right", padx=4)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right")
        self.wait_window(dlg)
        return result[0] if result else None

    def _load_folder(self, folder: str, prompt_empty: bool = True):
        chosen = self._choose_music_group(folder)
        if chosen is None:  # 用户取消选择
            return
        asset_name, songs_name = chosen
        mf = MusicFolder(folder, asset_name, songs_name)
        mf.load()
        has_any = os.path.exists(mf.asset_path) or os.path.exists(mf.songs_path)
        if not has_any:
            if prompt_empty and not messagebox.askyesno(
                    "空文件夹",
                    f"文件夹中没有 {asset_name} / {songs_name}。\n"
                    "将以空列表开始，保存时自动创建这两个文件。是否继续？"):
                return
        self.mf = mf
        self.folder = folder
        self.dirty = False
        self._folder_var.set(_shorten(folder))
        self.config["last_folder"] = folder
        self.config["last_asset"] = asset_name
        self.config["last_songs"] = songs_name
        save_config(self.config)
        self._refresh_tree()

    # -- 树视图 --------------------------------------------------------------

    def _tree_data(self):
        """返回 [(name, file, volume, logic, factor, conditions)]，支持搜索。"""
        rows = []
        if not self.mf:
            return rows
        kw = self._search_var.get().strip().lower()
        rules_by_song = {}
        for r in self.mf.rules:
            rules_by_song.setdefault(r.song, []).append(r)
        for e in self.mf.entries:
            if kw and kw not in e.name.lower() and kw not in e.file.lower():
                continue
            rules = rules_by_song.get(e.name, [])
            conds = rules[0].conditions if rules else []
            factor = rules[0].factor if rules else None
            rows.append((e.name, e.file, e.volume, summarize_conditions(conds),
                         factor, conds))
        return rows

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        rows = self._tree_data()
        idx = {"name": 0, "file": 1, "volume": 2, "logic": 3}[self._sort_key]
        rows.sort(key=lambda r: (r[idx] is None, str(r[idx]).lower()), reverse=self._sort_rev)
        for r in rows:
            self.tree.insert("", "end", values=(r[0], r[1], f"{r[2]:g}", r[3]))
        n = len(rows)
        self._status_var.set(f"{n} 首歌曲" + ("（未保存）" if self.dirty else ""))

    def _sort_by(self, key: str):
        if self._sort_key == key:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_key = key
            self._sort_rev = False
        self.config["sort_key"] = self._sort_key
        self.config["sort_rev"] = self._sort_rev
        save_config(self.config)
        self._update_headers()
        self._refresh_tree()

    def _update_headers(self):
        """表头显示排序方向三角：▲ 升序，▼ 降序。"""
        for cid, base in self._header_base.items():
            if cid == self._sort_key:
                mark = " ▼" if self._sort_rev else " ▲"
            else:
                mark = ""
            self.tree.heading(cid, text=base + mark)

    def _on_select_change(self, _=None):
        """多选时禁用编辑（只能删除/批量删除），单选时可编辑。"""
        if getattr(self, "_edit_btn", None):
            n = len(self.tree.selection())
            self._edit_btn.state(["!disabled"] if n == 1 else ["disabled"])

    def _selected_entry(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一行")
            return None
        name = self.tree.item(sel[0])["values"][0]
        return self.mf.entry_by_name(name)

    # -- 添加 ---------------------------------------------------------------

    @staticmethod
    def _parse_dnd_paths(data: str) -> list:
        """解析拖放事件数据：{含空格路径} 与裸路径混合，返回存在的文件路径。"""
        paths = []
        for m in re.finditer(r"\{([^}]*)\}|(\S+)", data or ""):
            p = (m.group(1) or m.group(2) or "").strip()
            if p and os.path.exists(p):
                paths.append(p)
        return paths

    def _on_drop(self, event):
        """把音乐文件拖入窗口 → 直接添加（ogg 复制、其余转换）。"""
        log(f"拖放事件: {event.data!r}")
        if not self.mf:
            messagebox.showinfo("提示", "请先选择 music 文件夹")
            return
        files = [f for f in self._parse_dnd_paths(event.data)
                 if f.lower().endswith((".ogg", ".mp3", ".flac", ".wav", ".m4a", ".aac"))]
        log(f"拖放解析出 {len(files)} 个音频文件: {files}")
        if not files:
            messagebox.showinfo("提示", "未识别到音频文件（支持 ogg/mp3/flac/wav/m4a/aac）")
            return
        oggs = [f for f in files if f.lower().endswith(".ogg")]
        convs = [f for f in files if not f.lower().endswith(".ogg")]
        if oggs:
            self._add_ogg_files(oggs)
        if convs:
            self._convert_and_add(convs)

    def _add_songs(self):
        if not self.mf:
            messagebox.showinfo("提示", "请先选择 music 文件夹")
            return
        files = filedialog.askopenfilenames(
            title="选择音频文件（ogg/mp3/flac 等，可多选）",
            filetypes=[("音频文件", "*.ogg *.mp3 *.flac *.wav *.m4a *.aac"),
                       ("Ogg 音频", "*.ogg"),
                       ("MP3 音频", "*.mp3"),
                       ("FLAC 音频", "*.flac"),
                       ("所有文件", "*.*")])
        if not files:
            return
        oggs = [f for f in files if f.lower().endswith(".ogg")]
        convs = [f for f in files if not f.lower().endswith(".ogg")]
        log(f"添加歌曲: 共 {len(files)} 个, ogg {len(oggs)} 个, 需转换 {len(convs)} 个")
        if oggs:
            self._add_ogg_files(oggs)
        if convs:
            self._convert_and_add(convs)

    # -- 批量添加 -----------------------------------------------------------

    def _batch_add(self):
        """打开批量添加对话框：统一音量/播放逻辑，一次添加多个文件。"""
        if not self.mf:
            messagebox.showinfo("提示", "请先选择 music 文件夹")
            return
        dlg = BatchAddDialog(self, volume=self.default_volume)
        self.wait_window(dlg)
        res = dlg.result()
        if not res:
            log("批量添加: 用户取消")
            return
        log(f"批量添加: 对话框返回 {len(res['files'])} 个文件: {res['files']}")
        settings = {"volume": res["volume"], "factor": res["factor"],
                    "conditions": res["conditions"]}
        files = res["files"]
        oggs = [f for f in files if f.lower().endswith(".ogg")]
        convs = [f for f in files if not f.lower().endswith(".ogg")]
        log(f"批量添加: ogg {len(oggs)} 个, 需转换 {len(convs)} 个")
        if oggs:
            copied = []
            for src in oggs:
                base = os.path.basename(src)
                target = self.mf.unique_filename(base)
                try:
                    shutil.copy2(src, os.path.join(self.folder, target))
                except OSError as e:
                    messagebox.showerror("复制失败", f"{base}\n{e}")
                    continue
                copied.append(target)
            if copied:
                self._register_targets(copied, settings)
        if convs:
            self._convert_and_add(convs, batch_settings=settings)

    def _register_targets(self, targets, settings):
        """批量静默注册条目：song name 自动按文件名生成（唯一），统一音量/播放逻辑。"""
        added = 0
        for target in targets:
            base = os.path.splitext(target)[0]
            name = self.mf.unique_song_name(base)
            if self.mf.entry_by_name(name):
                continue  # unique_song_name 保证唯一，防御
            self.mf.add_entry(name, target, settings["volume"],
                              settings["factor"], settings["conditions"])
            added += 1
        log(f"批量注册: 目标 {len(targets)} 个, 实际注册 {added} 个")
        if added:
            self.dirty = True
            self._refresh_tree()
        return added

    def _add_ogg_files(self, files):
        """ogg 直接复制进目标文件夹（保存时只写配置文件）。"""
        copied = []
        for src in files:
            base = os.path.basename(src)
            target = self.mf.unique_filename(base)
            try:
                shutil.copy2(src, os.path.join(self.folder, target))
            except OSError as e:
                messagebox.showerror("复制失败", f"{base}\n{e}")
                continue
            copied.append(target)
        if copied:
            self._prompt_and_register(copied)

    def _prompt_and_register(self, targets):
        """对每个目标文件名弹出设置对话框并注册条目。"""
        for target in targets:
            base = os.path.splitext(target)[0]
            dlg = SongDialog(
                self, f"添加歌曲：{target}",
                name=self.mf.unique_song_name(base),
                volume=self.default_volume)
            self.wait_window(dlg)
            res = dlg.result()
            if res is None:
                continue
            if self.mf.entry_by_name(res["name"]):
                messagebox.showerror("错误", f"song name 已存在：{res['name']}")
                continue
            self.mf.add_entry(res["name"], target, res["volume"],
                              res["factor"], res["conditions"])
            self.dirty = True
        self._refresh_tree()

    def _convert_and_add(self, files, batch_settings=None):
        """后台线程把 mp3/flac 等转换为 ogg 暂存 cache，完成后继续添加流程。
        batch_settings 非 None 时（批量添加）转换完成自动注册，不弹单个对话框。"""
        log(f"开始转换 {len(files)} 个文件: {[os.path.basename(f) for f in files]}")
        self._conv_queue = queue.Queue()
        total = len(files)
        done = [0]
        self._status_var.set(f"正在转换 {total} 个文件...")

        def worker():
            for i, src in enumerate(files):
                stem = os.path.splitext(os.path.basename(src))[0]
                try:
                    os.makedirs(self._cache_dir, exist_ok=True)
                    dst = os.path.join(self._cache_dir, f"{i}_{stem}.ogg")
                    music_lib.convert_to_ogg(src, dst)
                    self._conv_queue.put(("ok", dst, stem))
                except Exception as e:
                    self._conv_queue.put(("fail", os.path.basename(src), str(e)))
            self._conv_queue.put(("done", None, None))

        # 闭包累积：poll 每次 after 回调都会新建局部 results，
        # 必须跨轮次累积，否则只保留最后一轮取到的条目（批量添加只成功 1 个的根因）
        results_holder = []

        def poll():
            try:
                while True:
                    kind, p1, p2 = self._conv_queue.get_nowait()
                    if kind == "done":
                        self._on_convert_done(results_holder, batch_settings)
                        return
                    results_holder.append((kind, p1, p2))
                    if kind == "ok":
                        done[0] += 1
                        self._status_var.set(f"正在转换 {done[0]}/{total}...")
            except queue.Empty:
                pass
            self.after(100, poll)

        threading.Thread(target=worker, daemon=True).start()
        poll()

    def _on_convert_done(self, results, batch_settings=None):
        """转换完成：注册 pending（暂存 cache，保存时才复制进目标文件夹）。"""
        pending_targets, fails = [], []
        for kind, payload, stem in results:
            if kind == "ok":
                target = self.mf.unique_filename(stem + ".ogg")
                # 避免与已 pending 的同名目标冲突（不同目录同名文件批量添加）
                base2, ext2 = os.path.splitext(target)
                i = 2
                while target in self._pending_files:
                    target = f"{base2}_{i}{ext2}"
                    i += 1
                self._pending_files[target] = payload
                pending_targets.append(target)
            else:
                fails.append((payload, stem))
        log(f"转换完成: ok {len(pending_targets)} 个, 失败 {len(fails)} 个"
            + (f"; 失败: {fails}" if fails else ""))
        self._status_var.set("")
        for name, err in fails:
            messagebox.showerror("转换失败", f"{name}\n{err}", parent=self)
        if not pending_targets:
            if fails:
                messagebox.showinfo("转换完成", "没有可添加的文件", parent=self)
            return
        if batch_settings:
            n = self._register_targets(pending_targets, batch_settings)
            messagebox.showinfo(
                "批量添加",
                f"{n} 个文件已转换并添加到列表（点「保存」后才会复制到音乐文件夹）。",
                parent=self)
            return
        messagebox.showinfo("转换完成",
                            f"{len(pending_targets)} 个文件已转换，保存在缓存中。\n"
                            "点「保存」后才会复制到音乐文件夹。", parent=self)
        self._prompt_and_register(pending_targets)

    # -- 编辑 ---------------------------------------------------------------

    def _edit_selected(self):
        if not self.mf:
            return
        if len(self.tree.selection()) != 1:
            messagebox.showinfo("提示", "编辑仅支持单选（多选时请使用删除）")
            return
        entry = self._selected_entry()
        if entry is None:
            return
        rules = self.mf.rules_for_song(entry.name)
        conds = rules[0].conditions if rules else []
        factor = rules[0].factor if rules else DEFAULT_FACTOR
        war, faction = music_lib.preset_from_conditions(conds)
        # 无法识别全部条件时切到高级模式
        known = set(music_lib._WAR_BY_COND) | set(music_lib._FACTION_BY_COND)
        advanced = any(c.strip() not in known for c in conds)
        dlg = SongDialog(
            self, f"编辑歌曲：{entry.name}",
            name=entry.name,
            volume=entry.volume if entry.volume is not None else self.default_volume,
            factor=factor if factor is not None else DEFAULT_FACTOR,
            war=war, faction=faction, conditions=conds, advanced=advanced)
        self.wait_window(dlg)
        res = dlg.result()
        if res is None:
            return
        try:
            self.mf.update_entry(
                entry.name,
                new_name=res["name"] if res["name"] != entry.name else None,
                new_volume=res["volume"],
                new_factor=res["factor"],
                new_conditions=res["conditions"])
        except AssertionError as e:
            messagebox.showerror("错误", str(e))
            return
        self.dirty = True
        self._refresh_tree()

    # -- 删除 ---------------------------------------------------------------

    def _delete_selected(self):
        """单选/多选删除：多选时批量移除条目，可选一并删除 ogg 文件。"""
        if not self.mf:
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一行")
            return
        names = [self.tree.item(i)["values"][0] for i in sel]
        entries = [self.mf.entry_by_name(n) for n in names]
        entries = [e for e in entries if e is not None]
        if not entries:
            return
        if len(entries) == 1:
            desc = f"“{entries[0].name}”（文件 {entries[0].file}）"
        else:
            desc = f"以下 {len(entries)} 首歌曲：\n" + \
                   "\n".join(f"  · {e.name}（{e.file}）" for e in entries[:10])
            if len(entries) > 10:
                desc += f"\n  …等共 {len(entries)} 首"
        del_file = messagebox.askyesno(
            "删除确认",
            f"从播放列表移除 {desc}？\n\n"
            "是否同时删除文件夹中的 ogg 文件？\n"
            "「是」= 连文件一起删，「否」= 仅移除列表条目",
            icon="warning")
        if del_file is None:
            return
        failed = []
        for entry in entries:
            self.mf.remove_entry(entry.name)
            if del_file:
                if entry.file in self._pending_files:
                    path = self._pending_files.pop(entry.file, None)
                else:
                    path = os.path.join(self.folder, entry.file)
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError as e:
                        failed.append(f"{entry.file}: {e}")
        for msg in failed:
            messagebox.showerror("删除文件失败", msg)
        self.dirty = True
        self._refresh_tree()
        self._on_select_change()

    # -- 关于 ---------------------------------------------------------------

    def _show_about(self):
        dlg = tk.Toplevel(self)
        dlg.title("关于")
        dlg.resizable(False, False)
        dlg.geometry("420x300")
        dlg.transient(self)
        dlg.grab_set()
        dlg.wait_visibility()
        center_window(dlg, self)
        icon = resource_path(ICON_FILE)
        if os.path.exists(icon):
            try:
                dlg.iconbitmap(icon)
            except tk.TclError:
                pass
        frm = ttk.Frame(dlg, padding=20)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="HoI4 音乐播放列表编辑器",
                  font=("Microsoft YaHei UI", 16, "bold")).pack(pady=(0, 4))
        ttk.Label(frm, text=f"版本 {VERSION}", font=("Microsoft YaHei UI", 10)).pack()
        ttk.Label(frm, text=f"作者：{AUTHOR_NAME}", font=("Microsoft YaHei UI", 10)).pack(pady=(0, 4))
        ttk.Label(frm, text=QQ_GROUP, font=("Microsoft YaHei UI", 10)).pack(pady=(0, 10))
        ttk.Separator(frm).pack(fill="x", pady=6)
        desc = ("为《钢铁雄心4》玩家设计的音乐播放列表管理工具：\n"
                "支持 ogg 直接添加与 mp3/flac 自动转换、\n"
                "播放逻辑与音量设置、保存自动备份。")
        ttk.Label(frm, text=desc, justify="center",
                  foreground="#444444").pack(pady=4)
        ttk.Separator(frm).pack(fill="x", pady=6)
        ttk.Label(frm, text="© 2026 HoI4 Music Editor",
                  font=("Microsoft YaHei UI", 9), foreground="#888888").pack()
        btns = ttk.Frame(dlg, padding=(0, 0, 0, 12))
        btns.pack(fill="x")
        ttk.Button(btns, text="确定", command=dlg.destroy).pack(side="right", padx=12)

    # -- 默认音量 -----------------------------------------------------------

    def _set_default_volume(self):
        dlg = tk.Toplevel(self)
        dlg.title("全局默认音量")
        dlg.resizable(False, False)
        dlg.geometry("480x130")  # 显式尺寸，避免 wait_visibility 阶段窗口未布局导致裁切
        dlg.transient(self)
        dlg.grab_set()
        dlg.wait_visibility()
        center_window(dlg, self)  # 居中于主窗口所在显示器
        var = tk.DoubleVar(value=self.default_volume)
        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill="x")
        ttk.Label(frm, text="新添加歌曲的默认音量：").pack(side="left")
        val_label = ttk.Label(frm, text="", width=6, font=("Consolas", 11, "bold"))
        val_label.pack(side="left", padx=(8, 0))

        def show(_=None):
            val_label.config(text=f"{var.get():.2f}")

        # 滑块调节 + Label 显示精确值（Spinbox 在部分 DPI 缩放下数值被裁切）
        tk.Scale(frm, from_=0.0, to=2.0, resolution=0.05, orient="horizontal",
                 length=200, variable=var, command=show).pack(side="left", padx=8)
        show()

        def ok():
            v = var.get()
            if not (0.0 <= v <= 2.0):
                messagebox.showerror("错误", "音量范围 0.0 – 2.0", parent=dlg)
                return
            self.default_volume = v
            self.config["default_volume"] = v
            save_config(self.config)
            dlg.destroy()

        btns = ttk.Frame(dlg, padding=(12, 0, 12, 12))
        btns.pack(fill="x")
        ttk.Button(btns, text="确定", command=ok).pack(side="right", padx=4)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right")

    # -- 修复格式 -----------------------------------------------------------

    def _repair_format(self):
        """
        一键修复：电台模式下排查并修复电台模板问题（gui 资源引用 / supported_version /
        图标尺寸 / UTF-8 BOM）；普通文件夹模式下把 music.asset / _songs.txt 重建为标准格式。
        均自动备份 .bak。
        """
        if self._mode == "radio" and self.radio_mod:
            self._repair_radio_mod()
            return
        if not self.mf:
            messagebox.showinfo("提示", "请先选择 music 文件夹")
            return
        if not messagebox.askyesno(
                "修复格式",
                f"将把以下文件重新格式化为标准格式（参考 pla_music.asset / pla_songs.txt）：\n"
                f"  {os.path.basename(self.mf.asset_path)}\n"
                f"  {os.path.basename(self.mf.songs_path)}\n\n"
                "标准格式：每个 music 块之间空一行、规范缩进，\n"
                "注释与 music_station 保留，原文件自动备份为 .bak。\n"
                "未保存的修改也会一并保存。是否继续？",
                icon="warning"):
            return
        # 先提交缓存中已转换的文件
        if self._pending_files:
            committed = commit_pending_files(self.folder, self._pending_files)
            for old_target, new_target in committed.items():
                if new_target != old_target:
                    for e in self.mf.entries:
                        if e.file == old_target:
                            e.file = new_target
            self._pending_files.clear()
        try:
            self.mf.save(backup=True)
        except OSError as e:
            messagebox.showerror("修复失败", str(e))
            return
        self.dirty = False
        self._refresh_tree()
        messagebox.showinfo("修复完成", "文件已按标准格式重建，原文件已备份为 .bak")

    def _repair_radio_mod(self):
        """电台模式一键修复：先排查，再按确认修复全部问题。"""
        rm = self.radio_mod
        issues = rm.check_issues()
        if not issues:
            messagebox.showinfo("排查结果", "该电台模板未发现问题。")
            self._status_var.set("电台检查通过")
            return
        msg = ("排查到以下问题：\n\n" + "\n".join(f"· {i}" for i in issues)
               + "\n\n点击「是」将一键修复全部问题（原文件自动备份为 .bak）。")
        if not messagebox.askyesno("电台一键修复", msg, icon="warning", parent=self):
            return
        try:
            fixed = rm.repair()
        except OSError as e:
            messagebox.showerror("修复失败", str(e), parent=self)
            return
        # 修复可能改动 supported_version / 图标等，重载当前电台状态
        self._folder_var.set(f"电台 [{rm.radio_id}] {_shorten(rm.display_name, 20)}")
        # 修复后复查：确认问题已全部解决，剩余问题明确提示（避免“修了还报”）
        left = rm.check_issues()
        if left:
            self._status_var.set(f"仍有 {len(left)} 个电台问题未修复")
            messagebox.showwarning(
                "部分问题未修复",
                "以下问题未能自动修复（文件可能被占用或权限不足）：\n\n"
                + "\n".join(f"· {i}" for i in left)
                + "\n\n请检查后重试。", parent=self)
            return
        self._status_var.set("电台一键修复完成")
        messagebox.showinfo(
            "修复完成",
            "已修复：\n\n" + "\n".join(f"· {f}" for f in fixed)
            + "\n\n原文件均已备份为 .bak。", parent=self)

    # -- 保存与退出 ---------------------------------------------------------

    def _save(self):
        if not self.mf:
            messagebox.showinfo("提示", "请先选择 music 文件夹")
            return
        # 先把 cache 中已转换的文件复制进目标文件夹
        if self._pending_files:
            committed = commit_pending_files(self.folder, self._pending_files)
            for old_target, new_target in committed.items():
                if new_target != old_target:
                    for e in self.mf.entries:
                        if e.file == old_target:
                            e.file = new_target
            self._pending_files.clear()
        problems = self.mf.validate()
        if problems:
            msg = "发现以下问题，仍要保存吗？\n\n" + "\n".join(problems[:10])
            if not messagebox.askyesno("保存确认", msg, icon="warning"):
                return
        try:
            self.mf.save(backup=True)
        except OSError as e:
            messagebox.showerror("保存失败", str(e))
            return
        self.dirty = False
        self._refresh_tree()
        messagebox.showinfo(
            "保存成功",
            f"已写回：\n{self.mf.asset_path}\n{self.mf.songs_path}\n"
            "（原文件已备份为 .bak）")

    def _on_close(self):
        if self.dirty:
            if not messagebox.askyesno("未保存", "有未保存的修改，退出将丢弃。是否退出？"):
                return
        # 清理转换缓存（未保存的转换文件随程序关闭删除）
        shutil.rmtree(self._cache_dir, ignore_errors=True)
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
