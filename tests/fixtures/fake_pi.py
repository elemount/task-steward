"""A stand-in for the pi command line (pi is outside this repository; PI001, PI004).

    pi install <path> / pi remove <path>      edit packages in $PI_CODING_AGENT_DIR/settings.json
    pi ... --list-models                      a fixed model table
    pi ... --print --mode json ... -- <p>     one review: FAKE_PI_ANSWER (JSON text) as the reply
    pi --mode rpc ...                         prompt / abort over JSONL; each prompt is answered
                                              with "echo: <message>"; "orphan-and-exit" leaves a
                                              child holding stdout and exits; FAKE_PI_STALL never
                                              reads stdin

Every call is appended to FAKE_PI_LOG (argv, cwd, TSW_SESSION_ID, and each RPC command).
"""

import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]


def log(entry: dict) -> None:
    path = os.environ.get("FAKE_PI_LOG")
    if path:
        with Path(path).open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry) + "\n")


def emit(record: dict) -> None:
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


log({"argv": args, "cwd": str(Path.cwd()), "session": os.environ.get("TSW_SESSION_ID")})
settings = Path(os.environ.get("PI_CODING_AGENT_DIR", "")) / "settings.json"
if args[:1] in (["install"], ["remove"]):
    data = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
    packages = [p for p in data.get("packages", []) if p != args[1]]
    if args[0] == "install":
        packages.append(args[1])
    data["packages"] = packages
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data), encoding="utf-8")
elif "--list-models" in args:
    print("provider  model      context  max-out  thinking  images")
    print("openai    gpt-5.4    400K     128K     yes       yes")
    print("anthropic claude-x   200K     64K      yes       no")
elif "--print" in args:
    provider = args[args.index("--provider") + 1]
    model = args[args.index("--model") + 1]
    answer = os.environ.get("FAKE_PI_ANSWER", "{}")
    emit({"type": "session", "id": "x"})
    emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "provider": os.environ.get("FAKE_PI_PROVIDER", provider),
                "model": model,
                "stopReason": "stop",
                "content": [{"type": "text", "text": answer}],
            },
        }
    )
    emit({"type": "agent_settled", "aborted": False})
elif "--mode" in args and args[args.index("--mode") + 1] == "rpc":
    if os.environ.get("FAKE_PI_STALL"):
        # A pi that never reads its commands: the pipe fills and writes block.
        import time

        time.sleep(300)
    for line in sys.stdin:
        command = json.loads(line)
        log({"rpc": command, "session": os.environ.get("TSW_SESSION_ID")})
        ident = command.get("id")
        if command.get("message") == "orphan-and-exit":
            # pi exits and leaves a child holding its stdout (review F4).
            import subprocess

            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(300)"], stdout=sys.stdout
            )
            Path(os.environ["FAKE_PI_CHILD"]).write_text(str(child.pid), encoding="utf-8")
            sys.exit(0)
        if command.get("type") == "prompt":
            emit(
                {
                    "type": "response",
                    "id": ident,
                    "command": "prompt",
                    "success": True,
                    "data": {"disposition": "started"},
                }
            )
            emit({"type": "agent_start"})
            emit(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": "echo: " + command["message"]}],
                    },
                }
            )
            emit({"type": "agent_settled", "aborted": False})
        else:
            emit({"type": "response", "id": ident, "command": command.get("type"), "success": True})
