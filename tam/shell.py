"""Subprocess and ssh helpers shared by providers.

Kept separate so providers do not each grow their own idea of timeouts and
error handling, and so tests can patch one place.
"""

import subprocess

DEFAULT_TIMEOUT = 25


def run(cmd, timeout=DEFAULT_TIMEOUT, shell=False):
    """Run a command. Returns (returncode, stdout, stderr); never raises.

    Decoding uses errors="replace" deliberately. Observed output is not
    guaranteed to be valid UTF-8: `tail -c` slices a log at a byte offset and
    can cut a multi-byte character in half, and training logs are full of
    multi-byte progress bars. Strict decoding turned that into an exception,
    which surfaced as a healthy job being marked `unknown` -- and, worse,
    silently stopped its metrics being collected.
    """
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace",
            timeout=timeout, shell=shell)
        return proc.returncode, proc.stdout, proc.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return 1, "", str(exc)
    except UnicodeError as exc:                 # belt and braces
        return 1, "", f"undecodable output: {exc}"


def sh(script, timeout=DEFAULT_TIMEOUT):
    return run(["bash", "-lc", script], timeout=timeout)


def ssh(host, script, timeout=DEFAULT_TIMEOUT):
    if not host:
        return sh(script, timeout=timeout)
    # A host beginning with '-' would be read by ssh as an option, not a
    # destination: `-oProxyCommand=...` turns a "read-only" probe into local
    # command execution. Reject it rather than pass it through.
    if host.startswith("-"):
        return 1, "", f"refusing suspicious ssh host {host!r}"
    return run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "--", host, script], timeout=timeout)
