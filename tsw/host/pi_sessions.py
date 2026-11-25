# pi's RPC records are untyped JSON walked generically.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""Pi worker sessions (PI001 §5): ``tsw pi serve`` holds one ``pi --mode rpc`` per worker.

One server per data directory (``locks/pi-sessions.lock``) listens on a Unix socket only this user
can reach. It is started on demand, in a process group of its own, so the workers outlive the
main session's ``pi``; it exits once no worker is running for a while. Requests are one JSON
line, answered by one JSON line.

Each worker's stdout is read without pause and split only at LF (pi's RPC framing). The server
keeps, in memory only, the last completed replies of each worker for ``tsw pi chat``; it never
writes the model's output anywhere. What a worker reports goes through tsw's tools, as on every
host. Each worker's state (directory, whether it runs, its exit code) is in
``cache/pi-sessions/<name>.json`` so that a restarted server resumes the same pi session.

Messages to a session that the server does not hold (the main session's interactive ``pi``) go
to that session's mailbox file; its extension takes them when it polls (PI001 §5.3).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any, cast

from tsw.errors import ErrorCode, TswError, rejected
from tsw.home import Home
from tsw.host.identity import PI_PREFIX, TSW_SESSION_ENV
from tsw.locks import FileLock
from tsw.review.process import ProcessTree
from tsw.storage.fs import atomic_write

DIR = Path("cache") / "pi-sessions"
LOCK = "pi-sessions.lock"
MAILBOX = "mailbox"
PI_ENV = "TSW_PI"  # the pi command line (default: ``pi`` on PATH)
IDLE_ENV = "TSW_PI_IDLE_SECONDS"
IDLE_SECONDS = 600.0
COMMAND_TIMEOUT = 60.0
START_WAIT = 15.0
REPLIES = 50  # completed replies kept per worker for ``tsw pi chat``
NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# The pi package: pi loads a package once however many declarations name it (installed and -e).
EXTENSION = REPO_ROOT / "pi"
# Unix sockets: the server is POSIX only (PI001 §1); -1 makes a socket fail elsewhere.
AF_UNIX: int = getattr(socket, "AF_UNIX", -1)
WRAP = '<cross-session-message from="{sender}">\n{message}\n</cross-session-message>'


def folder(home: Home) -> Path:
    return home.root / DIR


def socket_path(home: Home) -> Path:
    """A short path (Unix sockets are limited to about 100 bytes) in a folder only this user
    can enter: ``$XDG_RUNTIME_DIR``, else the temporary directory."""
    key = hashlib.sha256(str(home.root.resolve()).encode("utf-8")).hexdigest()[:12]
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    uid = getattr(os, "getuid", lambda: 0)()  # POSIX only, as the server (PI001 §1)
    base = Path(runtime) if runtime else Path(tempfile.gettempdir()) / f"tsw-{uid}"
    return base / f"tsw-pi-{key}.sock"


def ref(name: str) -> str:
    return PI_PREFIX + name


def name_of(session_ref: str) -> str | None:
    return session_ref[len(PI_PREFIX) :] if session_ref.startswith(PI_PREFIX) else None


def check_name(name: str) -> str:
    if not NAME.match(name):
        raise rejected(
            "pi 会话名只能含字母、数字、.、_、-，且以字母或数字开头结尾",
            reason="invalid_input",
            name=name,
        )
    return name


def wrap(sender: str, message: str) -> str:
    """A message from another session, as Claude Code marks one: never the user's words."""
    return WRAP.format(sender=sender, message=message)


def state_file(home: Home, name: str) -> Path:
    return folder(home) / f"{name}.json"


def read_state(home: Home, name: str) -> dict[str, Any] | None:
    try:
        data: Any = json.loads(state_file(home, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None


def _write_state(home: Home, name: str, data: Mapping[str, Any]) -> None:
    atomic_write(state_file(home, name), (json.dumps(dict(data)) + "\n").encode("utf-8"))


# ---------------------------------------------------------------------- mailbox


def _mailbox(home: Home, session_ref: str) -> Path:
    digest = hashlib.sha256(session_ref.encode("utf-8")).hexdigest()[:16]
    return folder(home) / MAILBOX / f"{digest}.jsonl"


def post(home: Home, session_ref: str, text: str) -> None:
    path = _mailbox(home, session_ref)
    with FileLock(path.with_suffix(".lock")), path.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"to": session_ref, "text": text}, ensure_ascii=False) + "\n")


def take(home: Home, session_ref: str) -> list[str]:
    """The messages waiting for ``session_ref``, oldest first; they are removed."""
    path = _mailbox(home, session_ref)
    if not path.is_file():
        return []
    with FileLock(path.with_suffix(".lock")):
        lines = path.read_text(encoding="utf-8").splitlines()
        path.unlink()
    out: list[str] = []
    for line in lines:
        with contextlib.suppress(ValueError):
            entry: Any = json.loads(line)
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                out.append(str(entry["text"]))
    return out


# ---------------------------------------------------------------------- one worker


class Worker:
    """One ``pi --mode rpc`` and what the server knows of it."""

    def __init__(self, home: Home, name: str, cwd: Path, argv: list[str]) -> None:
        self.home = home
        self.name = name
        self.cwd = cwd
        self.busy = False
        self.replies: deque[tuple[int, str]] = deque(maxlen=REPLIES)
        self.seq = 0
        self.exit: int | None = None
        self._lock = threading.Lock()
        self._waiting: dict[str, tuple[threading.Event, list[dict[str, Any]]]] = {}
        self._next = 0
        env = dict(os.environ)
        env.update(
            {
                "TSW_HOME": str(home.root),
                TSW_SESSION_ENV: ref(name),
                "TSW_PYTHON": sys.executable,
                "TSW_REPO": str(REPO_ROOT),
            }
        )
        self.tree = ProcessTree(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    @property
    def alive(self) -> bool:
        return self.tree.proc.poll() is None

    def _read(self) -> None:
        stream = cast("IO[bytes]", self.tree.proc.stdout)
        for raw in iter(stream.readline, b""):
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                record: Any = json.loads(raw.rstrip(b"\r\n").decode("utf-8"))
                if isinstance(record, dict):
                    self._on(cast("dict[str, Any]", record))
        self.exit = self.tree.proc.wait()
        for pipe in (stream, self.tree.proc.stdin):
            with contextlib.suppress(OSError):
                if pipe is not None:
                    pipe.close()
        with self._lock:
            self.busy = False
            for event, box in self._waiting.values():
                box.append({"success": False, "error": "pi 已退出"})
                event.set()

    def _on(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        with self._lock:
            if kind == "response" and isinstance(record.get("id"), str):
                waiting = self._waiting.pop(record["id"], None)
                if waiting is not None:
                    waiting[1].append(record)
                    waiting[0].set()
            elif kind == "agent_start":
                self.busy = True
            elif kind == "agent_settled":
                self.busy = False
            elif kind == "message_end":
                message = record.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    text = "".join(
                        str(part.get("text") or "")
                        for part in message.get("content") or []
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                    if text.strip():
                        self.seq += 1
                        self.replies.append((self.seq, text))

    def command(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Send one RPC command and wait for its response."""
        with self._lock:
            self._next += 1
            ident = f"tsw-{self._next}"
            event, box = threading.Event(), cast("list[dict[str, Any]]", [])
            self._waiting[ident] = (event, box)
        stdin = cast("IO[bytes]", self.tree.proc.stdin)
        try:
            stdin.write(json.dumps({**payload, "id": ident}).encode("utf-8") + b"\n")
            stdin.flush()
        except OSError as exc:
            with self._lock:
                self._waiting.pop(ident, None)
            raise TswError(
                ErrorCode.UNAVAILABLE, "pi 不接受命令（已退出）", reason="pi_exited"
            ) from exc
        if not event.wait(COMMAND_TIMEOUT):
            with self._lock:
                self._waiting.pop(ident, None)
            raise TswError(ErrorCode.UNAVAILABLE, "pi 没有回应命令", reason="pi_timeout")
        return box[0]

    def prompt(self, message: str, *, steer: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "prompt", "message": message}
        with self._lock:
            busy = self.busy
        if busy:
            payload["streamingBehavior"] = "steer" if steer else "followUp"
        answer = self.command(payload)
        if not answer.get("success"):
            raise rejected(
                f"pi 拒绝了消息：{answer.get('error')}", reason="pi_rejected", name=self.name
            )
        return {"disposition": (answer.get("data") or {}).get("disposition")}

    def tail(self, after: int) -> dict[str, Any]:
        with self._lock:
            return {
                "replies": [{"seq": s, "text": t} for s, t in self.replies if s > after],
                "busy": self.busy,
                "alive": self.alive,
            }

    def stop(self) -> None:
        with contextlib.suppress(TswError):
            if self.alive:
                self.command({"type": "abort"})
        with contextlib.suppress(OSError):
            cast("IO[bytes]", self.tree.proc.stdin).close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.tree.proc.wait(timeout=10)
        self.tree.end()
        self._reader.join(timeout=10)


def argv(home: Home, name: str, request: Mapping[str, Any]) -> list[str]:
    command = os.environ.get(PI_ENV) or shutil.which("pi") or "pi"
    out = [
        command,
        "--mode",
        "rpc",
        "--session-dir",
        str(folder(home) / "sessions"),
        "--session-id",
        name,
        "--name",
        str(request.get("title") or name),
        "-e",
        str(EXTENSION),
    ]
    model = request.get("model")
    if isinstance(model, str) and "/" in model:
        out += ["--model", model]
    thinking = request.get("thinking")
    if isinstance(thinking, str) and thinking:
        out += ["--thinking", thinking]
    return out


# ---------------------------------------------------------------------- the server


class Server:
    def __init__(self, home: Home) -> None:
        self.home = home
        self.workers: dict[str, Worker] = {}
        self.lock = threading.Lock()
        self.last_busy = time.monotonic()

    def _spawn(self, name: str, cwd: Path, request: Mapping[str, Any]) -> Worker:
        state = {"name": name, "cwd": str(cwd), "state": "starting", "request": dict(request)}
        _write_state(self.home, name, state)  # before the process: a lost start is reconciled
        worker = Worker(self.home, name, cwd, argv(self.home, name, request))
        self.workers[name] = worker
        _write_state(self.home, name, {**state, "state": "running", "pid": worker.tree.proc.pid})
        return worker

    def _worker(self, name: str) -> Worker:
        """A running worker; one that ended (or ran under an earlier server) is resumed."""
        found = self.workers.get(name)
        if found is not None and found.alive:
            return found
        state = read_state(self.home, name)
        if state is None:
            raise rejected(f"没有 pi 会话 {name}", reason="unknown_session", name=name)
        request = state.get("request") if isinstance(state.get("request"), dict) else {}
        return self._spawn(name, Path(str(state["cwd"])), cast("dict[str, Any]", request))

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if op == "ping":
            return {"ok": True, "pid": os.getpid()}
        if op == "list":
            return {"ok": True, "sessions": self.listing()}
        name = check_name(str(request.get("name") or ""))
        with self.lock:
            if op == "start":
                existing = self.workers.get(name)
                if existing is not None and existing.alive:
                    return {"ok": True, "session_ref": ref(name), "existed": True}
                cwd = Path(str(request.get("cwd") or ""))
                if not cwd.is_dir():
                    raise rejected("会话目录不存在", reason="workdir_unavailable", cwd=str(cwd))
                worker = self._spawn(name, cwd, request)
            elif op == "stop":
                worker = self.workers.pop(name, None)
                if worker is not None:
                    worker.stop()
                state = read_state(self.home, name)
                if state is not None:
                    _write_state(self.home, name, {**state, "state": "stopped"})
                return {"ok": True, "stopped": worker is not None}
            elif op == "tail":
                worker = self.workers.get(name)
                if worker is None:
                    return {"ok": True, "replies": [], "busy": False, "alive": False}
                return {"ok": True, **worker.tail(int(request.get("after") or 0))}
            elif op in ("send", "chat"):
                worker = self._worker(name)
            else:
                raise rejected(f"未知的请求 {op}", reason="invalid_input")
        message = str(request.get("message") or request.get("prompt") or "")
        out: dict[str, Any] = {"ok": True, "session_ref": ref(name)}
        if message:
            out.update(worker.prompt(message, steer=op == "chat"))
        return out

    def listing(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted(folder(self.home).glob("*.json")):
            state = read_state(self.home, path.stem) or {}
            worker = self.workers.get(path.stem)
            out.append(
                {
                    "name": path.stem,
                    "session_ref": ref(path.stem),
                    "cwd": state.get("cwd"),
                    "alive": bool(worker and worker.alive),
                    "busy": bool(worker and worker.busy),
                    "exit": worker.exit if worker else None,
                    "state": state.get("state"),
                }
            )
        return out

    def idle(self) -> bool:
        if any(w.alive for w in self.workers.values()):
            self.last_busy = time.monotonic()
            return False
        limit = float(os.environ.get(IDLE_ENV) or IDLE_SECONDS)
        return time.monotonic() - self.last_busy > limit

    def close(self) -> None:
        for worker in self.workers.values():
            worker.stop()


def _answer(server: Server, conn: socket.socket) -> None:
    with conn, conn.makefile("rwb") as stream:
        line = stream.readline()
        try:
            request: Any = json.loads(line)
            if not isinstance(request, dict):
                raise rejected("请求须为 JSON 对象", reason="invalid_input")
            reply = server.handle(cast("dict[str, Any]", request))
        except TswError as exc:
            reply = {"ok": False, "error": exc.message, **exc.details}
        except (ValueError, OSError) as exc:
            reply = {"ok": False, "error": str(exc)}
        stream.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
        stream.flush()


def serve(home: Home) -> dict[str, Any]:
    """Run the server until it has been idle for a while; a second one exits at once."""
    lock = FileLock(home.locks_dir / LOCK, timeout=0)
    try:
        lock.acquire()
    except TswError:
        return {"served": False, "note": "已有 tsw pi serve 在运行"}
    path = socket_path(home)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.unlink(missing_ok=True)
    listener = socket.socket(AF_UNIX, socket.SOCK_STREAM)
    server = Server(home)
    try:
        old = os.umask(0o177)  # the socket is this user's only
        try:
            listener.bind(str(path))
        finally:
            os.umask(old)
        listener.listen(16)
        listener.settimeout(1.0)
        while not server.idle():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            threading.Thread(target=_answer, args=(server, conn), daemon=True).start()
    finally:
        listener.close()
        path.unlink(missing_ok=True)
        server.close()
        lock.release()
    return {"served": True}


# ---------------------------------------------------------------------- the client


def _ask(path: Path, request: Mapping[str, Any], timeout: float) -> dict[str, Any]:
    with socket.socket(AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(path))
        with conn.makefile("rwb") as stream:
            stream.write(json.dumps(dict(request)).encode("utf-8") + b"\n")
            stream.flush()
            line = stream.readline()
    data: Any = json.loads(line)
    return cast("dict[str, Any]", data) if isinstance(data, dict) else {"ok": False}


def _launch(home: Home) -> None:
    launcher = REPO_ROOT / "plugin" / "bin" / "tsw_launch.py"
    log = folder(home) / "serve.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as err:
        subprocess.Popen(
            [sys.executable, "-X", "utf8", str(launcher), "pi", "serve", "--home", str(home.root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
            start_new_session=True,  # outlives the main session's pi
            close_fds=True,
        )


def request(
    home: Home, payload: Mapping[str, Any], *, start: bool = True, timeout: float = 90.0
) -> dict[str, Any]:
    """Ask the server, starting it first when it is not running and ``start``."""
    path = socket_path(home)
    try:
        reply = _ask(path, payload, timeout)
    except (OSError, ValueError):
        if not start:
            return {"ok": False, "error": "tsw pi serve 没有运行", "reason": "not_running"}
        _launch(home)
        deadline = time.monotonic() + START_WAIT
        while True:
            time.sleep(0.2)
            try:
                reply = _ask(path, payload, timeout)
                break
            except (OSError, ValueError) as exc:
                if time.monotonic() >= deadline:
                    raise TswError(
                        ErrorCode.UNAVAILABLE,
                        f"tsw pi serve 没有启动（见 {folder(home) / 'serve.log'}）",
                        reason="pi_server_unavailable",
                    ) from exc
    if not reply.get("ok"):
        raise rejected(
            str(reply.get("error") or "请求失败"), reason=str(reply.get("reason") or "pi_failed")
        )
    reply.pop("ok", None)
    return reply


def send(home: Home, sender: str, target: str, message: str) -> dict[str, Any]:
    """A message from ``sender`` to ``target``: a worker the server holds, else the target's
    mailbox (the main session's pi)."""
    text = wrap(sender, message)
    name = name_of(target)
    if name is not None and read_state(home, name) is not None:
        return {
            "delivered": "pi-rpc",
            **request(home, {"op": "send", "name": name, "message": text}),
        }
    post(home, target, text)
    return {"delivered": "mailbox", "session_ref": target}
