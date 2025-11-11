#!/usr/bin/env python3

import argparse
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
    print("需要 Python 3.11 或更新版本（内置 tomllib 模块）。", file=sys.stderr)
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


def print_provider_list(providers: Dict[str, Definition]) -> None:
    enabled = [p for p in providers.values() if p.enable]
    if not enabled:
        print("当前配置中没有 enable=true 的 provider。")
        return
    print("可用的 provider：")
    for provider in sorted(enabled, key=lambda p: p.identifier):
        if provider.brief:
            print(f"- {provider.identifier} : {provider.brief}")
        else:
            print(f"- {provider.identifier}")


def select_provider_interactively(providers: Dict[str, Definition]) -> str:
    enabled = [p for p in providers.values() if p.enable]
    if not enabled:
        raise ConfigError("没有 enable=true 的 provider 可供选择，请检查配置。")
    enabled.sort(key=lambda p: p.identifier)
    print("请选择要使用的 provider：")
    for idx, provider in enumerate(enabled, start=1):
        label = provider.identifier
        if provider.brief:
            label = f"{label} - {provider.brief}"
        print(f"[{idx}] {label}")
    while True:
        user_input = input("请输入编号或 provider id：").strip()
        if not user_input:
            continue
        if user_input.isdigit():
            index = int(user_input) - 1
            if 0 <= index < len(enabled):
                return enabled[index].identifier
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
) -> Tuple[Dict[str, object], Optional[str], List[str], List[str], List[str]]:
    env_section = settings.get("env")
    if isinstance(env_section, dict):
        current_env = {str(k): "" if v is None else str(v) for k, v in env_section.items()}
    else:
        current_env = {}
    original_env = dict(current_env)

    previous_provider = original_env.get(CLAUDE_PROVIDER_KEY)
    if not isinstance(previous_provider, str):
        previous_provider = None

    removal_keys = determine_removal_keys(clean_strategy, previous_provider, resolved_envs, new_env)
    for key in removal_keys:
        current_env.pop(key, None)

    for key, value in new_env.items():
        current_env[key] = value
    current_env[CLAUDE_PROVIDER_KEY] = provider_id

    settings["env"] = current_env

    removed_keys = sorted(k for k in original_env.keys() if k not in current_env)
    added_keys = sorted(k for k in current_env.keys() if k not in original_env)
    changed_keys = sorted(
        k for k in current_env.keys() if k in original_env and original_env[k] != current_env[k]
    )

    return settings, previous_provider, removed_keys, added_keys, changed_keys


def make_backup(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.{timestamp}.bak")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("rb") as src, backup_path.open("wb") as dst:
        dst.write(src.read())
    return backup_path


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


def summarize_changes(
    provider_id: str,
    clean_strategy: str,
    previous_provider: Optional[str],
    removed_keys: Iterable[str],
    added_keys: Iterable[str],
    changed_keys: Iterable[str],
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

    removed = list(removed_keys)
    added = list(added_keys)
    changed = [k for k in changed_keys if k not in added]

    if removed:
        print(f"将移除 env 键：{', '.join(removed)}")
    else:
        print("无 env 键被移除。")
    if added:
        print(f"新增 env 键：{', '.join(added)}")
    else:
        print("无新增 env 键。")
    if changed:
        print(f"更新 env 键：{', '.join(changed)}")
    else:
        print("无需要更新的 env 键。")


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

    updated_settings, previous_provider, removed_keys, added_keys, changed_keys = apply_provider_env(
        settings=settings,
        provider_id=provider_id,
        new_env=new_env,
        clean_strategy=args.clean,
        resolved_envs=resolved_envs,
    )

    summarize_changes(
        provider_id=provider_id,
        clean_strategy=args.clean,
        previous_provider=previous_provider,
        removed_keys=removed_keys,
        added_keys=added_keys,
        changed_keys=changed_keys,
    )

    if args.dry_run:
        print("dry-run 模式：未对输出文件做任何改动。")
        return

    backup_path: Optional[Path] = None
    if not args.no_backup:
        backup_path = make_backup(output_path)
        if backup_path:
            print(f"已创建备份：{backup_path}")

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

