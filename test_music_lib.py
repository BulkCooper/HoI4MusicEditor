# -*- coding: utf-8 -*-
"""
test_music_lib.py — music_lib 单元测试
覆盖：解析、序列化往返、增删改、唯一命名、保存与校验。
使用 tempfile 副本，不污染原 music 文件夹。
"""

import os
import shutil
import tempfile
import unittest
import wave

import music_lib
import app  # 顶层不启动 GUI，仅验证可导入
from music_lib import MusicFolder

ASSET_SAMPLE = """\
# MAIN THEME
music = {
\tname = "maintheme"
\tfile = "hoi4mainthemeallies.ogg"
\tvolume = 0.65
}

# General Peace:
music = {
\tname = "general_peace_1"
\tfile = "general_peace_heartsofmen.ogg"
\tvolume = 0.65
}
"""

SONGS_SAMPLE = """\
music_station = "base_music"

music = {
\tsong = "maintheme"
\tchance = {
\t\tmodifier = {
\t\t\tfactor = 0.5
\t\t}\t\t
\t}
}

# PEACE SONGS ##################
music = {
\tsong = "general_peace_1"
\tchance = {
\t\tmodifier = {
\t\t\tfactor = 1
\t\t\thas_war = no
\t\t}\t\t
\t}\t
}
"""


class TestParse(unittest.TestCase):
    def test_parse_asset(self):
        outer, entries = music_lib.parse_asset(ASSET_SAMPLE)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].name, "maintheme")
        self.assertEqual(entries[0].file, "hoi4mainthemeallies.ogg")
        self.assertAlmostEqual(entries[0].volume, 0.65)
        # 注释保留在块外
        self.assertTrue(any("# MAIN THEME" in l for l in outer))

    def test_parse_songs(self):
        outer, rules = music_lib.parse_songs(SONGS_SAMPLE)
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0].song, "maintheme")
        self.assertAlmostEqual(rules[0].factor, 0.5)
        self.assertEqual(rules[0].conditions, [])
        self.assertEqual(rules[1].conditions, ["has_war = no"])
        # 顶层键保留
        self.assertTrue(any("music_station" in l for l in outer))

    def test_parse_real_files(self):
        """以真实 music/ 文件为基准（存在时）。"""
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music")
        if not os.path.exists(base):
            self.skipTest("music 文件夹不存在")
        with open(os.path.join(base, "music.asset"), encoding="utf-8-sig", newline="") as f:
            text = f.read()
        outer, entries = music_lib.parse_asset(text)
        self.assertEqual(len(entries), 26)
        names = [e.name for e in entries]
        self.assertEqual(names[0], "maintheme")
        # 非 ASCII 文件名
        fr = [e for e in entries if e.name == "resistance_french"]
        self.assertEqual(fr[0].file, "La_Liberté.ogg")

        with open(os.path.join(base, "_songs.txt"), encoding="utf-8-sig", newline="") as f:
            text = f.read()
        outer, rules = music_lib.parse_songs(text)
        self.assertEqual(len(rules), 22)
        self.assertEqual(rules[0].song, "maintheme")
        self.assertAlmostEqual(rules[0].factor, 0.5)


class TestPresets(unittest.TestCase):
    def test_conditions_from_preset(self):
        self.assertEqual(music_lib.conditions_from_preset("任意", "通用"), [])
        self.assertEqual(music_lib.conditions_from_preset("和平", "通用"),
                         ["has_war = no"])
        self.assertEqual(music_lib.conditions_from_preset("战争", "同盟国"),
                         ["has_war = yes", "has_government = democratic"])
        self.assertEqual(music_lib.conditions_from_preset("任意", "轴心国"),
                         ["has_government = fascism"])

    def test_preset_from_conditions(self):
        self.assertEqual(music_lib.preset_from_conditions(["has_war = no"]), ("和平", "通用"))
        self.assertEqual(music_lib.preset_from_conditions(
            ["has_war = yes", "has_government = communism"]), ("战争", "共产国际"))
        # 无法识别的条件 → 默认预设
        self.assertEqual(music_lib.preset_from_conditions(
            ["has_war = no", "has_government = democratic", "any_thing = 1"]),
            ("和平", "同盟国"))

    def test_summarize(self):
        self.assertEqual(music_lib.summarize_conditions([]), "任意播放")
        self.assertEqual(music_lib.summarize_conditions(["has_war = no"]), "和平")
        self.assertEqual(music_lib.summarize_conditions(
            ["has_war = yes", "has_government = fascism"]), "战争 · 轴心国")
        self.assertIn("any_thing = 1", music_lib.summarize_conditions(
            ["any_thing = 1", "has_war = no"]))


class TestConfig(unittest.TestCase):
    """配置持久化：读写往返与损坏容错（隔离到临时目录）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="config_test_")
        self._orig_dir, self._orig_path = app.CONFIG_DIR, app.CONFIG_PATH
        app.CONFIG_DIR = self.tmp
        app.CONFIG_PATH = os.path.join(self.tmp, "config.json")

    def tearDown(self):
        app.CONFIG_DIR, app.CONFIG_PATH = self._orig_dir, self._orig_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip(self):
        app.save_config({"default_volume": 0.5, "last_folder": "C:\\x",
                         "sort_key": "volume", "sort_rev": True})
        cfg = app.load_config()
        self.assertAlmostEqual(cfg["default_volume"], 0.5)
        self.assertEqual(cfg["last_folder"], "C:\\x")
        self.assertEqual(cfg["sort_key"], "volume")
        self.assertTrue(cfg["sort_rev"])

    def test_missing_file(self):
        self.assertEqual(app.load_config(), {})

    def test_corrupt_file(self):
        with open(app.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write("{ 这不是合法 JSON")
        self.assertEqual(app.load_config(), {})

    def test_non_dict(self):
        with open(app.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        self.assertEqual(app.load_config(), {})


class TestConvert(unittest.TestCase):
    """音频转换：wav→ogg 全链路与失败处理。"""

    def test_convert_wav_to_ogg(self):
        d = tempfile.mkdtemp(prefix="conv_test_")
        wav_path = os.path.join(d, "t.wav")
        ogg_path = os.path.join(d, "t.ogg")
        sr = 44100
        t = [i / sr for i in range(sr)]  # 1 秒
        frames = b"".join(int(0.3 * 32767 * __import__("math").sin(2 * 3.14159 * 440 * x))
                          .to_bytes(2, "little", signed=True) for x in t)
        with wave.open(wav_path, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(frames)
        try:
            music_lib.convert_to_ogg(wav_path, ogg_path)
            self.assertTrue(os.path.exists(ogg_path))
            import soundfile as sf
            y, sr2 = sf.read(ogg_path)
            self.assertAlmostEqual(sr2, sr)
            self.assertGreater(len(y), sr - 100)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_convert_missing_source(self):
        d = tempfile.mkdtemp(prefix="conv_test_")
        try:
            with self.assertRaises(OSError):
                music_lib.convert_to_ogg(os.path.join(d, "no.mp3"),
                                         os.path.join(d, "out.ogg"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestPendingCommit(unittest.TestCase):
    """转换缓存文件提交（保存时复制进目标文件夹）。"""

    def test_commit_pending(self):
        d = tempfile.mkdtemp(prefix="pending_test_")
        cache = os.path.join(d, "cache")
        target = os.path.join(d, "music")
        os.makedirs(cache)
        os.makedirs(target)
        src = os.path.join(cache, "song.ogg")
        with open(src, "wb") as f:
            f.write(b"OggS-test")
        pending = {"song.ogg": src}
        committed = app.commit_pending_files(target, pending)
        self.assertEqual(committed, {"song.ogg": "song.ogg"})
        self.assertTrue(os.path.exists(os.path.join(target, "song.ogg")))

    def test_commit_rename_on_conflict(self):
        d = tempfile.mkdtemp(prefix="pending_test_")
        cache = os.path.join(d, "cache")
        target = os.path.join(d, "music")
        os.makedirs(cache)
        os.makedirs(target)
        with open(os.path.join(target, "song.ogg"), "wb") as f:
            f.write(b"existing")
        src = os.path.join(cache, "song.ogg")
        with open(src, "wb") as f:
            f.write(b"OggS-test")
        committed = app.commit_pending_files(target, {"song.ogg": src})
        self.assertEqual(committed, {"song.ogg": "song_2.ogg"})
        self.assertTrue(os.path.exists(os.path.join(target, "song_2.ogg")))

    def test_commit_skip_missing_source(self):
        d = tempfile.mkdtemp(prefix="pending_test_")
        target = os.path.join(d, "music")
        os.makedirs(target)
        committed = app.commit_pending_files(target, {"gone.ogg": os.path.join(d, "gone.ogg")})
        self.assertEqual(committed, {})


class TestMusicGroups(unittest.TestCase):
    """mod 自定义文件名的音乐文件组识别与读写。"""

    def _mk(self):
        d = tempfile.mkdtemp(prefix="groups_test_")
        return d

    def test_find_default_and_mod_groups(self):
        d = self._mk()
        for f in ("music.asset", "_songs.txt",
                  "mymod_music.asset", "mymod_songs.txt",
                  "other_music.asset"):
            with open(os.path.join(d, f), "w", encoding="utf-8") as fh:
                fh.write("")
        groups = music_lib.find_music_groups(d)
        labels = [g[0] for g in groups]
        self.assertIn("(默认)", labels)
        self.assertIn("mymod", labels)
        self.assertIn("other", labels)
        by_label = {g[0]: g for g in groups}
        self.assertEqual(by_label["(默认)"][1], "music.asset")
        self.assertEqual(by_label["(默认)"][2], "_songs.txt")
        self.assertEqual(by_label["mymod"][1], "mymod_music.asset")
        self.assertEqual(by_label["mymod"][2], "mymod_songs.txt")
        self.assertIsNone(by_label["other"][2])  # 只有 asset
        shutil.rmtree(d, ignore_errors=True)

    def test_mod_group_roundtrip(self):
        d = self._mk()
        with open(os.path.join(d, "song1.ogg"), "wb") as f:
            f.write(b"OggS")
        mf = MusicFolder(d, "mymod_music.asset", "mymod_songs.txt")
        mf.load()
        mf.add_entry("my_song", "song1.ogg", 0.7, 1.0, ["has_war = no"])
        mf.save()
        # 默认文件不应被创建
        self.assertFalse(os.path.exists(os.path.join(d, "music.asset")))
        self.assertFalse(os.path.exists(os.path.join(d, "_songs.txt")))
        # mod 文件已写回
        mf2 = MusicFolder(d, "mymod_music.asset", "mymod_songs.txt")
        mf2.load()
        self.assertEqual(mf2.entries[0].name, "my_song")
        self.assertAlmostEqual(mf2.entries[0].volume, 0.7)
        self.assertEqual(mf2.rules[0].conditions, ["has_war = no"])
        shutil.rmtree(d, ignore_errors=True)


class TestDndPaths(unittest.TestCase):
    """拖放事件路径解析。"""

    def test_parse_dnd_paths(self):
        from app import App
        d = tempfile.mkdtemp(prefix="dnd test ")
        f1 = os.path.join(d, "a.ogg")
        with open(f1, "w") as f:
            f.write("x")
        real = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                            "System32", "notepad.exe")
        data = "{%s} %s %s" % (f1, real, os.path.join(d, "missing.ogg"))
        got = App._parse_dnd_paths(data)
        self.assertIn(f1, got)   # 花括号内带空格路径
        self.assertIn(real, got)  # 裸路径（无空格）
        self.assertEqual(len(got), 2)  # 不存在的文件被过滤
        shutil.rmtree(d, ignore_errors=True)


class TestPlaFormat(unittest.TestCase):
    """pla 标准格式：生成格式断言与往返一致性。"""

    def _pla_path(self, name):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)

    def test_asset_entry_blank_line_between_blocks(self):
        lines = music_lib._fmt_asset_entry("pla1", "pla1.ogg", 0.65)
        text = "".join(lines)
        self.assertIn('music = {\n\tname = "pla1"\n\tfile = "pla1.ogg"\n'
                      '\tvolume = 0.65\n}\n\n', text)

    def test_song_rule_blank_line_and_trailing_tab(self):
        lines = music_lib._fmt_song_rule("pla1", 0.5, [])
        text = "".join(lines)
        self.assertIn('\t\t}\t\t\n', text)  # modifier 闭合行带尾随 tab
        self.assertTrue(text.endswith("}\n\n"))  # 块后空行

    def test_pla_files_roundtrip(self):
        """用户提供的 pla 文件：解析→标准格式写回→再解析数据一致。"""
        d = tempfile.mkdtemp(prefix="pla_test_")
        shutil.copy2(self._pla_path("pla_music.asset"), os.path.join(d, "music.asset"))
        shutil.copy2(self._pla_path("pla_songs.txt"), os.path.join(d, "_songs.txt"))
        mf = MusicFolder(d)
        mf.load()
        self.assertEqual(len(mf.entries), 19)
        self.assertEqual(len(mf.rules), 19)
        self.assertAlmostEqual(mf.rules[0].factor, 0.5)
        mf.save()
        mf2 = MusicFolder(d)
        mf2.load()
        self.assertEqual([(e.name, e.file) for e in mf.entries],
                         [(e.name, e.file) for e in mf2.entries])
        self.assertEqual([(r.song, r.factor) for r in mf.rules],
                         [(r.song, r.factor) for r in mf2.rules])
        # 写回后文件应为标准格式（块间空行）
        with open(os.path.join(d, "music.asset"), encoding="utf-8-sig", newline="") as f:
            text = f.read()
        self.assertIn('}\n\nmusic = {\n', text)
        shutil.rmtree(d, ignore_errors=True)


class TestRoundTrip(unittest.TestCase):
    def test_real_folder_roundtrip(self):
        """以真实 music/ 文件夹副本为基准：load→save→reload 数据一致、校验通过。"""
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music")
        if not os.path.exists(base):
            self.skipTest("music 文件夹不存在")
        d = tempfile.mkdtemp(prefix="musiclib_real_")
        for fn in (music_lib.ASSET_FILENAME, music_lib.SONGS_FILENAME):
            src = os.path.join(base, fn)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(d, fn))
        mf = MusicFolder(d)
        mf.load()
        mf.save()
        problems = [p for p in mf.validate() if "引用的文件不存在" not in p]
        self.assertEqual(problems, [])
        mf2 = MusicFolder(d)
        mf2.load()
        self.assertEqual([(e.name, e.file, e.volume) for e in mf.entries],
                         [(e.name, e.file, e.volume) for e in mf2.entries])
        self.assertEqual([(r.song, r.factor, r.conditions) for r in mf.rules],
                         [(r.song, r.factor, r.conditions) for r in mf2.rules])
        shutil.rmtree(d, ignore_errors=True)

    def _make_folder(self):
        d = tempfile.mkdtemp(prefix="musiclib_test_")
        with open(os.path.join(d, music_lib.ASSET_FILENAME), "w", encoding="utf-8", newline="") as f:
            f.write(ASSET_SAMPLE)
        with open(os.path.join(d, music_lib.SONGS_FILENAME), "w", encoding="utf-8", newline="") as f:
            f.write(SONGS_SAMPLE)
        return d

    def test_roundtrip_unchanged(self):
        """未修改时 save 后重新加载，数据一致；注释仍保留。"""
        d = self._make_folder()
        mf = MusicFolder(d)
        mf.load()
        mf.save()
        with open(os.path.join(d, music_lib.ASSET_FILENAME), encoding="utf-8-sig", newline="") as f:
            text = f.read()
        self.assertIn("# MAIN THEME", text)
        outer, entries = music_lib.parse_asset(text)
        self.assertEqual(len(entries), 2)
        with open(os.path.join(d, music_lib.SONGS_FILENAME), encoding="utf-8-sig", newline="") as f:
            text = f.read()
        self.assertIn("music_station = \"base_music\"", text)
        outer, rules = music_lib.parse_songs(text)
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[1].conditions, ["has_war = no"])
        shutil.rmtree(d, ignore_errors=True)

    def test_backup_created(self):
        d = self._make_folder()
        mf = MusicFolder(d)
        mf.load()
        mf.save(backup=True)
        self.assertTrue(os.path.exists(os.path.join(d, music_lib.ASSET_FILENAME + ".bak")))
        shutil.rmtree(d, ignore_errors=True)


class TestMutations(unittest.TestCase):
    def _make_folder(self):
        d = tempfile.mkdtemp(prefix="musiclib_test_")
        # 放置一个真实 ogg 占位文件
        with open(os.path.join(d, "song1.ogg"), "wb") as f:
            f.write(b"OggS")
        return d

    def test_add_entry_and_save(self):
        d = self._make_folder()
        mf = MusicFolder(d)
        mf.load()  # 空文档
        mf.add_entry("my_song", "song1.ogg", 0.7, 2.0, ["has_war = no", "has_government = democratic"])
        self.assertEqual(mf.entry_by_name("my_song").file, "song1.ogg")
        mf.save()
        # 重新加载验证
        mf2 = MusicFolder(d)
        mf2.load()
        self.assertEqual(len(mf2.entries), 1)
        self.assertAlmostEqual(mf2.entries[0].volume, 0.7)
        self.assertEqual(mf2.rules[0].song, "my_song")
        self.assertAlmostEqual(mf2.rules[0].factor, 2.0)
        self.assertEqual(mf2.rules[0].conditions, ["has_war = no", "has_government = democratic"])
        self.assertEqual(mf2.validate(), [])
        shutil.rmtree(d, ignore_errors=True)

    def test_edit_entry(self):
        d = self._make_folder()
        mf = MusicFolder(d)
        mf.load()
        mf.add_entry("my_song", "song1.ogg", 0.7, 1.0, ["has_war = no"])
        mf.update_entry("my_song", new_volume=1.2, new_conditions=["has_war = yes"], new_factor=3.0)
        mf.save()
        mf2 = MusicFolder(d)
        mf2.load()
        e = mf2.entry_by_name("my_song")
        self.assertAlmostEqual(e.volume, 1.2)
        r = mf2.rules_for_song("my_song")[0]
        self.assertAlmostEqual(r.factor, 3.0)
        self.assertEqual(r.conditions, ["has_war = yes"])
        shutil.rmtree(d, ignore_errors=True)

    def test_rename_entry(self):
        d = self._make_folder()
        mf = MusicFolder(d)
        mf.load()
        mf.add_entry("old_name", "song1.ogg", 0.7, 1.0, ["has_war = no"])
        mf.update_entry("old_name", new_name="new_name")
        mf.save()
        mf2 = MusicFolder(d)
        mf2.load()
        self.assertIsNone(mf2.entry_by_name("old_name"))
        self.assertIsNotNone(mf2.entry_by_name("new_name"))
        self.assertEqual(mf2.rules_for_song("new_name")[0].song, "new_name")
        shutil.rmtree(d, ignore_errors=True)

    def test_remove_entry(self):
        d = self._make_folder()
        mf = MusicFolder(d)
        mf.load()
        mf.add_entry("my_song", "song1.ogg", 0.7, 1.0, [])
        mf.remove_entry("my_song")
        self.assertEqual(mf.entries, [])
        self.assertEqual(mf.rules, [])
        shutil.rmtree(d, ignore_errors=True)

    def test_unique_names(self):
        d = self._make_folder()
        mf = MusicFolder(d)
        mf.load()
        mf.add_entry("my_song", "song1.ogg", 0.7, 1.0, [])
        self.assertEqual(mf.unique_song_name("My Song!"), "my_song_2")
        self.assertEqual(mf.unique_filename("song1.ogg"), "song1_2.ogg")
        self.assertEqual(mf.unique_filename("other.ogg"), "other.ogg")
        shutil.rmtree(d, ignore_errors=True)

    def test_unique_names_chinese(self):
        """中文文件名 → song name 保留中文原文（适配文件中文名）。"""
        d = self._make_folder()
        mf = MusicFolder(d)
        mf.load()
        self.assertEqual(mf.unique_song_name("边境1"), "边境1")
        self.assertEqual(mf.unique_song_name("边境 1"), "边境_1")
        self.assertEqual(mf.unique_song_name("La Liberté"), "la_liberté")
        self.assertEqual(mf.unique_song_name("戦場の歌"), "戦場の歌")
        shutil.rmtree(d, ignore_errors=True)

    def test_chinese_song_roundtrip(self):
        """中文 song name 添加→保存→重载 数据一致。"""
        d = tempfile.mkdtemp(prefix="zh_test_")
        root = os.path.dirname(os.path.abspath(__file__))
        shutil.copy2(os.path.join(root, "pla_music.asset"), os.path.join(d, "music.asset"))
        shutil.copy2(os.path.join(root, "pla_songs.txt"), os.path.join(d, "_songs.txt"))
        mf = MusicFolder(d)
        mf.load()
        mf.add_entry("边境1", "边境1.ogg", 0.65, 0.5, ["has_war = no"])
        mf.save()
        mf2 = MusicFolder(d)
        mf2.load()
        self.assertIsNotNone(mf2.entry_by_name("边境1"))
        self.assertEqual(mf2.entry_by_name("边境1").file, "边境1.ogg")
        rules = mf2.rules_for_song("边境1")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].conditions, ["has_war = no"])
        shutil.rmtree(d, ignore_errors=True)

    def test_validate_missing_file(self):
        d = self._make_folder()
        mf = MusicFolder(d)
        mf.load()
        mf.add_entry("bad", "missing.ogg", 0.7, 1.0, [])
        problems = mf.validate()
        self.assertTrue(any("missing.ogg" in p for p in problems))
        shutil.rmtree(d, ignore_errors=True)

    def test_utf8_filename_roundtrip(self):
        d = self._make_folder()
        with open(os.path.join(d, "La_Liberté.ogg"), "wb") as f:
            f.write(b"OggS")
        mf = MusicFolder(d)
        mf.load()
        mf.add_entry("french", "La_Liberté.ogg", 0.65, 1.0, [])
        mf.save()
        mf2 = MusicFolder(d)
        mf2.load()
        self.assertEqual(mf2.entries[0].file, "La_Liberté.ogg")
        self.assertEqual(mf2.validate(), [])
        shutil.rmtree(d, ignore_errors=True)


class TestRadioMod(unittest.TestCase):
    """独立本地电台 MOD：结构生成、图标、元数据、扫描。"""

    def _make_rm(self):
        d = tempfile.mkdtemp(prefix="rm_test_")
        mod_dir = os.path.join(d, "mod")
        rm = music_lib.RadioMod("radio_my", "我的电台", mod_dir)
        rm.create()
        return d, mod_dir, rm

    def test_invalid_id(self):
        for bad in ("my radio", "中文", "1abc", "-x", "a" * 40, ""):
            with self.assertRaises(ValueError, msg=bad):
                music_lib.RadioMod(bad, "x", "d")
        ok = music_lib.RadioMod("Radio_2", "x", "d")
        self.assertEqual(ok.radio_id, "Radio_2")

    def test_create_structure(self):
        d, mod_dir, rm = self._make_rm()
        try:
            expected = [
                os.path.join(mod_dir, "radio_my.mod"),
                os.path.join(mod_dir, "radio_my", "descriptor.mod"),
                os.path.join(mod_dir, "radio_my", "gfx", "radio_my_album_art.png"),
                os.path.join(mod_dir, "radio_my", "interface", "music_station_radio_my.gfx"),
                os.path.join(mod_dir, "radio_my", "interface", "music_station_radio_my.gui"),
                os.path.join(mod_dir, "radio_my", "music", "radio_my", "radio_my_music.asset"),
                os.path.join(mod_dir, "radio_my", "music", "radio_my", "radio_my_songs.txt"),
            ]
            for p in expected:
                self.assertTrue(os.path.exists(p), p)
            # 注册文件与 descriptor 内容一致且含 path
            with open(os.path.join(mod_dir, "radio_my.mod"), encoding="utf-8") as f:
                reg = f.read()
            with open(os.path.join(mod_dir, "radio_my", "descriptor.mod"), encoding="utf-8") as f:
                desc = f.read()
            self.assertEqual(reg, desc)
            self.assertIn('name="我的电台"', reg)
            self.assertIn('path="mod/radio_my"', reg)
            self.assertIn('supported_version="1.19.*"', reg)
            # 模板引用一致
            with open(os.path.join(mod_dir, "radio_my", "interface",
                                    "music_station_radio_my.gfx"), encoding="utf-8") as f:
                gfx = f.read()
            with open(os.path.join(mod_dir, "radio_my", "interface",
                                    "music_station_radio_my.gui"), encoding="utf-8") as f:
                gui = f.read()
            self.assertIn('name = "GFX_radio_my_album_art"', gfx)
            self.assertIn('quadTextureSprite = "GFX_radio_my_album_art"', gui)
            self.assertIn('name = "radio_my_faceplate"', gui)
            self.assertIn('name = "radio_my_stations_entry"', gui)
            # 只引用游戏原版资源：不得含自定义 sprite/字体（注意精确匹配，避免命中 header_bg 前缀）
            self.assertNotIn('"GFX_musicplayer_head"', gui)
            self.assertNotIn('VCR02', gui)
            self.assertIn('font = "hoi_20b"', gui)
            self.assertIn('font = "hoi_18b"', gui)
            # 初始音乐文档含电台声明、无歌曲
            with open(os.path.join(mod_dir, "radio_my", "music", "radio_my",
                                      "radio_my_songs.txt"), encoding="utf-8") as f:
                songs = f.read()
            self.assertIn('music_station = "radio_my"', songs)
            mf = rm.music_folder()
            mf.load()
            self.assertEqual(len(mf.entries), 0)
            self.assertEqual(len(mf.rules), 0)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_album_art_two_frames(self):
        from PIL import Image
        d, mod_dir, rm = self._make_rm()
        try:
            img = Image.open(rm.album_art_path)
            # 权威尺寸：304x120 横向两帧（每帧 152x120）
            self.assertEqual(img.size, (304, 120))
            f1 = img.crop((0, 0, 152, 120))
            f2 = img.crop((152, 0, 304, 120))
            b1 = sum(f1.tobytes())
            b2 = sum(f2.tobytes())
            self.assertGreater(b2, b1, "选中帧（右）应比未选中帧（左）亮")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_replace_icon_from_jpg(self):
        from PIL import Image
        d, mod_dir, rm = self._make_rm()
        try:
            src = os.path.join(d, "src.jpg")
            Image.new("RGB", (400, 300), (200, 30, 30)).save(src, "JPEG")
            rm.replace_icon(src)
            img = Image.open(rm.album_art_path)
            self.assertEqual(img.size, (304, 120))
            # 左帧内像素偏红（JPEG 有损，用偏红判断）
            r, g, b = img.getpixel((60, 60))
            self.assertGreater(r - max(g, b), 100)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_descriptor_utf8_bom(self):
        """descriptor.mod / 注册 .mod 必须带 UTF-8 BOM（否则启动器把中文名显示为 ??????）。"""
        d, mod_dir, rm = self._make_rm()
        try:
            with open(os.path.join(mod_dir, "radio_my.mod"), "rb") as f:
                reg = f.read()
            with open(os.path.join(mod_dir, "radio_my", "descriptor.mod"), "rb") as f:
                desc = f.read()
            self.assertTrue(reg.startswith(b"\xef\xbb\xbf"), "注册文件应带 UTF-8 BOM")
            self.assertTrue(desc.startswith(b"\xef\xbb\xbf"), "descriptor 应带 UTF-8 BOM")
            self.assertIn('name="我的电台"', reg.decode("utf-8-sig"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_music_roundtrip_in_mod(self):
        d, mod_dir, rm = self._make_rm()
        try:
            with open(os.path.join(rm.music_dir, "测试歌.ogg"), "wb") as f:
                f.write(b"OggS")
            mf = rm.music_folder()
            mf.load()
            mf.add_entry("测试歌", "测试歌.ogg", 0.65, 1.0, ["has_war = yes"])
            mf.save()
            mf2 = rm.music_folder()
            mf2.load()
            self.assertEqual(mf2.entries[0].name, "测试歌")
            self.assertEqual(mf2.entries[0].file, "测试歌.ogg")
            self.assertEqual(mf2.rules_for_song("测试歌")[0].conditions, ["has_war = yes"])
            self.assertEqual(mf2.validate(), [])
            # 保存后 songs 仍保留 music_station 声明
            with open(os.path.join(rm.music_dir, rm.songs_filename), encoding="utf-8") as f:
                songs = f.read()
            self.assertIn('music_station = "radio_my"', songs)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_find_radio_mods_and_open(self):
        d, mod_dir, rm = self._make_rm()
        try:
            found = music_lib.find_radio_mods(mod_dir)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0][0], "radio_my")
            self.assertEqual(found[0][1], "我的电台")
            self.assertEqual(found[0][2], os.path.join(mod_dir, "radio_my"))
            rm2 = music_lib.RadioMod.open(mod_dir, "radio_my")
            self.assertEqual(rm2.display_name, "我的电台")
            self.assertEqual(rm2.supported_version, "1.19.*")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_music_folder_fallback_to_root(self):
        """music 文件在 mod/music/ 根（无 <id> 子目录）时回退定位并读出歌曲。"""
        d, mod_dir, rm = self._make_rm()
        try:
            # 把标准 music/<id>/* 移到 music/ 根
            dst = os.path.join(rm.mod_folder, "music")
            for f in os.listdir(rm.music_dir):
                shutil.move(os.path.join(rm.music_dir, f), os.path.join(dst, f))
            shutil.rmtree(rm.music_dir, ignore_errors=True)
            # 直接写带歌曲的 asset
            with open(os.path.join(dst, "radio_my_music.asset"), "w", encoding="utf-8") as f:
                f.write('music = {\n\tname = "根目录歌"\n\tfile = "根目录歌.ogg"\n'
                        '\tvolume = 0.65\n}\n')
            with open(os.path.join(dst, "根目录歌.ogg"), "wb") as f:
                f.write(b"OggS")
            mf = rm.music_folder()
            mf.load()
            self.assertEqual(mf.asset_path, os.path.join(dst, "radio_my_music.asset"))
            self.assertEqual([e.name for e in mf.entries], ["根目录歌"])
            self.assertEqual(mf.validate(), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_check_and_repair_issues(self):
        """问题电台（旧 gui/非法版本/旧图标/无 BOM）排查出 6 项并一键修复到无问题。"""
        d, mod_dir, rm = self._make_rm()
        try:
            # 制造旧版问题
            old_gui = music_lib._gui_template("radio_my").replace(
                'font = "hoi_20b"', 'font = "VCR02_14"').replace(
                'font = "hoi_18b"', 'font = "VCR02_12"')
            old_gui = old_gui.replace(
                "guiTypes = {",
                'guiTypes = {\n\ticonType = {\n\t\tname = "musicplayer_head"\n'
                '\t\tspriteType = "GFX_musicplayer_head"\n\t\tposition = { x = 180 y = 0 }\n'
                '\t\talwaystransparent = no\n\t}\n')
            with open(rm.gui_path, "w", encoding="utf-8") as f:
                f.write(old_gui)
            rm.supported_version = "1.*"
            for path in (rm.reg_file, rm.descriptor_path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(rm.descriptor_text())
            # 制造旧版误用 sound.asset（顶掉原版音效的问题）
            os.makedirs(os.path.dirname(rm.legacy_sound_asset_path), exist_ok=True)
            with open(rm.legacy_sound_asset_path, "w", encoding="utf-8") as f:
                f.write('sound = {\n\tname = "start_game_01"\n'
                        '\tfile = "menu/start_game_01.wav"\n\tvolume = 0.1\n}\n')
            from PIL import Image
            Image.new("RGB", (162, 260), (10, 10, 10)).save(rm.album_art_path)

            issues = rm.check_issues()
            self.assertEqual(len(issues), 7, issues)
            self.assertTrue(any("sound.asset" in i for i in issues))
            fixed = rm.repair()
            self.assertGreaterEqual(len(fixed), 5, fixed)
            # sound.asset 已迁移（重命名为 zz_ 或删除）
            self.assertFalse(os.path.exists(rm.legacy_sound_asset_path))
            rm2 = music_lib.RadioMod.open(mod_dir, "radio_my")
            self.assertEqual(rm2.check_issues(), [])
            self.assertEqual(rm2.supported_version, "1.19.*")
            with open(rm2.gui_path, encoding="utf-8-sig") as f:
                gui = f.read()
            self.assertNotIn('"GFX_musicplayer_head"', gui)
            self.assertIn('font = "hoi_20b"', gui)
            for p in (rm2.reg_file, rm2.descriptor_path):
                with open(p, "rb") as f:
                    self.assertEqual(f.read(3), b"\xef\xbb\xbf")
            self.assertTrue(os.path.exists(rm2.gui_path + ".bak"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_album_art_border_on_user_replace(self):
        """更换图标（make_album_art_from_image）时：左帧红边框、右帧绿边框（宽 2px）。"""
        from PIL import Image
        d, mod_dir, rm = self._make_rm()
        try:
            src = os.path.join(d, "src.png")
            Image.new("RGB", (500, 300), (10, 120, 200)).save(src, "PNG")
            rm.replace_icon(src)
            img = Image.open(rm.album_art_path)
            self.assertEqual(img.size, (304, 120))
            # 左帧 (1,1) 红边框、(152,1) 交界、(153,1) 右帧绿边框
            self.assertEqual(img.getpixel((1, 1)), (214, 48, 49), "左帧应有红边框")
            self.assertEqual(img.getpixel((153, 1)), (46, 160, 67), "右帧应有绿边框")
            # 帧内中心仍是原图内容（蓝青色，非边框色）
            r, g, b = img.getpixel((60, 60))
            self.assertGreater(b, r)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_repair_creates_missing_metadata(self):
        """修复时补生成缺失的注册文件与 descriptor（不再反复报“缺少…”）。"""
        d, mod_dir, rm = self._make_rm()
        try:
            os.remove(rm.reg_file)
            os.remove(rm.descriptor_path)
            self.assertIn("缺少 注册文件", rm.check_issues())
            fixed = rm.repair()
            self.assertTrue(os.path.exists(rm.reg_file))
            self.assertTrue(os.path.exists(rm.descriptor_path))
            self.assertTrue(any("注册文件" in f for f in fixed))
            self.assertEqual(rm.check_issues(), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestStartGame(unittest.TestCase):
    """开局音效：转 WAV / 裁剪 / 自动安装。"""

    def _make_src_wav(self, path: str, seconds: float = 3.0, rate: int = 44100):
        import math
        import struct
        import wave
        frames = []
        for i in range(int(rate * seconds)):
            v = int(8000 * math.sin(2 * math.pi * 440 * i / rate))
            frames.append(struct.pack("<hh", v, v))
        with wave.open(path, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"".join(frames))

    def test_convert_to_wav_spec(self):
        """转换结果为 44.1kHz/16bit/立体声 PCM，时长一致。"""
        import wave
        d = tempfile.mkdtemp(prefix="wav_spec_")
        try:
            src = os.path.join(d, "src.wav")
            self._make_src_wav(src, 2.0)
            dst = os.path.join(d, "out.wav")
            music_lib.convert_to_wav(src, dst)
            with wave.open(dst, "rb") as w:
                self.assertEqual(w.getframerate(), 44100)
                self.assertEqual(w.getsampwidth(), 2)
                self.assertEqual(w.getnchannels(), 2)
            self.assertAlmostEqual(music_lib.wav_duration(dst), 2.0, delta=0.2)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_crop_and_max_duration(self):
        """区间裁剪时长正确；超过 12s 上限被截断。"""
        d = tempfile.mkdtemp(prefix="wav_crop_")
        try:
            src = os.path.join(d, "src.wav")
            self._make_src_wav(src, 3.0)
            dst = os.path.join(d, "crop.wav")
            dur = music_lib.make_start_game_wav(src, dst, 0.5, 2.5)
            self.assertAlmostEqual(dur, 2.0, delta=0.3)
            dst2 = os.path.join(d, "long.wav")
            dur2 = music_lib.make_start_game_wav(src, dst2, 0.0, 20.0)
            self.assertLessEqual(dur2, music_lib.START_GAME_MAX_SECONDS + 0.1)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_install_start_game(self):
        """自动安装：追加定义(zz_) + 01/02 双 wav + 备份；禁用后全部删除。"""
        d = tempfile.mkdtemp(prefix="sg_")
        try:
            src = os.path.join(d, "src.wav")
            self._make_src_wav(src, 1.0)
            mod_dir = os.path.join(d, "mod")
            rm = music_lib.RadioMod("radio_sg", "音效电台", mod_dir)
            rm.create()
            # 预置旧定义文件验证备份
            os.makedirs(os.path.dirname(rm.start_game_asset_path), exist_ok=True)
            with open(rm.start_game_asset_path, "w", encoding="utf-8") as f:
                f.write("old")
            rm.install_start_game(src, volume=0.1)
            self.assertTrue(os.path.exists(rm.start_game_wav_path))
            self.assertTrue(os.path.exists(rm.start_game_wav_02_path))
            self.assertTrue(os.path.exists(rm.start_game_asset_path))
            # 绝不创建 sound.asset（整文件替换会顶掉原版音效）；追加定义用 zz_ 前缀
            self.assertFalse(os.path.exists(rm.legacy_sound_asset_path))
            self.assertTrue(os.path.basename(rm.start_game_asset_path).startswith("zz_"))
            with open(rm.start_game_asset_path, encoding="utf-8") as f:
                asset = f.read()
            self.assertIn('name = "start_game_01"', asset)
            self.assertIn('name = "start_game_02"', asset)
            self.assertIn('file = "menu/start_game_01.wav"', asset)
            self.assertIn('file = "menu/start_game_02.wav"', asset)
            self.assertIn("volume = 0.1", asset)
            self.assertTrue(os.path.exists(rm.start_game_asset_path + ".bak"))
            # 输出 wav 本身可读
            self.assertGreater(music_lib.wav_duration(rm.start_game_wav_path), 0)
            self.assertGreater(music_lib.wav_duration(rm.start_game_wav_02_path), 0)
            # 状态与禁用
            installed, desc = rm.start_game_status()
            self.assertTrue(installed)
            self.assertIn("已安装", desc)
            self.assertIn("秒", desc)
            self.assertIn("volume 0.1", desc)
            removed = rm.remove_start_game()
            self.assertGreaterEqual(len(removed), 3, removed)   # 01wav + 02wav + zz
            self.assertFalse(os.path.exists(rm.start_game_wav_path))
            self.assertFalse(os.path.exists(rm.start_game_wav_02_path))
            self.assertFalse(os.path.exists(rm.start_game_asset_path))
            installed2, desc2 = rm.start_game_status()
            self.assertFalse(installed2)
            self.assertEqual(desc2, "未安装")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_crop_wav_fast(self):
        """纯 Python 切片：时长正确、采样规格保留、无 ffmpeg（毫秒级）。"""
        import time
        d = tempfile.mkdtemp(prefix="crop_")
        try:
            src = os.path.join(d, "src.wav")
            self._make_src_wav(src, 10.0)
            dst = os.path.join(d, "crop.wav")
            t0 = time.perf_counter()
            music_lib.crop_wav(src, dst, 2.0, 5.0)
            elapsed = time.perf_counter() - t0
            print(f"\n  crop_wav 耗时: {elapsed * 1000:.1f} ms")
            self.assertAlmostEqual(music_lib.wav_duration(dst), 3.0, delta=0.05)
            import wave
            with wave.open(dst, "rb") as w:
                self.assertEqual(w.getframerate(), 44100)
                self.assertEqual(w.getsampwidth(), 2)
                self.assertEqual(w.getnchannels(), 2)
            # 边界：越界区间裁剪到有效范围
            music_lib.crop_wav(src, dst, 8.0, 99.0)
            self.assertAlmostEqual(music_lib.wav_duration(dst), 2.0, delta=0.05)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_apply_fade_out(self):
        """末尾淡出：末尾样本趋近 0、开头不变；fade<=0 等同复制。"""
        import array
        import wave
        d = tempfile.mkdtemp(prefix="fade_")
        try:
            self.assertEqual(music_lib.START_GAME_MAX_SECONDS, 15.0)
            src = os.path.join(d, "src.wav")
            self._make_src_wav(src, 2.0)   # 恒定 8000 正弦
            dst = os.path.join(d, "fade.wav")
            music_lib.apply_fade_out(src, dst, 0.5)
            with wave.open(src, "rb") as r:
                orig = array.array("h", r.readframes(r.getnframes()))
            with wave.open(dst, "rb") as r:
                faded = array.array("h", r.readframes(r.getnframes()))
            self.assertEqual(len(orig), len(faded))
            self.assertEqual(orig[0], faded[0], "开头不应受影响")
            self.assertLess(abs(faded[-1]), 500, "末尾应接近静音")

            def _avg_abs(data, start_idx, n):
                seg = data[start_idx:start_idx + n]
                return (sum(abs(x) for x in seg) / len(seg)) if seg else 0.0

            fade_samples = int(0.5 * 44100 * 2)
            tail_orig = _avg_abs(orig, len(orig) - fade_samples, fade_samples)
            tail_fade = _avg_abs(faded, len(faded) - fade_samples, fade_samples)
            self.assertLess(tail_fade, tail_orig * 0.6,
                            f"淡出段音量应显著减小: {tail_fade} vs {tail_orig}")
            # fade<=0 → 原样复制
            dst2 = os.path.join(d, "copy.wav")
            music_lib.apply_fade_out(src, dst2, 0)
            with wave.open(dst2, "rb") as r:
                self.assertEqual(orig.tolist(),
                                 array.array("h", r.readframes(r.getnframes())).tolist())
        finally:
            shutil.rmtree(d, ignore_errors=True)


    def test_radio_index(self):
        """电台索引：登记/更新/移除（指向任意目录，跨目录可发现）。"""
        d = tempfile.mkdtemp(prefix="ridx_")
        orig = music_lib.radio_index_path
        music_lib.radio_index_path = lambda: os.path.join(d, "radio_index.json")
        try:
            music_lib.index_add_radio("r1", "电台一", r"D:\mods\a")
            music_lib.index_add_radio("r2", "电台二", r"D:\mods\b")
            music_lib.index_add_radio("r1", "电台一改", r"D:\mods\a")  # 同条目更新显示名
            entries = music_lib.load_radio_index()
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["display_name"], "电台一改")
            # 编辑位置
            music_lib.index_update_radio("r1", r"D:\mods\a", r"D:\new\a")
            entries = music_lib.load_radio_index()
            self.assertEqual(entries[0]["mod_dir"], r"D:\new\a")
            # 移除
            music_lib.index_remove_radio("r2", r"D:\mods\b")
            entries = music_lib.load_radio_index()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["radio_id"], "r1")
        finally:
            music_lib.radio_index_path = orig
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
