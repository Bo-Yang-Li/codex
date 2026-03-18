#!/usr/bin/env python3
import shutil
from pathlib import Path


SOURCE_HOME = Path.home() / ".codex"
TARGET_HOME = Path.home() / ".ola-codex"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def main() -> None:
    if not SOURCE_HOME.exists():
        raise SystemExit(f"未找到源目录：{SOURCE_HOME}")

    ensure_dir(TARGET_HOME)
    ensure_dir(TARGET_HOME / "sessions")
    ensure_dir(TARGET_HOME / "memories")
    ensure_dir(TARGET_HOME / "log")
    ensure_dir(TARGET_HOME / "sqlite")
    ensure_dir(TARGET_HOME / "tmp")
    ensure_dir(TARGET_HOME / "skills")

    source_auth = SOURCE_HOME / "auth.json"
    target_auth = TARGET_HOME / "auth.json"
    if source_auth.exists() and not target_auth.exists():
        target_auth.symlink_to(source_auth)

    source_agents = SOURCE_HOME / "AGENTS.md"
    target_agents = TARGET_HOME / "AGENTS.md"
    if source_agents.exists() and not target_agents.exists():
        target_agents.symlink_to(source_agents)

    config_path = TARGET_HOME / "config.toml"
    if not config_path.exists():
        write_text(
            config_path,
            '\n'.join(
                [
                    'model = "gpt-5.4"',
                    'agent_max_threads = 3',
                    'model_auto_compact_token_limit = 120000',
                    '',
                    '[projects."/Users/bytedance/Documents/GitHub/codex"]',
                    'trust_level = "trusted"',
                    '',
                    '[notice.model_migrations]',
                    '"gpt-5.3-codex" = "gpt-5.4"',
                    '',
                ]
            ),
        )

    print(f"OLA 独立目录已准备好：{TARGET_HOME}")
    print("说明：")
    print(f"- 会话、日志、memory、配置会写到 {TARGET_HOME}")
    if target_auth.exists():
        print(f"- 认证沿用现有 ChatGPT 登录态：{target_auth} -> {source_auth}")
    else:
        print("- 没找到 ~/.codex/auth.json，请先在官方 Codex 中登录一次")


if __name__ == "__main__":
    main()
