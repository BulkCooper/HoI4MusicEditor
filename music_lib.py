# -*- coding: utf-8 -*-
"""
music_lib.py — HoI4 音乐播放列表解析/生成库

负责解析与写回两个文件：
  - music.asset    : music = { name = "X" file = "Y.ogg" volume = 0.65 } 块
  - _songs.txt     : music = { song = "X" chance = { modifier = { factor = N ... } } } 块

设计目标：解析时保留注释、空白、顺序与行尾；修改时只替换目标块的原始行，
其余内容（含 music_station 等顶层键、注释）原样保留。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SongRule:
    """_songs.txt 中的一条播放规则（一个 music = { ... } 块）。"""
    song: str
    factor: Optional[float] = None
    conditions: List[str] = field(default_factory=list)  # 如 ["has_war = no", "has_government = democratic"]
    raw_lines: List[str] = field(default_factory=list)   # 原始行（保留原样）


@dataclass
class AssetEntry:
    """music.asset 中的一条曲目（一个 music = { ... } 块）。"""
    name: str
    file: str
    volume: Optional[float] = None
    raw_lines: List[str] = field(default_factory=list)


class MusicDocument:
    """一个 script 文档：块外内容 + 顶层块列表。"""

    def __init__(self, outer_lines: List[str], newline: str):
        self.outer_lines = outer_lines   # 块外的行（注释、顶层键、空白），含行尾
        self.newline = newline           # 检测到的行尾（"\r\n" 或 "\n"）

    def render(self) -> str:
        return "".join(self.outer_lines)


# ---------------------------------------------------------------------------
# 底层：行级块扫描（不做词法分析，保留原文）
# ---------------------------------------------------------------------------

def _split_keepends(text: str) -> List[str]:
    """按行拆分并保留行尾。兼容 \r\n 与 \n。"""
    lines: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        j = text.find("\n", i)
        if j == -1:
            lines.append(text[i:])
            break
        lines.append(text[i : j + 1])
        i = j + 1
    return lines


def _detect_newline(text: str) -> str:
    idx = text.find("\n")
    if idx > 0 and text[idx - 1] == "\r":
        return "\r\n"
    return "\n"


def _scan_top_blocks(lines: List[str], block_key: str):
    """
    扫描顶层 block_key = { ... } 块。
    返回 (outer_lines, block_chunks)：
      outer_lines   — 块外的行（原样）
      block_chunks  — [(start_idx, end_idx_exclusive, body_lines_without_braces)]
    块必须顶格出现（strip 后以 "key = {" 开头）。
    """
    outer: List[str] = []
    chunks: List[tuple[int, int, List[str]]] = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped == f"{block_key} = {{":
            # 找匹配的右括号
            depth = 0
            j = i
            start_idx = i
            body: List[str] = []
            found = False
            while j < n:
                s = lines[j].strip()
                # 忽略注释行（# 开头的行不算括号）
                if s.startswith("#"):
                    if j > i:
                        body.append(lines[j])
                    j += 1
                    continue
                depth += s.count("{") - s.count("}")
                if j > i:
                    body.append(lines[j])
                if depth <= 0:
                    found = True
                    break
                j += 1
            if not found:
                # 括号不闭合：保守处理，整行当普通行
                outer.append(lines[i])
                i += 1
                continue
            chunks.append((start_idx, j + 1, body))
            i = j + 1
        else:
            outer.append(lines[i])
            i += 1
    return outer, chunks


def _strip_comment(line: str) -> str:
    """去掉行内注释（# 之后的内容），用于取值解析；引号内的 # 不处理（本格式无此用法）。"""
    idx = line.find("#")
    if idx == -1:
        return line
    return line[:idx]


def _parse_kv(line: str):
    """解析 "key = value" 行 → (key, value)。value 可能带引号或裸词/数字。"""
    s = _strip_comment(line).strip()
    if not s or s.startswith("#"):
        return None
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.+?)\s*$", s)
    if not m:
        return None
    key, raw = m.group(1), m.group(2)
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return key, raw[1:-1]
    return key, raw


# ---------------------------------------------------------------------------
# music.asset 解析
# ---------------------------------------------------------------------------

def parse_asset(text: str) -> tuple[List[str], List[AssetEntry]]:
    """
    解析 music.asset。
    返回 (outer_lines, entries)，entries 顺序即文件顺序。
    """
    lines = _split_keepends(text)
    outer, chunks = _scan_top_blocks(lines, "music")
    entries: List[AssetEntry] = []
    for _start, _end, body in chunks:
        name = file = None
        volume: Optional[float] = None
        for bl in body:
            kv = _parse_kv(bl)
            if not kv:
                continue
            k, v = kv
            if k == "name":
                name = v
            elif k == "file":
                file = v
            elif k == "volume":
                try:
                    volume = float(v)
                except ValueError:
                    volume = None
        if name is not None and file is not None:
            entries.append(AssetEntry(name=name, file=file, volume=volume, raw_lines=body))
    return outer, entries


# ---------------------------------------------------------------------------
# _songs.txt 解析
# ---------------------------------------------------------------------------

def parse_songs(text: str) -> tuple[List[str], List[SongRule]]:
    """
    解析 _songs.txt。
    返回 (outer_lines, rules)。music_station 等顶层键留在 outer_lines。
    """
    lines = _split_keepends(text)
    outer, chunks = _scan_top_blocks(lines, "music")
    rules: List[SongRule] = []
    for _start, _end, body in chunks:
        song = None
        factor: Optional[float] = None
        conditions: List[str] = []
        in_modifier = False
        for bl in body:
            stripped = bl.strip()
            if stripped.startswith(MAIN_THEME_COND):
                # 主菜单哨兵注释：保留以便持久化识别「主菜单」播放逻辑
                conditions.append(MAIN_THEME_COND)
                continue
            if stripped.startswith("#"):
                continue
            if stripped == "modifier = {":
                in_modifier = True
                continue
            if stripped == "}" and in_modifier:
                # 可能结束 modifier 或 chance
                continue
            if in_modifier:
                kv = _parse_kv(stripped)
                if kv:
                    k, v = kv
                    if k == "factor":
                        try:
                            factor = float(v)
                        except ValueError:
                            pass
                    else:
                        conditions.append(f"{k} = {v}")
                continue
            kv = _parse_kv(stripped)
            if kv and kv[0] == "song":
                song = kv[1]
        if song is not None:
            rules.append(SongRule(song=song, factor=factor, conditions=conditions, raw_lines=body))
    return outer, rules


# ---------------------------------------------------------------------------
# 生成规范格式（新增/编辑条目用）
# ---------------------------------------------------------------------------

def _fmt_asset_entry(name: str, file: str, volume: Optional[float]) -> List[str]:
    """标准格式（参考 pla_music.asset）：块之间空一行。"""
    nl = "\n"
    out = ["music = {", f'\tname = "{name}"', f'\tfile = "{file}"']
    if volume is not None:
        out.append(f"\tvolume = {volume:g}")
    out.append("}")
    return [l + nl for l in out] + [nl]


def _fmt_song_rule(song: str, factor: Optional[float], conditions: List[str]) -> List[str]:
    """标准格式（参考 pla_songs.txt）：块之间空一行，闭合行保留尾随 tab。"""
    nl = "\n"
    out = ["music = {", f'\tsong = "{song}"', "\tchance = {", "\t\tmodifier = {"]
    if factor is not None:
        out.append(f"\t\t\tfactor = {factor:g}")
    for cond in conditions:
        c = cond.strip()
        if c:
            out.append(f"\t\t\t{c}")
    out.append("\t\t}\t\t")
    out.append("\t}")
    out.append("}")
    return [l + nl for l in out] + [nl]


# ---------------------------------------------------------------------------
# 播放逻辑预设（GUI 用）
# ---------------------------------------------------------------------------

PRESET_WAR = {
    "任意": None,
    "和平": "has_war = no",
    "战争": "has_war = yes",
}

PRESET_FACTION = {
    "通用": None,
    "同盟国": "has_government = democratic",
    "轴心国": "has_government = fascism",
    "共产国际": "has_government = communism",
}

# 反查：条件 → 预设名
_WAR_BY_COND = {v: k for k, v in PRESET_WAR.items() if v}
_FACTION_BY_COND = {v: k for k, v in PRESET_FACTION.items() if v}


# 主菜单音乐标记（哨兵注释：写入 _songs.txt 的 modifier 内，游戏按注释忽略，
# 仅用于程序识别并持久化「主菜单」播放逻辑）
MAIN_THEME_COND = "# maintheme"


def conditions_from_preset(war: str, faction: str) -> List[str]:
    """由预设（和平/战争/任意/主菜单 × 阵营/通用）生成条件列表。
    主菜单：引擎固定播放名为 maintheme 的歌曲，无真实条件，仅带哨兵标记。"""
    if war == "主菜单":
        return [MAIN_THEME_COND]
    conds: List[str] = []
    c = PRESET_WAR.get(war)
    if c:
        conds.append(c)
    c = PRESET_FACTION.get(faction)
    if c:
        conds.append(c)
    return conds


def preset_from_conditions(conditions: List[str]) -> tuple[str, str]:
    """由条件列表反推预设名；无法识别的条件返回 任意/通用。"""
    war = "任意"
    faction = "通用"
    for cond in conditions:
        c = cond.strip()
        if c == MAIN_THEME_COND:
            war = "主菜单"
        elif c in _WAR_BY_COND:
            war = _WAR_BY_COND[c]
        elif c in _FACTION_BY_COND:
            faction = _FACTION_BY_COND[c]
    return war, faction


def summarize_conditions(conditions: List[str]) -> str:
    """生成播放逻辑摘要文本（树视图显示）。"""
    if not conditions:
        return "任意播放"
    war, faction = preset_from_conditions(conditions)
    extra = [c for c in conditions if c.strip() not in _WAR_BY_COND
             and c.strip() not in _FACTION_BY_COND and c.strip() != MAIN_THEME_COND]
    parts = []
    if war != "任意":
        parts.append(war)
    if faction != "通用":
        parts.append(faction)
    if extra:
        parts.append("; ".join(extra))
    return " · ".join(parts) if parts else "任意播放"


# ---------------------------------------------------------------------------
# 音频转换（mp3/flac/wav 等 → ogg）
# ---------------------------------------------------------------------------

def convert_to_ogg(src: str, dst: str, quality: int = 5) -> None:
    """
    用捆绑的 ffmpeg（imageio-ffmpeg）将任意音频文件转换为 ogg vorbis。
    quality: libvorbis 质量 0-10（默认 5 ≈ 160kbps，接近原版风格）。
    失败时抛出 OSError（含 ffmpeg 错误信息）。
    """
    import subprocess
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    # -vn：丢弃视频流/封面流，只保留音频（避免源文件含视频轨时 ogg 里混入画面）
    # encoding="utf-8", errors="replace"：ffmpeg 输出可能含非 GBK 字节，
    # 用 text=True 默认编码在中文系统会抛 UnicodeDecodeError 导致转换失败
    cmd = [ffmpeg, "-y", "-i", src, "-vn",
           "-c:a", "libvorbis", "-q:a", str(quality), dst]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not os.path.exists(dst):
        err = (proc.stderr or "").strip()[-500:]
        raise OSError(err or "转换失败")


# ---------------------------------------------------------------------------
# 开局音效（start_game）相关：转 WAV + 裁剪 + 时长
# ---------------------------------------------------------------------------
# 游戏规格（见 start_game音效修改.md）：WAV 真 PCM、44.1kHz / 16bit / 立体声

START_GAME_NAME = "start_game_01"          # 点击「开始游戏」时播放（引擎写死，不能改名）
START_GAME_NAME_2 = "start_game_02"        # 读条完成进入游戏世界时播放（真正能听到的）
START_GAME_WAV_DIR = "menu"                # 相对 mod/sound/ 目录
START_GAME_MAX_SECONDS = 15.0              # 裁剪时长上限
WAV_RATE = 44100
WAV_CHANNELS = 2


def convert_to_wav(src: str, dst: str, start: Optional[float] = None,
                   duration: Optional[float] = None) -> None:
    """
    用 ffmpeg 将任意音频转换为 44.1kHz / 16bit / 立体声 PCM WAV。
    start/duration：可选，裁剪区间（秒），用于截取音频片段。
    失败时抛出 OSError（含 ffmpeg 错误信息）。
    """
    import subprocess
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    # -vn 丢弃视频流/封面流；pcm_s16le + 44.1kHz + 立体声为游戏规格
    cmd = [ffmpeg, "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", src]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-c:a", "pcm_s16le", "-ar", str(WAV_RATE),
            "-ac", str(WAV_CHANNELS), dst]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not os.path.exists(dst):
        err = (proc.stderr or "").strip()[-500:]
        raise OSError(err or "转换失败")


def wav_duration(path: str) -> float:
    """读取 WAV 时长（秒）。非 PCM/WAV 或读取失败抛 OSError。"""
    import wave
    with wave.open(path, "rb") as w:
        return w.getnframes() / w.getframerate()


def crop_wav(src: str, dst: str, start: float, end: float) -> None:
    """纯 Python 切片 PCM WAV（毫秒级，无 ffmpeg）：取 [start, end) 秒区间写新文件。
    用于试听/安装前的快速裁剪；源必须是 PCM WAV（如探测生成的 44.1k 中间文件）。"""
    import wave
    with wave.open(src, "rb") as r:
        rate = r.getframerate()
        n = r.getnframes()
        s = max(0, min(int(start * rate), n))
        e = max(s, min(int(end * rate), n))
        r.setpos(s)
        frames = r.readframes(e - s)
        params = r.getparams()
    with wave.open(dst, "wb") as w:
        w.setparams(params)
        w.writeframes(frames)


def apply_fade_out(src: str, dst: str, fade_seconds: float) -> None:
    """对 PCM WAV 末尾 fade_seconds 秒做线性淡出（音量渐降到 0），写新文件。
    纯 Python（array 加速）；fade_seconds <= 0 时等同复制。"""
    import array
    import wave
    if fade_seconds is None or fade_seconds <= 0:
        shutil.copy2(src, dst)
        return
    with wave.open(src, "rb") as r:
        params = r.getparams()
        rate = r.getframerate()
        ch = r.getnchannels()
        data = array.array("h", r.readframes(r.getnframes()))
    total = len(data)
    fade = min(int(fade_seconds * rate * ch), total)
    if fade > 0:
        for i in range(fade):
            frac = (fade - i) / fade        # 1 → 0：淡出段开头正常，末尾渐降到静音
            data[total - fade + i] = int(data[total - fade + i] * frac)
    with wave.open(dst, "wb") as w:
        w.setparams(params)
        w.writeframes(data.tobytes())


def make_start_game_wav(src: str, dst: str, start: float, end: float) -> float:
    """转换并裁剪出开局音效 WAV（限制 ≤ START_GAME_MAX_SECONDS）。返回实际时长（秒）。"""
    duration = min(max(end - start, 0.1), START_GAME_MAX_SECONDS)
    convert_to_wav(src, dst, start=start, duration=duration)
    return wav_duration(dst)


# ---------------------------------------------------------------------------
# 文件夹级封装
# ---------------------------------------------------------------------------

ASSET_FILENAME = "music.asset"
SONGS_FILENAME = "_songs.txt"


def find_music_groups(folder: str):
    """
    扫描文件夹中的音乐文件组，返回 [(label, asset_name, songs_name)]。
    按前缀配对：{prefix}_music.asset ↔ {prefix}_songs.txt；
    默认组 music.asset ↔ _songs.txt 的 prefix 为空字符串。
    asset_name 或 songs_name 可能为 None（该组只有其中一个文件）。
    """
    assets: dict = {}  # prefix -> 文件名
    songs: dict = {}   # prefix -> 文件名
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for f in names:
        low = f.lower()
        if low.endswith("_music.asset"):
            assets[low[: -len("_music.asset")]] = f
        elif low == "music.asset":
            assets[""] = f
        elif low.endswith("_songs.txt"):
            songs[low[: -len("_songs.txt")]] = f
        elif low == "_songs.txt":
            songs[""] = f
    groups = []
    for prefix in sorted(set(assets) | set(songs)):
        groups.append((prefix or "(默认)", assets.get(prefix), songs.get(prefix)))
    return groups


class MusicFolder:
    """封装一个音乐文件夹（含 music.asset 与 _songs.txt 或其 mod 自定义命名）的读写。"""

    def __init__(self, folder: str,
                 asset_filename: str = ASSET_FILENAME,
                 songs_filename: str = SONGS_FILENAME):
        self.folder = folder
        self.asset_path = os.path.join(folder, asset_filename)
        self.songs_path = os.path.join(folder, songs_filename)
        self.asset_filename = asset_filename
        self.songs_filename = songs_filename
        self.entries: List[AssetEntry] = []
        self.rules: List[SongRule] = []
        self._asset_outer: List[str] = []
        self._songs_outer: List[str] = []
        self._asset_nl = "\n"
        self._songs_nl = "\n"

    # -- 读取 --------------------------------------------------------------

    def load(self) -> None:
        """读取两个文件；文件不存在则视为空文档。"""
        if os.path.exists(self.asset_path):
            with open(self.asset_path, "r", encoding="utf-8-sig", newline="") as f:
                text = f.read()
            self._asset_nl = _detect_newline(text)
            self._asset_outer, self.entries = parse_asset(text)
        else:
            self._asset_nl = "\n"
            self._asset_outer, self.entries = [], []

        if os.path.exists(self.songs_path):
            with open(self.songs_path, "r", encoding="utf-8-sig", newline="") as f:
                text = f.read()
            self._songs_nl = _detect_newline(text)
            self._songs_outer, self.rules = parse_songs(text)
        else:
            self._songs_nl = "\n"
            self._songs_outer, self.rules = [], []

    def is_loaded(self) -> bool:
        return os.path.exists(self.asset_path) or os.path.exists(self.songs_path)

    # -- 查询 --------------------------------------------------------------

    def entry_by_name(self, name: str) -> Optional[AssetEntry]:
        for e in self.entries:
            if e.name == name:
                return e
        return None

    def rules_for_song(self, song: str) -> List[SongRule]:
        return [r for r in self.rules if r.song == song]

    def file_exists(self, file_name: str) -> bool:
        return os.path.exists(os.path.join(self.folder, file_name))

    def unique_song_name(self, base: str) -> str:
        """生成唯一 song name：小写、非中英文（unicode 字母数字）转下划线、冲突加 _2/_3 后缀。
        中文等 unicode 字符保留原文（如 边境1 → 边境1）。"""
        clean = re.sub(r"[^\w]+", "_", base.lower(), flags=re.UNICODE).strip("_")
        if not clean:
            clean = "song"
        names = {e.name for e in self.entries} | {r.song for r in self.rules}
        if clean not in names:
            return clean
        i = 2
        while f"{clean}_{i}" in names:
            i += 1
        return f"{clean}_{i}"

    def unique_filename(self, file_name: str) -> str:
        """生成不冲突的目标文件名（保留扩展名）。"""
        base, ext = os.path.splitext(file_name)
        used = {os.path.basename(e.file) for e in self.entries} | set(os.listdir(self.folder))
        if file_name not in used:
            return file_name
        i = 2
        while f"{base}_{i}{ext}" in used:
            i += 1
        return f"{base}_{i}{ext}"

    # -- 修改（内存中，save() 时落盘） --------------------------------------

    def add_entry(self, name: str, file: str, volume: Optional[float],
                  factor: Optional[float], conditions: List[str]) -> None:
        """添加曲目（asset + songs 规则）。"""
        assert self.entry_by_name(name) is None, f"song name 已存在: {name}"
        self.entries.append(AssetEntry(name=name, file=file, volume=volume))
        self.rules.append(SongRule(song=name, factor=factor, conditions=[c for c in conditions if c.strip()]))

    def update_entry(self, name: str, new_file: Optional[str] = None,
                     new_volume: Optional[float] = None, new_name: Optional[str] = None,
                     new_factor: Optional[float] = None,
                     new_conditions: Optional[List[str]] = None) -> None:
        """编辑已有曲目；None 表示不修改该字段。"""
        entry = self.entry_by_name(name)
        if entry is None:
            return
        if new_name is not None and new_name != name:
            assert self.entry_by_name(new_name) is None, f"song name 已存在: {new_name}"
            entry.name = new_name
            for r in self.rules:
                if r.song == name:
                    r.song = new_name
            name = new_name
        if new_file is not None:
            entry.file = new_file
        if new_volume is not None:
            entry.volume = new_volume
        if new_factor is not None:
            for r in self.rules_for_song(name):
                r.factor = new_factor
        if new_conditions is not None:
            conds = [c for c in new_conditions if c.strip()]
            rules = self.rules_for_song(name)
            if rules:
                for r in rules:
                    r.conditions = list(conds)
            else:
                self.rules.append(SongRule(song=name, factor=new_factor, conditions=list(conds)))

    def remove_entry(self, name: str) -> None:
        """移除曲目（asset 条目 + 全部对应规则）。"""
        self.entries = [e for e in self.entries if e.name != name]
        self.rules = [r for r in self.rules if r.song != name]

    # -- 写回 --------------------------------------------------------------

    def save(self, backup: bool = True) -> None:
        """
        写回两个文件。
        策略：重新渲染文档 = 块外行 + 重建的块（块内注释保留在原始 raw_lines 中，
        数据部分用规范格式重新生成，保证字段顺序/格式一致）。
        """
        # ---- music.asset ----
        asset_lines: List[str] = []
        asset_lines.extend(self._asset_outer)
        for e in self.entries:
            asset_lines.extend(_fmt_asset_entry(e.name, e.file, e.volume))
        asset_text = "".join(asset_lines)

        # ---- _songs.txt ----
        songs_lines: List[str] = []
        songs_lines.extend(self._songs_outer)
        for r in self.rules:
            songs_lines.extend(_fmt_song_rule(r.song, r.factor, r.conditions))
        songs_text = "".join(songs_lines)

        self._write(self.asset_path, asset_text, backup)
        self._write(self.songs_path, songs_text, backup)

    @staticmethod
    def _write(path: str, text: str, backup: bool) -> None:
        if backup and os.path.exists(path):
            bak = path + ".bak"
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                data = f.read()
            with open(bak, "w", encoding="utf-8", newline="") as f:
                f.write(data)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    # -- 校验 --------------------------------------------------------------

    def validate(self) -> List[str]:
        """返回问题列表（空列表 = 通过）。"""
        problems: List[str] = []
        names = [e.name for e in self.entries]
        dupes = {n for n in names if names.count(n) > 1}
        for d in sorted(dupes):
            problems.append(f"music.asset 中 song name 重复: {d}")
        song_names = [r.song for r in self.rules]
        dupes2 = {n for n in song_names if song_names.count(n) > 1}
        for d in sorted(dupes2):
            problems.append(f"_songs.txt 中 song 重复: {d}")
        for e in self.entries:
            if not e.file:
                problems.append(f"条目 {e.name} 缺少 file")
            elif not self.file_exists(e.file):
                problems.append(f"条目 {e.name} 引用的文件不存在: {e.file}")
        for r in self.rules:
            if not r.song:
                problems.append("存在无 song 名的规则")
        return problems


# ---------------------------------------------------------------------------
# 独立本地电台 MOD（一个 MOD = 一个电台）
# ---------------------------------------------------------------------------
# 产物结构（可直接放入 文档\Paradox Interactive\Hearts of Iron IV\mod\）：
#   <mod_dir>/<radio_id>.mod               游戏注册文件（含 path 指向 mod 文件夹）
#   <mod_dir>/<radio_id>/
#       descriptor.mod                     元数据（同 .mod 内容）
#       gfx/<radio_id>_album_art.png       电台图标（304x120 横向两帧，每帧 152x120）
#       interface/music_station_<radio_id>.gfx   sprite 注册
#       interface/music_station_<radio_id>.gui   faceplate + stations_entry
#       music/<radio_id>/
#           <radio_id>_music.asset         由 MusicFolder 管理
#           <radio_id>_songs.txt           由 MusicFolder 管理
# ---------------------------------------------------------------------------

ALBUM_ART_FRAME_W = 152   # 单帧宽（原版 base_game_album_art.dds 权威尺寸：每帧 152x120）
ALBUM_ART_FRAME_H = 120   # 单帧高
DEFAULT_SUPPORTED_VERSION = "1.19.*"   # 注意：不能写 "1.*"（启动器校验为非法格式）


def is_valid_radio_id(s: str) -> bool:
    """电台 ID 合法性：字母开头，仅字母/数字/下划线，长度 <= 32。"""
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", s or ""))


def _descriptor_text(radio_id: str, display_name: str,
                     supported_version: str = DEFAULT_SUPPORTED_VERSION) -> str:
    """生成 descriptor.mod / 注册 .mod 的内容。"""
    return (f'version="1.0"\n'
            f'tags={{\n\t"Sound"\n}}\n'
            f'name="{display_name}"\n'
            f'supported_version="{supported_version}"\n'
            f'path="mod/{radio_id}"\n')


def _gfx_template(radio_id: str) -> str:
    return (
        "# ============================================\n"
        "#  sprite 注册:把电台图标图片注册为游戏资源\n"
        "# ============================================\n"
        "# name 被 .gui 文件引用,两者要一致\n"
        "# noOfFrames = 2:图片是\"横向两帧\"拼在一张图里(标准尺寸 304x120,每帧 152x120)\n"
        "#   第 1 帧(左)= 未选中,第 2 帧(右)= 选中(高亮)\n"
        "# 正式发布建议用 DDS(DXT5);png 也能加载,但体积大\n"
        "spriteTypes = {\n"
        "\tspriteType = {\n"
        f'\t\tname = "GFX_{radio_id}_album_art"\n'
        f'\t\ttexturefile = "gfx/{radio_id}_album_art.png"\n'
        "\t\tnoOfFrames = 2\n"
        "\t}\n"
        "}\n"
    )


def _gui_template(radio_id: str) -> str:
    """电台界面：faceplate(播放器面板) + stations_entry(电台列表图标)。
    注意：只引用游戏原版自带的 sprite/字体（勿抄其它 mod 的自定义资源）——
    原版 faceplate 无 musicplayer_head 元素；字体用 hoi_20b / hoi_18b。"""
    return (
        "# ============================================\n"
        "#  电台界面:faceplate(播放器面板)+ stations_entry(电台列表图标)\n"
        "# ============================================\n"
        "# 容器 name 必须严格是\"电台名_faceplate\"和\"电台名_stations_entry\",\n"
        "# 游戏按容器名自动绑定电台(这就是\"挂载\"生效的关键)\n"
        "# quadTextureSprite 引用 .gfx 里注册的 sprite 名\n"
        "# 只引用游戏原版自带的 sprite/字体;勿抄其它 mod 的自定义资源\n"
        "guiTypes = {\n"
        "\n"
        "\t# ---- 切到本电台后,播放器顶部的面板 ----\n"
        "\tcontainerWindowType = {\n"
        f'\t\tname = "{radio_id}_faceplate"\n'
        "\t\tposition = { x = 0 y = 0 }\n"
        "\t\tsize = { width = 590 height = 46 }\n"
        "\n"
        "\t\ticonType = {\n"
        "\t\t\tname = \"musicplayer_header_bg\"\n"
        "\t\t\tspriteType = \"GFX_musicplayer_header_bg\"\n"
        "\t\t\tposition = { x = 0 y = 0 }\n"
        "\t\t\talwaystransparent = yes\n"
        "\t\t}\n"
        "\t\tinstantTextboxType = {\n"
        "\t\t\tname = \"track_name\"\n"
        "\t\t\tposition = { x = 72 y = 15 }\n"
        "\t\t\tfont = \"hoi_20b\"\n"
        "\t\t\ttext = \"track name here\"\n"
        "\t\t\tmaxWidth = 450\n"
        "\t\t\tmaxHeight = 25\n"
        "\t\t\tformat = center\n"
        "\t\t}\n"
        "\t\tinstantTextboxType = {\n"
        "\t\t\tname = \"track_elapsed\"\n"
        "\t\t\tposition = { x = 255 y = 39 }\n"
        "\t\t\tfont = \"hoi_18b\"\n"
        "\t\t\ttext = \"00:00\"\n"
        "\t\t\tmaxWidth = 50\n"
        "\t\t\tmaxHeight = 25\n"
        "\t\t\tformat = center\n"
        "\t\t}\n"
        "\t\tinstantTextboxType = {\n"
        "\t\t\tname = \"track_duration\"\n"
        "\t\t\tposition = { x = 322 y = 39 }\n"
        "\t\t\tfont = \"hoi_18b\"\n"
        "\t\t\ttext = \"02:58\"\n"
        "\t\t\tmaxWidth = 50\n"
        "\t\t\tmaxHeight = 25\n"
        "\t\t\tformat = center\n"
        "\t\t}\n"
        "\t\tbuttonType = {\n"
        "\t\t\tname = \"prev_button\"\n"
        "\t\t\tposition = { x = 220 y = 12 }\n"
        "\t\t\tquadTextureSprite = \"GFX_musicplayer_previous_button\"\n"
        "\t\t\tbuttonFont = \"Main_14_black\"\n"
        "\t\t\tOrientation = \"LOWER_LEFT\"\n"
        "\t\t\tclicksound = click_close\n"
        "\t\t\tpdx_tooltip = \"MUSICPLAYER_PREV\"\n"
        "\t\t}\n"
        "\t\tbuttonType = {\n"
        "\t\t\tname = \"play_button\"\n"
        "\t\t\tposition = { x = 263 y = 10 }\n"
        "\t\t\tquadTextureSprite = \"GFX_musicplayer_play_pause_button\"\n"
        "\t\t\tbuttonFont = \"Main_14_black\"\n"
        "\t\t\tOrientation = \"LOWER_LEFT\"\n"
        "\t\t\tclicksound = click_close\n"
        "\t\t}\n"
        "\t\tbuttonType = {\n"
        "\t\t\tname = \"next_button\"\n"
        "\t\t\tposition = { x = 336 y = 10 }\n"
        "\t\t\tquadTextureSprite = \"GFX_musicplayer_next_button\"\n"
        "\t\t\tbuttonFont = \"Main_14_black\"\n"
        "\t\t\tOrientation = \"LOWER_LEFT\"\n"
        "\t\t\tclicksound = click_close\n"
        "\t\t\tpdx_tooltip = \"MUSICPLAYER_NEXT\"\n"
        "\t\t}\n"
        "\t\textendedScrollbarType = {\n"
        "\t\t\tname = \"volume_slider\"\n"
        "\t\t\tposition = { x = 306 y = 65 }\n"
        "\t\t\tsize = { width = 75 height = 18 }\n"
        "\t\t\ttileSize = { width = 12 height = 12 }\n"
        "\t\t\tmaxValue = 100\n"
        "\t\t\tminValue = 0\n"
        "\t\t\tstepSize = 1\n"
        "\t\t\tstartValue = 50\n"
        "\t\t\thorizontal = yes\n"
        "\t\t\torientation = lower_left\n"
        "\t\t\torigo = lower_left\n"
        "\t\t\tsetTrackFrameOnChange = yes\n"
        "\t\t\tslider = {\n"
        "\t\t\t\tname = \"Slider\"\n"
        "\t\t\t\tquadTextureSprite = \"GFX_scroll_drager\"\n"
        "\t\t\t\tposition = { x = 0 y = 1 }\n"
        "\t\t\t\tpdx_tooltip = \"MUSICPLAYER_ADJUST_VOL\"\n"
        "\t\t\t}\n"
        "\t\t\ttrack = {\n"
        "\t\t\t\tname = \"Track\"\n"
        "\t\t\t\tquadTextureSprite = \"GFX_volume_track\"\n"
        "\t\t\t\tposition = { x = 0 y = 3 }\n"
        "\t\t\t\talwaystransparent = yes\n"
        "\t\t\t\tpdx_tooltip = \"MUSICPLAYER_ADJUST_VOL\"\n"
        "\t\t\t}\n"
        "\t\t}\n"
        "\t\tbuttonType = {\n"
        "\t\t\tname = \"shuffle_button\"\n"
        "\t\t\tposition = { x = 370 y = 10 }\n"
        "\t\t\tquadTextureSprite = \"GFX_toggle_shuffle_buttons\"\n"
        "\t\t\tbuttonFont = \"Main_14_black\"\n"
        "\t\t\tOrientation = \"LOWER_LEFT\"\n"
        "\t\t\tclicksound = click_close\n"
        "\t\t}\n"
        "\t}\n"
        "\n"
        "\t# ---- 电台选择列表里的图标按钮(点它切换电台) ----\n"
        "\tcontainerWindowType = {\n"
        f'\t\tname = "{radio_id}_stations_entry"\n'
        "\t\tsize = { width = 162 height = 130 }\n"
        "\n"
        "\t\tcheckBoxType = {\n"
        "\t\t\tname = \"select_station_button\"\n"
        f'\t\t\tquadTextureSprite = "GFX_{radio_id}_album_art"\n'
        "\t\t\tclicksound = decisions_ui_button\n"
        "\t\t}\n"
        "\t}\n"
        "}\n"
    )


def _asset_template(radio_id: str) -> str:
    return (
        "# ============================================\n"
        f"#  电台:{radio_id} —— 歌曲资产定义(music.asset)\n"
        "# ============================================\n"
        "# name 是歌曲全局唯一 ID;file 相对本文件所在目录指向 .ogg\n"
        "# 歌曲请用程序「添加歌曲」功能加入\n"
    )


def _songs_template(radio_id: str) -> str:
    return (
        "# ============================================\n"
        f"#  电台:{radio_id} —— 电台声明与歌曲挂载(_songs.txt)\n"
        "# ============================================\n"
        "# music_station = \"电台名\":声明电台,名字会原样显示在游戏播放器里\n"
        "# 电台名不能与已有电台重复(同名=合并播放池)\n"
        "# chance.modifier.factor 是播放权重;可写任意触发条件\n"
        f'music_station = "{radio_id}"\n'
    )


# -- 电台图标（Pillow；函数内 import 保持模块轻量） ------------------------

def cover_crop_to_frame(image, frame_w: int = ALBUM_ART_FRAME_W,
                        frame_h: int = ALBUM_ART_FRAME_H):
    """等比缩放 + 居中裁剪（cover）为 frame_w x frame_h 的单帧图像。"""
    from PIL import Image
    img = image.convert("RGB")
    scale = max(frame_w / img.width, frame_h / img.height)
    nw = max(1, round(img.width * scale))
    nh = max(1, round(img.height * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - frame_w) // 2
    top = (nh - frame_h) // 2
    return img.crop((left, top, left + frame_w, top + frame_h))


def compose_album_art(frame_img, out_path: str, frame_w: int = ALBUM_ART_FRAME_W,
                      frame_h: int = ALBUM_ART_FRAME_H,
                      with_border: bool = False) -> None:
    """单帧图像 → 横向两帧 png（帧 1 原样、帧 2 提亮=选中态），宽 frame_w*2 高 frame_h。
    标准：304x120 整图，每帧 152x120（原版 base_game_album_art.dds 权威尺寸）。
    with_border=True 时：左帧红边框、右帧绿边框（宽 2px），用于提示未选中/选中态。"""
    from PIL import Image, ImageEnhance, ImageDraw
    canvas = Image.new("RGB", (frame_w * 2, frame_h))
    canvas.paste(frame_img, (0, 0))
    selected = ImageEnhance.Brightness(frame_img).enhance(1.18)
    canvas.paste(selected, (frame_w, 0))
    if with_border:
        d = ImageDraw.Draw(canvas)
        # 左帧（未选中）红边框、右帧（选中）绿边框，宽 2px
        d.rectangle([0, 0, frame_w - 1, frame_h - 1], outline=(214, 48, 49), width=2)
        d.rectangle([frame_w, 0, frame_w * 2 - 1, frame_h - 1], outline=(46, 160, 67), width=2)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    canvas.save(out_path, "PNG")


def make_album_art_from_image(src_path: str, out_path: str) -> None:
    """用任意图片生成电台图标（cover 适配到两帧）。用户更换图标：两帧加红/绿边框提示状态。"""
    from PIL import Image
    with Image.open(src_path) as img:
        img.load()
        frame = cover_crop_to_frame(img)
    compose_album_art(frame, out_path, with_border=True)


def make_default_album_art(out_path: str, frame_w: int = ALBUM_ART_FRAME_W,
                           frame_h: int = ALBUM_ART_FRAME_H) -> None:
    """生成深色渐变底 + 音符占位图标（两帧，帧 2 为选中高亮态）。"""
    from PIL import Image, ImageDraw
    frame = Image.new("RGB", (frame_w, frame_h))
    d = ImageDraw.Draw(frame)
    for y in range(frame_h):
        v = 30 + int(26 * y / max(1, frame_h - 1))
        d.line([(0, y), (frame_w, y)], fill=(v, v, v + 10))
    # 音符：两条竖线 + 顶部横梁 + 底部两个椭圆符头
    cx, cy = frame_w // 2, frame_h // 2
    stem_top = cy - frame_h // 3
    stem_bot = cy + frame_h // 4
    for x in (cx - 12, cx + 12):
        d.line([(x, stem_top), (x, stem_bot)], fill=(235, 235, 240), width=3)
    d.line([(cx - 12, stem_top), (cx + 12, stem_top)], fill=(235, 235, 240), width=5)
    for bx in (cx - 12, cx + 12):
        d.ellipse([bx - 8, stem_bot - 4, bx + 8, stem_bot + 12], fill=(235, 235, 240))
    d.rectangle([1, 1, frame_w - 2, frame_h - 2], outline=(90, 90, 100), width=1)
    compose_album_art(frame, out_path, frame_w, frame_h)


# -- RadioMod 类 --------------------------------------------------------------

class RadioMod:
    """独立的本地电台 MOD：一个 MOD 文件夹 = 一个电台。"""

    def __init__(self, radio_id: str, display_name: str, mod_dir: str,
                 supported_version: str = DEFAULT_SUPPORTED_VERSION):
        if not is_valid_radio_id(radio_id):
            raise ValueError(
                f"电台 ID 不合法: {radio_id!r}（需字母开头，仅含字母/数字/下划线，长度<=32）")
        self.radio_id = radio_id
        self.display_name = display_name
        self.mod_dir = mod_dir
        self.supported_version = supported_version

    # -- 路径 ---------------------------------------------------------------

    @property
    def mod_folder(self) -> str:
        return os.path.join(self.mod_dir, self.radio_id)

    @property
    def reg_file(self) -> str:
        return os.path.join(self.mod_dir, self.radio_id + ".mod")

    @property
    def descriptor_path(self) -> str:
        return os.path.join(self.mod_folder, "descriptor.mod")

    @property
    def gfx_dir(self) -> str:
        return os.path.join(self.mod_folder, "gfx")

    @property
    def interface_dir(self) -> str:
        return os.path.join(self.mod_folder, "interface")

    @property
    def music_dir(self) -> str:
        return os.path.join(self.mod_folder, "music", self.radio_id)

    @property
    def album_art_path(self) -> str:
        return os.path.join(self.gfx_dir, f"{self.radio_id}_album_art.png")

    @property
    def asset_filename(self) -> str:
        return f"{self.radio_id}_music.asset"

    @property
    def songs_filename(self) -> str:
        return f"{self.radio_id}_songs.txt"

    # -- 元数据 -------------------------------------------------------------

    def descriptor_text(self) -> str:
        return _descriptor_text(self.radio_id, self.display_name, self.supported_version)

    def reg_text(self) -> str:
        return _descriptor_text(self.radio_id, self.display_name, self.supported_version)

    # -- 操作 ---------------------------------------------------------------

    def create(self) -> "RadioMod":
        """生成完整 MOD 结构（目录 + 元数据 + 模板 + 占位图标 + 空 music 文档）。"""
        for d in (self.mod_folder, self.gfx_dir, self.interface_dir, self.music_dir):
            os.makedirs(d, exist_ok=True)
        self.write_descriptor()
        self.write_reg_file()
        with open(os.path.join(self.interface_dir, f"music_station_{self.radio_id}.gfx"),
                  "w", encoding="utf-8", newline="\n") as f:
            f.write(_gfx_template(self.radio_id))
        with open(os.path.join(self.interface_dir, f"music_station_{self.radio_id}.gui"),
                  "w", encoding="utf-8", newline="\n") as f:
            f.write(_gui_template(self.radio_id))
        if not os.path.exists(self.album_art_path):
            make_default_album_art(self.album_art_path)
        # 初始 music 文档：写入模板后加载进 MusicFolder（保留注释头与电台声明）
        for path, text in (
            (os.path.join(self.music_dir, self.asset_filename), _asset_template(self.radio_id)),
            (os.path.join(self.music_dir, self.songs_filename), _songs_template(self.radio_id)),
        ):
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        self.music_folder().load()
        return self

    def write_descriptor(self) -> None:
        os.makedirs(self.mod_folder, exist_ok=True)
        # utf-8-sig(带 BOM)：Paradox 启动器无 BOM 时无法正确解析中文等非 ASCII 显示名
        with open(self.descriptor_path, "w", encoding="utf-8-sig", newline="\n") as f:
            f.write(self.descriptor_text())

    def write_reg_file(self) -> None:
        os.makedirs(self.mod_dir, exist_ok=True)
        # utf-8-sig(带 BOM)：同上，否则游戏内电台名显示为 ??????
        with open(self.reg_file, "w", encoding="utf-8-sig", newline="\n") as f:
            f.write(self.reg_text())

    def music_folder(self) -> MusicFolder:
        """返回管理歌曲的 MusicFolder。
        优先 <mod>/music/<radio_id>/ 标准结构；若该目录下没有任何音乐文件，
        回退到 <mod>/music/ 根目录（兼容手工搭建/其他工具生成的电台结构）。
        """
        standard = MusicFolder(self.music_dir, self.asset_filename, self.songs_filename)
        if os.path.exists(standard.asset_path) or os.path.exists(standard.songs_path):
            return standard
        music_root = os.path.join(self.mod_folder, "music")
        if os.path.isdir(music_root):
            groups = find_music_groups(music_root)
            if groups:
                _label, asset_name, songs_name = groups[0]
                return MusicFolder(music_root,
                                   asset_name or ASSET_FILENAME,
                                   songs_name or SONGS_FILENAME)
        return standard

    def replace_icon(self, src_path: Optional[str] = None) -> str:
        """用图片生成电台图标；src_path 为 None 时生成占位图。返回图标路径。"""
        if src_path:
            make_album_art_from_image(src_path, self.album_art_path)
        else:
            make_default_album_art(self.album_art_path)
        return self.album_art_path

    # -- 开局音效（start_game） ---------------------------------------------

    @property
    def start_game_wav_path(self) -> str:
        """开局音效安装位置：<mod>/sound/menu/start_game_01.wav（点击开始游戏）"""
        return os.path.join(self.mod_folder, "sound", START_GAME_WAV_DIR,
                            f"{START_GAME_NAME}.wav")

    @property
    def start_game_wav_02_path(self) -> str:
        """开局音效安装位置：<mod>/sound/menu/start_game_02.wav（进入游戏世界）"""
        return os.path.join(self.mod_folder, "sound", START_GAME_WAV_DIR,
                            f"{START_GAME_NAME_2}.wav")

    @property
    def start_game_asset_path(self) -> str:
        """开局音效追加定义文件（自定义名 zz_<id>_sounds.asset）。
        注意：绝不能叫 sound.asset —— 游戏对该文件是整文件替换语义，会顶掉原版全部音效。"""
        return os.path.join(self.mod_folder, "sound",
                            f"zz_{self.radio_id}_sounds.asset")

    @property
    def legacy_sound_asset_path(self) -> str:
        """历史版本误用的 sound/sound.asset（会导致原版音效全灭，需迁移/删除）。"""
        return os.path.join(self.mod_folder, "sound", "sound.asset")

    def install_start_game(self, wav_path: str, volume: float = 0.1) -> str:
        """把开局音效安装进电台 mod（追加定义，覆盖 start_game_01 与 start_game_02，本体不动）。
        同一段裁剪音频复制为 01（点击开始）与 02（进入游戏世界）两个 wav，
        定义写入 zz_<id>_sounds.asset（自定义名=追加语义，不顶原版 sound.asset）。
        返回安装后的 01 wav 路径。"""
        os.makedirs(os.path.dirname(self.start_game_wav_path), exist_ok=True)
        shutil.copy2(wav_path, self.start_game_wav_path)
        shutil.copy2(wav_path, self.start_game_wav_02_path)
        if os.path.exists(self.start_game_asset_path):
            shutil.copy2(self.start_game_asset_path,
                         self.start_game_asset_path + ".bak")
        blocks = []
        for name in (START_GAME_NAME, START_GAME_NAME_2):
            blocks.append(
                f'sound =\n{{\n\tname = "{name}"\n'
                f'\tfile = "{START_GAME_WAV_DIR}/{name}.wav"\n'
                f'\talways_load = no\n\tvolume = {volume:g}\n}}\n')
        with open(self.start_game_asset_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(blocks))
        return self.start_game_wav_path

    def start_game_status(self) -> tuple[bool, str]:
        """返回 (是否已安装, 描述)。描述含音效时长与音量；检测到旧版误用 sound.asset 时附加警告。"""
        if not (os.path.exists(self.start_game_wav_path)
                and os.path.exists(self.start_game_wav_02_path)
                and os.path.exists(self.start_game_asset_path)):
            return False, "未安装"
        try:
            dur = wav_duration(self.start_game_wav_path)
        except OSError:
            dur = 0.0
        volume = None
        try:
            with open(self.start_game_asset_path, "r",
                      encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
            m = re.search(r'volume\s*=\s*([0-9.]+)', text)
            if m:
                volume = m.group(1)
        except OSError:
            pass
        vol_txt = f"，volume {volume}" if volume is not None else ""
        desc = f"已安装：{dur:.1f} 秒{vol_txt}"
        if os.path.exists(self.legacy_sound_asset_path):
            desc += "（警告：存在旧版 sound.asset，会顶掉原版音效，请用「修复格式」迁移）"
        return True, desc

    def remove_start_game(self) -> List[str]:
        """移除开局音效（删除追加定义、wav，以及历史误用的 sound.asset）。返回已删除项。"""
        removed: List[str] = []
        for path, label in ((self.start_game_wav_path, "sound/menu/start_game_01.wav"),
                            (self.start_game_wav_02_path, "sound/menu/start_game_02.wav"),
                            (self.start_game_asset_path,
                             f"sound/zz_{self.radio_id}_sounds.asset"),
                            (self.legacy_sound_asset_path, "sound/sound.asset（旧版）")):
            if os.path.exists(path):
                try:
                    os.remove(path)
                    removed.append(label)
                except OSError:
                    pass
        for d in (os.path.dirname(self.start_game_wav_path),
                  os.path.dirname(self.start_game_asset_path)):
            try:
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
            except OSError:
                pass
        return removed

    @classmethod
    def open(cls, mod_dir: str, radio_id: str) -> "RadioMod":
        """从 mod 目录打开已存在的电台 MOD，读取 descriptor 的显示名与版本。"""
        display_name = radio_id
        supported_version = DEFAULT_SUPPORTED_VERSION
        desc_path = os.path.join(mod_dir, radio_id, "descriptor.mod")
        if os.path.exists(desc_path):
            with open(desc_path, "r", encoding="utf-8-sig", newline="") as f:
                text = f.read()
            lines = _split_keepends(text)
            for ln in lines:
                kv = _parse_kv(ln)
                if not kv:
                    continue
                k, v = kv
                if k == "name":
                    display_name = v
                elif k == "supported_version":
                    supported_version = v
        return cls(radio_id, display_name, mod_dir, supported_version)

    # -- 排查与一键修复 ------------------------------------------------------

    @property
    def gui_path(self) -> str:
        return os.path.join(self.interface_dir, f"music_station_{self.radio_id}.gui")

    @property
    def gfx_path(self) -> str:
        return os.path.join(self.interface_dir, f"music_station_{self.radio_id}.gfx")

    def check_issues(self) -> List[str]:
        """排查电台常见问题（对应问题排查记录），返回问题列表（空 = 无问题）。"""
        issues: List[str] = []
        # 1) 电台界面：只允许原版资源
        if os.path.exists(self.gui_path):
            with open(self.gui_path, "r", encoding="utf-8-sig", errors="replace") as f:
                gui = f.read()
            if '"GFX_musicplayer_head"' in gui:
                issues.append("电台界面引用了不存在的 sprite GFX_musicplayer_head（面板会渲染失败）")
            if "VCR02_14" in gui or "VCR02_12" in gui:
                issues.append("电台界面引用了非原版字体 VCR02_14/VCR02_12（会报 No font 错误）")
        else:
            issues.append(f"缺少电台界面文件 music_station_{self.radio_id}.gui")
        # 2) supported_version 合法性（如 "1.*" 为非法，须形如 "1.19.*"）
        if not re.fullmatch(r"\d+\.\d+(\.\*)?", self.supported_version or ""):
            issues.append(f"supported_version 格式非法：{self.supported_version!r}（应如 \"1.19.*\"）")
        # 3) 图标尺寸（标准 304x120 横向两帧）
        if os.path.exists(self.album_art_path):
            from PIL import Image
            try:
                with Image.open(self.album_art_path) as im:
                    if im.size != (ALBUM_ART_FRAME_W * 2, ALBUM_ART_FRAME_H):
                        issues.append(f"电台图标尺寸异常 {im.size}（应为 304x120 横向两帧）")
            except Exception:
                issues.append("电台图标无法读取（可能损坏）")
        else:
            issues.append(f"缺少电台图标 gfx/{self.radio_id}_album_art.png")
        # 4) 元数据 UTF-8 BOM（仅当显示名含中文等非 ASCII 时才可能乱码）
        for path, label in ((self.reg_file, "注册文件"), (self.descriptor_path, "descriptor.mod")):
            if os.path.exists(path):
                with open(path, "rb") as f:
                    head = f.read(3)
                has_bom = head.startswith(b"\xef\xbb\xbf")
                if not has_bom:
                    # 读取显示名判断是否含非 ASCII（纯 ASCII 名无 BOM 也不会乱码）
                    non_ascii_name = False
                    try:
                        with open(path, "r", encoding="utf-8-sig",
                                  errors="replace") as f:
                            text = f.read()
                        m = re.search(r'name\s*=\s*"([^"]*)"', text)
                        nm = m.group(1) if m else ""
                        non_ascii_name = any(ord(c) > 127 for c in nm)
                    except OSError:
                        non_ascii_name = True
                    if non_ascii_name:
                        issues.append(
                            f"{label} 缺少 UTF-8 BOM（中文电台名会显示为 ??????；"
                            "Paradox 启动器每次启动游戏会重写 .mod 导致 BOM 丢失，"
                            "程序打开电台时会自动补回，或将显示名改为英文/拼音）")
            else:
                issues.append(f"缺少 {label}")
        # 5) 开局音效：误用 sound/sound.asset 会整文件替换顶掉原版全部音效
        if os.path.exists(self.legacy_sound_asset_path):
            issues.append("mod/sound/sound.asset 存在——游戏按整文件替换处理，会顶掉原版全部音效；"
                          "应改用追加定义文件 zz_<id>_sounds.asset")
        return issues

    def repair(self) -> List[str]:
        """一键修复可自动修复的问题。返回修复动作列表（原文件自动备份 .bak）。"""
        fixed: List[str] = []
        # 1) 电台界面重建为原版资源模板
        if os.path.exists(self.gui_path):
            with open(self.gui_path, "r", encoding="utf-8-sig", errors="replace") as f:
                gui = f.read()
            if '"GFX_musicplayer_head"' in gui or "VCR02_14" in gui or "VCR02_12" in gui:
                shutil.copy2(self.gui_path, self.gui_path + ".bak")
                with open(self.gui_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(_gui_template(self.radio_id))
                fixed.append("电台界面重建为原版资源模板（原文件备份 .bak）")
        else:
            with open(self.gui_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(_gui_template(self.radio_id))
            fixed.append("补生成电台界面文件")
        # 2/4) supported_version + UTF-8 BOM：需要时统一重写元数据（缺失文件也补生成）
        need_rewrite = False
        if not re.fullmatch(r"\d+\.\d+(\.\*)?", self.supported_version or ""):
            self.supported_version = DEFAULT_SUPPORTED_VERSION
            fixed.append(f"supported_version 修正为 {DEFAULT_SUPPORTED_VERSION}")
            need_rewrite = True
        for path, writer, label in ((self.reg_file, self.write_reg_file, "注册文件"),
                                    (self.descriptor_path, self.write_descriptor, "descriptor.mod")):
            if os.path.exists(path):
                with open(path, "rb") as f:
                    has_bom = f.read(3).startswith(b"\xef\xbb\xbf")
                if need_rewrite or not has_bom:
                    writer()
                    fixed.append(f"{label} 已重写（UTF-8 BOM）" if not has_bom
                                 else f"{label} 已按新版本重写")
            else:
                # 文件缺失：修复时一并补生成（避免“缺少注册文件/descriptor”反复出现）
                writer()
                fixed.append(f"补生成缺失的{label}")
        # 3) 图标重建为 304x120 横向两帧
        need_icon = not os.path.exists(self.album_art_path)
        if not need_icon:
            from PIL import Image
            try:
                with Image.open(self.album_art_path) as im:
                    need_icon = im.size != (ALBUM_ART_FRAME_W * 2, ALBUM_ART_FRAME_H)
            except Exception:
                need_icon = True
        if need_icon:
            if os.path.exists(self.album_art_path):
                shutil.copy2(self.album_art_path, self.album_art_path + ".bak")
            make_default_album_art(self.album_art_path)
            fixed.append("电台图标重建为 304x120 横向两帧（原文件备份 .bak）")
        # 5) 迁移误用的 sound.asset → 追加定义文件名（避免顶掉原版音效）
        if os.path.exists(self.legacy_sound_asset_path):
            shutil.copy2(self.legacy_sound_asset_path,
                         self.legacy_sound_asset_path + ".bak")
            if not os.path.exists(self.start_game_asset_path):
                os.replace(self.legacy_sound_asset_path, self.start_game_asset_path)
                fixed.append("sound.asset 已更名为 zz_<id>_sounds.asset（原文件备份 .bak）")
            else:
                os.remove(self.legacy_sound_asset_path)
                fixed.append("已删除误用的 sound.asset（原文件备份 .bak）")
        return fixed


def find_radio_mods(mod_dir: str):
    """扫描 mod 目录，返回已有本地电台 MOD 列表 [(radio_id, display_name, folder)]。
    只识别文件夹型本地 mod（.mod 文件含 path 且对应文件夹存在）。"""
    results = []
    try:
        names = os.listdir(mod_dir)
    except OSError:
        return results
    for f in sorted(names):
        if not f.lower().endswith(".mod"):
            continue
        radio_id = f[:-4]
        path = os.path.join(mod_dir, f)
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as fp:
                text = fp.read()
        except OSError:
            continue
        m = re.search(r'path\s*=\s*"([^"]+)"', text)
        if not m:
            continue
        folder_name = m.group(1).replace("\\", "/").rstrip("/")
        folder = os.path.join(mod_dir, os.path.basename(folder_name))
        if not os.path.isdir(folder):
            continue
        display = radio_id
        m2 = re.search(r'name\s*=\s*"([^"]*)"', text)
        if m2 and m2.group(1):
            display = m2.group(1)
        results.append((radio_id, display, folder))
    return results


# ---------------------------------------------------------------------------
# 电台索引（记录所有创建/打开的本地电台及其目录，支持任意目录创建的电台被找到）
# ---------------------------------------------------------------------------

RADIO_INDEX_FILE = "radio_index.json"


def radio_index_path() -> str:
    """索引文件位置：%APPDATA%\\HoI4MusicEditor\\radio_index.json"""
    base = os.environ.get("APPDATA", os.path.expanduser("~"))
    return os.path.join(base, "HoI4MusicEditor", RADIO_INDEX_FILE)


def load_radio_index() -> List[dict]:
    """读取电台索引：[{radio_id, display_name, mod_dir, ts}]；损坏或不存在返回 []。"""
    try:
        with open(radio_index_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_radio_index(entries: List[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(radio_index_path()), exist_ok=True)
        with open(radio_index_path(), "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _norm(p: str) -> str:
    return os.path.normpath(p or "").lower()


def index_add_radio(radio_id: str, display_name: str, mod_dir: str) -> None:
    """登记电台到索引（同 radio_id+目录 则更新显示名）。创建/打开电台后调用。"""
    entries = load_radio_index()
    for e in entries:
        if e.get("radio_id") == radio_id and _norm(e.get("mod_dir")) == _norm(mod_dir):
            e["display_name"] = display_name
            e["ts"] = time.time()
            save_radio_index(entries)
            return
    entries.append({"radio_id": radio_id, "display_name": display_name,
                    "mod_dir": mod_dir, "ts": time.time()})
    save_radio_index(entries)


def index_remove_radio(radio_id: str, mod_dir: str) -> None:
    """从索引移除指定电台（不删除电台文件）。"""
    entries = [e for e in load_radio_index()
               if not (e.get("radio_id") == radio_id
                       and _norm(e.get("mod_dir")) == _norm(mod_dir))]
    save_radio_index(entries)


def index_update_radio(radio_id: str, old_mod_dir: str, new_mod_dir: str) -> None:
    """更新索引中电台的目录位置（电台文件夹移动后修正）。"""
    entries = load_radio_index()
    for e in entries:
        if e.get("radio_id") == radio_id and _norm(e.get("mod_dir")) == _norm(old_mod_dir):
            e["mod_dir"] = new_mod_dir
            e["ts"] = time.time()
    save_radio_index(entries)
