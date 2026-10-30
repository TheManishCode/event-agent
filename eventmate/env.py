"""Loads secrets from a local ``.env`` into the process environment (AI_CODING_RULES: keys come from the environment only).

Only allow-listed names are read, values already set in the real environment win, and nothing here ever returns, logs or
prints a value: callers get names and warnings. ``.env`` is git-ignored; keep it out of version control and shared drives.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import MutableMapping, Optional, Tuple

ALLOWED = frozenset({
    "OPENWEATHER_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "EVENTMATE_ENABLE_LLM", "EVENTMATE_OVERPASS_URL",
    "EVENTMATE_NVIDIA_MODEL", "EVENTMATE_NVIDIA_MODEL_LIGHT", "EVENTMATE_OPENROUTER_MODEL_HEAVY", "EVENTMATE_OPENROUTER_MODEL_LIGHT", "EVENTMATE_LLM_MAX_TOKENS", "EVENTMATE_LLM_MAX_CALLS",
    "LANGFUSE_HOST", "LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "FOURSQUARE_API_KEY", "BRAVE_SEARCH_API_KEY", "GOOGLE_PLACES_API_KEY",
    "EVENTMATE_SMTP_HOST", "EVENTMATE_SMTP_PORT", "EVENTMATE_SMTP_USER", "EVENTMATE_SMTP_PASSWORD", "EVENTMATE_MAIL_FROM", "EVENTMATE_IMAP_HOST", "EVENTMATE_IMAP_PORT",
    "EVENTMATE_WAHA_URL", "EVENTMATE_WAHA_API_KEY", "EVENTMATE_WAHA_SESSION", "EVENTMATE_POLL_SECONDS",
})
MAX_BYTES = 64_000
DEFAULT_PATH = Path(__file__).resolve().parents[1] / ".env"
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


@dataclass(frozen=True)
class EnvLoad:
    loaded: Tuple[str, ...] = ()  # names only
    warnings: Tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"EnvLoad(loaded={self.loaded!r}, warnings={len(self.warnings)})"


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v.split(" #", 1)[0].strip()


def load_env(path: Optional[Path] = None, environ: Optional[MutableMapping[str, str]] = None) -> EnvLoad:
    environ = os.environ if environ is None else environ
    if path is None:
        path = Path(environ.get("EVENTMATE_ENV_FILE") or DEFAULT_PATH)
    path = Path(path)
    try:
        st = os.lstat(path)
    except OSError:
        return EnvLoad()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return EnvLoad(warnings=("the env file is a symlink or not a regular file and was ignored",))
    if st.st_size > MAX_BYTES:
        return EnvLoad(warnings=(f"the env file is larger than {MAX_BYTES} bytes and was ignored",))
    warnings = []
    if st.st_mode & 0o077:
        warnings.append("the env file's permissions let other users read it; run chmod 600 on it")
    try:
        text = path.read_text(encoding="utf-8-sig")  # tolerate the BOM some Windows editors add
    except (OSError, UnicodeDecodeError):
        return EnvLoad(warnings=tuple(warnings) + ("the env file could not be read",))
    loaded = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m or m.group(1) not in ALLOWED or m.group(1) in environ:
            continue
        value = _unquote(m.group(2))
        if value:
            environ[m.group(1)] = value
            loaded.append(m.group(1))
    return EnvLoad(loaded=tuple(loaded), warnings=tuple(warnings))
