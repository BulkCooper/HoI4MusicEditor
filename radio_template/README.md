# 电台挂载模板(radio_my)

一套可直接复制的 HOI4 音乐电台模板。参照 `烈焰升腾:潜龙腾渊电台`(3370537530)的标准结构制作。

## 文件结构

```
radio_template/
├── music/radio_my/
│   ├── radio_my_music.asset      # ① 定义歌曲(名字 + ogg 文件 + 音量)
│   ├── radio_my_songs.txt        # ② 声明电台 + 挂歌(播放权重/条件)
│   └── (把你的 .ogg 歌曲文件放这里)
├── gfx/
│   └── radio_my_album_art.png    # ③ 电台图标(占位图,304x120 横向两帧)
└── interface/
    ├── music_station_radio_my.gfx  # ④ sprite 注册(图标 -> 游戏资源)
    └── music_station_radio_my.gui  # ⑤ 电台界面(faceplate + stations_entry)
```

## 改名清单(把 radio_my 换成你的电台名,全局替换)

| 位置 | 内容 |
|---|---|
| `music/radio_my/` | 目录名 |
| `music/radio_my/*.asset` | 歌曲 name、file(对应 ogg 文件名) |
| `music/radio_my/*.songs.txt` | `music_station = "..."`(电台名,游戏里显示的名字) |
| `interface/music_station_radio_my.gfx` | 文件名、sprite 名 `GFX_...`、texturefile |
| `interface/music_station_radio_my.gui` | 文件名;容器名 `radio_my_faceplate`、`radio_my_stations_entry`(**绑定关键**);quadTextureSprite |

## 生效规则(记牢 4 条)

1. **容器名绑定电台**——`.gui` 文件名随意,但容器 `name` 必须是 `电台名_faceplate` 和 `电台名_stations_entry`,游戏按容器名自动挂载。
2. **电台名不重复**——同名电台会合并播放池;歌曲 `name` 全局唯一,别和别的 mod 撞。
3. **图标两帧(横向)**——`gfx/radio_my_album_art.png` 整图 **304×120**,左边一帧=未选中、右边一帧=选中(高亮),每帧 **152×120**。这是原版 `base_game_album_art.dds` 的权威尺寸。正式发布建议用 GIMP/PS 转成 DDS;原版 mod 的 `.dds` 内容实际是 PNG 也能加载(游戏按内容识别,不严格看扩展名)。
4. **没有 ③④⑤ 也能播**——①②(asset + songs.txt)是必须的,图标/面板是可选的装饰。

## 落位方式(二选一)

**方式 A:并入现有电台 mod(不推荐)**
把 `music/radio_my/`、`gfx/`、`interface/` 的文件合并进 mod 目录(如 `SteamLibrary/steamapps/workshop/content/394360/3370537530/`)。注意:创意工坊文件会被 Steam 校验还原,更新后文件可能被清掉。

**方式 B:独立本地 mod(推荐)**
复制 `radio_template/` 整个目录到本地 mod 目录,并创建一个 `.mod` 注册文件:

```
# Documents/Paradox Interactive/Hearts of Iron IV/mod/radio_my.mod
name="我的电台"
path="C:/Users/BulkCooper/Documents/Paradox Interactive/Hearts of Iron IV/mod/radio_my"
supported_version="1.19.*"
tags={
	"Sound"
}
```

然后在启动器里启用它(你机器上所有 mod 目前都是未启用状态,记得勾选)。

## 其他

- 音频必须是 **OGG Vorbis** 格式,否则游戏不认。
- 想加更多歌:复制 asset 里的 `music = {...}` 块、songs.txt 里的 `music = {...}` 块即可。
- 条件示例(歌曲只在特定情况进播放池):

```clausewitz
music = {
	song = "示例歌曲二"
	chance = {
		modifier = {
			factor = 1
			has_war = yes            # 战争时
			# tag = CHI             # 或限定国家(任意触发条件都可用)
		}
	}
}
```
