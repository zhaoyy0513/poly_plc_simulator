"""Socket options that differ per platform, in one place.

The two platforms disagree about the one option this product depends on:

- POSIX ``SO_REUSEADDR`` only allows rebinding a port that is in TIME_WAIT; a
  live listener still fails with EADDRINUSE. That is what a simulator which
  restarts often wants.
- Windows ``SO_REUSEADDR`` means "anyone may bind over anyone", which is
  exactly the wrong thing for a port-uniqueness guarantee: a second instance
  binds the same port and both report success while only one receives traffic.
  Windows has ``SO_EXCLUSIVEADDRUSE`` for the intent we actually have.

So the rule is: exclusive on Windows, reuse on POSIX - applied by both the
pre-flight check and the real listener, so "the check said free" and "the bind
worked" can never disagree.
"""

from __future__ import annotations

import socket
import sys


def is_windows() -> bool:
    return sys.platform == "win32"


def bind_for_listening(sock: socket.socket) -> None:
    """Apply the platform's "this port is mine" option, before ``bind``.

    POSIX callers get TIME_WAIT reuse (an old socket from a previous run must
    not block a restart); Windows callers get exclusivity (nobody may bind
    over a live listener, us included).
    """
    if is_windows() and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def can_bind(host: str, port: int) -> str | None:
    """Would a real listener be able to take ``host:port`` right now?

    Returns ``None`` when the answer is yes, or a human-readable reason. The
    probe uses the same socket options as the server that will follow it, which
    is the only way the two answers stay the same - a bare ``bind`` on POSIX is
    stricter than a real listener, and on Windows a bare ``bind`` is *looser*
    than an exclusive one.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        bind_for_listening(probe)
        probe.bind((host, port))
    except OSError as exc:
        return exc.strerror or str(exc)
    finally:
        probe.close()
    return None
