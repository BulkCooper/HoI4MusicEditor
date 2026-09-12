# HOI4 电台（Music Station）制作指南

> 适用：TFR_PLA_Advances_Onward / FireBoyKing_TFR_SuperPLA（LYHC「陆域合成专辑」电台实证）
> 来源：HOI4 官方 Wiki（Music modding）+ 社区进阶指南（Steam/GamePretty）+ LYHC 实做记录
> 维护：本文件是电台制作的**唯一权威参考**，skill 中的电台节从此文档引用

---

## 一、文件结构总览（一个完整电台的全部文件）

```
<mod>/
├── music/<电台ID>/
│   ├── <任意>.asset            # 歌曲定义（name/file/volume）
│   ├── <任意>_songs.txt        # 电台声明 + 歌曲归属（chance）
│   └── *.ogg                   # 歌曲文件（真 Vorbis）
├── interface/
│   ├── music_station_<电台ID>.gfx      # 封面 sprite 注册（GFX_<电台ID>_album_art）
│   └── music_station_<电台ID>.gui      # 播放器面板 + 电台列表条目
├── gfx/
│   └── <电台ID>_album_art.(dds|png)    # 封面 304x120 横向双帧
├── localisation/<lang>/
│   └── *.yml                   # <电台ID>_TITLE + 每首歌名（UTF-8 + BOM）
└── （可选）sound/              # 启动音效等其他声音
```

> 实做注意：**文件夹名/文件名不需要等于电台 ID**（原版 `base_music` 的 gui 就叫 `music_station_base.gui`），
> 真正的绑定靠 `.gui` 里的**容器命名**（见第四节）与 `music_station` 声明值。

---

## 二、基础五件套

### 1. 歌曲与资产（`.asset`）

```clausewitz
# music/<电台ID>/<任意>.asset —— 文件位置：music/ 或子目录
music = {
	name = "钢铁洪流进行曲"        # ① 歌曲唯一 ID ② 同时是本地化键
	file = "钢铁洪流进行曲.ogg"     # 相对本 asset 文件所在目录
	volume = 0.65                  # 0–1；1 = 100%
}
```

- ⚠️ `name` **≤ 63 字符**，超限可能整个文件加载失败
- ⚠️ `.ogg` 必须是**真 Vorbis 编码**（MP3 改后缀 ≠ 转换，游戏不播）
- 文件名建议**英文 + 下划线**（游戏对空格处理差；中文可用但属经验用法）

### 2. 电台声明与歌曲归属（`_songs.txt`）

```clausewitz
# music/<电台ID>/<任意>_songs.txt —— 首行必须是 music_station
music_station = "<电台ID>"        # 电台内部 ID（ASCII，勿用中文）

music = {
	song = "钢铁洪流进行曲"        # 对应 .asset 的 name
	chance = {
		modifier = {
			factor = 1             # 基础权重（全曲 1 = 等概率）
		}
	}
}
# ……每首歌一段
```

- `music_station` 的值 = 电台内部 ID；**同名 = 合并播放池**（两个 mod 声明同名会并池）
- 该值还会用于：`_TITLE` 本地化键前缀、`.gui` 容器名前缀

### 3. 显示名（本地化，`_TITLE` 键）

```yml
# localisation/simp_chinese/*.yml （UTF-8 带 BOM + l_simp_chinese: 首行 + LF）
<电台ID>_TITLE: "陆域合成专辑"     # 电台在播放器里显示的名字
钢铁洪流进行曲: "钢铁洪流进行曲"    # 歌曲名（键 = .asset 的 name）
```

- **显示名 = 本地化键 `<电台ID>_TITLE`**（原版实证：`base_music_TITLE: "默认音轨专辑"`、`classic_TITLE: "经典专辑"`）
- 改显示名**只改这个键**，不动电台 ID（ID 变动会牵动 gui 容器绑定）

### 4. 界面（`.gfx` + `.gui`）

```clausewitz
# interface/music_station_<任意>.gfx —— 封面 sprite 注册
spriteTypes = {
	spriteType = {
		name = "GFX_<电台ID>_album_art"
		texturefile = "gfx/<电台ID>_album_art.dds"   # 或 .png（可用但体积大）
		noOfFrames = 2                                # 横向两帧：左=未选中 右=选中
	}
}
```

```clausewitz
# interface/music_station_<任意>.gui —— 两个容器，命名是硬绑定
guiTypes = {
	# ① 切到本电台后，播放器顶部的面板
	containerWindowType = {
		name = "<电台ID>_faceplate"     # ⚠️ 必须 "<电台ID>_faceplate"
		size = { width = 590 height = 46 }
		# 内含：header_bg iconType / track_name / track_elapsed / track_duration
		#       prev_button / play_button / next_button / shuffle_button / volume_slider
		# 全部引用游戏原版 sprite（GFX_musicplayer_*），不要抄其他 mod 的自定义资源
	}

	# ② 电台列表里的图标按钮（点它切台）
	containerWindowType = {
		name = "<电台ID>_stations_entry"   # ⚠️ 必须 "<电台ID>_stations_entry"
		size = { width = 162 height = 130 }
		checkBoxType = {
			name = "select_station_button"
			quadTextureSprite = "GFX_<电台ID>_album_art"   # 指向 .gfx 注册名
		}
	}
}
```

- **绑定机制**：游戏按 `<电台ID>_faceplate` / `<电台ID>_stations_entry` 容器名自动挂载（原版 `base_music` → `base_music_faceplate`）
- ⚠️ `.gui` 容器 `name` **无中文先例**（TFR/红黎明/本体 0 命中）——保持 ASCII
- 自定义 faceplate 是**可选**的（TFR 的 `one_minute` 电台就没有 gui，用默认面板）

### 5. 封面（`.dds`）

- 尺寸 **304×120**、横向两帧（左 = 未选中 / 右 = 选中）
- 右帧自带**勾选标记**（✓）；改图时别盖掉
- dds 未压缩格式（参考原版 `base_game_album_art.dds`：复用 header + 换像素即可）
- 常规位置：`gfx/interface/topbar/musicplayer/<名>_album_art.dds`

---

## 三、进阶能力

### 1. `chance` = 条件播放权重（Weighted Shuffle 模式生效）

```clausewitz
music = {
	song = "钢铁洪流进行曲"
	chance = {
		base = 1                                     # base = 整体替换（重设）
		modifier = { factor = 3  has_war = yes }     # 条件全满足时 ×3
		modifier = { add = 1  tag = PRC }            # 满足时 +1
	}
}
```

- 参数：`base`（替换）/ `factor`（乘）/ `add`（加）
- `modifier = {}` 内**所有触发器都满足**才应用
- 只在游戏内播放器的 **Weighted Shuffle（加权随机）** 模式生效
- 玩法示例：
  - 战时军歌权重 ×3、和平时期交响乐 +1
  - 仅 PRC 播放《请你检阅》

### 2. `play_song` effect —— 在事件/决议里播歌

```clausewitz
complete_effect = {
	play_song = { song = "钢铁洪流进行曲" }   # 歌曲必须已在电台中定义
}
# scoped_play_song = { ... }  # 只对某国播放
```

用途：决议仪式感（如「决心发展下一代人民陆军体系」完成时播放指定曲目）。

### 3. `common/playlists/settings.txt` —— 电台激活清单

- Wiki 规定：电台在 `common/playlists/settings.txt` 中列出（空格分隔）
- LYHC 实做未提供该文件也正常工作（说明现代版本非强制），但按 Wiki 补上更稳

---

## 四、硬性规格速查表

| 项目 | 规格 |
|---|---|
| 音频格式 | 真 Vorbis `.ogg`（非改名 MP3） |
| `.asset` name | **≤ 63 字符**，且 = 本地化键 |
| 封面 | `.dds`（或 png）**304×120**、`noOfFrames = 2` |
| 容器命名 | `<电台ID>_faceplate` / `<电台ID>_stations_entry`（ASCII） |
| sprite 命名 | `GFX_<电台ID>_album_art`（常用惯例） |
| 电台显示名 | 本地化键 `<电台ID>_TITLE` |
| 电台 ID | ASCII，勿中文；同名 = 合并播放池 |
| 本地化 yml | **UTF-8 + BOM** + 首行 `l_simp_chinese:` + LF |
| 其他文件（.gui/.gfx/.asset/.txt） | UTF-8 **无 BOM** |
| 范本 | 官方 **Sabaton DLC**（最标准参考） |
| 工具 | Music Mod Creation Tool（浏览器生成器）、HOI4 Music Manager（GitHub） |

---

## 五、LYHC「陆域合成专辑」实做记录（本项目）

### 实际文件清单（新 mod 内）
```
music/LYHC/LYHC_music.asset          # 11 首歌定义（volume 0.65）
music/LYHC/LYHC_songs.txt            # music_station = "LYHC" + 11 首 chance
music/LYHC/*.ogg                     # 15 个 ogg（含 1 个未引用、1 个重复版）
interface/music_station_LYHC.gfx     # GFX_LYHC_album_art 注册（png、两帧）
interface/music_station_LYHC.gui     # LYHC_faceplate + LYHC_stations_entry
gfx/LYHC_album_art.png               # 304x120 双帧封面（png 版）
sound/zz_LYHC_sounds.asset + menu/*.wav   # 启动音效覆盖
```

### 关键实做结论
1. **显示名**：加 `LYHC_TITLE: "陆域合成专辑"` 即改播放器显示名（不动电台 ID）
2. **封面用 png 也能加载**（LYHC 实做），但 Wiki 推荐 dds（体积小）
3. **未做但可升级**：`chance` 条件播放、`play_song` 决议播歌、`playlists/settings.txt`
4. `LYHC_songs.txt` 里 `chance.modifier.factor = 1` 全曲等概率 = 当前行为

### 已踩/已避的坑
- 电台 ID 用中文会牵动 `<ID>_faceplate` 容器名（中文容器名无先例）→ **ID 保持 ASCII，显示名走 `_TITLE`**
- 两 mod 声明同名电台 → 合并播放池（无害但重复）；同名 gui/gfx 文件 → 后加载覆盖（内容相同则无害）
- `.ogg` 文件名含中文/特殊符号（LYHC 有括号、空格、`(0x2f)` 等）实际可用，但新歌建议英文+下划线
- **`chance` 条件播放不可靠**：`chance` 权重/条件仅在「加权随机（Weighted Shuffle）」模式生效，且该模式存在社区已知 bug；本项目 v1.7.0 曾据此实现「特定时间播放 / 独占」（`factor = 0` + `date` 触发器），游戏内实测（含切到加权随机模式）无效，已于 v1.7.1 移除，勿再基于该机制做功能

---

## 六、参考来源

| 来源 | 内容 |
|---|---|
| HOI4 Wiki – Music modding | 官方规范：asset/songs/chance/faceplate/封面/激活 |
| HOI4 Wiki – Effect（play_song） | 决议/事件播歌 |
| Steam 指南（Stannid 等） | faceplate/stations_entry 逐文件拆解、双帧封面技巧 |
| GamePretty 指南 | 从原版模板复制的完整流程 |
| 本项目 LYHC 实做 | `_TITLE` 显示名方案、png 封面可用性、ASCII 容器名约束 |

---

*本文件由项目开发记录自动整理；新增电台经验请追加到「LYHC 实做记录」或对应小节。*

> 项目相关：本工具的开发 / 维护规范见 [DEVELOPMENT.md](../DEVELOPMENT.md)，版本更新记录见 [CHANGELOG.md](../CHANGELOG.md)。
