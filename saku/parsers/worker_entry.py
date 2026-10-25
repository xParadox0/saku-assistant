"""Apply per-process limits in the child, before any untrusted document is parsed.

Do not use Popen(preexec_fn=...) in the threaded parser service: Python warns that
preexec hooks can deadlock after fork in a multithreaded process.
"""
from __future__ import annotations

import resource
import sys

from .sandbox import CPU_SECONDS, MEMORY_LIMIT, _worker


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        return 2
    try:
        memory, cpu = (int(value, 10) for value in args)
    except (TypeError, ValueError):
        return 2
    if not 0 < memory <= MEMORY_LIMIT or not 0 < cpu <= CPU_SECONDS:
        return 2

    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    _worker()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
