# 开发文档（DEVELOPMENT.md）

面向**开发者 / 维护者**的技术文档与维护规范。

- 使用说明（玩家向）→ [README.md](README.md)
- 版本更新记录 → [CHANGELOG.md](CHANGELOG.md)
- 开发文档（本文档）→ **每次更新代码后必须同步更新本文档**

---

## 1. 项目概览

《钢铁雄心4》(Hearts of Iron IV) 音乐播放列表编辑器：图形界面工具，编辑游戏音乐目录下的 `music.asset` 与 `_songs.txt`，支持上传/转换 ogg、设置播放逻辑（状态 × 阵营）、音量、一键生成独立电台 MOD 与开局音效。

| 项 | 说明 |
|---|---|
| 语言 | Python 3.11+ |
| GUI | tkinter（标准库）+ tkinterdnd2（文件拖放） |
| 音频转换 | imageio-ffmpeg（内置 ffmpeg，转 ogg / wav） |
| 图标处理 | Pillow（电台封面裁剪/合成） |
| 打包 | PyInstaller（`HoI4MusicEditor.spec`，单文件免安装） |
| 测试 | `unittest`（test_music_lib.py，50 项） |
| 编码 | 源码与文档 UTF-8；写 `.mod`/`descriptor.mod` 用 UTF-8 BOM |

## 2. 目录结构

```
app.py                  GUI 主程序（tkinter，入口）
music_lib.py            核心库：解析/序列化/播放逻辑/音频转换/电台 MOD
test_music_lib.py       单元测试（50 项）
HoI4MusicEditor.spec    PyInstaller 打包配置
logo.ico                软件图标（窗口 + exe）
README.md               使用说明
CHANGELOG.md            版本更新记录
DEVELOPMENT.md          开发文档（本文件）
docs/HOI4_电台制作指南.md  电台制作权威参考（文件结构 / 硬性规格 / 条件播放 / 封面）
pla_music.asset / pla_songs.txt   参考样例（标准格式范本）
radio_template/         电台 MOD 结构模板（gfx / interface / music）
music/                  （本地音乐素材，**不入库**）
release/                （发布包产物，**不入库**）
build/ dist/            （PyInstaller 产物，**不入库**）
.reasonix/ reasonix.toml（开发工具配置，含本机路径，**不入库**）
```

不入库内容由 `.gitignore` 控制，勿手动 add。

## 3. 模块架构

### 3.1 app.py — GUI 主程序

两级界面：

1. **模式选择页（一级）**：选择「本地电台编辑」或「已有电台编辑」；左下角「⚙ 设置」入口
2. **编辑页（二级）**：按模式加载对应内容，工具栏 + 歌曲树视图 + 状态栏

主要类：

| 类 | 职责 |
|---|---|
| `App` | 主窗口：模式切换、工具栏、歌曲列表（排序/搜索/多选）、保存/修复/删除、设置持久化 |
| `SongDialog` | 添加/编辑歌曲对话框：song name、音量、播放逻辑（状态 × 阵营 / 高级自定义） |
| `BatchAddDialog` | 批量添加（入口已移除，代码保留） |
| `ImageCropDialog` | 电台图标裁剪编辑器（152:120 选区 → 304×120 两帧） |
| `RadioModDialog` | 新建电台 MOD（ID/显示名/目标目录） |
| `RadioModOpenDialog` | 打开电台 MOD（索引 + 目录扫描） |
| `StartGameDialog` | 开局音效：转换/裁剪/试听/淡出/安装/禁用 |

模块级关键函数：`load_config` / `save_config`（`%APPDATA%\HoI4MusicEditor\config.json`）、`center_window`（多屏居中）。

### 3.2 music_lib.py — 核心库

数据模型：

- `SongRule`：一首歌的完整规则。字段含 `song`、`chance/factor`、`conditions`（预设组合）以及 `raw_lines`。
- `AssetEntry`：`music.asset` 条目（name/file/volume/raw_lines）。
- `MusicDocument`：整体文档（条目列表 + 文件级操作）。
- `MusicFolder`：音乐文件夹（含文件组定位）。
- `RadioMod`：电台 MOD（结构/注册文件/修复）。

解析与序列化（`parse_asset` / `parse_songs` / `_parse_main_modifier` / `_fmt_asset_entry` / `_fmt_song_rule`）：写回为标准格式（每个块间空一行、规范缩进）；解析 `chance` 内的主 modifier 块（factor 与触发器条件）。

播放逻辑（`conditions_from_preset` / `preset_from_conditions` / `summarize_conditions`）：预设组合 ↔ 条件文本；「主菜单」以哨兵注释 `# maintheme` 持久化。

音频：`convert_to_ogg` / `convert_to_wav` / `wav_duration` / `crop_wav` / `apply_fade_out` / `make_start_game_wav`（44.1kHz/16bit/立体声；缓存 `%TEMP%\hoi4music_cache`）。

电台 MOD：模板 `_descriptor_text` / `_gfx_template` / `_gui_template` / `_asset_template` / `_songs_template`；封面 `compose_album_art` / `make_album_art_from_image` / `make_default_album_art`；索引 `load_radio_index` / `save_radio_index` / `index_add_radio` / `index_remove_radio` / `index_update_radio`（`%APPDATA%\HoI4MusicEditor\radio_index.json`）。

### 3.3 test_music_lib.py — 单元测试

```powershell
python -m unittest test_music_lib
```

50 项，覆盖：真实文件往返一致性、解析/序列化、播放逻辑预设、音频转换、电台 MOD 结构。改动 `music_lib.py` 后必须全绿；新增行为必须补测试并同步 README 的测试数量。

## 4. 关键文件格式

`music.asset`（`AssetEntry`）：

```
music = {
    name = "my_song"
    file = "my_song.ogg"
    volume = 0.65
}
```

`_songs.txt`（`SongRule`）条目：

```
music = {
    song = "my_song"
    chance = {
        modifier = {
            factor = 1
            has_war = yes
        }
    }
}
```

> 详细条件说明见 README「文件格式说明」。

## 5. 环境与常用命令

```powershell
# 运行
python app.py

# 单元测试（改动后必须全绿）
python -m unittest test_music_lib

# 打包单文件 exe（无控制台窗口，含 tkinterdnd2 拖放）
python -m PyInstaller --noconfirm HoI4MusicEditor.spec

# 产物：dist\HoI4MusicEditor.exe
```

依赖：`pip install imageio-ffmpeg Pillow tkinterdnd2`（GUI 本身仅标准库）。打包前若 `HoI4MusicEditor.exe` 正在运行会导致 exe 被占用、PyInstaller 报 PermissionError，先结束进程。

## 6. 版本号与更新规范（每次更新必读）

版本常量：`app.py` 顶部 `VERSION = "1.7.1"`。

### 版本规则

- **大版本**：新功能里程碑（x 递增，如 1.6 → 1.7）。
- **小版本**：修复 / 微调（z 递增）。历史上曾用 `1.4.2-beta.x` / `1.6.xbeta_n` 等命名，新开发建议统一为 `x.y.z` + 后缀 `beta_n` / `alpha_n`。
- 测试中 / 未修完已知 bug 的版本标注 **Beta / Alpha**；修完才发布正式版。
- **测试版不打包压缩包**；正式版才打包并放入 `release\`。

### 每次修改后的必做清单（checklist）

1. **递增 `app.py` 的 `VERSION`**（功能 = 大版本；修复 = 小版本）。
2. **更新 `CHANGELOG.md`**：顶部新增版本条目，格式仿照现有（`## [x.y.z]（正式版/测试版）` + 分条更新内容）。
3. **更新 `README.md`**：「当前版本」、功能列表（新增功能要点）、「版本历史」摘要、测试数量。
4. **更新本开发文档 `DEVELOPMENT.md`**：涉及架构 / 数据模型 / 接口 / 文件格式 / 流程变化时同步本节与第 3/4 节；新增行为同步 3.3 的测试说明。
5. **更新发布包使用说明**：涉及用户可见功能时，同步 `release\HoI4音乐播放列表编辑器_<版本>\使用说明.txt`（含【版本更新记录】段），并保持版本号一致。
6. **测试全绿**：`python -m unittest test_music_lib`，并把 README/DEVELOPMENT 中的测试数量改准确。
7. **提交并推送**（见第 8 节）。若为发布版本，打包前用 ask 确认大 / 小版本与是否正式版。

## 7. 打包与发布

```powershell
# 打包（先确认没有正在运行的 exe）
python -m PyInstaller --noconfirm HoI4MusicEditor.spec

# Authenticode 自签名（证书已导出 release\HoI4MusicEditor.pfx）
$cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Subject -like "*HoI4*" } | Select-Object -First 1
Set-AuthenticodeSignature -FilePath dist\HoI4MusicEditor.exe -Certificate $cert
```

发布流程：**测试版**只更新源码与 exe 供验证（不打包 zip、不建 release 目录）；**正式版**把 exe + 使用说明.txt 等打包进 `release\HoI4音乐播放列表编辑器_<版本>\`。发布前向用户确认版本类型。

## 8. Git / GitHub 工作流

- 远程：`https://github.com/BulkCooper/HoI4MusicEditor.git`（分支 `main`）。
- `.gitignore` 排除：`music/`（本地素材）、`release/`、`build/`、`dist/`、`__pycache__/`、`.reasonix/`、`reasonix.toml`。
- 提交推送：

```powershell
git add .
git commit -m "描述本次改动（如：v1.7.1 移除无效的特定时间播放/独占功能）"
git push origin main
```

## 9. 已知限制与经验教训

- **`chance` 条件播放不可靠（重要教训）**：`_songs.txt` 的 `chance` 权重/条件仅在游戏「加权随机（Weighted Shuffle）」播放模式下生效，且该模式存在社区已知 bug（条件时常不生效）。v1.7.0 曾据此实现「特定时间播放 / 独占」（写入 `factor = 0` + `date` 触发器的 modifier 块，写法与官方 Sabaton DLC 一致），**游戏内实测（含切到加权随机模式）仍无效**，故 v1.7.1 整体移除。今后不要基于该机制设计功能。（电台文件结构、硬性规格等详见 [docs/HOI4_电台制作指南.md](docs/HOI4_电台制作指南.md)）
- 窗口居中基于主窗口所在显示器；高 DPI（150%+）下 tkinter 由系统缩放，文字可能略模糊。
- 自签名证书不受系统信任，SmartScreen 仍提示未知发布者。
