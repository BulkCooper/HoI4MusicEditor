# HOI4 开局加载音效(start_game)修改指南

记录「点击开始游戏 → 进入加载界面」那一刻播放的**音效**如何定位与修改。基于本机 1.19.* 游戏本体的实际文件结构。

---

## 一、这个音效是什么

- **触发时机**:单人(或多人主机)点击「开始游戏」按钮、进入加载读条的那一刻,由**游戏代码**播放一次。
- **不是**按钮点击音(那类是 `ui_menu_over` 等,0.1~0.2 秒);这段是 8~10 秒的过场音效。
- 每次开局固定播放,与 mod、DLC 无关(除非被 mod 覆盖)。

## 二、文件位置与定义链

| 内容 | 位置 |
|---|---|
| 音效本体 | `<游戏根目录>/sound/menu/start_game_01.wav`、`start_game_02.wav` |
| 音效定义 | `<游戏根目录>/sound/sound.asset` → **START GAME** 区块(约 L50–64) |
| 触发逻辑 | 引擎代码写死查找名为 `start_game_01` 的音效,不由 GUI 按钮触发 |

`sound.asset` 中原始定义:

```clausewitz
######################### START GAME ############################
sound =
{
	name = "start_game_01"
	file = "menu/start_game_01.wav"
	always_load = no
	volume = 0.1
}
sound =
{
	name = "start_game_02"
	file = "menu/start_game_02.wav"
	always_load = no
}
```

**触发方式的开发者注释证据**(`interface/frontendgamesetupview.gui` L2291/L2303):

> `#clicksound = start_game_01` — Don't add this here. I intentionally removed it because otherwise the sound is played when the client clicks on Ready/Unready button. **The sound should only play when the host starts the game (or when it's SP) which is done in the code.**

即:P 社故意不把它绑在按钮上(怕多人房间点 Ready 就响),而是由代码在开始游戏时播放。

## 三、音频规格(改之前必看)

- **格式**:WAV(PCM 编码),44.1kHz / 16bit / 立体声。不是 44.1kHz 会在 `error.log` 刷
  `For best performance and quality music files should be in 44.1kHz` 警告,且可能音调/速度异常。
- **时长**:建议 5~12 秒(原版 8.48s / 9.94s)。太短会突兀,太长会盖过加载提示音。
- **音量**:原版定义里 `start_game_01` 有 `volume = 0.1`(音量仅 10%),`start_game_02` 无此字段(默认)。
  替换时按自己音频响度调整该值。

## 四、修改方式(两种)

### 方式 A:直接替换本体文件(简单,不推荐)

用同名 wav 覆盖 `<游戏根目录>/sound/menu/start_game_01.wav` 即可,无需改任何定义文件。

- ✅ 零配置、立刻生效
- ❌ **会被 Steam 校验还原**:游戏更新 / 「验证游戏文件完整性」后恢复原样
- ❌ 污染游戏本体,无法随 mod 分发给别人

### 方式 B:独立本地 mod(推荐)

在本地 mod 目录里**重定义同名音效**,原版文件保持不动。游戏对 `sound/sound.asset` 的处理是**同路径合并加载、同名 sound 后加载者覆盖**,所以 mod 里只需要写 `start_game_01` 这一个块,其余音效全部不受影响。

目录结构(放在 `Documents/Paradox Interactive/Hearts of Iron IV/mod/` 下):

```
my_startgame/
├── descriptor.mod
└── sound/
    ├── sound.asset              # 重定义 start_game_01
    └── menu/
        └── start_game_01.wav    # 你的音频(注意 file 路径相对 sound/ 目录)
```

`descriptor.mod`:

```
name="开局音效修改"
supported_version="1.19.*"
tags={
	"Sound"
}
path="mod/my_startgame"
```

`sound/sound.asset`(mod 版,只需一个块):

```clausewitz
sound =
{
	name = "start_game_01"
	file = "menu/start_game_01.wav"
	always_load = no
	volume = 0.1
}
```

想连 `start_game_02` 一起换,照抄第二个块即可。

### ⚠️ 为什么必须沿用 `start_game_01` 这个名字?

触发逻辑是**引擎代码写死**的:开局时只查找名为 `start_game_01` 的音效。换个新名字(如 `start_game_my`)游戏根本不会播——所以**改名字做不到,只能覆盖原定义**。

## 五、验证与排查

1. 启动游戏 → 点「开始游戏」→ 听加载界面音效是否替换成功。
2. 看日志 `Documents/Paradox Interactive/Hearts of Iron IV/logs/error.log`:
   - `For best performance... 44.1kHz` → 音频采样率不对
   - `Could not find sound` 之类 → 定义或路径写错
3. 调试期可临时把 `volume` 调成 `1` 方便分辨,确认后改回。

## 六、注意事项

- **别改创意工坊目录**(`steamapps/workshop/content/394360/<id>/`):Steam 会校验还原。
- **别改本体 `sound.asset`**:更新会被还原;mod 方式足够,无需碰本体。
- wav 必须是**真 PCM 编码**,不能是 mp3 改扩展名(游戏不认)。
- `descriptor.mod` 里的中文名有编码问题风险(ANSI/UTF-8 混用会乱码),乱码不影响加载,介意就用英文名。
- 音效属于 mod 内容,会改变联机校验和(checksum),多人游戏需全员一致。
