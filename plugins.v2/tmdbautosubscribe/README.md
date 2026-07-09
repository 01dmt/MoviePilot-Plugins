# TMDB 自动订阅

按 TMDB 数据扫描近期新上映电影、新剧首播和老剧新季，并生成 MoviePilot 订阅建议。

## 当前策略

- 数据源以 TMDB 为主。
- 默认只启用剧集、动画、日本、日语，适合先观察日番。
- 默认关闭自动订阅；扫描会按规则拉取 TMDB 数据并生成订阅建议，不会直接改动订阅。
- `TMDB API Key` 留空时使用 MoviePilot 系统配置里的 `TMDB_API_KEY`；接口域名跟随 `TMDB_API_DOMAIN`。
- 如果 TMDB Key、域名或网络异常，扫描结果会在详情页用红色告警显示 TMDB 返回的错误信息。
- 如果只是个别 TMDB 详情请求失败，会记录为黄色告警并跳过该条，其他候选会继续分析。
- 新剧按 `first_air_date` 判断。
- 老剧新季按 `season.air_date` 判断。
- 同季新集只写入缓存观察，不触发订阅。

## MoviePilot 试用流程

1. 让 MoviePilot 使用当前 `MoviePilot-Plugins` 仓库。
2. 刷新或重载插件列表，找到 `TMDB自动订阅`。
3. 先保持 `自动订阅` 关闭。
4. 在基础配置里调整扫描天数和媒体类型。
5. 需要细分时展开 `细节分类`，调整类型、原产国、原始语言。
6. 打开插件详情页，点击 `立即扫描`，扫描完成后刷新详情页查看最新海报墙。
7. 查看 `拉取诊断`、`缓存观察`、`订阅建议` 和 `候选明细`。
8. 多轮测试前可点击 `清空缓存`，清掉上次结果和剧集观察缓存。

## 真实 MP 验收清单

首次接入 MoviePilot 时建议先只生成订阅建议：

1. 配置页能保存默认配置：`剧集 + 动画 + 日本 + 日语`、向前 7 天、向后 0 天。
2. `自动订阅` 保持关闭。
3. `细节分类` 能展开，类型、原产国、原始语言可保存。
4. 详情页点击 `立即扫描` 后接口返回成功提示；刷新详情页后能看到最近扫描时间。
5. `订阅建议` 海报墙只出现新剧首播或老剧新季；卡片角标显示 `符合`。
6. `候选明细` 海报墙可以出现柯南、航海王这类长期更新剧，但角标应为 `未命中`，原因不是新剧或新季。
7. `拉取诊断` 显示每个队列拉了几页、总量、停止原因；`连续低新数据` 说明后续页基本都是已见过的剧。
8. `缓存观察` 显示已见过的剧集、是否发现新季变化或同季新集；同季新集只记录，不会创建订阅。
9. 确认订阅建议符合预期后，再开启 `自动订阅`。

## 面板解读

- `订阅建议`：符合规则的项目；只有开启 `自动订阅` 后才会提交给 MoviePilot。
- `候选明细`：本次从 TMDB 拉到并分析过的项目，用来判断有没有漏扫或误筛。
- `拉取诊断`：判断页数是否够用。`P4 新1 已知19` 表示第 4 页只有 1 个陌生 TMDB ID，其余 19 个已经在缓存里见过。
- `缓存观察`：用 TMDB 详情快照对比季数、季首播日期和集数变化。老剧同季新集会记录为观察信号，但当前策略不会订阅；来源为近期播出队列。

## 扫描参数

- `电影/新剧页数`：电影上映和新剧首播固定拉取的页数。
- `播出最少页数` / `播出最多页数`：近期播出队列至少和最多拉取的页数，用于发现老剧新季。
- `低新连续页数`：连续多少页低于阈值后停止继续拉取。
- `每页有效新数据`：一页里陌生 TMDB ID 少于这个数时，算作低新数据。
- `TMDB超时秒数` / `TMDB重试次数`：单次 TMDB 请求的等待和重试控制；默认 10 秒、2 次，避免交互扫描被慢请求长时间拖住。
- `执行周期` 使用 cron 表达式；如果填写错误，插件会关闭启用状态并在详情页显示定时任务配置错误，避免插件页面加载失败。
- 类型、原产国、原始语言多选时按“任一匹配”筛选，例如 `日本 + 美国` 表示日本或美国。

## 本地验证

一键本地验收，默认不访问 TMDB：

```powershell
python tests\v2\tmdbautosubscribe\acceptance_all.py
```

一键本地验收并额外访问真实 TMDB 做安全试跑：

```powershell
python tests\v2\tmdbautosubscribe\acceptance_all.py --real
```

安装前结构自检，不启动真实 MoviePilot、不访问 TMDB：

```powershell
python tests\v2\tmdbautosubscribe\preflight.py
```

不启动真实 MoviePilot、不访问 TMDB 的完整桩验证：

```powershell
python tests\v2\tmdbautosubscribe\verify_stub.py
```

真实访问 TMDB 的安全试跑，默认按 `剧集 + 动画 + 日本 + 日语` 只扫一页；自动订阅关闭，不创建 MoviePilot 订阅、不写真实 MoviePilot 数据库。输出会包含候选的命中状态和未命中原因，方便判断柯南、航海王这类长期更新剧为什么没有进入订阅建议：

```powershell
python tests\v2\tmdbautosubscribe\real_dry_run.py
```

常规门禁：

```powershell
python -m compileall plugins.v2\tmdbautosubscribe tests\v2\tmdbautosubscribe
python .github\scripts\check_plugin_versions.py package.v2.json
```

`pytest tests\v2\tmdbautosubscribe\test_plugin.py -q` 依赖本地 MoviePilot 后端测试环境。当前本机如果缺 `psutil`，会在 MoviePilot bootstrap 阶段中断，尚未进入插件测试本身。

`psutil` 是 MoviePilot 主程序运行依赖，已在 MoviePilot 的 `requirements.in` 声明。需要跑完整 pytest 时，先在 MoviePilot 仓库安装后端开发依赖：

```powershell
pip install -r requirements-dev.in
```

## 接入 MoviePilot

本插件已经按 MoviePilot V2 本地插件仓库结构放在 `plugins.v2/tmdbautosubscribe`，并在 `package.v2.json` 中声明 `TmdbAutoSubscribe`。

推荐接入方式：

1. 在 MoviePilot 设置中把本仓库加入本地插件仓库路径，路径为当前 `MoviePilot-Plugins` 目录。
2. 刷新插件市场，找到 `TMDB自动订阅`。
3. 先安装并保持 `自动订阅` 关闭。
4. 配置页先改时间范围和媒体类型；需要细分时展开 `细节分类`。
5. 详情页点 `立即扫描`，刷新详情页后检查 `拉取诊断`、`订阅建议`、`候选明细` 和 `缓存观察`。
6. 确认订阅建议符合预期后，再开启 `自动订阅`。

真实 MP 验收重点：

- 配置页可以保存默认配置。
- `细节分类` 展开区能打开并保存类型、原产国、原始语言。
- 详情页按钮能触发页面相对 API `plugin/TmdbAutoSubscribe/scan`；外部 HTTP 地址为 `/api/v1/plugin/TmdbAutoSubscribe/scan?apikey=<API_TOKEN>`。
- `订阅建议` 海报墙只展示新剧首播、老剧新季。
- `候选明细` 海报墙展示全部拉取明细，并用 `符合` / `未命中` 标识。

验收辅助脚本：

```powershell
# 离线检查插件结构、默认配置、表单和详情页按钮
python tests\v2\tmdbautosubscribe\mp_acceptance.py

# MoviePilot 启动后，检查插件 API 是否可访问
python tests\v2\tmdbautosubscribe\mp_acceptance.py --base-url http://127.0.0.1:3000 --token <API_TOKEN>

# 确认自动订阅关闭后，再触发一次真实立即扫描 API
python tests\v2\tmdbautosubscribe\mp_acceptance.py --base-url http://127.0.0.1:3000 --token <API_TOKEN> --run-scan
```

