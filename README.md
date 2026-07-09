# MoviePilot-Plugins

这是 `01dmt` 维护的 MoviePilot 第三方插件库。

仓库采用 MoviePilot 插件市场常见结构：根目录维护 `package.v2.json` 插件索引，插件代码放在 `plugins.v2/<plugin_id>/` 下。后续新增插件时，只需要继续添加插件目录并更新 `package.v2.json`。

## 插件列表

| 插件 | 目录 | 说明 |
| --- | --- | --- |
| TMDB自动订阅 | `plugins.v2/tmdbautosubscribe` | 按 TMDB 新上映、新剧首播和老剧新季生成 MoviePilot 订阅建议，支持自动订阅、筛选配置、扫描进度和海报墙调试。 |

## MoviePilot 使用方式

1. 在 MoviePilot 插件市场设置中添加本仓库地址：

   ```text
   https://github.com/01dmt/MoviePilot-Plugins
   ```

2. 刷新插件市场。
3. 安装需要的插件，例如 `TMDB自动订阅`。

## 仓库结构

```text
MoviePilot-Plugins/
├─ package.v2.json
└─ plugins.v2/
   └─ tmdbautosubscribe/
      ├─ __init__.py
      └─ README.md
```

## 本地校验

插件发布前建议执行：

```powershell
python -m json.tool package.v2.json > $null
python -m compileall plugins.v2
```
