# 看点工场 · Kandian Workbench

看点工场是一个在本机运行的图文编辑 Agent。它从公开热榜和赛事报道发现选题，辅助写作、配图和生成比赛图表。你可以在工作台确认选题、修改稿件并导出内嵌图片的 Word 文档。项目内置体育模式，可扩展到其他内容方向。

[下载 Windows 独立版](https://github.com/YRjuuuuuuuuu/kandian-workbench/releases/latest) · [Windows 使用手册](docs/windows.md) · [贡献指南](CONTRIBUTING.md) · [MIT 许可证](LICENSE)

## 五分钟上手

1. 下载发布页中以 `windows-x64.zip` 结尾的发行包，完整解压后双击 **看点工场.exe**。系统浏览器会打开 [本地工作台](http://127.0.0.1:8765)。
2. 在「AI 模型」填入自己的 DeepSeek API Key，测试连接。Windows 独立版内置 Python、采集浏览器及制图依赖，七个模型阶段都走 DeepSeek。联网检索、生成和视觉核对需要可用的 API 额度。
3. 在首页选择运动项目和稿型，点击「开始联网找题」。确认选题后生成图文，在「稿件中心」编辑、预览并点击「导出 Word」。可一次选多条候选加入生产队列。
4. 在头条等发布后台导入 Word，自行完成发布。发布后可以在稿件中心标记「已发布」。
5. 使用完毕，先确认稿件已保存，再在控制台点击「停止工作台」，或双击 **停止工作台.cmd**。关闭浏览器后后台仍可运行。

## 功能

| 功能 | 说明 |
| --- | --- |
| 主动找题 | 读取可访问的微博体育热搜、微博实时热搜和抖音热点榜，再查赛事原文。可按赛果、复盘、人物和规则等稿型筛选。支持北京时间的定时找题。 |
| 图文生产 | 根据来源和体育写作规则生成稿件。正文有四档展开程度，默认「丰富」。配图附来源记录，有可核对的数据时生成统计卡或比较图。 |
| 图文编辑 | 支持文字与图片模块排序、封图管理、自动保存、手机与桌面预览。AI 改稿以建议形式交付。 |
| 导出与记录 | 导出 DOCX，并在本地记录已导出版本和人工确认的发布状态。 |
| 扩展模式 | 可在 `modes/` 增加赛道定义。频道写作规则支持学习、启用和回退，任务固定所用版本。 |

公开榜单会随网络和页面变化。采集器遇到访问限制时保留可读来源，其余核查回退到公开赛事检索。热榜只用于找线索，赛事结果以可核对的材料为依据。发布前请确认事实、图片使用权和平台要求的 AI 标注。

## 从源码启动

需要 Python 3.11+，推荐在 Windows x64 上使用。开发版支持文件任务交接，以下示例启用 DeepSeek 独立模式。

```powershell
git clone https://github.com/YRjuuuuuuuuu/kandian-workbench.git
cd kandian-workbench
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
New-Item -ItemType Directory -Force data/dev | Out-Null
Set-Content data/dev/standalone.deepseek 'DeepSeek API'
.\.venv\Scripts\python.exe -X utf8 -m content_engine serve --data-dir data/dev
```

打开 [本地工作台](http://127.0.0.1:8765)。可以在同一命令后加 `--demo` 初始化合成演示内容。源码模式能使用 DeepSeek 联网找题，测试内置热榜浏览器时请使用独立包或在 Windows 上构建并同步其运行时。工作台只监听回环地址，稿件与配置存放在选定的数据目录。

## 构建和测试

```powershell
python -X utf8 -m unittest discover -s tests -v
node --test tests/editor_state.test.cjs tests/publishing_ui.test.cjs tests/stage_models_ui.test.cjs
python -X utf8 scripts/build_standalone.py
```

Python 测试使用上节依赖，前端测试需要现代 Node.js。Windows 构建脚本编译启动器，下载并校验锁定的运行时，并在 `dist/` 生成 ZIP 与 SHA-256。Windows x64 的首次构建需要联网。发布包的迁移和启停说明见 [Windows 使用手册](docs/windows.md)。

## 源码布局

| 目录 | 内容 |
| --- | --- |
| `content_engine/` | 调度、模型接入、证据、稿件、配图和 Word 导出 |
| `components/hotlists/` | 微博和抖音公开榜单的 Playwright 适配器 |
| `web/` | 工作台、编辑器和预览界面 |
| `modes/`、`skills/` | 赛道配置与编辑规则 |
| `launcher/`、`scripts/` | Windows 启停、构建与验收 |

## 许可

项目源码采用 MIT 许可证。发行包携带的 Python、浏览器及依赖遵循各自的许可证。媒体素材的许可按来源核对。详见 [第三方说明](THIRD_PARTY_NOTICES.md)。
