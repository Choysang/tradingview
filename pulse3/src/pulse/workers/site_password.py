"""Set the site login password (interactive; the password is typed, never passed as an argument or stored in clear).

Usage (on the server, from an interactive SSH session)::

    powershell -File C:\\pulse3\\set_site_password.ps1

Writes the scrypt hash to ``PULSE_SITE_PASSWORD_FILE`` and, when missing, a random key to ``PULSE_SITE_SECRET_FILE``.
A new password logs out every existing session. Exit codes: 0 done; 2 refused (mismatch, too short, no terminal, no path).
"""
from __future__ import annotations

import getpass
import os
import secrets
import sys
from pathlib import Path

from pulse.api import site_auth as SA


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run(argv: list[str] | None = None, *, prompt=getpass.getpass) -> int:
    pw_path, key_path = os.environ.get("PULSE_SITE_PASSWORD_FILE"), os.environ.get("PULSE_SITE_SECRET_FILE")
    if not pw_path or not key_path:
        print("PULSE_SITE_PASSWORD_FILE and PULSE_SITE_SECRET_FILE must be set", file=sys.stderr)
        return 2
    try:
        first = prompt("新密码（至少 12 位）: ")
        second = prompt("再输入一次: ")
    except (EOFError, KeyboardInterrupt):
        print("no terminal input: run it in an interactive session (ssh -t)", file=sys.stderr)
        return 2
    if first != second:
        print("两次输入不一致，未修改", file=sys.stderr)
        return 2
    if len(first) < SA.MIN_PASSWORD_LEN:
        print(f"密码太短（至少 {SA.MIN_PASSWORD_LEN} 位），未修改", file=sys.stderr)
        return 2
    if not Path(key_path).exists():
        _write(Path(key_path), secrets.token_hex(32))
    _write(Path(pw_path), SA.hash_password(first))
    print("密码已设置；已有的登录会话全部失效")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
