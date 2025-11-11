# Claude Code Provider 切换脚本使用说明

## 功能简介
`switch_claude_provider.py` 通过统一的 TOML 配置文件，将不同模型服务商（provider）的环境变量写入 Claude Code 的 `settings.json`。相比直接复制多个 `settings.json.xxx` 备份，它具备：
- 交互式与参数化两种启动方式；
- 配置继承（bases + providers），便于复用公共片段；
- 按策略清理旧 provider 的环境变量；
- 自动备份与原子写入，降低误操作风险；
- `--dry-run` 预览变更，避免意外覆盖。

## 环境要求
- Python 3.11 及以上（依赖标准库 `tomllib`）。
- 运行目录建议为脚本所在的 `G:\ask_ai\20251111`。

## TOML 配置结构
示例文件：`providers.toml`
- `bases.*`：可复用的“基类”。支持 `extends`，适合将公共 env 拆出。
- `providers.*`：可选的实际 provider。字段说明：
  - `enable`：是否可用；交互式和 `--list` 只展示可用项。
  - `brief`：简短说明，展示在列表中。
  - `extends`：继承的基类或其他 provider。
  - `env`：最终需要写入 `settings.json` 的键值对。
- 所有 `env` 键将被转换为串，`None` 会写成空串。

## 常用命令
PowerShell（Windows）：
```powershell
python .\switch_claude_provider.py --list
python .\switch_claude_provider.py --provider claude
python .\switch_claude_provider.py --input .\providers.toml --output "$HOME/.claude/settings.json"
```

Bash/WSL：
```bash
python3 ./switch_claude_provider.py --list
python3 ./switch_claude_provider.py --provider openai
```

未指定 `--provider` 时进入交互模式，按提示选择编号或直接输入 provider id。

## 关键参数
- `--input`：TOML 配置路径，默认为脚本同目录的 `providers.toml`。
- `--output`：目标 `settings.json` 输出路径，默认为 `~/.claude/settings.json`。
- `--clean`：清理策略（默认 `previous`）：
  - `previous`：移除上一次 provider 的 env（依赖 `CLAUDE_PROVIDER` 记录；无法识别时仅覆盖冲突键）。
  - `all-known`：移除所有已知 provider 的 env 键。
  - `none`：不主动清理，仅覆盖冲突键。
- `--no-backup`：禁用备份。默认会生成 `settings.json.YYYYMMDD_HHMMSS.bak`。
- `--dry-run`：只输出变更摘要，不写文件。
- `--list`：列出所有启用的 provider 后退出。

## 工作流程
1. 解析 TOML，按继承合成每个 provider 的 env。
2. 读取输出文件（若不存在则视为 `{"env":{}}`）。
3. 根据 `--clean` 策略移除旧 provider 的 env 键，同时始终写入 `env.CLAUDE_PROVIDER = <provider_id>`。
4. `--dry-run` 会打印新增、变更、移除的 env 键；否则执行备份并原子写入。

## 建议操作
- 配置敏感信息时可使用环境变量或独立的 secrets 管理工具，再写入 TOML。
- 若要共享配置，保留 `providers.toml` 中的虚拟占位符，真实 key 可运行前再注入。
- 建议先 `--dry-run` 审核变更，再进行正式写入。


