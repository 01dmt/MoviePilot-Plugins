# 01dmt MoviePilot Plugins

`01dmt` 维护的 MoviePilot 第三方插件仓库。

## 安装

1. 在 MoviePilot 的插件市场设置中添加仓库地址：

   ```text
   https://github.com/01dmt/MoviePilot-Plugins
   ```

2. 刷新插件市场。
3. 在插件市场安装需要的插件。
4. 安装后打开插件配置页完成首次设置。

## 插件

| 插件 | 当前版本 | 说明 |
| --- | --- | --- |
| [TMDB自动订阅](plugins.v2/tmdbautosubscribe/README.md) | `1.0.12` | 根据 TMDB 发现近期电影、新剧首播和老剧新季，生成或自动提交 MoviePilot 订阅。 |

## TMDB自动订阅

适用于希望持续发现新内容、但仍需要保留筛选与订阅控制的人。

- 扫描近期上映电影、新剧首播与老剧新季。
- 支持媒体类型、类型、原产国、原始语言和 TMDB ID 黑名单。
- 默认仅生成订阅建议；开启自动订阅后才会提交给 MoviePilot。
- 提交前按媒体类型、TMDB ID 和季号跳过 MoviePilot 已有订阅。
- 默认跳过未提供中文标题的条目，可在配置页关闭。

详细的发现规则、配置说明与排障方法见 [插件说明](plugins.v2/tmdbautosubscribe/README.md)。

## 兼容性

- MoviePilot：`>= 2.12.0`
- TMDB：可使用 MoviePilot 全局 `TMDB_API_KEY`，也可在插件中单独填写。

## 仓库结构

```text
MoviePilot-Plugins/
├─ package.v2.json
└─ plugins.v2/
   └─ tmdbautosubscribe/
      ├─ __init__.py
      ├─ tmdbautosubscribe.svg
      └─ README.md
```

## 反馈

请在本仓库的 GitHub Issues 中附上 MoviePilot 版本、插件版本、扫描配置和相关日志；请勿提交 TMDB API Key、Cookie 或其他凭据。
