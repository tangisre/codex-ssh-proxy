import argparse
import base64
import os
from pathlib import Path
import re
import subprocess
import sys


SSH = Path(os.environ["WINDIR"]) / "System32" / "OpenSSH" / "ssh.exe"
DEFAULT_HOST = "codex-linux"
DEFAULT_PROXY_PORT = 7890
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8"]
SETUP_OPTIONS = ["-nT", *SSH_OPTIONS, "-o", "ClearAllForwardings=yes"]


def run(mode, host, proxy_port, project):
    print("[1/3] Connecting to {}...".format(host), flush=True)
    directory = subprocess.check_output(
        [SSH, *SETUP_OPTIONS, host, "mktemp -d /tmp/codex-ssh-proxy.XXXXXXXXXX"],
        text=True, timeout=20,
    ).strip()
    if not re.fullmatch(r"/tmp/codex-ssh-proxy\.[0-9A-Za-z]{10}", directory):
        raise ValueError("SSH did not return the expected private directory.")

    socket_path = "{}/windows-proxy.sock".format(directory)
    try:
        source = Path(__file__).resolve().with_name("proxy.py").read_bytes()
        payload = base64.b64encode(source).decode("ascii")
        project_arg = base64.b64encode(project.encode("utf-8")).decode("ascii")
        bootstrap = "import base64,sys;exec(base64.b64decode(sys.argv.pop(1)))"
        command = "exec python3 -c '{}' {} {} {} {}".format(bootstrap, payload, socket_path, mode, project_arg)
        tty = "-tt" if mode in {"Login", "Run"} else "-T"
        print("[2/3] Opening the Codex-only proxy through Windows port {}...".format(proxy_port),
              flush=True)
        forwarding = "{}:127.0.0.1:{}".format(socket_path, proxy_port)
        return subprocess.call([
            SSH, tty, *SSH_OPTIONS, "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3", "-R", forwarding, host, command,
        ])
    finally:
        cleanup = "if test -S {0}; then unlink {0}; fi && rmdir -- {1}".format(socket_path, directory)
        try:
            result = subprocess.call([SSH, *SETUP_OPTIONS, host, cleanup], timeout=20)
            if result:
                print("Could not clean up the private forwarding directory.", file=sys.stderr)
        except (OSError, subprocess.TimeoutExpired):
            print("SSH could not clean up the private forwarding directory.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Run Codex on a Linux SSH host through your Windows proxy.")
    parser.add_argument("mode", nargs="?", default="run",
                        choices=("login", "run", "verify", "smoke", "probe", "desktop"))
    parser.add_argument("--host", default=DEFAULT_HOST, help="SSH alias (default: %(default)s)")
    parser.add_argument("--windows-proxy-port", type=int, default=DEFAULT_PROXY_PORT,
                        help="Local HTTP or mixed proxy port (default: %(default)s)")
    parser.add_argument("--project", default="~", help="Linux working directory for run and smoke (default: %(default)s)")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.host):
        parser.error("--host must be a valid SSH alias")
    if not 1 <= args.windows_proxy_port <= 65535:
        parser.error("--windows-proxy-port must be between 1 and 65535")
    if not args.project.strip():
        parser.error("--project must be a non-empty Linux path")
    try:
        return run(args.mode.capitalize(), args.host, args.windows_proxy_port, args.project)
    except KeyboardInterrupt:
        return 130
    except subprocess.CalledProcessError as error:
        print("SSH setup failed (exit {}).".format(error.returncode), file=sys.stderr)
    except subprocess.TimeoutExpired as error:
        print("SSH setup to {} did not finish within {} seconds. The proxy has not started."
              .format(args.host, error.timeout), file=sys.stderr)
    except (OSError, ValueError) as error:
        print("Launcher: {}".format(error), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
