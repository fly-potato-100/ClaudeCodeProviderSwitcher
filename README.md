# Claude Code Provider 切换脚本使用说明

## 功能简介
`switch_claude_provider.py` 通过统一的 TOML 配置文件，将不同模型服务商（provider）的环境变量写入 Claude Code 的 `settings.json`。相比直接复制多个 `settings.json.xxx` 备份，它具备：
- 交互式与参数化两种启动方式；
- 配置继承（bases + providers），便于复用公共片段；
- 按策略清理旧 provider 的环境变量；
- 自动备份与原子写入，降低误操作风险；
- `--dry-run` 预览变更，避免意外覆盖；
- 敏感信息自动脱敏显示，保障配置安全；
- 智能备份轮转，自动清理历史备份文件。

## 环境要求
- Python 3.11 及以上；或 Python 3.9/3.10 + `tomli`。
- 建议在项目根目录 `G:\scp` 下运行。

如果您在 WSL 或旧环境中使用 Python 3.9/3.10，而又不想升级 Python 版本，可先安装轻量兼容库：

```bash
python3 -m pip install --user tomli
```

## TOML 配置结构
参考示例文件：`providers.toml.example`
- `bases.*`：可复用的"基类"。支持 `extends`，适合将公共 env 拆出。
- `providers.*`：可选择的实际 provider。字段说明：
  - `enable`：是否可用；交互式和 `--list` 只展示可用项。
  - `brief`：简短说明，展示在列表中。
  - `extends`：继承的基类或其他 provider。
  - `env`：最终需要写入 `settings.json` 的键值对。
- 所有 `env` 键将被转换为字符串，`None` 会写成空字符串。

## 快速开始

### 1. 复制并编辑配置文件
```powershell
copy .\providers.toml.example .\providers.toml
# 编辑 providers.toml，填入真实的 API keys
```

### 2. 查看可用的 providers
```powershell
python .\switch_claude_provider.py --list
```

### 3. 切换 provider（交互模式）
```powershell
python .\switch_claude_provider.py
```

### 4. 切换 provider（参数模式）
```powershell
python .\switch_claude_provider.py --provider claude_from_zulong
```

## 常用命令

### PowerShell（Windows）
```powershell
# 列出所有可用的 providers
python .\switch_claude_provider.py --list

# 交互式选择 provider
python .\switch_claude_provider.py

# 直接指定 provider
python .\switch_claude_provider.py --provider qwen3_from_modelscope

# 指定配置文件和输出路径
python .\switch_claude_provider.py --input .\providers.toml --output "$HOME/.claude/settings.json"

# 预览变更（不实际写入文件）
python .\switch_claude_provider.py --provider claude_from_zulong --dry-run

# 使用不同的清理策略
python .\switch_claude_provider.py --provider claude_from_zulong --clean all-known
```

### Bash/WSL
```bash
# 列出所有可用的 providers
python3 ./switch_claude_provider.py --list

# 交互式选择 provider
python3 ./switch_claude_provider.py

# 直接指定 provider
python3 ./switch_claude_provider.py --provider qwen3_from_modelscope
```

## 关键参数说明

- `--input`：TOML 配置路径，默认为脚本同目录的 `providers.toml`。
- `--output`：目标 `settings.json` 输出路径，默认为 `~/.claude/settings.json`。
- `--provider`：直接指定要切换的 provider 标识。未指定时进入交互模式。
- `--clean`：清理策略（默认 `previous`）：
  - `previous`：移除上一次 provider 的环境变量（依赖 `CLAUDE_PROVIDER` 记录；无法识别时仅覆盖冲突键）。
  - `all-known`：移除所有已知 provider 的环境变量键。
  - `none`：不主动清理，仅覆盖冲突键。
- `--no-backup`：禁用备份。默认会生成 `settings.json.YYYYMMDD_HHMMSS.bak` 文件。
- `--dry-run`：仅输出变更摘要，不写文件。
- `--list`：列出所有启用的 provider 后退出。

## 工作流程
1. 解析 TOML 配置文件，按继承关系合成每个 provider 的环境变量。
2. 读取目标 settings.json 文件（若不存在则视为空配置）。
3. 根据 `--clean` 策略移除旧 provider 的环境变量，同时写入 `env.CLAUDE_PROVIDER = <provider_id>` 标记当前 provider。
4. `--dry-run` 模式会显示详细的环境变量对比表格（敏感信息自动脱敏）；否则执行备份并原子写入。

## 安全特性
- **敏感信息脱敏**：包含 key、secret、token、password 等关键词的环境变量值在显示时会被自动脱敏。
- **原子写入**：使用临时文件确保写入过程的原子性，避免文件损坏。
- **自动备份**：写入前自动创建时间戳备份文件。

## 最佳实践
1. **配置管理**：
   - 使用环境变量或独立的 secrets 管理工具来配置敏感信息，再引用到 TOML 文件中。
   - 在共享配置时，保留 `providers.toml.example` 中的虚拟占位符，真实密钥可在运行前注入。

2. **操作建议**：
   - 首次使用或切换到新 provider 前，建议先使用 `--dry-run` 预览变更。
   - 定期清理备份文件，或依赖自动轮转机制（保留最近5个备份）。

3. **故障排除**：
   - 如果切换失败，可从自动生成的 `.bak` 备份文件恢复配置。
   - 查看详细错误信息，检查 TOML 配置文件格式和 provider 定义。

## 示例配置说明
项目提供的 `providers.toml.example` 包含了两个实际可用的 provider 示例：
- `claude_from_zulong`：祖龙搭建的 Claude Code 代理
- `qwen3_from_modelscope`：ModelScope 上的 Qwen3

您可以根据需要修改、扩展这些配置，或添加新的 provider。


