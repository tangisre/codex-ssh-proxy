import base64
from contextlib import contextmanager
import hmac
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time


ALLOWED_TARGETS = {
    b"auth.openai.com:443",
    b"chatgpt.com:443",
    b"api.openai.com:443",
}
PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "WS_PROXY", "WSS_PROXY", "NO_PROXY")
CODEX = os.path.expanduser("~/.local/bin/codex")
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
DESKTOP_PORT = 47890
CONFIG_MARKER = "# Codex 4090: keep proxy settings out of spawned shell commands."
SHELL_POLICY = 'shell_environment_policy.exclude=[{}]'.format(
    ",".join('"{}"'.format(name) for name in PROXY_NAMES)
)


def read_headers(stream):
    data = bytearray()
    while b"\r\n\r\n" not in data and len(data) < 16384:
        part = stream.recv(4096)
        if not part:
            break
        data.extend(part)
    headers, separator, remainder = data.partition(b"\r\n\r\n")
    if not separator:
        raise ValueError("Incomplete proxy headers")
    return bytes(headers), bytes(remainder)


def relay(client, upstream):
    peers = {client: upstream, upstream: client}
    for stream in peers:
        stream.settimeout(30)
    while peers:
        readable, _, _ = select.select(list(peers), [], [])
        for source in readable:
            chunk = source.recv(65536)
            destination = peers[source]
            if chunk:
                destination.sendall(chunk)
            else:
                destination.shutdown(socket.SHUT_WR)
                del peers[source]


class ConnectHandler(socketserver.BaseRequestHandler):
    def respond(self, status, headers=b""):
        self.request.sendall(b"HTTP/1.1 " + status + b"\r\n" + headers
                             + b"Content-Length: 0\r\nConnection: close\r\n\r\n")

    def handle(self):
        self.request.settimeout(8)
        try:
            headers, pending = read_headers(self.request)
            request_line, *fields = headers.split(b"\r\n")
            method, target, _ = request_line.split()
            if method != b"CONNECT" or target.lower() not in ALLOWED_TARGETS:
                self.respond(b"403 Forbidden")
                return

            credentials = {
                name.lower(): value.strip()
                for field in fields
                for name, colon, value in [field.partition(b":")]
                if colon
            }
            if not hmac.compare_digest(credentials.get(b"proxy-authorization", b""),
                                       self.server.authorization):
                self.respond(b"407 Proxy Authentication Required",
                             b'Proxy-Authenticate: Basic realm="Codex SSH proxy"\r\n')
                return

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
                upstream.settimeout(12)
                upstream.connect(self.server.forward_socket)
                upstream.sendall(b"CONNECT " + target + b" HTTP/1.1\r\nHost: " + target + b"\r\n\r\n")
                response, received = read_headers(upstream)
                if response.split(b" ", 2)[1:2] != [b"200"]:
                    self.respond(b"502 Bad Gateway")
                    return
                if self.server.trace:
                    print("Proxy CONNECT {}".format(target.decode("ascii")), file=sys.stderr, flush=True)
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n" + received)
                if pending:
                    upstream.sendall(pending)
                relay(self.request, upstream)
        except (OSError, ValueError):
            return


class ConnectProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def proxy_environment(url):
    env = {key: value for key, value in os.environ.items() if key.upper() not in PROXY_NAMES}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "WS_PROXY", "WSS_PROXY"):
        env[name] = env[name.lower()] = url
    return env


def request_status(url, env):
    result = subprocess.run(
        ["curl", "--silent", "--output", "/dev/null", "--max-time", "12",
         "--write-out", "%{http_connect}/%{http_code}", url], env=env, stdout=subprocess.PIPE
    )
    return result.stdout.decode("ascii")


def verify(env, bare_proxy):
    auth = "https://auth.openai.com/"
    status = request_status(auth, proxy_environment(bare_proxy))
    print("Proxy without session credentials: {}".format(status), flush=True)
    if status != "407/000":
        return 1

    checks = (
        (auth, {"200/200", "200/301", "200/302", "200/303", "200/307", "200/308"}),
        ("https://chatgpt.com/backend-api/codex/models", {"200/401"}),
        ("https://api.openai.com/v1/models", {"200/401"}),
        ("https://example.com/", {"403/000"}),
    )
    for url, expected in checks:
        status = request_status(url, env)
        print("{} {}".format(status, url), flush=True)
        if status not in expected:
            return 1

    inherited = sorted(key for key in os.environ if key.upper() in PROXY_NAMES)
    print("Ordinary SSH proxy environment: {}".format(", ".join(inherited) or "none"), flush=True)
    return subprocess.call([CODEX, "--version"], env=env)


def probe_login(env):
    with tempfile.TemporaryDirectory(prefix="codex-auth-probe-") as directory:
        env["CODEX_HOME"] = directory
        process = subprocess.Popen([CODEX, "login", "--device-auth"], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = bytearray()
        deadline = time.monotonic() + 25
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if not select.select([process.stdout], [], [], max(0, remaining))[0]:
                    break
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                output.extend(chunk)
                if b"Enter this one-time code" in output:
                    print("Device code issued; test stopped before approval.", flush=True)
                    return 0
            indicators = (b"407", b"403", b"404", b"429", b"not enabled", b"error sending request",
                          b"connection refused", b"timed out", b"certificate")
            matched = [item.decode("ascii") for item in indicators if item in output.lower()]
            print("Device code unavailable: {}".format(", ".join(matched) or "no prompt before exit or timeout"),
                  flush=True)
            return 1
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()


def desktop_secret():
    path = CODEX_HOME / "codex-4090-proxy.secret"
    flags = os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(str(path), flags | os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = os.open(str(path), flags | os.O_RDONLY)
        with os.fdopen(descriptor) as file:
            if os.fstat(file.fileno()).st_mode & 0o077:
                raise RuntimeError("Desktop proxy secret is readable outside this Linux account.")
            secret = file.read(128).strip()
        if not re.fullmatch(r"[a-zA-Z0-9_-]{43}", secret):
            raise RuntimeError("Invalid desktop proxy secret.")
        return secret
    secret = secrets.token_urlsafe(32)
    with os.fdopen(descriptor, "w") as file:
        file.write(secret + "\n")
    return secret


def configure_desktop_shell():
    path = CODEX_HOME / "config.toml"
    block = "\n\n{}\n[shell_environment_policy.filters]\n{}\n".format(
        CONFIG_MARKER, "\n".join('"{}" = "exclude"'.format(name) for name in PROXY_NAMES)
    )
    descriptor = os.open(str(path), os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as config:
        config.seek(0)
        current = config.read()
        if block.strip() in current:
            return
        if re.search(r"(?m)^\s*(?:\[+\s*|[\"']?)shell_environment_policy\b", current):
            raise RuntimeError("Codex already has a shell environment policy; integrate the proxy filters first.")
        config.write(block)


@contextmanager
def desktop_environment(env):
    path = CODEX_HOME / ".env"
    proxy_vars = "# Codex 4090 desktop proxy. Managed by the Windows launcher.\n"
    proxy_vars += "".join("{}={}\n".format(name, env[name]) for name in sorted(env) if name.upper() in PROXY_NAMES)
    try:
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(proxy_vars)
    except FileExistsError:
        if (path.is_symlink() or path.stat().st_mode & 0o077
                or path.read_text(encoding="utf-8") != proxy_vars):
            raise RuntimeError("Codex already has a separate .env file; leaving it unchanged.")
    try:
        yield
    finally:
        if not path.is_symlink() and path.exists() and path.read_text(encoding="utf-8") == proxy_vars:
            path.unlink()


def wait_for_desktop(env):
    def stop(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGHUP, stop)
    signal.signal(signal.SIGTERM, stop)
    with desktop_environment(env):
        configure_desktop_shell()
        print("Desktop proxy ready. Keep this window open while using desktop Remote SSH.", flush=True)
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except KeyboardInterrupt:
            return 0
    return 0


def main():
    forward_socket, mode, project_arg = sys.argv[1:]
    project = os.path.expanduser(base64.b64decode(project_arg).decode("utf-8"))
    port = DESKTOP_PORT if mode == "Desktop" else 0
    with ConnectProxy(("127.0.0.1", port), ConnectHandler) as proxy:
        secret = desktop_secret() if mode == "Desktop" else secrets.token_urlsafe(32)
        credential = "codex:" + secret
        proxy.authorization = b"Basic " + base64.b64encode(credential.encode("ascii"))
        proxy.forward_socket = forward_socket
        proxy.trace = mode != "Run"
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        port = proxy.server_address[1]
        env = proxy_environment("http://{}@127.0.0.1:{}".format(credential, port))
        try:
            print("[3/3] Linux proxy ready ({})".format(mode.lower()), flush=True)
            if mode == "Desktop":
                return wait_for_desktop(env)
            if mode == "Verify":
                return verify(env, "http://127.0.0.1:{}".format(port))
            if mode == "Probe":
                return probe_login(env)
            if mode == "Login":
                status = subprocess.call([CODEX, "login", "status"], env=env,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if status == 0:
                    print("The SSH host already has a Codex login. Use run or desktop to work.", flush=True)
                    return 0
                return subprocess.call([CODEX, "login", "--device-auth"], env=env)

            command = [CODEX, "-c", SHELL_POLICY]
            if mode == "Smoke":
                command += ["exec", "--sandbox", "read-only",
                            "Read-only connectivity check. Run a shell command to show pwd and the names "
                            "of any inherited proxy environment variables, never their values. Then reply "
                            "with the result. Do not modify the repository or run experiments."]
                return subprocess.call(command, cwd=project, env=env, stdin=subprocess.DEVNULL, timeout=120)
            return subprocess.call(command, cwd=project, env=env)
        finally:
            proxy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
