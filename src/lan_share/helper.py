"""Root entry point for the GUI (run through pkexec; see packaging/io.github.lan_share.policy.in).

pkexec gives this program root after the user authenticates. To keep that narrow it accepts ONLY the exact
command shapes the GUI sends, and rejects everything else (notably `--root`, which would redirect where the
tool writes, and any value that looks like a flag). The CLI's own validation still applies on top.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from . import LanShareError

# subcommand -> (number of positional args, {flag: (takes_a_value, required, repeatable)})
_SHAPES: dict[tuple[str, ...], tuple[int, dict[str, tuple[bool, bool, bool]]]] = {
    ("doctor",): (0, {"--json": (False, True, False)}),
    ("setup",): (0, {"--apply": (False, True, False)}),
    ("share", "add"): (2, {"--apply": (False, True, False), "--user": (True, True, True), "--rw": (False, False, False)}),
    ("share", "remove"): (1, {"--apply": (False, True, False)}),
    ("user", "add"): (1, {"--password-stdin": (False, True, False)}),
    ("mount", "add"): (2, {"--user": (True, True, False), "--local-user": (True, True, False),
                           "--password-stdin": (False, True, False), "--domain": (True, False, False)}),
    ("mount", "remove"): (1, {}),
}


def validate(argv: list[str]) -> list[str]:
    """Return argv unchanged if it matches an allowed shape, else raise LanShareError."""
    for words in sorted(_SHAPES, key=len, reverse=True):
        if tuple(argv[: len(words)]) == words:
            break
    else:
        raise LanShareError("this command is not allowed through the privileged helper")
    npos, flags = _SHAPES[words]
    rest = argv[len(words):]
    if len(rest) < npos or any(a.startswith("-") for a in rest[:npos]):
        raise LanShareError("bad arguments for the privileged helper")
    seen: dict[str, int] = {}
    i = npos
    while i < len(rest):
        flag = rest[i]
        if flag not in flags:
            raise LanShareError(f"option {flag!r} is not allowed through the privileged helper")
        takes_value, _required, repeatable = flags[flag]
        seen[flag] = seen.get(flag, 0) + 1
        if seen[flag] > 1 and not repeatable:
            raise LanShareError(f"option {flag!r} given more than once")
        if takes_value:
            if i + 1 >= len(rest) or rest[i + 1].startswith("-"):
                raise LanShareError(f"option {flag!r} needs a value")
            i += 1
        i += 1
    missing = [f for f, (_v, required, _r) in flags.items() if required and f not in seen]
    if missing:
        raise LanShareError(f"missing required option(s): {', '.join(missing)}")
    return argv


def main(argv: Optional[list[str]] = None) -> int:
    from . import cli

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        validate(args)
    except LanShareError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    os.environ.pop("LAN_SHARE_ROOT", None)  # never let the environment redirect a root run
    os.umask(0o022)
    return cli.main(args)
