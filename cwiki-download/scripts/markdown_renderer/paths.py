import os
import re
from pathlib import Path


def resolve_output_dir(workspace_root: Path) -> Path:
  custom = os.getenv("CWIKI_DOWNLOAD_SAVE_DIR")
  if custom:
    return Path(custom).expanduser()
  return workspace_root / "docs"


def _derive_page_filename_stem(page_title: str, page_id: str) -> str:
  filename = re.sub(r'[\\/:*?"<>|]+', "-", page_title).strip()
  filename = filename.replace(" / ", "-").replace("/", "-")
  filename = re.sub(r"\s*-\s*", "-", filename)
  filename = re.sub(r"\s+", " ", filename)
  filename = filename.strip(" .-") or "confluence-page"
  return f"{filename}-{page_id}"


def derive_markdown_filename(page_title: str, page_id: str) -> str:
  return f"{_derive_page_filename_stem(page_title, page_id)}.md"


def derive_llm_markdown_filename(page_title: str, page_id: str) -> str:
  return f"{_derive_page_filename_stem(page_title, page_id)}.llm.md"

