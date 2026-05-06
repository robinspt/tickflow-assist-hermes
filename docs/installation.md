# TickFlow Assist Hermes 安装指南

## 前置条件

- Python `>=3.10`
- Debian/Ubuntu 如缺少 venv 支持，先安装 `python3-venv`
- Hermes Agent
- TickFlow API Key
- OpenAI-compatible LLM API Key
- 可选：东方财富妙想 Skills API Key
- 可选：金十数据 MCP API Token

## 安装源码插件

推荐方式：

```bash
git clone https://github.com/robinspt/tickflow-assist-hermes.git
cd tickflow-assist-hermes
./setup-tickflow.sh
```

脚本会：

- 检查 Python 版本是否为 `>=3.10`
- 优先使用 Hermes 自己的 Python 创建项目内 `.venv`
- 执行 `.venv/bin/python -m pip install -e .`
- 写入 `.tickflow-assist-venv`，供 Hermes 运行时定位实际虚拟环境
- 创建或更新 `~/.hermes/plugins/tickflow-assist` 符号链接
- 清理本项目旧的 `~/.hermes/skills/ta*` 符号链接，避免继续通过 skill 触发
- 交互式生成或更新 `local.config.json`

如果项目目录不可写，脚本会自动改用 `~/.local/share/tickflow-assist-hermes/venv`。也可以显式指定虚拟环境目录：

```bash
TICKFLOW_ASSIST_VENV=~/.local/share/tickflow-assist-hermes/venv ./setup-tickflow.sh
```

如果 Hermes 安装在非默认路径，可以显式指定 Hermes Python；也可以指定插件目录：

```bash
HERMES_PYTHON=/path/to/hermes/venv/bin/python3 HERMES_PLUGIN_DIR=/path/to/hermes/plugins ./setup-tickflow.sh
```

手动安装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
mkdir -p ~/.hermes/plugins
ln -s "$(pwd)" ~/.hermes/plugins/tickflow-assist
```

如果曾安装过旧的 `/ta`、`/ta-*` 或 `/ta_*` skill 链接，重新运行脚本会清理指向本项目的旧符号链接，避免命令通过 skill 慢路径触发。

如果创建虚拟环境时报错，Debian/Ubuntu 可先安装：

```bash
sudo apt install python3-venv
```

如果报 `Permission denied: .venv`，说明项目目录或已有 `.venv` 权限不属于当前用户，可直接使用上面的 `TICKFLOW_ASSIST_VENV=...` 方式。

Hermes 插件默认是 opt-in。源码目录链接到 `~/.hermes/plugins/tickflow-assist` 后，还需要显式启用插件：

```bash
hermes plugins enable tickflow-assist
hermes plugins list
hermes gateway restart
```

也可以运行 `hermes plugins` 打开交互式插件管理界面，用空格勾选 `tickflow-assist`。

重启 Hermes gateway 后，Hermes chat / CLI 的 `/plugins` 应能看到 `tickflow-assist`。Telegram / Discord 消息端可用 `/commands` 查看 gateway 当前命令；如果 `/ta_*` 不在列表里，说明 gateway 尚未加载插件命令，执行 `hermes gateway restart` 或在聊天里用 `/restart`。Discord 全局命令可能有短暂传播延迟。

如果 `/plugins` 正常，但命令执行时报缺少 `lancedb`、`pandas`、`pyarrow` 等 Python 依赖，请先重新执行 `./setup-tickflow.sh` 并重启 Hermes。脚本会优先使用 Hermes 自己的 Python，并在已有 `.venv` Python 版本不匹配时自动挪走旧环境后重建；仍失败时运行 `/ta_debug`，把其中的 Python、虚拟环境记录、依赖状态和 Python 路径用于排查。

### Discord / Telegram 命令验证

插件提供独立命令 `/ta_addstock`、`/ta_analyze`、`/ta_watchlist` 等。它们由 Hermes 插件 `register_command` 注册，handler 直接调用工具并返回 `text`，不会加载 skill，也不会走模型规划。

插件还会注册同名 `ta-*` 兼容别名，用于适配 Hermes gateway 在 Telegram 分发插件命令时将下划线转换为连字符的查找逻辑；Telegram 菜单仍显示 `/ta_*`，实际执行仍走同一个直接 handler。

- Hermes chat / CLI：直接选择或输入 `/ta_addstock`、`/ta_analyze`、`/ta_testalert` 等。
- Telegram / Discord：先用 `/commands` 确认列表中有 `/ta_watchlist`、`/ta_testalert` 等命令，然后直接发送 `/ta_*`。
- 如果 Telegram 回复 `Unknown command /ta_watchlist`，不是插件 handler 报错，而是 gateway 在进入插件前没有识别该命令；先重启 gateway，确认消息端和 Hermes chat / CLI 使用同一个 Hermes profile / `$HOME`，必要时升级 Hermes 到包含插件 `register_command()` gateway 支持的版本。

如果 Discord 里仍看不到命令：

- 先看 Discord `/` 菜单是否有 Hermes 内置 `/model`。如果内置命令也没有，通常是 bot 没有用 `applications.commands` scope 邀请，或 `DISCORD_COMMAND_SYNC_POLICY=off` / `slash_commands: false` 禁用了同步。
- 如果内置命令存在但 `/ta_*` 不存在，说明当前 gateway 没有加载插件命令；检查插件是否启用、gateway 是否重启，以及消息端 gateway 是否运行在同一个 `~/.hermes`。
- 如必须要 Discord 菜单可选择且 Hermes 版本不支持插件命令同步，只能使用 skill 入口，但会有额外加载和 token 开销。

## 配置

推荐使用 `setup-tickflow.sh` 生成 `local.config.json`，插件会读取其中的 `plugin` 字段。也可以写入 Hermes 的 `.env`：

```bash
TICKFLOW_ASSIST_TICKFLOW_API_KEY=your-tickflow-key
TICKFLOW_ASSIST_TICKFLOW_API_KEY_LEVEL=Free
TICKFLOW_ASSIST_LLM_BASE_URL=https://api.openai.com/v1
TICKFLOW_ASSIST_LLM_API_KEY=sk-xxx
TICKFLOW_ASSIST_LLM_MODEL=gpt-4o
TICKFLOW_ASSIST_DATABASE_PATH=/path/to/tickflow-assist-hermes/data/lancedb
TICKFLOW_ASSIST_ALERT_DELIVERY_TARGET=telegram
TICKFLOW_ASSIST_ALERT_IMAGE_ENABLED=true
```

可选配置：

```bash
TICKFLOW_ASSIST_MX_SEARCH_API_KEY=mkt_xxx
TICKFLOW_ASSIST_JIN10_API_TOKEN=jin10_xxx
```

如果手动复制 `local.config.example.json` 为 `local.config.json`，请只在本机填写真实密钥；`local.config.json` 已被 Git 忽略。

### 告警投递目标

`alertDeliveryTarget` 参考 Hermes delivery target 格式：

- `telegram`：Telegram home channel
- `telegram:-1001234567890`：指定 Telegram 群
- `telegram:-1001234567890:17585`：指定 Telegram topic
- `discord:999888777`：指定 Discord channel
- `slack`：Slack home channel
- `sms:+15551234567`：指定手机号

监控和测试告警通过 Hermes 内置 `send_message` 工具发送，实际可用目标取决于 Hermes gateway 中已配置的平台和 home channel。PNG 告警卡使用 Hermes 支持的 `MEDIA:/path/to/file` 格式附加到消息中。

本项目只使用 Hermes 格式；请不要再配置旧的通道/目标拆分字段。

## LanceDB

数据库继续使用 LanceDB，默认路径 `./data/lancedb`。如果你从旧项目复制了数据目录，保持 `databasePath` 或 `TICKFLOW_ASSIST_DATABASE_PATH` 指向同一目录即可。

插件不会改动表名和字段；Python 版只会按原 schema 创建缺失表。
