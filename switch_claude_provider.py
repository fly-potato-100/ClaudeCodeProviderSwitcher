#!/usr/bin/env python3

import argparse
import importlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    try:
        tomllib = importlib.import_module("tomli")
    except ModuleNotFoundError:
        print(
            "需要 Python 3.11+（内置 tomllib），或在 Python 3.9/3.10 下先安装 tomli：python -m pip install tomli",
            file=sys.stderr,
        )
        sys.exit(1)


CLAUDE_PROVIDER_KEY = "__CLAUDE_SWITCHER_PROVIDER"


class ConfigError(Exception):
    """输入配置（providers.toml 或 settings.json）错误。"""


@dataclass
class Definition:
    identifier: str
    kind: str  # "base" or "provider"
    brief: Optional[str]
    enable: bool
    extends: List[str]
    env: Dict[str, str]


def default_input_path() -> Path:
    return Path(__file__).resolve().with_name("providers.toml")


def default_output_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Claude Code provider 环境变量切换脚本（基于 TOML 配置）。"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=default_input_path(),
        help="TOML 输入文件路径，默认为脚本所在目录下的 providers.toml。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output_path(),
        help="目标 settings.json 文件路径，默认为 ~/.claude/settings.json。",
    )
    parser.add_argument(
        "--provider",
        type=str,
        help="要切换的 provider 标识（providers.<id> 中的 <id>）。未提供时进入交互式选择。",
    )
    parser.add_argument(
        "--clean",
        choices=("previous", "all-known", "none"),
        default="previous",
        help=(
            "清理策略：previous=移除上一次 provider 的 env；all-known=移除所有 provider 的 env；"
            "none=不额外清理，仅覆盖冲突键。"
        ),
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="写入前不生成备份。默认会在目标同目录生成时间戳后缀的 .bak 文件。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅展示变更摘要，不写入文件。",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="列出当前启用（enable=true）的 provider 后退出。",
    )
    return parser.parse_args()


def ensure_list(value: object, what: str, identifier: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ConfigError(f"{what} 应为字符串或字符串列表（定义：{identifier}）。")


def ensure_env_table(value: object, identifier: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{identifier} 的 env 必须是一个表（key/value 对）。")
    env: Dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str):
            raise ConfigError(f"{identifier} 的 env 键必须是字符串。")
        env[key] = "" if raw is None else str(raw)
    return env


def build_definitions(path: Path) -> Tuple[Dict[str, Definition], Dict[str, Definition]]:
    if not path.exists():
        raise ConfigError(f"找不到输入文件：{path}")
    try:
        with path.open("rb") as fp:
            data = tomllib.load(fp)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML 解析失败：{exc}") from exc

    bases_section = data.get("bases", {})
    providers_section = data.get("providers", {})
    if not isinstance(bases_section, dict):
        raise ConfigError("bases 必须是一个表。")
    if not isinstance(providers_section, dict) or not providers_section:
        raise ConfigError("providers 必须是一个非空表。")

    bases: Dict[str, Definition] = {}
    for base_id, raw in bases_section.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"bases.{base_id} 必须是一个表。")
        extends = ensure_list(raw.get("extends"), "bases extends", f"bases.{base_id}")
        brief = raw.get("brief")
        if brief is not None and not isinstance(brief, str):
            raise ConfigError(f"bases.{base_id}.brief 必须是字符串。")
        env = ensure_env_table(raw.get("env"), f"bases.{base_id}")
        bases[base_id] = Definition(
            identifier=base_id,
            kind="base",
            brief=brief,
            enable=True,
            extends=extends,
            env=env,
        )

    providers: Dict[str, Definition] = {}
    for provider_id, raw in providers_section.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"providers.{provider_id} 必须是一个表。")
        extends = ensure_list(raw.get("extends"), "providers extends", f"providers.{provider_id}")
        brief = raw.get("brief")
        if brief is not None and not isinstance(brief, str):
            raise ConfigError(f"providers.{provider_id}.brief 必须是字符串。")
        enable_raw = raw.get("enable", True)
        enable = bool(enable_raw)
        env = ensure_env_table(raw.get("env"), f"providers.{provider_id}")
        providers[provider_id] = Definition(
            identifier=provider_id,
            kind="provider",
            brief=brief,
            enable=enable,
            extends=extends,
            env=env,
        )

    return bases, providers


def resolve_envs(
    bases: Dict[str, Definition], providers: Dict[str, Definition]
) -> Dict[str, Dict[str, str]]:
    all_nodes: Dict[str, Definition] = {**bases, **providers}

    # 验证继承范围
    for base in bases.values():
        for parent in base.extends:
            if parent not in bases:
                raise ConfigError(f"bases.{base.identifier} 只能继承其他 bases，未找到：{parent}")
    for provider in providers.values():
        for parent in provider.extends:
            if parent not in all_nodes:
                raise ConfigError(f"providers.{provider.identifier} 继承未知定义：{parent}")

    memo: Dict[str, Dict[str, str]] = {}
    visiting: Set[str] = set()

    def dfs(node_id: str) -> Dict[str, str]:
        if node_id in memo:
            return memo[node_id]
        if node_id in visiting:
            raise ConfigError(f"检测到继承循环，涉及：{node_id}")
        visiting.add(node_id)
        node = all_nodes[node_id]
        merged: Dict[str, str] = {}
        for parent_id in node.extends:
            parent_env = dfs(parent_id)
            merged.update(parent_env)
        merged.update(node.env)
        visiting.remove(node_id)
        memo[node_id] = merged
        return merged

    resolved: Dict[str, Dict[str, str]] = {}
    for provider_id in providers:
        resolved[provider_id] = dict(dfs(provider_id))
    return resolved


def _collect_providers(providers: Dict[str, Definition], only_enabled: bool = True) -> List[Definition]:
    entries = [p for p in providers.values() if (p.enable or not only_enabled)]
    entries.sort(key=lambda p: p.identifier)
    return entries


def _format_provider_table(entries: List[Definition]) -> List[str]:
    if not entries:
        return []
    id_cells = [f"[{i}]" for i in range(1, len(entries) + 1)]
    name_cells = [p.identifier for p in entries]
    brief_cells = [p.brief or "" for p in entries]

    id_width = max(len("ID"), *(len(cell) for cell in id_cells))
    name_width = max(len("name"), *(len(cell) for cell in name_cells))

    header = f"{'ID'.ljust(id_width)}  {'name'.ljust(name_width)}  brief"
    lines: List[str] = [header, "-" * len(header)]
    for id_cell, name_cell, brief_cell in zip(id_cells, name_cells, brief_cells):
        lines.append(f"{id_cell.ljust(id_width)}  {name_cell.ljust(name_width)}  {brief_cell}")
    return lines


def print_provider_list(providers: Dict[str, Definition]) -> None:
    entries = _collect_providers(providers, only_enabled=True)
    if not entries:
        print("当前配置中没有 enable=true 的 provider。")
        return
    for line in _format_provider_table(entries):
        print(line)


def select_provider_interactively(providers: Dict[str, Definition]) -> str:
    entries = _collect_providers(providers, only_enabled=True)
    if not entries:
        raise ConfigError("没有 enable=true 的 provider 可供选择，请检查配置。")
    for line in _format_provider_table(entries):
        print(line)
    while True:
        user_input = input("请输入编号或 provider id：").strip()
        if not user_input:
            continue
        if user_input.isdigit():
            index = int(user_input) - 1
            if 0 <= index < len(entries):
                return entries[index].identifier
        provider = providers.get(user_input)
        if provider:
            if not provider.enable:
                print(f"provider '{user_input}' 已被禁用，请重新选择。")
                continue
            return provider.identifier
        print("输入无效，请重新输入。")


def load_settings(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"settings.json 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("settings.json 顶层必须是一个对象。")
    return data


def determine_removal_keys(
    strategy: str,
    previous_provider: Optional[str],
    resolved_envs: Dict[str, Dict[str, str]],
    new_env: Dict[str, str],
) -> Set[str]:
    keys: Set[str] = set()
    if strategy == "none":
        return keys
    if strategy == "all-known":
        for env in resolved_envs.values():
            keys.update(env.keys())
        keys.add(CLAUDE_PROVIDER_KEY)
        return keys
    if strategy == "previous":
        if previous_provider and previous_provider in resolved_envs:
            keys.update(resolved_envs[previous_provider].keys())
        else:
            keys.update(new_env.keys())
        keys.add(CLAUDE_PROVIDER_KEY)
        return keys
    raise ConfigError(f"未知的清理策略：{strategy}")


def apply_provider_env(
    settings: Dict[str, object],
    provider_id: str,
    new_env: Dict[str, str],
    clean_strategy: str,
    resolved_envs: Dict[str, Dict[str, str]],
) -> Tuple[Dict[str, object], Optional[str]]:
    env_section = settings.get("env")
    if isinstance(env_section, dict):
        current_env = {str(k): "" if v is None else str(v) for k, v in env_section.items()}
    else:
        current_env = {}

    previous_provider = current_env.get(CLAUDE_PROVIDER_KEY)
    if not isinstance(previous_provider, str):
        previous_provider = None

    removal_keys = determine_removal_keys(clean_strategy, previous_provider, resolved_envs, new_env)
    for key in removal_keys:
        current_env.pop(key, None)

    for key, value in new_env.items():
        current_env[key] = value
    current_env[CLAUDE_PROVIDER_KEY] = provider_id

    settings["env"] = current_env

    return settings, previous_provider


def make_backup(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.{timestamp}.bak")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("rb") as src, backup_path.open("wb") as dst:
        dst.write(src.read())
    return backup_path


def rotate_backups(path: Path, keep: int = 5) -> List[Path]:
    """
    仅保留最近 keep 个备份（按修改时间倒序），删除更旧的备份。
    备份文件命名约定：<filename>.<timestamp>.bak
    """
    parent = path.parent
    prefix = f"{path.name}."
    suffix = ".bak"
    if not parent.exists():
        return []
    candidates = [
        p for p in parent.iterdir()
        if p.is_file() and p.name.startswith(prefix) and p.name.endswith(suffix)
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed: List[Path] = []
    for old in candidates[keep:]:
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            pass
    return removed


def atomic_write_json(path: Path, data: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dumps = json.dumps(data, ensure_ascii=False, indent=2)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(dumps)
            tmp_file.write("\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def mask_sensitive_value(var_name: str, value: str, max_len: int = 50) -> str:
    """
    对敏感环境变量值进行 mask 处理。
    如果变量名包含敏感关键词（key, secret, token, password），则：
    - 值长度 > 12：保留前后各 6 字符，中间用 *** 替代
    - 值长度 ≤ 12：保留前后各 2 字符，中间用 *** 替代
    最后截断到 max_len 长度。
    """
    sensitive_keywords = ("key", "secret", "token", "password")
    var_name_lower = var_name.lower()
    is_sensitive = any(keyword in var_name_lower for keyword in sensitive_keywords)
    
    if not is_sensitive:
        masked = value
    else:
        val_len = len(value)
        if val_len > 12:
            # 保留前后各 6 字符
            masked = f"{value[:6]}***{value[-6:]}"
        elif val_len > 4:
            # 保留前后各 2 字符
            masked = f"{value[:2]}***{value[-2:]}"
        else:
            # 太短则全部 mask
            masked = "***"
    
    # 截断处理
    if len(masked) > max_len:
        masked = masked[:47] + "..."
    
    return masked


def format_env_comparison_table(
    current_env: Dict[str, str], new_env: Dict[str, str], provider_id: str
) -> List[str]:
    """
    生成环境变量对比表格。
    左列为变量名，中列为 current 值，右列为 new 值。
    """
    # 合并所有变量名，保持 new_env 的顺序，额外的 current 变量按字母序追加
    all_vars: List[str] = []
    seen: Set[str] = set()
    
    # 先添加 new_env 中的变量（保持原顺序）
    for var in new_env.keys():
        if var not in seen:
            all_vars.append(var)
            seen.add(var)
    
    # 再添加 current_env 中额外的变量（字母序）
    extra_vars = sorted(k for k in current_env.keys() if k not in seen)
    all_vars.extend(extra_vars)
    
    if not all_vars:
        return []
    
    # 准备表格数据
    rows: List[Tuple[str, str, str]] = []
    for var in all_vars:
        current_val = current_env.get(var, "(not set)")
        new_val = new_env.get(var, "(will be removed)")
        
        # 对实际存在的值进行 mask 处理
        if current_val not in ("(not set)", "(will be removed)"):
            current_val = mask_sensitive_value(var, current_val)
        if new_val not in ("(not set)", "(will be removed)"):
            new_val = mask_sensitive_value(var, new_val)
        
        rows.append((var, current_val, new_val))
    
    # 计算列宽
    col1_width = max(len("Variable Name"), *(len(r[0]) for r in rows))
    col2_width = max(len("Current Value"), *(len(r[1]) for r in rows))
    col3_header = f"New Value (Provider: {provider_id})"
    col3_width = max(len(col3_header), *(len(r[2]) for r in rows))
    
    # 生成表格
    lines: List[str] = []
    header = (
        f"{'Variable Name'.ljust(col1_width)} | "
        f"{'Current Value'.ljust(col2_width)} | "
        f"{col3_header.ljust(col3_width)}"
    )
    lines.append(header)
    separator = f"{'-' * col1_width}-+-{'-' * col2_width}-+-{'-' * col3_width}"
    lines.append(separator)
    
    for var, cur_val, new_val in rows:
        line = (
            f"{var.ljust(col1_width)} | "
            f"{cur_val.ljust(col2_width)} | "
            f"{new_val.ljust(col3_width)}"
        )
        lines.append(line)
    
    return lines


def summarize_changes(
    provider_id: str,
    clean_strategy: str,
    previous_provider: Optional[str],
    current_env: Dict[str, str],
    new_env: Dict[str, str],
) -> None:
    print(f"将切换至 provider: {provider_id}")
    if previous_provider:
        if previous_provider == provider_id:
            print(f"此前 provider: {previous_provider}（未变化）")
        else:
            print(f"此前 provider: {previous_provider}")
    else:
        print("此前 provider: 未记录")
    print(f"清理策略: {clean_strategy}")
    print()
    
    # 生成并打印对比表格
    table_lines = format_env_comparison_table(current_env, new_env, provider_id)
    if table_lines:
        for line in table_lines:
            print(line)
    else:
        print("无环境变量变更。")


def main() -> None:
    args = parse_args()
    input_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve()

    try:
        bases, providers = build_definitions(input_path)
        resolved_envs = resolve_envs(bases, providers)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        sys.exit(1)

    if args.list:
        print_provider_list(providers)
        return

    provider_id: Optional[str] = args.provider.strip() if args.provider else None
    if provider_id:
        definition = providers.get(provider_id)
        if not definition:
            print(f"配置中不存在 provider：{provider_id}", file=sys.stderr)
            sys.exit(1)
        if not definition.enable:
            print(f"provider {provider_id} 当前设置为 enable=false。", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            provider_id = select_provider_interactively(providers)
        except ConfigError as exc:
            print(f"配置错误：{exc}", file=sys.stderr)
            sys.exit(1)

    assert provider_id  # for type checker
    new_env = dict(resolved_envs[provider_id])

    try:
        settings = load_settings(output_path)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        sys.exit(1)

    # 提取当前的 env 用于展示对比
    current_env_section = settings.get("env")
    if isinstance(current_env_section, dict):
        current_env_dict = {str(k): "" if v is None else str(v) for k, v in current_env_section.items()}
    else:
        current_env_dict = {}

    updated_settings, previous_provider = apply_provider_env(
        settings=settings,
        provider_id=provider_id,
        new_env=new_env,
        clean_strategy=args.clean,
        resolved_envs=resolved_envs,
    )

    # 提取新的 env 用于展示对比
    new_env_section = updated_settings.get("env")
    if isinstance(new_env_section, dict):
        new_env_dict = {str(k): "" if v is None else str(v) for k, v in new_env_section.items()}
    else:
        new_env_dict = {}

    summarize_changes(
        provider_id=provider_id,
        clean_strategy=args.clean,
        previous_provider=previous_provider,
        current_env=current_env_dict,
        new_env=new_env_dict,
    )

    if args.dry_run:
        print("dry-run 模式：未对输出文件做任何改动。")
        return

    backup_path: Optional[Path] = None
    if not args.no_backup:
        backup_path = make_backup(output_path)
        if backup_path:
            print(f"已创建备份：{backup_path}")
            removed = rotate_backups(output_path, keep=5)
            if removed:
                print(f"已清理旧备份 {len(removed)} 个。")

    try:
        atomic_write_json(output_path, updated_settings)
    except OSError as exc:
        print(f"写入文件失败：{exc}", file=sys.stderr)
        if backup_path and backup_path.exists():
            print(f"备份文件保留在：{backup_path}", file=sys.stderr)
        sys.exit(1)

    print(f"已写入新的 settings.json：{output_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n操作已取消。", file=sys.stderr)
        sys.exit(130)

