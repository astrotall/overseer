#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

TASK_KEY = re.compile(r"\bOVE-\d+\b")

EXEMPT_PREFIXES = ("chore", "ci", "build", "revert", "merge", "bump")

WARNING = """
  ⚠  В сообщении коммита нет ключа задачи OVE-<n>.
     Добавь его отдельной строкой в футер, через пустую строку после заголовка:
         OVE-42

     Без ключа Jira не привяжет коммит к задаче.
     Это только предупреждение — коммит не отменён.
"""


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0

    try:
        raw = Path(argv[1]).read_text(encoding="utf-8")
    except OSError:
        return 0

    lines = [ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")]
    message = "\n".join(lines).strip()
    if not message:
        return 0

    if TASK_KEY.search(message):
        return 0

    first_line = message.splitlines()[0].strip().lower()
    if first_line.startswith(EXEMPT_PREFIXES):
        return 0

    print(WARNING, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
