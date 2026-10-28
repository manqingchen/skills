import argparse
import os
import subprocess
from pathlib import Path

from .common import (
  CWIKI_COOKIE_SOURCE_ENV,
  CWIKI_MANUAL_COOKIE_ENV,
  CWIKI_UPLOAD_CONFIG_FILE_ENV,
  CWIKI_USE_MANUAL_COOKIE_ENV,
  CwikiUploadError,
  GUAZI_SSO_SKILL_ROOT_ENV,
)


def get_workspace_root() -> Path:
  env_root = os.getenv("CWIKI_UPLOAD_WORKSPACE_ROOT")
  if env_root:
    return Path(env_root).expanduser()
  try:
    result = subprocess.run(
      ["git", "rev-parse", "--show-toplevel"],
      check=True,
      capture_output=True,
      text=True,
    )
    git_root = result.stdout.strip()
    if git_root:
      return Path(git_root)
  except Exception:
    pass
  return Path.cwd()


def get_user_config_path() -> Path:
  configured = os.getenv(CWIKI_UPLOAD_CONFIG_FILE_ENV, "").strip()
  if configured:
    return Path(configured).expanduser()
  return Path.home() / ".agents" / "config" / "cwiki-upload.env"


def load_env_file(env_path: Path, *, override: bool = False, protected_keys: set[str] | None = None) -> None:
  protected_keys = protected_keys or set()
  if not env_path.is_file():
    return
  try:
    content = env_path.read_text(encoding="utf-8")
  except Exception:
    return

  for raw_line in content.splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
      continue
    key, value = line.split("=", 1)
    key = key.strip()
    if not key or key.startswith("#"):
      continue
    if key in os.environ and (not override or key in protected_keys):
      continue
    os.environ[key] = value.strip().strip("\"'")


def load_workspace_env(workspace_root: Path) -> None:
  load_env_file(workspace_root / ".env")


def load_login_config(workspace_root: Path) -> None:
  protected_keys = set(os.environ)
  load_env_file(get_user_config_path(), protected_keys=protected_keys)
  load_env_file(workspace_root / ".env", override=True, protected_keys=protected_keys)


def _quote_env_value(value: str) -> str:
  escaped = value.replace("\\", "\\\\").replace('"', '\\"')
  return f'"{escaped}"'


def write_env_values(env_path: Path, updates: dict[str, str]) -> Path:
  lines: list[str] = []
  if env_path.is_file():
    try:
      lines = env_path.read_text(encoding="utf-8").splitlines()
    except Exception:
      lines = []

  remaining = dict(updates)
  output_lines: list[str] = []
  for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
      output_lines.append(line)
      continue
    key = stripped.split("=", 1)[0].strip()
    if key in remaining:
      output_lines.append(f"{key}={_quote_env_value(remaining.pop(key))}")
    else:
      output_lines.append(line)

  if remaining and output_lines and output_lines[-1].strip():
    output_lines.append("")
  for key, value in remaining.items():
    output_lines.append(f"{key}={_quote_env_value(value)}")

  env_path.parent.mkdir(parents=True, exist_ok=True)
  env_path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")
  return env_path


def write_workspace_env_values(workspace_root: Path, updates: dict[str, str]) -> Path:
  return write_env_values(workspace_root / ".env", updates)


def apply_login_cli_config(args: argparse.Namespace, *, workspace_root: Path) -> Path | None:
  updates: dict[str, str] = {}

  if getattr(args, "cookie_source", None):
    os.environ[CWIKI_COOKIE_SOURCE_ENV] = args.cookie_source
    updates[CWIKI_COOKIE_SOURCE_ENV] = args.cookie_source

  if getattr(args, "sso_skill_root", None):
    sso_skill_root = str(Path(args.sso_skill_root).expanduser())
    os.environ[GUAZI_SSO_SKILL_ROOT_ENV] = sso_skill_root
    updates[GUAZI_SSO_SKILL_ROOT_ENV] = sso_skill_root

  if getattr(args, "manual_cookie", None):
    os.environ[CWIKI_USE_MANUAL_COOKIE_ENV] = "1"
    os.environ[CWIKI_MANUAL_COOKIE_ENV] = args.manual_cookie
    os.environ[CWIKI_COOKIE_SOURCE_ENV] = "manual"
    updates[CWIKI_USE_MANUAL_COOKIE_ENV] = "1"
    updates[CWIKI_MANUAL_COOKIE_ENV] = args.manual_cookie
    updates[CWIKI_COOKIE_SOURCE_ENV] = "manual"

  save_scope = getattr(args, "save_login_config", None)
  if save_scope:
    if not updates:
      raise CwikiUploadError(
        "E_LOGIN_CONFIG_EMPTY",
        "没有可保存的登录态配置。",
        "请同时传入 --cookie-source、--manual-cookie 或 --sso-skill-root。",
      )
    if save_scope == "project":
      return write_workspace_env_values(workspace_root, updates)
    return write_env_values(get_user_config_path(), updates)

  return None


__all__ = [
  "apply_login_cli_config",
  "get_user_config_path",
  "get_workspace_root",
  "load_env_file",
  "load_login_config",
  "load_workspace_env",
  "write_env_values",
  "write_workspace_env_values",
]
