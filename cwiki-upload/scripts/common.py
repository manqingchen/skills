from dataclasses import dataclass
from pathlib import Path

CWIKI_USE_MANUAL_COOKIE_ENV = "CWIKI_USE_MANUAL_COOKIE"
CWIKI_MANUAL_COOKIE_ENV = "CWIKI_MANUAL_COOKIE"
CWIKI_COOKIE_SOURCE_ENV = "CWIKI_COOKIE_SOURCE"
GUAZI_SSO_SKILL_ROOT_ENV = "GUAZI_SSO_SKILL_ROOT"
CWIKI_UPLOAD_CONFIG_FILE_ENV = "CWIKI_UPLOAD_CONFIG_FILE"


@dataclass(slots=True)
class CwikiUploadError(Exception):
  code: str
  message: str
  suggestion: str = ""


@dataclass(slots=True)
class CheckResult:
  name: str
  status: str
  detail: str


@dataclass(slots=True)
class LocalReference:
  kind: str
  label: str
  raw_path: str
  file_name: str
  absolute_path: Path | None = None


@dataclass(slots=True)
class MermaidBlock:
  filename: str
  source: str


def escape_html(text: str) -> str:
  return (
    text.replace("&", "&amp;")
    .replace("<", "&lt;")
    .replace(">", "&gt;")
    .replace('"', "&quot;")
  )


__all__ = [
  "CWIKI_COOKIE_SOURCE_ENV",
  "CWIKI_MANUAL_COOKIE_ENV",
  "CWIKI_UPLOAD_CONFIG_FILE_ENV",
  "CWIKI_USE_MANUAL_COOKIE_ENV",
  "CheckResult",
  "CwikiUploadError",
  "GUAZI_SSO_SKILL_ROOT_ENV",
  "LocalReference",
  "MermaidBlock",
  "escape_html",
]
