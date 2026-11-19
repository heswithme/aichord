#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# ///
"""End-to-end tests of the aichord core with simulated agents (no real AI tools are started).

Run: uv run --no-project python tests/test_core.py
"""
import json
import re
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "aichord.py"
SCRIPT = Path(os.environ.get("AICHORD_TEST_SCRIPT") or SCRIPT).resolve()  # a candidate copy can be tested in its place
# Every command under test reads an empty stdin, never the terminal the tests run in: a restart that finds an agent
# at work asks on the terminal, and would wait for an answer.
os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
PY = sys.executable
FAILS: list[str] = []


# When an agent in an aichord session runs these tests, its own identity, session, message socket and tmux must not
# leak in: a simulated claude with the real session's socket would post into that real session.
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_CODE_") and k not in (
    "AICHORD_AGENT", "AICHORD_TOOL", "AICHORD_LAUNCHED", "CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID", "TMUX",
    "TMUX_PANE")}


def run(agent, *args, stdin=None, env_extra=None, check=True, background=False):
    env = {**CLEAN_ENV, "AICHORD_ROOT": str(ROOT), "TMUX_PANE": ""}
    if agent:
        env["AICHORD_AGENT"] = agent
    env.update(env_extra or {})
    cmd = [PY, "-I", str(SCRIPT), *args]
    if background:
        return subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out = subprocess.run(cmd, cwd=ROOT, env=env, input=stdin, capture_output=True, text=True, timeout=120)
    if check and out.returncode not in (0, 2):
        raise AssertionError(f"{args} failed ({out.returncode}): {out.stderr}")
    return out


def hook(agent, tool, event, payload, env_extra=None):
    return run(agent, "hook", tool, event, stdin=json.dumps(payload), env_extra=env_extra)


def context(out):
    return json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"] if out.stdout.strip() else ""


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"\n     {detail}"))
    if not cond:
        FAILS.append(name)


def q(sql, *p):
    c = sqlite3.connect(ROOT / ".aichord" / "aichord.db")
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, p).fetchall()
    finally:
        c.close()


def wake_prompt(agent):
    """The prompt of the wake-up that holds the agent's reservation, as its tool would deliver it."""
    tag = q("SELECT value FROM meta WHERE key=?", f"wake_pending:{agent}")
    return f"[aichord] New messages for you (wake-up {tag[0]['value'] if tag else ''}):\n\n..."


class FakeSocket:
    """A stand-in for a Claude Code session's message socket: it records what aichord posts to it."""

    def __init__(self, path):
        self.path, self.frames = str(path), []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(8)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            data = b""
            while chunk := c.recv(65536):
                data += chunk
            self.frames.append([json.loads(x) for x in data.decode().splitlines() if x.strip()])
            c.close()

    def texts(self):
        return [f[-1]["message"]["content"] for f in self.frames]

    def close(self):
        self.srv.close()
        os.unlink(self.path)


with tempfile.TemporaryDirectory() as tmp:
    ROOT = Path(tmp) / "proj"
    ROOT.mkdir()
    SOCKDIR = Path(tempfile.mkdtemp(prefix="acs", dir="/tmp"))  # a short path: socket paths are limited to ~100 bytes
    # A stand-in codex comes first on the PATH, so a wake-up of a simulated codex never reaches the real Codex. It
    # fails, so the mail waits for codex's hooks, as the sections that don't test wake-ups expect.
    STUBS = Path(tmp) / "stubs"
    STUBS.mkdir()
    (STUBS / "codex").write_text("#!/bin/sh\nexit 1\n")
    (STUBS / "codex").chmod(0o755)
    os.environ["PATH"] = f"{STUBS}:{os.environ['PATH']}"
    CLEAN_ENV["PATH"] = os.environ["PATH"]
    CLEAN_ENV["CLAUDE_CONFIG_DIR"] = str(Path(tmp) / "claude-config")  # the user's own Claude Code settings stay out
    CONFIG = Path(tmp) / "aichord-config"  # and so do their aichord settings: each test that needs some writes them here
    CONFIG.mkdir()
    CLEAN_ENV["AICHORD_CONFIG_DIR"] = str(CONFIG)
    g = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout  # noqa
    g("init", "-q")
    (ROOT / "a.txt").write_text("one\n")
    g("add", "a.txt")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qm",
                    "init"], cwd=ROOT, check=True)

    # 1. Agents come alive through their session-start hooks.
    starts = {}
    for name, tool in (("claude", "claude"), ("agy", "claude"), ("codex", "codex")):
        starts[name] = hook(name, tool, "SessionStart", {"session_id": f"s-{name}", "source": "startup"}).stdout
    c = sqlite3.connect(ROOT / ".aichord" / "aichord.db")
    c.execute("UPDATE agents SET state='idle', updated=0")
    c.commit()
    c.close()
    check("three agents registered", len(q("SELECT * FROM agents")) == 3)

    # 2. Hooks do nothing outside aichord sessions.
    out = run(None, "hook", "claude", "UserPromptSubmit", stdin=json.dumps({"prompt": "hi"}))
    check("hook is silent without AICHORD_AGENT", out.stdout == "" and out.returncode == 0)

    # 3. The user types to agy: recorded verbatim, shared with the others as context.
    ctx = json.loads(starts["agy"])["hookSpecificOutput"]["additionalContext"]
    check("session start shows the seat card", "You are agy" in ctx and "claude" in ctx, ctx[:300])
    out = hook("agy", "claude", "UserPromptSubmit", {"session_id": "s-agy", "prompt": "Should the oracle be settable?\nKeep it simple."})
    check("seat card is not repeated on the first prompt", "You are agy" not in out.stdout, out.stdout[:200])
    m = q("SELECT * FROM messages WHERE kind='user'")
    check("the user's message recorded verbatim", m and m[0]["body"] == "Should the oracle be settable?\nKeep it simple.")
    out = hook("claude", "claude", "PostToolUse", {"session_id": "s-claude", "tool_name": "Bash"})
    ctx = context(out)
    check("claude sees the user's message to agy as context", "The user → agy" in ctx and "Keep it simple." in ctx, out.stdout)
    out = hook("claude", "claude", "PostToolUse", {"session_id": "s-claude", "tool_name": "Bash"})
    check("context is shown only once", out.stdout == "")

    # 4. aichord's own prompts (the first prompt at launch, a wake-up) are not mistaken for the user's, and don't make
    # the turn the user's.
    before = len(q("SELECT * FROM messages WHERE kind='user'"))
    for line in ("[aichord] Your session has started.", "[aichord] New messages for you:\n\n#1 note"):
        hook("codex", "codex", "UserPromptSubmit", {"session_id": "s-codex", "prompt": line})
    check("aichord's own prompts are not recorded as the user's", len(q("SELECT * FROM messages WHERE kind='user'")) == before
          and q("SELECT turn_from FROM agents WHERE name='codex'")[0]["turn_from"] != "user")

    # 5. agy asks both; the Stop hook makes claude continue with the question. A question read but not answered makes
    # the next stop carry on once more, with a reminder; after that the agent may stop.
    out = run("agy", "ask", "--no-wait", "--subject", "oracle", stdin="Should the oracle address be settable after deploy?")
    ask_id = int(out.stdout.split("#")[1].split()[0])
    check("ask goes to claude and codex", sorted(r["recipient"] for r in q("SELECT recipient FROM deliveries WHERE message=?", ask_id)) == ["claude", "codex"])
    out = hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False})
    stop = json.loads(out.stdout) if out.stdout else {}
    check("Stop hook blocks with the question", stop.get("decision") == "block" and "oracle address" in stop.get("reason", ""), out.stdout)
    out = hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": True})
    stop = json.loads(out.stdout) if out.stdout else {}
    check("a stop after reading a question without answering it carries on once, with a reminder",
          stop.get("decision") == "block" and f"you have read #{ask_id} from agy but not answered" in stop.get("reason", ""), out.stdout)
    out = hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": True})
    check("the next stop lets the agent stop", out.stdout == "")

    # 6. Replies and waiting.
    run("claude", "reply", str(ask_id), stdin="ANSWER: Yes, behind the admin role.")
    out = run("agy", "wait", str(ask_id), "--max", "5")
    check("wait shows the first reply and reports the missing one", "Yes, behind the admin role" in out.stdout and "Still waiting on: codex" in out.stdout, out.stdout)
    p = run("agy", "wait", str(ask_id), "--max", "20", background=True)
    time.sleep(1.5)
    run("codex", "reply", str(ask_id), stdin="ANSWER: Yes, with a two-step change and an event.")
    so, se = p.communicate(timeout=30)
    check("wait returns when the last reply arrives", "two-step change" in so and "All replies are in" in so, so + se)

    # 7. No deadlock: agy waits on claude while claude asks agy.
    p = run("agy", "ask", "--to", "claude", "--wait", "30", "Question A for claude", background=True)
    time.sleep(1.5)
    run("claude", "ask", "--to", "agy", "--no-wait", "Question B for agy")
    so, se = p.communicate(timeout=40)
    check("a waiting agent is interrupted by a new question for it", "new question for you arrived" in so and "Question B" in so, so + se)
    for who, body in (("claude", "Question A for claude"), ("agy", "Question B for agy")):
        run(who, "reply", str(q("SELECT id FROM messages WHERE body=?", body)[0]["id"]), "Answered.")

    # 8. Decisions.
    run("agy", "decide", "The oracle address is settable by the admin role.")
    dec = (ROOT / ".aichord" / "decisions.md").read_text()
    check("decision written to decisions.md", "D1" in dec and "settable by the admin role" in dec)
    check("decision shared with the others", len(q("SELECT * FROM deliveries d JOIN messages m ON m.id=d.message WHERE m.kind='decision'")) == 2)

    # 9. Review snapshot: includes new files, leaves the working tree and staging area alone.
    (ROOT / "a.txt").write_text("two\n")
    (ROOT / "new_contract.vy").write_text("# new\n")
    status_before = g("status", "--porcelain")
    out = run("claude", "review", "--no-wait", "--subject", "milestone 1", stdin="Changed a.txt, added new_contract.vy.")
    rid = int(out.stdout.split("#")[1].split()[0])
    ref = q("SELECT ref FROM messages WHERE id=?", rid)[0]["ref"]
    check("snapshot contains the new untracked file", g("show", f"{ref}:new_contract.vy") == "# new\n")
    check("snapshot contains the modified file", g("show", f"{ref}:a.txt") == "two\n")
    check("working tree and staging area untouched", g("status", "--porcelain") == status_before, g("status", "--porcelain"))
    check("snapshot kept by a aichord ref", "refs/aichord/review-1" in g("for-each-ref", "refs/aichord/"))
    body = q("SELECT body FROM messages WHERE id=?", rid)[0]["body"]
    check("review text gives exact diff commands", f"git diff {g('rev-parse', 'HEAD').strip()[:12]} {ref[:12]}" in body, body[:400])
    check("review goes to agy and codex", sorted(r["recipient"] for r in q("SELECT recipient FROM deliveries WHERE message=?", rid)) == ["agy", "codex"])
    out = run("codex", "read", str(rid))
    check("a review request asks for findings marked Must fix or Suggestion", "'Must fix:' or 'Suggestion:'" in out.stdout
          and "at most 3 suggestions" in out.stdout, out.stdout[-400:])

    # 10. Plain-language warning.
    out = run("agy", "send", "--wake", "--to", "claude", "The RPF bound in M1 uses `RPF_MAX` and src/x.py.")
    check("warns about undefined terms", "RPF" in out.stderr and "M1" in out.stderr and "RPF_MAX" not in out.stderr, out.stderr)
    run("agy", "glossary", "add", "RPF", "revenue participation fraction")
    out = run("agy", "send", "--wake", "--to", "claude", "The RPF bound in M1.")
    check("glossary terms are accepted", "RPF" not in out.stderr and "M1" in out.stderr, out.stderr)

    # 11. When mail is stored, the sending command wakes an idle Claude Code agent through its message socket, and the
    # wake-up carries the mail. The socket is recorded from the agent's hooks. One wake-up at a time: until the woken
    # turn's first hook the agent counts as busy. A failed wake-up puts the mail back and is tried again later.
    def set_sql(sql, *p):
        c = sqlite3.connect(ROOT / ".aichord" / "aichord.db")
        c.execute(sql, p)
        c.commit()
        c.close()

    sock = FakeSocket(SOCKDIR / "claude.sock")
    claude_env = {"CLAUDE_CODE_MESSAGING_SOCKET": sock.path, "CLAUDE_CODE_MESSAGING_TOKEN": "tok-1"}
    for _ in range(3):  # clear claude's backlog; the hooks record its socket
        hook("claude", "claude", "PostToolUse", {"session_id": "s-claude", "tool_name": "Bash"}, env_extra=claude_env)
    set_sql("UPDATE agents SET state='idle' WHERE name='claude'")
    out = run("codex", "send", "--wake", "--to", "claude", "Watcher test message.")
    check("an idle Claude Code agent is woken through its message socket, with the mail and the session's token",
          len(sock.frames) == 1 and sock.frames[0][0] == {"type": "auth", "token": "tok-1"}
          and sock.texts()[0].startswith("[aichord] New messages for you (wake-up ") and "Watcher test message." in sock.texts()[0],
          sock.frames)
    check("the receipt says the wake-up's turn hasn't started yet", "claude: busy, woken by aichord" in out.stdout, out.stdout)
    run("codex", "send", "--wake", "--to", "claude", "A second message before the turn starts.")
    check("no second wake-up while the first one's turn hasn't started", len(sock.frames) == 1, sock.frames)
    ctx = context(hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": sock.texts()[0]}))
    check("the woken turn's first hook shows only the mail that came after the wake-up",
          "A second message" in ctx and "Watcher test message" not in ctx, ctx)
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False})
    sock.close()
    out = run("codex", "send", "--wake", "--to", "claude", "Nobody is listening for this one.")
    nobody = q("SELECT id FROM messages WHERE body='Nobody is listening for this one.'")[0]["id"]
    d = dict(q("SELECT notified, read FROM deliveries WHERE message=? AND recipient='claude'", nobody)[0])
    check("a failed wake-up puts the mail back, leaves the agent idle, and the receipt says it failed",
          d == {"notified": None, "read": None} and q("SELECT state FROM agents WHERE name='claude'")[0]["state"] == "idle"
          and "the last wake-up" in out.stdout and "failed" in out.stdout, f"{d} {out.stdout}")
    sock = FakeSocket(SOCKDIR / "claude.sock")
    probe_tend = ("import importlib.util, sys\n"
                  "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                  "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                  "t.WAKE_RETRY = float(sys.argv[2]); t.tend(t.db())\n")
    env_card = {**CLEAN_ENV, "AICHORD_ROOT": str(ROOT)}
    tend = lambda retry="0", extra=None: subprocess.run([PY, "-I", "-c", probe_tend, str(SCRIPT), retry], capture_output=True,  # noqa: E731
                                                        text=True, env={**env_card, **(extra or {})})
    tend("3600")
    waited = len(sock.frames)
    tend("0")
    check("a failed wake-up is tried again by a later aichord command, once its waiting time has passed",
          waited == 0 and len(sock.frames) == 1 and "Nobody is listening" in sock.texts()[0], f"{waited} {sock.frames}")
    # A message stored while the Stop hook runs, just after it looked for mail: the hook marked the agent idle first,
    # so the sender wakes it (stored the other way round, the message would wait with nobody to show it).
    hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": sock.texts()[-1]})  # the retry's turn
    set_sql("UPDATE agents SET state='busy', detail='last used Bash' WHERE name='claude'")
    probe_race = ("import importlib.util, sys\n"
                  "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                  "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                  "conn = t.db(); done = []; looked = t.claim_unseen\n"
                  "def claim(conn, agent, *a, **k):\n"
                  "    out = looked(conn, agent, *a, **k)\n"
                  "    if agent == 'claude' and not k.get('peer_turn') and not done:\n"
                  "        done.append(1); t.post(conn, 'codex', ['claude'], {'claude': 'read'}, 'note', 'Posted while claude stops.')\n"
                  "    return out\n"
                  "t.claim_unseen = claim\n"
                  "t.stop_hook(conn, 'claude', 'claude', t.agent_row(conn, 'claude'), {})\n")
    frames = len(sock.frames)
    out = subprocess.run([PY, "-I", "-c", probe_race, str(SCRIPT)], capture_output=True, text=True, env=env_card)
    check("a message stored while the Stop hook looks for mail still wakes the agent",
          len(sock.frames) == frames + 1 and "Posted while claude stops." in sock.texts()[-1], out.stderr + str(sock.frames[frames:]))
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False})
    set_sql("UPDATE meta SET value='' WHERE key='socket:claude'")  # later sections wake claude only where they say so

    # 12. A lead's answer to the user is shared (Codex Stop gives last_assistant_message).
    hook("codex", "codex", "UserPromptSubmit", {"session_id": "s-codex", "prompt": "Check the fee bound."})
    hook("codex", "codex", "Stop", {"session_id": "s-codex", "stop_hook_active": False,
                                    "last_assistant_message": "The fee bound holds for all tested pools."})
    ans = q("SELECT * FROM messages WHERE kind='answer'")
    check("lead's answer to the user recorded for the others", ans and "fee bound holds" in ans[-1]["body"])
    asked = q("SELECT id FROM messages WHERE kind='user' AND body='Check the fee bound.'")
    check("the answer is filed after the user's message, in its thread", asked and ans[-1]["thread"] == asked[0]["id"],
          [dict(r) for r in ans[-1:]])

    # 13. Helper threads and subagents don't count as the agent (Codex memory consolidation ran codex's hooks).
    before = len(q("SELECT * FROM messages WHERE kind='user'"))
    hook("codex", "codex", "UserPromptSubmit", {"session_id": "s-memory", "transcript_path": None,
                                                 "prompt": "## Memory Writing Agent: Phase 2"})
    hook("codex", "codex", "UserPromptSubmit", {"session_id": "s-codex", "agent_id": "a1", "agent_type": "explorer",
                                                 "prompt": "subagent task text"})
    hook("codex", "codex", "SessionStart", {"session_id": "s-other", "transcript_path": "/x/rollout.jsonl", "source": "startup"})
    hook("codex", "codex", "SessionEnd", {"session_id": "s-memory", "transcript_path": None})
    row = q("SELECT session_id, state FROM agents WHERE name='codex'")[0]
    check("helper prompts are not recorded as the user's", len(q("SELECT * FROM messages WHERE kind='user'")) == before)
    check("helper sessions don't take over the agent's session", row["session_id"] == "s-codex", dict(row))
    check("a helper ending doesn't mark the agent gone", row["state"] != "gone", dict(row))
    out = run("codex", "status", env_extra={"AICHORD_TOOL": "codex", "CODEX_THREAD_ID": "s-codex-2"})
    check("the agent's own command confirms its session", q("SELECT session_id FROM agents WHERE name='codex'")[0]["session_id"] == "s-codex-2")

    # 13b. Claude Code delivers subagent reports, background results and socket messages as prompts. They are not
    # the user's, and one arriving mid-turn must not stop the lead's answer to the user from being shared.
    hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": "Rename the tool."})
    before = len(q("SELECT * FROM messages WHERE kind='user'"))
    for frame in ('<agent-message from="a55f">\n[Subagent hand-back] The names were checked.',
                  "<system-reminder>\nA background task finished.", "<task-notification>\n<task-id>x</task-id>",
                  '<cross-session-message from="aichord">\n[aichord] New messages for you:'):
        hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": frame})
    check("subagent reports and socket messages are not recorded as the user's",
          len(q("SELECT * FROM messages WHERE kind='user'")) == before,
          [r["body"][:60] for r in q("SELECT body FROM messages WHERE kind='user'")])
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False,
                                    "last_assistant_message": "Five names are proposed."})
    check("the answer to the user is shared after a mid-turn subagent report",
          "Five names are proposed." in q("SELECT body FROM messages WHERE kind='answer' ORDER BY id DESC LIMIT 1")[0]["body"])

    # 14. Status and log render.
    out = run("agy", "status")
    check("status lists agents", "claude" in out.stdout and "codex" in out.stdout, out.stdout)
    log = (ROOT / ".aichord" / "log.md").read_text()
    check("log.md has every message", log.count("### #") == len(q("SELECT * FROM messages")))

    # 15. Every tool starts with aichord's first prompt. agy (Antigravity) keeps its permission prompts unless the
    # user's settings name flags for it, and a flag is never repeated. Claude Code gets no background checker hook.
    def settings(**values):  # the user's config.json for the commands that follow; none at all without values
        (CONFIG / "config.json").write_text(json.dumps(values)) if values else (CONFIG / "config.json").unlink(missing_ok=True)

    probe = ("import importlib.util, json, sys\n"
             f"spec = importlib.util.spec_from_file_location('aichord', {str(SCRIPT)!r})\n"
             "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
             "t.AGY_HOOKS_FILE = t.Path(sys.argv[1]); root = t.Path(sys.argv[2])\n"
             "print(json.dumps([t.FIRST_PROMPT, t.agy_launch_command('agy', root, []),\n"
             "                  t.agy_launch_command('agy', root, ['--dangerously-skip-permissions', '-c']),\n"
             "                  t.launch_command('claude', 'claude', root, ['--model', 'opus']),\n"
             "                  t.launch_command('codex', 'codex', root, []), sorted(t.agy_hooks_entry()),\n"
             "                  json.loads(t.claude_settings(root).read_text())]))\n")
    hooks_file = Path(tmp) / "hooks.json"
    hooks_file.write_text('{"aichord": {}}')
    out = subprocess.run([PY, "-I", "-c", probe, str(hooks_file), str(ROOT)], capture_output=True, text=True, env=env_card)
    first, *cmds, agy_events, claude_hooks = json.loads(out.stdout) if out.returncode == 0 else ["", [], [], [], [], [], {}]
    check("agy keeps Antigravity's permission prompts by default, and starts with a first prompt",
          cmds[0] == ["agy", "-i", first] and cmds[1] == ["agy", "--dangerously-skip-permissions", "-c", "-i", first],
          out.stdout + out.stderr)
    settings(agy_flags=["--dangerously-skip-permissions"])
    out = subprocess.run([PY, "-I", "-c", probe, str(hooks_file), str(ROOT)], capture_output=True, text=True, env=env_card)
    settings()
    flagged = json.loads(out.stdout)[1:3] if out.returncode == 0 else [[], []]
    check("agy gets the flags the user's settings name, and one also passed after -- isn't repeated",
          flagged == [["agy", "--dangerously-skip-permissions", "-i", first],
                      ["agy", "--dangerously-skip-permissions", "-c", "-i", first]], out.stdout + out.stderr)
    check("Claude Code and Codex get the first prompt after the tool's own flags", first.startswith("[aichord] ")
          and cmds[2][-3:] == ["--model", "opus", first] and cmds[3][0] == "codex" and cmds[3][-1] == first, out.stdout)
    check("Antigravity gets only the hooks whose output it uses", agy_events == ["PreInvocation", "SessionStart", "Stop"], agy_events)
    stop_hooks = claude_hooks.get("hooks", {}).get("Stop", [{}])[0].get("hooks", [])
    check("Claude Code's Stop has only aichord's stop hook, no background checker",
          len(stop_hooks) == 1 and stop_hooks[0]["command"].endswith("hook claude Stop") and "async" not in stop_hooks[0], stop_hooks)

    # 16. Replies take their text as arguments too. Each of the user's messages starts a thread of its own, and the
    # lead's answer is filed after the latest one (a timeline).
    ask = run("agy", "ask", "--to", "claude", "--no-wait", "Is the cache warm?").stdout
    ask_id = int(ask.split("#")[1].split()[0])
    out = run("claude", "reply", str(ask_id), "Yes,", "it", "is.")
    check("reply takes its text as arguments", "Sent reply" in out.stdout and
          q("SELECT body FROM messages WHERE kind='reply' ORDER BY id DESC LIMIT 1")[0]["body"] == "Yes, it is.", out.stderr)
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False})
    hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": "Rename it to aichord."})
    hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": "And keep the full log."})
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False, "last_assistant_message": "Done."})
    turn = q("SELECT id, thread, kind FROM messages WHERE body IN ('Rename it to aichord.', 'And keep the full log.', 'Done.') ORDER BY id")
    check("each of the user's messages starts its own thread, and the answer is filed after the latest one",
          len(turn) == 3 and turn[0]["thread"] == turn[0]["id"] and turn[1]["thread"] == turn[1]["id"]
          and turn[2]["thread"] == turn[1]["id"], [dict(r) for r in turn])
    out = run("codex", "read", str(turn[2]["id"]))
    check("an answer says it came after the user's message", "claude to the user" in out.stdout
          and f"after the user's #{turn[1]['id']}" in out.stdout, out.stdout)

    # 17. An Antigravity slash command is not recorded, and doesn't break the hook.
    transcript = Path(tmp) / "agy-transcript.jsonl"
    transcript.write_text(json.dumps({"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>/help</USER_REQUEST>"}) + "\n")
    before = len(q("SELECT * FROM messages WHERE kind='user'"))
    out = hook("agy", "agy", "PreInvocation", {"transcriptPath": str(transcript)})
    with open(transcript, "a") as f:
        f.write(json.dumps({"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>Check the spec.</USER_REQUEST>"}) + "\n")
    hook("agy", "agy", "PreInvocation", {"transcriptPath": str(transcript)})
    errors = (ROOT / ".aichord" / "runtime" / "hooks.log").read_text() if (ROOT / ".aichord" / "runtime" / "hooks.log").exists() else ""
    check("an Antigravity slash command is skipped without a hook error",
          "Error" not in errors and len(q("SELECT * FROM messages WHERE kind='user'")) == before + 1, errors[-300:])

    # 18. Decisions can replace older ones; only one of two agents can replace the same decision.
    run("agy", "decide", "--supersedes", "D1", "The oracle address is fixed at deploy.")
    dec = (ROOT / ".aichord" / "decisions.md").read_text()
    out = run("codex", "decide", "--supersedes", "D1", "Something else.", check=False)
    check("a replaced decision is marked in decisions.md", "*(replaced by D2)*" in dec and "fixed at deploy" in dec, dec)
    check("a decision can't be replaced twice", out.returncode != 0 and "already replaced by D2" in out.stderr, out.stderr)

    # 19. Closing a thread nobody needs to answer.
    stale = int(run("claude", "ask", "--to", "codex", "--no-wait", "Check-in after restart?").stdout.split("#")[1].split()[0])
    run("claude", "close", str(stale), "Superseded by the restart.")
    left = q("SELECT * FROM deliveries WHERE message=? AND action='reply' AND replied IS NULL", stale)
    check("close finishes the thread's open questions", not left and
          q("SELECT closed FROM threads WHERE id=?", stale)[0]["closed"] == "claude: Superseded by the restart.")

    # 20. Search.
    out = run("codex", "recall", "oracle")
    check("recall finds decisions and messages, newest first", out.stdout.splitlines()[0].startswith("D2 ")
          and "#" in out.stdout and "oracle" in out.stdout.lower(), out.stdout[:400])
    out = run("codex", "recall", "e", "--limit", "2")
    check("recall says how many more matches there are", out.stdout.count("\n") == 3 and "more matches" in out.stdout, out.stdout)

    # 21. The start note keeps to its budget and never silently drops decisions in force.
    probe_card = ("import importlib.util, sqlite3, sys, time\n"
                  "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                  "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                  "conn = t.db()\n"
                  "if sys.argv[2] == 'flood':\n"
                  "    for i in range(80):\n"
                  "        conn.execute('INSERT INTO decisions(ts, by, text) VALUES(?, ?, ?)', (time.time(), 'claude', f'Decision number {i} ' + 'x' * 250))\n"
                  "    t.post(conn, 'user', ['claude'], {}, 'user', 'word ' * 400)\n"
                  "print(t.seat_card(conn, 'claude'))\n")
    card = subprocess.run([PY, "-I", "-c", probe_card, str(SCRIPT), "plain"], capture_output=True, text=True, env=env_card).stdout
    check("the start note lists finished threads, with closed ones marked", f"#{stale} claude → codex" in card
          and "closed by claude: Superseded" in card, card[-1500:])
    card = subprocess.run([PY, "-I", "-c", probe_card, str(SCRIPT), "flood"], capture_output=True, text=True, env=env_card).stdout
    lines = card.rstrip("\n").split("\n")
    check("the start note stays within its byte budget (Claude Code's 9,000 less the room kept for mail)",
          len(card.encode()) <= 7500, f"{len(card.encode())} bytes")
    check("decisions that don't fit are named, with where to read them", "older decisions in force are in .aichord/decisions.md" in card, card[-800:])
    check("a long message from the user is clipped with where to read it in full", "full: aichord read" in card, card[-800:])

    # 23. The answer to the user is saved even when mail makes the agent carry on; a new final answer is saved too.
    hook("codex", "codex", "UserPromptSubmit", {"session_id": "s-codex-2", "prompt": "Is the migration safe?"})
    run("agy", "send", "--wake", "--to", "codex", "A note that arrives before codex stops.")
    out = hook("codex", "codex", "Stop", {"session_id": "s-codex-2", "stop_hook_active": False,
                                          "last_assistant_message": "Yes, under the documented shutdown."})
    stop = json.loads(out.stdout) if out.stdout.strip() else {}
    hook("codex", "codex", "Stop", {"session_id": "s-codex-2", "stop_hook_active": True,
                                    "last_assistant_message": "Yes, and agy's note changes nothing."})
    answers = [r["body"] for r in q("SELECT body FROM messages WHERE kind='answer' AND sender='codex' ORDER BY id")][-2:]
    check("the answer is saved before the agent carries on for mail, and a new final answer too",
          stop.get("decision") == "block" and answers == ["Yes, under the documented shutdown.", "Yes, and agy's note changes nothing."],
          f"{stop} {answers}")
    hook("codex", "codex", "UserPromptSubmit", {"session_id": "s-codex-2", "prompt": "And the rename?"})
    run("agy", "send", "--wake", "--to", "codex", "Another note before codex stops.")
    for active in (False, True):
        hook("codex", "codex", "Stop", {"session_id": "s-codex-2", "stop_hook_active": active,
                                        "last_assistant_message": "The rename is fine."})
    check("the same answer at the next stop isn't saved twice",
          len(q("SELECT * FROM messages WHERE kind='answer' AND body='The rename is fine.'")) == 1)

    # 24. Antigravity drops a tool hook's output, so mail waits for the hook before its next model call.
    transcript = Path(tmp) / "agy-turns.jsonl"

    def agy_step(kind, content, source="USER_EXPLICIT"):
        with open(transcript, "a") as f:
            f.write(json.dumps({"type": kind, "source": source, "content": content}) + "\n")

    def agy(event):
        return hook("agy", "agy", event, {"transcriptPath": str(transcript)})

    def last_id():
        return q("SELECT MAX(id) AS n FROM messages")[0]["n"]

    agy("PreInvocation")  # clears agy's backlog
    run("codex", "ask", "--to", "agy", "--no-wait", "Does the tool hook keep this question?")
    qid = last_id()
    out = agy("PostToolUse")
    d = dict(q("SELECT notified, read FROM deliveries WHERE message=? AND recipient='agy'", qid)[0])
    check("a tool hook left from an older Antigravity install prints {} and leaves the mail unshown",
          out.stdout.strip() == "{}" and d == {"notified": None, "read": None}, out.stdout + str(d))
    out = agy("PreInvocation")
    check("the mail reaches agy before its next model call", "Does the tool hook keep this question?" in out.stdout, out.stdout)
    run("agy", "reply", str(qid), "It does.")

    # 25. Antigravity ends a turn whenever agy waits for helpers, and each helper or background task finishing
    # starts a new model call; so does a finished `aichord watch`. Each task goes on with the work it was started for
    # (seen in agy's transcript at its start). The user's final answer is recorded even when peer mail arrives
    # during the pause, and a separate peer turn, helpers included, is not recorded as an answer to the user.
    watch = run("agy", "watch", background=True)  # holds agy's checker lock, so Stop doesn't remind it
    time.sleep(1.5)

    def started(task, description):
        agy_step("GENERIC", f"Tool is running as a background task with task id: conv/{task}\nTask Description: {description}", "MODEL")

    def helpers(*ids):
        agy_step("GENERIC", "Created the following subagents:\n" + "\n".join(json.dumps({"conversationId": i}) for i in ids), "MODEL")

    def finished(sender):
        agy_step("SYSTEM_MESSAGE", f"[Message] sender={sender} priority=MESSAGE_PRIORITY_HIGH content=finished", "SYSTEM")

    def model_call(answer, *before):  # what Antigravity writes during one model call, then the Stop that ends it
        for write in before:
            write()
        agy("PreInvocation")
        agy_step("PLANNER_RESPONSE", answer, "MODEL")
        return agy("Stop")

    def agy_answers():
        return [(r["body"], r["thread"]) for r in q("SELECT body, thread FROM messages WHERE kind='answer' AND sender='agy' ORDER BY id")]

    agy_step("USER_INPUT", "<USER_REQUEST>Draft the spec.</USER_REQUEST>")
    agy("PreInvocation")
    asked = last_id()
    pause = model_call("Three helpers are drafting it.", lambda: started("task-1", "aichord watch"), lambda: helpers("helper-a"))
    run("codex", "send", "--wake", "--to", "agy", "A note that arrives while agy waits for its helpers.")
    hint, _ = watch.communicate(timeout=10)  # agy is idle, so its watch ends with the hint
    run("agy", "inbox")
    watch = run("agy", "watch", background=True)
    time.sleep(1.5)
    model_call("Replied to codex's note.", lambda: finished("conv/task-1"))
    model_call("The spec is in notes/spec.md.", lambda: finished("helper-a"), lambda: started("task-2", "aichord watch"))
    check("a pause while helpers work lets agy stop, with no reminder while its watch runs",
          pause.stdout.strip() == "{}" and "New mail" in hint, pause.stdout + hint)
    check("the user's answers are recorded across a pause with peer mail in it, and the peer reply is not",
          agy_answers()[-2:] == [("Three helpers are drafting it.", asked), ("The spec is in notes/spec.md.", asked)], agy_answers())
    before = agy_answers()
    model_call("Two helpers are checking codex's question.", lambda: finished("conv/task-2"), lambda: helpers("helper-b"))
    model_call("Replied to codex with what the helpers found.", lambda: finished("helper-b"),
               lambda: started("task-3", "uv run --no-project python tests/test_core.py"))
    model_call("The tests codex asked for pass.", lambda: finished("conv/task-3"))
    check("a peer's turn, with its own helpers and background tasks, is not recorded as an answer to the user",
          agy_answers() == before, agy_answers()[len(before):])
    # A helper that started before aichord first read agy's transcript (a resumed session, or an upgrade) has no
    # known owner. Its finish counts as the user's work, so their answer isn't lost after a peer's turn in between.
    transcript = Path(tmp) / "agy-resumed.jsonl"
    agy_step("USER_INPUT", "<USER_REQUEST>Plan the release.</USER_REQUEST>")
    helpers("helper-old")
    agy("PreInvocation")
    planned = last_id()
    model_call("Helpers are on it.", lambda: started("task-4", "aichord watch"))
    model_call("Replied to codex.", lambda: finished("conv/task-4"))
    model_call("The release plan is ready.", lambda: finished("helper-old"))
    check("a helper whose start wasn't seen still counts as the user's work",
          agy_answers()[-2:] == [("Helpers are on it.", planned), ("The release plan is ready.", planned)], agy_answers()[-3:])
    agy_step("USER_INPUT", "<USER_REQUEST>Now the README.</USER_REQUEST>")
    agy("PreInvocation")
    readme = last_id()
    check("each of the user's messages to agy starts its own thread", q("SELECT thread FROM messages WHERE id=?", readme)[0]["thread"] == readme)

    # 26. agy's watch ends with a hint only, whether agy is busy or idle, and leaves the mail unread, so a dropped
    # result loses nothing.
    set_sql("UPDATE agents SET state='busy' WHERE name='agy'")
    run("codex", "ask", "--to", "agy", "--no-wait", "--subject", "contract", "Is the watch hint enough?")
    qid = last_id()
    flags = lambda: dict(q("SELECT notified, read, replied FROM deliveries WHERE message=? AND recipient='agy'", qid)[0])  # noqa: E731
    so, _ = watch.communicate(timeout=10)
    check("the watch ends with only a hint, even while agy is busy, and marks nothing",
          "New mail: run aichord inbox" in so and "Is the watch hint enough?" not in so
          and flags() == {"notified": None, "read": None, "replied": None}, so + str(flags()))
    again = run("agy", "watch")
    check("a new watch gives the same hint while the mail is unread", "New mail" in again.stdout and flags()["notified"] is None, again.stdout)
    out = run("agy", "inbox")
    check("the inbox shows the new question in full", "Is the watch hint enough?" in out.stdout and f"aichord reply {qid}" in out.stdout, out.stdout)
    watch = run("agy", "watch", background=True)
    time.sleep(2.5)
    check("after the inbox, the watch waits for new mail instead of waking for the open question", watch.poll() is None)

    # 27. Read is not answered: the inbox lists a read question until it is answered.
    out = run("agy", "inbox")
    check("a read but unanswered question stays listed, without its full text", f"#{qid} from codex: contract" in out.stdout
          and "Is the watch hint enough?" not in out.stdout and f"aichord reply {qid}" in out.stdout, out.stdout)
    agy("Stop")
    run("agy", "send", "--wake", "--to", "codex", "Progress: still checking.")
    out = run("agy", "inbox")
    check("a stop or a progress note doesn't count as an answer", f"#{qid} from codex" in out.stdout, out.stdout)
    run("agy", "reply", str(qid), "Yes, now that the inbox lists open questions.")
    out = run("agy", "inbox")
    check("an explicit reply takes it off the list", f"#{qid} from codex" not in out.stdout, out.stdout)

    # 28. agy's Stop reminds it once to start its watch when none runs, including just after a watch ended on its
    # own (its lock is free even though it ran a moment ago), and again only after a new watch has run.
    run("codex", "send", "--wake", "--to", "agy", "This ends the watch.")
    so, _ = watch.communicate(timeout=10)
    run("agy", "inbox")
    first_stop, second_stop = agy("Stop"), agy("Stop")
    check("Stop reminds agy once when its watch has just ended", "New mail" in so
          and "aichord watch isn't running" in first_stop.stdout
          and json.loads(first_stop.stdout).get("decision") == "continue" and second_stop.stdout.strip() == "{}",
          so + first_stop.stdout + second_stop.stdout)
    watch = run("agy", "watch", background=True)
    time.sleep(1.5)
    held = agy("Stop")
    watch.kill()
    watch.wait()
    check("no reminder while a watch runs, and a reminder again once it has stopped",
          held.stdout.strip() == "{}" and "aichord watch isn't running" in agy("Stop").stdout, held.stdout)

    # 29. A send says what was seen of each recipient. A wait stops once only recipients whose tool has exited are
    # left, and their questions stay open. Broadcasts include them. A stand-in tmux reports
    # codex's pane back at a shell.
    fakebin = Path(tmp) / "bin"
    fakebin.mkdir()
    for name, script in (("tmux", 'cat "$FAKE_PANES"\n'), ("codex", 'echo "$@" >> "$FAKE_CODEX_LOG"\n')):
        (fakebin / name).write_text("#!/bin/sh\n" + script)
        (fakebin / name).chmod(0o755)
    panes = Path(tmp) / "panes.txt"
    panes.write_text(f"%1\t0\tclaude\t1\t-\t{ROOT}\n%2\t0\tzsh\t2\t-\t{ROOT}\n%3\t0\tagy\t3\t-\t{ROOT}\n")
    set_sql("UPDATE agents SET pane = CASE name WHEN 'claude' THEN '%1' WHEN 'codex' THEN '%2' ELSE '%3' END")
    fake = {"TMUX": "fake", "PATH": f"{fakebin}:{os.environ['PATH']}", "FAKE_PANES": str(panes),
            "FAKE_CODEX_LOG": str(Path(tmp) / "codex.log")}
    p = run("agy", "ask", "--to", "claude,codex", "--wait", "30", "Is either of you still there?", env_extra=fake, background=True)
    time.sleep(2)
    aid = last_id()
    run("claude", "reply", str(aid), "claude is here.", env_extra=fake)
    so, se = p.communicate(timeout=40)
    check("the receipt says which recipient's tool has exited", "codex: its tool has exited" in so and "\n  claude: " in so, so + se)
    check("waiting stops once only exited recipients are left, without saying all replies are in",
          "claude is here." in so and "Stopped waiting for codex" in so and "All replies are in" not in so, so + se)
    check("the exited recipient's question stays open",
          q("SELECT replied FROM deliveries WHERE message=? AND recipient='codex'", aid)[0]["replied"] is None)
    out = run("codex", "inbox")
    check("after a restart the question is in the inbox for an ordinary reply",
          "Is either of you still there?" in out.stdout and f"aichord reply {aid}" in out.stdout, out.stdout[-600:])
    run("agy", "send", "--wake", "Broadcast to everyone.", env_extra=fake)
    check("a broadcast also goes to an agent whose tool has exited",
          sorted(r["recipient"] for r in q("SELECT recipient FROM deliveries WHERE message=?", last_id())) == ["claude", "codex"])
    panes.unlink()
    set_sql("UPDATE agents SET state='idle' WHERE name='claude'")
    run("agy", "status", env_extra=fake)
    check("when tmux can't be asked, nobody is marked as exited", q("SELECT state FROM agents WHERE name='claude'")[0]["state"] == "idle")

    # 30. The sending command wakes an idle Codex session with codex queue, once, and the queued prompt carries the
    # mail. Nothing is typed.
    set_sql("UPDATE agents SET state='idle', updated=0, session_id='s-codex-2' WHERE name='codex'")
    set_sql("UPDATE deliveries SET notified=1, read=1 WHERE recipient='codex' AND notified IS NULL")
    set_sql("DELETE FROM meta WHERE key IN ('wake_error:codex', 'last_wake:codex')")  # the stand-in's failures so far
    run("claude", "send", "--wake", "--to", "codex", "Wake up through codex queue.", env_extra=fake)
    codex_log = Path(fake["FAKE_CODEX_LOG"])
    logged = codex_log.read_text() if codex_log.exists() else ""
    check("the sender wakes idle Codex once with codex queue, carrying the mail in aichord's marked prompt",
          logged.count("queue --thread s-codex-2 --message [aichord] New messages for you (wake-up ") == 1
          and "Wake up through codex queue." in logged, logged)

    # 31. Updating the tools: all at once and bounded. A failed or stuck update doesn't stop the start (a stuck one is
    # stopped with everything it started); a tool that no longer runs does.
    ubin, state = Path(tmp) / "ubin", Path(tmp) / "ustate"
    ubin.mkdir()
    state.mkdir()
    fakes = {
        "claude": 'if [ "$1" = update ]; then [ -n "$BREAK" ] && touch "$S/claude.broken"; echo 1.1.0 > "$S/claude"; echo Updated; '
                  'if [ -n "$BUSY_DB" ]; then sqlite3 "$BUSY_DB" "UPDATE agents SET state=\'busy\' WHERE name=\'codex\'"; fi; '
                  'else [ -e "$S/claude.broken" ] && exit 1; echo "$(cat "$S/claude") (Claude Code)"; fi',
        "codex": 'if [ "$1" = update ]; then echo "Checking..."; echo "network down" >&2; exit 1; else echo "codex-cli 0.1.0"; fi',
        "agy": 'if [ "$1" = update ]; then echo $$ > "$S/agy.pid"; sleep 30 & echo $! > "$S/agy.child"; wait; else echo 2.0.0; fi',
    }
    for name, body in fakes.items():
        (ubin / name).write_text(f"#!/bin/sh\n{body}\n")
        (ubin / name).chmod(0o755)
    (state / "claude").write_text("1.0.0\n")
    probe_up = ("import importlib.util, sys, time\n"
                "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                "t.UPDATE_SECONDS = 2\n"
                "began = time.time(); changed = t.update_tools(['claude', 'codex', 'agy'])\n"
                "print('RESULT', repr(changed), round(time.time() - began, 1))\n")
    uenv = {**CLEAN_ENV, "PATH": f"{ubin}:{os.environ['PATH']}", "S": str(state)}
    out = subprocess.run([PY, "-I", "-c", probe_up, str(SCRIPT)], capture_output=True, text=True, env=uenv)
    result = out.stdout.split("RESULT ")[-1].split()
    check("an update that changed a version is reported, and only that one counts as a change",
          "Claude Code 1.1.0: updated from 1.0.0" in out.stdout and result[:4] == ["'Claude", "Code", "1.0.0", "to"], out.stdout + out.stderr)
    check("a failed update says why, and the tool starts at the version it has",
          "Codex 0.1.0: update failed (network down); starting this version" in out.stdout, out.stdout)
    def updater_gone():  # the stuck Antigravity updater and the process it started
        names = ("agy.pid", "agy.child")
        pids = [(state / f).read_text().strip() for f in names if (state / f).exists()]
        for f in names:
            (state / f).unlink(missing_ok=True)
        return len(pids) == 2 and all(subprocess.run(["kill", "-0", pid], capture_output=True).returncode != 0 for pid in pids)

    check("a stuck update is stopped at the time limit, with what it started, and the start goes on",
          "Antigravity 2.0.0: update failed (no result after 2 s" in out.stdout and float(result[-1]) < 6
          and updater_gone(), out.stdout + out.stderr)
    out = subprocess.run([PY, "-I", "-c", probe_up, str(SCRIPT)], capture_output=True, text=True, env={**uenv, "BREAK": "1"})
    check("a tool that doesn't run after its update stops the start, after every other updater has ended",
          out.returncode != 0 and "Claude Code doesn't run after its update" in out.stderr and "RESULT" not in out.stdout
          and updater_gone(), out.stdout + out.stderr)
    (state / "claude.broken").unlink()
    p = subprocess.Popen([PY, "-I", "-c", probe_up.replace("t.UPDATE_SECONDS = 2", "t.UPDATE_SECONDS = 60"), str(SCRIPT)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=uenv)
    give_up = time.time() + 10
    while time.time() < give_up and not (state / "agy.child").exists():
        time.sleep(0.1)
    p.send_signal(2)  # Ctrl-C
    p.communicate(timeout=20)
    check("Ctrl-C during the updates leaves no updater running", updater_gone())

    # 32. Resuming: each tool continues its recorded conversation, and the first prompt says so. After a version
    # change the first agent's prompt asks for a live check of every wake-up path.
    probe_resume = ("import importlib.util, json, sys\n"
                    f"spec = importlib.util.spec_from_file_location('aichord', {str(SCRIPT)!r})\n"
                    "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                    "t.AGY_HOOKS_FILE = t.Path(sys.argv[1]); root = t.Path(sys.argv[2])\n"
                    "print(json.dumps([t.launch_command('claude', 'claude', root, [], 'sid-1', 'P'),\n"
                    "                  t.launch_command('codex', 'codex', root, [], 'sid-2', 'P'),\n"
                    "                  t.launch_command('agy', 'agy', root, [], 'sid-3', 'P'),\n"
                    "                  t.first_prompt(True), t.first_prompt(False, 'Codex 0.1 to 0.2')]))\n")
    out = subprocess.run([PY, "-I", "-c", probe_resume, str(hooks_file), str(ROOT)], capture_output=True, text=True, env=env_card)
    claude_cmd, codex_cmd, agy_cmd, resumed, live = json.loads(out.stdout) if out.returncode == 0 else [[], [], [], "", ""]
    check("each tool is started on its recorded conversation",
          claude_cmd[-3:] == ["--resume", "sid-1", "P"] and codex_cmd[:2] == ["codex", "resume"]
          and codex_cmd[-2:] == ["sid-2", "P"] and "hooks.Stop" in " ".join(codex_cmd)
          and agy_cmd[-4:] == ["--conversation", "sid-3", "-i", "P"], out.stdout + out.stderr)
    check("the first prompt after a restart says the conversation resumed, and a version change asks for a live check",
          resumed.startswith("[aichord] Your tool was restarted and this conversation resumed.")
          and "Codex 0.1 to 0.2" in live and "live check after an update" in live, f"{resumed} | {live}")

    # 33. A resumed launch keeps the conversation and records the tool version it runs; a late hook from the tool it
    # replaced changes nothing.
    lbin = Path(tmp) / "lbin"
    lbin.mkdir()
    (lbin / "codex").write_text('#!/bin/sh\necho "ARGS $* LAUNCHED $AICHORD_LAUNCHED"\n')
    (lbin / "codex").chmod(0o755)
    out = run(None, "launch", "codex", "--tool", "codex", "--resume", "s-codex-9",
              env_extra={"PATH": f"{lbin}:{os.environ['PATH']}"})
    row = dict(q("SELECT session_id, state, launched FROM agents WHERE name='codex'")[0])
    tool_path = q("SELECT value FROM meta WHERE key='tool_path:codex'")
    check("a resumed launch keeps the conversation and starts the tool on it",
          row["session_id"] == "s-codex-9" and row["state"] == "starting" and "resume" in out.stdout
          and "s-codex-9 [aichord] Your tool was restarted" in out.stdout, out.stdout + out.stderr + str(row))
    check("the launch records the exact tool it started and its launch time for the tool's hooks",
          tool_path and tool_path[0]["value"] == os.path.realpath(lbin / "codex")
          and f"LAUNCHED {row['launched']!r}" in out.stdout, out.stdout + str(tool_path))
    hook_env = lambda launched: {"AICHORD_LAUNCHED": launched}  # noqa: E731
    run("codex", "hook", "codex", "SessionEnd", stdin=json.dumps({"session_id": "s-codex-9"}), env_extra=hook_env("1.5"))
    late = q("SELECT state FROM agents WHERE name='codex'")[0]["state"]
    run("codex", "hook", "codex", "SessionEnd", stdin=json.dumps({"session_id": "s-codex-9"}), env_extra=hook_env(repr(row["launched"])))
    check("a hook from the replaced tool changes nothing; the current tool's hook still counts",
          late == "starting" and q("SELECT state FROM agents WHERE name='codex'")[0]["state"] == "gone", late)
    set_sql("DELETE FROM agents WHERE name='codex'")  # as after a database reset while codex runs
    run("codex", "hook", "codex", "SessionStart", stdin=json.dumps({"session_id": "s-codex-9"}), env_extra=hook_env("7.25"))
    run("codex", "hook", "codex", "PostToolUse", stdin=json.dumps({"session_id": "s-codex-9", "tool_name": "Bash"}),
        env_extra=hook_env("7.25"))
    row = dict(q("SELECT launched, state, detail FROM agents WHERE name='codex'")[0])
    check("an agent registered again after a database reset keeps its own launch time, so its hooks still count",
          row == {"launched": 7.25, "state": "busy", "detail": "last used Bash"}, row)

    # 34. codex queue runs the Codex version the session was started with.
    (lbin / "codex-old").write_text(f'#!/bin/sh\necho "old $*" >> {Path(tmp) / "old.log"}\n')
    (lbin / "codex-old").chmod(0o755)
    probe_queue = ("import importlib.util, sys\n"
                   "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                   "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                   "conn = t.db(); t.meta_set(conn, 'tool_path:codex', sys.argv[2])\n"
                   "print(repr(t.codex_queue(conn, {'name': 'codex', 'session_id': 's-codex-9'}, 'hello')))\n")
    out = subprocess.run([PY, "-I", "-c", probe_queue, str(SCRIPT), str(lbin / "codex-old")], capture_output=True, text=True, env=env_card)
    old_log = (Path(tmp) / "old.log").read_text() if (Path(tmp) / "old.log").exists() else ""
    check("codex queue uses the session's own Codex version", old_log.startswith("old queue --thread s-codex-9 --message hello")
          and out.stdout.strip() == "''", old_log + out.stdout + out.stderr)

    # 35. A restart closes this project's agent and log panes only: never the pane it runs in, another project's log,
    # or a recorded pane that is now in another folder (a new tmux server numbers panes again).
    tbin = Path(tmp) / "tbin"
    tbin.mkdir()
    (tbin / "tmux").write_text('#!/bin/sh\ncase "$*" in\n  *list-panes*) cat "$FAKE_PANES";;\n'
                               '  *) echo "$@" >> "$FAKE_TMUX_LOG";;\nesac\n')
    (tbin / "tmux").chmod(0o755)
    panes = Path(tmp) / "tpanes.txt"
    rows = [("%1", "claude", "claude (Claude Code)", ROOT), ("%2", "zsh", "notes", ROOT), ("%3", "codex", "codex (Codex)", ROOT),
            ("%4", "python3.12", "aichord log", ROOT), ("%5", "python3.12", "aichord log", "/elsewhere"),
            ("%6", "zsh", "zsh", ROOT), ("%7", "agy", "agy (Antigravity)", "/elsewhere")]
    panes.write_text("".join(f"{p}\t0\t{cmd}\t{999990 + i}\t{title}\t{path}\n" for i, (p, cmd, title, path) in enumerate(rows)))
    tlog = Path(tmp) / "tmux.log"
    set_sql("UPDATE agents SET pane = CASE name WHEN 'claude' THEN '%1' WHEN 'codex' THEN '%3' ELSE '%7' END, state='idle'")
    tenv = {**CLEAN_ENV, "AICHORD_ROOT": str(ROOT), "PATH": f"{tbin}:{os.environ['PATH']}",
            "FAKE_PANES": str(panes), "FAKE_TMUX_LOG": str(tlog)}
    probe_close = ("import importlib.util, sys\n"
                   "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                   "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                   "conn = t.db(); c = t.team_panes(conn, t.Path(sys.argv[2]), sys.argv[3])\n"
                   "t.hand_over(conn, [n for n in c.values() if n]); t.close_panes(list(c))\n")
    closed = {}
    launched_before = q("SELECT launched FROM agents WHERE name='codex'")[0]["launched"]
    for here in ("%6", "%1"):  # from a new tab, and from the tab of the agent whose tool the user quit
        tlog.unlink(missing_ok=True)
        subprocess.run([PY, "-I", "-c", probe_close, str(SCRIPT), str(ROOT), here], capture_output=True, text=True, env=tenv)
        closed[here] = sorted(tlog.read_text().split("\n")[:-1]) if tlog.exists() else []
    run("codex", "hook", "codex", "UserPromptSubmit", stdin=json.dumps({"session_id": "s-codex-9", "prompt": "[aichord] x"}),
        env_extra={"AICHORD_LAUNCHED": repr(launched_before)})
    check("once its pane is closed, the old tool's hooks write nothing", q("SELECT state FROM agents WHERE name='codex'")[0]["state"] == "gone")
    check("a restart closes this project's agent and log panes and nothing else",
          closed["%6"] == ["kill-pane -t %1", "kill-pane -t %3", "kill-pane -t %4"], closed["%6"])
    check("the pane a restart runs in is never closed", closed["%1"] == ["kill-pane -t %3", "kill-pane -t %4"], closed["%1"])
    marked = {r["name"]: (r["state"], r["detail"]) for r in q("SELECT name, state, detail FROM agents")}
    check("agents being restarted are marked, so their old hooks stop and nothing wakes them meanwhile",
          marked["codex"] == ("gone", "being restarted") and marked["claude"] == ("gone", "being restarted")
          and marked["agy"][0] == "idle", marked)

    # 36. Before anything is closed: up refuses while the team runs, restart refuses inside an agent, and a restart
    # that would stop an agent's work asks first (without a terminal to ask in, it doesn't restart).
    tenv_run = {"PATH": tenv["PATH"], "FAKE_PANES": str(panes), "FAKE_TMUX_LOG": str(tlog),
                "TMUX": "fake", "TMUX_PANE": "%6"}
    tlog.unlink(missing_ok=True)
    set_sql("UPDATE agents SET state='idle'")  # claude (%1) and codex (%3) run here; agy's recorded %7 is now elsewhere
    out = run(None, "up", "--no-update", env_extra=tenv_run, check=False)
    check("up refuses while the team runs, and points to restart",
          out.returncode != 0 and "already running" in out.stderr and "claude" in out.stderr and "codex" in out.stderr
          and "aichord restart" in out.stderr, out.stderr)
    check("a recorded pane that is now another project's doesn't count as the agent running",
          q("SELECT state FROM agents WHERE name='agy'")[0]["state"] == "gone" and "agy" not in out.stderr, out.stderr)
    out = run("claude", "restart", "--no-update", env_extra=tenv_run, check=False)
    check("restart refuses inside an agent", out.returncode != 0 and "run it from a shell" in out.stderr, out.stderr)
    set_sql("UPDATE agents SET state='busy' WHERE name='codex'")
    out = run(None, "restart", "--no-update", env_extra=tenv_run, check=False)
    check("a restart that would stop an agent's work asks first, and closes nothing without a yes",
          out.returncode != 0 and "codex is working" not in out.stdout and "Nothing was restarted" in out.stderr
          and not tlog.exists(), out.stdout + out.stderr)
    out = run(None, "restart", "--no-update", env_extra={**tenv_run, "TMUX_PANE": "%1"}, check=False)
    check("run from the tab where the user quit an agent's tool, that agent counts as exited at once",
          q("SELECT state FROM agents WHERE name='claude'")[0]["state"] == "gone" and "Nothing was restarted" in out.stderr
          and not tlog.exists(), out.stdout + out.stderr)

    # 38. A hook of a replaced tool can't take the new tool's mail: the check that the agent is idle in this launch
    # and the taking happen in one step.
    set_sql("UPDATE agents SET state='idle', launched=5.0 WHERE name='claude'")
    run("codex", "send", "--wake", "--to", "claude", "Mail for the new claude.")
    probe_claim = ("import importlib.util, os, sys\n"
                   "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                   "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                   "os.environ.update(AICHORD_AGENT='claude', AICHORD_LAUNCHED=sys.argv[2]); conn = t.db()\n"
                   "woken = len(t.claim_unseen(conn, 'claude', actions=t.ACTIONS_THAT_WAKE, peer_turn='woken'))\n"
                   "print(woken, len(t.claim_unseen(conn, 'claude', actions=t.ACTIONS_THAT_WAKE)))\n")
    claim = lambda launched: subprocess.run([PY, "-I", "-c", probe_claim, str(SCRIPT), launched], capture_output=True,  # noqa: E731
                                            text=True, env=env_card).stdout.split()
    old_claim = claim("4.0")
    unseen = q("SELECT COUNT(*) AS n FROM deliveries WHERE recipient='claude' AND notified IS NULL")[0]["n"]
    new_claim = claim("5.0")
    check("a replaced tool's hooks take no mail; the current tool's wake-up takes it and starts a turn",
          old_claim == ["0", "0"] and unseen >= 1 and new_claim[:1] != ["0"] and len(new_claim) == 2
          and q("SELECT state FROM agents WHERE name='claude'")[0]["state"] == "busy", f"{old_claim} {unseen} {new_claim}")

    # 39. A whole restart, with stand-in tmux and tools: the recorded team comes back, each agent in its own tool and
    # conversation (codex was recorded in Antigravity; agy isn't recorded, so it isn't started).
    (tbin / "tmux").write_text('#!/bin/sh\ncase "$*" in\n  *list-panes*) cat "$FAKE_PANES";;\n'
                               '  *new-window*) echo "$@" >> "$FAKE_TMUX_LOG"; echo "@9 %9";;\n'
                               '  *) echo "$@" >> "$FAKE_TMUX_LOG";;\nesac\n')
    set_sql("DELETE FROM agents WHERE name='agy'")
    set_sql("UPDATE agents SET state='idle', tool='agy', pane='%3' WHERE name='codex'")
    set_sql("UPDATE agents SET state='idle', pane='%1' WHERE name='claude'")
    tlog.unlink(missing_ok=True)
    db_path = str(ROOT / ".aichord" / "aichord.db")
    settings(update_tools=True)  # the tools are updated first only when the user's settings (or --update) ask for it
    out = run(None, "restart", env_extra={**tenv_run, "PATH": f"{tbin}:{ubin}:{os.environ['PATH']}", "S": str(state),
                                         "BUSY_DB": db_path}, check=False)
    settings()
    check("an agent that starts working during the updates is asked about too, before anything closes",
          out.returncode != 0 and "Updating" in out.stdout and "Nothing was restarted" in out.stderr and not tlog.exists(),
          out.stdout + out.stderr)
    set_sql("UPDATE agents SET state='idle' WHERE name='codex'")
    out = run(None, "restart", env_extra={**tenv_run, "PATH": f"{tbin}:{ubin}:{os.environ['PATH']}", "S": str(state)},
              check=False)  # stand-in tools come first, in case an update ran anyway
    sent = tlog.read_text() if tlog.exists() else ""
    check("restart brings back the recorded team, each in its own tool and conversation, after closing the old panes, "
          "and updates no tool unless asked",
          out.returncode == 0 and "Updating" not in out.stdout and "launch claude --tool claude --resume s-claude" in sent
          and "launch codex --tool agy --resume s-codex-9" in sent and "launch agy" not in sent
          and sent.index("kill-pane -t %3") < sent.index("launch codex"), out.stdout + out.stderr + sent)
    out = run(None, "restart", "--no-update", env_extra={**tenv_run, "AICHORD_ROOT": str(Path(tmp) / "empty")}, check=False)
    check("restart in a project with no recorded team says so and creates nothing",
          out.returncode != 0 and "no aichord session" in out.stderr and not (Path(tmp) / "empty" / ".aichord").exists(), out.stderr)

    # 40. A tool that ignores its hang-up is killed; one that can't be stopped stops the restart before anything starts.
    hung = subprocess.run(["sh", "-c", "trap '' HUP; sleep 60 >/dev/null 2>&1 & echo $!"], capture_output=True, text=True).stdout.strip()
    panes.write_text(f"%3\t0\tcodex\t{hung}\tcodex (Codex)\t{ROOT}\n")
    probe_kill = ("import importlib.util, sys, time\n"
                  "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                  "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                  "t.CLOSE_SECONDS = 1\n"
                  "if sys.argv[3] == 'stuck': t.still_running = lambda pids, seconds: pids\n"
                  "conn = t.db(); c = t.team_panes(conn, t.Path(sys.argv[2]), '%6')\n"
                  "t.hand_over(conn, [n for n in c.values() if n]); t.close_panes(list(c)); print('closed')\n")
    out = subprocess.run([PY, "-I", "-c", probe_kill, str(SCRIPT), str(ROOT), "normal"], capture_output=True, text=True, env=tenv)
    check("a tool still running after its hang-up is killed", out.stdout.strip() == "closed"
          and subprocess.run(["kill", "-0", hung], capture_output=True).returncode != 0, out.stdout + out.stderr)
    out = subprocess.run([PY, "-I", "-c", probe_kill, str(SCRIPT), str(ROOT), "stuck"], capture_output=True, text=True, env=tenv)
    check("an old process that can't be stopped stops the restart", out.returncode != 0 and "closed" not in out.stdout
          and "nothing was started" in out.stderr, out.stdout + out.stderr)

    # 41. The handover waits for a hook that is already running. If that hook started work, the user is asked about
    # it, and without a yes nothing is handed over.
    set_sql("UPDATE agents SET state='idle', launched=6.0 WHERE name='codex'")
    holder = subprocess.Popen([PY, "-I", "-c", (
        "import fcntl, sqlite3, sys, time\n"
        "f = open(sys.argv[1], 'a+'); fcntl.flock(f, fcntl.LOCK_SH); print('held', flush=True); time.sleep(1.0)\n"
        "c = sqlite3.connect(sys.argv[2]); c.execute(\"UPDATE agents SET state='busy' WHERE name='codex'\"); c.commit()\n"
        "time.sleep(0.3)\n"), str(ROOT / ".aichord" / "runtime" / "agent-codex.lock"), str(ROOT / ".aichord" / "aichord.db")],
        stdout=subprocess.PIPE, text=True)
    holder.stdout.readline()
    probe_hand = ("import importlib.util, sys, time\n"
                  "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                  "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                  "began = time.time()\n"
                  "try:\n    t.hand_over(t.db(), ['codex'])\nexcept SystemExit as e:\n    print('EXIT', e, round(time.time() - began, 1))\n")
    out = subprocess.run([PY, "-I", "-c", probe_hand, str(SCRIPT)], capture_output=True, text=True, env=env_card)
    holder.wait(timeout=10)
    row = dict(q("SELECT state, launched FROM agents WHERE name='codex'")[0])
    check("the handover waits for a running hook, and work that hook started is asked about first",
          out.stdout.startswith("EXIT Nothing was restarted.") and float(out.stdout.split()[-1]) >= 0.9
          and row == {"state": "busy", "launched": 6.0}, out.stdout + out.stderr + str(row))

    # 42. A message that starts with a path is the user's; a slash command isn't (such messages were once lost). The
    # lead is told the number of the user's message, so it cites the right one. What a tool command prints isn't an
    # answer.
    set_sql("UPDATE agents SET state='idle', turn_from='', session_id='s-claude', launched=0 WHERE name='claude'")
    set_sql("UPDATE deliveries SET notified=1, read=1 WHERE recipient='claude' AND notified IS NULL")
    user_count = lambda: len(q("SELECT * FROM messages WHERE kind='user'"))  # noqa: E731
    before = user_count()
    ctx = context(hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude",
                                                               "prompt": "/Users/someone/x.pdf parse that and present it to the others"}))
    newest = q("SELECT id, body FROM messages WHERE kind='user' ORDER BY id DESC LIMIT 1")[0]
    check("a message that starts with a path is recorded, and the lead is told its number",
          user_count() == before + 1 and newest["body"].startswith("/Users/someone/x.pdf")
          and f"This is the user's message #{newest['id']}." in ctx, ctx[:300])
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False, "last_assistant_message": "Parsed."})
    hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": "/compact keep the plan"})
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False, "last_assistant_message": "Compacted."})
    check("a slash command is not recorded, and what it prints isn't recorded as an answer",
          user_count() == before + 1 and not q("SELECT 1 FROM messages WHERE body='Compacted.'"))

    # 43. The lead's later answers are recorded: after replies to its questions wake it (the main flow of a lead), and
    # after its own background work, even when a peer's question came in between. A turn a peer's question starts is
    # not an answer to the user, nor is a background result started for a peer.
    sock = FakeSocket(SOCKDIR / "claude2.sock")
    claude_env = {"CLAUDE_CODE_MESSAGING_SOCKET": sock.path, "CLAUDE_CODE_MESSAGING_TOKEN": "tok-2"}

    def claude_hook(event, payload):
        return hook("claude", "claude", event, {"session_id": "s-claude", **payload}, env_extra=claude_env)

    def claude_stop(text, **extra):
        return claude_hook("Stop", {"stop_hook_active": False, "last_assistant_message": text, **extra})

    def woken_turn():  # the wake-up the sender posted to claude's socket starts its turn
        return context(claude_hook("UserPromptSubmit", {"prompt": sock.texts()[-1]}))

    def background(call):  # claude starts a background command; Claude Code's tool hook names the call
        claude_hook("PostToolUse", {"tool_name": "Bash", "tool_use_id": call,
                                  "tool_input": {"command": "sleep 60", "run_in_background": True}})

    def finished_task(call):  # Claude Code reports a background task's finish as a prompt
        claude_hook("UserPromptSubmit", {"prompt": (
            f"<task-notification>\n<task-id>t-{call}</task-id>\n<tool-use-id>{call}</tool-use-id>\n"
            "<status>completed</status>\n<summary>Background command completed</summary>\n</task-notification>")})

    def thread_answers(tid):
        return [r["body"] for r in q("SELECT body FROM messages WHERE kind='answer' AND thread=? ORDER BY id", tid)]

    claude_hook("UserPromptSubmit", {"prompt": "Is the fee bound safe?"})
    request = last_id()
    background("toolu_backtest")
    asked = int(run("claude", "ask", "--to", "codex", "--no-wait", "Check the fee bound.").stdout.split("#")[1].split()[0])
    claude_stop("I asked codex; I'll answer when it replies.")
    frames = len(sock.frames)
    run("codex", "reply", str(asked), "ANSWER: It holds.")
    woke = len(sock.frames) == frames + 1 and "ANSWER: It holds." in sock.texts()[-1]
    woken_turn()
    claude_stop("The fee bound holds; codex confirmed it.")
    peer = int(run("codex", "ask", "--to", "claude", "--no-wait", "Which file has the bound?").stdout.split("#")[1].split()[0])
    woken_turn()
    background("toolu_peer")  # a probe for codex's question
    run("claude", "reply", str(peer), "fees.py")
    claude_stop("Replied to codex.")
    finished_task("toolu_peer")
    claude_stop("The probe for codex passes.")
    finished_task("toolu_backtest")
    claude_stop("The backtest finished: the fee bound holds at 3 bp.")
    answers = thread_answers(request)
    check("replies wake the lead through its socket, and the answer they lead to is recorded after the user's message",
          woke and answers[:2] == ["I asked codex; I'll answer when it replies.", "The fee bound holds; codex confirmed it."], answers)
    check("a peer's question starts a turn that isn't an answer to the user, and neither is a task started for it",
          not q("SELECT 1 FROM messages WHERE kind='answer' AND body IN ('Replied to codex.', 'The probe for codex passes.')"))
    check("a background task started for the user goes on with the user's work after a peer's turn in between",
          answers[-1:] == ["The backtest finished: the fee bound holds at 3 bp."], answers)

    # 44. The user types while the lead works on something else: the message is recorded at once, in a thread of
    # its own, and the answers that follow are filed after it (a timeline).
    claude_hook("UserPromptSubmit", {"prompt": "Compare the two oracles."})
    oracles = last_id()
    asked = int(run("claude", "ask", "--to", "codex", "--no-wait", "Which oracle is fresher?").stdout.split("#")[1].split()[0])
    claude_stop("I asked codex about the oracles.")
    claude_hook("UserPromptSubmit", {"prompt": "Also, what time is it in Tokyo?"})
    tokyo = last_id()
    claude_stop("It is 19:00 in Tokyo.")
    run("codex", "reply", str(asked), "ANSWER: Pyth is fresher.")
    woken_turn()
    claude_stop("Pyth is fresher; codex confirmed it.")
    check("answers are filed by time, after the user's latest message to the lead",
          thread_answers(oracles) == ["I asked codex about the oracles."]
          and thread_answers(tokyo) == ["It is 19:00 in Tokyo.", "Pyth is fresher; codex confirmed it."],
          f"{thread_answers(oracles)} {thread_answers(tokyo)}")
    claude_hook("UserPromptSubmit", {"prompt": "Analyse the fee curve."})
    analysis = last_id()
    asked = int(run("claude", "ask", "--to", "codex", "--no-wait", "Check the bound.").stdout.split("#")[1].split()[0])
    claude_stop("Waiting for the bound check.")
    claude_hook("UserPromptSubmit", {"prompt": "/compact Keep the fee analysis."})
    claude_stop("Compacted the conversation.")
    run("codex", "reply", str(asked), "ANSWER: The bound holds.")
    woken_turn()
    claude_stop("The fee curve is safe: the bound holds.")
    check("a tool command between a question and its replies doesn't lose the answer, and isn't one itself",
          thread_answers(analysis) == ["Waiting for the bound check.", "The fee curve is safe: the bound holds."]
          and not q("SELECT 1 FROM messages WHERE body='Compacted the conversation.'"), thread_answers(analysis))
    sock.close()

    # 45. One wake-up at a time, and an honest one. Codex can spend minutes compacting a long conversation before its
    # prompt hook runs (once seven wake-ups, then six empty turns). Until the woken turn's first
    # hook no second wake-up follows; a failed one puts the mail back and is tried again; one whose turn never starts
    # is reported, once, never repeated on time alone.
    qlog = Path(tmp) / "queue.log"
    (lbin / "codex-q").write_text(f'#!/bin/sh\necho "CALL $*" >> {qlog}\nexit "${{FAKE_RC:-0}}"\n')
    (lbin / "codex-q").chmod(0o755)
    set_sql("UPDATE agents SET tool='codex', state='idle', updated=0, session_id='s-codex-9', launched=0 WHERE name='codex'")
    for key, value in (("last_wake", ""), ("wake_error", ""), ("tool_path", str(lbin / "codex-q"))):
        set_sql("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                f"{key}:codex", value)
    set_sql("UPDATE deliveries SET notified=1, read=1, replied=1 WHERE recipient='codex'")
    calls = lambda: qlog.read_text().count("CALL queue --thread s-codex-9") if qlog.exists() else 0  # noqa: E731
    run("claude", "send", "--wake", "--to", "codex", "Wake codex for one turn.")
    run("claude", "send", "--wake", "--to", "codex", "And a second message.")
    out = run("claude", "status")
    check("one wake-up while its turn hasn't started, and status says so",
          calls() == 1 and "woken by aichord; its turn hasn't started yet" in out.stdout, f"{calls()} {out.stdout}")
    ctx = context(hook("codex", "codex", "UserPromptSubmit", {"session_id": "s-codex-9", "prompt": wake_prompt("codex")}))
    check("the woken turn's first hook shows what came after the wake-up", "And a second message." in ctx
          and "Wake codex for one turn." not in ctx, ctx)
    hook("codex", "codex", "Stop", {"session_id": "s-codex-9", "stop_hook_active": False})
    out = run("claude", "send", "--wake", "--to", "codex", "This wake-up fails.", env_extra={"FAKE_RC": "1"})
    failed = q("SELECT id FROM messages WHERE body='This wake-up fails.'")[0]["id"]
    d = dict(q("SELECT notified FROM deliveries WHERE message=? AND recipient='codex'", failed)[0])
    check("a failed wake-up puts the mail back, leaves the agent idle, and says it failed",
          calls() == 2 and d == {"notified": None} and q("SELECT state FROM agents WHERE name='codex'")[0]["state"] == "idle"
          and "failed" in out.stdout, f"{calls()} {d} {out.stdout}")
    tend("3600")
    tend("0")
    check("the failed wake-up is tried again, with the same mail, once its waiting time has passed",
          calls() == 3 and qlog.read_text().rstrip().endswith("This wake-up fails."), qlog.read_text()[-300:])
    set_sql("UPDATE meta SET value=? WHERE key='wake_pending:codex'", repr(time.time() - 2000))  # its turn never started
    tend("0")
    tend("0")
    out = run("claude", "status")
    wake_log = (ROOT / ".aichord" / "runtime" / "wake.log").read_text()
    check("a wake-up whose turn never starts is reported once, never repeated",
          calls() == 3 and wake_log.count("hasn't started a turn") == 1
          and "stalled: typing anything in codex's tab starts its turn" in out.stdout, f"{calls()} {out.stdout}")
    set_sql("UPDATE agents SET state='idle', detail='', updated=? WHERE name='codex'", time.time())
    set_sql("UPDATE meta SET value='' WHERE key='wake_pending:codex'")

    # 46. Helpers: Claude Code's run on Sonnet unless the user's settings or the environment name another model; Codex
    # keeps its own default unless the settings name one; agy's instructions name Antigravity's tiers.
    (lbin / "claude").write_text('#!/bin/sh\necho "HELPERS=$CLAUDE_CODE_SUBAGENT_MODEL"\n')
    (lbin / "claude").chmod(0o755)
    path_env = {"PATH": f"{lbin}:{os.environ['PATH']}"}
    default = run(None, "launch", "claude", "--tool", "claude", env_extra=path_env).stdout
    own = run(None, "launch", "claude", "--tool", "claude", env_extra={**path_env, "CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}).stdout
    settings(helper_models={"claude": "opus", "codex": "a-codex-model"})
    configured = run(None, "launch", "claude", "--tool", "claude", env_extra=path_env).stdout
    out = subprocess.run([PY, "-I", "-c", probe_resume, str(hooks_file), str(ROOT)], capture_output=True, text=True, env=env_card)
    codex_configured = json.loads(out.stdout)[1] if out.returncode == 0 else []
    check("claude's helpers run on Sonnet unless the settings or the environment name another model",
          "HELPERS=sonnet" in default and "HELPERS=haiku" in own and "HELPERS=opus" in configured, default + own + configured)
    check("codex's helpers keep Codex's own default unless the settings name a model",
          not any("default_subagent_model" in a for a in codex_cmd + cmds[3])
          and 'agents.default_subagent_model="a-codex-model"' in codex_configured and codex_configured[-2:] == ["sid-2", "P"],
          f"{codex_cmd} {codex_configured}")
    set_sql("INSERT OR REPLACE INTO agents(name, tool, state) VALUES('agy', 'agy', 'idle')")
    probe_seat = ("import importlib.util, sys\n"
                  "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                  "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                  "conn = t.db(); print(t.seat_card(conn, sys.argv[2]))\n")
    seat_of = lambda n: subprocess.run([PY, "-I", "-c", probe_seat, str(SCRIPT), n], capture_output=True, text=True,  # noqa: E731
                                       env=env_card).stdout
    seat = {n: seat_of(n) for n in ("claude", "codex", "agy")}
    settings()
    plain = {n: seat_of(n) for n in ("claude", "codex")}
    check("each agent's start note tells it to use helpers, naming the default model they run on",
          "run on opus by default" in seat["claude"] and "run on a-codex-model by default" in seat["codex"]
          and "run on sonnet by default" in plain["claude"] and "run on Codex's default model" in plain["codex"]
          and 'invoke_subagent with Model "flash"' in seat["agy"]
          and all("Helpers never run aichord commands" in s for s in seat.values()), {n: s[:600] for n, s in seat.items()})
    check("a launch forgets the last launch's message socket", not q("SELECT value FROM meta WHERE key='socket:claude' AND value != ''"))

    # 47. Every command answers --help with its own line; wait takes --wait like ask; a decision's own label at the
    # start isn't stored twice (no more "D18: D18: ...").
    decisions = len(q("SELECT * FROM decisions"))
    out = run("claude", "decide", "--help")
    check("a command's --help shows its line of the help and does nothing else", out.returncode == 0
          and "aichord decide [--supersedes D3,D5]" in out.stdout and "aichord ask" not in out.stdout
          and len(q("SELECT * FROM decisions")) == decisions, out.stdout + out.stderr)
    out = run("claude", "wait", "--wait", "2", check=False)
    check("wait takes --wait, like ask", out.returncode == 0, out.stderr)
    run("claude", "decide", "D9: The fee tier is dropped.")
    newest = dict(q("SELECT text FROM decisions ORDER BY id DESC LIMIT 1")[0])
    check("a decision's own label at the start isn't stored twice", newest["text"] == "The fee tier is dropped.", newest)

    # 48. A forgotten reply (an agent finished its research and ended its turn without replying). A question read
    # in this launch and not answered makes the stop carry on once, with a reminder. Not while the agent's own
    # background work runs (it hasn't finished), and not for a question read in an earlier launch.
    set_sql("UPDATE agents SET state='idle', launched=? WHERE name='codex'", time.time() - 5)
    old_q = int(run("claude", "ask", "--to", "codex", "--no-wait", "An old question.").stdout.split("#")[1].split()[0])
    set_sql("UPDATE deliveries SET notified=1, read=1 WHERE message=?", old_q)  # read long before this launch
    new_q = int(run("claude", "ask", "--to", "codex", "--no-wait", "Does the bound hold?").stdout.split("#")[1].split()[0])
    hook("codex", "codex", "PostToolUse", {"session_id": "s-codex-9", "tool_name": "Bash"})  # shows it to codex
    first = hook("codex", "codex", "Stop", {"session_id": "s-codex-9", "stop_hook_active": False})
    second = hook("codex", "codex", "Stop", {"session_id": "s-codex-9", "stop_hook_active": True})
    reason = json.loads(first.stdout).get("reason", "") if first.stdout.strip() else ""
    check("a question read and not answered makes the stop carry on once, with a reminder naming it",
          f"#{new_q} from claude" in reason and f"#{old_q}" not in reason and second.stdout.strip() == "",
          first.stdout + second.stdout)
    set_sql("UPDATE agents SET launched=0 WHERE name='claude'")
    peer = int(run("codex", "ask", "--to", "claude", "--no-wait", "And the gauge?").stdout.split("#")[1].split()[0])
    hook("claude", "claude", "PostToolUse", {"session_id": "s-claude", "tool_name": "Bash"})
    busy = hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False,
                                           "background_tasks": [{"id": "b1", "type": "local_agent"}]})
    done = hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False, "background_tasks": []})
    check("no reminder while the agent's own background work runs, and one once it has finished",
          busy.stdout.strip() == "" and f"#{peer} from codex" in done.stdout, busy.stdout + done.stdout)
    run("claude", "reply", str(peer), "The gauge is fine.")

    # 49. Whose work a turn is on.
    # A peer's question shown as the user's turn ends makes the rest of that turn the peer's: its closing text isn't
    # recorded as an answer to the user.
    hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": "Summarize the fees."})
    fees = last_id()
    peer = int(run("codex", "ask", "--to", "claude", "--no-wait", "Which fee file?").stdout.split("#")[1].split()[0])
    shown = hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False,
                                            "last_assistant_message": "The fees are summarized."})
    run("claude", "reply", str(peer), "fees.py")
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": True,
                                    "last_assistant_message": "Replied to codex about the fee file."})
    check("a peer's question shown as the user's turn ends doesn't make the peer's work an answer to the user",
          "Which fee file?" in shown.stdout and thread_answers(fees) == ["The fees are summarized."], thread_answers(fees))
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False})
    # Inside Codex's sandbox a command can't reach another session's socket: codex's ask doesn't try, doesn't sit out
    # its time limit, and its next hook (which Codex runs outside the sandbox) sends the wake-up.
    sock = FakeSocket(SOCKDIR / "claude3.sock")
    hook("claude", "claude", "PostToolUse", {"session_id": "s-claude", "tool_name": "Bash"},
         env_extra={"CLAUDE_CODE_MESSAGING_SOCKET": sock.path, "CLAUDE_CODE_MESSAGING_TOKEN": "tok-3"})
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False})
    began = time.time()
    out = run("codex", "ask", "--to", "claude", "Is the sandbox handled?", env_extra={"CODEX_SANDBOX": "seatbelt"})
    took = time.time() - began
    check("an ask inside Codex's sandbox doesn't try to wake the lead and doesn't wait out its time limit",
          not sock.frames and took < 15 and "can't be woken until this command ends" in out.stdout
          and "Run aichord wait" in out.stdout, f"{took:.1f} s {sock.frames} {out.stdout}")
    hook("codex", "codex", "PostToolUse", {"session_id": "s-codex-9", "tool_name": "Bash"})
    check("the asker's next hook, outside the sandbox, sends the wake-up", len(sock.frames) == 1
          and "Is the sandbox handled?" in sock.texts()[0], sock.frames)
    hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": sock.texts()[0]})  # its turn starts
    run("claude", "reply", str(q("SELECT id FROM messages WHERE body='Is the sandbox handled?'")[0]["id"]), "Yes.")
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False})
    sock.close()
    # An aichord watch agy started before the swap isn't in the new record of watches; when it ends for mail, the
    # turn it starts is still a peer's.
    agy_step("USER_INPUT", "<USER_REQUEST>Check the audit.</USER_REQUEST>")
    agy("PreInvocation")
    audit = last_id()
    run("codex", "send", "--wake", "--to", "agy", "A note that ends a watch started before the swap.")
    model_call("Read codex's note.", lambda: finished("conv/task-before-swap"))
    check("a watch started before the swap still starts a peer's turn when it ends for mail",
          not q("SELECT 1 FROM messages WHERE kind='answer' AND body=?", "Read codex's note.") and
          q("SELECT turn_from FROM agents WHERE name='agy'")[0]["turn_from"] == "aichord", audit)

    # 50. Wake-up reservations, launches and reminders.
    # An accepted wake-up keeps its reservation until its own prompt starts the turn: an older turn's Stop, which
    # marks the agent idle, doesn't let a second wake-up be queued meanwhile.
    for key in ("wake_pending", "wake_error", "last_wake"):
        set_sql("DELETE FROM meta WHERE key=?", f"{key}:codex")
    set_sql("UPDATE agents SET state='idle', detail='' WHERE name='codex'")
    set_sql("UPDATE deliveries SET notified=1, read=1, replied=1 WHERE recipient='codex'")
    before_calls = calls()
    run("claude", "send", "--wake", "--to", "codex", "First wake-up's mail.")
    hook("codex", "codex", "Stop", {"session_id": "s-codex-9", "stop_hook_active": False})  # an older turn ends
    run("claude", "send", "--wake", "--to", "codex", "Mail while the first wake-up waits.")
    check("an older turn's stop doesn't free the reservation of a wake-up whose turn hasn't started",
          calls() == before_calls + 1, f"{calls() - before_calls} calls")
    ctx = context(hook("codex", "codex", "UserPromptSubmit", {"session_id": "s-codex-9", "prompt": wake_prompt("codex")}))
    first = q("SELECT id FROM messages WHERE body=?", "First wake-up's mail.")[0]["id"]
    check("the wake-up's own prompt ends the reservation and marks the mail it carried read",
          not q("SELECT value FROM meta WHERE key='wake_pending:codex' AND value != ''")
          and q("SELECT read FROM deliveries WHERE message=? AND recipient='codex'", first)[0]["read"] is not None
          and "Mail while the first wake-up waits." in ctx, ctx)
    hook("codex", "codex", "Stop", {"session_id": "s-codex-9", "stop_hook_active": False})
    # A wake-up is tied to the launch it took the mail for: after a restart the old row's session gets nothing, and the
    # mail waits for the new launch.
    probe_stale = ("import importlib.util, sys\n"
                   "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                   "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                   "conn = t.db(); old = t.agent_row(conn, 'codex')\n"
                   "conn.execute(\"UPDATE agents SET launched = launched + 1, session_id = 's-codex-new' WHERE name = 'codex'\")\n"
                   "t.wake(conn, old)\n")
    stale = int(run("agy", "ask", "--to", "codex", "--no-wait", "Mail for the restarted codex.").stdout.split("#")[1].split()[0])
    set_sql("UPDATE agents SET state='idle', detail='' WHERE name='codex'")
    set_sql("UPDATE deliveries SET notified=NULL, read=NULL WHERE message=?", stale)
    for key in ("wake_pending", "wake_error"):
        set_sql("DELETE FROM meta WHERE key=?", f"{key}:codex")
    before_calls = calls()
    subprocess.run([PY, "-I", "-c", probe_stale, str(SCRIPT)], capture_output=True, text=True, env=env_card)
    after = dict(q("SELECT state, detail FROM agents WHERE name='codex'")[0])
    check("a wake-up for a launch that was replaced meanwhile sends nothing, takes nothing, and leaves the new launch idle",
          calls() == before_calls and q("SELECT notified FROM deliveries WHERE message=? AND recipient='codex'", stale)[0]["notified"] is None
          and after == {"state": "idle", "detail": ""}, f"{calls() - before_calls} calls {after}")
    # The same short answer to two separate messages of the user's is recorded twice; a repeated stop isn't.
    for prompt in ("Run the first check.", "Run the second check."):
        hook("codex", "codex", "UserPromptSubmit", {"session_id": "s-codex-new", "prompt": prompt})
        for active in (False, True):
            hook("codex", "codex", "Stop", {"session_id": "s-codex-new", "stop_hook_active": active,
                                            "last_assistant_message": "Done."})
    check("the same short answer to two separate messages is recorded twice, a repeated stop once",
          len(q("SELECT 1 FROM messages WHERE kind='answer' AND sender='codex' AND body='Done.'")) == 2)
    # The reminder goes by reading in full in this launch: a question announced before the restart and read in full
    # now gets one; a long question shown shortened, never opened, gets none.
    set_sql("UPDATE agents SET launched=? WHERE name='codex'", time.time() - 5)
    announced = int(run("claude", "ask", "--to", "codex", "--no-wait", "Announced before the restart.").stdout.split("#")[1].split()[0])
    set_sql("UPDATE deliveries SET notified=1, read=NULL WHERE message=?", announced)
    set_sql("UPDATE agents SET state='busy' WHERE name='codex'")
    run("codex", "read", str(announced))
    long_q = int(run("claude", "ask", "--to", "codex", "--no-wait", "A long question. " + "x" * 2000).stdout.split("#")[1].split()[0])
    hook("codex", "codex", "PostToolUse", {"session_id": "s-codex-new", "tool_name": "Bash"})  # shown shortened
    out = hook("codex", "codex", "Stop", {"session_id": "s-codex-new", "stop_hook_active": False})
    reason = json.loads(out.stdout).get("reason", "") if out.stdout.strip() else ""
    check("a question read in full in this launch gets a reminder; one only shown shortened doesn't",
          f"#{announced} from claude" in reason and f"#{long_q}" not in reason, reason)
    # agy's watch records what it ended for: when a Stop shows the reply before the finish reaches the transcript,
    # the turn still goes on with the user's work, and its final answer is recorded.
    set_sql("UPDATE agents SET state='busy' WHERE name='agy'")
    agy_step("USER_INPUT", "<USER_REQUEST>Prove the bound.</USER_REQUEST>")
    agy("PreInvocation")
    bound = last_id()
    asked = int(run("agy", "ask", "--to", "codex", "--no-wait", "Is the bound tight?").stdout.split("#")[1].split()[0])
    model_call("Waiting for codex.", lambda: started("task-late", "aichord watch"))
    watch = run("agy", "watch", background=True)
    time.sleep(1.5)
    run("codex", "reply", str(asked), "ANSWER: Yes.")
    watch.communicate(timeout=10)
    agy("Stop")  # a stop shows the reply before the watch's finish reaches agy's transcript
    model_call("The bound holds, codex confirmed it.", lambda: finished("conv/task-late"))
    check("a watch that ended for a reply keeps the turn on the user's work, even when a stop showed the reply first",
          ("The bound holds, codex confirmed it.", bound) in agy_answers(), agy_answers()[-3:])
    # 51. An accepted wake-up keeps its reservation until its own prompt starts the turn, whatever hooks run
    # meanwhile (in Claude Code: or until the agent's next turn ends without it, 66). aichord wakes a Claude Code
    # session only where Claude Code delivers the
    # message at once: with the session's token, and no setting that holds or refuses messages. Codex keeps a queued
    # wake-up until it runs, as soon as the session is idle. A late prompt of an older wake-up never speaks for a
    # newer one.
    pending = lambda name: q("SELECT value FROM meta WHERE key=? AND value != ''", f"wake_pending:{name}")  # noqa: E731
    sock = FakeSocket(SOCKDIR / "claude4.sock")
    hook("claude", "claude", "PostToolUse", {"session_id": "s-claude", "tool_name": "Bash"},
         env_extra={"CLAUDE_CODE_MESSAGING_SOCKET": sock.path, "CLAUDE_CODE_MESSAGING_TOKEN": "tok-4"})
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False})
    run("codex", "send", "--wake", "--to", "claude", "A wake-up whose turn starts late.")
    later = context(hook("claude", "claude", "PostToolUse", {"session_id": "s-claude", "tool_name": "Bash"}))
    later += context(hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": "The user types meanwhile."}))
    kept = bool(pending("claude")) and "starts late" not in later
    hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": sock.texts()[0]})  # taken in mid-turn
    freed = not pending("claude")
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False})
    check("a Claude Code wake-up keeps its reservation through the turn's later hooks, until its own prompt is taken in",
          len(sock.frames) == 1 and kept and freed, later)
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False})
    run("codex", "send", "--wake", "--to", "claude", "Mail of the first wake-up.")
    # a new launch of the same conversation gives the mail back (as cmd_launch does); the session's next hook shows it
    set_sql("UPDATE deliveries SET notified=NULL WHERE recipient='claude' AND read IS NULL")
    set_sql("UPDATE meta SET value='' WHERE key IN ('wake_pending:claude', 'wake_msgs:claude', 'wake_read:claude')")
    hook("claude", "claude", "PostToolUse", {"session_id": "s-claude", "tool_name": "Bash"})
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False})
    run("codex", "send", "--wake", "--to", "claude", "Mail of the newer wake-up.")
    newer = last_id()
    hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": sock.texts()[1]})  # the older one, late
    late = (bool(pending("claude")), q("SELECT read FROM deliveries WHERE message=? AND recipient='claude'", newer)[0]["read"])
    hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": sock.texts()[2]})
    check("an older wake-up's late prompt neither ends the newer one's reservation nor marks its mail read",
          len(sock.frames) == 3 and late == (True, None) and not pending("claude")
          and q("SELECT read FROM deliveries WHERE message=? AND recipient='claude'", newer)[0]["read"] is not None, str(late))
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False})
    (ROOT / ".claude").mkdir(exist_ok=True)
    (ROOT / ".claude" / "settings.local.json").write_text('{"crossSessionInbound": "hold"}')
    out = run("codex", "send", "--wake", "--to", "claude", "Mail that a hold setting would keep.")
    (ROOT / ".claude" / "settings.local.json").unlink()
    # The settings that count are the recipient's: its hooks record its user settings folder, and a sender's own
    # folder changes nothing.
    held_dir, open_dir = Path(tmp) / "config-hold", Path(tmp) / "config-open"
    for d, body in ((held_dir, '{"crossSessionInbound": "hold"}'), (open_dir, "{}")):
        d.mkdir()
        (d / "settings.json").write_text(body)
    hook("claude", "claude", "PostToolUse", {"session_id": "s-claude", "tool_name": "Bash"}, env_extra={"CLAUDE_CONFIG_DIR": str(held_dir)})
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False}, env_extra={"CLAUDE_CONFIG_DIR": str(held_dir)})
    own_hold = run("codex", "send", "--wake", "--to", "claude", "The recipient holds this.", env_extra={"CLAUDE_CONFIG_DIR": str(open_dir)})
    hook("claude", "claude", "PostToolUse", {"session_id": "s-claude", "tool_name": "Bash"}, env_extra={"CLAUDE_CONFIG_DIR": str(open_dir)})
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False}, env_extra={"CLAUDE_CONFIG_DIR": str(open_dir)})
    sender_hold = run("codex", "send", "--wake", "--to", "claude", "Only the sender holds messages.", env_extra={"CLAUDE_CONFIG_DIR": str(held_dir)})
    check("the recipient's own settings folder decides whether it is woken, not the sender's",
          f"crossSessionInbound is 'hold' in {held_dir}" in own_hold.stdout and len(sock.frames) == 4
          and "Only the sender holds messages." in sock.texts()[-1], own_hold.stdout + sender_hold.stdout)
    hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": sock.texts()[-1]})
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False})
    set_sql("UPDATE meta SET value='' WHERE key='socket_token:claude'")
    tokenless = run("codex", "send", "--wake", "--to", "claude", "Mail for a session without a token.")
    check("no socket wake-up where a setting would hold it or without the session's token, and the receipt says why",
          len(sock.frames) == 4 and "crossSessionInbound is 'hold'" in out.stdout
          and "didn't give this session a message token" in tokenless.stdout, out.stdout + tokenless.stdout)
    set_sql("UPDATE meta SET value='tok-4' WHERE key='socket_token:claude'")
    hook("claude", "claude", "PostToolUse", {"session_id": "s-claude", "tool_name": "Bash"})
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False})
    sock.close()
    for key in ("wake_pending", "wake_error", "last_wake"):
        set_sql("DELETE FROM meta WHERE key=?", f"{key}:codex")
    set_sql("UPDATE agents SET state='idle', detail='' WHERE name='codex'")
    run("claude", "send", "--wake", "--to", "codex", "A queued wake-up that Codex runs after the user's turn.")
    sent = pending("codex")[0]["value"]
    set_sql("UPDATE meta SET value=? WHERE key='wake_pending:codex'", repr(time.time() - 2000))  # stalled long ago
    typed = context(hook("codex", "codex", "UserPromptSubmit", {"session_id": "s-codex-new", "prompt": "The user types while it waits."}))
    kept = bool(pending("codex")) and "runs after the user's turn" not in typed
    hook("codex", "codex", "Stop", {"session_id": "s-codex-new", "stop_hook_active": False})
    queued_prompt = qlog.read_text().rsplit("--message ", 1)[-1]
    tagged = f"(wake-up {sent}):" in queued_prompt
    queued_prompt = queued_prompt.replace(f"(wake-up {sent}):", f"(wake-up {(pending('codex') or [{'value': ''}])[0]['value']}):")  # aged
    hook("codex", "codex", "UserPromptSubmit", {"session_id": "s-codex-new", "prompt": queued_prompt})
    check("Codex keeps even a stalled wake-up's reservation when the user types; the queued prompt, run after the "
          "user's turn, ends it", kept and tagged and not pending("codex"), typed + queued_prompt[:200])
    hook("codex", "codex", "Stop", {"session_id": "s-codex-new", "stop_hook_active": False})
    # The user's prompt and an old watch's finish read in one batch: that model call answers the user.
    run("codex", "send", "--wake", "--to", "agy", "A note that ends an old watch.")
    agy_step("USER_INPUT", "<USER_REQUEST>Another request.</USER_REQUEST>")
    finished("conv/task-old-watch")
    agy("PreInvocation")
    check("the user's prompt and an old watch's finish read together keep the model call the user's",
          q("SELECT turn_from FROM agents WHERE name='agy'")[0]["turn_from"] == "user")

    # 52. One wake-up at a time, what it marks read, and whose work a reply is for.
    queued = lambda: qlog.read_text().count("CALL queue") if qlog.exists() else 0  # noqa: E731

    def codex_idle():
        for key in ("wake_pending", "wake_error", "last_wake", "wake_msgs", "wake_read"):
            set_sql("DELETE FROM meta WHERE key=?", f"{key}:codex")
        set_sql("UPDATE agents SET state='idle', detail='' WHERE name='codex'")
        set_sql("UPDATE deliveries SET notified=1, read=1, replied=1 WHERE recipient='codex'")

    def codex_turn():  # the queued wake-up's prompt starts codex's turn
        return context(hook("codex", "codex", "UserPromptSubmit", {"session_id": "s-codex-new", "prompt": wake_prompt("codex")}))

    # The mail is taken and the reservation stored in one step: an older turn's Stop marking the agent idle in between,
    # then a second sender, doesn't produce a second wake-up.
    codex_idle()
    probe_two = ("import importlib.util, sys\n"
                 "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                 "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                 "conn = t.db(); took = t.claim_unseen; done = []\n"
                 "def claim(conn, agent, *a, **k):\n"
                 "    out = took(conn, agent, *a, **k)\n"
                 "    if k.get('reserve') and out and not done:\n"
                 "        done.append(1); t.set_state(conn, agent, 'idle', detail='')\n"
                 "        t.post(conn, 'agy', [agent], {agent: 'read'}, 'note', 'From the second sender.')\n"
                 "    return out\n"
                 "t.claim_unseen = claim\n"
                 "t.post(conn, 'claude', ['codex'], {'codex': 'read'}, 'note', 'From the first sender.')\n")
    before = queued()
    out = subprocess.run([PY, "-I", "-c", probe_two, str(SCRIPT)], capture_output=True, text=True, env=env_card)
    ctx = codex_turn()
    check("an older stop between taking the mail and sending the wake-up doesn't let a second sender wake it too",
          queued() == before + 1 and "From the second sender." in ctx, f"{queued() - before} calls {out.stderr} {ctx}")
    hook("codex", "codex", "Stop", {"session_id": "s-codex-new", "stop_hook_active": False})
    # While a restart holds the agent, no wake-up is sent; the mail waits for the next aichord command.
    codex_idle()
    holder = subprocess.Popen([PY, "-I", "-c", "import fcntl, sys, time\nf = open(sys.argv[1], 'a+')\n"
                               "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(2)\n",
                               str(ROOT / ".aichord" / "runtime" / "agent-codex.lock")], stdout=subprocess.PIPE, text=True)
    holder.stdout.readline()
    before = queued()
    run("claude", "send", "--wake", "--to", "codex", "Mail during a restart's handover.")
    during, waiting = queued() - before, q("SELECT notified FROM deliveries WHERE message=? AND recipient='codex'", last_id())[0]["notified"]
    holder.wait(timeout=10)
    tend("0")
    check("no wake-up is sent while a restart holds the agent, and the next command sends it",
          during == 0 and waiting is None and queued() == before + 1, f"{during} {waiting} {queued() - before}")
    codex_turn()
    hook("codex", "codex", "Stop", {"session_id": "s-codex-new", "stop_hook_active": False})
    # A wake-up marks read only what it showed in full: shortened and omitted questions stay unread.
    codex_idle()
    set_sql("UPDATE agents SET state='busy' WHERE name='codex'")  # mail collects while codex works
    run("claude", "send", "--wake", "--to", "codex", "A short note.")
    short = last_id()
    longs = [int(run("claude", "ask", "--to", "codex", "--no-wait", f"Long question {i}. " + "y" * 2000).stdout.split("#")[1].split()[0])
             for i in range(4)]
    set_sql("UPDATE agents SET state='idle' WHERE name='codex'")
    tend("0")
    text = qlog.read_text().rsplit("CALL queue", 1)[-1]
    codex_turn()
    reads = {r["message"]: r["read"] for r in q("SELECT message, read FROM deliveries WHERE recipient='codex' AND message>=?", short)}
    check("a wake-up marks read only what it showed in full, not shortened or omitted questions",
          "(shortened" in text and "not shown to save space" in text and reads[short] is not None
          and all(reads[i] is None for i in longs), str(reads))
    set_sql("UPDATE deliveries SET replied=1 WHERE recipient='codex'")
    hook("codex", "codex", "Stop", {"session_id": "s-codex-new", "stop_hook_active": False})
    # 201 questions read and not answered: one reminder, then the agent may stop (a list capped at 200 dropped the
    # oldest and reminded it at every stop).
    codex_idle()
    c = sqlite3.connect(ROOT / ".aichord" / "aichord.db")
    for i in range(201):
        mid = c.execute("INSERT INTO messages(ts, sender, recipients, kind, body) VALUES(?, 'claude', 'codex', 'ask', ?)",
                        (time.time(), f"Bulk question {i}.")).lastrowid
        c.execute("UPDATE messages SET thread=id WHERE id=?", (mid,))
        c.execute("INSERT INTO deliveries(message, recipient, action, notified, read) VALUES(?, 'codex', 'reply', ?, ?)",
                  (mid, time.time(), time.time()))
    c.commit()
    c.close()
    set_sql("UPDATE agents SET state='busy' WHERE name='codex'")
    stops = [hook("codex", "codex", "Stop", {"session_id": "s-codex-new", "stop_hook_active": active}).stdout.strip()
             for active in (False, True, True)]
    check("201 unanswered questions are reminded once, and then the agent may stop",
          "Bulk question" not in stops[0] and stops[0].count("from claude") == 201 and stops[1:] == ["", ""],
          f"{stops[0][:200]} {stops[1:]}")
    set_sql("UPDATE deliveries SET replied=1 WHERE recipient='codex'")
    # Replies go on with the work their question was asked for, even after a peer's turn in between: claude asked for
    # the user, answered a question of codex's meanwhile, and its answer once the reply arrives is the user's.
    set_sql("UPDATE deliveries SET notified=1, read=1, replied=1 WHERE recipient='claude'")
    set_sql("UPDATE agents SET state='busy' WHERE name='claude'")
    sock = FakeSocket(SOCKDIR / "claude5.sock")
    claude_env = {"CLAUDE_CODE_MESSAGING_SOCKET": sock.path, "CLAUDE_CODE_MESSAGING_TOKEN": "tok-5"}
    claude_hook("UserPromptSubmit", {"prompt": "Is the oracle safe?"})
    oracle = last_id()
    asked = int(run("claude", "ask", "--to", "codex", "--no-wait", "Check the oracle.").stdout.split("#")[1].split()[0])
    claude_stop("I asked codex about the oracle.")
    peer = int(run("codex", "ask", "--to", "claude", "--no-wait", "Which oracle file?").stdout.split("#")[1].split()[0])
    woken_turn()
    run("claude", "reply", str(peer), "oracle.py")
    claude_stop("Replied to codex about the file.")
    run("codex", "reply", str(asked), "ANSWER: The oracle is safe.")
    woken_turn()
    claude_stop("The oracle is safe; codex confirmed it.")
    check("a reply goes on with the work its question was asked for, after a peer's turn in between",
          thread_answers(oracle) == ["I asked codex about the oracle.", "The oracle is safe; codex confirmed it."],
          thread_answers(oracle))
    sock.close()
    # The same in Antigravity: the watch records whose work the reply is for; a stop showing the reply and a later
    # peer's question don't lose it.
    agy_step("USER_INPUT", "<USER_REQUEST>Prove the second bound.</USER_REQUEST>")
    agy("PreInvocation")
    second = last_id()
    asked = int(run("agy", "ask", "--to", "codex", "--no-wait", "Is the second bound tight?").stdout.split("#")[1].split()[0])
    model_call("Waiting for codex on the second bound.", lambda: started("task-second", "aichord watch"))
    watch = run("agy", "watch", background=True)
    time.sleep(1.5)
    run("codex", "reply", str(asked), "ANSWER: Tight.")
    watch.communicate(timeout=10)
    agy("Stop")  # shows the reply
    peer = int(run("codex", "ask", "--to", "agy", "--no-wait", "One question of mine.").stdout.split("#")[1].split()[0])
    agy("Stop")  # shows a peer's question: the rest of that turn is the peer's
    run("agy", "reply", str(peer), "Answered.")
    model_call("The second bound is tight; codex confirmed it.", lambda: finished("conv/task-second"))
    check("a watch's finish goes on with the work its reply was asked for, after a peer's question in between",
          ("The second bound is tight; codex confirmed it.", second) in agy_answers(), agy_answers()[-3:])
    # A finish aichord didn't see start, while a watch it saw is still out, isn't that watch: it counts as the user's
    # work, and the watch keeps what it recorded.
    agy_step("USER_INPUT", "<USER_REQUEST>Third request.</USER_REQUEST>")
    agy("PreInvocation")
    third = last_id()
    model_call("Started a watch for the third request.", lambda: started("task-third", "aichord watch"))
    set_sql("INSERT INTO meta(key, value) VALUES('watch_ended:agy', 'aichord') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    model_call("My helper finished.", lambda: finished("conv/helper-unseen"))
    check("an unseen task's finish doesn't take the record of a watch that is still out",
          ("My helper finished.", third) in agy_answers()
          and q("SELECT value FROM meta WHERE key='watch_ended:agy'")[0]["value"] == "aichord", agy_answers()[-3:])
    model_call("Read the mail.", lambda: finished("conv/task-third"))
    # A watch from before the swap that ends for a reply to a question asked before owners were recorded keeps the
    # turn's owner.
    set_sql("DELETE FROM meta WHERE key='agy_watches:agy'")
    agy_step("USER_INPUT", "<USER_REQUEST>Fourth request.</USER_REQUEST>")
    agy("PreInvocation")
    fourth = last_id()
    asked = int(run("agy", "ask", "--to", "codex", "--no-wait", "A question asked before the swap.").stdout.split("#")[1].split()[0])
    set_sql("DELETE FROM meta WHERE key=?", f"asked_for:{asked}")
    model_call("Waiting on a question from before the swap.")
    run("codex", "reply", str(asked), "ANSWER: An old reply.")
    model_call("The old question is answered.", lambda: finished("conv/watch-before-swap-2"))
    check("an old watch whose reply has no recorded owner keeps the turn's owner",
          ("The old question is answered.", fourth) in agy_answers(), agy_answers()[-3:])
    # A reply to a question asked during a peer's turn, shown as the user's turn ends, goes on with the peer's work:
    # the turn's closing text after it isn't an answer to the user.
    set_sql("UPDATE deliveries SET notified=1, read=1, replied=1 WHERE recipient='claude'")
    set_sql("UPDATE agents SET state='busy', turn_from='aichord' WHERE name='claude'")
    asked = int(run("claude", "ask", "--to", "codex", "--no-wait", "A check for codex's own question.").stdout.split("#")[1].split()[0])
    claude_hook("UserPromptSubmit", {"prompt": "A new request from the user."})
    request = last_id()
    run("codex", "reply", str(asked), "ANSWER: Checked.")
    claude_stop("The answer to the user's new request.")  # this stop shows the reply
    claude_stop("Passed the check on to codex.")
    check("a reply to a question asked for a peer's work keeps that ownership when a stop shows it",
          thread_answers(request) == ["The answer to the user's new request."], thread_answers(request))
    # A launch waits while a wake-up is being sent to the agent it replaces, then gives back the mail of a wake-up its
    # earlier session never started.
    codex_idle()
    run("claude", "send", "--wake", "--to", "codex", "Mail whose wake-up the old session never started.")
    stranded = last_id()
    was_pending = bool(pending("codex"))
    holder = subprocess.Popen([PY, "-I", "-c", "import fcntl, sys, time\nf = open(sys.argv[1], 'a+')\n"
                               "fcntl.flock(f, fcntl.LOCK_SH); print('held', flush=True); time.sleep(2)\n",
                               str(ROOT / ".aichord" / "runtime" / "agent-codex.lock")], stdout=subprocess.PIPE, text=True)
    holder.stdout.readline()
    began = time.time()
    run(None, "launch", "codex", "--tool", "codex", check=False)  # the stand-in codex exits at once
    took = time.time() - began
    holder.wait(timeout=10)
    check("a launch waits while a wake-up is being sent to the agent it replaces", took >= 1.5, f"{took:.1f} s")
    check("a launch gives back the mail of a wake-up its earlier session never started",
          was_pending and q("SELECT notified FROM deliveries WHERE message=? AND recipient='codex'", stranded)[0]["notified"] is None
          and not pending("codex"))

    # 53. Codex helpers. Codex runs SubagentStart and SubagentStop, with the agent's own session and the helper's id,
    # and hands a helper's result over without starting a turn: codex once sat idle for two hours. A helper's steps
    # don't make the agent busy; the reminder waits while helpers run; once the last one finishes, an idle agent is
    # woken once, and that turn goes on with the helpers' work.
    set_sql("UPDATE agents SET tool='codex', state='idle', detail='', session_id='s-codex-h', turn_from='' WHERE name='codex'")
    set_sql("INSERT INTO meta(key, value) VALUES('tool_path:codex', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            str(lbin / "codex-q"))
    codex_idle()

    def codex_h(event, **payload):
        return hook("codex", "codex", event, {"session_id": "s-codex-h", "transcript_path": "/tmp/t.jsonl", **payload})

    def codex_state():
        return q("SELECT state FROM agents WHERE name='codex'")[0]["state"]

    codex_h("UserPromptSubmit", prompt="Prove the third bound.")
    third = last_id()
    owed = int(run("claude", "ask", "--to", "codex", "--no-wait", "Is the lemma needed?").stdout.split("#")[1].split()[0])
    codex_h("PostToolUse", tool_name="Bash")  # shows the question
    for h in ("h1", "h2"):
        codex_h("SubagentStart", agent_id=h, agent_type="default")
    first_stop = codex_h("Stop", stop_hook_active=False, last_assistant_message="Two helpers are checking it.").stdout.strip()
    codex_h("PostToolUse", tool_name="Bash", agent_id="h1", agent_type="default")  # a helper's own step
    helper_step = codex_state()
    run("claude", "send", "--wake", "--to", "codex", "A note while codex's helpers run.")  # wakes codex for a peer's turn
    codex_h("UserPromptSubmit", prompt=qlog.read_text().rsplit("--message ", 1)[-1])
    note_stop = codex_h("Stop", stop_hook_active=False, last_assistant_message="Read the note.").stdout.strip()
    check("a helper's steps don't make an idle Codex agent busy", helper_step == "idle", helper_step)
    check("the forgotten-reply reminder waits while Codex helpers run", first_stop == "" and note_stop == "",
          first_stop + note_stop)
    before = queued()
    codex_h("SubagentStop", agent_id="h1", agent_type="default", stop_hook_active=False)
    after_first = queued() - before
    codex_h("SubagentStop", agent_id="h2", agent_type="default", stop_hook_active=False)
    helpers_prompt = qlog.read_text().rsplit("--message ", 1)[-1]
    check("once its last helper finishes, an idle Codex agent is woken once",
          after_first == 0 and queued() == before + 1 and helpers_prompt.startswith("[aichord] Your helpers have finished")
          and codex_state() == "busy", f"{after_first} {queued() - before} {helpers_prompt[:80]}")
    codex_h("UserPromptSubmit", prompt=helpers_prompt)
    codex_h("SubagentStart", agent_id="h3", agent_type="default")
    before = queued()
    codex_h("SubagentStop", agent_id="h3", agent_type="default", stop_hook_active=False)
    check("a helper that finishes while the agent works wakes nothing", queued() == before)
    collect = codex_h("Stop", stop_hook_active=False, last_assistant_message="The third bound holds; both helpers agree.").stdout
    check("a helper that finishes while the agent works is collected at its stop, not dropped",
          "Your helpers have finished. Collect their results" in collect, collect)
    last_stop = codex_h("Stop", stop_hook_active=True, last_assistant_message="The third bound holds; both helpers agree.")
    check("the turn after the helpers finish goes on with the user's work, after a peer's turn in between",
          thread_answers(third) == ["Two helpers are checking it.", "The third bound holds; both helpers agree."]
          and not q("SELECT 1 FROM messages WHERE kind='answer' AND body='Read the note.'"), thread_answers(third))
    check("once the helpers are done, the reminder comes", f"#{owed} from claude" in last_stop.stdout, last_stop.stdout)
    run("codex", "reply", str(owed), "Not needed.")
    # The events aren't balanced: a helper given more work starts no new SubagentStart, so its
    # own steps count as running; one that fails sends no SubagentStop, so after an hour without a step it stops
    # counting and no longer holds the reminder back.
    codex_h("UserPromptSubmit", prompt="Check the fourth bound.")
    fourth = last_id()
    pending_q = int(run("claude", "ask", "--to", "codex", "--no-wait", "And the fifth?").stdout.split("#")[1].split()[0])
    codex_h("PostToolUse", tool_name="Bash")  # shows it
    codex_h("PostToolUse", tool_name="Bash", agent_id="h1", agent_type="default")  # h1 reused: no SubagentStart
    reused_stop = codex_h("Stop", stop_hook_active=False, last_assistant_message="Helper h1 is on it again.").stdout.strip()
    before = queued()
    codex_h("SubagentStop", agent_id="h1", agent_type="default", stop_hook_active=False)
    reused = qlog.read_text().rsplit("--message ", 1)[-1]
    codex_h("UserPromptSubmit", prompt=reused)
    codex_h("Stop", stop_hook_active=False, last_assistant_message="The fourth bound holds.")
    check("a reused helper's steps count as running, and its finish wakes the agent on the work it was given",
          reused_stop == "" and queued() == before + 1 and thread_answers(fourth)[-1:] == ["The fourth bound holds."],
          reused_stop + str(thread_answers(fourth)))
    run("codex", "reply", str(pending_q), "Fine.")
    asked = int(run("claude", "ask", "--to", "codex", "--no-wait", "One more question.").stdout.split("#")[1].split()[0])
    codex_h("UserPromptSubmit", prompt=qlog.read_text().rsplit("--message ", 1)[-1])  # the mail wake-up's turn
    codex_h("SubagentStart", agent_id="h9", agent_type="default")  # a helper that will fail without reporting back
    held = codex_h("Stop", stop_hook_active=False).stdout.strip()
    set_sql("UPDATE meta SET value=? WHERE key='agy_running:codex'", json.dumps({"h9": [time.time() - 3700, ""]}))
    before = queued()
    tend("0")  # any later aichord command
    codex_h("UserPromptSubmit", prompt=qlog.read_text().rsplit("--message ", 1)[-1])
    released = codex_h("Stop", stop_hook_active=False).stdout
    check("a helper that never reports back counts as finished an hour after its last step: the next aichord command "
          "wakes the idle agent, and the reminder comes", held == "" and queued() == before + 1
          and f"#{asked} from claude" in released, held + released)
    run("codex", "reply", str(asked), "Answered.")
    # A failed helper wake-up is given back and tried again by a later command; only the wake-up whose tag matches
    # restores the work the agent paused, so an older prompt arriving late takes nothing.
    codex_h("Stop", stop_hook_active=False)
    codex_h("UserPromptSubmit", prompt="Check the sixth bound.")
    sixth = last_id()
    codex_h("SubagentStart", agent_id="h6", agent_type="default")
    codex_h("Stop", stop_hook_active=False, last_assistant_message="A helper checks the sixth bound.")
    before = queued()
    hook("codex", "codex", "SubagentStop", {"session_id": "s-codex-h", "transcript_path": "/tmp/t.jsonl", "agent_id": "h6",
                                            "agent_type": "default", "stop_hook_active": False}, env_extra={"FAKE_RC": "1"})
    failed_prompt = qlog.read_text().rsplit("--message ", 1)[-1]
    failed = (queued() - before, codex_state())
    for key in ("last_wake",):
        set_sql("UPDATE meta SET value='0' WHERE key=?", f"{key}:codex")
    tend("0")
    retried_prompt = qlog.read_text().rsplit("--message ", 1)[-1]
    check("a failed helper wake-up leaves the agent idle and is tried again by a later aichord command",
          failed == (1, "idle") and queued() == before + 2 and retried_prompt != failed_prompt, f"{failed} {queued() - before}")
    codex_h("UserPromptSubmit", prompt=failed_prompt)  # the older wake-up's prompt, arriving late
    late = (q("SELECT value FROM meta WHERE key='helpers_owner:codex'")[0]["value"],
            q("SELECT value FROM meta WHERE key='wake_pending:codex'")[0]["value"])
    check("an older helper wake-up's late prompt neither takes the paused work's owner nor ends the newer reservation",
          late[0] == "user" and late[1] and f"(wake-up {late[1]}):" in retried_prompt, str(late))
    codex_h("Stop", stop_hook_active=False, last_assistant_message="Collected the sixth bound's result.")
    codex_h("UserPromptSubmit", prompt=retried_prompt)
    codex_h("Stop", stop_hook_active=False)
    # A helper reused for the user's work without a start or a step is invisible when codex pauses; a peer's turn
    # comes next; the result still goes on with the user's work (the worst case is an extra line in the user's
    # thread, never a lost answer).
    codex_h("UserPromptSubmit", prompt="Ask h1 about the eighth bound.")
    eighth = last_id()
    codex_h("Stop", stop_hook_active=False, last_assistant_message="Waiting for reused h1.")
    run("claude", "send", "--wake", "--to", "codex", "A peer's note after the pause.")
    codex_h("UserPromptSubmit", prompt=qlog.read_text().rsplit("--message ", 1)[-1])
    codex_h("Stop", stop_hook_active=False, last_assistant_message="Read the note after the pause.")
    codex_h("SubagentStop", agent_id="h1", agent_type="default", stop_hook_active=False)
    codex_h("UserPromptSubmit", prompt=qlog.read_text().rsplit("--message ", 1)[-1])
    codex_h("Stop", stop_hook_active=False, last_assistant_message="The eighth bound holds.")
    check("a reused helper's result after a pause and a peer's turn is still filed as the user's answer",
          thread_answers(eighth) == ["Waiting for reused h1.", "The eighth bound holds."], thread_answers(eighth))
    # Helpers started for the user that finish during a peer's turn are collected at that turn's stop, and the
    # collecting goes on with the user's work.
    codex_h("UserPromptSubmit", prompt="Check the seventh bound.")
    seventh = last_id()
    codex_h("SubagentStart", agent_id="h7", agent_type="default")
    codex_h("Stop", stop_hook_active=False, last_assistant_message="A helper checks the seventh bound.")
    run("claude", "send", "--wake", "--to", "codex", "A peer's note while h7 runs.")
    codex_h("UserPromptSubmit", prompt=qlog.read_text().rsplit("--message ", 1)[-1])  # a peer's turn
    codex_h("SubagentStop", agent_id="h7", agent_type="default", stop_hook_active=False)
    codex_h("Stop", stop_hook_active=False, last_assistant_message="Read the peer's note.")
    codex_h("Stop", stop_hook_active=True, last_assistant_message="The seventh bound holds.")
    check("helpers that finish during a peer's turn are collected there, on the work they were started for",
          thread_answers(seventh)[-1:] == ["The seventh bound holds."]
          and not q("SELECT 1 FROM messages WHERE kind='answer' AND body=?", "Read the peer's note."), thread_answers(seventh))
    # While a helper wake-up is reserved, only its own prompt collects: an older prompt's turn doesn't take the result
    # at its stop.
    codex_h("UserPromptSubmit", prompt="Check the ninth bound.")
    ninth = last_id()
    codex_h("SubagentStart", agent_id="h11", agent_type="default")
    codex_h("Stop", stop_hook_active=False, last_assistant_message="A helper checks the ninth bound.")
    codex_h("SubagentStop", agent_id="h11", agent_type="default", stop_hook_active=False)  # reserves a wake-up
    reserved_prompt = qlog.read_text().rsplit("--message ", 1)[-1]
    codex_h("UserPromptSubmit", prompt="[aichord] Your helpers have finished (wake-up 1.0): an older one.")
    stale_stop = codex_h("Stop", stop_hook_active=False, last_assistant_message="An older prompt's turn.").stdout
    kept = (q("SELECT value FROM meta WHERE key='helpers_pending:codex'")[0]["value"],
            q("SELECT value FROM meta WHERE key='helpers_owner:codex'")[0]["value"])
    set_sql("UPDATE agents SET state='idle' WHERE name='codex'")
    codex_h("UserPromptSubmit", prompt=reserved_prompt)
    codex_h("Stop", stop_hook_active=False, last_assistant_message="The ninth bound holds.")
    check("while a helper wake-up is reserved, an older prompt's stop doesn't collect its result",
          "Collect their results" not in stale_stop and kept == ("1", "user")
          and thread_answers(ninth)[-1:] == ["The ninth bound holds."], f"{stale_stop} {kept} {thread_answers(ninth)}")
    # A silent helper stops counting an hour after its last step even when no other aichord command ran meanwhile.
    set_sql("UPDATE agents SET state='busy' WHERE name='codex'")
    set_sql("UPDATE meta SET value=? WHERE key='agy_running:codex'", json.dumps({"h10": [time.time() - 3700, ""]}))
    silent = codex_h("Stop", stop_hook_active=False).stdout
    check("an agent's own stop counts a helper silent for an hour as finished", "Collect their results" in silent, silent)
    codex_h("Stop", stop_hook_active=True)
    # agy's helpers follow the same expiry: one silent for an hour no longer holds its reminder back.
    owed_agy = int(run("codex", "ask", "--to", "agy", "--no-wait", "A question for agy.").stdout.split("#")[1].split()[0])
    agy("PreInvocation")  # shows it
    set_sql("INSERT INTO meta(key, value) VALUES('agy_running:agy', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            json.dumps({"conv/silent": [time.time() - 3700, "user"]}))
    agy_stop = agy("Stop").stdout
    check("agy's helper silent for an hour no longer holds the reminder back", f"#{owed_agy} from codex" in agy_stop, agy_stop)
    run("agy", "reply", str(owed_agy), "Answered.")
    probe_flags = ("import importlib.util, sys\n"
                   "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                   "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                   "print(' '.join(t.codex_hook_flags()))\n")
    flags = subprocess.run([PY, "-I", "-c", probe_flags, str(SCRIPT)], capture_output=True, text=True, env=env_card).stdout
    check("Codex is launched with its helper hooks", "hooks.SubagentStart=" in flags and "hooks.SubagentStop=" in flags
          and "hook codex SubagentStop" in flags, flags[:300])

    # 54. After a compaction an agent is grounded again: the start note, the team's recent discussion and the rule to
    # check the code before stating facts about it. agy once answered from a compacted summary with an invented
    # contract file and lender; Antigravity has no compaction hook, so aichord sees the compaction in agy's
    # transcript.
    long_body = "A long design note. " * 200
    run("claude", "send", "--wake", "--to", "codex", long_body)
    recent = int(run("claude", "send", "--wake", "--to", "codex", "The executor borrows from the Curve lender or Morpho.").stdout.split("#")[1].split()[0])
    agy("PreInvocation")
    agy_step("CHECKPOINT", "# Resuming from a compaction", "SYSTEM")
    with open(transcript, "a") as f:  # Antigravity numbers its steps; the compaction counts once by its number
        f.write(json.dumps({"step_index": 9001, "type": "CHECKPOINT", "source": "SYSTEM", "content": "summary"}) + "\n")
    grounded = agy("PreInvocation").stdout
    again = agy("PreInvocation").stdout
    check("after agy's compaction its next model call gets the start note, the rule and the recent discussion, once",
          "[aichord] You are agy" in grounded and "Your conversation was just compacted" in grounded
          and "path:line" in grounded and f"#{recent} " in grounded and "Curve lender or Morpho" in grounded
          and "You are agy" not in again and "just compacted" not in again, grounded[-1500:] + again)
    check("the recent discussion is oldest first and shortens a long message, with where to read it in full",
          grounded.find("A long design note.") < grounded.find("Curve lender or Morpho")
          and "full: aichord read" in grounded, grounded[-2500:])
    lines = transcript.read_text().splitlines()
    transcript.write_text("\n".join(l for l in lines if '"step_index": 9001' in l) + "\n")  # replaced, shorter
    reread = agy("PreInvocation").stdout
    check("a compaction already seen doesn't ground agy again when its transcript is read from the start",
          "just compacted" not in reread, reread)
    out = hook("claude", "claude", "SessionStart", {"session_id": "s-claude", "source": "compact"}).stdout
    check("Claude Code's compaction grounds claude too", "You are claude" in out and "just compacted" in out
          and f"#{recent} " in out, out[-800:])
    hook("codex", "codex", "PostCompact", {"session_id": "s-codex-h", "transcript_path": "/tmp/t.jsonl"})
    out = context(hook("codex", "codex", "PostToolUse", {"session_id": "s-codex-h", "transcript_path": "/tmp/t.jsonl",
                                                          "tool_name": "Bash"}))
    later = context(hook("codex", "codex", "PostToolUse", {"session_id": "s-codex-h", "transcript_path": "/tmp/t.jsonl",
                                                            "tool_name": "Bash"}))
    check("Codex compacting in the middle of a turn grounds codex at its next tool step, once",
          "You are codex" in out and "just compacted" in out and "just compacted" not in later, out[-600:] + later)
    asked = int(run("claude", "ask", "--to", "agy", "--no-wait", "Which lender does the executor use?").stdout.split("#")[1].split()[0])
    shown = agy("PreInvocation").stdout
    check("a question asks for the file and line behind each claim about the code",
          f"aichord reply {asked}" in shown and "path:line you opened" in shown, shown[-600:])
    run("agy", "reply", str(asked), "Curve lender or Morpho (from the test).")
    set_sql("INSERT INTO meta(key, value) VALUES('reground:claude', 'launched') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    set_sql("UPDATE meta SET value='' WHERE key='seated:claude'")
    out = context(hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": "Carry on."}))
    check("a launch grounds the agent with the recent discussion too", "You have just started" in out
          and f"#{recent} " in out, out[-600:])
    # Mail the agent hasn't seen yet comes once, in full, after the recent discussion, not in it too.
    run("claude", "send", "--wake", "--to", "agy", "A note agy hasn't seen yet.")
    with open(transcript, "a") as f:
        f.write(json.dumps({"step_index": 9002, "type": "CHECKPOINT", "source": "SYSTEM", "content": "summary"}) + "\n")
    out = agy("PreInvocation").stdout
    check("unseen mail isn't repeated in the recent discussion", "just compacted" in out
          and out.count("A note agy hasn't seen yet.") == 1, out[-1200:])
    # A compaction during a peer's turn keeps it the peer's, and a transcript replaced by one of the same size is
    # noticed.
    set_sql("UPDATE agents SET turn_from='aichord' WHERE name='agy'")
    set_sql("INSERT INTO meta(key, value) VALUES('turn_msg:agy', '7') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    with open(transcript, "a") as f:
        f.write(json.dumps({"step_index": 9003, "type": "CHECKPOINT", "source": "SYSTEM", "content": "summary"}) + "\n")
    agy("PreInvocation")
    check("a compaction in a peer's turn doesn't make it the user's",
          q("SELECT turn_from FROM agents WHERE name='agy'")[0]["turn_from"] == "aichord")
    size = transcript.stat().st_size
    line = json.dumps({"step_index": 9004, "type": "CHECKPOINT", "source": "SYSTEM", "content": "summary"})
    replacement = transcript.with_suffix(".new")
    replacement.write_text(line + "\n" + " " * max(0, size - len(line) - 2) + "\n")
    os.replace(replacement, transcript)
    out = agy("PreInvocation").stdout
    check("a transcript replaced by one of the same size still shows its new compaction", "just compacted" in out
          and transcript.stat().st_size == size, out[-400:])
    # A rewritten transcript keeps the new steps in it: a new prompt is recorded once, old ones aren't again.
    old = transcript.read_text()
    rewritten = old + "".join(json.dumps(x) + "\n" for x in (
        {"step_index": 9005, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>After the rewrite.</USER_REQUEST>"},
        {"step_index": 9006, "type": "CHECKPOINT", "source": "SYSTEM", "content": "summary"}))
    replacement = transcript.with_suffix(".new")
    replacement.write_text(rewritten.replace("summary", "summarY", 1))  # changed before the old end: a rewrite
    os.replace(replacement, transcript)
    out = agy("PreInvocation").stdout
    check("after a rewrite, a new prompt and a new compaction count, and nothing old counts again",
          len(q("SELECT * FROM messages WHERE kind='user' AND body='After the rewrite.'")) == 1
          and "just compacted" in out, out[-400:])
    run(None, "launch", "codex", "--tool", "codex", "--resume", "s-codex-h", check=False)
    resumed = q("SELECT value FROM meta WHERE key='reground:codex'")[0]["value"]
    run(None, "launch", "codex", "--tool", "codex", check=False)  # the stand-in codex exits at once
    check("a new launch marks the agent to be grounded at its first hook; a resumed conversation keeps its context",
          q("SELECT value FROM meta WHERE key='reground:codex'")[0]["value"] == "launched" and resumed == "", resumed)

    # 56. `aichord up` names its tmux session after the project's last two folders, so `tmux ls` tells several teams
    # apart. A name the user gave stays; a name another session has gets a number.
    nbin = Path(tmp) / "nbin"
    nbin.mkdir()
    (nbin / "tmux").write_text(r'''#!/bin/sh
case "$1" in
  display-message)
    [ -n "$FAKE_NAME" ] || exit 0
    case "$*" in
      *status-left-length*) printf '%s\t%s\t%s\n' "\$$FAKE_SID" "$FAKE_NAME" "${FAKE_WIDTH:-10}";;
      *) printf '%s\n' "$FAKE_NAME";;
    esac;;
  list-sessions) cat "$FAKE_SESSIONS";;
  rename-session)
    echo "$@" >> "$FAKE_TMUX_LOG"
    if [ -e "$FAKE_RACE" ]; then rm "$FAKE_RACE"; echo "$4" >> "$FAKE_SESSIONS"; exit 1; fi;;
  *) echo "$@" >> "$FAKE_TMUX_LOG";;
esac
''')
    (nbin / "tmux").chmod(0o755)
    sessions, nlog, race = Path(tmp) / "sessions.txt", Path(tmp) / "names.log", Path(tmp) / "race"
    probe_name = ("import importlib.util, sys\n"
                  "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                  "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                  "print(repr(t.name_session(t.Path(sys.argv[2]), '%5')))\n")

    def named(current, taken, root="/Users/m/projects/my.app", width="10", sid="12", raced=False):
        sessions.write_text("".join(f"{s}\n" for s in taken))
        nlog.unlink(missing_ok=True)
        race.write_text("") if raced else race.unlink(missing_ok=True)
        out = subprocess.run([PY, "-I", "-c", probe_name, str(SCRIPT), root], capture_output=True, text=True,
                             env={**env_card, "PATH": f"{nbin}:{os.environ['PATH']}", "FAKE_NAME": current,
                                  "FAKE_SID": sid, "FAKE_WIDTH": width, "FAKE_SESSIONS": str(sessions),
                                  "FAKE_TMUX_LOG": str(nlog), "FAKE_RACE": str(race)})
        return out.stdout.strip() + " | " + (nlog.read_text().strip().replace("\n", "; ") if nlog.exists() else "") + out.stderr

    first = named("12", ["3", "12"])
    check("a session tmux named by number is renamed after the project's last two folders, without '.' or ':', and "
          "the status bar is widened to show it",
          first == "'projects/my_app' | rename-session -t $12 projects/my_app; set-option -t $12 status-left-length 18", first)
    taken = named("12", ["12", "projects/my_app", "projects/my_app-2"], width="40")
    check("a name another session already has gets a number; a status bar wide enough is left alone",
          taken == "'projects/my_app-3' | rename-session -t $12 projects/my_app-3", taken)
    kept = [named("work", ["work"]), named("2026", ["2026"]), named("", [])]
    check("a name the user gave, a number the user chose (not the session's own id), or no answer from tmux leaves the "
          "session as it is", kept == ["'' | "] * 3, kept)
    check("a project at the top of the disk is named by its one folder",
          named("12", ["12"], "/proj") == "'proj' | rename-session -t $12 proj")
    hashed = named("12", ["12"], "/x/proj#S/app")
    check("a '#' in a folder name is doubled for tmux, which would expand it",
          hashed == "'proj#S/app' | rename-session -t $12 proj##S/app; set-option -t $12 status-left-length 13", hashed)
    raced = named("12", ["12"], raced=True)
    check("a name another start takes at the same moment makes this one try the next number",
          raced == "'projects/my_app-2' | rename-session -t $12 projects/my_app; rename-session -t $12 projects/my_app-2; "
                   "set-option -t $12 status-left-length 20", raced)

    # 57. Text given twice, or only agent names, is refused instead of sent: the words used to win and the piped text
    # was dropped, so messages went out carrying only names such as "agy codex".
    def stored(body):
        return bool(q("SELECT 1 FROM messages WHERE body=?", body))

    twice = run("agy", "send", "claude,codex", stdin="The real note.", check=False)
    check("words and piped text together are refused, with a hint about --to, and nothing is stored",
          twice.returncode != 0 and "given twice" in twice.stderr and "--to" in twice.stderr
          and not stored("claude,codex") and not stored("The real note."), twice.stderr)
    names = run("agy", "ask", "--to", "claude", "--no-wait", "codex", "agy", stdin="", check=False)
    check("a question whose whole text is agent names is refused", names.returncode != 0
          and "whole text is agent names" in names.stderr and not stored("codex agy"), names.stderr)
    lead = int(run("agy", "ask", "--to", "claude", "--no-wait", "Who should lead the review?", stdin="").stdout.split("#")[1].split()[0])
    reply = run("claude", "reply", str(lead), "codex", stdin="", check=False)
    piped = run("agy", "send", "--to", "claude", stdin="Only on stdin.", check=False)
    check("a reply may be a name, and text on stdin alone still works", reply.returncode == 0 and stored("codex")
          and piped.returncode == 0 and stored("Only on stdin."), reply.stderr + piped.stderr)

    # A pipe whose writer stays open can't hold a message given as words: text waiting in it is refused at once, and
    # an empty one is no text.
    def open_pipe(data):
        r, w = os.pipe()
        if data:
            os.write(w, data)
        began = time.time()
        try:
            out = subprocess.run([PY, "-I", str(SCRIPT), "send", "--to", "claude", "Words beside an open pipe."], cwd=ROOT,
                                 env={**CLEAN_ENV, "AICHORD_ROOT": str(ROOT), "AICHORD_AGENT": "agy"},
                                 stdin=r, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            out = None
        finally:
            os.close(r)
            os.close(w)
        return out, time.time() - began

    full, took_full = open_pipe(b"Text whose writer never closes")
    empty, took_empty = open_pipe(b"")
    check("a pipe whose writer stays open doesn't hold the command: waiting text is refused at once, an empty pipe "
          "is no text", full is not None and full.returncode != 0 and "given twice" in full.stderr and took_full < 10
          and empty is not None and empty.returncode == 0 and took_empty < 10,
          f"{took_full:.1f} s {full and full.stderr} | {took_empty:.1f} s {empty and empty.stderr}")

    # 58. A helper collection that changes nothing ends with "nothing new", which isn't recorded as another answer to
    # the user (about 170 such answers once piled up). Only a collection aichord started counts: the user's own
    # request for those words is recorded, and so is a collection that changes the answer.
    set_sql("UPDATE agents SET tool='codex', state='idle', detail='', session_id='s-codex-h', turn_from='' WHERE name='codex'")
    set_sql("INSERT INTO meta(key, value) VALUES('tool_path:codex', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            str(lbin / "codex-q"))
    codex_idle()
    codex_h("UserPromptSubmit", prompt="Check the tenth bound.")
    tenth = last_id()
    codex_h("SubagentStart", agent_id="h20", agent_type="default")
    codex_h("Stop", stop_hook_active=False, last_assistant_message="The tenth bound holds; a helper double-checks.")
    codex_h("SubagentStop", agent_id="h20", agent_type="default", stop_hook_active=False)  # codex idle: woken
    codex_h("UserPromptSubmit", prompt=qlog.read_text().rsplit("--message ", 1)[-1])
    codex_h("Stop", stop_hook_active=False, last_assistant_message="nothing new")
    codex_h("UserPromptSubmit", prompt="Check the eleventh bound.")
    eleventh = last_id()
    codex_h("SubagentStart", agent_id="h21", agent_type="default")
    codex_h("SubagentStop", agent_id="h21", agent_type="default", stop_hook_active=False)  # codex busy: collected at its stop
    collect = codex_h("Stop", stop_hook_active=False, last_assistant_message="The eleventh bound holds.").stdout
    codex_h("Stop", stop_hook_active=True, last_assistant_message="Nothing new.")
    check("a collection that changes nothing, woken or at the agent's stop, isn't recorded again",
          "end your turn with exactly: nothing new" in collect
          and thread_answers(tenth) == ["The tenth bound holds; a helper double-checks."]
          and thread_answers(eleventh) == ["The eleventh bound holds."], f"{thread_answers(tenth)} {thread_answers(eleventh)}")
    codex_h("UserPromptSubmit", prompt="If nothing changed since yesterday, reply exactly: nothing new")
    asked_words = last_id()
    codex_h("Stop", stop_hook_active=False, last_assistant_message="nothing new")
    codex_h("UserPromptSubmit", prompt="Check the twelfth bound.")
    twelfth = last_id()
    codex_h("SubagentStart", agent_id="h22", agent_type="default")
    codex_h("SubagentStop", agent_id="h22", agent_type="default", stop_hook_active=False)
    codex_h("Stop", stop_hook_active=False, last_assistant_message="The twelfth bound seems to hold.")
    codex_h("Stop", stop_hook_active=True, last_assistant_message="Correction: the twelfth bound fails at 3 bp.")
    check("the user's own request for the words is recorded, and so is a collection that changes the answer",
          thread_answers(asked_words) == ["nothing new"] and thread_answers(twelfth) == [
              "The twelfth bound seems to hold.", "Correction: the twelfth bound fails at 3 bp."],
          f"{thread_answers(asked_words)} {thread_answers(twelfth)}")

    # 59. Antigravity starts one helper with "Created the following subagent:" (785 times in agy's transcripts,
    # against 121 for several): it counts as running too.
    transcript = Path(tmp) / "agy-single.jsonl"
    agy_step("USER_INPUT", "<USER_REQUEST>Draft the release note.</USER_REQUEST>")
    agy("PreInvocation")
    agy_step("GENERIC", "Created the following subagent:\n" + json.dumps({"conversationId": "helper-single"}), "MODEL")
    agy("PreInvocation")
    running = q("SELECT value FROM meta WHERE key='agy_running:agy'")
    check("a single helper of agy's counts as running", running and "helper-single" in running[0]["value"], running)

    # 60. Decisions can be retired without a new one in their place; they leave the start note but stay in
    # decisions.md. Decisions never take the room kept for recent threads (one project had 43 in force).
    newest = q("SELECT MAX(id) AS n FROM decisions")[0]["n"]
    out = run("claude", "decide", "--retire", f"D{newest}", "A finished one-off task.", stdin="")
    dec = (ROOT / ".aichord" / "decisions.md").read_text()
    again = run("claude", "decide", "--retire", f"D{newest}", "Once more.", stdin="", check=False)
    replace = run("claude", "decide", "--supersedes", f"D{newest}", "Something else.", stdin="", check=False)
    bare = run("claude", "decide", "--retire", f"D{newest - 1}", stdin="", check=False)
    card = subprocess.run([PY, "-I", "-c", probe_card, str(SCRIPT), "plain"], capture_output=True, text=True, env=env_card).stdout
    check("a retired decision is marked in decisions.md and leaves the start note; it can't be retired or replaced again",
          out.returncode == 0 and "(retired: A finished one-off task.)" in dec and f"D{newest}:" not in card
          and "already retired" in again.stderr and "already retired" in replace.stderr
          and bare.returncode != 0 and "say why" in bare.stderr, out.stderr + again.stderr + replace.stderr + bare.stderr)
    recent_lines = card.split("Recent finished threads (newest first):\n")[-1].splitlines() if "Recent finished threads" in card else []
    check("with more decisions in force than fit, the start note still shows at least five recent finished threads",
          "older decisions in force" in card and sum(line.startswith("  - #") for line in recent_lines) >= 5
          and len(card.encode()) <= 7500, card[-1200:])

    # 61. Names after tools: roles come with the tool, a coding task goes to the agent in Claude Code,
    # and every agent is told its name isn't its model.
    run("codex", "code", "--no-wait", "Fix the fee rounding.", stdin="")
    check("a coding task goes to the agent in Claude Code by default",
          [r["recipient"] for r in q("SELECT recipient FROM deliveries WHERE message=?", last_id())] == ["claude"])
    seat = subprocess.run([PY, "-I", "-c", probe_seat, str(SCRIPT), "agy"], capture_output=True, text=True, env=env_card).stdout
    check("roles come with the tool, and the start note says a name isn't a model",
          "agy (Antigravity): big-picture architecture" in seat and "name says nothing about its model" in seat
          and "only claude's helpers edit" in seat, seat[:1200])

    # 62. A hook that can't get its seat's lock in time (a restart or a launch holds it) writes nothing rather than
    # write unfenced, and says so in hooks.log.
    ROOT4 = Path(tmp) / "proj4"
    ROOT4.mkdir()
    env4 = {"AICHORD_ROOT": str(ROOT4)}
    hook("claude", "claude", "SessionStart", {"session_id": "s4-claude", "source": "startup"}, env_extra=env4)
    c4 = sqlite3.connect(ROOT4 / ".aichord" / "aichord.db")
    c4.execute("UPDATE agents SET state='gone'")
    c4.commit()
    c4.close()
    holding = subprocess.Popen([PY, "-I", "-c", "import fcntl, sys, time\nf = open(sys.argv[1], 'a+')\n"
                                "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(3)\n",
                                str(ROOT4 / ".aichord" / "runtime" / "agent-claude.lock")], stdout=subprocess.PIPE, text=True)
    holding.stdout.readline()
    probe_locked = ("import importlib.util, os, sys\n"
                    "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                    "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                    "t.HOOK_LOCK_WAIT = 0.5; t.IN_HOOK = True\n"
                    "t.handle_hook('claude', 'tool', 'claude', {'tool_name': 'Bash'})\n")
    out = subprocess.run([PY, "-I", "-c", probe_locked, str(SCRIPT)], capture_output=True, text=True,
                         env={**env_card, **env4, "AICHORD_AGENT": "claude"})
    holding.wait()
    state4 = [r[0] for r in sqlite3.connect(ROOT4 / ".aichord" / "aichord.db").execute("SELECT state FROM agents WHERE name='claude'")]
    hooks_log = (ROOT4 / ".aichord" / "runtime" / "hooks.log").read_text() if (ROOT4 / ".aichord" / "runtime" / "hooks.log").exists() else ""
    check("a hook that can't get its seat's lock in time skips its event, writes nothing, and says so in hooks.log",
          out.returncode == 0 and out.stdout.strip() == "" and state4 == ["gone"]
          and "the seat stayed locked for 0.5 s, so this event was skipped" in hooks_log, out.stdout + out.stderr + hooks_log)

    # 63. A note waits for the recipient's next step; only --wake starts an idle agent's turn for it (an informational
    # note used to wake claude, with Claude Code's long notice about messages from other sessions). Questions,
    # replies and the user's messages still wake.
    set_sql("UPDATE agents SET state='busy', launched=0, session_id='s-claude' WHERE name='claude'")
    set_sql("UPDATE deliveries SET notified=1, read=1, replied=1 WHERE recipient IN ('claude', 'agy')")
    for key in ("wake_pending", "wake_error", "last_wake"):
        set_sql("DELETE FROM meta WHERE key=?", f"{key}:claude")
    sock = FakeSocket(SOCKDIR / "claude6.sock")
    quiet_env = {"CLAUDE_CODE_MESSAGING_SOCKET": sock.path, "CLAUDE_CODE_MESSAGING_TOKEN": "tok-6"}
    hook("claude", "claude", "PostToolUse", {"session_id": "s-claude", "tool_name": "Bash"}, env_extra=quiet_env)
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False}, env_extra=quiet_env)
    stored = run("codex", "send", "--to", "claude", "An informational note: the ledger is ready.", stdin="").stdout
    quiet_frames = len(sock.frames)
    check("a quiet note's receipt says it waits, and shows no wake-up path", "it waits for each one's next step" in stored
          and "--wake" in stored and "wake-up path" not in stored, stored)
    shown = context(hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": "Carry on."}, env_extra=quiet_env))
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False}, env_extra=quiet_env)
    run("codex", "send", "--wake", "--to", "claude", "Stop the release: my pass is withdrawn.", stdin="")
    check("a note doesn't wake an idle agent but reaches it at its next step; --wake wakes it",
          quiet_frames == 0 and "the ledger is ready" in shown and len(sock.frames) == 1
          and "my pass is withdrawn" in sock.texts()[0], f"{quiet_frames} {sock.frames} {shown[-300:]}")
    hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": sock.texts()[0]}, env_extra=quiet_env)
    hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": False}, env_extra=quiet_env)
    sock.close()
    watch = run("agy", "watch", background=True)
    time.sleep(1.5)
    run("codex", "send", "--to", "agy", "Another informational note.", stdin="")
    time.sleep(2)
    still_watching = watch.poll() is None
    run("codex", "send", "--wake", "--to", "agy", "A note that can't wait.", stdin="")
    ended, _ = watch.communicate(timeout=10)
    check("a note doesn't end agy's watch; one sent with --wake does", still_watching and "New mail" in ended, ended)
    run("agy", "inbox")
    asked = int(run("codex", "ask", "--to", "claude", "--no-wait", "Is the shorter hint enough?", stdin="").stdout.split("#")[1].split()[0])
    shown = run("claude", "read", str(asked)).stdout
    check("the reply hint is one line with the reply's parts", "Reply with: aichord reply" in shown
          and "path:line you opened" in shown and "<<'EOF'" not in shown, shown)
    run("claude", "reply", str(asked), "Yes.", stdin="")

    # 64. The big picture survives a compaction. A start note longer than the tool passes in full (Claude Code's cap
    # is 10,000 characters, Codex's 10,000 bytes) reaches the agent only as a short preview, and nothing else would
    # hold the intent. Research tracks hold it, the messages since the last review stay listed, and the whole note
    # fits each tool's budget.
    ROOT6 = Path(tmp) / "proj6"
    ROOT6.mkdir()
    env6 = {"AICHORD_ROOT": str(ROOT6)}
    for name, tool in (("claude", "claude"), ("agy", "agy"), ("codex", "codex")):
        hook(name, tool, "SessionStart", {"session_id": f"s6-{name}", "source": "startup"}, env_extra=env6)

    def track(*argv, agent="claude", stdin="", check=False):
        return run(agent, "track", *argv, stdin=stdin, env_extra=env6, check=check)

    def user(text, to="claude"):
        hook(to, "codex" if to == "codex" else "claude", "UserPromptSubmit",
             {"session_id": f"s6-{to}", "prompt": text}, env_extra=env6)
        return int(sqlite3.connect(ROOT6 / ".aichord" / "aichord.db").execute("SELECT MAX(id) FROM messages").fetchone()[0])

    fees = [user("the fee should be 0.3% normally and 1% while the pool is unbalanced, then back to 0.3%"),
               user("the pool is unbalanced right now; which rule could switch the fee automatically?"),
               user("Pausing comes first: if the pool must pause, the fee doesn't matter.")]
    card_text = (f"Intent: normal fee 0.3%; 1% while the pool is unbalanced, then back to 0.3% (#{fees[0]}, "
                 f"#{fees[1]}); pausing takes priority over the fee (#{fees[2]}). Triggers under study.")
    new = track("fee-controller", "--focus", card_text)
    stale = track("fee-controller", "--rev", "0", "--park", card_text)
    nosource = track("oracle", "An oracle card without a source.")
    toolong = track("oracle", "#1 " + "x" * 700)
    shown = track("fee-controller").stdout
    check("a track is written with its sources; a stale revision, a card without #N or one too long is refused",
          new.returncode == 0 and "rev 1" in new.stdout and stale.returncode != 0 and "at rev 1, not 0" in stale.stderr
          and "pausing takes priority" in stale.stderr and nosource.returncode != 0 and "#N" in nosource.stderr
          and toolong.returncode != 0 and "at most 600" in toolong.stderr and "[in focus, rev 1, claude" in shown,
          new.stdout + stale.stderr + nosource.stderr + toolong.stderr + shown)
    parked = track("fee-controller", "--rev", "1", "--park")
    run("claude", "track", "--reviewed", str(fees[2]), env_extra=env6)
    for i in range(70):  # far more than the last 45 or 60 of the user's messages, more than any note keeps
        user(f"Short instruction number {i}: run the next grid and report.")
    correction = user("Correction to the fee: return to 0.3% only after two balanced days.")
    track("stress-test", "--focus", f"Stress test of pausing, fee only, no donations (#{correction - 3}).")
    for _ in range(40):
        run("codex", "send", "--to", "agy", "Overnight report: " + "долгий отчёт, überprüft " * 100, stdin="", env_extra=env6)
    probe_ctx = ("import importlib.util, os, sys\n"
                 "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                 "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                 "t.IN_HOOK = True; conn = t.db()\n"
                 "agent, tool = sys.argv[2], sys.argv[3]\n"
                 "t.meta_set(conn, f'seated:{agent}', ''); t.meta_set(conn, f'reground:{agent}', 'compacted')\n"
                 "os.environ['AICHORD_AGENT'] = agent\n"
                 "import io, contextlib\n"
                 "buf = io.StringIO()\n"
                 "with contextlib.redirect_stdout(buf):\n"
                 "    t.handle_hook(tool, 'agy_pre' if tool == 'agy' else 'tool', agent, {'session_id': f's6-{agent}', 'tool_name': 'Bash'})\n"
                 "out = buf.getvalue()\n"
                 "import json\n"
                 "ctx = json.loads(out)['hookSpecificOutput']['additionalContext'] if tool != 'agy' else json.loads(out)['injectSteps'][0]['systemMessage']['systemMessage']\n"
                 "sys.stdout.write(ctx)\n")
    ctx = {tool: subprocess.run([PY, "-I", "-c", probe_ctx, str(SCRIPT), tool, tool], capture_output=True, text=True,
                                env={**env_card, **env6}).stdout for tool in ("claude", "codex", "agy")}
    sizes = {k: len(v.encode()) for k, v in ctx.items()}
    check("after a compaction the whole note fits the tool's budget, also with non-English text, and opens with who "
          "the agent is and that it was compacted",
          sizes["claude"] <= 9000 and sizes["codex"] <= 9000 and 0 < sizes["agy"] <= 24000
          and all(v.startswith("[aichord] You are") and "just compacted" in v for v in ctx.values()),
          f"{sizes} {ctx['claude'][:300]}")
    check("the research tracks survive 70 later messages and a flood of reports: fees, its priority and its sources",
          all("fee-controller [parked" in v and "pausing takes priority" in v and f"#{fees[2]}" in v
              for v in ctx.values()), ctx["claude"][:3000])
    check("the user's correction after the review mark stays listed, even with a newer unrelated track",
          all(f"#{correction} " in v and "Correction to the fee" in v and f"reviewed (through #{fees[2]})" in v
              for v in ctx.values()), ctx["claude"][:3000])
    brief = subprocess.run([PY, "-I", "-c", "import importlib.util, sys\nspec = importlib.util.spec_from_file_location("
                            "'aichord', sys.argv[1])\nt = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                            "print(t.compaction_brief(t.db()))\n", str(SCRIPT)], capture_output=True, text=True,
                           env={**env_card, **env6}).stdout
    claude_hooks = json.loads(subprocess.run([PY, "-I", "-c", "import importlib.util, sys\nspec = importlib.util.spec_from_"
                                          "file_location('aichord', sys.argv[1])\nt = importlib.util.module_from_spec(spec); "
                                          "spec.loader.exec_module(t)\nprint(open(t.claude_settings(t.Path(sys.argv[2]))).read())\n",
                                          str(SCRIPT), str(ROOT6)], capture_output=True, text=True, env={**env_card, **env6}).stdout)
    check("Claude Code's pre-compaction hook asks the summary to keep the tracks and the user's words, within 10,000",
          "keep these word for word" in brief and "fee-controller" in brief and "Correction to the fee" in brief
          and len(brief) <= 10000 and claude_hooks["hooks"]["PreCompact"][0]["hooks"][0]["command"].endswith("hook claude PreCompact"),
          brief[:500])
    twice = track("stress-test", "--rev", "1", "short #1", stdin="A heredoc that would be dropped.")
    heredoc = track("stress-test", "--rev", "1", stdin=f"Stress test, from a heredoc (#{correction - 3}).")

    def track_open_pipe(*argv):  # a pipe whose writer stays open, with nothing in it
        r, w = os.pipe()
        began = time.time()
        try:
            out = subprocess.run([PY, "-I", str(SCRIPT), "track", *argv], cwd=ROOT6, stdin=r, capture_output=True, text=True,
                                 timeout=20, env={**CLEAN_ENV, **env6, "AICHORD_AGENT": "claude"})
        except subprocess.TimeoutExpired:
            out = None
        finally:
            os.close(r)
            os.close(w)
        return out, time.time() - began

    shown_open, took_shown = track_open_pipe("fee-controller")
    parked_open, took_parked = track_open_pipe("stress-test", "--rev", "2", "--park")
    parked_closed = track("stress-test", "--rev", "2", "--park", stdin="")  # stdin closed: a status change alone
    check("aichord track refuses text given twice and takes a heredoc; an open silent pipe holds no read, and a change "
          "made while one is open is refused at once (its text may still come), while a closed stdin allows it",
          twice.returncode != 0 and "given twice" in twice.stderr and heredoc.returncode == 0 and "rev 2" in heredoc.stdout
          and shown_open is not None and "pausing takes priority" in shown_open.stdout and took_shown < 10
          and parked_open is not None and parked_open.returncode != 0 and "open with no text" in parked_open.stderr
          and took_parked < 10 and parked_closed.returncode == 0 and "rev 3" in parked_closed.stdout,
          f"{twice.stderr} {heredoc.stdout} {took_shown:.1f} {took_parked:.1f} {parked_open and parked_open.stderr} {parked_closed.stdout}")
    late = subprocess.Popen([PY, "-I", str(SCRIPT), "track", "stress-test", "--rev", "3", "--focus"], cwd=ROOT6,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**CLEAN_ENV, **env6, "AICHORD_AGENT": "claude"})
    time.sleep(0.4)  # its replacement text comes late
    try:
        late.stdin.write(f"Updated goal and correction (#{fees[2]}).\n")
        late.stdin.close()
    except BrokenPipeError:
        pass
    late_err = late.stderr.read()
    late.wait(timeout=20)
    after_late = track("stress-test").stdout
    check("a status change whose replacement text arrives late is refused, not saved with the old text",
          late.returncode != 0 and "rev 3" in after_late and "[parked" in after_late, late_err + after_late)
    def slow_writer(name, pause):  # a writer that pauses mid-stream
        p = subprocess.Popen([PY, "-I", str(SCRIPT), "track", name, "--focus"], cwd=ROOT6, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env={**CLEAN_ENV, **env6, "AICHORD_AGENT": "claude"})
        p.stdin.write(f"Intent: normal fee 0.3% (#{fees[0]}).\n")
        p.stdin.flush()
        time.sleep(pause)
        try:
            p.stdin.write(f"Correction: pausing takes priority (#{fees[2]}).\n")
            p.stdin.close()
        except BrokenPipeError:
            pass
        err = p.stderr.read()
        p.wait(timeout=20)
        return p.returncode, err

    slow_ok, slow_ok_err = slow_writer("slow-card", 0.4)
    slow_card = track("slow-card").stdout
    slow_bad, slow_bad_err = slow_writer("stalled-card", 2.5)
    check("text that pauses mid-stream is read to its end; one that stalls is refused, never saved in part",
          slow_ok == 0 and "Correction: pausing takes priority" in slow_card and slow_bad != 0
          and "stopped without ending" in slow_bad_err and track("stalled-card").returncode != 0,
          f"{slow_ok} {slow_ok_err} {slow_card} {slow_bad} {slow_bad_err}")
    zero = track("oracle", "A card citing #0.")
    check("a card citing #0 is refused", zero.returncode != 0 and "that exists" in zero.stderr, zero.stderr)
    cleared = context(hook("claude", "claude", "SessionStart", {"session_id": "s6-claude", "source": "clear"}, env_extra=env6))
    check("after /clear the agent gets its start note and the recent discussion again", "You are claude" in cleared
          and "You have just started" in cleared and "The team's recent discussion" in cleared, cleared[-600:])
    # A revision written between another write and its record keeps both records.
    probe_race = ("import importlib.util, subprocess, sys, argparse\n"
                  "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                  "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                  "first = t.render_tracks; done = []\n"
                  "def render(conn):\n"
                  "    if not done:\n"
                  "        done.append(1)\n"
                  "        subprocess.run([sys.executable, '-I', sys.argv[1], 'track', 'race-card', '--rev', '1', f'Newer intent #{sys.argv[2]}'], check=True)\n"
                  "    first(conn)\n"
                  "t.render_tracks = render\n"
                  "t.cmd_track(argparse.Namespace(name='race-card', text=[f'Original intent #{sys.argv[2]}'], rev=None, reviewed=None, focus=True, park=False, done=False))\n")
    subprocess.run([PY, "-I", "-c", probe_race, str(SCRIPT), str(fees[0])], capture_output=True, text=True,
                   env={**env_card, **env6, "AICHORD_AGENT": "claude"})
    records = [r[0] for r in sqlite3.connect(ROOT6 / ".aichord" / "aichord.db").execute(
        "SELECT body FROM messages WHERE subject = 'track race-card' ORDER BY id")]
    check("a revision written between another write and its record keeps both records, each with its own text",
          len(records) == 2 and any("rev 1" in r and "Original intent" in r for r in records)
          and any("rev 2" in r and "Newer intent" in r for r in records), records)
    bad_cite = track("oracle", f"An oracle card citing #{correction + 10000}.")
    check("a card citing a message that doesn't exist is refused", bad_cite.returncode != 0 and "that exists" in bad_cite.stderr,
          bad_cite.stderr)
    # No section disappears unannounced, even when there's hardly room; counts include what was never fetched.
    for i in range(20):
        run("claude", "decide", f"Decision {i}: " + "settled detail " * 18, stdin="", env_extra=env6)
    c6 = sqlite3.connect(ROOT6 / ".aichord" / "aichord.db")
    for i in range(260):
        c6.execute("INSERT INTO messages(ts, sender, recipients, kind, body) VALUES(?, 'user', 'claude', 'user', ?)",
                   (time.time(), f"Bulk instruction {i}"))
    c6.execute("UPDATE messages SET thread = id WHERE thread IS NULL")
    c6.commit()
    pending = c6.execute("SELECT COUNT(*) FROM messages WHERE kind = 'user' AND id > ?", (fees[2],)).fetchone()[0]
    c6.close()
    probe_tight = ("import importlib.util, sys\n"
                   "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                   "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                   "print(t.seat_card(t.db(), 'claude', int(sys.argv[2]), 'compacted'))\n")
    tight = {b: subprocess.run([PY, "-I", "-c", probe_tight, str(SCRIPT), str(b)], capture_output=True, text=True,
                               env={**env_card, **env6}).stdout for b in (2600, 7500)}
    said = [line for line in tight[7500].splitlines() if line.startswith("  - #") and "Bulk instruction" in line or "Correction to the fee" in line]
    more = [line for line in tight[7500].split("The user's messages since")[1].splitlines() if "more:" in line][:1]
    check("a starved section still says what it held, and the start note keeps to a small budget",
          all(h in tight[2600] for h in ("Research tracks", "The user's messages", "Decisions in force"))
          and len(tight[2600].encode()) <= 2600 + 120, tight[2600][-900:])
    check("the count of the user's messages not shown includes those never fetched (more than 200 pending)",
          more and int(more[0].split("(")[1].split()[0]) == pending - len(said), f"{pending} {len(said)} {more}")
    probe_render = ("import importlib.util, sys\n"
                    "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                    "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                    "conn = t.db()\n"
                    "ids = [t.post(conn, 'codex', ['agy'], {'agy': 'read'}, 'note', ('first ' * 230) if i == 0 else f'note {i}') for i in range(10)]\n"
                    "items = [(conn.execute('SELECT * FROM messages WHERE id=?', (i,)).fetchone(), 'read') for i in ids]\n"
                    "out = t.render(conn, 'agy', items, limit=1500)\n"
                    "print(len(out.encode()))\n")
    rendered = subprocess.run([PY, "-I", "-c", probe_render, str(SCRIPT)], capture_output=True, text=True,
                              env={**env_card, **env6}).stdout.strip()
    check("the mail shown at once keeps to its byte limit, the line naming the rest included",
          rendered.isdigit() and int(rendered) <= 1500, rendered)
    listing = track().stdout
    tracks_md = (ROOT6 / ".aichord" / "tracks.md").read_text()
    check("aichord track lists the tracks and the review mark, and tracks.md keeps them for people",
          "fee-controller" in listing and "stress-test" in listing and f"#{fees[2]}" in listing
          and "**fee-controller** (parked, rev 2" in tracks_md and "**stress-test** (parked, rev 3" in tracks_md
          and parked.returncode == 0, listing + tracks_md)

    # 65. The user's own settings, and privacy. A before_launch command runs before its tool starts, and its failure
    # doesn't stop the launch; rules.md reaches every agent's start note; .aichord/ is readable only by the user, even
    # under a permissive umask, and a new project's git ignores all of it.
    marker = Path(tmp) / "before.log"
    settings(before_launch={"codex": ["sh", "-c", f'echo "before $0" >> {marker}', "codex"]})
    started = run(None, "launch", "codex", "--tool", "codex", env_extra=path_env, check=False)
    settings(before_launch={"codex": "sh -c 'exit 3'"})
    failed = run(None, "launch", "codex", "--tool", "codex", env_extra=path_env, check=False)
    malformed = []
    for value in ({"codex": "   "}, {"codex": 'sh -c "unfinished'}, {"codex": 7}, ["not", "a", "map"]):
        settings(before_launch=value)
        malformed.append(run(None, "launch", "codex", "--tool", "codex", env_extra=path_env, check=False))
    settings(agy_flags="--dangerously-skip-permissions")  # a string instead of a list
    out = subprocess.run([PY, "-I", "-c", probe, str(hooks_file), str(ROOT)], capture_output=True, text=True, env=env_card)
    shown = json.loads(out.stdout) if out.returncode == 0 else [out.stderr, None]
    flags_from_text = shown[1] == ["agy", "--dangerously-skip-permissions", "-i", shown[0]]
    settings()
    check("malformed settings never stop a launch: an empty or unparsable before_launch command is reported, a wrong "
          "type is ignored, and agy_flags given as text is split into flags",
          all("ARGS" in m.stdout for m in malformed) and "the command is empty" in malformed[0].stderr
          and "No closing quotation" in malformed[1].stderr and "TypeError" in malformed[2].stderr
          and not malformed[3].stderr and flags_from_text, [m.stdout[-80:] + m.stderr[-200:] for m in malformed] + shown)
    check("a before_launch command runs before its tool starts, and a failing one doesn't stop the launch",
          marker.exists() and marker.read_text() == "before codex\n" and "ARGS" in started.stdout
          and "before_launch command for codex failed (3)" in failed.stderr and "ARGS" in failed.stdout,
          started.stdout + started.stderr + failed.stdout + failed.stderr)
    (CONFIG / "rules.md").write_text("Sign nothing.\nKeep commits short.\n")
    ruled = seat_of("codex")
    (CONFIG / "rules.md").write_text("A long rule. " * 100)
    pointed = seat_of("codex")
    (CONFIG / "rules.md").unlink()
    check("the user's rules reach every agent's start note; a long rules file is pointed to",
          "The user's standing rules" in ruled and "Sign nothing. Keep commits short." in ruled
          and f"rules are in {CONFIG / 'rules.md'}" in pointed and "A long rule. A long rule." not in pointed, ruled[:1500])
    ROOT7 = Path(tmp) / "proj7"
    ROOT7.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ROOT7, check=True)
    (ROOT7 / "code.py").write_text("x = 1\n")
    subprocess.run([PY, "-I", str(SCRIPT), "hook", "claude", "SessionStart"], cwd=ROOT7, capture_output=True, text=True,
                   input=json.dumps({"session_id": "s7", "source": "startup"}), preexec_fn=lambda: os.umask(0),
                   env={**CLEAN_ENV, "AICHORD_ROOT": str(ROOT7), "AICHORD_AGENT": "claude", "TMUX_PANE": ""})
    mode = (ROOT7 / ".aichord").stat().st_mode & 0o777
    subprocess.run(["git", "add", "-A"], cwd=ROOT7, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=ROOT7, capture_output=True, text=True).stdout.split()
    (ROOT / ".aichord").chmod(0o755)
    run("claude", "status")
    check("a new project's .aichord is readable only by the user, even under a permissive umask, and git ignores all "
          "of it; an older project's is made private at its next command",
          mode == 0o700 and staged == ["code.py"] and (ROOT7 / ".aichord" / ".gitignore").read_text() == "*\n"
          and (ROOT / ".aichord").stat().st_mode & 0o777 == 0o700, f"{oct(mode)} {staged}")

    # 66. A wake-up Claude Code accepts and never delivers (seen once: the session had five background helpers
    # running, and the reservation then kept every later reply from waking it for 12 hours). The reservation ends when
    # the agent's next turn does: that Stop shows the mail it carried, and later mail wakes the agent again. The lost
    # wake-up's own prompt, arriving late after all, changes nothing of a newer one. Only the prompt that carries the
    # reservation's tag ends it: a helper's report that merely quotes a wake-up's wording doesn't.
    set_sql("UPDATE agents SET state='idle', detail='', turn_from='', launched=0, session_id='s-claude', pane='' WHERE name='claude'")
    set_sql("UPDATE agents SET state='busy', pane='' WHERE name IN ('agy', 'codex')")
    set_sql("UPDATE deliveries SET notified=COALESCE(notified, 1), read=COALESCE(read, 1), replied=COALESCE(replied, 1)")
    for key in ("wake_pending", "wake_error", "last_wake", "wake_msgs", "wake_read", "open_msgs"):
        set_sql("DELETE FROM meta WHERE key=?", f"{key}:claude")
    waking = lambda name: q("SELECT value FROM meta WHERE key=? AND value != ''", f"wake_pending:{name}")  # noqa: E731
    recorded = lambda body: bool(q("SELECT 1 FROM messages WHERE body=?", body))  # noqa: E731
    sock = FakeSocket(SOCKDIR / "claude7.sock")
    lost_env = {"CLAUDE_CODE_MESSAGING_SOCKET": sock.path, "CLAUDE_CODE_MESSAGING_TOKEN": "tok-7"}

    def claude7(event, **payload):
        return hook("claude", "claude", event, {"session_id": "s-claude", **payload}, env_extra=lost_env)

    claude7("PostToolUse", tool_name="Bash")  # records the socket
    claude7("Stop", stop_hook_active=False)
    run("codex", "send", "--wake", "--to", "claude", "Mail of a wake-up that is never delivered.", stdin="")
    run("codex", "send", "--wake", "--to", "claude", "Mail that can't wake it meanwhile.", stdin="")
    one_wake = len(sock.frames) == 1 and bool(waking("claude"))
    began = context(claude7("UserPromptSubmit", prompt='<agent-message from="a1">\n[Subagent hand-back] The record holds '
                                                       'no "[aichord] New messages for you" line.'))
    still = bool(waking("claude")) and "never delivered" not in began and "can't wake it meanwhile" in began
    ended = claude7("Stop", stop_hook_active=False).stdout
    check("a Claude Code wake-up that never starts a turn of its own keeps its reservation only until the agent's next "
          "turn ends, and that stop shows its mail",
          one_wake and still and "Mail of a wake-up that is never delivered." in ended and not waking("claude"),
          f"{one_wake} {still} {began} {ended}")
    claude7("Stop", stop_hook_active=True)
    run("codex", "send", "--wake", "--to", "claude", "Mail after the lost wake-up.", stdin="")
    newer = last_id()
    woke_again = len(sock.frames) == 2 and "Mail after the lost wake-up." in sock.texts()[1]
    claude7("UserPromptSubmit", prompt=sock.texts()[0])  # the lost one after all, late
    late = (bool(waking("claude")), q("SELECT read FROM deliveries WHERE message=? AND recipient='claude'", newer)[0]["read"])
    claude7("UserPromptSubmit", prompt=sock.texts()[1])
    check("after it, mail wakes the agent again, and the lost wake-up's prompt arriving late changes nothing of the newer one",
          woke_again and late == (True, None) and not waking("claude")
          and "never started a turn of its own" in (ROOT / ".aichord" / "runtime" / "wake.log").read_text(),
          f"{len(sock.frames)} {late}")
    claude7("Stop", stop_hook_active=False)
    sock.close()
    set_sql("UPDATE meta SET value='' WHERE key='socket:claude'")

    # 67. An agent that gives no sign of life is no longer invisible (two sat at a "trust this folder?" prompt for 24
    # hours listed as "starting"; one whose login was revoked was waited on for 3.6 hours). aichord reads its tab and
    # shows the last lines: for one still starting after two minutes or busy without a step for ten, wherever it is
    # mentioned; and when a wait for its reply runs out, for one that has been quiet for 45 seconds. Nothing is typed.
    sbin, screens, slog = Path(tmp) / "sbin", Path(tmp) / "screens", Path(tmp) / "stuck-tmux.log"
    sbin.mkdir()
    screens.mkdir()
    (sbin / "tmux").write_text('#!/bin/sh\ncase "$1" in\n  list-panes) cat "$FAKE_PANES";;\n'
                               '  capture-pane) echo "$@" >> "$FAKE_TMUX_LOG"; cat "$FAKE_SCREENS/$4" 2>/dev/null;;\n'
                               '  *) echo "$@" >> "$FAKE_TMUX_LOG";;\nesac\n')
    (sbin / "tmux").chmod(0o755)
    spanes = Path(tmp) / "spanes.txt"
    spanes.write_text(f"%8\t0\tclaude\t1\t-\t{ROOT}\n%9\t0\tcodex\t2\t-\t{ROOT}\n")
    (screens / "%8").write_text("──────────────────────────\n Accessing workspace:\n\n /proj\n\n Quick safety check: Is this a "
                                "project you created or one you trust?\n\n ❯ No, exit\n   Yes, I trust this folder\n\n"
                                " Enter to confirm · Esc to cancel\n")
    (screens / "%9").write_text("• Ran the tests\n\n■ Your access token could not be refreshed because your refresh token "
                                "was revoked. Please log out and sign in again.\n\n──────────\n› Ask Codex to do anything\n")
    stuck_env = {"TMUX": "fake", "PATH": f"{sbin}:{os.environ['PATH']}", "FAKE_PANES": str(spanes),
                 "FAKE_SCREENS": str(screens), "FAKE_TMUX_LOG": str(slog)}
    set_sql("UPDATE agents SET pane='%8', state='starting', detail='', updated=? WHERE name='claude'", time.time() - 300)
    set_sql("UPDATE agents SET pane='%9', state='busy', detail='last used Bash', updated=? WHERE name='codex'", time.time() - 60)
    set_sql("UPDATE agents SET state='busy', pane='', updated=? WHERE name='agy'", time.time())
    out = run("agy", "ask", "--to", "claude,codex", "--wait", "5", "Are you both there?", stdin="", env_extra=stuck_env).stdout
    there = last_id()
    receipt_part, _, timeout_part = out.partition("Still waiting on")
    check("the receipt shows the tab of an agent that has been starting for minutes, without borders, and not the tab "
          "of one that worked a minute ago",
          "claude may be stuck. Its tab (pane %8) shows:" in receipt_part and "| Yes, I trust this folder" in receipt_part
          and "──" not in receipt_part and "could not be refreshed" not in receipt_part
          and "tell the user now" in receipt_part, out)
    check("when the wait runs out, the tab of an agent that has been quiet for 45 seconds is shown too",
          "codex's tab (pane %9) shows:" in timeout_part and "Please log out and sign in again." in timeout_part
          and "codex may be stuck" not in timeout_part and "claude may be stuck" in timeout_part, out)
    recent = run("agy", "status", env_extra=stuck_env).stdout  # codex owes a reply and was quiet for a minute only
    set_sql("UPDATE agents SET updated=? WHERE name='codex'", time.time() - 700)
    status = run("agy", "status", env_extra=stuck_env).stdout
    for_user = run(None, "status", env_extra=stuck_env).stdout
    outside = run("agy", "status").stdout  # not in tmux: the tab can't be read
    check("status shows a busy agent without a step for ten minutes as possibly stuck, with its tab, and not one "
          "quiet for a minute; where the tab can't be read it says so; and aichord only reads tabs, it types nothing",
          "codex may be stuck. Its tab (pane %9) shows:" in status and "could not be refreshed" in status
          and "claude may be stuck" in recent and "codex's tab" not in recent and "codex may be stuck" not in recent
          and "claude may be stuck, and its tab (pane %8) can't be read from here" in outside
          and "send-keys" not in slog.read_text() and "capture-pane -p -t %9" in slog.read_text(), recent + status + outside)
    check("the advice under a tab is worded for whoever reads it: an agent tells the user, the user is told where to look",
          "tell the user now" in status and "tell the user now" not in for_user
          and "it is waiting for you in that tab" in for_user, for_user)
    run("agy", "close", str(there), "A test of stuck agents.", stdin="")
    set_sql("UPDATE agents SET state='busy', pane='', updated=? WHERE name IN ('claude', 'codex')", time.time())

    # 68. Reviews. A question whose subject starts with "review" is a review request like `aichord review`: its hint
    # asks for a verdict, and a reply whose VERDICT line isn't PASS, PASS WITH NITS or BLOCK, or a BLOCK without a
    # "Must fix", is refused (one review went nine rounds on "VERDICT: REVISE"). A reply to `aichord review` must
    # carry a verdict; other questions are answered freely. Round 3 is the user's call, and the author is told so.
    out = run("claude", "ask", "--to", "codex", "--no-wait", "--subject", "review round 3: the fee cut (branch fee-cut)",
              "Please review the branch.", stdin="")
    rev = int(out.stdout.split("#")[1].split()[0])
    hint = run("codex", "read", str(rev)).stdout
    revise = run("codex", "reply", str(rev), stdin="VERDICT: REVISE before deployment.\nFINDINGS: 1. The names are unclear.", check=False)
    bare_block = run("codex", "reply", str(rev), stdin="VERDICT: BLOCK\nFINDINGS: 1. Suggestion: rename the helper.", check=False)
    title_case = run("codex", "reply", str(rev), stdin="Verdict: approve\nFindings: none blocking.", check=False)
    hidden = run("codex", "reply", str(rev), stdin="VERDICT: BLOCK\nFINDINGS: None.\nCHECKED: the diff.\n"
                                                    "NOT CHECKED: whether any must-fix exists.", check=False)
    denied = run("codex", "reply", str(rev), stdin="VERDICT: BLOCK\nFINDINGS: No must-fix findings: formatting "
                                                    "suggestions only.", check=False)
    nits = run("codex", "reply", str(rev), stdin="**VERDICT:** pass with nits\nFINDINGS: 1. Suggestion: rename the helper.", check=False)
    check("a question whose subject starts with 'review' asks for a verdict, and round 3 tells the author it is the user's call",
          "VERDICT: PASS, PASS WITH NITS or BLOCK" in hint and "Round 3 of this review" in out.stdout
          and "let them choose" in out.stdout, hint[-300:] + out.stdout)
    check("a verdict that isn't one of the three, or a BLOCK without a 'Must fix', is refused and nothing is stored; a valid one is sent",
          revise.returncode != 0 and "PASS, PASS WITH NITS or BLOCK; yours says: REVISE" in revise.stderr
          and not recorded("VERDICT: REVISE before deployment.\nFINDINGS: 1. The names are unclear.")
          and bare_block.returncode != 0 and "Must fix" in bare_block.stderr and nits.returncode == 0,
          revise.stderr + bare_block.stderr + nits.stderr)
    check("the verdict line counts in any case, and a BLOCK needs a finding that starts with 'Must fix': the words in "
          "NOT CHECKED, or in a sentence denying one, don't count",
          title_case.returncode != 0 and "yours says: approve" in title_case.stderr
          and hidden.returncode != 0 and "among its FINDINGS" in hidden.stderr and denied.returncode != 0,
          title_case.stderr + hidden.stderr + denied.stderr)
    probe_block = ("import importlib.util, sys\n"
                   "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                   "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                   "ok = ['FINDINGS:\\n1. Must fix: a.py:3 loses the row.', 'FINDINGS: 1. Must fix: a.py:3.',\n"
                   "      'FINDINGS:\\n- **Must fix:** a.py:3.', '\\nFINDINGS:\\n- Must fix (a.py:3-9): the row is lost.',\n"
                   "      'FINDINGS: Must-fix: a.py:3.']\n"
                   "print([t.verdict_problem('VERDICT: BLOCK\\n' + x + '\\nCHECKED: all', True) == '' for x in ok])\n")
    out = subprocess.run([PY, "-I", "-c", probe_block, str(SCRIPT)], capture_output=True, text=True, env=env_card)
    check("a BLOCK whose finding starts with 'Must fix' passes in the forms reviewers use: numbered, inline, bold, with "
          "a file in brackets, hyphenated", out.stdout.strip() == str([True] * 5), out.stdout + out.stderr)
    out = run("claude", "ask", "--to", "codex", "--no-wait", "--subject", "review: the notes", "Please review the notes.", stdin="")
    notes = int(out.stdout.split("#")[1].split()[0])
    back = run("codex", "reply", str(notes), "Which notes do you mean?", stdin="", check=False)
    plain = int(run("claude", "ask", "--to", "codex", "--no-wait", "--subject", "should we review the fee design?",
                    "Give me your verdict in a sentence.", stdin="").stdout.split("#")[1].split()[0])
    plain_hint = run("codex", "read", str(plain)).stdout
    prose = run("codex", "reply", str(plain), "VERDICT: the design holds, with one open point.", stdin="", check=False)
    (ROOT / "a.txt").write_text("three\n")
    snap = int(run("claude", "review", "--no-wait", "--subject", "the third line", stdin="Changed a.txt.").stdout.split("#")[1].split()[0])
    deferred = run("codex", "reply", str(snap), "I can't read the snapshot yet.", stdin="", check=False)
    passed = run("codex", "reply", str(snap), stdin="Verdict: PASS\nFINDINGS: none\nCHECKED: a.txt\nNOT CHECKED: nothing", check=False)
    check("no round reminder before round 3; a question back on a review sent as a question is allowed; another "
          "question keeps the ANSWER format and any wording",
          "Round" not in out.stdout and back.returncode == 0 and "ANSWER; WHY" in plain_hint and prose.returncode == 0,
          out.stdout + back.stderr + plain_hint[-200:] + prose.stderr)
    check("a reply to `aichord review` without a verdict is refused, pointing to a note; with one it is sent",
          deferred.returncode != 0 and "send a note instead" in deferred.stderr
          and q("SELECT replied FROM deliveries WHERE message=? AND recipient='codex'", snap)[0]["replied"] is not None
          and passed.returncode == 0, deferred.stderr + passed.stderr)

    # 69. Several messages from the user before one answer (a quarter of them come within 90 seconds of the one
    # before). The lead is told the earlier ones it hasn't finished answering, at most six. Its answer takes them off
    # that list and is filed after the latest, as before: nothing records which messages an answer covers.
    set_sql("UPDATE deliveries SET notified=COALESCE(notified, 1), read=COALESCE(read, 1), replied=COALESCE(replied, 1)")
    set_sql("UPDATE agents SET state='idle', detail='', turn_from='', pane='' WHERE name='claude'")
    set_sql("DELETE FROM meta WHERE key='open_msgs:claude'")

    def typed(text):
        return context(hook("claude", "claude", "UserPromptSubmit", {"session_id": "s-claude", "prompt": text}))

    def said(text, active=False):
        return hook("claude", "claude", "Stop", {"session_id": "s-claude", "stop_hook_active": active,
                                                 "last_assistant_message": text})

    def last_answer():
        return dict(q("SELECT id, thread, subject FROM messages WHERE kind='answer' ORDER BY id DESC LIMIT 1")[0])

    def sent_to(mid):
        return sorted(r["recipient"] for r in q("SELECT recipient FROM deliveries WHERE message=?", mid))

    first_ctx = typed("Why did the vault revert?")
    bug = last_id()
    second_ctx = typed("Also analyse the night's performance.")
    night = last_id()
    check("the second of two messages in a row names the first as not finished yet",
          "haven't finished answering" not in first_ctx and f"This is the user's message #{night}." in second_ctx
          and f"haven't finished answering: #{bug}." in second_ctx, second_ctx[:500])
    said(f"The revert (#{bug}) came from a stale price; the night (#{night}) made 0.4%.")
    ans = last_answer()
    third_ctx = typed("And the day?")
    earlier_thread, latest_thread = run("codex", "thread", str(bug)).stdout, run("codex", "thread", str(night)).stdout
    check("the answer is filed after the latest message, and takes both off the list",
          ans["thread"] == night and ans["subject"] == "" and "haven't finished answering" not in third_ctx, f"{ans} {third_ctx[:300]}")
    check("the earlier message's thread shows that answer as the one that followed, without calling it its answer",
          f"a later answer, filed after the user's #{night}: #{ans['id']}" in earlier_thread
          and "stale price" in earlier_thread and "a later answer" not in latest_thread
          and latest_thread.count("stale price") == 1, earlier_thread + latest_thread)
    said("The day made 0.1%.")
    row_of = []
    for i in range(8):
        last_ctx = typed(f"Message {i} of eight in a row.")
        row_of.append(last_id())
    named = last_ctx.split("haven't finished answering:")[-1].split(".")[0]
    check("at most the six latest earlier messages are named",
          f"#{row_of[0]}" not in named and all(f"#{i}" in named for i in row_of[1:7]) and f"#{row_of[7]}" not in named, last_ctx[:400])
    said("All eight are done.")
    check("after that answer no earlier message is named", "haven't finished answering" not in typed("A new topic."))
    said("Noted.")

    # 70. An answer given while the lead's own question is open (31 % of all answers were) isn't shown to the agents
    # still answering it, and leaves the user's message on the list; the final answer goes to everyone. An old question
    # nobody closed doesn't count. When a wait runs out in the user's turn, the lead is told to say in one line what it
    # waits for.
    typed("Is the new bound safe?")
    bound = last_id()
    asked = int(run("claude", "ask", "--to", "codex", "--no-wait", "Check the new bound.", stdin="").stdout.split("#")[1].split()[0])
    said("My first view: probably safe. Waiting for codex.")
    early_to = sent_to(last_answer()["id"])
    also_ctx = typed("And the old bound?")
    old = last_id()
    run("codex", "reply", str(asked), "ANSWER: Safe.", stdin="")
    said(f"Both bounds are safe (#{bound}, #{old}); codex confirmed the new one.")
    final = last_answer()
    bound_thread = run("codex", "thread", str(bound)).stdout
    check("an answer given while the lead's question is open isn't shown to the agent still answering it and leaves the "
          "user's message on the list; the final answer goes to everyone",
          early_to == ["agy"] and f"haven't finished answering: #{bound}." in also_ctx
          and sent_to(final["id"]) == ["agy", "codex"] and final["thread"] == old,
          f"{early_to} {sent_to(final['id'])} {final} {also_ctx[:300]}")
    check("the first message's thread shows its own early answer and then the final one as a later answer",
          "My first view" in bound_thread and f"a later answer, filed after the user's #{old}: #{final['id']}" in bound_thread,
          bound_thread)
    said("Nothing more.", active=True)
    typed("And the gauge?")
    stale_ask = int(run("claude", "ask", "--to", "codex", "--no-wait", "An old question nobody closed.", stdin="").stdout.split("#")[1].split()[0])
    set_sql("UPDATE messages SET ts=? WHERE id=?", time.time() - 4000, stale_ask)
    said("The gauge is fine.")
    check("a question older than an hour that nobody closed no longer holds answers back",
          sent_to(last_answer()["id"]) == ["agy", "codex"]
          and "haven't finished answering" not in typed("One more check, please."), str(sent_to(last_answer()["id"])))
    out = run("claude", "ask", "--to", "codex", "--wait", "5", "A last check.", stdin="").stdout
    check("when a wait runs out during the user's turn, the lead is told to say in one line what it waits for",
          "Still waiting on: codex" in out and "tell the user in one line what you are waiting for" in out, out)
    for open_thread in (stale_ask, last_id()):
        run("claude", "close", str(open_thread), "A test of waiting.", stdin="")
    said("Waited.")

    # A reservation only its own wake-up may change (two hooks of one agent overlapping): a late prompt that passed
    # its tag check before the reservation ended must not speak for the newer one made meanwhile.
    probe_ack = ("import importlib.util, sys\n"
                 "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                 "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                 "conn = t.db()\n"
                 "for key, value in (('wake_pending', '2.0'), ('wake_read', '[]'), ('wake_msgs', '[]')):\n"
                 "    t.meta_set(conn, f'{key}:claude', value)\n"
                 "old = t.wake_started(conn, 'claude', '[aichord] New messages for you (wake-up 1.0):')\n"
                 "old = old or t.wake_started(conn, 'claude', 'a report that quotes [aichord] New messages for you')\n"
                 "kept = t.meta_get(conn, 'wake_pending:claude')\n"
                 "new = t.wake_started(conn, 'claude', '[aichord] New messages for you (wake-up 2.0):')\n"
                 "print(old, kept, new, repr(t.meta_get(conn, 'wake_pending:claude')), conn.in_transaction)\n")
    out = subprocess.run([PY, "-I", "-c", probe_ack, str(SCRIPT)], capture_output=True, text=True, env=env_card)
    check("only the prompt with the reservation's own tag ends it, in one step; an older tag or none doesn't",
          out.stdout.split() == ["False", "2.0", "True", "''", "False"], out.stdout + out.stderr)

    # 71. Teams of any size (#669): stated at `aichord up` and checked whole first; agents are named after their tool and
    # numbered from 0 (#692); the agents that write code are those in Claude Code, else in Codex; the start note counts
    # the actual peers; a tool's name addresses every agent in it. And changing the team while it runs, from an agent
    # (#675): spawn starts one more agent in a new tab of this team, or brings a stopped one back; stop closes the tab
    # aichord opened for it, found by its mark, and --drop takes it off the team.
    troot = Path(tmp) / "teamproj"
    troot.mkdir()

    def tq(sql, *p):
        c = sqlite3.connect(troot / ".aichord" / "aichord.db")
        c.row_factory = sqlite3.Row
        try:
            return c.execute(sql, p).fetchall()
        finally:
            c.close()

    def tsql(sql, *p):
        c = sqlite3.connect(troot / ".aichord" / "aichord.db")
        c.execute(sql, p)
        c.commit()
        c.close()

    xbin, xpanes, xtabs = Path(tmp) / "xbin", Path(tmp) / "xpanes.txt", Path(tmp) / "xtabs.txt"
    xlog, xn = Path(tmp) / "xtmux.log", Path(tmp) / "xn"
    xbin.mkdir()
    (xbin / "tmux").write_text('#!/bin/sh\ncase "$*" in\n  *aichord_agent}*) cat "$FAKE_TABS";;\n'
                               '  *list-panes*) cat "$FAKE_PANES";;\n'
                               '  *new-window*) n=$(( $(cat "$FAKE_N" 2>/dev/null || echo 0) + 1 )); echo $n > "$FAKE_N"; '
                               'echo "$@" >> "$FAKE_TMUX_LOG"; echo "@$n %10$n";;\n'
                               '  *window_index*) echo "@1 1";;\n  *window_id*) echo "@1";;\n  *display-message*) ;;\n'
                               '  *send-keys*) [ -n "$FAKE_SENDKEYS_FAIL" ] && { echo "no server" >&2; exit 1; }; '
                               'echo "$@" >> "$FAKE_TMUX_LOG";;\n'
                               '  *) echo "$@" >> "$FAKE_TMUX_LOG";;\nesac\n')
    for tool in ("tmux", "claude", "agy"):
        if tool != "tmux":
            (xbin / tool).write_text("#!/bin/sh\nexit 0\n")
        (xbin / tool).chmod(0o755)
    xpanes.write_text("")
    xtabs.write_text("")
    xenv = {"AICHORD_ROOT": str(troot), "PATH": f"{xbin}:{os.environ['PATH']}", "FAKE_PANES": str(xpanes),
            "FAKE_TABS": str(xtabs), "FAKE_TMUX_LOG": str(xlog), "FAKE_N": str(xn), "TMUX": "fake", "TMUX_PANE": "%6"}
    trun = lambda agent, *a, stdin="", **extra: run(agent, *a, stdin=stdin, env_extra={**xenv, **extra}, check=False)  # noqa: E731
    xenv_card = {**CLEAN_ENV, **xenv}
    tseat = lambda n: subprocess.run([PY, "-I", "-c", probe_seat, str(SCRIPT), n], capture_output=True, text=True,  # noqa: E731
                                     env=xenv_card).stdout
    real_root = os.path.realpath(troot)  # the mark names the project's real folder
    tab_of = lambda name, pane, dead="0": xtabs.write_text(xtabs.read_text() + f"{pane}\t{dead}\t{name} {real_root}\n")  # noqa: E731
    probe_team = ("import importlib.util, json, sys\n"
                  "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                  "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                  "print(json.dumps(t.parse_team(sys.argv[2])))\n")
    team_of = lambda spec: subprocess.run([PY, "-I", "-c", probe_team, str(SCRIPT), spec], capture_output=True,  # noqa: E731
                                          text=True, env=xenv_card)
    parsed = {s: team_of(s) for s in ("claude,claude,claude,codex,codex,agy,agy", "codex", "lead:claude,claude,math:codex",
                                      "claude,claude,claude-1", "claude,x:foo", "a:claude,a:codex", "all:codex", "", "x")}
    ok = lambda s: json.loads(parsed[s].stdout) if parsed[s].returncode == 0 else parsed[s].stderr  # noqa: E731
    check("a team is stated as tools, repeated for more agents in one, or NAME:TOOL; agents are numbered from 0, never "
          "taking a name the user gave",
          ok("claude,claude,claude,codex,codex,agy,agy") == [["claude-0", "claude"], ["claude-1", "claude"],
                                                            ["claude-2", "claude"], ["codex-0", "codex"], ["codex-1", "codex"],
                                                            ["agy-0", "agy"], ["agy-1", "agy"]]
          and ok("codex") == [["codex-0", "codex"]]
          and ok("lead:claude,claude,math:codex") == [["lead", "claude"], ["claude-0", "claude"], ["math", "codex"]]
          and ok("claude,claude,claude-1") == [["claude-0", "claude"], ["claude-2", "claude"], ["claude-1", "claude"]],
          json.dumps({s: ok(s) for s in parsed}))
    refused = {s: parsed[s].stderr for s in ("claude,x:foo", "a:claude,a:codex", "all:codex", "", "x")}
    check("an unknown tool, a name given twice or reserved, an empty team, or a name with no tool starts nothing",
          all(parsed[s].returncode != 0 and "nothing was started" in e for s, e in refused.items())
          and "no tool called foo" in refused["claude,x:foo"] and "a is named twice." in refused["a:claude,a:codex"]
          and "can't be an agent's name" in refused["all:codex"] and "The team is empty" in refused[""]
          and "x: which tool? Write NAME:TOOL." in refused["x"], json.dumps(refused))

    # An earlier team in this project, named before agents were numbered: claude and agy, both exited, agy still owing
    trun("claude", "hook", "claude", "SessionStart", stdin=json.dumps({"session_id": "s-old-claude", "source": "startup"}))
    trun("agy", "hook", "agy", "SessionStart", stdin=json.dumps({"conversationId": "s-old-agy"}))
    old_q = int(trun("claude", "ask", "--to", "agy", "--no-wait", "An old question.").stdout.split("#")[1].split()[0])
    tsql("UPDATE agents SET state='gone'")
    out = trun(None, "up", "--no-update", "--team", "claude,claude,codex")
    started = xlog.read_text() if xlog.exists() else ""
    check("up starts the stated team, one tab per agent named after it, each running only its agent and marked as its "
          "tab; the agents of an earlier team leave it, with what they owed closed",
          out.returncode == 0 and sorted(r["name"] for r in tq("SELECT name FROM agents")) == ["claude-0", "claude-1", "codex-0"]
          and all(f"-n {n} " in started for n in ("claude-0", "claude-1", "codex-0", "log"))
          and "exec " in started and "launch claude-1 --tool claude" in started and "launch codex-0 --tool codex" in started
          and f"@aichord_agent codex-0 {real_root}" in started and "remain-on-exit on" in started
          and tq("SELECT replied FROM deliveries WHERE message=? AND recipient='agy'", old_q)[0]["replied"] is not None,
          out.stdout + out.stderr + started)
    cards = {n: tseat(n) for n in ("claude-1", "codex-0")}
    check("every agent of the team is listed in the start note with its role; both Claude Code agents write code, and "
          "the lead asks the others",
          "claude-1 (Claude Code): writes all code" in cards["codex-0"] and "codex-0 (Codex): scientific and formal" in cards["claude-1"]
          and "asks both others" in cards["claude-1"] and "only the helpers of claude-0 and claude-1 edit" in cards["codex-0"],
          cards["codex-0"][:1500])
    out = trun("codex-0", "code", "--no-wait", "Fix the rounding.")
    to_tool = trun("codex-0", "ask", "--to", "claude", "--no-wait", "Both of you?")
    check("a coding task with several agents writing code names one of them; a tool's name reaches every agent in it",
          out.returncode != 0 and "several agents write code here (claude-0 and claude-1): name one with --to" in out.stderr
          and sorted(r["recipient"] for r in tq("SELECT recipient FROM deliveries WHERE message=(SELECT MAX(id) FROM messages)"))
          == ["claude-0", "claude-1"], out.stderr + to_tool.stdout + to_tool.stderr)
    trun("codex-0", "close", str(int(to_tool.stdout.split("#")[1].split()[0])), "A test of addressing.")

    out = trun(None, "up", "--no-update", "--team", "codex,agy")
    pair = tseat("codex-0")
    trun("agy-0", "code", "--no-wait", "Fix the rounding.")
    check("without Claude Code, the agent in Codex writes the code, and a coding task goes to it",
          out.returncode == 0 and "codex-0 (Codex): writes all code, tests and HTML artifacts; also scientific" in pair
          and "asks the other agent" in pair and "only codex-0's helpers edit" in pair
          and [r["recipient"] for r in tq("SELECT recipient FROM deliveries WHERE message=(SELECT MAX(id) FROM messages)")] == ["codex-0"],
          out.stderr + pair[:1200])
    xlog.unlink(missing_ok=True)
    out = trun(None, "up", "--no-update", "--team", "codex")
    solo, alone = tseat("codex-0"), trun("codex-0", "ask", "--no-wait", "Anyone?")
    check("a Codex alone is told nobody can be consulted, and a question has nobody to go to",
          out.returncode == 0 and "you are the only agent on this team" in solo and "both others" not in solo
          and alone.returncode != 0 and "no other agents" in alone.stderr, solo[:900] + alone.stderr)

    # A team of seven, running: three in Claude Code, two in Codex, two in Antigravity
    out = trun(None, "up", "--no-update", "--team", "claude,claude,claude,codex,codex,agy,agy")
    seven = ["claude-0", "claude-1", "claude-2", "codex-0", "codex-1", "agy-0", "agy-1"]  # panes %1 to %7
    check("up registers a team of seven before any of them starts",
          out.returncode == 0 and sorted(r["name"] for r in tq("SELECT name FROM agents")) == sorted(seven), out.stderr)
    for i, n in enumerate(seven, 1):
        tsql("UPDATE agents SET state='idle', pane=?, session_id=?, updated=? WHERE name=?", f"%{i}", f"s-{n}", time.time(), n)
        tab_of(n, f"%{i}")
    rows = lambda: "".join(f"%{i}\t0\t{n.split('-')[0]}\t{999990 + i}\t{n}\t{troot}\n" for i, n in enumerate(seven, 1))  # noqa: E731
    xpanes.write_text(rows())
    big, status = tseat("agy-1"), trun(None, "status").stdout
    check("in a team of seven every agent is listed, the lead asks the 6 others, and status keeps names apart from tools",
          all(f"  - {n} (" in big for n in seven) and "asks the 6 others" in big
          and re.search(r"claude-1 +Claude Code", status) is not None, big[:1500] + status)
    xlog.unlink(missing_ok=True)
    out = trun("claude-0", "spawn", "codex", TMUX_PANE="%1")
    log = xlog.read_text() if xlog.exists() else ""
    new = tq("SELECT * FROM agents WHERE name='codex-2'")
    note = tq("SELECT id, body FROM messages WHERE subject='codex-2 joined the team'")
    told = sorted(r["recipient"] for r in tq("SELECT recipient FROM deliveries WHERE message=?", note[0]["id"])) if note else []
    check("an agent asked to run another codex spawns one: a new tab next to the team's, running only that agent and "
          "marked as its tab, the agent registered at once, and the others told",
          out.returncode == 0 and f"-n codex-2 -c {troot} -a -t @1" in log and "exec " in log
          and "launch codex-2 --tool codex" in log and f"@aichord_agent codex-2 {real_root}" in log
          and "--resume" not in log and new and new[0]["state"] == "starting" and new[0]["tool"] == "codex"
          and told == sorted(n for n in seven if n != "claude-0"), out.stdout + out.stderr + log + str(told))
    xpanes.write_text(rows().replace(f"%5\t0\tcodex\t{999995}\tcodex-1", f"%5\t0\tzsh\t{999995}\tcodex-1")
                      + f"{new[0]['pane']}\t0\tcodex\t999989\tcodex-2\t{troot}\n")
    tab_of("codex-2", new[0]["pane"])
    xlog.unlink(missing_ok=True)
    out = trun("agy-0", "spawn", "codex", TMUX_PANE="%6")
    log = xlog.read_text() if xlog.exists() else ""
    check("a codex that stopped comes back in its own conversation before a new one is made",
          out.returncode == 0 and "launch codex-1 --tool codex --resume s-codex-1" in log
          and not tq("SELECT 1 FROM agents WHERE name='codex-3'"), out.stdout + out.stderr + log)
    running = trun("claude-0", "spawn", "claude-1", TMUX_PANE="%1")
    sandboxed = trun("codex-0", "spawn", "codex", TMUX_PANE="%4", CODEX_SANDBOX="seatbelt")
    twice = run(None, "launch", "codex-0", "--tool", "codex", env_extra={**xenv, "TMUX_PANE": "%99"}, check=False)
    named = trun("claude-0", "spawn", "audit:codex", TMUX_PANE="%1")
    nowhere = trun("claude-0", "spawn", "agy", TMUX="", TMUX_PANE="", FAKE_PANES=str(Path(tmp) / "none.txt"))
    check("spawn refuses a name that runs, works nowhere inside Codex's sandbox, takes NAME:TOOL, and outside tmux says "
          "how to start it by hand; launch refuses a second copy of a running agent",
          running.returncode != 0 and "claude-1 is already running" in running.stderr and "aichord spawn claude" in running.stderr
          and sandboxed.returncode != 0 and "sandbox" in sandboxed.stderr
          and twice.returncode != 0 and "codex-0 is already running, in pane %4" in twice.stderr
          and named.returncode == 0 and tq("SELECT tool FROM agents WHERE name='audit'")[0]["tool"] == "codex"
          and nowhere.returncode != 0 and "aichord launch agy-2 --tool agy" in nowhere.stderr,
          running.stderr + sandboxed.stderr + twice.stderr + named.stderr + nowhere.stderr)
    audit_pane = tq("SELECT pane FROM agents WHERE name='audit'")[0]["pane"]
    xpanes.write_text(xpanes.read_text() + f"{audit_pane}\t0\tcodex\t999988\taudit\t{troot}\n")
    tab_of("audit", audit_pane)
    tsql("UPDATE agents SET state='busy' WHERE name='codex-0'")
    itself, working = trun("claude-0", "stop", "claude-0", TMUX_PANE="%1"), trun("claude-0", "stop", "codex-0", TMUX_PANE="%1")
    by_tool = trun("claude-0", "stop", "codex", TMUX_PANE="%1")
    xlog.unlink(missing_ok=True)
    stopped = trun("claude-0", "stop", "audit", TMUX_PANE="%1")
    log = xlog.read_text() if xlog.exists() else ""
    check("stop refuses the agent running it, an agent at work, and a tool's name (naming its agents); otherwise it "
          "closes the agent's tab and keeps it on the team",
          itself.returncode != 0 and "can't stop itself" in itself.stderr and working.returncode != 0
          and "--force" in working.stderr and by_tool.returncode != 0 and "Name one of the agents in Codex: codex-0, codex-1, codex-2" in by_tool.stderr
          and stopped.returncode == 0 and f"kill-pane -t {audit_pane}" in log
          and tq("SELECT state FROM agents WHERE name='audit'")[0]["state"] == "gone"
          and "aichord spawn audit brings it back" in stopped.stdout,
          itself.stderr + working.stderr + by_tool.stderr + stopped.stdout + log)
    owed_q = int(trun("claude-0", "ask", "--to", "audit", "--no-wait", "Still there?", TMUX_PANE="%1").stdout.split("#")[1].split()[0])
    dropped = trun("claude-0", "stop", "audit", "--drop", TMUX_PANE="%1")
    after = trun("claude-0", "ask", "--no-wait", "Who is here?", TMUX_PANE="%1")
    asked = sorted(r["recipient"] for r in tq("SELECT recipient FROM deliveries WHERE message=(SELECT MAX(id) FROM messages "
                                               "WHERE kind='ask')"))
    check("stop --drop takes the agent off the team: what it owed is closed for it, and questions to everyone leave it out",
          dropped.returncode == 0 and not tq("SELECT 1 FROM agents WHERE name='audit'") and f"#{owed_q}" in dropped.stdout
          and tq("SELECT replied FROM deliveries WHERE message=? AND recipient='audit'", owed_q)[0]["replied"] is not None
          and "audit" not in asked and "codex-2" in asked, dropped.stdout + dropped.stderr + str(asked))
    late = trun("audit", "hook", "codex", "SessionEnd", stdin=json.dumps({"session_id": "s-audit"}), AICHORD_LAUNCHED="1.0")
    probe_post = ("import importlib.util, sys\n"
                  "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                  "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                  "conn = t.db(); mid = t.post(conn, 'claude-0', ['audit'], {'audit': 'reply'}, 'ask', 'A late question.')\n"
                  "print(conn.execute('SELECT COUNT(*) FROM deliveries WHERE message=?', (mid,)).fetchone()[0])\n")
    late_post = subprocess.run([PY, "-I", "-c", probe_post, str(SCRIPT)], capture_output=True, text=True, env=xenv_card)
    check("an agent that left the team stays out: a late hook of its old tool doesn't register it again, and a question "
          "addressed to it before it left creates no reply it owes",
          not tq("SELECT 1 FROM agents WHERE name='audit'") and late_post.stdout.strip() == "0", late.stdout + late_post.stderr)

    # Tabs aichord didn't open for an agent are never closed by stop: a shell or an editor there may be the user's
    tsql("UPDATE agents SET state='idle', updated=? WHERE name IN ('claude-1', 'claude-2')", time.time())
    xtabs.write_text(f"%298\t0\tclaude-2 {real_root}\n" + "".join(line for line in xtabs.read_text().splitlines(keepends=True)
                                                        if not line.startswith(("%2\t", "%3\t"))))
    xpanes.write_text(xpanes.read_text().replace(f"%3\t0\tclaude\t{999993}", f"%3\t0\tzsh\t{999993}")
                      .replace(f"%2\t0\tclaude\t{999992}\tclaude-1\t{troot}", f"%2\t0\tvim\t{999992}\tclaude-1\t"))
    xlog.unlink(missing_ok=True)
    left_shell, editor = trun("claude-0", "stop", "claude-2", TMUX_PANE="%1"), trun("claude-0", "stop", "claude-1", TMUX_PANE="%1")
    log = xlog.read_text() if xlog.exists() else ""
    check("stop never closes a tab aichord didn't open for the agent: a shell left behind stays open, and a program "
          "running in such a tab (an editor) makes stop refuse",
          left_shell.returncode == 0 and "nothing was closed" in left_shell.stdout and "kill-pane" not in log
          and tq("SELECT state FROM agents WHERE name='claude-2'")[0]["state"] == "gone"
          and editor.returncode != 0 and "runs in a tab aichord didn't open for it (pane %2)" in editor.stderr
          and tq("SELECT state FROM agents WHERE name='claude-1'")[0]["state"] == "idle",
          left_shell.stdout + left_shell.stderr + editor.stderr + log)

    tsql("UPDATE agents SET state='gone' WHERE name='agy-1'")
    # a pane split off beside agy-1's (even were it marked), and an earlier closed tab of agy-1's
    xtabs.write_text(f"%299\t0\tagy-1 {real_root}\n%296\t1\tagy-1 {real_root}\n%297\t1\tagy-0 {real_root}\n"
                     + xtabs.read_text())
    xpanes.write_text(xpanes.read_text().replace(f"%7\t0\tagy\t{999997}", f"%7\t1\tagy\t{999997}")
                      + f"%299\t0\tzsh\t999969\tsplit\t{troot}\n%296\t1\tagy\t999968\tagy-1\t\n"
                      + f"%297\t1\tagy\t999967\tagy-0\t\n")
    xlog.unlink(missing_ok=True)
    split = trun("claude-0", "stop", "agy-1", TMUX_PANE="%1")
    log = xlog.read_text() if xlog.exists() else ""
    xtabs.write_text(xtabs.read_text().replace(f"%299\t0\tagy-1 {real_root}\n", ""))  # tmux never copies a pane's mark to a split
    probe_restart = ("import importlib.util, sys\n"
                     "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                     "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                     "print(' '.join(sorted(t.team_panes(t.db(), t.Path(sys.argv[2]), '%1'))))\n")
    closing = subprocess.run([PY, "-I", "-c", probe_restart, str(SCRIPT), str(troot)], capture_output=True, text=True,
                             env=xenv_card).stdout.split()
    check("stop closes the agent's own pane and its earlier closed tabs, never a pane split off beside it; a restart "
          "also closes an agent's earlier closed tab",
          split.returncode == 0 and "kill-pane -t %7" in log and "kill-pane -t %296" in log and "%299" not in log
          and "%297" in closing and "%299" not in closing, split.stdout + split.stderr + log + str(closing))

    bystander = subprocess.Popen(["sleep", "60"])  # a live program whose process number a closed pane still reports
    xpanes.write_text(xpanes.read_text() + f"%295\t1\tagy\t{bystander.pid}\tagy-9\t\n")
    probe_dead = ("import importlib.util, sys\n"
                  "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                  "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                  "t.CLOSE_SECONDS = 1; t.close_panes(['%295']); print('closed')\n")
    dead_close = subprocess.run([PY, "-I", "-c", probe_dead, str(SCRIPT)], capture_output=True, text=True, env=xenv_card)
    alive = bystander.poll() is None
    bystander.kill()
    bystander.wait()
    check("closing a pane that shows its agent closed stops no process: the number it reports may belong to another "
          "program by now", dead_close.stdout.strip() == "closed" and alive, dead_close.stdout + dead_close.stderr)

    probe_race = ("import importlib.util, sys, argparse\n"
                  "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                  "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                  "other = t.db()\n"
                  "t.close_panes = lambda panes: other.execute(\"UPDATE agents SET launched=launched+1, state='starting' "
                  "WHERE name=?\", (sys.argv[2],))\n"
                  "t.cmd_stop(argparse.Namespace(name=sys.argv[2], drop=True, force=False))\n")
    race = subprocess.run([PY, "-I", "-c", probe_race, str(SCRIPT), "codex-2"], capture_output=True, text=True,
                          env={**xenv_card, "TMUX_PANE": "%1"})
    check("stop --drop leaves an agent on the team when a launch replaced it while its tab was closing",
          race.returncode != 0 and "started again while it was being stopped" in race.stderr
          and tq("SELECT state FROM agents WHERE name='codex-2'")[0]["state"] == "starting", race.stdout + race.stderr)

    n0 = int(xn.read_text().strip())
    xpanes.write_text(xpanes.read_text() + "".join(f"%10{n0 + i}\t0\tzsh\t{999970 + i}\tnew\t{troot}\n" for i in (1, 2)))
    xlog.unlink(missing_ok=True)
    first, second = trun("claude-0", "spawn", "agy", TMUX_PANE="%1"), trun("claude-0", "spawn", "agy", TMUX_PANE="%1")
    log = xlog.read_text() if xlog.exists() else ""
    panes_of = {r["name"]: r["pane"] for r in tq("SELECT name, pane FROM agents WHERE name IN ('agy-1', 'agy-2')")}
    check("two spawns in a row start two agents: the stopped agy-1 comes back, and its new tab, still at its shell, "
          "isn't taken for an agent that stopped by the second spawn, which starts agy-2",
          first.returncode == 0 and second.returncode == 0 and panes_of == {"agy-1": f"%10{n0 + 1}", "agy-2": f"%10{n0 + 2}"}
          and "launch agy-1 --tool agy --resume s-agy-1" in log and log.count("--resume") == 1
          and "launch agy-2 --tool agy" in log, first.stdout + second.stdout + second.stderr + log + str(panes_of))

    tsql("UPDATE agents SET state='idle', pane='%5', session_id='s-codex-1', updated=? WHERE name='codex-1'", time.time())
    xpanes.write_text(xpanes.read_text().replace(f"%5\t0\tzsh\t{999995}", f"%5\t0\tcodex\t{999995}"))
    tab_of("codex-1", "%5")
    trun("claude-0", "stop", "codex-1", TMUX_PANE="%1")
    xlog.unlink(missing_ok=True)
    before_fail = sorted(r["name"] for r in tq("SELECT name FROM agents"))
    failed = trun("claude-0", "spawn", "codex", "--fresh", TMUX_PANE="%1", FAKE_SENDKEYS_FAIL="1")
    log_failed = xlog.read_text() if xlog.exists() else ""
    after_fail = sorted(r["name"] for r in tq("SELECT name FROM agents"))
    xlog.unlink(missing_ok=True)
    fresh = run("claude-0", "spawn", "codex", "--fresh", "--", "-m", "some-model", env_extra={**xenv, "TMUX_PANE": "%1"},
                check=False)
    log = xlog.read_text() if xlog.exists() else ""
    check("spawn --fresh on a tool starts a new agent and leaves a stopped one's conversation alone; tool flags after -- "
          "reach the launch; a launch tmux can't type closes the new tab, and nothing is registered or announced",
          tq("SELECT state FROM agents WHERE name='codex-1'")[0]["state"] == "gone"
          and failed.returncode != 0 and "couldn't type the launch" in failed.stderr and "kill-window" in log_failed
          and before_fail == after_fail and fresh.returncode == 0 and "launch codex-3 --tool codex -- -m some-model" in log
          and "--resume" not in log and tq("SELECT session_id FROM agents WHERE name='codex-1'")[0]["session_id"] == "s-codex-1",
          failed.stderr + log_failed + fresh.stdout + fresh.stderr + log)

    probe_child = ("import importlib.util, sys, argparse, time\n"
                   "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                   "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                   "other, real = t.db(), t.subprocess.run\n"
                   "def run(cmd, *a, **k):\n"
                   "    out = real(cmd, *a, **k)\n"
                   "    if cmd[:2] == ['tmux', 'send-keys']:  # the launch registers it and finishes its first turn at once\n"
                   "        other.execute(\"INSERT INTO agents(name,tool,pane,state,detail,updated,launched) VALUES('quick',"
                   "'codex','%1','idle','',?,?) ON CONFLICT(name) DO UPDATE SET state='idle', launched=excluded.launched\","
                   " (time.time(), time.time()))\n"
                   "    return out\n"
                   "t.subprocess.run = run\n"
                   "t.cmd_spawn(argparse.Namespace(spec='quick:codex', fresh=False, extra=[]))\n"
                   "print(other.execute(\"SELECT state FROM agents WHERE name='quick'\").fetchone()[0])\n")
    quick = subprocess.run([PY, "-I", "-c", probe_child, str(SCRIPT)], capture_output=True, text=True,
                           env={**xenv_card, "TMUX_PANE": "%1"})
    check("spawn keeps the record of a launch that registered itself first, so a wake-up still finds it idle",
          quick.stdout.strip().splitlines()[-1:] == ["idle"], quick.stdout + quick.stderr)

    count_before = q("SELECT value FROM meta WHERE key IN ('review_count', 'last_review_commit') ORDER BY key")
    bad_base = run("claude", "review", "--no-wait", "--base", "no-such-ref", stdin="A review on a base that isn't there.",
                   check=False)
    count_after = q("SELECT value FROM meta WHERE key IN ('review_count', 'last_review_commit') ORDER BY key")
    check("a review on a base git doesn't know is refused before anything changes, so the next review's default base "
          "stays the last one sent",
          bad_base.returncode != 0 and "isn't a commit in this repository" in bad_base.stderr
          and [r["value"] for r in count_before] == [r["value"] for r in count_after], bad_base.stderr)
    probe_reviews = ("import importlib.util, sys, threading\n"
                     "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                     "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                     "real, gate = t.git, threading.Event()\n"
                     "def slow(*a, **k):\n"
                     "    if a[:1] == ('update-ref',) and threading.current_thread().name == 'A':\n"
                     "        gate.wait(3)\n"
                     "    return real(*a, **k)\n"
                     "t.git = slow\n"
                     "out = {}\n"
                     "def snap(who):\n"
                     "    out[who] = t.review_snapshot(t.db(), None, None)['text'].splitlines()[0]\n"
                     "a = threading.Thread(target=snap, args=('A',), name='A'); a.start()\n"
                     "import time; time.sleep(0.5)\n"
                     "b = threading.Thread(target=snap, args=('B',), name='B'); b.start(); time.sleep(0.5); gate.set()\n"
                     "a.join(); b.join()\n"
                     "conn = t.db(); last = t.meta_get(conn, 'last_review_number'); c = t.review_snapshot(conn, None, None)\n"
                     "print(last, real('rev-parse', f'refs/aichord/review-{last}') == t.meta_get(conn, 'last_review_commit') or 'x', "
                     "f'review-{last})' in c['text'].splitlines()[0])\n")
    out = subprocess.run([PY, "-I", "-c", probe_reviews, str(SCRIPT)], capture_output=True, text=True, env=env_card)
    words = out.stdout.split()
    check("two review requests at once are recorded one after the other, and the next names the snapshot actually "
          "recorded before it as its base",
          len(words) == 3 and words[2] == "True", out.stdout + out.stderr)
    probe_settings = ("import importlib.util, sys\n"
                      "spec = importlib.util.spec_from_file_location('aichord', sys.argv[1])\n"
                      "t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)\n"
                      "p = t.claude_settings(t.Path(sys.argv[2])); print(p.name, sorted(x.name for x in p.parent.iterdir() "
                      "if x.name.startswith('.claude-settings')))\n")
    out = subprocess.run([PY, "-I", "-c", probe_settings, str(SCRIPT), str(troot)], capture_output=True, text=True, env=xenv_card)
    check("Claude Code's settings file is replaced in one step, leaving no part behind (two Claude Code agents start at once)",
          out.stdout.strip() == "claude-settings.json []", out.stdout + out.stderr)

# ---- install and uninstall, in a home folder of their own ---------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp).resolve() / "home"
    links = {t: home / d / "aichord" for t, d in
             (("claude", ".claude/skills"), ("codex", ".codex/skills"), ("agy", ".gemini/config/skills"))}
    links["agy"].mkdir(parents=True)  # something else is there already: left alone
    hooks_file = home / ".gemini" / "config" / "hooks.json"
    hooks_file.write_text(json.dumps({"other": {"Stop": [{"command": "true"}]}}))
    env_home = {**CLEAN_ENV, "HOME": str(home)}
    inst = lambda *a: subprocess.run([PY, "-I", str(SCRIPT), *a], capture_output=True, text=True, env=env_home,  # noqa: E731
                                     cwd=tmp, timeout=120)
    dry = inst("install", "--dry-run")
    untouched = not (home / ".local").exists() and not links["claude"].is_symlink() and "aichord" not in hooks_file.read_text()
    first, again = inst("install"), inst("install")
    wrapper = (home / ".local" / "bin" / "aichord").read_text()
    entry = json.loads(hooks_file.read_text())
    skill_dir = str(SCRIPT.parent.parent)
    check("install --dry-run changes nothing; install writes the command, the skill links and agy's hooks, leaves what "
          "isn't its own, and a second install finds them in place",
          dry.returncode == 0 and untouched and os.readlink(links["claude"]) == skill_dir
          and os.readlink(links["codex"]) == skill_dir and links["agy"].is_dir() and not links["agy"].is_symlink()
          and str(SCRIPT) in wrapper and "other" in entry and str(SCRIPT) in json.dumps(entry.get("aichord"))
          and again.stdout.count("already in place") == 3, dry.stdout + first.stdout + first.stderr + again.stdout)
    gone = inst("uninstall")
    entry = json.loads(hooks_file.read_text())
    check("uninstall removes the command, the skill links and agy's hooks entry, and nothing else",
          gone.returncode == 0 and not (home / ".local" / "bin" / "aichord").exists() and not links["claude"].is_symlink()
          and not links["codex"].is_symlink() and links["agy"].is_dir() and "aichord" not in entry and "other" in entry,
          gone.stdout + gone.stderr)

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
