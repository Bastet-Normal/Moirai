# 📖 Moirai - 中文长篇小说创作台

在自己的电脑上写长篇。你可以把已有稿件导入书桌，逐章修改，沿人物和线索查回原文，最后导出交稿文件。手写、检索、人工审阅和导出都能离线使用。需要续写或自动审稿时，再接入模型。

## 打开书桌

1. 安装 Python 3.11 或更高版本。在[发行页](https://github.com/Bastet-Normal/Moirai/releases/latest)下载附件中的 `Moirai-Studio-*.zip`，解压到准备长期保存的目录。
2. macOS 双击 `scripts/launch.command`，Windows 双击 `scripts/launch.bat`。Linux 可在解压目录运行 `python3 scripts/launch.py`。
3. 首次启动需要联网安装锁定依赖。启动器会打开系统默认浏览器，正式书桌的默认地址为 `http://127.0.0.1:8000/`。使用期间请保持启动器窗口开启。

想先熟悉界面，可以在项目目录运行以下命令。演示书桌在独立临时目录打开《雾港来信》和《群星航路》，默认端口为 8766。退出时清理样书，正式作品请在正式书桌中创作。

```sh
python3 scripts/launch.py --demo
```

启动器支持 `--port` 和 `--no-browser`。已经安装项目依赖的开发环境可以加 `--current-python` 使用当前解释器。

## 从存稿到交稿

| 工作 | 在书桌里怎么做 |
| --- | --- |
| 导入作品 | 选择「导入存稿」，上传 DOCX、UTF-8 TXT、Markdown 或粘贴正文。在预览里核对章名、分章和顺序，再导入作品库。 |
| 阅读和改稿 | 从书桌继续上次阅读，或通过作品库切换。宽屏可向左收起作品库，窄屏使用书架抽屉。章节里可编辑整章，或打开「工具」逐段修改。 |
| 追踪设定 | 实体图谱用机器人标记人物，用不同图标区分地点、物品和线索。选中实体可看直接关系，跳到对应的人物卡或伏笔。人物卡先展示关键设定，详情中可查完整状态。 |
| 审阅正文 | 「审阅」分别记录机器检查与人工结论。跨章问题可点开两端引文核对上下文。人工结论对应保存时的正文版本，改稿后可在历史里回看。 |
| 交稿和备份 | 「交稿预览」可检查排版，下载整本 DOCX 或分章 TXT ZIP。作品菜单还提供 Markdown、TXT、EPUB 和完整项目备份；迁移电脑或升级前可导出完整备份。 |

**草稿和正式稿分别保存。** 编辑中的草稿保存在当前浏览器，使用同一浏览器和访问地址可恢复。点击「保存」才会写入作品目录。如果另一窗口已经改过同一作品，先对照新旧稿，再选择要保留的内容。「章节版本」可对比和恢复最近 30 份旧稿。

**作品存储。** 默认作品库在 `novel_projects/`，可用 `OUTPUT_DIR` 指向已有作品库。更换发行目录后，请让新版使用原作品库，或导入完整备份。误删的作品可从回收站恢复。历史版本留下的 `.novelengine_*` 目录用于加锁、回收和故障恢复，请随作品库一起保留。

## 使用模型

在「模型与 API」中设置 OpenAI 兼容服务的地址、密钥和模型名，也可参照 `.env.example` 配置项目根目录的 `.env`。主笔、策划、质检和状态提取可以分别指定模型。密钥只填在自己电脑上；启动器默认只监听本机回环地址。

续写、自动审稿、提取设定等功能会向所配置的服务发送相应稿件材料。运行前可在「查看模型与用量」核对范围和估算。正文有手动修改时，按提示补录状态，再继续依赖人物和时间线的自动写作。短剧、演播和宣发可按需生成。

## 命令行与 Python

以下命令使用已安装项目依赖的 Python。新建时给作品一个唯一标识；后续命令使用同一个标识。

```sh
python main.py plan --name fog_harbor --idea "一封退回的信寄自消失多年的码头" --genre 悬疑
python main.py continue-gen --name fog_harbor --count 3
python main.py status --name fog_harbor
python main.py export --name fog_harbor
```

`resume-plan` 可接续中断的策划，`chapter --num N --rewrite` 可显式重写已有章节，`list` 列出作品。完整参数见 `python main.py --help` 及各子命令的 `--help`。在 Python 中可从 `pipeline.novel_pipeline` 导入 `NovelPipeline`，调用同一套工作流。

## 架构与开发

| 位置 | 职责 |
| --- | --- |
| `web/` | 写作书桌、编辑器和审阅界面。图谱由独立的 `web/assets/entity-graph.js` 绘制，并与人物卡共用头像。字体、图库和样式随发行包提供。 |
| `web_ui.py`、`api/` | 本机 HTTP 入口、输入契约、请求限制和下载响应。 |
| `services/`、`pipeline/`、`agents/` | 后台任务、章节工作流和各写作角色的模型调用。 |
| `core/` | 作品状态、正文版本、图谱、审稿证据、事务、检索与导出。CLI 和网页共用这层读写作品。 |
| `scripts/`、`tests/` | 启动、隔离演示、构建发行包，以及后端、界面状态和原生 Safari 回归。 |

版本冲突、快照恢复和派生结果失效的设计详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

从源码开发时，使用 Python 3.11+ 和 Node.js 安装锁定依赖。下列 `python` 指向当前已激活的开发虚拟环境。

```sh
python -m pip install --require-hashes -r requirements-dev.lock
npm ci --ignore-scripts
npm run build:web
python scripts/check.py
```

质量检查涵盖后端测试、前端状态测试、静态检查和依赖一致性。加 `--audit` 可联网核查 Python 锁文件，`npm audit --audit-level=high` 核查前端依赖。macOS 首次启用 Safari 网页自动化后可运行 `npm run test:browser`，在隔离样书上验证实际操作。

从通过检查的源码生成可分发的 ZIP：

```sh
python scripts/package_release.py Moirai-Studio.zip
```
