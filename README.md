# astrbot_plugin_rss_forwarder（中文）

[English](./README.en.md) | [日本語](./README.ja.md)

`astrbot_plugin_rss_forwarder` 是一个面向 AstrBot 的 RSS / RSSHub 推送编排插件，用于从多个订阅源拉取内容，并将结果按可视化配置的路由规则主动推送到指定平台会话（群/频道/私聊）。

## 定位

本项目不是对 `https://github.com/Soulter/astrbot_plugin_rss` 的简单重复实现，当前定位更偏向“RSS 推送编排”：

- 支持 RSS 去重持久化，避免重启后历史内容全量重推。
- 支持在插件面板中图形化定义 feed、target、job 和推送方式。
- 支持首轮启动延迟、历史条目抑制、无效 target 抑制等实际部署问题修复。
- 后续将扩展自动翻译、自动总结、Agent 辅助网页读取与图片提取等能力。


## 更新日志

### v0.4.1

- 调整插件面板中的草稿保存行为：新建 `feed`、`target`、`job` 默认关闭，可先保存未填完的条目。
- 配置校验仅针对启用中的 `feed`、`target`、`job` 与 `daily_digest` 执行，避免半填配置导致插件重载失败。
- `targets[].unified_msg_origin` 的面板提示改为“启用时必须填写”，减少填写过程中的误操作。

### v0.4.0

- 新增 `daily_digests[]` 日报任务，可按每日固定时间汇总指定 RSS 源并发送摘要。
- 日报任务与即时推送分离，未绑定即时推送任务的 feed 也可单独参与日报归档与发送。
- 新增 `/rss digest run [digest_id]`，便于立即验证某个日报任务。
- 日报支持 `text` 与 `image` 两种独立渲染模式，并支持在 GUI 中修改默认提示词模板。

### v0.3.2

- 翻译顺序调整为 `LLM -> Google -> GitHub Models`，其中 GitHub Models 作为第二后备。
- AstrBot 插件面板中的翻译配置说明已与实际顺序保持一致。
- 中文、英文、日文 README 补充了三种翻译服务的配置获取方式。

### v0.3.1

- 翻译链路新增 GitHub Models 回退层，顺序为 LLM -> GitHub Models -> Google。
- 支持从 `data/github.token` 或环境变量读取 GitHub token，用于 GitHub Models 调用。
- 翻译诊断命令新增 GitHub Models 状态输出，便于确认 token、模型与代理配置。

### v0.3.0

- 翻译输出改为中文标题 + 中文摘要，不再出现摘要/翻译分段文本。
- 生产推送翻译链路改为严格串行回退：
  - LLM 成功即停止，不再调用 Google；
  - 仅当 LLM 超时/失败/返回无效时，才回退 Google；
  - 两者都失败时回退到清洗后的原文标题与摘要。
- 新增翻译路径日志：每条推送会记录本次走了 llm / google / fallback 及失败原因，便于排查。
- 新增插件诊断命令：/rss test（别名 test_translate），用于快速验证翻译链路与耗时。
- 增强 RSS 图片链路：支持从 RSS 条目提取原图，并在文本/卡片模式按策略附图。

## 功能

- 支持多 RSS 源（每个源可单独启用/禁用）。
- 支持鉴权：
  - `none`：公开链接；
  - `query`：在 URL 上自动附加 `key`；
  - `header`：通过 `Authorization: Bearer <key>` 发送。
- 支持任务级路由：一个 Job 绑定多个 feed + 多个 target。
- 支持定时执行：`interval_seconds`（已实现）与 `cron`（预留字段，当前回退到 interval）。
- 支持启动首轮延迟：默认在插件启动后等待 `45` 秒再执行第一次轮询，避免平台适配器尚未就绪时抢跑。
- 支持永久去重（推过的内容不再推送）与 feed 状态（ETag/Last-Modified/last_success_time）。
- 支持管理指令：`/rss list`、`/rss status`、`/rss run [job_id]`、`/rss pause [job_id]`、`/rss resume [job_id]`、`/rss reset`（清空去重记录）。
- 支持日报汇总：`daily_digests[]`、`/rss digest run [digest_id]`。
- 支持三级翻译链路：LLM、Google Translate、GitHub Models。
- 支持 text / image 两种渲染模式（image 使用 `html_render`）。

## 与 `astrbot_plugin_rss` 的主要区别

- 更强调“推送编排”而不是基础订阅。
- 已实现去重持久化与重启恢复。
- 已实现可视化 feed/target/job 配置。
- 已实现启动阶段的稳态保护，减少平台未就绪时的误推送和误重试。
- 为后续 LLM/Agent 增强保留了清晰的处理管线。

## 配置（插件面板）

本插件使用 `_conf_schema.json`，可在 AstrBot 插件面板中直接可视化配置：

- `feeds[]`
  - `id`（唯一）
  - `url`
  - `auth_mode`：`none|query|header`
  - `key`
  - `enabled`
  - `timeout`
- `targets[]`
  - `id`（唯一）
  - `platform`
  - `unified_msg_origin`（建议优先）
  - `enabled`
- `jobs[]`
  - `id`（唯一）
  - `feed_ids[]`
  - `target_ids[]`
  - `interval_seconds`（推荐）
  - `cron`（可填，当前版本回退到 interval）
  - `batch_size`
  - `enabled`
- `daily_digests[]`
  - `id`（唯一）
  - `title`
  - `feed_ids[]`
  - `target_ids[]`
  - `send_time`（`HH:MM`）
  - `window_hours`
  - `max_items`
  - `render_mode`（`text|image`）
  - `prompt_template`
  - `enabled`
- 翻译增强（`translation`）
  - `llm_enabled`：是否启用 LLM 摘要/翻译
  - `llm_provider_id`：LLM Provider（WebUI 可下拉选择）
  - `llm_timeout_seconds`：LLM 超时
  - `llm_profile`：LLM profile（高级）
  - `max_input_chars`：传给翻译器的最大输入字符数
  - `llm_proxy_mode` / `llm_proxy_url`：LLM 独立代理（尽力，是否生效取决于 Provider）
  - `google_translate_enabled`：是否启用 Google 翻译第一后备
  - `google_translate_api_key`：Google Cloud Translation API Key
  - `google_translate_target_lang`：目标语言（默认 `zh-CN`）
  - `google_translate_timeout_seconds`：Google 超时
  - `google_translate_proxy_mode` / `google_translate_proxy_url`：Google 独立代理
  - `github_models_enabled`：是否启用 GitHub Models 第二后备
  - `github_models_model`：GitHub Models 模型 ID
  - `github_models_timeout_seconds`：GitHub Models 超时
  - `github_models_token_file`：GitHub token 文件路径，默认按 `data/github.token` 解析
  - `github_models_proxy_mode` / `github_models_proxy_url`：GitHub Models 独立代理
- 其他
  - `startup_delay_seconds`
  - `render_mode`（`text|image`）
  - `summary_max_chars`
  - `render_card_template`

说明：
- 去重记录会同时写入 AstrBot KV 与 `data/plugin_data/astrbot_rss/state.json`
- 若条目发布时间早于该 feed 的 `last_success_time`，插件会仅补记去重而不重复推送
- `startup_delay_seconds` 默认为 `45`，用于给平台适配器和主动消息通道预留启动时间
- `translation` 下的全部字段都可在 AstrBot 插件面板中配置，无需手动修改 JSON 文件
- `daily_digests` 与 `jobs` 相互独立；只配置日报时，不会自动生成即时推送任务
- 日报默认在窗口内无条目时跳过发送，并在状态中记录 `empty_window`

## 示例配置

```json
{
  "feeds": [
    {
      "id": "rsshub_it",
      "url": "https://rsshub.example.com/36kr/newsflash",
      "auth_mode": "query",
      "key": "YOUR_RSSHUB_KEY",
      "enabled": true,
      "timeout": 10
    }
  ],
  "targets": [
    {
      "id": "tg_group_a",
      "platform": "telegram",
      "unified_msg_origin": "telegram:group:xxxx",
      "enabled": true
    }
  ],
  "jobs": [
    {
      "id": "it_news",
      "feed_ids": ["rsshub_it"],
      "target_ids": ["tg_group_a"],
      "interval_seconds": 300,
      "batch_size": 10,
      "enabled": true
    }
  ],
  "daily_digests": [
    {
      "id": "daily_chip_cn",
      "title": "芯片日报",
      "feed_ids": ["rsshub_it"],
      "target_ids": ["tg_group_a"],
      "send_time": "09:00",
      "window_hours": 24,
      "max_items": 20,
      "render_mode": "text",
      "prompt_template": "请根据以下 RSS 条目生成一份简体中文日报，严格输出纯文本编号列表。\\n要求：\\n1) 只输出编号列表，不要导语、总结、分类标题或 Markdown 代码块；\\n2) 最多输出 {max_items} 条；\\n3) 每条一句话，优先保留来源信息；\\n4) 如果多条内容高度相近，可合并为一条更准确的概述。\\n\\n统计窗口：{window_start} 至 {window_end}\\n条目：\\n{items}",
      "enabled": true
    }
  ],
  "translation": {
    "llm_enabled": true,
    "llm_provider_id": "",
    "llm_timeout_seconds": 15,
    "llm_profile": "rss_enrich",
    "max_input_chars": 2000,
    "google_translate_enabled": true,
    "google_translate_api_key": "YOUR_GOOGLE_TRANSLATE_API_KEY",
    "google_translate_target_lang": "zh-CN",
    "google_translate_timeout_seconds": 15,
    "google_translate_proxy_mode": "system",
    "google_translate_proxy_url": "",
    "github_models_enabled": true,
    "github_models_model": "openai/gpt-4o-mini",
    "github_models_timeout_seconds": 15,
    "github_models_token_file": "github.token"
  },
  "render_mode": "text"
}
```


## 安装与环境依赖说明

### 1) 已修复的面板安装报错

若你遇到 `ModuleNotFoundError: No module named 'commands'`，这是由于旧版本插件使用了顶层导入方式（`from commands import ...`）导致的。

本仓库已修复为包内相对导入（`from .commands import ...`），可被 AstrBot 面板按 `astrbot_rss.main` 正确加载。

### 2) 依赖对比（相对 AstrBot 默认环境）

本插件核心功能仅依赖：
- AstrBot 运行时（由 AstrBot 主程序提供）
- Python 标准库（`asyncio`、`urllib`、`xml`、`json` 等）

**结论：本插件没有必须额外 `pip install` 的第三方 Python 依赖。**

### 3) 可选能力说明

- `render_mode = image` 时，依赖 AstrBot 侧提供的 `html_render` 能力。
- `llm_enabled = true` 时，依赖 AstrBot 已配置可用的大模型提供商。
- `google_translate_enabled = true` 时，依赖 Google Cloud Translation API Key。
- `github_models_enabled = true` 时，默认从 `data/github.token` 读取 GitHub token，也可使用 `ASTRBOT_GITHUB_TOKEN`、`GITHUB_TOKEN` 或 `GH_TOKEN`。
- 翻译顺序：LLM -> Google -> GitHub Models。若仅开启其中一层，则直接使用该层。

若上述 AstrBot 能力未配置，本插件会记录日志并自动降级，不影响基础 RSS 文本推送。

## 翻译服务获取方式

### 1）AstrBot LLM Provider

在 AstrBot 主程序中先配置可用模型提供商，再进入插件面板的 `translation.llm_provider_id` 选择对应提供商。若留空，插件会尝试使用当前会话或默认模型。

### 2）Google Cloud Translation API Key

在 Google Cloud Console 创建项目，启用 `Cloud Translation API` 的 Basic v2，再在 `APIs & Services -> Credentials` 中创建 API Key。创建完成后，将 Key 填入插件面板的 `translation.google_translate_api_key`。

### 3）GitHub Models Token

在 GitHub 账户中创建带有 `models` 权限的 token，推荐放入 AstrBot `data` 目录映射的 `github.token` 文件，也可以通过环境变量 `ASTRBOT_GITHUB_TOKEN`、`GITHUB_TOKEN` 或 `GH_TOKEN` 提供。插件面板中的 `translation.github_models_token_file` 默认值就是 `github.token`。

## 开发参考

- Getting Started: https://docs.astrbot.app/dev/star/plugin-new.html
- Guides:
  - simple / listen-message-event / send-message / plugin-config
  - ai / storage / html-to-pic / session-control / other

## 路线图

见 [ROADMAP.md](./ROADMAP.md)。

## 已知限制

- 当前未实现真正的 cron 调度器（配置 `cron` 时会回退到最小 interval 轮询）。
- 主动消息依赖平台能力，若平台不支持会记录错误日志。

## 日报任务建议

`daily_digests[]` 适合用于每天定时汇总一个或多个 feed 的重点条目。常见配置方式如下：

- `send_time` 设为固定日报时间，例如 `09:00`
- `window_hours` 设为 `24` 或 `72`
- `render_mode=text` 适合链接较多的场景
- `render_mode=image` 适合群内阅读体验优先的场景
- `prompt_template` 保持默认值即可开箱使用，若希望偏重某类信息，可在 GUI 中按需调整
