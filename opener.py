#!/usr/bin/env python3
"""Standalone local Rex URL/file opener. Python standard library only."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlparse
import uuid

PACKAGE = Path(__file__).resolve().parent


def run(argv, **kwargs):
    result = subprocess.run([str(part) for part in argv], text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Command failed: {argv[0]}")
    return result.stdout.strip()


class Rex:
    def __init__(self):
        bundled = Path(os.environ.get("REX_BIN_DIR", "/nonexistent")) / "rex"
        self.binary = str(bundled) if bundled.is_file() else shutil.which("rex")
        if not self.binary:
            raise RuntimeError("Put the Rex CLI on PATH or set REX_BIN_DIR to its directory")
        self.identity = json.loads(run([self.binary, "--autostart=false", "whoami", "--json"]))
        endpoint = urlparse(self.identity["endpoint"])
        if endpoint.scheme != "unix" or not stat.S_ISSOCK(os.stat(unquote(endpoint.path)).st_mode):
            raise RuntimeError("The opener supports a local Rex server only")

    def call(self, method, **args):
        return json.loads(run([self.binary, "--autostart=false", "api", "call", method, json.dumps(args)]))

    def action(self, client, action, **args):
        if not client:
            raise RuntimeError("An explicit Rex client is required for focus")
        return run([self.binary, "--autostart=false", "-C", client, "do", action, "--args", json.dumps(args)])


def context(rex, session, block, client=""):
    if not session or not block:
        raise RuntimeError("Exact Rex session and block are required")
    description = rex.call("session.describe_block", session_id=session, block_id=block)["block"]
    if description.get("host_id") != rex.identity["host_id"]:
        raise RuntimeError("The target block is not on this machine")
    process = rex.call("com.superlogical.terminal.process", session_id=session, block_id=block, args={})
    child = process.get("child") or {}
    login = child.get("user_id") == 0 and child.get("path") == "/usr/bin/login" and sys.platform == "darwin"
    if child.get("user_id") != os.getuid() and not login:
        raise RuntimeError("The terminal is not owned by this user")
    if not client:
        candidates = [
            peer["client_id"]
            for peer in rex.call("client.list")["clients"]
            if peer.get("info", {}).get("kind") == "app"
            and session in peer.get("session_ids", [])
            and peer["client_id"] == description.get("owner")
        ]
        client = candidates[0] if len(candidates) == 1 else ""
    cwd = (process.get("foreground") or child).get("cwd")
    if not cwd:
        cwd = rex.call("com.superlogical.terminal.pwd", session_id=session, block_id=block, args={}).get("pwd", "")
        cwd = unquote(urlparse(cwd).path) if cwd.startswith("file://") else cwd
    if not cwd or not Path(cwd).is_dir():
        raise RuntimeError("The terminal's working directory is unavailable")
    return description, Path(cwd), client


def layout(cwd, command):
    fish = shutil.which("fish")
    if not fish:
        raise RuntimeError("Fish is required for picker and editor terminals")
    reported = r"\033]7;" + Path(cwd).resolve().as_uri() + r"\033\134"
    # Fish -c otherwise hides commands in login's foreground process group.
    launch = (
        "status job-control full; "
        + command
        + "; set -g _rex_exit_status $status; while jobs -q; "
        + "function _rex_job_exit --on-process-exit (jobs -lp)[-1]; set -g _rex_exit_status $argv[3]; end; "
        + "fg; end; exit $_rex_exit_status"
    )
    return {
        "block": {
            "flavor": "com.superlogical.terminal.shell",
            "label": "opener",
            "options": {
                "cwd": str(cwd),
                "shell": "none",
                "command": [
                    "env",
                    "-u",
                    "NVIM",
                    "-u",
                    "OPENCODE_NVIM_GENERATION",
                    "-u",
                    "CLAUDECODE",
                    fish,
                    "-lic",
                    "printf %b " + shlex.quote(reported) + "; " + launch,
                ],
            },
        }
    }


def process_info(pid):
    fields = run(["ps", "-p", str(pid), "-o", "ppid=", "-o", "uid=", "-o", "tty="]).split()
    if len(fields) != 3:
        raise RuntimeError("Process ownership unavailable")
    return int(fields[0]), int(fields[1]), fields[2]


def owned_tty(process, pid):
    child = process.get("child") or {}
    owner = child.get("pid")
    login = child.get("user_id") == 0 and child.get("path") == "/usr/bin/login" and sys.platform == "darwin"
    if not owner or (child.get("user_id") != os.getuid() and not login):
        raise RuntimeError("Terminal ownership unavailable")
    current = pid
    for _ in range(64):
        parent, uid, tty = process_info(current)
        if current == owner:
            if uid != os.getuid() and not (login and uid == 0):
                break
            if tty in ("?", "??", "-"):
                break
            return tty if tty.startswith("/dev/") else "/dev/" + tty
        if uid != os.getuid() or parent == current or parent <= 1:
            break
        current = parent
    raise RuntimeError("Terminal ownership not proven by process ancestry")


def foreground_program(rex, session, block):
    process = rex.call("com.superlogical.terminal.process", session_id=session, block_id=block, args={})
    terminal = owned_tty(process, (process.get("child") or {}).get("pid", 0))
    rows = run(["ps", "-t", terminal, "-o", "pid=", "-o", "pgid=", "-o", "tpgid=", "-o", "uid=", "-o", "comm="])
    programs = []
    for row in rows.splitlines():
        fields = row.split(None, 4)
        if len(fields) == 5 and fields[1] == fields[2] and int(fields[3]) == os.getuid():
            programs.append((int(fields[0]), Path(fields[4]).name))
    if not programs:
        raise RuntimeError("The terminal's foreground process is unavailable")
    pid, name = next(
        (item for item in programs if re.fullmatch(r"n?vim|view|fzf", item[1])),
        next((item for item in programs if item[1] in ("fish", "bash", "zsh", "sh")), programs[-1]),
    )
    if name in ("nvim", "vim", "view", "fzf"):
        owned_tty(process, pid)
    return pid, name


def neovim_target(pid):
    processes = [row.split(None, 3) for row in run(["ps", "-axo", "pid=,ppid=,uid=,comm="], timeout=1).splitlines()]
    owners = {pid}
    while True:
        children = {
            int(row[0])
            for row in processes
            if len(row) == 4 and int(row[1]) in owners and int(row[2]) == os.getuid() and Path(row[3]).name == "nvim"
        }
        if children <= owners:
            break
        owners.update(children)
    candidates = run(["lsof", "-a", "-p", ",".join(map(str, sorted(owners))), "-U", "-Fpn"], timeout=1)
    socket_owner = 0
    for row in candidates.splitlines():
        if row.startswith("p"):
            socket_owner = int(row[1:])
        if not row.startswith("n/"):
            continue
        socket = Path(row[1:])
        try:
            metadata = socket.stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
            continue
        try:
            owner = run(["nvim", "--server", socket, "--remote-expr", "getpid()"], timeout=1)
        except (RuntimeError, subprocess.TimeoutExpired):
            continue
        if socket_owner in owners and owner == str(socket_owner):
            return {"socket": str(socket)}
    raise RuntimeError("No owned Neovim socket; refusing to steal editor focus")


def popup_neovim(target, cwd, command):
    token = uuid.uuid4().hex

    def request(action, **args):
        code = "assert(loadfile(" + json.dumps(str(PACKAGE / "popup.lua")) + "))(vim.json.decode(_A))"
        data = json.dumps({"action": action, "token": token, **args})
        expression = "luaeval(" + json.dumps(code) + "," + json.dumps(data) + ")"
        return int(run(["nvim", "--server", target["socket"], "--remote-expr", expression], timeout=3))

    colors = "bg:-1,gutter:-1,bg+:-1,preview-bg:-1,list-bg:-1,input-bg:-1,header-bg:-1"
    transparent = 'set -gx FZF_DEFAULT_OPTS "$FZF_DEFAULT_OPTS --height=100% --color=' + colors + '"; '
    argv = layout(cwd, transparent + command)["block"]["options"]["command"]
    failure = None
    try:
        request("open", argv=argv, cwd=str(cwd))
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            status = request("status")
            if status != -1:
                return status
            time.sleep(0.2)
        raise RuntimeError("Picker timed out")
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            deadline = time.monotonic() + 3
            while request("close") == -1:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Neovim picker cleanup timed out")
                time.sleep(0.2)
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
            if failure is None:
                raise
            print(f"rex-opener: picker cleanup also failed: {error}", file=sys.stderr)


def popup(rex, session, block, client, cwd, command):
    if not client:
        raise RuntimeError("Picker needs an unambiguous app client; pass --client CLIENT_ID")
    original = rex.call("session.describe_block", session_id=session, block_id=block)["block"]
    pid, program = foreground_program(rex, session, block)
    if program == "nvim":
        return popup_neovim(neovim_target(pid), cwd, command)
    terminal = layout(cwd, command)
    terminal["block"]["options"]["exit"] = {"on_completion": False}
    window = original["window_id"]
    view = rex.call("session.view", session_id=session)
    source = next(item for item in view["windows"] if item["window_id"] == window)
    previous_zoom, previous_focus = source.get("zoomed_block_id"), source.get("focused_block_id")
    cancelled = False

    def panes(view):
        return [
            pane
            for current in view["windows"]
            for layer in current.get("layers", [])
            for pane in layer.get("blocks", [])
        ]

    created = None
    try:
        created = rex.call(
            "session.new_layer",
            session_id=session,
            window_id=original["window_id"],
            bounds=[0.1, 0.15, 0.8, 0.7],
            layout=terminal,
            focus=False,
        )
        target_block = created["block_ids"][0]
        rex.action(
            client,
            "session.select",
            session_id=session,
            window_id=created.get("window_id", original["window_id"]),
        )
        rex.call("session.focus_block", session_id=session, block_id=target_block)
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            try:
                process = rex.call(
                    "com.superlogical.terminal.process", session_id=session, block_id=target_block, args={}
                )
            except RuntimeError:
                if not any(
                    pane["block_id"] == target_block for pane in panes(rex.call("session.view", session_id=session))
                ):
                    cancelled = True
                    return 130
                raise
            if process.get("last_exit") is not None:
                return process["last_exit"]["exit_code"]
            time.sleep(0.2)
        raise RuntimeError("Picker timed out")
    finally:
        if created:
            try:
                rex.call("session.close_layer", session_id=session, layer_id=created["layer_id"])
            except RuntimeError:
                if any(
                    pane["block_id"] == created["block_ids"][0]
                    for pane in panes(rex.call("session.view", session_id=session))
                ):
                    raise
            if previous_zoom or (cancelled and previous_focus):
                view = rex.call("session.view", session_id=session)
                source = next((item for item in view["windows"] if item["window_id"] == window), {})
                if view.get("active_window_id") == window:
                    if cancelled and any(pane["block_id"] == previous_focus for pane in panes({"windows": [source]})):
                        rex.call("session.focus_block", session_id=session, block_id=previous_focus)
                        source["focused_block_id"] = previous_focus
                    if previous_zoom and source.get("focused_block_id") == previous_zoom:
                        rex.call("session.zoom_block", session_id=session, block_id=previous_zoom, zoom=True)


def choose(rex, session, block, client, cwd, rows, prompt):
    with tempfile.TemporaryDirectory(prefix="rex-select-") as temporary:
        source, selected = [Path(temporary) / name for name in ("rows", "selected")]
        source.write_text("\n".join(rows) + "\n")
        command = (
            f"fzf --height=100% --delimiter='\\t' --with-nth=2.. --prompt={shlex.quote(prompt)}"
            f" < {shlex.quote(str(source))} > {shlex.quote(str(selected))}"
        )
        status = popup(rex, session, block, client, cwd, command)
        if status not in (0, 1, 130):
            raise RuntimeError(f"Picker exited with status {status}; check that fzf is on PATH")
        return selected.read_text().split("\t", 1)[0].strip() if status == 0 and selected.exists() else ""


def capture_text(rex, session, block):
    args = {"session_id": session, "block_id": block}
    result = rex.call(
        "com.superlogical.terminal.format", **args, args={"format": "text", "unwrap": True, "max_bytes": 983040}
    )
    parts = [result["content"]]
    offset = len(parts[0].encode("utf-8"))
    while "id" in result and not result.get("eof", False):
        result = rex.call(
            "com.superlogical.terminal.blob_read",
            **args,
            args={"id": result["id"], "offset": offset, "max_bytes": 983040},
        )
        if not result["data"] and not result["eof"]:
            raise RuntimeError("Scrollback read made no progress")
        parts.append(result["data"])
        offset += len(parts[-1].encode("utf-8"))
    return "".join(parts)


def text_targets(text, cwd):
    # Explicit schemes are limited to browser targets; never reinterpret another scheme's host.
    url = re.compile(
        r"(?<![\w@./:-])(?:https?|ftp)://[^\s<>\"`]+?"
        r"(?=$|[\s<>\"`]|,(?=(?:https?|ftp)://))"
        r"|(?<![\w@./:-])(?:[\w-]+\.)+"
        r"(?:com|org|net|io|dev|co\.uk|gov\.uk|edu|info|me|app|lighting|ai|xyz|tech|gg|so|cloud)"
        r"\b(?::\d+)?(?:[/?#][^\s<>\"`]*)?",
        re.I,
    )
    text_extensions = set(
        (
            "md markdown txt log json jsonc yaml yml toml typ xml csv tsv sh bash zsh fish js jsx ts tsx "
            "mjs cjs py rb go rs lua vim java kt kts c cc cpp h hpp cs php swift sql tf tfvars nix conf ini env"
        ).split()
    )
    targets = []
    spans = []

    def trim(value):
        value = value.rstrip(".,;:!?")
        for left, right in (("(", ")"), ("[", "]"), ("{", "}")):
            while value.endswith(right) and value.count(right) > value.count(left):
                value = value[:-1]
        return value

    for match in url.finditer(text):
        value = trim(match.group())
        # A surrounding single quote is punctuation; apostrophes inside a URL are valid.
        if match.start() and text[match.start() - 1] == "'" and value.endswith("'"):
            value = value[:-1]
        normalized = value if "://" in value else "https://" + value
        targets.append((match.start(), ("url", normalized, value.split("://", 1)[-1], "")))
        spans.append(match.span())

    candidates = list(re.finditer(r"!?\[[^\]]*\]\(([^\n]+?)\)(?=\s|$)", text))
    candidates += list(re.finditer(r"^\*\*\* (?:Add File:|Update File:|Move to:) (.+)$|^([^\n]+)$", text, re.M))
    candidates += list(re.finditer(r"""["'`]([^\n]+?)["'`]|(?:^|(?<=[\s<(]))([^\s<>"'`|]+)""", text, re.M))
    for match in candidates:
        if any(start <= match.start() < end for start, end in spans):
            continue
        raw = next(group for group in match.groups() if group is not None)
        value = trim(raw.strip())
        line = ""
        location = re.search(r":(\d+)(?::\d+)?$", value)
        if location:
            line = location[1]
            value = value[: location.start()]
        if value.startswith("file://"):
            value = unquote(urlparse(value).path)
        elif re.match(r"\w+://", value):
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(cwd) / path
        try:
            if not path.exists() or path.resolve() == Path(cwd).resolve():
                continue
            path = path.resolve()
        except (OSError, ValueError):
            continue
        extension = path.suffix.lower().lstrip(".")
        kind = (
            "dir"
            if path.is_dir()
            else "md"
            if extension in ("md", "markdown")
            else "text"
            if extension in text_extensions
            else "video"
            if extension in ("mp4", "mov", "m4v", "webm", "mkv", "avi")
            else "file"
        )
        label = str(path.relative_to(cwd)) if path.is_relative_to(cwd) else str(path)
        targets.append((match.start(), (kind, str(path), label + (":" + line if line else ""), line)))
    seen = set()
    result = []
    for _, target in sorted(targets, key=lambda item: item[0], reverse=True):
        key = target[0], target[1], target[3]
        if key not in seen:
            seen.add(key)
            result.append(target)
    return result


def split_command(rex, session, block, client, cwd, command):
    size = rex.call("com.superlogical.terminal.size", session_id=session, block_id=block, args={})
    direction = "horizontal" if size["columns"] * 8 >= size["rows"] * 20 else "vertical"
    created = rex.call(
        "session.new_split",
        session_id=session,
        anchor_id=block,
        direction=direction,
        side="after",
        ratio=0.5,
        layout=layout(cwd, command),
        focus=False,
    )
    window = rex.call("session.describe_block", session_id=session, block_id=block)["block"]["window_id"]
    route = {"session": session, "window": window, "block": created["block_ids"][0]}
    shared = any(
        peer.get("info", {}).get("kind") == "app"
        and peer["client_id"] != client
        and session in peer.get("session_ids", [])
        for peer in rex.call("client.list")["clients"]
    )
    if client and not shared:
        rex.action(client, "session.select", session_id=session, window_id=window)
        rex.call("session.focus_block", session_id=session, block_id=route["block"])
    return route


def open_target(rex, session, block, client, cwd, target):
    kind, value, _, line = target
    if kind in ("md", "text"):
        args = ["nvim"]
        if line:
            args.append("+" + str(max(1, int(line))))
        return split_command(rex, session, block, client, cwd, shlex.join([*args, "--", value]))
    if kind == "dir":
        return popup(rex, session, block, client, Path(value), shlex.join(["yazi", value]))
    command = ["open"]
    if kind == "video" and Path("/Applications/IINA.app").is_dir():
        command += ["-a", "IINA"]
    run([*command, value])


def open_text_picker(rex, session, block, client, cwd):
    targets = text_targets(capture_text(rex, session, block), cwd)
    if not targets:
        raise RuntimeError("No URL or existing file found in this block's output")
    if len(targets) == 1:
        selected = "0"
    else:
        rows = [f"{index}\t{kind}\t{label}" for index, (kind, _, label, _) in enumerate(targets)]
        selected = choose(rex, session, block, client, cwd, rows, "open URL / file ❯ ")
    if selected:
        open_target(rex, session, block, client, cwd, targets[int(selected)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--block", required=True)
    parser.add_argument("--client", default="")
    args = parser.parse_args()
    rex = Rex()
    _, cwd, client = context(rex, args.session, args.block, args.client)
    try:
        open_text_picker(rex, args.session, args.block, client, cwd)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        if client:
            message = re.sub(r"[\x00-\x1f\x7f]", " ", str(error))
            command = (
                "printf '%s\\n' "
                + shlex.quote("Rex opener: " + message)
                + "; read --prompt-str='Press Enter to close: '"
            )
            try:
                popup(rex, args.session, args.block, client, cwd, command)
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
                pass  # Preserve the original failure if the invoking client has closed.
        raise


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"rex-opener: {error}", file=sys.stderr)
        sys.exit(1)
