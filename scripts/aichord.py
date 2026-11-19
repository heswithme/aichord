#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# ///
"""aichord: a shared message board for AI coding agents working on one project.

Several agents, each in its own tool and its own tmux tab. The default team,
each named after its tool, because the user picks the model inside each tool:
  claude in Claude Code   writes all code
  agy    in Antigravity   big-picture architecture
  codex  in Codex         scientific and formal review
Agents are named after their tool and numbered from 0 (claude-0, claude-1, agy-0). Any
mix works (aichord up --team claude,claude,codex), and an agent or the user can start
and stop agents while the team runs (aichord spawn, aichord stop).
Whoever the user types to leads that turn. Agents talk to each other through
this command; each tool's hooks record the user's messages and deliver mail
between steps. When mail is stored, the command that stored it wakes each idle
recipient once, by the one path its tool supports: Claude Code's message
socket, `codex queue`, or the Antigravity agent's own `aichord watch`. There is
no background helper: whichever aichord command runs next also retries a failed
wake-up.

State lives in <project>/.aichord/:
  aichord.db    messages, deliveries, agent status, decisions (SQLite)
  log.md        every message in readable form, append-only
  decisions.md  numbered decisions
  tracks.md     research tracks and the review mark
  glossary.md   project terms (messages with undefined abbreviations get a warning)
  notes/        long specs, reviews and data, linked from messages by path

Run `aichord help` for the command list.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve()
SKILL_DIR = SCRIPT.parent.parent

TEAM = {"claude": "claude", "agy": "agy", "codex": "codex"}  # the default team: agent name -> tool
TOOL_LABEL = {"claude": "Claude Code", "agy": "Antigravity", "codex": "Codex"}
WRITER_TOOLS = ("claude", "codex", "agy")  # the agents that write code are those in the first of these on the team
RESERVED_NAMES = {"user", "all", "team", "others"}  # words aichord reads as recipients
# Each agent's job comes with its tool, whatever model runs in it.
ROLES = {
    "agy": "big-picture architecture, specs and cross-checks; explains things in plain English",
    "claude": "writes all code, tests and HTML artifacts",
    "codex": "scientific and formal advisor: math, invariants, edge cases, adversarial review",
}
WAIT_SECONDS = 90      # one `aichord wait` call; stays under Claude Code's 2-minute default shell limit
INLINE_LIMIT = 1500    # bodies up to this many characters are shown in full in hook context
CONTEXT_LIMIT = 6000   # cap on one batch of messages shown at once (a wake-up, a stop)
WAKE_RETRY = 30        # seconds before a failed wake-up is tried again
WAKE_STALLED = 900     # an accepted wake-up whose turn hasn't started after this long is reported as stalled; it is
                       # never repeated on time alone (a long compaction can come first)
WOKEN = "woken by aichord; its turn hasn't started yet"  # an agent's state between a wake-up and its turn's first hook
STARTING_LONG = 120    # an agent still "starting" after this many seconds probably sits at a prompt only the user answers
QUIET_LONG = 600       # seconds without a hook from an agent that should be working before aichord reads its tab
OVERDUE_QUIET = 45     # ... and when a wait for its reply has just run out: a revoked login once showed on a tab 50
                       # seconds before the lead's 90-second wait ended, and nobody looked for 3.6 hours
TAB_LINES = 8          # how many of the last lines on such a tab are shown (borders left out)
AWAIT_FRESH = 3600     # a question older than this no longer counts as one its sender waits on (20 of 1,155 replies
                       # took longer): an old question nobody closed doesn't hold anything back for ever
OPEN_MSGS = 6          # how many of the user's earlier messages it hasn't finished answering an agent is reminded of
# Claude Code's helpers (subagents) default to Sonnet: the strong model keeps the judgement, a cheaper one does the
# bounded jobs. Codex keeps its own default unless the user's settings name one. Antigravity takes a tier per helper,
# so the agy agent's instructions name it.
HELPER_MODEL = {"claude": "sonnet"}
# The user's own settings, all optional: config.json and rules.md in this folder (README, "Settings").
CONFIG_DIR = Path(os.environ.get("AICHORD_CONFIG_DIR") or Path.home() / ".aichord")
ACTIONS_THAT_WAKE = ("reply", "read")
NEW_MAIL = "notified IS NULL AND action IN ('reply', 'read')"  # deliveries that should wake their recipient

# --------------------------------------------------------------------------------------------
# Project, database, identity
# --------------------------------------------------------------------------------------------


def project_root() -> Path:
    env = os.environ.get("AICHORD_ROOT")
    if env:
        return Path(env)
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd()


def state_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / ".aichord"


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS agents(
    name TEXT PRIMARY KEY,
    tool TEXT,
    pane TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    state TEXT DEFAULT 'starting',      -- starting | idle | busy | waiting | gone
    detail TEXT DEFAULT '',             -- last tool used, or what it is waiting for
    updated REAL DEFAULT 0,
    launched REAL DEFAULT 0,
    turn_from TEXT DEFAULT ''           -- whose work the agent's turns are on: 'user', or 'aichord' (a peer's
                                        -- question); kept across stops until a new request changes it
);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    sender TEXT,
    recipients TEXT,                    -- comma-separated, as addressed
    kind TEXT,                          -- user | answer | ask | code | review | reply | note | decision
    thread INTEGER,
    subject TEXT DEFAULT '',
    body TEXT,
    ref TEXT DEFAULT ''                 -- commit or file the message is about
);
CREATE TABLE IF NOT EXISTS deliveries(
    message INTEGER,
    recipient TEXT,
    action TEXT,                        -- reply (must answer) | read (should read) | context (awareness only)
    notified REAL,                      -- when it was shown to the recipient (hook, wait or inbox)
    read REAL,                          -- when the full text was shown
    replied REAL,                       -- when it was answered with `aichord reply`, or closed
    PRIMARY KEY(message, recipient)
);
CREATE INDEX IF NOT EXISTS deliveries_open ON deliveries(recipient, notified);
CREATE TABLE IF NOT EXISTS decisions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    by TEXT,
    thread INTEGER,
    text TEXT,
    superseded_by INTEGER,              -- the decision that replaced this one; 0 when retired without one
    retired TEXT DEFAULT ''             -- why it was retired (aichord decide --retire)
);
CREATE TABLE IF NOT EXISTS threads(
    id INTEGER PRIMARY KEY,             -- the thread's first message
    closed TEXT DEFAULT ''              -- "<who>: <resolution>", from `aichord close`
);
CREATE TABLE IF NOT EXISTS tracks(      -- research tracks: the user's threads of work and their intent in each
    name TEXT PRIMARY KEY,
    status TEXT,                        -- focus | parked | done
    body TEXT,
    rev INTEGER,
    by TEXT,
    updated REAL
);
"""

GITIGNORE = "*\n"  # the whole conversation stays out of the project's git; a note worth keeping is copied out on purpose


IN_HOOK = False


def db(root: Path | None = None, create: bool = True) -> sqlite3.Connection:
    cdir = state_dir(root)
    if not create and not (cdir / "aichord.db").exists():
        sys.exit(f"aichord: no aichord session in {cdir.parent} (there is no .aichord/aichord.db). Start one with `aichord up`.")
    cdir.mkdir(parents=True, exist_ok=True)
    if cdir.stat().st_mode & 0o077:  # every message, and a session token for Claude Code's socket: the user's alone
        cdir.chmod(0o700)
    (cdir / "notes").mkdir(exist_ok=True)
    (cdir / "runtime").mkdir(exist_ok=True)
    gi = cdir / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE)
    conn = sqlite3.connect(cdir / "aichord.db", timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    if not IN_HOOK:
        confirm_session(conn)
    return conn


def me() -> str:
    """Who is running this command. Set once at launch; a plain shell is the user."""
    return os.environ.get("AICHORD_AGENT") or "user"


def config() -> dict:
    """The user's settings in config.json: agy_flags, helper_models, update_tools, before_launch. {} when none."""
    path = CONFIG_DIR / "config.json"
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        print(f"aichord: {path} can't be read ({e}), so the defaults apply.", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def helper_model(tool: str) -> str:
    """The default model of this tool's helpers: the user's setting, else aichord's default, else "" (the tool's own)."""
    models = config().get("helper_models")
    return str((models.get(tool) if isinstance(models, dict) else "") or HELPER_MODEL.get(tool, ""))


RULES_MAX = 1000  # bytes of the user's rules.md shown in the start note; a longer file is only pointed to


def own_rules() -> str:
    """The user's standing rules (rules.md), for every agent's start note."""
    path = CONFIG_DIR / "rules.md"
    try:
        text = " ".join(path.read_text().split())
    except OSError:
        return ""
    if len(text.encode()) > RULES_MAX:
        return f"The user's standing rules are in {path}: read them before you act."
    return f"The user's standing rules ({path}): {text}" if text else ""


SESSION_ENV = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID", "agy": "ANTIGRAVITY_CONVERSATION_ID"}


def confirm_session(conn) -> None:
    """When an agent runs an aichord command, its tool tells us (through the environment) which session is the main one."""
    agent, tool = os.environ.get("AICHORD_AGENT"), os.environ.get("AICHORD_TOOL", "")
    sid = os.environ.get(SESSION_ENV.get(tool, ""), "")
    if not agent or not sid:
        return
    row = conn.execute("SELECT session_id, state FROM agents WHERE name=?", (agent,)).fetchone()
    if row and (row["session_id"] != sid or row["state"] == "gone"):
        conn.execute("UPDATE agents SET session_id=?, state='busy', updated=? WHERE name=?", (sid, time.time(), agent))


def now() -> float:
    return time.time()


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def ago(ts: float) -> str:
    if not ts:
        return "never"
    s = int(now() - ts)
    if s < 90:
        return f"{s} s ago"
    if s < 5400:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


def meta_get(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# Prompts aichord writes itself start with MARK, so they are never recorded as the user's.
MARK = "[aichord]"


def first_prompt(resumed: bool = False, versions_changed: str = "") -> str:
    """The prompt each tool starts with. After a tool update, the first agent also checks every wake-up path live."""
    start = "Your tool was restarted and this conversation resumed." if resumed else "Your session has started."
    if not versions_changed:
        return f"{MARK} {start} Follow the session-start note; if nothing needs you, reply in one line."
    return (f"{MARK} {start} Follow the session-start note. Tool versions changed at this start ({versions_changed}): "
            "when `aichord status` shows the others idle, check every wake-up path, yours included: run aichord ask "
            "--no-wait --subject \"live check after an update\" \"Reply with one word: ok.\" and end your turn. The "
            "replies should wake you; then report in one line who answered.")


FIRST_PROMPT = first_prompt()
# Prompts the tool writes itself. Claude Code delivers finished background commands, subagent reports and messages
# that arrive on its socket (aichord's wake-ups among them) as prompts.
TOOL_FRAMES = ("<task-notification>", "<agent-message", "<system-reminder", "<cross-session-message",
               "<teammate-message", "<channel")


def not_from_user(text: str) -> bool:
    return text.lstrip().startswith((MARK, *TOOL_FRAMES))


def task_call(text: str) -> str:
    """The tool call that started the background command or helper whose finish this Claude Code notice reports."""
    m = re.match(r"\s*<task-notification>.*?<tool-use-id>([^<\s]+)</tool-use-id>", text, re.S)
    return m.group(1) if m else ""


def is_slash_command(text: str) -> bool:
    """A tool command such as /compact or /model. A message that starts with a path, like /Users/..., is the user's."""
    words = text.split(maxsplit=1)
    return bool(words) and re.fullmatch(r"/[A-Za-z][\w:.-]*", words[0]) is not None


def agents(conn, live_only: bool = False) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM agents ORDER BY launched").fetchall()
    return [r for r in rows if r["state"] != "gone"] if live_only else rows


def agent_row(conn, name: str):
    return conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()


def same_launch(name: str) -> tuple[str, list]:
    """A condition for writes an agent's own hooks and checkers make: once `aichord restart` has started its tool
    again, the replaced tool's hooks and checkers, finishing late, write nothing."""
    mine = os.environ.get("AICHORD_LAUNCHED")
    return (" AND launched=?", [float(mine)]) if mine and name == os.environ.get("AICHORD_AGENT") else ("", [])


def set_state(conn, name: str, state: str, detail: str | None = None, **extra) -> None:
    if name == "user":
        return
    sets, vals = ["state=?", "updated=?"], [state, now()]
    if detail is not None:
        sets.append("detail=?")
        vals.append(detail)
    for k, v in extra.items():
        sets.append(f"{k}=?")
        vals.append(v)
    guard, guard_vals = same_launch(name)
    conn.execute(f"UPDATE agents SET {', '.join(sets)} WHERE name=?{guard}", [*vals, name, *guard_vals])


# --------------------------------------------------------------------------------------------
# Posting messages
# --------------------------------------------------------------------------------------------


def post(conn, sender: str, to: list[str], deliver: dict[str, str], kind: str, body: str,
         subject: str = "", thread: int | None = None, ref: str = "", asked_for: str = "") -> int:
    """Store a message and wake its idle recipients. `asked_for` is whose work a question is for (the sender's
    turn when it asked), so the replies go on with that work (work_of)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        mid, sender, deliver = store(conn, sender, to, deliver, kind, body, subject, thread, ref, asked_for)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    append_log(conn, mid)
    # Stored first; then each idle recipient is woken once. A busy one sees it between its steps or before it
    # stops (its Stop hook marks it idle before it looks for mail, so one of the two always finds it).
    for who in [w for w, action in deliver.items() if action in ACTIONS_THAT_WAKE and w != sender]:
        a = agent_row(conn, who)
        if a is not None and a["state"] == "idle":
            wake(conn, a)
    return mid


def store(conn, sender: str, to: list[str], deliver: dict[str, str], kind: str, body: str, subject: str = "",
          thread: int | None = None, ref: str = "", asked_for: str = "") -> tuple[int, str, dict[str, str]]:
    """Inside the caller's transaction: the message row and its deliveries. Returns its number and the names used."""
    mid = conn.execute("INSERT INTO messages(ts,sender,recipients,kind,thread,subject,body,ref) VALUES(?,?,?,?,?,?,?,?)",
                       (now(), sender, ",".join(to), kind, thread, subject, body, ref)).lastrowid
    if thread is None:
        conn.execute("UPDATE messages SET thread=? WHERE id=?", (mid, mid))
    if asked_for:
        meta_set(conn, f"asked_for:{mid}", asked_for)
    # Only to the user and to agents still on the team: one that left since its name was looked up owes nothing
    members = {r[0] for r in conn.execute("SELECT name FROM agents")} | {"user"}
    for who, action in deliver.items():
        if who != sender and who in members:
            conn.execute("INSERT OR IGNORE INTO deliveries(message,recipient,action) VALUES(?,?,?)", (mid, who, action))
    return mid, sender, deliver


def append_log(conn, mid: int) -> None:
    m = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    path = state_dir() / "log.md"
    head = f"### #{m['id']} · {datetime.fromtimestamp(m['ts']).strftime('%Y-%m-%d %H:%M:%S')} · {m['sender']} → {m['recipients'] or 'team'} · {m['kind']}"
    if m["thread"] != m["id"]:
        head += f" · thread #{m['thread']}"
    lines = [head]
    if m["subject"]:
        lines.append(f"**{m['subject']}**")
    if m["ref"]:
        lines.append(f"ref: `{m['ref']}`")
    lines += ["", m["body"].rstrip(), "", ""]
    with open(path, "a") as f:
        f.write("\n".join(lines))


def live_names(conn, exclude: str | None = None) -> list[str]:
    return [r["name"] for r in agents(conn, live_only=True) if r["name"] != exclude]


def resolve_to(conn, raw: str | None, sender: str) -> list[str]:
    """The recipients: everyone by default; agent names; a tool's name (codex) for every agent in that tool, unless an
    agent has that very name (a team started before agents were numbered)."""
    rows = agents(conn)
    known = {r["name"] for r in rows} | {"user"}
    if not raw or raw in ("all", "team", "others"):
        # Everyone registered, including agents whose tool has exited: the message waits for their next start.
        names = [r["name"] for r in rows if r["name"] != sender]
        if not names:
            sys.exit("aichord: no other agents are registered in this project yet (start them with `aichord up`).")
        return names
    names, unknown = [], []
    for n in dict.fromkeys(n.strip() for n in raw.split(",") if n.strip()):
        group = [n] if n in known else [r["name"] for r in rows if r["tool"] == n] if n in TOOL_LABEL else []
        names += group
        unknown += [] if group else [n]
    if unknown:
        sys.exit(f"aichord: unknown agent(s): {', '.join(unknown)}. Known: {', '.join(sorted(known))}, or a tool's name "
                 "for every agent in it.")
    return [n for n in dict.fromkeys(names) if n != sender]


def writers(conn) -> list[str]:
    """The agents that write code: every agent in Claude Code; on a team without one, every agent in Codex; else in
    Antigravity. By tool, so a relaunch never moves the job."""
    rows = agents(conn)
    return next(([r["name"] for r in rows if r["tool"] == t] for t in WRITER_TOOLS if any(r["tool"] == t for r in rows)), [])


def role_of(conn, name: str, tool: str) -> str:
    """An agent's job on this team: its tool's, and writing the code for the agents that write it."""
    if tool == "claude" or name not in writers(conn):
        return ROLES.get(tool, "")
    return f"{ROLES['claude']}; also {ROLES[tool]}"


def names_line(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def stdin_has_text(idle: float = 0.2) -> bool:
    """Whether any text arrives on stdin within `idle` seconds; nothing is kept. For refusing text given twice."""
    try:
        if sys.stdin is None or sys.stdin.isatty() or not select.select([sys.stdin], [], [], idle)[0]:
            return False
        return bool(os.read(sys.stdin.fileno(), 65536).strip())
    except (OSError, ValueError):
        return False


def stdin_text(idle: float = 0.2, pause: float = 2.0) -> str | None:
    """The text on stdin (a heredoc, a file, a pipe), read to its end: "" for a terminal or a stdin that ends at once
    (/dev/null). None when it stays open with nothing on it for `idle` seconds: not a text, and not proof there is
    none, so a caller about to change something must refuse; an open pipe can't hold the command. Text that stops
    for `pause` seconds without ending is refused, never taken in part."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        fd, chunks = sys.stdin.fileno(), []
        while True:
            if not select.select([fd], [], [], pause if chunks else idle)[0]:
                if chunks:
                    sys.exit("aichord: the text on stdin stopped without ending, so nothing was saved. Give it as words "
                             "or in a heredoc.")
                return None
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks).decode(errors="replace").strip()
            chunks.append(chunk)
    except (OSError, ValueError):
        return ""


def read_body(args, names_of=None) -> str:
    """The message text: the words after the command, or else stdin. Words and stdin together are refused, because
    the words used to win and the piped text was dropped without a word, so messages went out carrying only
    recipient names, such as "agy codex", typed without --to. Given the project's connection (`names_of`), a text
    that is only agent names is refused too: a note or a question never is one, while a reply may be ("who should
    lead?")."""
    words = " ".join(getattr(args, "text", None) or []).strip()
    if words:
        if stdin_has_text():
            sys.exit("aichord: the text was given twice, as words after the command and on stdin, so nothing was sent. "
                     "Recipients go after --to (for example --to agy,codex); give the text one way.")
        text = words
    else:
        text = "" if sys.stdin.isatty() else sys.stdin.read().strip()
    if not text:
        sys.exit("aichord: empty message. Pass text as arguments or on stdin (a heredoc works best for multi-line text).")
    if names_of is not None:
        names = {"user", "all", "team", "others", *TOOL_LABEL}
        names |= {r["name"] for r in agents(names_of)}
        if set(re.split(r"[\s,]+", text.lower())) - {""} <= names:
            sys.exit(f"aichord: the whole text is agent names ({text}), so nothing was sent. Recipients go after --to; "
                     "the text comes after them or on stdin.")
    return text


# --------------------------------------------------------------------------------------------
# Plain-language check
# --------------------------------------------------------------------------------------------

COMMON_TERMS = set("""
OK API APIs CLI JSON SQL SQLite HTTP HTTPS URL URLs PR PRs CI UI UX HTML CSS JS TS ID IDs SHA EVM RPC TODO MCP
SSH TCP UDP IO CPU GPU RAM OS USD EUR UTC PDF CSV YAML TOML MD ERC EIP AMM APY APR LP ETH BTC USDC USDT NFT
DAO README AWS LLM AI ML GB MB KB TB ms CEST CET PST EST NB FAQ VS CLAUDE AGY CODEX AGENTS GEMINI SKILL STATE
MODEL VERDICT CONFIDENCE ANSWER WHY EVIDENCE CRUX CHECKED NOT PASS BLOCK NITS WITH NONE ASK NOTE FINDINGS EOF
x86 arm64 utf8 sha256 v1 v2 v3 H1 H2 3D 2D
""".split())
TOKEN_RE = re.compile(r"(?<![\w/.\-#@])([A-Z][A-Z0-9]{1,7}s?|[A-Za-z]{1,3}\d{1,3}|[A-Z]/[A-Z])(?![\w\-/])")
SKIP_RE = re.compile(r"```.*?```|`[^`\n]*`|https?://\S+|\b0x[0-9a-fA-F]+\b|(?:[\w.\-]+/)+[\w.\-]+|\b[0-9a-f]{7,40}\b", re.S)


def glossary_terms() -> set[str]:
    path = state_dir() / "glossary.md"
    terms: set[str] = set()
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r"\s*[-*]\s*\**([^*:—–]+?)\**\s*[:—–-]", line)
            if m:
                terms.add(m.group(1).strip())
    return terms


def undefined_terms(text: str) -> list[str]:
    cleaned = SKIP_RE.sub(" ", text)
    known = COMMON_TERMS | glossary_terms()
    found: list[str] = []
    for tok in TOKEN_RE.findall(cleaned):
        base = tok[:-1] if tok.endswith("s") and tok[:-1].isupper() else tok
        if tok in known or base in known or tok in found or re.fullmatch(r"D\d+", tok):  # D4 is decision 4
            continue
        found.append(tok)
    return found


def plain_language_warning(text: str) -> None:
    terms = undefined_terms(text)
    if terms:
        print(f"Plain-language check: these terms aren't in .aichord/glossary.md: {', '.join(terms[:12])}. "
              f"Spell them out next time, or define one: aichord glossary add TERM \"meaning\". (Message sent anyway.)",
              file=sys.stderr)


# --------------------------------------------------------------------------------------------
# Showing messages
# --------------------------------------------------------------------------------------------


VERDICTS = ("PASS WITH NITS", "PASS", "BLOCK")


def is_review(m) -> bool:
    """A review request: `aichord review`, or a question whose subject starts with "review" (the way to ask for one in
    a project without git, or of something outside the working tree: a branch, a commit range, a note). Of 435
    replies to `aichord review` requests 433 gave a valid verdict; every invalid one (25, such as "REVISE" and
    "approve") and every BLOCK without a "Must fix" (24) answered a review sent as a question, whose hint asked for an
    ANSWER; 28 of those 49 questions had a subject starting with "review"."""
    return m["kind"] == "review" or (m["kind"] == "ask" and re.match(r"\W*review\b", m["subject"] or "", re.I) is not None)


def verdict_problem(body: str, required: bool) -> str:
    """Why a reply to a review request isn't sent as it is, or "". Its VERDICT line must give one of the three
    verdicts, and a BLOCK needs a finding that starts with "Must fix" (after FINDINGS, a number or Markdown; the words
    in a sentence such as "no must-fix findings", or in a NOT CHECKED line, don't count; all 144 past BLOCK replies to
    `aichord review` have one): one review went nine rounds on "VERDICT: REVISE" with unmarked findings, and passed
    once the reviewer had to say which of them had to be fixed. `required` (an `aichord review`
    request): a reply without a VERDICT line is refused too, so a deferral or a question back, sent as a note, leaves
    the review open. A question that only starts with "review" may be answered without one."""
    line = re.search(r"^\W*VERDICT\W*?:\W*(.*)$", body, re.M | re.I)  # 35 of 965 past verdict lines wrote "Verdict:"
    if not line:
        return ("A reply to a review request gives its verdict: a line VERDICT: PASS, PASS WITH NITS or BLOCK. If you "
                "aren't giving one yet (a question back, a deferral), send a note instead (aichord send --wake --to "
                "NAME ...): the review stays open.") if required else ""
    value = line.group(1).strip()
    verdict = next((v for v in VERDICTS if re.match(re.escape(v) + r"\b", value, re.I)), "")
    if not verdict:
        return (f"A review's verdict is PASS, PASS WITH NITS or BLOCK; yours says: {clip(value, 60) or '(nothing)'}. BLOCK "
                "means at least one finding breaks behaviour, loses data or is unsafe, and names it with \"Must fix:\"; "
                "anything else is a \"Suggestion:\" under PASS WITH NITS.")
    findings = re.split(r"(?m)^\W*(?:NOT\s+)?CHECKED\b", body, maxsplit=1)[0]  # the part before CHECKED and NOT CHECKED
    if verdict == "BLOCK" and not re.search(r"(?im)^\W*(?:FINDINGS\W*)?(?:\d+\W+)?must[ -]fix\b", findings):
        return ("A BLOCK names at least one finding that starts with \"Must fix:\" (it breaks behaviour, loses data or is "
                "unsafe), among its FINDINGS. With suggestions only, the verdict is PASS WITH NITS.")
    return ""


def reply_hint(m) -> str:
    """One line: the command and the reply's parts, so each message stays short."""
    if is_review(m):
        return (f"Reply with: aichord reply {m['id']} (VERDICT: PASS, PASS WITH NITS or BLOCK; FINDINGS marked 'Must fix:' "
                "or 'Suggestion:', with file:line, at most 3 suggestions; CHECKED; NOT CHECKED)")
    return f"Reply with: aichord reply {m['id']} (ANSWER; WHY; EVIDENCE: path:line you opened; NOT CHECKED)"


def describe(m: sqlite3.Row, action: str, viewer: str, full: bool) -> str:
    body = m["body"].rstrip()
    clipped = not full and len(body) > INLINE_LIMIT
    if clipped:
        body = body[:INLINE_LIMIT].rstrip() + f"\n… (shortened; full text: aichord read {m['id']})"
    when = hhmm(m["ts"])
    if m["kind"] == "user":
        if viewer in (m["recipients"] or "").split(","):
            return f"The user → you ({when}), #{m['id']}:\n{body}"
        return f"The user → {m['recipients']} (#{m['id']}, {when}), for your awareness:\n{body}"
    if m["kind"] == "answer":  # filed by time: what the agent said after the user's latest message to it
        return f"{m['sender']} to the user ({when}), #{m['id']}, after the user's #{m['thread']}:\n{body}"
    if m["kind"] == "decision":
        return f"Decision recorded by {m['sender']} ({when}): {body}"
    if m["kind"] == "reply":
        return f"#{m['id']} reply from {m['sender']} ({when}) in thread #{m['thread']}:\n{body}"
    label = {"ask": "question", "code": "coding task", "review": "review request", "note": "note"}.get(m["kind"], m["kind"])
    head = f"#{m['id']} {label} from {m['sender']} ({when})"
    if m["subject"]:
        head += f": {m['subject']}"
    if m["ref"]:
        head += f"\nref: {m['ref']}"
    text = f"{head}\n{body}"
    if action == "reply":
        text += "\n" + reply_hint(m)
    return text


def work_of(conn, msgs) -> str:
    """Whose work a turn that shows these messages goes on with. Replies to the agent's own questions go on with the
    work each question was asked for (recorded when it was sent; the user's wins), even when a peer's turn came in
    between; "" keeps the agent's current work (a question asked before that was recorded). Anything else is a
    peer's turn ("aichord"), whose final text isn't an answer to the user."""
    replies = [m for m in msgs if m["kind"] == "reply"]
    if not replies:
        return "aichord"
    owners = {meta_get(conn, f"asked_for:{m['thread']}") for m in replies}
    owners -= {"", "?"}
    return "user" if "user" in owners else (owners.pop() if len(owners) == 1 else "")


def meta_ids(conn, key: str) -> list[int]:
    try:
        return [int(i) for i in json.loads(meta_get(conn, key) or "[]")]
    except (ValueError, TypeError):
        return []


def claim_unseen(conn, agent: str, actions: tuple[str, ...] | None = None, kinds: tuple[str, ...] | None = None,
                 threads: list[int] | None = None, peer_turn: str = "", launched: float | None = None,
                 reserve: str = "") -> list[tuple[sqlite3.Row, str]]:
    """Atomically take deliveries not yet shown to `agent`, so two hooks never show the same message twice. With
    `peer_turn`, they are taken only while the agent is idle in this launch, and taking them starts that turn on
    work_of(them): a wake-up, of the launch `launched` if given (a restart in between makes it take nothing). With
    `reserve` (the wake-up's id), they are taken only while no other wake-up of the agent waits, and the reservation
    is stored in the same step, so two senders never both wake it."""
    q = ("SELECT d.message, d.action, m.kind, m.thread FROM deliveries d JOIN messages m ON m.id=d.message "
         "WHERE d.recipient=? AND d.notified IS NULL")
    params: list = [agent]
    if actions:
        q += f" AND d.action IN ({','.join('?' * len(actions))})"
        params += list(actions)
    if kinds:
        q += f" AND m.kind IN ({','.join('?' * len(kinds))})"
        params += list(kinds)
    if threads:
        q += f" AND m.thread IN ({','.join('?' * len(threads))})"
        params += threads
    q += " ORDER BY d.message"
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = [] if reserve and meta_get(conn, f"wake_pending:{agent}") else conn.execute(q, params).fetchall()
        guard, guard_vals = same_launch(agent)
        if rows and guard and not conn.execute(f"SELECT 1 FROM agents WHERE name=?{guard}", (agent, *guard_vals)).fetchone():
            rows = []  # a replaced tool's hook, checker or command takes nothing
        if rows and peer_turn and not start_peer_turn(conn, agent, peer_turn, work_of(conn, rows), launched):
            rows = []  # a prompt, an exit or a restart got there first; that turn's own hooks show the mail
        t = now()
        for r in rows:
            conn.execute("UPDATE deliveries SET notified=? WHERE message=? AND recipient=?", (t, r["message"], agent))
        if rows and reserve:
            for key, value in (("wake_pending", reserve), ("last_wake", reserve), ("wake_read", ""),
                               ("wake_msgs", json.dumps([r["message"] for r in rows]))):
                meta_set(conn, f"{key}:{agent}", value)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    out = []
    for r in rows:
        m = conn.execute("SELECT * FROM messages WHERE id=?", (r["message"],)).fetchone()
        out.append((m, r["action"]))
    return out


def render(conn, agent: str, items: list[tuple[sqlite3.Row, str]], limit: int = CONTEXT_LIMIT,
           mark_read: bool = True, shown_in_full: list[int] | None = None) -> str:
    """The messages as the agent sees them. Those shown in full count as read, unless `mark_read` is False: a
    wake-up's text reaches the agent only when its turn starts, so their numbers go to `shown_in_full` and are
    marked read then (wake_started). Within `limit` UTF-8 bytes: once one doesn't fit, the rest are named in one line
    and stay unread, so `aichord inbox` shows them in full."""
    texts = [describe(m, action, agent, full=False) for m, action in items]

    def rest(k: int) -> str:  # names the messages from k on
        ids = [m["id"] for m, _ in items[k:]]
        named = ", ".join(f"#{i}" for i in ids[:8]) + (" …" if len(ids) > 8 else "")
        return f"{len(ids)} more not shown to save space ({named}): aichord inbox shows them in full."

    k, used = 0, 0  # as many as fit with the line naming the rest, measured exactly
    while k < len(items):
        if used + sum(nbytes(x) + 2 for x in texts[k:]) <= limit:  # all the rest fit: no line naming them needed
            k = len(items)
            break
        if used + nbytes(texts[k]) + 2 + nbytes(rest(k + 1)) > limit:
            break
        used += nbytes(texts[k]) + 2
        k += 1
    t = now()
    for m, _ in items[:k]:
        if len(m["body"]) <= INLINE_LIMIT:
            if mark_read:
                conn.execute("UPDATE deliveries SET read=COALESCE(read, ?) WHERE message=? AND recipient=?", (t, m["id"], agent))
            if shown_in_full is not None:
                shown_in_full.append(m["id"])
    return "\n\n".join(texts[:k] + ([rest(k)] if k < len(items) else []))


DIGEST_HEAD = "[aichord] New for you since your last step:\n\n"


def digest(conn, agent: str, limit: int = CONTEXT_LIMIT) -> str:
    items = claim_unseen(conn, agent)
    if not items:
        return ""
    return DIGEST_HEAD + render(conn, agent, items, limit=limit - nbytes(DIGEST_HEAD))


# --------------------------------------------------------------------------------------------
# Seat card (shown at session start and after compaction)
# --------------------------------------------------------------------------------------------


# What one hook may inject, in UTF-8 bytes. Claude Code passes at most 10,000 characters of a hook's context and
# Codex 0.169 at most 10,000 bytes; past that each gives the agent only a preview and a file path it isn't asked to
# read (code.claude.com/docs/en/hooks.md; Codex's source). Antigravity took 18,336 characters whole. A
# 17,621-character note once reached claude as a 2,000-character preview, and an idea of the user's was lost with it.
HOOK_BUDGET = {"claude": 9000, "codex": 9000, "agy": 24000}
MAIL_ROOM = 1500  # kept for new mail when the start note is shown
TRACK_MAX = 600   # characters in one research track; details go in a note it links to
TRACK_LABEL = {"focus": "in focus", "parked": "parked", "done": "done"}

# The strong model keeps the reasoning and hands bounded jobs to helpers, on the model each job needs: a small one for
# scouting, summaries and research, the strong one for tougher work.
HELPER_DUTY = ("Helpers: keep the judgement, the decisive check and your reply; hand bounded jobs to helpers without being "
               "asked, and pick each one's model for its job. ")


def helper_line(tool: str) -> str:
    """How this tool's agent uses its helpers, with the default model they run on."""
    model = helper_model(tool)
    if tool == "claude":
        runs = f"run on {model} by default" if model else "run on Claude Code's default model"
        return (HELPER_DUTY + f"Your subagents {runs}: right for scouting, summaries, collecting and research. For tougher "
                "work spawn a stronger model (for example model \"opus\").")
    if tool == "codex":
        return HELPER_DUTY + (f"Your spawn_agent subagents run on {model} by default." if model else
                              "Your spawn_agent subagents run on Codex's default model.")
    return HELPER_DUTY + ("Call invoke_subagent with Model \"flash\" (the cheap tier) for scouting, summaries, collecting "
                          "and research, and Model \"inherit\" (your own model) for tougher work; this version accepts "
                          "only inherit, flash, pro and flash_lite. Helpers can run commands, so their task must say what "
                          "they may not do.")

# A thread is finished when nobody still owes it a reply and, for the user's turns, the lead has answered or someone
# closed it. Only threads that start with a request count; notes and decisions are not threads to finish.
FINISHED = """m.id = m.thread AND m.kind IN ('user', 'ask', 'code', 'review')
    AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.message = m.id AND d.action = 'reply' AND d.replied IS NULL)
    AND (m.kind != 'user' OR COALESCE(t.closed, '') != ''
         OR EXISTS (SELECT 1 FROM messages a WHERE a.thread = m.id AND a.kind = 'answer'))"""


def nbytes(text: str) -> int:
    return len(text.encode())


def clip(text: str, n: int, full: str = "") -> str:
    """One line of at most about n characters; `full` says where the rest is."""
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    return text[:n].rstrip() + "…" + (f" ({full})" if full else "")


def fit(sections: list[tuple], budget: int) -> str:
    """Sections in priority order, each (heading, lines, cap, where[, total]): as many whole lines as fit in the
    section's cap (0: none of its own) and in the room left, then "(K more: where)". Every non-empty section keeps room
    for one line saying what it held, so none disappears unannounced. `total` counts lines that were never
    fetched."""
    secs = [(h, lines, cap, where, rest[0] if rest else len(lines)) for h, lines, cap, where, *rest in sections if lines]

    def gone(head: str, total: int, where: str) -> str:
        return f"{head.rstrip(':')}: {total} not shown ({where})"

    floor = [nbytes(gone(h, total, w)) + 1 for h, _, _, w, total in secs]
    out, left = [], budget
    for i, (head, lines, cap, where, total) in enumerate(secs):
        room = max(min(cap, left - sum(floor[i + 1:])) if cap else left - sum(floor[i + 1:]), floor[i])
        block, size = [head] if head else [], nbytes(head) + 1 if head else 0
        for line in lines:
            if size + nbytes(line) + 1 > room:
                break
            block.append(line)
            size += nbytes(line) + 1
        shown = len(block) - bool(head)
        while shown < total:  # say what was left out, making room for it if needed
            more = f"  ({total - shown} more: {where})"
            if size + nbytes(more) + 1 <= room:
                block.append(more)
                size += nbytes(more) + 1
                break
            if shown == 0:
                block, size = [gone(head, total, where)], floor[i]
                break
            size -= nbytes(block.pop()) + 1
            shown -= 1
        out += block
        left -= size
    return "\n".join(out)


def thread_line(t) -> str:
    who = f"{t['sender']} → {t['recipients'] or 'team'}"
    gist = t["subject"] or t["body"]
    line = f"#{t['id']} {who}: {clip(gist, 120)}"
    if t["closed"]:
        line += f" (closed by {clip(t['closed'], 60)})"
    return line


def stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def track_lines(conn) -> list[str]:
    """The research tracks in focus, then the parked ones, newest first; done ones by name only."""
    rows = conn.execute("SELECT * FROM tracks ORDER BY status != 'focus', updated DESC").fetchall()
    lines = [f"  - {r['name']} [{TRACK_LABEL.get(r['status'], r['status'])}, rev {r['rev']}, {r['by']}, "
             f"{ago(r['updated'])}]: {' '.join(r['body'].split())}" for r in rows if r["status"] != "done"]
    done = [r["name"] for r in rows if r["status"] == "done"]
    return lines + ([f"  - done: {clip(', '.join(done), 200)}"] if done else [])


def user_lines(conn) -> tuple[str, list[str], int]:
    """The user's messages after the project's review mark, newest first, and at least the latest five; with how many
    there are in all (only the newest 200 are fetched)."""
    mark = int(meta_get(conn, "reviewed_through") or 0)
    which = ("FROM messages WHERE kind = 'user' AND (id > ? OR id IN (SELECT id FROM messages WHERE kind = 'user' "
             "ORDER BY id DESC LIMIT 5))")
    rows = conn.execute(f"SELECT * {which} ORDER BY id DESC LIMIT 200", (mark,)).fetchall()
    total = conn.execute(f"SELECT COUNT(*) {which}", (mark,)).fetchone()[0]
    head = (f"The user's messages since the tracks were last reviewed (through #{mark}), newest first:" if mark else
            "The user's messages, newest first (the lead marks them reviewed with aichord track --reviewed N):")
    return head, [f"  - #{m['id']} {stamp(m['ts'])} to {m['recipients']}: " + clip(m["body"], 300, f"full: aichord read {m['id']}")
                  for m in rows], total


def decision_lines(conn) -> list[str]:
    return [f"  - D{d['id']}: {clip(d['text'], 300, 'full text in .aichord/decisions.md')}" for d in conn.execute(
        "SELECT * FROM decisions WHERE superseded_by IS NULL ORDER BY id DESC")]


DECISIONS_WHERE = ("older decisions in force are in .aichord/decisions.md; read them before acting on anything they may "
                   "cover, and retire those that no longer matter: aichord decide --retire D4,D7 \"why\"")


def memory_sections(conn, budget: int) -> list[tuple[str, list[str], int, str]]:
    """What carries the big picture through a compaction: the research tracks, the user's own words since they were
    last reviewed, and the decisions in force."""
    head, said, total = user_lines(conn)
    tracks = track_lines(conn) or ["  (none yet: when the user opens, settles, parks or switches a topic, the lead "
                                   "writes one with aichord track NAME)"]
    return [("Research tracks: the user's threads of work and their intent in each (aichord track NAME shows one):",
             tracks, budget * 2 // 5, "aichord track lists them all"),
            (head, said, budget // 5, "aichord recall <word>, or .aichord/log.md", total),
            ("Decisions in force (newest first):", decision_lines(conn), budget // 6, DECISIONS_WHERE)]


GROUND_MSG = 1200  # one message in the recent discussion after a compaction or launch
GROUND_WHY = {
    "compacted": "Your conversation was just compacted: what you remember of this project is now a summary, and it may "
                 "be wrong about the code. If the summary says files are loaded in memory or that no more tool calls "
                 "are needed, that is false: what you read before is gone (Antigravity issue 1294).",
    "launched": "You have just started in a new conversation: you remember nothing of this project yet.",
}


def grounding(conn, agent: str, limit: int, skip: set[int] = frozenset()) -> str:
    """After a compaction or a launch: the team's latest messages, newest kept first, shown oldest first, in what is
    left of the hook's budget. Antigravity once answered from a compacted summary that held none of the facts it was
    asked about, so the agent is pointed back at the log and the code."""
    head = "The team's recent discussion, oldest first (everything older: aichord recall <word>, or .aichord/log.md):"
    shown, used = [], nbytes(head) + 2
    # Mail not yet shown to the agent comes in full with the digest (`skip`), so it isn't repeated here.
    for m in conn.execute("SELECT * FROM messages m WHERE NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.message = m.id "
                          "AND d.recipient = ? AND d.notified IS NULL) ORDER BY id DESC LIMIT 200", (agent,)):
        if m["id"] in skip:
            continue
        body = m["body"].strip()
        if len(body) > GROUND_MSG:
            body = body[:GROUND_MSG].rstrip() + f"… (full: aichord read {m['id']})"
        where = f", thread #{m['thread']}" if m["thread"] and m["thread"] != m["id"] else ""
        subject = f": {m['subject']}" if m["subject"] else ""
        text = f"#{m['id']} {stamp(m['ts'])} {m['sender']} → {m['recipients'] or 'team'} ({m['kind']}{where}){subject}\n{body}"
        if used + nbytes(text) + 2 > limit:
            break
        shown.append(text)
        used += nbytes(text) + 2
    return head + "\n\n" + "\n\n".join(reversed(shown)) if shown else ""


MODEL_NOTE = ("An agent's name says nothing about its model: check your tool's model setting (/model, or its "
              "settings file) before saying which model you run.")


def seat_card(conn, agent: str, budget: int | None = None, why: str = "") -> str:
    """The start note: who the agent is and the rules, open work, the big picture (research tracks, the user's words,
    decisions), then recent finished threads, all within `budget` bytes, most important first."""
    row = agent_row(conn, agent)
    tool = row["tool"] if row else TEAM.get(agent, "?")
    budget = budget or HOOK_BUDGET.get(tool, HOOK_BUDGET["claude"]) - MAIL_ROOM
    me_line = [f"[aichord] You are {agent}, running in {TOOL_LABEL.get(tool, tool)}, in an aichord session on {project_root().name}."]
    if why:
        me_line.append(f"[aichord] {GROUND_WHY.get(why, GROUND_WHY['compacted'])} Rebuild the picture from the research "
                       "tracks and the user's messages below, and from the files.")
    members = agents(conn)
    team = [f"  - {r['name']} ({TOOL_LABEL.get(r['tool'], r['tool'])})"
            + (f": {role}" if (role := role_of(conn, r["name"], r["tool"])) else "")
            + (" (not running now)" if r["state"] == "gone" else "") for r in members]
    peers = sum(r["name"] != agent for r in members)
    consult = ("you are the only agent on this team, so there is nobody to consult: answer the user yourself." if not peers
               else "whoever the user types to leads that turn, and asks " + (
                   "the other agent" if peers == 1 else "both others" if peers == 2 else f"the {peers} others")
               + " before answering anything that isn't trivial, unless the user says \"solo\".")
    lines = [*me_line, "Team:", *team,
             f"Rules: {consult} Read the aichord skill: {SKILL_DIR / 'SKILL.md'}. "
             "The user's messages to the others reach you as context; act only on what concerns your role. Plain full "
             "sentences, no undefined abbreviations; long content goes in .aichord/notes/. A claim about the code or the "
             "project's current state rests on a file you opened in this turn, cited as path:line; mark anything else "
             "\"from memory, not checked\". " + MODEL_NOTE]
    coders = writers(conn)
    if tool in TOOL_LABEL:
        edit_line = (f"only {coders[0]}'s helpers edit" if len(coders) == 1 else
                     f"only the helpers of {names_line(coders)} edit" if coders else "no helper edits")
        lines.append(helper_line(tool) + f" Helpers never run aichord commands, and {edit_line} project files; check "
                     "their decisive claims yourself.")
    rules = own_rules()
    if rules:
        lines.append(rules)
    if tool == "agy":
        lines.append("Wake-up: start `aichord watch` now as a background command. It ends, and wakes you, when new mail "
                     "arrives; then run `aichord inbox` and start the watch again.")
    work = [f"  - #{m['id']} from {m['sender']} waits for your reply: {clip(m['subject'] or m['body'], 100)}"
            for m in conn.execute("SELECT m.* FROM deliveries d JOIN messages m ON m.id = d.message WHERE d.recipient = ? "
                                  "AND d.action = 'reply' AND d.replied IS NULL ORDER BY m.id", (agent,))]
    asked: dict[int, list[str]] = {}
    for a in open_asks(conn, agent):
        asked.setdefault(a["message"], []).append(a["recipient"])
    for mid, who in asked.items():
        first = conn.execute("SELECT subject, body FROM messages WHERE id = ?", (mid,)).fetchone()
        work.append(f"  - your #{mid} still waits for {', '.join(who)}: {clip(first[0] or first[1], 80)} "
                    f"(if it no longer matters: aichord close {mid} \"why\")")
    finished = f"FROM messages m LEFT JOIN threads t ON t.id = m.id WHERE {FINISHED} AND m.kind != 'user'"  # the user's are above
    threads = ["  - " + thread_line(t) for t in conn.execute(f"SELECT m.*, t.closed {finished} ORDER BY m.id DESC LIMIT 60")]
    thread_total = conn.execute(f"SELECT COUNT(*) {finished}").fetchone()[0]
    tail = "Older history: aichord recall <word>, aichord thread N, or .aichord/log.md (every message in full)."
    return fit([("", lines, 0, ""), ("Open work:", work, budget // 8, "aichord status and aichord inbox"),
                *memory_sections(conn, budget),
                ("Recent finished threads (newest first):", threads, budget // 8, "aichord recall <word>", thread_total)],
               budget - nbytes(tail) - 1) + "\n" + tail


def compaction_brief(conn, budget: int = 9000) -> str:
    """Claude Code uses what a PreCompact hook prints as instructions for the compaction summary (hooks.md, "PreCompact
    output", at most 10,000 characters). Asking it to keep the big picture word for word keeps it in the summary too,
    not only in the note after it."""
    head = ("When you summarize this conversation, keep these word for word, with their message numbers: the research "
            "tracks and the user's messages below, and the decisions in force. Keep what the user asked for apart from "
            "results and proposals, and say which track is in focus.")
    return head + "\n\n" + fit(memory_sections(conn, budget), budget - nbytes(head) - 2)


# --------------------------------------------------------------------------------------------
# Commands for agents and the user
# --------------------------------------------------------------------------------------------


def cmd_ask(args, kind: str = "ask") -> None:
    conn, sender = db(), me()
    if kind == "code" and not args.to:
        coders = writers(conn)
        if len(coders) != 1:
            sys.exit("aichord code: " + (f"several agents write code here ({names_line(coders)}): name one with --to, "
                                         "and give each task its own files." if coders else "no agent is registered yet."))
        args.to = coders[0]
    to = resolve_to(conn, args.to, sender)
    body = read_body(args, names_of=conn)
    ref = getattr(args, "ref", "") or ""
    if kind == "review":
        snap = review_snapshot(conn, getattr(args, "base", None), getattr(args, "include", None))
        ref = snap["ref"]
        body = snap["text"] + "\n\nSummary from " + sender + ":\n" + body
    plain_language_warning(body)
    row = agent_row(conn, sender)
    mid = post(conn, sender, to, {r: "reply" for r in to}, kind, body, subject=args.subject or "", ref=ref,
               asked_for=(row["turn_from"] or "") if row is not None else "")
    print(receipt(conn, f"Sent #{mid} to {', '.join(to)}.", to))
    rounds = re.search(r"\bround\s+(\d+)", args.subject or "", re.I)
    if rounds and int(rounds.group(1)) >= 3 and is_review({"kind": kind, "subject": args.subject or ""}):
        # 43 review requests in two weeks were for round 3 or later, and two chains ran 10 rounds
        print(f"Round {rounds.group(1)} of this review: tell the user now which finding is still open, and let them "
              "choose: fix it, accept the risk, or stop here.")
    asleep = [n for n in to if (a := agent_row(conn, n)) is not None and a["state"] == "idle" and a["tool"] != "agy"]
    if in_sandbox() and asleep and not args.no_wait:
        # Waking them takes this agent's next hook, which runs once this command has ended; waiting here would only
        # spend the time limit.
        print(f"From inside Codex's sandbox {', '.join(asleep)} can't be woken until this command ends; your next "
              f"hook wakes {'them' if len(asleep) > 1 else 'it'}. Run aichord wait {mid} next.")
        return
    if sender == "user" or args.no_wait:
        print("Replies will appear in `aichord inbox` and `aichord tail`." if sender == "user" else
              f"Not waiting. Replies reach you as context, or run: aichord wait {mid}")
        return
    wait_loop(conn, sender, threads=[mid], max_seconds=args.wait)


def cmd_send(args) -> None:
    conn, sender = db(), me()
    to = resolve_to(conn, args.to, sender)
    body = read_body(args, names_of=conn)
    plain_language_warning(body)
    # A note waits for the recipient's next step; only --wake starts an idle agent's turn for it. An informational
    # note to everyone used to wake claude through its socket, each time with Claude Code's long notice about
    # messages from other sessions.
    action = "read" if args.wake else "context"
    mid = post(conn, sender, to, {r: action for r in to}, "note", body, subject=args.subject or "")
    if args.wake:
        print(receipt(conn, f"Sent #{mid} to {', '.join(to)}, waking each idle one.", to))
    else:  # said plainly, so a note meant to be urgent isn't left waiting unnoticed
        print(receipt(conn, f"Stored note #{mid} for {', '.join(to)}: it waits for each one's next step. If it can't "
                            "wait, send it again with --wake.", to, quiet=True))


def cmd_reply(args) -> None:
    conn, sender = db(), me()
    orig = conn.execute("SELECT * FROM messages WHERE id=?", (args.id,)).fetchone()
    if not orig:
        sys.exit(f"aichord: there is no message #{args.id}.")
    body = read_body(args)
    problem = verdict_problem(body, required=orig["kind"] == "review") if is_review(orig) else ""
    if problem:
        sys.exit(f"aichord: nothing was sent. {problem}")
    plain_language_warning(body)
    target = orig["sender"]
    mid = post(conn, sender, [target], {target: "read"}, "reply", body,
               subject=f"re #{orig['id']}" + (f": {orig['subject']}" if orig["subject"] else ""), thread=orig["thread"])
    t = now()
    conn.execute("UPDATE deliveries SET replied=?, read=COALESCE(read, ?), notified=COALESCE(notified, ?) "
                 "WHERE message=? AND recipient=?", (t, t, t, orig["id"], sender))
    print(receipt(conn, f"Sent reply #{mid} to {target} (thread #{orig['thread']}).", [target]))


def presence(conn, name: str, quiet: bool = False, owes: bool = False) -> str:
    """What was last observed about an agent. It never promises when the agent will see a message. For a quiet note
    no wake-up path is shown: nothing is sent to wake anyone. `owes`: the caller waits for this agent's reply, so a
    long idle counts as looking stuck too (stuck_note)."""
    a = agent_row(conn, name)
    if a is None:
        return "user: reads it with aichord inbox or in the log tab" if name == "user" else f"{name}: not registered"
    if a["state"] == "gone":
        return f"{name}: {a['detail'] or 'its tool has exited'}; the message waits for its next start"
    text = f"{name}: {a['state']}{', ' + a['detail'] if a['detail'] else ''} (updated {ago(a['updated'])})"
    if woken_stalled(conn, a):
        text += f"; stalled: typing anything in {name}'s tab starts its turn"
    text += f"; wake-up path: {wake_path(a, conn)}" if a["state"] == "idle" and not quiet else ""
    return text + stuck_note(a, owes, overdue=owes)


def receipt(conn, first_line: str, to: list[str], quiet: bool = False) -> str:
    """The message is stored; this adds what is known about each recipient right now."""
    mark_exited(conn)
    return "\n".join([first_line] + ["  " + presence(conn, n, quiet) for n in to])


def looks_stuck(a, owes: bool = False) -> bool:
    """Whether an agent has given no sign of life for too long: still starting after STARTING_LONG (a prompt only the
    user can answer, a resume that failed), or busy with no hook for QUIET_LONG (a revoked login, a usage limit).
    With `owes`, when its reply is being waited for, being idle that long counts too."""
    quiet = now() - (a["updated"] or 0)
    if a["state"] == "starting":
        return quiet > STARTING_LONG
    return (a["state"] == "busy" or (owes and a["state"] == "idle")) and quiet > QUIET_LONG


def tab_shows(a) -> list[str]:
    """The last lines on an agent's tmux tab, for when nobody is looking at it. Reading only: nothing is typed.
    Lines without a letter or digit (blank ones, box borders) are left out. [] outside tmux, where tmux can't be
    asked (Codex's sandbox), or when the recorded pane is no longer this project's."""
    if not os.environ.get("TMUX") or not a["pane"]:
        return []
    pane = tmux_panes().get(a["pane"])
    if pane is None or pane["dead"] == "1" or not in_project(pane, project_root()):
        return []
    lines = [clip(x, 150) for x in tmux_out("capture-pane", "-p", "-t", a["pane"]).splitlines()]
    return [x for x in lines if re.search(r"\w", x)][-TAB_LINES:]


def stuck_note(a, owes: bool = False, overdue: bool = False) -> str:
    """Lines to go under what is said about an agent, with what its tab shows: for one that looks stuck (`owes`: it
    owes a reply, so a long idle counts), and, with `overdue` (a wait for its reply has just run out), for one that
    has been quiet for OVERDUE_QUIET. "" for any other. A blocked agent used to be invisible: two sat at a "trust
    this folder?" prompt for 24 hours listed as "starting", and one whose login was revoked was waited on for 3.6
    hours until the user asked. The lines are the tab's own text, which goes into the reader's conversation; the
    agents already read each other's tabs by hand when one seems stuck (SKILL.md)."""
    stuck = looks_stuck(a, owes)
    if not stuck and not (overdue and a["state"] != "gone" and now() - (a["updated"] or 0) >= OVERDUE_QUIET):
        return ""
    where = f"pane {a['pane']}" if a["pane"] else "no pane recorded"
    shown = tab_shows(a)
    if not shown:
        return (f"\n    {a['name']} may be stuck, and its tab ({where}) can't be read from here: have it looked at."
                if stuck else "")
    advice = ("If that is a prompt, a sign-in error or a usage limit, it is waiting for you in that tab." if me() == "user"
              else "If that is a prompt only the user can answer, a sign-in error or a usage limit, tell the user now, "
                   "and answer with what you have.")
    return (f"\n    {a['name']} may be stuck. Its tab" if stuck else f"\n    {a['name']}'s tab") + (
        f" ({where}) shows:\n" + "\n".join(f"      | {x}" for x in shown) + f"\n    {advice}")


def open_asks(conn, agent: str, threads: list[int] | None = None) -> list[sqlite3.Row]:
    q = ("SELECT d.message, d.recipient, m.thread, m.subject, m.ts FROM deliveries d JOIN messages m ON m.id=d.message "
         "WHERE m.sender=? AND d.action='reply' AND d.replied IS NULL")
    params: list = [agent]
    if threads:
        q += f" AND m.thread IN ({','.join('?' * len(threads))})"
        params += threads
    return conn.execute(q + " ORDER BY d.message", params).fetchall()


def has_exited(conn, name: str) -> bool:
    a = agent_row(conn, name)
    return a is not None and a["state"] == "gone"


def wait_loop(conn, agent: str, threads: list[int] | None, max_seconds: int) -> None:
    """Wait for replies to `agent`'s open questions. Returns early if a new question for `agent` arrives, or when
    everyone still owing a reply has exited (their questions stay open)."""
    max_seconds = max(5, min(max_seconds, 540))
    deadline = now() + max_seconds
    waiting_on = open_asks(conn, agent, threads)
    if not waiting_on:
        shown = claim_unseen(conn, agent, kinds=("reply",), threads=threads)
        if shown:
            print(render(conn, agent, shown, limit=10**9))
        else:
            print("Nothing to wait for: you have no unanswered questions" + (f" in thread(s) {threads}." if threads else "."))
        return
    set_state(conn, agent, "waiting", detail="for replies to " + ", ".join(f"#{w['message']}" for w in waiting_on))
    try:
        while True:
            replies = claim_unseen(conn, agent, kinds=("reply",))
            if replies:
                print(render(conn, agent, replies, limit=10**9))
                print()
            interrupts = claim_unseen(conn, agent, actions=("reply",))
            if interrupts:
                print("[aichord] A new question for you arrived while you were waiting. Answer it first, then run "
                      "`aichord wait` again:\n\n" + render(conn, agent, interrupts, limit=10**9))
                return
            tend(conn)  # also retries a failed wake-up of the agents it waits on
            remaining = open_asks(conn, agent, threads)
            if not remaining:
                print("[aichord] All replies are in.")
                return
            missing = sorted({r["recipient"] for r in remaining})
            if all(has_exited(conn, n) for n in missing):
                print(f"[aichord] Stopped waiting for {', '.join(missing)}: not running any more. The questions stay "
                      "open and are shown to them when they start again.")
                return
            if now() >= deadline:
                print("[aichord] Still waiting on: " + "\n  ".join(presence(conn, n, owes=True) for n in missing))
                ids = sorted({r["thread"] for r in remaining})
                mine = agent_row(conn, agent)
                print(f"Run `aichord wait {' '.join(map(str, ids))}` to keep waiting, or carry on and the replies "
                      "will reach you as context." + (
                          " If you end your turn now, tell the user in one line what you are waiting for; your answer "
                          "comes when the replies are in." if mine is not None and mine["turn_from"] == "user" else ""))
                return
            time.sleep(1.0)
    finally:
        set_state(conn, agent, "busy", detail="")


def cmd_wait(args) -> None:
    conn = db(create=False)
    wait_loop(conn, me(), args.threads or None, args.max)


def cmd_inbox(args) -> None:
    """Unread messages in full (context last), then every question already read but not answered yet."""
    conn, agent = db(create=False), me()
    rows = conn.execute(
        "SELECT d.action, d.read AS seen, m.* FROM deliveries d JOIN messages m ON m.id=d.message WHERE d.recipient=? "
        "AND (d.read IS NULL OR (d.action='reply' AND d.replied IS NULL)) ORDER BY m.id", (agent,)).fetchall()
    unread = sorted((r for r in rows if r["seen"] is None), key=lambda r: r["action"] == "context")
    owed = [r for r in rows if r["seen"] is not None]
    out, t = [], now()
    for r in unread:
        out.append(describe(r, r["action"], agent, full=True))
        conn.execute("UPDATE deliveries SET read=?, notified=COALESCE(notified, ?) WHERE message=? AND recipient=?",
                     (t, t, r["id"], agent))
    if owed:
        out.append("Questions you have read but not answered yet (see one again with aichord read N):\n" + "\n".join(
            f"  #{r['id']} from {r['sender']}: {clip(r['subject'] or r['body'], 100)} (answer: aichord reply {r['id']})"
            for r in owed))
    print("\n\n".join(out) if out else "No unread aichord messages, and no question waits for your reply.")


def cmd_read(args) -> None:
    conn, agent = db(create=False), me()
    for mid in args.ids:
        m = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not m:
            print(f"There is no message #{mid}.")
            continue
        d = conn.execute("SELECT action FROM deliveries WHERE message=? AND recipient=?", (mid, agent)).fetchone()
        print(describe(m, d["action"] if d else "read", agent, full=True))
        print()
        t = now()
        conn.execute("UPDATE deliveries SET read=?, notified=COALESCE(notified, ?) WHERE message=? AND recipient=?",
                     (t, t, mid, agent))


def cmd_thread(args) -> None:
    conn = db(create=False)
    rows = conn.execute("SELECT * FROM messages WHERE thread=? ORDER BY id", (args.id,)).fetchall()
    if not rows:
        sys.exit(f"aichord: no thread #{args.id}.")
    for m in rows:
        print(f"--- #{m['id']} {hhmm(m['ts'])} {m['sender']} → {m['recipients']} ({m['kind']})")
        print(m["body"].rstrip())
        print()
    # One of several messages the user sent before one answer: that answer is filed after the latest of them
    after = conn.execute("SELECT * FROM messages WHERE id=?", (int(meta_get(conn, f"next_answer:{args.id}") or 0),)).fetchone()
    if after:
        print(f"--- a later answer, filed after the user's #{after['thread']}: #{after['id']} "
              f"{hhmm(after['ts'])} {after['sender']} → {after['recipients']} (answer)")
        print(after["body"].rstrip())
        print()


def render_decisions(conn) -> None:
    rows = conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
    lines = ["# Decisions", "", "Numbered decisions for this project. Written by `aichord decide`; don't edit by hand.", ""]
    for d in rows:
        stamp = datetime.fromtimestamp(d["ts"]).strftime("%Y-%m-%d %H:%M")
        src = f", thread #{d['thread']}" if d["thread"] else ""
        lines.append(f"- **D{d['id']}** ({stamp}, {d['by']}{src}): {d['text']}{decision_end(d, '*')}")
    (state_dir() / "decisions.md").write_text("\n".join(lines) + "\n")


def decision_end(d, mark: str = "") -> str:
    """What became of a decision no longer in force: replaced by a newer one, or retired with a reason."""
    if d["superseded_by"]:
        return f" {mark}(replaced by D{d['superseded_by']}){mark}"
    if d["superseded_by"] is not None:
        return f" {mark}(retired: {d['retired'] or 'no reason given'}){mark}"
    return ""


def gone_decision(r) -> str:
    """Why a decision can't be replaced or retired again, or "" while it is in force."""
    if r["superseded_by"]:
        return f"was already replaced by D{r['superseded_by']}."
    return "was already retired." if r["superseded_by"] is not None else ""


def decision_ids(text: str) -> list[int]:
    try:
        return [int(x.strip().lstrip("Dd")) for x in (text or "").split(",") if x.strip()]
    except ValueError:
        sys.exit(f"aichord: decisions are listed by number, for example D3,D5 (got {text!r}).")


def cmd_decide(args) -> None:
    conn, agent = db(), me()
    if args.retire:
        why = " ".join((" ".join(args.text or []) or stdin_text() or "").split())
        return retire_decisions(conn, agent, decision_ids(args.retire), why, args.thread)
    text = re.sub(r"^\**D\d+\**\s*[:.\-–—]\s*", "", read_body(args))  # the number is added below; "D18: D18: ..." no more
    if not text:
        sys.exit("aichord: empty decision.")
    plain_language_warning(text)
    replaced = decision_ids(args.supersedes)
    conn.execute("BEGIN IMMEDIATE")  # two agents replacing the same decision at once: the second is refused
    try:
        for old in replaced:
            r = conn.execute("SELECT superseded_by FROM decisions WHERE id = ?", (old,)).fetchone()
            if r is None or gone_decision(r):
                sys.exit(f"aichord: D{old} " + ("doesn't exist." if r is None else gone_decision(r)))
        did = conn.execute("INSERT INTO decisions(ts, by, thread, text) VALUES(?, ?, ?, ?)",
                           (now(), agent, args.thread, text)).lastrowid
        for old in replaced:
            conn.execute("UPDATE decisions SET superseded_by = ? WHERE id = ?", (did, old))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    render_decisions(conn)
    note = f"D{did}: {text}" + (f" (replaces {', '.join(f'D{o}' for o in replaced)})" if replaced else "")
    post(conn, agent, ["team"], {n: "context" for n in live_names(conn, exclude=agent)}, "decision", note,
         thread=args.thread)
    print(f"Recorded D{did}. It is in .aichord/decisions.md and will be shown to the others.")


def retire_decisions(conn, agent: str, ids: list[int], why: str, thread: int | None) -> None:
    """Take decisions out of force without a new one in their place: a finished one-off task, or a rule the skill
    already states. They stay in decisions.md, marked retired with the reason, and leave the start note."""
    if not ids or not why:
        sys.exit("aichord: name the decisions and say why, for example: aichord decide --retire D4,D7 \"one-off tasks, done\".")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for i in ids:
            r = conn.execute("SELECT superseded_by FROM decisions WHERE id = ?", (i,)).fetchone()
            if r is None or gone_decision(r):
                sys.exit(f"aichord: D{i} " + ("doesn't exist." if r is None else gone_decision(r)) + " Nothing was retired.")
        conn.executemany("UPDATE decisions SET superseded_by = 0, retired = ? WHERE id = ?", [(why, i) for i in ids])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    render_decisions(conn)
    names = ", ".join(f"D{i}" for i in ids)
    post(conn, agent, ["team"], {n: "context" for n in live_names(conn, exclude=agent)}, "note",
         f"Retired {names}: {why}", subject=f"retired {names}", thread=thread)
    print(f"Retired {names}. They stay in .aichord/decisions.md, marked retired, and leave the start note.")


def cmd_close(args) -> None:
    """Finish a thread nobody needs to answer any more, with a one-line reason."""
    conn, agent = db(create=False), me()
    root = conn.execute("SELECT * FROM messages WHERE id = ?", (args.id,)).fetchone()
    if not root or root["thread"] != root["id"]:
        sys.exit(f"aichord: #{args.id} doesn't start a thread." + (f" Its thread is #{root['thread']}." if root else ""))
    text = " ".join(read_body(args).split())
    t = now()
    conn.execute("UPDATE deliveries SET replied = ?, notified = COALESCE(notified, ?) WHERE action = 'reply' "
                 "AND replied IS NULL AND message IN (SELECT id FROM messages WHERE thread = ?)", (t, t, args.id))
    conn.execute("INSERT INTO threads(id, closed) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET closed = excluded.closed",
                 (args.id, f"{agent}: {text}"))
    post(conn, agent, ["team"], {n: "context" for n in live_names(conn, exclude=agent)}, "note",
         f"Closed thread #{args.id}: {text}", subject=f"closed #{args.id}", thread=args.id)
    print(f"Closed thread #{args.id}.")


def render_tracks(conn) -> None:
    lines = ["# Research tracks", "", "The user's threads of work and their intent in each. Written by "
             "`aichord track`; don't edit by hand.", ""]
    for r in conn.execute("SELECT * FROM tracks ORDER BY status != 'focus', status = 'done', updated DESC"):
        lines.append(f"- **{r['name']}** ({TRACK_LABEL.get(r['status'], r['status'])}, rev {r['rev']}, {r['by']}, "
                     f"{stamp(r['updated'])}): {r['body']}")
    mark = meta_get(conn, "reviewed_through")
    lines += ["", f"The user's messages reviewed through #{mark}." if mark else "No review mark yet."]
    (state_dir() / "tracks.md").write_text("\n".join(lines) + "\n")


def cmd_track(args) -> None:
    """The research tracks: one short card per thread of work, in the user's words with the number of each message
    it rests on; in focus, parked or done. A write names the revision it was based on, and one based on an older
    revision is refused with the current card, so a stale draft never replaces a newer one."""
    conn, agent = db(), me()
    if args.reviewed is not None:
        newest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        if not 0 < args.reviewed <= newest:
            sys.exit(f"aichord track: --reviewed takes a message number up to #{newest}.")
        meta_set(conn, "reviewed_through", str(args.reviewed))
        render_tracks(conn)
        print(f"The user's messages are marked reviewed through #{args.reviewed}; later ones stay listed under the tracks.")
        return
    if not args.name:
        print(fit([("Research tracks:", track_lines(conn), 0, "")], 10 ** 6) or "No research tracks yet.")
        mark = meta_get(conn, "reviewed_through")
        print(f"The user's messages are reviewed through #{mark}." if mark else "No review mark yet: aichord track --reviewed N.")
        return
    name = args.name.lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name):
        sys.exit("aichord track: a track's name is a short lowercase word or words joined by hyphens, like rpf-controller.")
    status = "focus" if args.focus else "parked" if args.park else "done" if args.done else ""
    words = " ".join(args.text or []).strip()
    if words and stdin_has_text():
        sys.exit("aichord track: the text was given twice, as words and on stdin, so nothing was saved. Give it one way.")
    piped = None if words else stdin_text()
    text = words or piped or ""
    row = conn.execute("SELECT * FROM tracks WHERE name = ?", (name,)).fetchone()
    card = (lambda r: f"{r['name']} [{TRACK_LABEL.get(r['status'], r['status'])}, rev {r['rev']}, {r['by']}, "
                      f"{ago(r['updated'])}]\n{r['body']}")
    if not text and not status:
        if not row:
            sys.exit(f"aichord track: there is no track {name} yet.")
        print(card(row))
        return
    if piped is None and not words:  # a change while stdin is open with nothing on it yet: its text may still come
        sys.exit("aichord track: stdin is open with no text on it yet, so nothing was changed. Give the text as words or "
                 "in a heredoc; for a status change alone, close stdin: < /dev/null.")
    if not row and not text:
        sys.exit(f"aichord track: {name} is new, so it needs its text.")
    text = text or row["body"]
    if len(text) > TRACK_MAX:
        sys.exit(f"aichord track: a track holds at most {TRACK_MAX} characters ({len(text)} given). Keep the user's "
                 "intent here and put details in a note it names.")
    cited = [int(n) for n in re.findall(r"#(\d+)", text)]
    newest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
    if not cited or min(cited) < 1 or max(cited) > newest:
        sys.exit("aichord track: cite the message each claim rests on, as #N of a message that exists (up to "
                 f"#{newest}); the user's words are the authority.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM tracks WHERE name = ?", (name,)).fetchone()
        rev = row["rev"] if row else 0
        if (args.rev or 0) != rev:
            conn.execute("ROLLBACK")
            sys.exit(f"aichord track: {name} is at rev {rev}, not {args.rev or 0}: it changed since you read it. Fold in "
                     f"the current card and write again with --rev {rev}:\n" + (card(row) if row else "(no card)"))
        saved = {"name": name, "status": status or (row["status"] if row else "focus"), "body": text, "rev": rev + 1,
                 "by": agent, "updated": now()}
        conn.execute("INSERT INTO tracks(name, status, body, rev, by, updated) VALUES(:name, :status, :body, :rev, :by, "
                     ":updated) ON CONFLICT(name) DO UPDATE SET status = excluded.status, body = excluded.body, rev = "
                     "excluded.rev, by = excluded.by, updated = excluded.updated", saved)
        # Every revision's record is stored with it, from the values written, never reread
        mid, _, _ = store(conn, agent, ["team"], {n: "context" for n in live_names(conn, exclude=agent)}, "note",
                          f"Track {card(saved)}", subject=f"track {name}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    append_log(conn, mid)
    render_tracks(conn)
    print(f"Saved track {name}, rev {saved['rev']} ({TRACK_LABEL[saved['status']]}).")


RECALL_CHARS = 6000


def cmd_recall(args) -> None:
    """Search every decision and message, newest first."""
    conn = db(create=False)
    try:
        pat = re.compile(args.pattern, re.I)
    except re.error as e:
        sys.exit(f"aichord recall: not a valid pattern ({e}).")

    def snippet(text: str) -> str:
        text = " ".join(text.split())
        m = pat.search(text)
        lo = max(0, m.start() - 80) if m else 0
        return ("…" if lo else "") + text[lo:lo + 220] + ("…" if lo + 220 < len(text) else "")

    hits: list[tuple[float, str]] = []
    for d in conn.execute("SELECT * FROM decisions"):
        if pat.search(d["text"]):
            hits.append((d["ts"], f"D{d['id']} {datetime.fromtimestamp(d['ts']):%Y-%m-%d %H:%M} {d['by']}: "
                                  f"{snippet(d['text'])}{decision_end(d)}"))
    for m in conn.execute("SELECT * FROM messages WHERE kind != 'decision'"):  # decisions were searched above
        text = " ".join(x for x in (m["subject"], m["body"]) if x)
        if pat.search(text):
            hits.append((m["ts"], f"#{m['id']} {datetime.fromtimestamp(m['ts']):%Y-%m-%d %H:%M} {m['sender']} → "
                                  f"{m['recipients'] or 'team'} ({m['kind']}): {snippet(text)}"))
    hits.sort(key=lambda h: -h[0])
    shown, size = [], 0
    for _, line in hits[:args.limit]:
        if size + len(line) > RECALL_CHARS:
            break
        shown.append(line)
        size += len(line) + 1
    print("\n".join(shown) if shown else "No match.")
    if len(shown) < len(hits):
        print(f"({len(hits) - len(shown)} more matches. Narrow the pattern, or pass --limit. Full text: aichord read N.)")


def cmd_say(args) -> None:
    """The user posts to the team (or one agent) from any shell, for example the tail pane."""
    conn = db()
    to = resolve_to(conn, args.to, "user")
    body = read_body(args)
    mid = post(conn, "user", to, {r: "read" for r in to}, "user", body)
    print(receipt(conn, f"Posted #{mid} to {', '.join(to)}.", to))


def cmd_glossary(args) -> None:
    path = state_dir() / "glossary.md"
    db()  # make sure .aichord exists
    if not path.exists():
        path.write_text("# Glossary\n\nProject terms. One per line: `- TERM: meaning`. Agents use these instead of inventing labels.\n\n")
    if args.term:
        meaning = " ".join(args.meaning).strip()
        if not meaning:
            sys.exit("aichord: give a meaning, e.g. aichord glossary add RPF \"revenue participation fraction\"")
        with open(path, "a") as f:
            f.write(f"- {args.term}: {meaning}\n")
        print(f"Added {args.term} to .aichord/glossary.md.")
    else:
        print(path.read_text())


# --------------------------------------------------------------------------------------------
# Review snapshots
# --------------------------------------------------------------------------------------------


def git(*a: str, env: dict | None = None, check: bool = True) -> str:
    out = subprocess.run(["git", *a], cwd=project_root(), capture_output=True, text=True, env=env)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(a)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


SNAPSHOT_EXCLUDE = (".aichord/", "__pycache__/", ".pytest_cache/", "node_modules/", ".DS_Store", ".venv/")


def snapshot_excluded(path: str) -> bool:
    """Untracked files never worth reviewing: aichord's own files, caches, bytecode."""
    return path.endswith(".pyc") or any(path.startswith(x) or f"/{x}" in f"/{path}" for x in SNAPSHOT_EXCLUDE)


def review_snapshot(conn, base: str | None, include: list[str] | None) -> dict:
    """Record the working tree, new files included, as a commit that nothing else points to.

    Your files, your staging area and your branches are not touched. The commit is kept alive by a ref
    under refs/aichord/ so reviewers can read it with git show / git diff."""
    try:
        head = git("rev-parse", "HEAD")
    except RuntimeError:
        sys.exit("aichord review: this project has no commits yet; make a first commit so there is a base to compare with.")
    given_base = ""
    if base:
        try:
            given_base = git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
        except RuntimeError:
            given_base = ""
        if not given_base:
            sys.exit(f"aichord review: {base!r} isn't a commit in this repository, so nothing was sent.")
    gitdir = Path(git("rev-parse", "--absolute-git-dir"))
    tmp_index = gitdir / f"aichord-index-{os.getpid()}"
    env = {**os.environ, "GIT_INDEX_FILE": str(tmp_index),
           "GIT_AUTHOR_NAME": "aichord", "GIT_AUTHOR_EMAIL": "aichord@localhost",
           "GIT_COMMITTER_NAME": "aichord", "GIT_COMMITTER_EMAIL": "aichord@localhost"}
    skipped: list[str] = []
    try:
        git("read-tree", head, env=env)
        git("add", "-u", env=env)  # changed and deleted tracked files
        untracked = [p for p in git("ls-files", "--others", "--exclude-standard", "-z", env=env).split("\0") if p]
        untracked = [p for p in untracked if not snapshot_excluded(p)]
        if include:
            untracked = [p for p in untracked if any(p == i or p.startswith(i.rstrip("/") + "/") for i in include)]
        root = project_root()
        keep, total = [], 0
        for p in untracked:
            try:
                size = (root / p).stat().st_size
            except OSError:
                continue
            if size > 5_000_000 or total + size > 50_000_000 or len(keep) >= 2000:
                skipped.append(p)
                continue
            keep.append(p)
            total += size
        for i in range(0, len(keep), 200):
            git("add", "--", *keep[i:i + 200], env=env)
        tree = git("write-tree", env=env)
        commit = git("commit-tree", tree, "-p", head, "--no-gpg-sign", "-m", "aichord review snapshot", env=env)
    except RuntimeError as e:
        if "Operation not permitted" in str(e) or "Permission denied" in str(e):
            sys.exit("aichord review: can't write to .git from this sandbox (Codex blocks it). Ask an agent in Claude "
                     "Code or Antigravity to request the review, or the user to run `aichord review` from a shell.")
        raise
    finally:
        tmp_index.unlink(missing_ok=True)
    # Number, ref and "previous snapshot" are published as one step: two agents asking for reviews at once never take
    # the same number, and the next one names the snapshot actually recorded before it
    publishing = take_lock(state_dir() / "runtime" / "review.lock", wait=60)  # noqa: F841 (held until published)
    if publishing is None:
        sys.exit("aichord review: another review request was still being recorded after 60 s, so nothing was sent.")
    n = int(meta_get(conn, "review_count", "0")) + 1
    prev, prev_n = meta_get(conn, "last_review_commit"), meta_get(conn, "last_review_number") or str(n - 1)
    ref = f"refs/aichord/review-{n}"
    git("update-ref", ref, commit)
    conn.execute("BEGIN IMMEDIATE")
    try:  # only a snapshot whose ref was kept becomes the next default base
        for key, value in (("review_count", str(n)), ("last_review_commit", commit), ("last_review_number", str(n))):
            meta_set(conn, key, value)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    publishing.close()
    if base:
        base_commit, base_desc = given_base, base
    elif prev:
        base_commit, base_desc = prev, f"the previous review snapshot (refs/aichord/review-{prev_n})"
    else:
        base_commit, base_desc = head, "HEAD (the last commit)"
    stat = git("diff", "--stat", base_commit, commit, check=False) or "(no changes compared with the base)"
    s, b = commit[:12], base_commit[:12]
    text = textwrap.dedent(f"""\
        Review snapshot {s} (kept as {ref}). Base: {base_desc}, {b}.
        These commands show exactly what is under review, even if files change meanwhile:
          git diff {b} {s} --stat
          git diff {b} {s}
          git show {s}:<path>
        To run tests on the exact snapshot (outside a sandbox): git worktree add --detach .aichord/review-{n} {s}
        Changed files:
        {textwrap.indent(stat, '  ')}""")
    if skipped:
        text += "\nNot included (too large or too many untracked files): " + ", ".join(skipped[:20])
    return {"ref": commit, "text": text}


# --------------------------------------------------------------------------------------------
# Status, log, tail
# --------------------------------------------------------------------------------------------


def cmd_status(args) -> None:
    conn = db(create=False)
    mark_exited(conn)
    tend(conn)
    rows = agents(conn)
    pend = conn.execute(
        "SELECT d.recipient, m.id, m.sender, m.kind, m.subject, m.ts FROM deliveries d JOIN messages m ON m.id=d.message "
        "WHERE d.action='reply' AND d.replied IS NULL ORDER BY m.id").fetchall()
    owing = {p["recipient"] for p in pend}
    print(f"Project: {project_root()}")
    if not rows:
        print("No agents registered. Start them with `aichord up` (inside tmux) or `aichord launch <name>` in a pane.")
    else:
        print("Agents:")
        width = max(7, *(len(r["name"]) for r in rows))
        for r in rows:
            tool = TOOL_LABEL.get(r["tool"], r["tool"])
            detail = f" — {r['detail']}" if r["detail"] else ""
            path = f" · wake-up path: {wake_path(r, conn)}" if r["state"] == "idle" else ""
            if woken_stalled(conn, r):
                path = f" · stalled: typing anything in {r['name']}'s tab starts its turn"
            print(f"  {r['name']:<{width}} {tool:<12} {r['state']:<8}{detail} · updated {ago(r['updated'])} · pane {r['pane'] or '?'}{path}"
                  + stuck_note(r, owes=r["name"] in owing))
    if pend:
        print("Waiting for replies:")
        for p in pend:
            print(f"  #{p['id']} {p['sender']} → {p['recipient']} ({p['kind']}{': ' + p['subject'] if p['subject'] else ''}), asked {ago(p['ts'])}")
    unshown = conn.execute(f"SELECT recipient, COUNT(*) n FROM deliveries WHERE {NEW_MAIL} GROUP BY recipient").fetchall()
    if unshown:
        print("Not yet delivered: " + ", ".join(f"{u['recipient']} ({u['n']})" for u in unshown))


def format_for_tail(m: sqlite3.Row) -> str:
    head = f"\033[1m#{m['id']} {hhmm(m['ts'])} {m['sender']} → {m['recipients'] or 'team'}\033[0m [{m['kind']}]"
    if m["subject"]:
        head += f" {m['subject']}"
    return head + "\n" + textwrap.indent(m["body"].rstrip(), "  ") + "\n"


def cmd_tail(args) -> None:
    conn = db(create=False)
    last = conn.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
    start = max(0, last - args.n)
    for m in conn.execute("SELECT * FROM messages WHERE id>? ORDER BY id", (start,)):
        print(format_for_tail(m))
    if args.no_follow or me() != "user":  # agents get a snapshot; following forever would hang their shell
        return
    try:
        while True:
            rows = conn.execute("SELECT * FROM messages WHERE id>? ORDER BY id", (last,)).fetchall()
            for m in rows:
                print(format_for_tail(m), flush=True)
                last = m["id"]
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------------------------
# Tool adapters: hooks, launching, waking
# --------------------------------------------------------------------------------------------


def aichord_cmd(python: str | None = None) -> str:
    """How hooks call this script: a uv-managed Python run directly, which also works inside Codex's sandbox."""
    return f"{shlex.quote(python or sys.executable)} -I {shlex.quote(str(SCRIPT))}"


HOOK_EVENTS = {
    # tool event name -> our event
    "claude": {"SessionStart": "start", "UserPromptSubmit": "prompt", "PostToolUse": "tool", "Stop": "stop",
               "SessionEnd": "end", "PreCompact": "precompact"},
    "codex": {"SessionStart": "start", "UserPromptSubmit": "prompt", "PostToolUse": "tool", "Stop": "stop",
              "PostCompact": "compacted", "SessionEnd": "end", "SubagentStart": "helper_start",
              "SubagentStop": "helper_stop"},
    # Antigravity drops the output of its after-tool and after-model-call hooks, so aichord doesn't use them; mail
    # reaches agy before its next model call instead. Events not listed here answer with nothing to do.
    "agy": {"SessionStart": "start", "PreInvocation": "agy_pre", "Stop": "stop"},
}


HOOK_LOCK_WAIT = 20  # seconds a hook waits for its seat's lock; restarts, launches and renames hold it far shorter


def hook_log(msg: str) -> None:
    try:
        with open(state_dir() / "runtime" / "hooks.log", "a") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def hook_main(tool: str, event: str) -> int:
    """Entry point for every hook. Does nothing outside sessions started by `aichord launch`."""
    global IN_HOOK
    IN_HOOK = True
    agent = os.environ.get("AICHORD_AGENT")
    quiet = "{}" if tool == "agy" else ""  # agy reports a failed hook when the output isn't JSON
    if not agent or not os.environ.get("AICHORD_ROOT"):
        quiet and print(quiet)
        return 0
    raw = "" if sys.stdin.isatty() else sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    try:
        return handle_hook(tool, HOOK_EVENTS.get(tool, {}).get(event, event), agent, data)
    except Exception as e:  # a broken hook must never break the agent's session
        hook_log(f"{agent} {tool} {event}: {type(e).__name__}: {e}")
        quiet and print(quiet)
        return 0


def session_of(tool: str, data: dict) -> str:
    for key in ("session_id", "conversation_id", "conversationId", "sessionId"):
        if data.get(key):
            return str(data[key])
    if tool == "agy":
        return os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "")
    return ""


def prompt_of(tool: str, data: dict) -> str:
    for key in ("prompt", "user_prompt", "userPrompt", "text", "message"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def last_answer_of(tool: str, data: dict) -> str:
    for key in ("last_assistant_message", "lastAssistantMessage", "response"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def emit(tool: str, kind: str, text: str) -> int:
    """Print hook output in the format each tool understands. Returns the exit code."""
    if tool == "agy":
        return emit_agy(kind, text)
    if text and kind == "stop":
        print(json.dumps({"decision": "block", "reason": text}))
    elif text:
        name = {"start": "SessionStart", "prompt": "UserPromptSubmit", "tool": "PostToolUse"}[kind]
        print(json.dumps({"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}}))
    return 0


def agent_lock(agent: str) -> Path:
    """Shared by the agent's running hooks; `aichord restart` takes it alone to hand the agent over (fence)."""
    return state_dir() / "runtime" / f"agent-{agent}.lock"


def handle_hook(tool: str, ev: str, agent: str, data: dict) -> int:
    conn = db()
    # A hook runs whole before a restart takes the agent over, or after it and then sees the new launch.
    hold = take_lock(agent_lock(agent), wait=HOOK_LOCK_WAIT, shared=True)  # noqa: F841 (held until exit)
    if hold is None:  # something has held the seat alone far longer than a restart, launch or rename takes: this
        hook_log(f"{agent} {tool} {ev}: the seat stayed locked for {HOOK_LOCK_WAIT} s, so this event was skipped")
        return emit(tool, "noop", "")  # event writes nothing, rather than write unfenced
    sid = session_of(tool, data)
    if os.environ.get("AICHORD_DEBUG") == "1":  # field names only, never contents
        extra = {k: data.get(k) for k in ("agent_id", "agent_type", "source") if k in data}
        hook_log(f"debug {agent} {tool} {ev} session={sid} keys={sorted(data)} {extra}")
    row = agent_row(conn, agent)
    if row is None and meta_get(conn, f"left:{agent}"):  # it left the team; a late hook of its old tool adds nothing
        return emit(tool, "noop", "")
    if row is None:  # launched before this project's database existed, or the database was reset
        conn.execute("INSERT OR IGNORE INTO agents(name,tool,pane,launched,updated) VALUES(?,?,?,?,?)",
                     (agent, tool, os.environ.get("TMUX_PANE", ""), float(os.environ.get("AICHORD_LAUNCHED") or now()), now()))
        row = agent_row(conn, agent)
    if older_launch(row):  # a hook of the tool this agent ran before `aichord restart`, finishing late
        return emit(tool, "noop", "")
    if ev in ("helper_start", "helper_stop"):
        return codex_helper(conn, tool, agent, row, ev, sid, data)

    # Only the agent's main session counts. Subagents and the tools' internal helper sessions (Codex runs memory
    # consolidation and auto-review as separate threads that inherit this environment) fire the same hooks.
    helper = any(data.get(k) for k in ("agent_id", "agent_type", "agentId", "agentType"))
    if tool == "codex" and "transcript_path" in data and not data.get("transcript_path"):
        helper = True  # Codex's internal threads have no transcript file
    main = row["session_id"]
    if helper or (main and sid and sid != main and not (ev == "start" and data.get("source") in ("clear", "resume"))):
        if tool == "codex" and ev == "tool" and data.get("agent_id"):
            return codex_helper(conn, tool, agent, row, ev, sid, data)
        if os.environ.get("AICHORD_DEBUG") == "1":
            hook_log(f"ignored {ev} from a helper or another session of {agent} (session {sid})")
        return emit(tool, "noop", "")
    if sid and sid != main:
        conn.execute("UPDATE agents SET session_id=? WHERE name=?", (sid, agent))
        meta_set(conn, f"seated:{agent}", "")
        row = agent_row(conn, agent)
    if tool == "claude":  # where aichord wakes this session: Claude Code (2.1.224 and later) tells every hook
        sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET", "")
        if sock and meta_get(conn, f"socket:{agent}") != sock:
            meta_set(conn, f"socket:{agent}", sock)
            meta_set(conn, f"socket_token:{agent}", os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN", ""))
        # and the settings folder this session reads, for the check that a wake-up would be delivered
        config = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
        if meta_get(conn, f"claude_config:{agent}") != config:
            meta_set(conn, f"claude_config:{agent}", config)

    def context(mids=(), seat: bool = True) -> str:
        """All one hook gives the agent, within its tool's budget, most important first: the start note when it
        hasn't been shown in this session (again after a compaction), the number of the user's message, new mail, and
        after a compaction or launch the recent discussion in whatever room is left."""
        left = HOOK_BUDGET.get(tool, HOOK_BUDGET["claude"])
        number = message_number(conn, agent, list(mids))
        card, why = "", ""
        if seat and meta_get(conn, f"seated:{agent}") != (sid or "1"):
            meta_set(conn, f"seated:{agent}", sid or "1")
            why = meta_get(conn, f"reground:{agent}")
            meta_set(conn, f"reground:{agent}", "")
            card = seat_card(conn, agent, left - nbytes(number) - MAIL_ROOM, why) + "\n\n"
        left -= nbytes(card) + nbytes(number)
        new = {r[0] for r in conn.execute("SELECT message FROM deliveries WHERE recipient = ? AND notified IS NULL", (agent,))}
        mail = digest(conn, agent, limit=min(left, CONTEXT_LIMIT))
        recent = grounding(conn, agent, left - nbytes(mail) - 4, skip=new) if why else ""
        return card + number + mail + ("\n\n" + recent if recent else "")

    def compacted() -> None:  # the start note and the recent discussion come again at the agent's next hook
        meta_set(conn, f"seated:{agent}", "")
        meta_set(conn, f"reground:{agent}", "compacted")

    if ev == "start":
        source = data.get("source", "")
        if source == "compact":
            compacted()
        elif source in ("clear", "resume"):
            meta_set(conn, f"seated:{agent}", "")
            if source == "clear":  # an emptied conversation needs the recent discussion too
                meta_set(conn, f"reground:{agent}", "launched")
        if tool == "claude" and source in ("startup", "", "resume", "clear"):
            set_state(conn, agent, "idle", detail="")
        tend(conn, skip=agent)  # this agent's own mail comes with the note below
        return emit(tool, "start", context())

    if ev == "compacted":
        compacted()
        return 0

    if ev == "end":
        set_state(conn, agent, "gone", detail="its session ended")
        return 0

    if ev == "prompt":
        mid = record_prompt(conn, agent, prompt_of(tool, data))
        return emit(tool, "prompt", context([mid]))

    if ev == "tool":
        name = data.get("tool_name") or data.get("toolName") or data.get("tool") or "a tool"
        set_state(conn, agent, "busy", detail=f"last used {name}")
        started = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        if tool == "claude" and data.get("tool_use_id") and (name in ("Agent", "Task") or started.get("run_in_background")):
            # A helper or background command started: whose work it is, so the turn its finish starts goes on with it
            owners = json.loads(meta_get(conn, f"task_owners:{agent}") or "{}")
            owners[str(data["tool_use_id"])] = row["turn_from"]
            meta_set(conn, f"task_owners:{agent}", json.dumps(dict(list(owners.items())[-50:])))
        text = context(seat=bool(meta_get(conn, f"reground:{agent}")))
        wake_idle(conn, skip=agent)  # what this agent's sandboxed command just stored and couldn't wake anyone for
        return emit(tool, "tool", text)

    if ev == "stop":
        return stop_hook(conn, tool, agent, row, data)

    if ev == "agy_pre":
        events = agy_new_events(conn, agent, data.get("transcriptPath", ""))
        if any(kind == "compacted" for kind, _ in events):  # Antigravity has no compaction hook
            compacted()
            events = [e for e in events if e[0] != "compacted"]  # not a finished task
        mids = agy_follow_turns(conn, agent, events)
        if not any(kind == "prompt" for kind, _ in events):
            set_state(conn, agent, "busy")
        return emit(tool, "prompt", context(mids))

    if ev == "precompact":  # Claude Code: what this prints steers the compaction summary (compaction_brief)
        print(compaction_brief(conn))
        return 0
    return emit(tool, "noop", "")


def stop_hook(conn, tool: str, agent: str, row, data: dict) -> int:
    """The end of a turn, in this order:
    0. In Claude Code, a wake-up whose own prompt never came is counted as lost (end_lost_wake).
    1. The answer to the user is recorded: the final text of a turn on the user's work, filed by time after the
       user's latest message to this agent (a timeline). A repeated text isn't saved twice. One given while the
       agent's own question is open isn't shown to the agents still answering it, and the user's messages that
       question came after stay on the agent's list of messages it hasn't finished answering (open_msgs).
    2. The agent is marked idle, and only then is mail looked for: a message stored meanwhile is found here or
       wakes the agent (post), never neither.
    3. New mail makes the turn carry on; so does a question it read in this launch and hasn't answered (once).
    4. agy is reminded once to start its watch if none runs."""
    if tool == "claude" and meta_get(conn, f"wake_pending:{agent}"):
        end_lost_wake(conn, agent)
    answer = last_answer_of(tool, data)
    if tool == "agy" and row["turn_from"] == "user":
        answer = agy_last_answer(data.get("transcriptPath", ""))
    skipped = meta_get(conn, f"skip_answer:{agent}")  # the turn ran a tool command such as /compact
    if skipped:
        meta_set(conn, f"skip_answer:{agent}", "")
    request = meta_get(conn, f"turn_msg:{agent}")
    said = f"{request}\n{answer}"  # the same text again counts as a repeat only after the same message of the user's
    if nothing_new(conn, agent, request, answer):
        skipped = "1"
    if not skipped and row["turn_from"] == "user" and request.isdigit() and answer \
            and said != meta_get(conn, f"turn_answer:{agent}"):
        # An answer given while the agent's own question is open is its first view, not its last word: 31 % of the
        # answers were such, and each was shown to the two agents still working on the question. A question counts
        # while it is fresh (AWAIT_FRESH), and for the user's messages that came before it: those stay on the list.
        # Every other message leaves the list with this answer, and its thread shows this answer as the one that
        # followed (cmd_thread). Nothing records which messages an answer covers: it is filed by time, and answers
        # cite their own message's number in about one case in ten.
        asks = [r for r in open_asks(conn, agent) if now() - r["ts"] < AWAIT_FRESH]
        opened = meta_ids(conn, f"open_msgs:{agent}")
        held = [i for i in opened if any(r["message"] > i for r in asks)]
        waiting_on = {r["recipient"] for r in asks if r["message"] > min(opened + [int(request)])}
        mid = post(conn, agent, ["user"], {n: "context" for n in live_names(conn, exclude=agent) if n not in waiting_on},
                   "answer", answer, thread=int(request))
        for i in opened:
            if i not in held and i != int(request):
                meta_set(conn, f"next_answer:{i}", str(mid))
        meta_set(conn, f"turn_answer:{agent}", said)
        meta_set(conn, f"open_msgs:{agent}", json.dumps(held))
    if tool == "codex" and row["turn_from"] == "user":
        # Results that arrive later from helpers belong to the user's work: a reused helper starts no SubagentStart and
        # may take no step, so its assignment is invisible here. The worst case is an extra line in the user's thread,
        # never a lost answer.
        meta_set(conn, f"helpers_owner:{agent}", "user")
    set_state(conn, agent, "idle", detail="")
    items = claim_unseen(conn, agent, actions=ACTIONS_THAT_WAKE)
    if items:
        # A peer's question shown now makes the rest of the turn the peer's; replies go on with the work their
        # questions were asked for; a note goes on with the work the turn was on.
        msgs = [m for m, _ in items]
        if any(m["kind"] == "reply" for m in msgs):
            owner = work_of(conn, msgs)
        else:  # a question needs a reply (action "reply"); a note doesn't
            owner = "aichord" if any(action == "reply" for _, action in items) else ""
        set_state(conn, agent, "busy", detail="handling aichord messages", **({"turn_from": owner} if owner else {}))
        return emit(tool, "stop", "[aichord] Before you stop, there are new messages for you:\n\n" + render(conn, agent, items))
    collected = claim_helpers(conn, agent) if tool == "codex" else None
    if collected is not None:  # a result arrived while it worked: collect it now, on the work it paused for them
        set_state(conn, agent, "busy", detail="collecting its helpers' results", **({"turn_from": collected} if collected else {}))
        meta_set(conn, f"collecting:{agent}", request)
        return emit(tool, "stop", COLLECT)
    owed = unanswered(conn, agent, row, tool, data)
    if owed:
        set_state(conn, agent, "busy", detail="answering a question it read")
        return emit(tool, "stop", owed)
    if tool == "agy" and not lock_held(checker_lock(agent)) and not meta_get(conn, f"reminded:{agent}"):
        meta_set(conn, f"reminded:{agent}", "1")
        set_state(conn, agent, "busy", detail="restarting aichord watch")
        return emit(tool, "stop", WATCH_AGAIN)
    tend(conn, skip=agent, retry_now=True)  # a wake-up that just failed in a sandboxed command is tried at once
    return emit(tool, "noop", "")


def unanswered(conn, agent: str, row, tool: str, data: dict) -> str:
    """A reminder, once per question, when the agent ends a turn after reading a question it hasn't answered (an
    agent once finished its research and stopped without replying). A question counts once it was shown in full in
    this launch (read), not merely announced; none while the agent's own helpers or background commands still run,
    because then it hasn't finished."""
    if tool == "claude" and data.get("background_tasks"):
        return ""
    if tool in ("agy", "codex") and helpers_running(conn, agent):
        return ""
    reminded = set(meta_ids(conn, f"reply_reminded:{agent}"))
    owed = conn.execute("SELECT m.id, m.sender FROM deliveries d JOIN messages m ON m.id = d.message "
                        "WHERE d.recipient = ? AND d.action = 'reply' AND d.replied IS NULL AND d.read >= ? "
                        "ORDER BY m.id", (agent, row["launched"] or 0)).fetchall()
    new = [m for m in owed if m["id"] not in reminded]
    if not new:
        return ""
    # Every question still owed has been reminded now; answered or closed ones drop out, so the list never needs a cap.
    meta_set(conn, f"reply_reminded:{agent}", json.dumps([m["id"] for m in owed]))
    which = ", ".join(f"#{m['id']} from {m['sender']}" for m in new)
    return (f"{MARK} Before you stop: you have read {which} but not answered. Answer with aichord reply N, or, if it "
            "needs no answer, close it with aichord close N \"why\". If you are still waiting for your own helpers, "
            "say so in one line and stop.")


def record_prompt(conn, agent: str, text: str) -> int | None:
    """Something arrived in the agent's input box: the user's own words, or a line the tools or aichord wrote.
    Each of the user's messages starts a thread of its own, and the agent's answers are filed after the latest one
    (Stop hook). Returns the number of the user's recorded message."""
    if text and not_from_user(text):
        # aichord's wake-ups, finished background commands and helper reports. A finished task goes on with the work
        # it was started for (recorded at its start), and so does the wake-up after Codex helpers finish; anything
        # else with the work the agent was on (a mail wake-up has already set that, from what it carries).
        owners = json.loads(meta_get(conn, f"task_owners:{agent}") or "{}")
        owner = owners.pop(task_call(text), "") if owners else ""
        if owner:
            meta_set(conn, f"task_owners:{agent}", json.dumps(owners))
        helpers = text.lstrip().startswith(HELPERS_DONE)
        accepted = wake_started(conn, agent, text) if WAKE_TEXT in text or helpers else False
        if accepted and helpers:  # the work the agent paused for its helpers, the user's first
            owner = meta_get(conn, f"helpers_owner:{agent}") or owner
            for key in ("helpers_owner", "helpers_pending"):
                meta_set(conn, f"{key}:{agent}", "")
            meta_set(conn, f"collecting:{agent}", meta_get(conn, f"turn_msg:{agent}"))
        set_state(conn, agent, "busy", detail="handling aichord messages", **({"turn_from": owner} if owner else {}))
        return None
    for key in ("helpers_owner", "helpers_pending", "collecting"):  # the user's turn sees waiting results itself
        meta_set(conn, f"{key}:{agent}", "")
    if not text or is_slash_command(text):
        # A tool command (/compact) isn't a request: what it prints isn't an answer, and the user's request stays open.
        meta_set(conn, f"skip_answer:{agent}", "1")
        set_state(conn, agent, "busy", detail="running a tool command")
        return None
    set_state(conn, agent, "busy", detail="working on the user's message", turn_from="user")
    meta_set(conn, f"skip_answer:{agent}", "")
    mid = post(conn, "user", [agent], {n: "context" for n in live_names(conn, exclude=agent)}, "user", text)
    meta_set(conn, f"turn_msg:{agent}", str(mid))
    # The user's messages this agent hasn't finished answering; an answer takes them off the list (stop_hook)
    meta_set(conn, f"open_msgs:{agent}", json.dumps((meta_ids(conn, f"open_msgs:{agent}") + [mid])[-50:]))
    return mid


HELPERS_DONE = f"{MARK} Your helpers have finished"  # how a helper wake-up's prompt starts
NOTHING_NEW = "nothing new"
COLLECT = (f"{HELPERS_DONE}. Collect their results (if one isn't in your conversation yet, wait for it with your "
           "wait tool) and carry on with the work you started them for. If they change nothing in what you have "
           f"already said, end your turn with exactly: {NOTHING_NEW}")


def nothing_new(conn, agent: str, request: str, answer: str) -> bool:
    """At a stop: whether the turn only collected helper results that changed nothing, for a message of the user's
    that already has this agent's answer. Such a text isn't recorded: closing lines like "Both helper reviews were
    incorporated; no helper work remains pending" once made about 170 extra answers in the user's threads. Only a
    collection aichord started counts (its stop, or its wake-up's prompt, marks it for that request), so the user's
    own request for those words is still recorded. The mark is used up here."""
    marked = meta_get(conn, f"collecting:{agent}")
    if not marked:
        return False
    meta_set(conn, f"collecting:{agent}", "")
    return (marked == request and request.isdigit() and answer.strip().strip(".!").strip().lower() == NOTHING_NEW
            and conn.execute("SELECT 1 FROM messages WHERE kind = 'answer' AND sender = ? AND thread = ?",
                             (agent, int(request))).fetchone() is not None)


def codex_helper(conn, tool: str, agent: str, row, ev: str, sid: str, data: dict) -> int:
    """A helper of this Codex agent started (SubagentStart), took a step (its own tool hook) or ended a turn
    (SubagentStop). Codex gives these hooks the agent's own session and the helper's agent_id, and hands a helper's
    result over without starting a turn (codex-rs 0.163.0, agent/control/completion.rs, trigger_turn false): codex
    once sat idle for two hours with its helpers' results. So aichord keeps, per agent, which helpers run (each
    step counts, because a reused helper starts no new SubagentStart) and whether a result may be waiting. While one
    runs, the forgotten-reply reminder waits. A waiting result is collected by the agent's own Stop if it is still
    working, or by a wake-up (helper_wake) if it is idle. Each update is one step, so two helpers finishing at once
    can't overwrite each other."""
    hid = str(data.get("agent_id") or "")
    if not hid or tool != "codex" or (row["session_id"] and sid and sid != row["session_id"]):
        return emit(tool, "noop", "")
    conn.execute("BEGIN IMMEDIATE")
    try:
        running = json.loads(meta_get(conn, f"agy_running:{agent}") or "{}")
        if ev == "helper_stop":
            running.pop(hid, None)
            meta_set(conn, f"helpers_pending:{agent}", "1")
        else:
            running[hid] = [now(), ""]
        meta_set(conn, f"agy_running:{agent}", json.dumps(running))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if ev == "helper_stop":
        helper_wake(conn, agent_row(conn, agent))
    return emit(tool, "noop", "")


def helpers_settled(conn, agent: str) -> bool:
    """Inside a transaction: whether a result of this agent's helpers may be waiting while none of them still runs. A
    helper unseen for HELPER_QUIET counts as finished: one that fails or is interrupted sends no SubagentStop."""
    running = json.loads(meta_get(conn, f"agy_running:{agent}") or "{}")
    fresh = {k: v for k, v in running.items() if now() - v[0] < HELPER_QUIET}
    if fresh != running:
        meta_set(conn, f"agy_running:{agent}", json.dumps(fresh))
        meta_set(conn, f"helpers_pending:{agent}", "1")
    return not fresh and bool(meta_get(conn, f"helpers_pending:{agent}"))


def claim_helpers(conn, agent: str) -> str | None:
    """At the agent's own Stop: take its helpers' waiting result for collection in this turn, in one step, unless a
    wake-up is reserved (its own prompt collects it then). Returns the paused work's owner
    ("" for none), or None when there is nothing to collect here."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        owner = None
        if not meta_get(conn, f"wake_pending:{agent}") and helpers_settled(conn, agent):
            owner = meta_get(conn, f"helpers_owner:{agent}")
            for key in ("helpers_pending", "helpers_owner"):
                meta_set(conn, f"{key}:{agent}", "")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return owner


def helper_wake(conn, a, retry_now: bool = False) -> None:
    """Wake an idle Codex agent whose helpers have all finished, once, under the same reservation as a mail wake-up:
    one queued prompt at most, tagged, given back if it fails and then tried again by a later aichord command. Its
    turn goes on with the work the agent paused for them (helpers_owner, the user's first)."""
    if a is None or a["tool"] != "codex" or a["state"] != "idle" or not a["session_id"] or in_sandbox():
        return
    name = a["name"]
    if not meta_get(conn, f"helpers_pending:{name}") and not meta_get(conn, f"agy_running:{name}").strip("{}"):
        return
    last = float(meta_get(conn, f"last_wake:{name}") or 0)
    if meta_get(conn, f"wake_error:{name}") and not retry_now and now() - last < WAKE_RETRY:
        return
    hold = take_lock(agent_lock(name), shared=True)
    if hold is None:
        return
    try:
        attempt = repr(now())
        conn.execute("BEGIN IMMEDIATE")
        try:
            claimed = not meta_get(conn, f"wake_pending:{name}") and helpers_settled(conn, name) and conn.execute(
                "UPDATE agents SET state='busy', detail=?, updated=? WHERE name=? AND state='idle' AND launched=?",
                (WOKEN, now(), name, a["launched"])).rowcount == 1
            if claimed:
                for key, value in (("wake_pending", attempt), ("last_wake", attempt), ("wake_msgs", ""), ("wake_read", "")):
                    meta_set(conn, f"{key}:{name}", value)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if not claimed:
            return
        error = codex_queue(conn, a, COLLECT.replace(HELPERS_DONE, f"{HELPERS_DONE} (wake-up {attempt}):", 1))
    finally:
        hold.close()
    meta_set(conn, f"wake_error:{name}", error)
    if error:
        conn.execute("BEGIN IMMEDIATE")  # put back only what this attempt still holds, as wake() does
        try:
            if meta_get(conn, f"wake_pending:{name}") == attempt:
                give_back(conn, name)
                conn.execute("UPDATE agents SET state='idle', detail='' WHERE name=? AND state='busy' AND detail=? "
                             "AND launched=?", (name, WOKEN, a["launched"]))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    wake_log(f"waking {name} after its helpers finished failed: {error}" if error else
             f"woke {name} via codex queue: its helpers have finished")


def message_number(conn, agent: str, mids: list[int | None]) -> str:
    """Tells the agent the number of the user's message, so it cites the right one, and the latest OPEN_MSGS earlier
    ones it hasn't finished answering (open_msgs: no answer yet, or only one given while its own question was open).
    A quarter of the user's messages come within 90 seconds of the one before; told only the latest number, an agent
    once answered the first of two, and the user typed the second again four hours later."""
    mids = [m for m in mids if m]
    if not mids:
        return ""
    earlier = [i for i in meta_ids(conn, f"open_msgs:{agent}") if i < mids[-1]][-OPEN_MSGS:]
    also = (f" Earlier messages of the user's that you haven't finished answering: {', '.join(f'#{i}' for i in earlier)}. "
            "Your answer covers each one that still stands, and cites its number.") if earlier else ""
    return f"{MARK} This is the user's message #{mids[-1]}.{also}\n\n"


def older_launch(row) -> bool:
    """True in a hook or checker started by an earlier launch of this agent: its tool has been replaced since."""
    mine = os.environ.get("AICHORD_LAUNCHED")
    return bool(mine) and row is not None and float(mine) != row["launched"]


def new_mail(conn, agent: str) -> bool:
    return conn.execute(f"SELECT 1 FROM deliveries WHERE recipient=? AND {NEW_MAIL} LIMIT 1", (agent,)).fetchone() is not None


def unseen_mail(conn, agent: str) -> list[sqlite3.Row]:
    """The new mail that would wake the agent, with what work_of needs."""
    return conn.execute("SELECT m.kind, m.thread FROM deliveries d JOIN messages m ON m.id = d.message "
                        f"WHERE d.recipient = ? AND {NEW_MAIL}", (agent,)).fetchall()


def start_peer_turn(conn, agent: str, detail: str, owner: str = "aichord", launched: float | None = None) -> bool:
    """Mark the agent busy with a turn aichord starts, on `owner`'s work ("" keeps the agent's current work), only if
    it is still idle in this launch (the given one, or for its own hooks the one they belong to): a prompt, an exit
    or a restart that got there first keeps its own state and turn."""
    guard, guard_vals = same_launch(agent)
    if launched is not None:
        guard, guard_vals = guard + " AND launched=?", [*guard_vals, launched]
    return conn.execute(f"UPDATE agents SET state='busy', detail=?, updated=?, turn_from=COALESCE(NULLIF(?, ''), "
                        f"turn_from) WHERE name=? AND state='idle'{guard}",
                        (detail, now(), owner, agent, *guard_vals)).rowcount == 1


# ---- waking idle agents ---------------------------------------------------------------------

WAKE_TEXT = f"{MARK} New messages for you"  # how a wake-up's prompt starts; its turn's prompt hook looks for it
WAKE_TAG = re.compile(r"\(wake-up ([0-9.]+)\):")  # which wake-up the prompt is: its reservation


def wake(conn, a, retry_now: bool = False) -> None:
    """Start a turn in an idle agent that has new mail, by its tool's own path, and carry the mail in it.
    - The mail is taken, and a reservation stored, in one step, for this launch of the agent only: no hook shows the
      mail twice, and no second wake-up is sent until this one's own prompt starts the turn (wake_started), whatever
      other hooks do meanwhile. New mail waits for that turn's hooks. A
      reservation that outlives WAKE_STALLED is reported, never repeated on time alone: Codex may first spend
      minutes compacting a long conversation.
    - In Claude Code an accepted wake-up can still be lost, so there the reservation also ends when the agent's
      next turn does (end_lost_wake).
    - It holds the agent's hand-over lock, shared, until the wake-up is sent, so a restart never takes the agent
      over in between; while a restart holds it, nothing is sent.
    - A failed wake-up puts back what this attempt still holds and is tried again after WAKE_RETRY by the next
      aichord command.
    agy wakes itself: its `aichord watch` ends once mail is stored."""
    name, tool = a["name"], a["tool"]
    if tool not in ("claude", "codex") or (tool == "codex" and not a["session_id"]) or in_sandbox():
        return
    if tool == "claude" and claude_unwakeable(conn, name):
        return
    last = float(meta_get(conn, f"last_wake:{name}") or 0)
    if meta_get(conn, f"wake_error:{name}") and not retry_now and now() - last < WAKE_RETRY:
        return
    hold = take_lock(agent_lock(name), shared=True)
    if hold is None:
        return
    try:
        attempt = repr(now())
        items = claim_unseen(conn, name, actions=ACTIONS_THAT_WAKE, peer_turn=WOKEN, launched=a["launched"],
                             reserve=attempt)
        if not items:
            return
        full: list[int] = []
        text = f"{WAKE_TEXT} (wake-up {attempt}):\n\n" + render(conn, name, items, mark_read=False, shown_in_full=full)
        # Shortened and omitted ones stay unread. Written only while this wake-up still holds the reservation.
        conn.execute("UPDATE meta SET value=? WHERE key=? AND EXISTS (SELECT 1 FROM meta WHERE key=? AND value=?)",
                     (json.dumps(full), f"wake_read:{name}", f"wake_pending:{name}", attempt))
        cur = agent_row(conn, name)
        if cur is None or cur["launched"] != a["launched"]:
            error = "the agent was started again meanwhile"
        elif tool == "codex":
            error = codex_queue(conn, cur, text)
        else:
            error = claude_post(conn, name, text)
    finally:
        hold.close()
    ids = [m["id"] for m, _ in items]
    meta_set(conn, f"wake_error:{name}", error)
    shown = ", ".join(f"#{i}" for i in ids)
    if not error:
        wake_log(f"woke {name} with {shown} via {'codex queue' if tool == 'codex' else 'its message socket'}")
        return
    conn.execute("BEGIN IMMEDIATE")  # put back only what this attempt still holds: a newer turn's progress stays
    try:
        if meta_get(conn, f"wake_pending:{name}") == attempt:
            give_back(conn, name)
            conn.execute("UPDATE agents SET state='idle', detail='', turn_from=? WHERE name=? AND state='busy' "
                         "AND detail=? AND launched=?", (a["turn_from"], name, WOKEN, a["launched"]))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    wake_log(f"waking {name} with {shown} failed: {error}")


def wake_started(conn, agent: str, text: str) -> bool:
    """A wake-up's own prompt has started the agent's turn: what it showed in full is read now, and the reservation
    ends, so the next mail can wake the agent again. An older wake-up's prompt arriving late (Codex keeps a queued
    one across a restart of the same conversation) speaks only for itself, never for the newer one, and a prompt
    without the tag for nobody: a helper's report that only quotes a wake-up's wording once ended a reservation and
    marked its unseen mail read. The check and the clearing are one step: a reservation that ends meanwhile
    (end_lost_wake), and a newer one made after it, are never spoken for by this prompt."""
    tag = WAKE_TAG.search(text)
    conn.execute("BEGIN IMMEDIATE")
    try:
        mine = tag is not None and tag.group(1) == meta_get(conn, f"wake_pending:{agent}")
        if mine:
            t = now()
            for i in meta_ids(conn, f"wake_read:{agent}"):
                conn.execute("UPDATE deliveries SET read=COALESCE(read, ?) WHERE message=? AND recipient=?", (t, i, agent))
            for key in ("wake_pending", "wake_msgs", "wake_read"):
                meta_set(conn, f"{key}:{agent}", "")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return mine


def give_back(conn, agent: str) -> None:
    """End the agent's wake-up reservation and put back the mail it carried that was never read, so the next hook or
    wake-up shows it."""
    for i in meta_ids(conn, f"wake_msgs:{agent}"):
        conn.execute("UPDATE deliveries SET notified=NULL WHERE message=? AND recipient=? AND read IS NULL", (i, agent))
    for key in ("wake_pending", "wake_msgs", "wake_read"):
        meta_set(conn, f"{key}:{agent}", "")


def end_lost_wake(conn, agent: str) -> None:
    """At the end of a Claude Code agent's own turn: a wake-up whose prompt still hasn't come is counted as lost. Its
    reservation ends and the mail it carried that was never read is put back, so this Stop shows it and later mail
    wakes the agent again. Claude Code once took a wake-up on its socket and never delivered it (the session had five
    background helpers running; the wake-up's tag is nowhere in its conversation record): the reservation stayed for
    12 hours, and five replies waited 12 to 16 minutes each for the agent's next turn.
    Why the end of a turn: a message that reaches Claude Code during a turn runs its prompt hook when it is taken in
    at the next tool step, so by now the wake-up's prompt has come if it was going to. One that still comes later
    speaks only for itself (wake_started), and its mail is then shown a second time.
    Codex keeps a queued prompt until it runs, also behind another turn or a compaction, so its reservations wait
    for their own prompt and are only reported when stalled."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        pending, sent = meta_get(conn, f"wake_pending:{agent}"), wake_waiting(conn, agent)
        if pending:  # still there: its own prompt may have ended it since the caller looked
            give_back(conn, agent)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if pending:
        wake_log(f"the wake-up of {agent} sent at {hhmm(sent) if sent else 'an unknown time'} never started a turn of "
                 "its own; its reservation ended with the agent's next turn")


def in_sandbox() -> bool:
    """A command running inside Codex's sandbox (it sets CODEX_SANDBOX), which can reach neither another session's
    message socket nor Codex's own server. Its wake-ups are sent by the agent's next hook instead: Codex runs hooks
    outside the sandbox (a live check, and codex-rs/hooks/src/engine/command_runner.rs)."""
    return bool(os.environ.get("CODEX_SANDBOX"))


def wake_idle(conn, skip: str = "", retry_now: bool = False) -> None:
    """Wake every idle agent that has new mail (one that a sandboxed command or a failed wake-up left waiting)."""
    idle = conn.execute("SELECT * FROM agents a WHERE state = 'idle' AND EXISTS (SELECT 1 FROM deliveries d "
                        f"WHERE d.recipient = a.name AND {NEW_MAIL})").fetchall()
    for a in idle:
        if a["name"] != skip:
            wake(conn, a, retry_now)


def claude_settings_files(conn, name: str) -> list[Path]:
    """The settings files that can set how this Claude Code session treats incoming messages (macOS paths): its own
    user settings folder, as its hooks recorded it, never the one of the command asking."""
    user = Path(meta_get(conn, f"claude_config:{name}") or Path.home() / ".claude")
    root = project_root()
    return [Path("/Library/Application Support/ClaudeCode/managed-settings.json"), user / "settings.json",
            root / ".claude" / "settings.json", root / ".claude" / "settings.local.json"]


def claude_unwakeable(conn, name: str) -> str:
    """Why a socket wake-up couldn't start this Claude Code session's turn at once, or "". A message that carries
    the session's token counts as the session's own, and Claude Code delivers it at once unless a crossSessionInbound
    setting applies: "hold" keeps it, with no time limit, until an accept applies, and "refuse" drops it (Claude
    Code's cross-session messaging docs). aichord doesn't send into either, so an accepted wake-up always starts its
    turn, and its reservation can wait for that prompt."""
    if not meta_get(conn, f"socket:{name}"):
        return "its message socket is recorded at its next hook"
    if not meta_get(conn, f"socket_token:{name}"):
        return "Claude Code didn't give this session a message token"
    for f in claude_settings_files(conn, name):
        try:
            value = json.loads(f.read_text()).get("crossSessionInbound")
        except (OSError, ValueError, AttributeError):
            continue
        if value not in (None, "accept"):
            return f"crossSessionInbound is {value!r} in {f}"
    return ""


def claude_post(conn, name: str, text: str) -> str:
    """Deliver text to a Claude Code session through its message socket: an idle session starts a turn with it (a
    live check). Returns "" when the socket took it, or the error. The session's own token makes the message
    count as the session's own, also from a process that isn't its child."""
    path, token = meta_get(conn, f"socket:{name}"), meta_get(conn, f"socket_token:{name}")
    frames = ([{"type": "auth", "token": token}] if token else []) + [
        {"type": "user", "from": "aichord", "message": {"content": text}}]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(path)
            s.sendall("".join(json.dumps(f) + "\n" for f in frames).encode())
            s.shutdown(socket.SHUT_WR)
            try:
                s.recv(4096)  # Claude Code closes the connection once it has read the message
            except OSError:
                pass
        return ""
    except OSError as e:
        return f"{type(e).__name__}: {e}"


def codex_queue(conn, a, text: str) -> str:
    """Start a turn in an idle Codex session with this text as its prompt. The session's own Codex version is used,
    so an update while it runs doesn't pair it with a newer queue command. Returns "" or the error; no answer
    within 30 seconds counts as queued, because it may have been."""
    exe = meta_get(conn, f"tool_path:{a['name']}")
    try:
        out = subprocess.run([exe if exe and os.path.exists(exe) else "codex", "queue", "--thread", a["session_id"],
                              "--message", text], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        wake_log(f"codex queue for {a['name']} gave no answer in 30 s; counting the wake-up as queued")
        return ""
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return "" if out.returncode == 0 else (out.stderr.strip()[:300] or f"exit code {out.returncode}")


def wake_waiting(conn, name: str) -> float:
    """When the agent's accepted wake-up was sent, while its turn hasn't started yet; 0 when none waits."""
    pending = meta_get(conn, f"wake_pending:{name}")
    try:
        return float(pending) if pending else 0.0
    except ValueError:
        return 0.0


def woken_stalled(conn, a) -> bool:
    """An accepted wake-up whose turn hasn't started after WAKE_STALLED. It is reported, never repeated."""
    sent = wake_waiting(conn, a["name"])
    return bool(sent) and a["state"] != "gone" and now() - sent >= WAKE_STALLED


def wake_log(msg: str) -> None:
    try:
        with open(state_dir() / "runtime" / "wake.log", "a") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def tend(conn, skip: str = "", retry_now: bool = False) -> None:
    """Upkeep that no background process does, done by whichever aichord command runs (a wait, a status, an
    agent's stop or start): wake idle agents that have new mail, a failed wake-up again; report a wake-up whose turn
    never started; notice agents whose tool has exited."""
    t = now()
    if t - float(meta_get(conn, "tended") or 0) > 5:
        meta_set(conn, "tended", str(t))
        mark_exited(conn)
    wake_idle(conn, skip, retry_now)
    for a in agents(conn, live_only=True):
        if a["tool"] == "codex" and a["name"] != skip:
            helper_wake(conn, a, retry_now)
        sent = meta_get(conn, f"wake_pending:{a['name']}")
        if woken_stalled(conn, a) and meta_get(conn, f"wake_stall:{a['name']}") != sent:
            meta_set(conn, f"wake_stall:{a['name']}", sent)
            wake_log(f"the wake-up of {a['name']} sent at {hhmm(float(sent))} hasn't started a turn; typing anything "
                     "in its tab starts one")


# ---- launching ----------------------------------------------------------------------------


def identity_prompt(name: str, tool: str, role: str | None = None) -> str:
    role = ROLES.get(tool, "") if role is None else role
    return (f"You are {name} in an aichord session: AI agents in different tools working on this project with the user. "
            f"The session-start note lists the team. {('Your role: ' + role + '. ') if role else ''}"
            f"Use the aichord skill ({SKILL_DIR / 'SKILL.md'}) and the `aichord` command to talk to the others. "
            + MODEL_NOTE)


def claude_settings(root: Path) -> Path:
    t = aichord_cmd()
    hook = lambda ev, **kw: {"type": "command", "command": f"{t} hook claude {ev}", **kw}  # noqa: E731
    settings = {"hooks": {
        "SessionStart": [{"hooks": [hook("SessionStart")]}],
        "UserPromptSubmit": [{"hooks": [hook("UserPromptSubmit")]}],
        "PostToolUse": [{"matcher": "*", "hooks": [hook("PostToolUse")]}],
        "Stop": [{"hooks": [hook("Stop")]}],
        "SessionEnd": [{"hooks": [hook("SessionEnd")]}],
        "PreCompact": [{"hooks": [hook("PreCompact")]}],
    }}
    path = state_dir(root) / "runtime" / "claude-settings.json"
    part = path.with_name(f".claude-settings-{os.getpid()}.json")
    part.write_text(json.dumps(settings, indent=2))
    os.replace(part, path)  # never half-written while another Claude Code agent of the team starts and reads it
    return path


def codex_hook_flags() -> list[str]:
    t = aichord_cmd()
    flags = []
    for ev in HOOK_EVENTS["codex"]:
        cmd = json.dumps(f"{t} hook codex {ev}")  # a TOML basic string is compatible with a JSON string here
        flags += ["-c", f"hooks.{ev}=[{{hooks=[{{type=\"command\",command={cmd}}}]}}]"]
    return flags


def launch_command(name: str, tool: str, root: Path, extra: list[str], resume: str = "",
                   prompt: str = FIRST_PROMPT, role: str | None = None) -> list[str]:
    """Each tool starts with a first prompt of aichord's own. The first turn gives every tool what its wake-up path
    needs: Claude Code's hooks record its message socket, Codex has a session for codex queue only after a turn, and
    agy starts `aichord watch` in it. `resume` is the conversation to continue."""
    if tool == "claude":
        return ["claude", "--settings", str(claude_settings(root)), "--append-system-prompt",
                identity_prompt(name, tool, role), *(["--resume", resume] if resume else []), *extra, prompt]
    if tool == "codex":  # its helpers' default model, if set (Codex refuses unknown keys under agents: this one exists)
        model = helper_model("codex")
        helpers = ["-c", f"agents.default_subagent_model={json.dumps(model)}"] if model else []
        return ["codex", *(["resume"] if resume else []), *codex_hook_flags(), *helpers, *extra,
                *([resume] if resume else []), prompt]
    return agy_launch_command(name, root, extra, resume, prompt)


LAUNCH_WAIT = 40  # seconds a launch waits for a wake-up being sent to the agent it replaces
BEFORE_LAUNCH_SECONDS = 600


def before_launch(tool: str) -> None:
    """The user's own command to run before this tool starts (before_launch in config.json). Its output shows in the
    pane, and a failure never stops the launch."""
    commands = config().get("before_launch")
    cmd = commands.get(tool) if isinstance(commands, dict) else None
    if not cmd:
        return
    try:
        argv = [os.path.expanduser(a) for a in (shlex.split(cmd) if isinstance(cmd, str) else map(str, cmd))]
        if not argv:
            raise ValueError("the command is empty")
        code = subprocess.run(argv, stdin=subprocess.DEVNULL, timeout=BEFORE_LAUNCH_SECONDS).returncode
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as e:
        code = f"{type(e).__name__}: {e}"
    if code:
        print(f"aichord: the before_launch command for {tool} failed ({code}); starting {tool} anyway.", file=sys.stderr)


def cmd_launch(args) -> None:
    if check_name(args.name):
        sys.exit(f"aichord: {check_name(args.name)}")
    root = project_root()
    conn = db(root)
    name = args.name
    tool = args.tool or (agent_row(conn, name) or {"tool": ""})["tool"] or default_tool(name)
    if not tool:
        sys.exit(f"aichord: which tool should {name} run in? Pass --tool claude|agy|codex.")
    if shutil.which(tool) is None:
        sys.exit(f"aichord: {tool} is not on PATH.")
    resume = getattr(args, "resume", "") or ""
    launched = now()
    # A resumed agent keeps its conversation: hooks from any other session stay ignored, and a resume that failed
    # shows as an agent that never leaves "starting", with the conversation still recorded for the next restart.
    # Like a restart, a launch takes the agent over only once no wake-up is being sent to it (codex queue may take
    # 30 s) and its hooks have finished: a wake-up never goes to a replaced session.
    hold = take_lock(agent_lock(name), wait=LAUNCH_WAIT)
    if hold is None:
        sys.exit(f"aichord launch: a wake-up or hook of {name} was still running after {LAUNCH_WAIT} s, so nothing was "
                 "started. Run it again.")
    conn.execute("BEGIN IMMEDIATE")  # one step, so a wake-up either took its mail before (and it comes back) or not at all
    try:
        elsewhere = running_pane(conn, name)  # checked under the lock, so two launches at once can't both pass
        if elsewhere and elsewhere != os.environ.get("TMUX_PANE", ""):  # two copies would share one seat
            sys.exit(f"aichord: {name} is already running, in pane {elsewhere}. To start another agent in "
                     f"{TOOL_LABEL[tool]}: aichord spawn {tool}")
        meta_set(conn, f"left:{name}", "")  # on the team again
        give_back(conn, name)  # mail an earlier launch was woken with and never read
        conn.execute(
            "INSERT INTO agents(name,tool,pane,session_id,state,detail,updated,launched,turn_from) "
            "VALUES(?,?,?,?,'starting','',?,?,'') ON CONFLICT(name) DO UPDATE SET tool=excluded.tool, "
            "pane=excluded.pane, session_id=excluded.session_id, state='starting', detail='', updated=excluded.updated, "
            "launched=excluded.launched, turn_from=''", (name, tool, os.environ.get("TMUX_PANE", ""), resume, launched, launched))
        for key in ("seated", "reminded", "turn_msg", "turn_answer", "skip_answer", "reply_reminded", "task_owners",
                    "agy_running", "helpers_owner", "helpers_pending", "collecting", "agy_watches", "watch_ended",
                    "last_wake", "wake_error", "wake_stall", "socket", "socket_token", "claude_config"):
            meta_set(conn, f"{key}:{name}", "")
        meta_set(conn, f"reground:{name}", "" if resume else "launched")  # a resumed conversation keeps its context
        if not resume:  # and still owes the user the answers it owed
            meta_set(conn, f"open_msgs:{name}", "")

        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        hold.close()
    meta_set(conn, f"tool_path:{name}", os.path.realpath(shutil.which(tool)))  # codex queue uses this same version
    if os.environ.get("TMUX_PANE"):
        subprocess.run(["tmux", "select-pane", "-t", os.environ["TMUX_PANE"], "-T", f"{name} ({TOOL_LABEL[tool]})"],
                       capture_output=True)
    env = {**os.environ, "AICHORD_AGENT": name, "AICHORD_ROOT": str(root), "AICHORD_TOOL": tool,
           "AICHORD_LAUNCHED": repr(launched)}
    model = helper_model(tool) if tool == "claude" else ""
    if model:  # its helpers' default model; one helper can still ask for another, and a variable already set wins
        env.setdefault("CLAUDE_CODE_SUBAGENT_MODEL", model)
    before_launch(tool)
    prompt = first_prompt(bool(resume), getattr(args, "versions_changed", "") or "")
    cmd = launch_command(name, tool, root, list(args.extra), resume, prompt, role_of(conn, name, tool))
    os.chdir(root)
    os.execvpe(cmd[0], cmd, env)


# ---- updating the tools ---------------------------------------------------------------------

UPDATE_SECONDS = 120  # for all updates together; they run at once, and Antigravity downloads about 190 MB


def tool_version(tool: str) -> str:
    """The installed version, or "" when the command doesn't run."""
    try:
        out = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"\d+(?:\.\d+)+", out.stdout)
    return m.group(0) if out.returncode == 0 and m else ""


def update_tools(tools: list[str]) -> str:
    """Run each tool's own updater, all at once, before anything is started or closed. A failed, slow or prompting
    update doesn't stop the start: the tool runs at the version it has. A tool that no longer runs stops it.
    Returns the version changes, for example "Codex 0.163.0 to 0.164.0"."""
    tools = list(dict.fromkeys(tools))
    before = {t: tool_version(t) for t in tools}
    print(f"Updating {', '.join(TOOL_LABEL[t] for t in tools)} (at most {UPDATE_SECONDS} s; skip with --no-update)...",
          flush=True)
    runs, problems, give_up = {}, {}, now() + UPDATE_SECONDS
    try:
        for t in tools:
            if before[t]:
                out = tempfile.TemporaryFile("w+")  # a file, not a pipe: progress output can't fill and stall it
                runs[t] = (subprocess.Popen([t, "update"], stdin=subprocess.DEVNULL, stdout=out,
                                            stderr=subprocess.STDOUT, text=True, start_new_session=True), out)
        for t, (p, out) in runs.items():  # every updater has ended before any tool is judged
            try:
                p.wait(timeout=max(0.1, give_up - now()))
            except subprocess.TimeoutExpired:
                problems[t] = f"no result after {UPDATE_SECONDS} s, so it was stopped"
                continue
            if p.returncode:
                out.seek(0)
                said = [x.strip() for x in re.split(r"[\r\n]", out.read()) if x.strip()]
                problems[t] = clip(said[-1], 160) if said else f"exit code {p.returncode}"
    finally:  # however this ends (a time limit, an error, Ctrl-C), no updater keeps changing an installation
        for p, out in runs.values():
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)  # the updater and anything it started
                except ProcessLookupError:
                    pass
                p.wait()
            out.close()
    changes, broken = [], []
    for t in tools:
        after, label, problem = tool_version(t), TOOL_LABEL[t], problems.get(t, "")
        if not after:
            broken.append(f"{label} doesn't run" + (f" after its update ({problem or 'no version'})" if before[t] else
                                                    f" (`{t} --version` fails)"))
            continue
        if before[t] != after:
            changes.append(f"{label} {before[t]} to {after}")
        print(f"  {label} {after}: " + (f"update failed ({problem}); starting this version" if problem else
                                        f"updated from {before[t]}" if before[t] != after else "up to date"))
    if broken:
        sys.exit(f"aichord: {'; '.join(broken)}. Nothing was started or closed. Repair it (its own update command, or "
                 "reinstall), or start without updating: --no-update.")
    return ", ".join(changes)


# ---- starting and restarting the team -------------------------------------------------------


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


CLOSE_SECONDS = 10  # for the closed tools, and everything they started, to exit after their hang-up


def team_panes(conn, root: Path, here: str) -> dict[str, str]:
    """This project's agent panes and its log pane, never the pane this runs in: pane id -> agent ("" for the log).
    A pane counts only while it is in this project's folder; another project's log pane has the same title."""
    panes = tmux_panes()
    owner = {r["pane"]: r["name"] for r in agents(conn) if r["pane"]}
    ours = set(owner) | {p for p, v in panes.items() if v["title"] == "aichord log"}
    found = {p: owner.get(p, "") for p in sorted(ours - {here}) if p in panes and in_project(panes[p], root)}
    for p, _, _ in marked_panes(root):  # the tabs aichord opened for this project's agents, closed ones included
        if p != here and p in panes and p not in found:
            found[p] = owner.get(p, "")
    return found


def hand_over(conn, names: list[str]) -> None:
    """Take the agents over for a restart in one step, so no work slips in between asking and closing. With their
    running hooks finished and new ones held back, it checks whether any of them is working; once the user agrees
    (or none is), it marks them all as being restarted, with a new launch time. From then on their old hooks and
    checkers write nothing, and nothing wakes them."""
    agreed: set[str] = set()
    marks = ",".join("?" * len(names))
    while names:
        locks = [take_lock(agent_lock(n), wait=10) for n in names]
        try:
            if None in locks:
                sys.exit("aichord restart: an agent's hooks were still running after 10 s, so nothing was closed. "
                         "Run it again.")
            conn.execute("BEGIN IMMEDIATE")
            try:
                busy = {r["name"] for r in conn.execute(
                    f"SELECT name FROM agents WHERE name IN ({marks}) AND state IN ('busy', 'waiting')", names)} - agreed
                if not busy:
                    t = now()
                    conn.execute(f"UPDATE agents SET state='gone', detail='being restarted', updated=?, launched=? "
                                 f"WHERE name IN ({marks})", (t, t, *names))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            for f in locks:
                if f:
                    f.close()
        if not busy:
            return
        if not confirm(f"{', '.join(sorted(busy))} {'is' if len(busy) == 1 else 'are'} working. A restart stops that "
                       "work, and the resumed conversation won't finish it on its own. Restart anyway?"):
            sys.exit("Nothing was restarted.")
        agreed |= busy


def close_panes(pane_ids: list[str]) -> None:
    """Close these panes and wait until every process in them has exited: the tools, their hooks and checkers.
    Whatever is still running after CLOSE_SECONDS is killed, so two copies never share a conversation."""
    panes = tmux_panes()
    # A closed pane (remain-on-exit) still reports the number of its exited process, which may by now belong to an
    # unrelated program: only live panes give processes to wait for and stop.
    pids = descendants([int(panes[p]["pid"]) for p in pane_ids
                        if p in panes and panes[p]["pid"].isdigit() and panes[p]["dead"] != "1"])
    for pane in pane_ids:
        subprocess.run(["tmux", "kill-pane", "-t", pane], capture_output=True)  # its last pane closes the tab
    pids = still_running(pids, CLOSE_SECONDS)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if still_running(pids, 3):
        sys.exit(f"aichord restart: old agent processes didn't exit ({', '.join(map(str, pids))}), so nothing was "
                 "started. Stop them, then run aichord restart again.")


def descendants(pids: list[int]) -> list[int]:
    """These processes and every process they started."""
    children: dict[int, list[int]] = {}
    for line in subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout.splitlines():
        pid, ppid = (int(x) for x in line.split())
        children.setdefault(ppid, []).append(pid)
    found, todo = [], [p for p in pids if p > 1]
    while todo:
        pid = todo.pop()
        found.append(pid)
        todo += children.get(pid, [])
    return found


def still_running(pids: list[int], seconds: float) -> list[int]:
    """Wait up to `seconds` for these processes to exit; return those still running."""
    give_up = now() + seconds
    while True:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                alive.append(pid)
        if not alive or now() >= give_up:
            return alive
        pids = alive
        time.sleep(0.1)


def cmd_up(args, restart: bool = False) -> None:
    """Start the team: the first agent in this tmux tab, a tab for each other agent, and a log tab. A restart brings
    the team back in the same conversations: it closes this project's agent and log tabs (never the one it runs
    in) and resumes each recorded conversation. Both update the tools first with --update, or when the user's settings
    ask for it (update_tools) and --no-update isn't given."""
    word = "restart" if restart else "up"
    if not os.environ.get("TMUX"):
        sys.exit(f"aichord {word}: run this inside tmux (it starts the first agent in your current tab and adds a tab "
                 "for each other agent, and a log tab, to your current tmux session).")
    if restart and os.environ.get("AICHORD_AGENT"):
        sys.exit("aichord restart: run it from a shell. It closes every agent's tab, and an agent would close its own.")
    root = project_root()
    conn = db(root, create=not restart)
    lock = take_lock(state_dir(root) / "runtime" / "up.lock")  # noqa: F841 (held until this process ends or execs)
    if lock is None:
        sys.exit(f"aichord {word}: another aichord up or restart is already running for this project.")
    here = os.environ.get("TMUX_PANE", "")
    mark_exited(conn)
    rows = {r["name"]: r for r in agents(conn)}
    running = [r["name"] for r in rows.values() if r["state"] != "gone" and r["pane"] and r["pane"] != here]
    resume: dict[str, str] = {}
    if restart:  # the team as recorded: each agent in its own tool, on its own conversation
        order = list(TEAM.values())  # Claude Code first, in the tab this runs in
        team = [(r["name"], r["tool"]) for r in sorted(
            rows.values(), key=lambda r: (order.index(r["tool"]) if r["tool"] in order else len(order), r["launched"]))]
        if not team:
            sys.exit("aichord restart: no team is recorded in this project yet. Start one with aichord up.")
        resume = {n: rows[n]["session_id"] for n, _ in team if rows[n]["session_id"]}
        new = [n for n, _ in team if n not in resume]
        if new and not confirm(f"{', '.join(new)} {'has' if len(new) == 1 else 'have'} no recorded conversation. "
                               "Start in a new one?"):
            sys.exit("Nothing was restarted.")
        mine = [i for i, (n, _) in enumerate(team) if rows[n]["pane"] == here]
        if mine:  # the user quit this agent's tool here: it comes back in this tab
            team.insert(0, team.pop(mine[0]))
            set_state(conn, team[0][0], "gone", detail="its tool has exited")  # Antigravity has no hook for that
    else:
        team = parse_team(args.team or config().get("team") or ",".join(TEAM))
        if running:
            sys.exit(f"aichord up: {', '.join(running)} already running in this project. To start the team again "
                     "with updated tools, in the same conversations: aichord restart. To add one agent: aichord spawn TOOL")
    update = args.update or (bool(config().get("update_tools")) and not args.no_update)
    changed = update_tools([tool for _, tool in team]) if update else ""
    if restart:  # after the updates, so work that started during them counts too
        mark_exited(conn)
        closing = team_panes(conn, root, here)
        hand_over(conn, [n for n in closing.values() if n])
        close_panes(list(closing))
    else:  # the team is the one stated: earlier agents leave it, and every member is known before any starts
        settle_team(conn, team)
    t = aichord_bin()
    session = name_session(root, here)  # after a restart has closed the old tabs, so their session's name is free
    # One tmux window (a tab in the status bar) per agent, named after the agent, then a log tab.
    tabs = [name for name, _ in team]
    if not args.no_tail:
        tabs.append("log")
    # The tab the user runs this from becomes the first agent's tab, unless it is split into several panes or this
    # isn't an interactive shell (an agent or a script running `aichord up` must not turn into Claude Code).
    reuse = bool(here) and not args.new_tab and sys.stdin.isatty() and not os.environ.get("AICHORD_AGENT") \
        and tmux_out("display-message", "-p", "-t", here, "#{window_panes}").strip() == "1"
    windows: list[tuple[str, str]] = []  # (window id, pane id)
    if reuse:
        win = tmux_out("display-message", "-p", "-t", here, "#{window_id}").strip()
        subprocess.run(["tmux", "rename-window", "-t", win, tabs[0]], check=True)  # also stops automatic renaming
        subprocess.run(["tmux", "set-option", "-w", "-t", win, "allow-rename", "off"], capture_output=True)
        windows.append((win, here))
    # The other tabs go side by side right after the current one; -d keeps the focus still.
    for tab in tabs[len(windows):]:
        cmd = ["tmux", "new-window", "-d", "-a", "-P", "-F", "#{window_id} #{pane_id}", "-n", tab, "-c", str(root)]
        if windows:
            cmd += ["-t", windows[-1][0]]
        win, pane = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.split()
        subprocess.run(["tmux", "set-option", "-w", "-t", win, "allow-rename", "off"], capture_output=True)
        windows.append((win, pane))
    # The first agent checks the wake-up paths live when a tool's version changed.
    # With more than one agent, the first checks the wake-up paths live when a tool's version changed.
    launches = [argparse.Namespace(name=name, tool=tool, extra=[], resume=resume.get(name, ""),
                                   versions_changed=changed if i == 0 and len(team) > 1 else "")
                for i, (name, tool) in enumerate(team)]
    for a, (win, pane) in list(zip(launches, windows))[int(reuse):]:  # each new tab runs only its agent
        agent_tab_setup(win, pane, a.name, root)
        subprocess.run(["tmux", "send-keys", "-t", pane, "exec " + launch_line(t, a.name, a.tool, a.resume,
                                                                               a.versions_changed), "Enter"], check=True)
    if not args.no_tail:
        tail = windows[-1][1]
        subprocess.run(["tmux", "send-keys", "-t", tail, f"{t} tail", "Enter"], check=True)
        subprocess.run(["tmux", "select-pane", "-t", tail, "-T", "aichord log"], capture_output=True)
    print(("Restarted " if restart else "Started ")
          + ", ".join(f"{a.name} ({TOOL_LABEL.get(a.tool, a.tool)}{', same conversation' if a.resume else ''}) in the "
                      f"'{tab}' tab" for a, tab in zip(launches, tabs))
          + (", and the message log in the 'log' tab." if not args.no_tail else ".")
          + (f" This tmux session is now called {session}." if session else ""))
    if reuse:
        cmd_launch(launches[0])  # replaces this process; no return
    subprocess.run(["tmux", "select-window", "-t", windows[0][0]], capture_output=True)


# ---- the team: stated at up, changed while it runs (spawn, stop) -----------------------------


def check_name(name: str) -> str:
    """Why this can't be an agent's name, or ""."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", name) or name in RESERVED_NAMES:
        return (f"{name!r} can't be an agent's name: names are short lowercase words (letters, digits, - and _), "
                f"not {', '.join(sorted(RESERVED_NAMES))}.")
    return ""


def default_tool(name: str) -> str:
    """The tool a name implies: one numbered after it (codex-1), or a tool's own name (an agent named before agents
    were numbered)."""
    base = re.sub(r"-\d+$", "", name)
    return TEAM.get(name, "") or (base if base in TOOL_LABEL else "")


def next_name(tool: str, taken) -> str:
    """The first free name for an agent in this tool: codex-0, then codex-1, codex-2, ..."""
    n = 0
    while f"{tool}-{n}" in taken:
        n += 1
    return f"{tool}-{n}"


def parse_team(spec) -> list[tuple[str, str]]:
    """The team `aichord up` starts, from --team or the team setting: tools, repeated for several agents in one tool
    (claude,claude,codex starts claude-0, claude-1 and codex-0), or NAME:TOOL. Checked whole before anything starts: an
    unknown tool, or a name that is invalid or given twice, starts nothing."""
    items = [str(x).strip() for x in (spec if isinstance(spec, list) else str(spec).split(",")) if str(x).strip()]
    team: list[tuple[str, str]] = []
    problems = []
    for item in items:
        name, colon, tool = (x.strip() for x in item.partition(":"))
        if not colon:
            name, tool = ("", name) if name in TOOL_LABEL else (name, default_tool(name))
        if tool not in TOOL_LABEL:
            problems.append(f"{item}: " + (f"no tool called {tool}" if tool else "which tool? Write NAME:TOOL"))
            continue
        named = {i.partition(":")[0].strip() for i in items if ":" in i or i not in TOOL_LABEL}  # given by the user
        name = name or next_name(tool, {n for n, _ in team} | named)
        problem = check_name(name) or (f"{name} is named twice" if name in {n for n, _ in team} else "")
        if problem:
            problems.append(problem)
            continue
        team.append((name, tool))
    if problems or not team:
        sys.exit("aichord up: nothing was started. " + ("; ".join(problems) + "." if problems else "The team is empty.")
                 + " The tools are claude, agy and codex; for example --team claude,claude,codex or --team lead:claude,math:codex.")
    return team


def settle_team(conn, team: list[tuple[str, str]]) -> None:
    """At a fresh `aichord up`: the stated team is the team. Agents registered earlier and not in it leave (their
    messages stay), with what they still owed closed for them, so default questions and restarts leave them out; the
    members are registered before any of them starts, so every start note lists the whole team."""
    names = [n for n, _ in team]
    conn.execute("BEGIN IMMEDIATE")
    try:
        left = [r["name"] for r in agents(conn) if r["name"] not in names]
        close_owed(conn, left)
        conn.executemany("DELETE FROM agents WHERE name=?", [(n,) for n in left])
        for name in left:
            meta_set(conn, f"left:{name}", "1")
        t = now()
        for name, tool in team:
            meta_set(conn, f"left:{name}", "")
            conn.execute("INSERT INTO agents(name,tool,state,updated,launched) VALUES(?,?,'starting',?,?) "
                         "ON CONFLICT(name) DO UPDATE SET tool=excluded.tool, state='starting', detail='', pane='', "
                         "session_id='', turn_from='', updated=excluded.updated, launched=excluded.launched",
                         (name, tool, t, t))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def close_owed(conn, names: list[str]) -> list[int]:
    """Inside the caller's transaction: the questions these agents still owe a reply are closed for them, so nobody
    waits for an agent that left the team. Returns their numbers."""
    if not names:
        return []
    marks = ",".join("?" * len(names))
    owed = [r[0] for r in conn.execute(f"SELECT message FROM deliveries WHERE recipient IN ({marks}) AND action='reply' "
                                       "AND replied IS NULL ORDER BY message", names)]
    t = now()
    conn.execute(f"UPDATE deliveries SET replied=?, notified=COALESCE(notified, ?) WHERE recipient IN ({marks}) "
                 "AND action='reply' AND replied IS NULL", (t, t, *names))
    return owed


TAB_MARK = "@aichord_agent"  # a tmux option on each tab aichord opens for an agent: "<name> <project folder>"


def agent_tab_setup(win: str, pane: str, name: str, root: Path) -> None:
    """A tab aichord opens for an agent: it keeps its name, it stays on screen as a closed tab with the last output
    when the agent's tool exits (nothing else can run in it), and its pane carries the agent's mark. The mark is the
    pane's, not the tab's: a pane the user splits off beside it doesn't get it."""
    for option, value in (("allow-rename", "off"), ("remain-on-exit", "on")):
        subprocess.run(["tmux", "set-option", "-w", "-t", win, option, value], capture_output=True)
    subprocess.run(["tmux", "set-option", "-p", "-t", pane, TAB_MARK, f"{name} {os.path.realpath(root)}"],
                   capture_output=True)


def marked_panes(root: Path) -> list[tuple[str, str, bool]]:
    """Every pane aichord opened for an agent of this project, by its mark: (pane, agent, whether it shows the agent
    closed). An agent brought back or started again in a new tab leaves its earlier, closed one marked too."""
    where, found = os.path.realpath(root), []
    for line in tmux_out("list-panes", "-a", "-F", "#{pane_id}\t#{pane_dead}\t#{" + TAB_MARK + "}").splitlines():
        pane, dead, mark = (line.split("\t") + ["", ""])[:3]
        name, _, folder = mark.partition(" ")
        if name and folder == where:
            found.append((pane, name, dead == "1"))
    return found


def agent_tab(name: str, root: Path, pane: str) -> str:
    """The agent's registered pane, if it is one aichord opened for this agent in this project (it carries the mark),
    else "". Such a pane runs only the agent (`exec aichord launch`), or shows it closed, so closing it never closes
    anything of the user's: not a pane split off beside it, nor an older closed tab of the same agent."""
    return pane if pane and any(p == pane and n == name for p, n, _ in marked_panes(root)) else ""


def closed_tabs(name: str, root: Path, keep: str = "") -> list[str]:
    """The agent's earlier tabs that show it closed (marked, dead): nothing runs in them, so they can always go."""
    return [p for p, n, dead in marked_panes(root) if n == name and dead and p != keep]


def aichord_bin() -> str:
    """How a tab started by aichord runs aichord: the installed command, else this script."""
    wrapper = Path.home() / ".local/bin/aichord"
    return shlex.quote(str(wrapper)) if wrapper.exists() else aichord_cmd()


def launch_line(t: str, name: str, tool: str, resume: str = "", changed: str = "", extra: list[str] | None = None) -> str:
    return (f"{t} launch {name} --tool {tool}" + (f" --resume {shlex.quote(resume)}" if resume else "")
            + (f" --versions-changed {shlex.quote(changed)}" if changed else "")
            + (" -- " + shlex.join(extra) if extra else ""))


def running_pane(conn, name: str) -> str:
    """The tmux pane in which this agent runs now, as far as tmux can tell, or ""."""
    a = agent_row(conn, name)
    if a is None or a["state"] == "gone" or not a["pane"] or not os.environ.get("TMUX"):
        return ""
    return a["pane"] if pane_runs_agent(a, tmux_panes(), project_root()) else ""


def team_window(conn, root: Path) -> str:
    """The tmux window a new agent's tab goes after: the right-most tab of this team's running agents, else the
    caller's own tab when it is in this project. Never just "the current session": the user may have many attached."""
    panes = tmux_panes()
    for ours in ([r["pane"] for r in agents(conn, live_only=True) if r["pane"]], [os.environ.get("TMUX_PANE", "")]):
        best, where = "", -1
        for pane in ours:
            if pane and pane in panes and in_project(panes[pane], root):
                win, _, index = tmux_out("display-message", "-p", "-t", pane,
                                         "#{window_id} #{window_index}").strip().partition(" ")
                if win and index.isdigit() and int(index) > where:
                    best, where = win, int(index)
        if best:
            return best
    return ""


def cmd_spawn(args) -> None:
    """Start an agent in a new tab while the team runs, from an agent or a shell. `aichord spawn codex`: the first
    agent in Codex that isn't running comes back in its own conversation, or, if all of them run, the next one starts
    (codex-0, codex-1, ...). `aichord spawn NAME` or `NAME:TOOL`: that agent. The others are told it joined. The tab
    runs only the agent, and carries its mark, so `aichord stop` can close it safely."""
    if in_sandbox():
        sys.exit("aichord spawn: Codex's sandbox can't reach tmux, so nothing was started. Ask an agent in Claude Code "
                 "or Antigravity (aichord send --wake --to NAME), or the user, to run it.")
    root = project_root()
    conn = db(root)
    lock = take_lock(state_dir(root) / "runtime" / "up.lock", wait=15)  # noqa: F841 (one start at a time picks names)
    if lock is None:
        sys.exit("aichord spawn: an aichord up, restart or spawn is still running for this project. Run it again.")
    mark_exited(conn)
    rows = {r["name"]: r for r in agents(conn)}
    name, colon, tool = (x.strip() for x in args.spec.partition(":"))
    if not colon and name in TOOL_LABEL and (name not in rows or rows[name]["state"] != "gone"):
        # one more agent in this tool: one that stopped comes back in its own conversation; with --fresh, or if none
        # stopped, a new one starts, so no stopped agent's conversation is replaced
        tool = name
        stopped = [r for r in rows.values() if r["tool"] == tool and r["state"] == "gone"] if not args.fresh else []
        name = stopped[0]["name"] if stopped else next_name(tool, rows)
    elif not colon:
        tool = rows[name]["tool"] if name in rows else default_tool(name)
    if tool not in TOOL_LABEL:
        sys.exit(f"aichord spawn: which tool should {name} run in? Write aichord spawn {name}:TOOL (tools: claude, agy, "
                 "codex), or name a tool: aichord spawn codex.")
    if check_name(name):
        sys.exit(f"aichord spawn: {check_name(name)}")
    a = rows.get(name)
    if a is not None and a["tool"] != tool:
        sys.exit(f"aichord spawn: {name} is an agent in {TOOL_LABEL.get(a['tool'], a['tool'])}, not {TOOL_LABEL[tool]}.")
    if a is not None and a["state"] != "gone":
        sys.exit(f"aichord spawn: {name} is already running (pane {a['pane'] or '?'}). To start another agent in "
                 f"{TOOL_LABEL[tool]}: aichord spawn {tool}")
    if shutil.which(tool) is None:
        sys.exit(f"aichord spawn: {tool} is not on PATH.")
    resume = a["session_id"] if a is not None and not args.fresh else ""
    win = team_window(conn, root) or (tmux_out("display-message", "-p", "-t", os.environ["TMUX_PANE"], "#{window_id}").strip()
                                      if os.environ.get("TMUX") and os.environ.get("TMUX_PANE") else "")
    if not win:  # never "the current session": the user may have many attached
        sys.exit(f"aichord spawn: no tab of this team is open in tmux to put {name} next to. Start it by hand, in a new "
                 "tab in the project: " + launch_line("aichord", name, tool, resume, extra=args.extra))
    cmd = ["tmux", "new-window", "-d", "-P", "-F", "#{window_id} #{pane_id}", "-n", name, "-c", str(root)]
    out = subprocess.run(cmd + ["-a", "-t", win], capture_output=True, text=True)
    if out.returncode or len(out.stdout.split()) != 2:
        sys.exit(f"aichord spawn: tmux couldn't open a tab ({clip(out.stderr, 160) or 'no answer'}), so nothing was started.")
    new_win, pane = out.stdout.split()
    agent_tab_setup(new_win, pane, name, root)
    old_tabs = closed_tabs(name, root, pane)  # its earlier tabs that show it closed: closed once it is registered
    sent_at = now()
    typed = subprocess.run(["tmux", "send-keys", "-t", pane, "exec " + launch_line(aichord_bin(), name, tool, resume,
                                                                                    extra=args.extra), "Enter"],
                           capture_output=True, text=True)
    if typed.returncode:  # nothing will start there: the tab goes, and nothing was registered or announced
        subprocess.run(["tmux", "kill-window", "-t", new_win], capture_output=True)
        sys.exit(f"aichord spawn: tmux couldn't type the launch into the new tab ({clip(typed.stderr, 160) or 'no answer'}), "
                 "so nothing was started.")
    t = now()  # registered at once (still under the team's lock): its name is taken, and the others can address it
    conn.execute("BEGIN IMMEDIATE")
    try:
        # unless the launch typed above has already registered it (and perhaps finished its first turn): its own
        # record is newer, and a wake-up needs it as it is
        conn.execute("INSERT INTO agents(name,tool,pane,state,detail,updated,launched) VALUES(?,?,?,'starting','',?,?) "
                     "ON CONFLICT(name) DO UPDATE SET pane=excluded.pane, state='starting', detail='', "
                     "updated=excluded.updated WHERE agents.launched < ?", (name, tool, pane, t, t, sent_at))
        meta_set(conn, f"left:{name}", "")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if old_tabs:
        close_panes(old_tabs)
    how = "in its own conversation" if resume else "in a new conversation"
    post(conn, me(), ["team"], {n: "context" for n in live_names(conn, exclude=me()) if n != name}, "note",
         f"{name} ({TOOL_LABEL[tool]}) joined the team, {how}, in the '{name}' tab: {role_of(conn, name, tool)}. "
         "Questions to everyone reach it from now on.", subject=f"{name} joined the team")
    print(f"Started {name} ({TOOL_LABEL[tool]}) {how}, in a new tab '{name}'; the others were told. If this is the "
          f"first {TOOL_LABEL[tool]} agent in this project, its tab may ask whether to trust the folder (Codex also asks "
          "about hooks): only the user can answer that. aichord status shows it once it has started.")


def cmd_stop(args) -> None:
    """Stop an agent and close its tab, from an agent or a shell. It stays on the team, so aichord restart or aichord
    spawn brings it back in its conversation, unless --drop takes it off the team: then questions to everyone and
    restarts leave it out, and what it still owed is closed for it. Its messages stay either way."""
    name = args.name
    if name == me():
        sys.exit("aichord stop: an agent can't stop itself (its own tab would close in the middle of this command). The "
                 "user can quit its tool, or another agent can run this.")
    if in_sandbox():
        sys.exit("aichord stop: Codex's sandbox can't reach tmux, so nothing was stopped. Ask an agent in Claude Code or "
                 "Antigravity, or the user, to run it.")
    root = project_root()
    conn = db(root, create=False)
    lock = take_lock(state_dir(root) / "runtime" / "up.lock", wait=15)  # noqa: F841 (no spawn, up or restart meanwhile)
    if lock is None:
        sys.exit("aichord stop: an aichord up, restart, spawn or stop is still running for this project. Run it again.")
    mark_exited(conn)
    if agent_row(conn, name) is None:
        group = [r["name"] for r in agents(conn) if r["tool"] == name]
        sys.exit(f"aichord stop: there is no agent {name} in this project." + (
            f" Name one of the agents in {TOOL_LABEL[name]}: {names_line(group)}." if group else ""))
    hold = take_lock(agent_lock(name), wait=10)  # its running hooks finish first; later ones of this tool write nothing
    if hold is None:
        sys.exit(f"aichord stop: {name}'s hooks were still running after 10 s, so nothing was stopped. Run it again.")
    try:
        conn.execute("BEGIN IMMEDIATE")  # decided on the agent as it is now, with none of its hooks running
        try:
            a = agent_row(conn, name)
            tab = agent_tab(name, root, a["pane"])
            if a["state"] in ("busy", "waiting") and not args.force:
                sys.exit(f"aichord stop: {name} is working ({a['detail'] or a['state']}); stopping it stops that work, "
                         "and its conversation won't finish it on its own. Run it again with --force to stop it anyway.")
            running = running_pane(conn, name)
            if running and running != tab:  # a tab aichord didn't open for it, such as the one `aichord up` ran in
                sys.exit(f"aichord stop: {name} runs in a tab aichord didn't open for it (pane {running}), so it isn't "
                         "closed from here: quit its tool in that tab, then run this again.")
            target = tab  # runs only this agent, or shows it closed: never anything of the user's
            stopped = now()
            conn.execute("UPDATE agents SET state='gone', detail=?, updated=?, launched=? WHERE name=?",
                         ("left the team" if args.drop else "stopped with aichord stop", stopped, stopped, name))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        hold.close()
    closing = ([target] if target and target != os.environ.get("TMUX_PANE", "") else []) + closed_tabs(name, root, target)
    if closing:
        close_panes(closing)  # every process in them exits before the agent can leave the team
    owed: list[int] = []
    replaced = False
    if args.drop:
        conn.execute("BEGIN IMMEDIATE")
        try:  # only the agent this stop took over: a launch since then keeps it on the team
            replaced = conn.execute("DELETE FROM agents WHERE name=? AND launched=?", (name, stopped)).rowcount == 0
            if not replaced:
                owed = close_owed(conn, [name])
                meta_set(conn, f"left:{name}", repr(stopped))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    label = TOOL_LABEL.get(a["tool"], a["tool"])
    if replaced:
        sys.exit(f"aichord stop: {name} was started again while it was being stopped, so it stays on the team.")
    said = (f"{name} ({label}) " + ("left the team" if args.drop else
            f"was stopped; aichord spawn {name} brings it back in its own conversation") + "."
            + (f" The questions it still owed are closed for it: {', '.join(f'#{i}' for i in owed)}." if owed else ""))
    post(conn, me(), ["team"], {n: "context" for n in live_names(conn, exclude=me())}, "note", said,
         subject=f"{name} {'left the team' if args.drop else 'stopped'}")
    print(said + ("" if target or not a["pane"] else " aichord didn't open its tab, so nothing was closed."))


# ---- presence and wake-up paths -------------------------------------------------------------

SHELLS = {"zsh", "bash", "sh", "fish", "dash", "tcsh", "ksh", "login"}


def tmux_out(*a: str) -> str:
    try:
        out = subprocess.run(["tmux", *a], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def session_name(root: Path) -> str:
    """The tmux session name for a project: its last two folders, for example work/myapp. A "." or ":"
    becomes "_": tmux 3.7 accepts them in a name, but reads them as separators in -t session:window.pane."""
    return re.sub(r"[.:]", "_", "/".join(p for p in root.parts[-2:] if p != root.anchor))


def name_session(root: Path, here: str) -> str:
    """Name the tmux session `aichord up` runs in after the project, so `tmux ls` tells several teams apart. Only a
    session still called by tmux's own number (its id $N is named N) is renamed, so a name the user gave stays; when
    another session has the name, this one gets "-2", "-3" and so on, also when another start takes it at the same
    moment. The status bar's left part, which shows "[name] ", is widened for this session if it would cut the name
    (tmux's default is 10 characters). Returns the new name, or ""."""
    found = tmux_out("display-message", "-p", *(["-t", here] if here else []),
                     "#{session_id}\t#{session_name}\t#{status-left-length}").strip().split("\t")
    sid, current, width = (found + ["", "", ""])[:3]
    base = session_name(root)
    if not sid or current != sid.lstrip("$") or not base:
        return ""
    for _ in range(3):
        taken = set(tmux_out("list-sessions", "-F", "#{session_name}").splitlines())
        name, n = base, 1
        while name in taken:
            n += 1
            name = f"{base}-{n}"
        # tmux expands #{...} in a new name, so a "#" from a folder name is doubled to stay as it is
        if subprocess.run(["tmux", "rename-session", "-t", sid, name.replace("#", "##")], capture_output=True).returncode == 0:
            break
        if tmux_out("display-message", "-p", "-t", sid, "#{session_name}").strip() != current:
            return ""  # gone, or named meanwhile by someone else
    else:
        return ""
    if width.isdigit() and int(width) < len(name) + 3:
        subprocess.run(["tmux", "set-option", "-t", sid, "status-left-length", str(len(name) + 3)], capture_output=True)
    return name


PANE_FIELDS = ("pane_id", "pane_dead", "pane_current_command", "pane_pid", "pane_title", "pane_current_path")


def tmux_panes() -> dict[str, dict[str, str]]:
    """Every tmux pane by id: dead, current_command, pid (its shell), title, current_path. Empty when tmux can't be
    asked."""
    panes = {}
    for line in tmux_out("list-panes", "-a", "-F", "\t".join("#{%s}" % f for f in PANE_FIELDS)).splitlines():
        values = line.split("\t")
        if len(values) == len(PANE_FIELDS):
            panes[values[0]] = {f.removeprefix("pane_"): v for f, v in zip(PANE_FIELDS, values)}
    return panes


def in_project(pane: dict[str, str], root: Path) -> bool:
    """Whether the pane runs in this project's folder. Pane ids start again with a new tmux server, so a recorded id
    can name another project's pane."""
    return os.path.realpath(pane["current_path"]) == os.path.realpath(root)


LAUNCH_GRACE = 30  # seconds a new tab may still show its shell before the `aichord launch` typed into it starts


def pane_runs_agent(a, panes: dict, root: Path) -> bool:
    """Whether the agent's recorded pane still runs it: open, in this project, with a program other than a shell in
    front. Its shell counts too within LAUNCH_GRACE of a launch typed into a new tab: two spawns in a row once both
    took the first one's new agent, still at its shell, for one that had stopped, and launched it twice."""
    p = panes.get(a["pane"]) if a["pane"] else None
    if p is None or p["dead"] == "1" or (p["current_path"] and not in_project(p, root)):
        return False
    return p["current_command"].lstrip("-") not in SHELLS or (
        a["state"] == "starting" and now() - (a["updated"] or 0) < LAUNCH_GRACE)


def mark_exited(conn) -> None:
    """Mark agents gone whose tmux pane has closed, is back at a shell prompt, or is now another project's. Outside
    tmux, or when tmux can't be asked (Codex's sandbox), nothing changes."""
    if not os.environ.get("TMUX"):
        return
    panes = tmux_panes()
    if not panes:
        return
    root = project_root()
    for a in agents(conn, live_only=True):
        if a["pane"] and not pane_runs_agent(a, panes, root):
            set_state(conn, a["name"], "gone", detail="its tool has exited")


def take_lock(path: Path, wait: float = 0.0, shared: bool = False):
    """A lock on `path`, held while the returned file stays open: exclusive, or shared with other shared holders.
    None if it can't be had within `wait` seconds (a status check holds a lock only for an instant)."""
    f = open(path, "a+")
    give_up = now() + wait
    while True:
        try:
            fcntl.flock(f, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if now() >= give_up:
                f.close()
                return None
            time.sleep(0.1)


def lock_held(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        f = take_lock(path)
    except OSError:  # unreadable: say not held rather than fail a command that has already stored its message
        return False
    if f is None:
        return True
    f.close()
    return False


def checker_lock(agent: str) -> Path:
    """Held by the agy agent's `aichord watch` while it runs."""
    return state_dir() / "runtime" / f"watch-{agent}.lock"


def wake_path(a, conn=None) -> str:
    """How new mail can start a turn for this agent while it is idle, as things stand now."""
    name = a["name"]
    if a["tool"] == "agy":
        return "aichord watch" if lock_held(checker_lock(name)) else "none (aichord watch is not running)"
    if a["tool"] == "codex":
        if not a["session_id"]:
            return "none yet (codex queue needs the session's first turn)"
        how = "codex queue"
    else:
        why = claude_unwakeable(conn, name) if conn is not None else ""
        if why:
            return f"none ({why})"
        how = "its message socket"
    if conn is not None:
        if wake_waiting(conn, name):
            return f"{how} (a wake-up sent at {hhmm(wake_waiting(conn, name))} hasn't started its turn yet)"
        last, error = float(meta_get(conn, f"last_wake:{name}") or 0), meta_get(conn, f"wake_error:{name}")
        if last and error:
            return f"{how} (the last wake-up, at {hhmm(last)}, failed: {clip(error, 120)}; the next aichord command tries again)"
    return how


# ---- Antigravity (agy) --------------------------------------------------------------------
# agy 1.4 facts, from experiments:
# - Hooks live in ~/.gemini/config/hooks.json or <workspace>/.agents/hooks.json, keyed by a hook name; there is no
#   per-session flag. Events: SessionStart (fires at the first message), PreInvocation (before every model call),
#   PostInvocation, PreToolUse, PostToolUse, Stop. A PreToolUse hook that prints nothing blocks the tool, so aichord
#   doesn't use it. PostToolUse output never reaches the model.
# - No hook receives the typed prompt; the transcript (path given to every hook) records it as a USER_INPUT step with
#   source USER_EXPLICIT. Steps injected by hooks have source SYSTEM_SDK.
# - Output: {"injectSteps":[{"systemMessage":{"systemMessage": ...}}]} adds a system message; Stop continues with
#   {"decision":"continue","reason": ...}. Stop also fires while agy waits for its own helpers.
# - `agy -i PROMPT` starts an interactive session with a first prompt.
# - `agy agentapi send-message` works only from inside agy (it needs agy's local address and token), so an outside
#   process can't wake agy. A background command that agy started does wake it when it finishes: `aichord watch`.
#   Its result reaches the model only once the model stops calling tools.
# - Waiting for helpers or background tasks ends the turn (Stop fires). Each one finishing arrives as a SYSTEM
#   message with "sender=<its id>" and starts a new model call.

AGY_HOOKS_FILE = Path.home() / ".gemini" / "config" / "hooks.json"
WATCH_AGAIN = (f"{MARK} Your aichord watch isn't running, so new messages can't wake you. Run aichord inbox, then "
               "start aichord watch again as a background command.")


def emit_agy(kind: str, text: str) -> int:
    if text and kind == "stop":
        print(json.dumps({"decision": "continue", "reason": text}))
    elif text and kind in ("start", "prompt"):
        print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": text}}]}))
    else:
        print("{}")
    return 0


def agy_steps(path: str, offset: int) -> tuple[list[dict], int]:
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1  # only complete lines
    steps = []
    for line in data[:end].splitlines():
        try:
            steps.append(json.loads(line))
        except ValueError:
            continue
    return steps, offset + end


def agy_request_text(step: dict) -> str:
    content = str(step.get("content", ""))
    m = re.search(r"<USER_REQUEST>\n?(.*?)\n?</USER_REQUEST>", content, re.S)
    return (m.group(1) if m else content).strip()


# A background task's start says "task id: X" and a helper's start lists "conversationId": "X"; the system message
# that reports either one finishing says "sender=X". (Listing the running helpers repeats their ids, so only the
# steps that start something count.) One helper is "Created the following subagent:", several "...subagents:": in
# one set of agy's transcripts the singular came 785 times and the plural 121, and matching only the plural missed
# most helpers.
AGY_START_STEP = re.compile(r"(?m)^(Created the following subagents?:|Tool is running as a background task with task id:)")
AGY_STARTED = re.compile(r'task id: (\S+)|"conversationId":\s*"([^"]+)"')
AGY_FINISHED = re.compile(r"sender=(\S+)")


def agy_new_events(conn, agent: str, path: str) -> list[tuple[str, str]]:
    """What appeared in agy's transcript since the last call, in order: ("prompt", text) for typed input,
    ("started", id) or ("started_watch", id) for a background task or helper being started, ("finished", id), and
    ("compacted", step) when Antigravity replaced the conversation with a summary (a CHECKPOINT step: each of the 16
    compactions in one long transcript was one). A transcript that was replaced or rewritten is read again from its
    start, and then only steps numbered after the last one handled count."""
    if not path or not os.path.exists(path):
        return []
    key = f"agy_offset:{agent}:{path}"
    saved = meta_get(conn, key)
    first_sight = saved == ""
    high_key = f"agy_step:{agent}:{path}"
    known = meta_get(conn, high_key)
    steps, mark, rewritten = agy_read(path, saved)
    meta_set(conn, key, mark)
    high = int(known) if known else -1
    events = []
    for s in steps:
        number = s.get("step_index")
        if isinstance(number, int):
            if rewritten and number <= high:
                continue  # handled before the rewrite
            high = max(high, number)
        elif rewritten:
            continue  # no number: can't tell whether it is new
        content = str(s.get("content", ""))
        if s.get("type") == "USER_INPUT" and s.get("source") == "USER_EXPLICIT":
            events.append(("prompt", agy_request_text(s)))
        elif s.get("type") == "GENERIC" and AGY_START_STEP.search(content):
            kind = "started_watch" if re.search(r"Task Description: .*\baichord watch\b", content) else "started"
            events += [(kind, a or b) for a, b in AGY_STARTED.findall(content)]
        elif s.get("type") == "SYSTEM_MESSAGE" and s.get("source") == "SYSTEM":
            events += [("finished", x) for x in AGY_FINISHED.findall(content)[:1]]
        elif s.get("type") == "CHECKPOINT":
            events.append(("compacted", str(number if isinstance(number, int) else "")))
    meta_set(conn, high_key, str(high))
    if first_sight:  # only the prompt being answered now; earlier tasks have no known owner, and the start note
        events = [e for e in events if e[0] == "prompt"][-1:]  # comes anyway with a new conversation
    return events


def agy_read(path: str, saved: str) -> tuple[list[dict], str, bool]:
    """From one read of one open file: the complete steps after the saved point, the new mark (offset, file
    identity and a hash of the 256 bytes before it, taken from the bytes read), and whether the file had been
    replaced, shortened or rewritten so it was read from its start. A mark with only the offset is taken as it is.
    Accepted limit: a rewrite that keeps the same file and length and changes only bytes before the last 256 read
    goes unnoticed. Antigravity appends: one long record kept all 3,472 steps, with each of its 16 compactions in
    place, and a whole rewrite (its changelog mentions one during compaction) changes the file or its length."""
    parts = saved.split(":") if saved else []
    old = int(parts[0]) if parts and parts[0].isdigit() else None
    digest_of = lambda b: hashlib.sha256(b).hexdigest()[:16]  # noqa: E731
    base = 0 if old is None else max(0, old - 256)
    with open(path, "rb") as f:
        ino = os.fstat(f.fileno()).st_ino
        f.seek(base)
        chunk = f.read()
    ok = old is not None and base + len(chunk) >= old
    if ok and len(parts) == 3:
        ok = parts[1] == str(ino) and parts[2] == digest_of(chunk[max(0, old - 256) - base:old - base])
    if not ok and base:  # not the file read before: read it all, once more
        base = 0
        with open(path, "rb") as f:
            ino = os.fstat(f.fileno()).st_ino
            chunk = f.read()
    offset = old if ok else 0
    data = chunk[offset - base:]
    cut = data.rfind(b"\n") + 1  # only complete lines
    consumed = chunk[:offset - base + cut]
    mark = f"{offset + cut}:{ino}:{digest_of(consumed[-256:])}"
    parse = lambda raw: [x for x in (agy_json(line) for line in raw.splitlines()) if x is not None]  # noqa: E731
    return parse(data[:cut]), mark, bool(saved) and not ok


def agy_json(line: bytes):
    try:
        return json.loads(line)
    except ValueError:
        return None


def agy_follow_turns(conn, agent: str, events: list[tuple[str, str]]) -> list[int | None]:
    """Keep track of agy's turns from its transcript. Antigravity ends a turn whenever agy waits for its helpers or
    background tasks, and each one finishing starts a new model call that goes on with the work it was started for
    (recorded at its start), even when a peer's turn came in between. A finished `aichord watch` starts a peer's
    turn, unless the mail it ended for brings replies to agy's own questions. Returns the numbers of the user's
    messages it recorded."""
    running = json.loads(meta_get(conn, f"agy_running:{agent}") or "{}")  # helper or task id -> [started, whose work]
    watches = json.loads(meta_get(conn, f"agy_watches:{agent}") or "[]")
    mids, prompted = [], False
    for kind, value in events:
        if kind == "prompt":
            mids.append(record_prompt(conn, agent, value))
            prompted = True  # this model call answers the user; a finish read in the same batch doesn't change that
        elif kind == "started_watch":
            watches = (watches + [value])[-20:]
        elif kind == "started":
            running[value] = [now(), agent_row(conn, agent)["turn_from"]]
        elif value in running:
            owner = running.pop(value)[1]
            if not prompted:
                conn.execute("UPDATE agents SET turn_from=? WHERE name=?", (owner, agent))
        elif value in watches or (not watches and (new_mail(conn, agent) or meta_get(conn, f"watch_ended:{agent}"))):
            # A watch ended (also one whose start aichord didn't see, if no watch it saw starting is still out): the
            # turn goes on with the work its mail is for, recorded by the watch when it ended (for an older watch,
            # the mail still unread).
            if value in watches:
                watches.remove(value)
            owner = meta_get(conn, f"watch_ended:{agent}") or work_of(conn, unseen_mail(conn, agent)) or "own"
            meta_set(conn, f"watch_ended:{agent}", "")
            if owner != "own" and not prompted:
                conn.execute("UPDATE agents SET turn_from=? WHERE name=?", (owner, agent))
        elif meta_get(conn, f"turn_msg:{agent}") and not prompted:  # started before aichord first read this
            conn.execute("UPDATE agents SET turn_from='user' WHERE name=?", (agent,))  # transcript: the user's
    running = {k: v for k, v in running.items() if now() - v[0] < HELPER_QUIET}  # one that never reports back stops counting
    meta_set(conn, f"agy_running:{agent}", json.dumps(running))
    meta_set(conn, f"agy_watches:{agent}", json.dumps(watches))
    return mids


HELPER_QUIET = 3600  # seconds after its start (agy) or last step (codex) a helper that never reported back stops counting


def helpers_running(conn, agent: str) -> bool:
    """Whether the agent still waits for a helper or background task it started: agy's, seen in its transcript;
    codex's, from Codex's helper hooks. (The record keeps the name it had when only agy's were tracked.)"""
    return any(now() - v[0] < HELPER_QUIET for v in json.loads(meta_get(conn, f"agy_running:{agent}") or "{}").values())


def agy_last_answer(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    size = os.path.getsize(path)
    steps, _ = agy_steps(path, max(0, size - 400_000))
    answer = ""
    for s in steps:
        if s.get("type") == "USER_INPUT" and s.get("source") == "USER_EXPLICIT":
            answer = ""
        elif s.get("type") == "PLANNER_RESPONSE" and isinstance(s.get("content"), str) and s["content"].strip():
            answer = s["content"]
    return answer


def agy_launch_command(name: str, root: Path, extra: list[str], resume: str = "", prompt: str = FIRST_PROMPT) -> list[str]:
    if not AGY_HOOKS_FILE.exists() or '"aichord"' not in AGY_HOOKS_FILE.read_text():
        print("aichord: agy hooks are not installed, so the agent in Antigravity won't see team messages. Run `aichord install` first.",
              file=sys.stderr)
        time.sleep(3)
    flags = config().get("agy_flags") or []  # the user's choice, such as running without prompts
    flags = shlex.split(flags) if isinstance(flags, str) else [str(f) for f in flags] if isinstance(flags, list) else []
    return ["agy", *[f for f in flags if f not in extra], *extra, *(["--conversation", resume] if resume else []),
            "-i", prompt]


def agy_hooks_entry(python: str | None = None) -> dict:
    t = aichord_cmd(python)
    return {ev: [{"command": f"{t} hook agy {ev}"}] for ev in HOOK_EVENTS["agy"]}


# ---- agy's background checker (works in any tool) ------------------------------------------


def cmd_watch(args) -> None:
    """Run as a background command. Ends when new mail arrives, printing only a hint: the mail stays unread in the
    inbox, so a dropped or cut-off result loses nothing. Ends quietly if the agent exits or is launched again."""
    conn, agent = db(), me()
    if agent == "user":
        sys.exit("aichord watch is for agents (it needs AICHORD_AGENT).")
    lock = take_lock(checker_lock(agent), wait=1)
    if lock is None:
        print("An aichord watch is already running for you; nothing to do.")
        return
    meta_set(conn, f"reminded:{agent}", "")
    first = agent_row(conn, agent)
    while True:
        r = agent_row(conn, agent)
        if not first or not r or r["state"] == "gone" or r["launched"] != first["launched"]:
            return
        mail = unseen_mail(conn, agent)  # one look, so a hook taking the mail meanwhile can't split it
        if mail:
            # What it ended for decides whose work the turn it starts is on (agy_follow_turns): replies go on with
            # the work their questions were asked for. Recorded now, because a hook may show the mail before the
            # finish reaches agy's transcript.
            meta_set(conn, f"watch_ended:{agent}", work_of(conn, mail) or "own")
            print(f"{MARK} New mail: run aichord inbox, then start aichord watch again as a background command.")
            return
        time.sleep(1.0)


# ---- install -------------------------------------------------------------------------------

SKILL_LINKS = {
    "Claude Code": Path.home() / ".claude" / "skills" / "aichord",
    "Codex": Path.home() / ".codex" / "skills" / "aichord",
    "Antigravity": Path.home() / ".gemini" / "config" / "skills" / "aichord",
}
WRAPPER = Path.home() / ".local" / "bin" / "aichord"


def stable_python() -> str:
    """A uv-managed interpreter that survives uv cache cleaning (sys.executable may be a cached script env)."""
    uv = shutil.which("uv")
    clean_env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV" and not k.startswith("UV_")}
    for ver in ("3.12", "3.13", "3.11"):
        if uv:
            out = subprocess.run([uv, "python", "find", "--managed-python", "--no-project", ver], capture_output=True,
                                 text=True, cwd=Path.home(), env=clean_env)
            found = out.stdout.strip()
            if out.returncode == 0 and found and not re.search(r"\d+\.\d+\.\d+(a|b|rc)\d", found):
                return found
    return getattr(sys, "_base_executable", sys.executable)


def cmd_install(args, remove: bool) -> None:
    dry = args.dry_run
    say = lambda s: print(("[dry run] " if dry else "") + s)  # noqa: E731
    if remove:
        if WRAPPER.exists() and str(SCRIPT) in WRAPPER.read_text():
            say(f"remove {WRAPPER}")
            dry or WRAPPER.unlink()
        for tool, link in SKILL_LINKS.items():
            if link.is_symlink() and link.resolve() == SKILL_DIR:
                say(f"remove skill link for {tool}: {link}")
                dry or link.unlink()
        if AGY_HOOKS_FILE.exists():
            cfg = json.loads(AGY_HOOKS_FILE.read_text() or "{}")
            if "aichord" in cfg:
                say(f"remove the 'aichord' entry from {AGY_HOOKS_FILE}")
                if not dry:
                    cfg.pop("aichord")
                    AGY_HOOKS_FILE.write_text(json.dumps(cfg, indent=2) + "\n")
        return
    py = stable_python()
    wrapper = f'#!/bin/sh\n# aichord command (written by `aichord install`). Runs a uv-managed Python directly so it also\n# works inside Codex\'s sandbox, where `uv run` cannot write its cache.\nexec "{py}" -I "{SCRIPT}" "$@"\n'
    say(f"write {WRAPPER} (runs {SCRIPT} with {py})")
    if not dry:
        WRAPPER.parent.mkdir(parents=True, exist_ok=True)
        WRAPPER.write_text(wrapper)
        WRAPPER.chmod(0o755)
    for tool, link in SKILL_LINKS.items():
        target = Path(os.path.normpath(link.parent / os.readlink(link))) if link.is_symlink() else None
        if target == SKILL_DIR:
            say(f"skill link for {tool} already in place: {link}")
            continue
        if link.exists() or link.is_symlink():
            say(f"skip {link}: something else is already there")
            continue
        say(f"link the skill for {tool}: {link} -> {SKILL_DIR}")
        if not dry:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(SKILL_DIR)
    cfg = json.loads(AGY_HOOKS_FILE.read_text() or "{}") if AGY_HOOKS_FILE.exists() else {}
    entry = agy_hooks_entry(py)
    if cfg.get("aichord") == entry:
        say(f"agy hooks already in place in {AGY_HOOKS_FILE}")
    else:
        say(f"{'update the' if 'aichord' in cfg else 'add an'} 'aichord' entry in {AGY_HOOKS_FILE} (its hooks do nothing "
            "outside sessions started by `aichord launch`)")
        if not dry:
            if AGY_HOOKS_FILE.exists():
                shutil.copy2(AGY_HOOKS_FILE, AGY_HOOKS_FILE.with_suffix(".json.bak"))
            AGY_HOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
            cfg["aichord"] = entry
            AGY_HOOKS_FILE.write_text(json.dumps(cfg, indent=2) + "\n")
    say("Claude Code and Codex get their hooks per session from `aichord launch` (nothing global changes).")
    say("Codex asks once, at the first `aichord launch` of the Codex agent, whether to trust the aichord hooks: choose 'Trust all'.")


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------

HELP = """aichord: messages between the AI agents working on this project.

Agents (and the user, from any shell):
  aichord ask [--to a,b] [--subject S] [--no-wait] [--wait SECONDS] [TEXT]   ask; waits for replies (90 s
                                                         unless --wait says otherwise, at most 540)
  aichord wait [N ...] [--wait SECONDS]                    wait again for replies (90 s by default, at most 540)
  aichord reply N [TEXT]                                   answer message N
  aichord send [--to a,b] [--wake] [TEXT]                  a note that needs no reply; it waits for each recipient's
                                                         next step, unless --wake starts an idle agent's turn
  aichord inbox | read N ... | thread N                    unread messages and unanswered questions; one message; a thread
  aichord decide [--supersedes D3,D5] [--thread N] TEXT    record a numbered decision; --supersedes lists the decisions
                                                         it replaces
  aichord decide --retire D3,D5 WHY                        take decisions out of force (a finished task, a rule the
                                                         skill states); they stay in decisions.md, marked retired
  aichord close N TEXT                                     finish thread N that nobody needs to answer, with a reason
  aichord track [NAME [--rev R] [--focus|--park|--done] [TEXT]]   research tracks: list them, show one, or write
                                                         one (the user's intent in their words, each claim with its #N)
  aichord track --reviewed N                               the user's messages through #N are folded into the tracks
  aichord recall PATTERN [--limit N]                       search every decision and message
  aichord code [--to NAME] [TEXT]                          a coding task (default: the agent that writes code;
                                                         with several, name one). --to takes agent names, or a
                                                         tool's name for every agent in it (--to codex)
  aichord review [--base REF] [--include PATH] [--to a,b] [TEXT]   review request with a working-tree snapshot
                                                         (--include: only these untracked paths)
  aichord status | tail                                    who is busy or idle; live log
  aichord glossary [add TERM MEANING]                      project terms
  aichord watch                                            (agy) run in the background; ends, waking you, on new mail
The user:
  aichord up [--team T] [--new-tab] [--no-tail] [--update|--no-update]   start the team: the first agent in this
                                                         tmux tab, the others in new tabs, plus a log tab. --team: tools,
                                                         repeated for more agents in one (claude,claude,codex makes
                                                         claude-0, claude-1, codex-0), or NAME:TOOL; default
                                                         claude,agy,codex, or the team setting in
                                                         ~/.aichord/config.json (--new-tab: leave this tab alone); a tmux
                                                         session still named by number is renamed after the project's
                                                         last two folders. --update updates the tools first
                                                         (update_tools in ~/.aichord/config.json does it every time)
  aichord restart [--new-tab] [--no-tail] [--update|--no-update]   close this project's agent and log tabs (never
                                                         this one) and start every agent again in its same
                                                         conversation. Run it from a new tab, or from the tab you
                                                         ran aichord up from, after quitting its agent's tool there
                                                         (that agent comes back there)
  aichord spawn TOOL|NAME[:TOOL] [--fresh] [-- ARGS]       start an agent in a new tab while the team runs (agents
                                                         can run it too, except inside Codex's sandbox): spawn codex
                                                         brings back a codex agent that stopped, or starts the next
                                                         one (codex-1, codex-2, ...); --fresh:
                                                         a new agent (or, for a NAME, a new conversation); ARGS go to
                                                         the tool
  aichord stop NAME [--drop] [--force]                     stop an agent and close the tab aichord opened for it;
                                                         --drop takes it off the team; --force stops one that works
  aichord launch NAME [--tool claude|agy|codex] [--resume ID] [-- ARGS]   start one agent in the current pane
  aichord say [--to a,b] TEXT                              post to the team as the user
  aichord install [--dry-run] | uninstall                  set up the command, skill links and hooks
Text can be given as arguments or on stdin (use a heredoc for multi-line text).
"""


def command_help(cmd: str) -> str:
    """The lines of HELP about one command (`aichord decide --help`), or all of it."""
    out, keep = [], False
    for line in HELP.splitlines():
        text = line.strip()
        if text.startswith("aichord "):
            usage = text.split("   ")[0]
            keep = bool(cmd) and re.search(rf"(^aichord |\| ){re.escape(cmd)}\b", usage) is not None
        elif not line.startswith(" " * 20):
            keep = False
        if keep:
            out.append(line)
    return "\n".join(out) if out else HELP


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aichord", add_help=False)
    sub = p.add_subparsers(dest="cmd")

    def text_cmd(name: str, **kw):
        sp = sub.add_parser(name, add_help=False, **kw)
        sp.add_argument("text", nargs="*")
        return sp

    for name in ("ask", "code", "review"):
        sp = text_cmd(name)
        sp.add_argument("--to")
        sp.add_argument("--subject", "-s")
        sp.add_argument("--no-wait", action="store_true")
        sp.add_argument("--wait", type=int, default=WAIT_SECONDS)
        sp.add_argument("--ref", default="")
        if name == "review":
            sp.add_argument("--base")
            sp.add_argument("--include", action="append")
    sp = text_cmd("send")
    sp.add_argument("--to")
    sp.add_argument("--subject", "-s")
    sp.add_argument("--wake", action="store_true")
    sp = sub.add_parser("reply", add_help=False)
    sp.add_argument("id", type=int)
    sp.add_argument("text", nargs="*")
    sp = sub.add_parser("wait", add_help=False)
    sp.add_argument("threads", nargs="*", type=int)
    sp.add_argument("--wait", "--max", dest="max", type=int, default=WAIT_SECONDS)  # the same name as on ask
    sub.add_parser("inbox", add_help=False)
    sp = sub.add_parser("read", add_help=False)
    sp.add_argument("ids", nargs="+", type=int)
    sp = sub.add_parser("thread", add_help=False)
    sp.add_argument("id", type=int)
    sp = sub.add_parser("track", add_help=False)
    sp.add_argument("name", nargs="?")
    sp.add_argument("text", nargs="*")
    sp.add_argument("--rev", type=int)
    sp.add_argument("--reviewed", type=int)
    status = sp.add_mutually_exclusive_group()
    for flag in ("--focus", "--park", "--done"):
        status.add_argument(flag, action="store_true")
    sp = text_cmd("decide")
    sp.add_argument("--thread", type=int)
    sp.add_argument("--supersedes", help="decisions this one replaces, e.g. D3 or D3,D5")
    sp.add_argument("--retire", help="decisions to take out of force without a new one, e.g. D3,D5; the text says why")
    sp = sub.add_parser("close", add_help=False)
    sp.add_argument("id", type=int)
    sp.add_argument("text", nargs="*")
    sp = sub.add_parser("recall", add_help=False)
    sp.add_argument("pattern")
    sp.add_argument("--limit", type=int, default=20)
    sp = text_cmd("say")
    sp.add_argument("--to")
    sub.add_parser("status", add_help=False)
    sp = sub.add_parser("tail", add_help=False)
    sp.add_argument("-n", type=int, default=20)
    sp.add_argument("--no-follow", action="store_true")
    sp = sub.add_parser("glossary", add_help=False)
    sp.add_argument("op", nargs="?", choices=["add"])
    sp.add_argument("term", nargs="?")
    sp.add_argument("meaning", nargs="*")
    sp = sub.add_parser("hook", add_help=False)
    sp.add_argument("tool", choices=list(TOOL_LABEL))
    sp.add_argument("event")
    sub.add_parser("watch", add_help=False)
    sp = sub.add_parser("spawn", add_help=False)
    sp.add_argument("spec")
    sp.add_argument("--fresh", action="store_true")
    sp = sub.add_parser("stop", add_help=False)
    sp.add_argument("name")
    sp.add_argument("--drop", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp = sub.add_parser("launch", add_help=False)
    sp.add_argument("name")
    sp.add_argument("--tool", choices=list(TOOL_LABEL))
    sp.add_argument("--resume", default="")
    sp.add_argument("--versions-changed", default="")
    for name in ("up", "restart"):  # a restart's team is the one recorded
        sp = sub.add_parser(name, add_help=False)
        if name == "up":
            sp.add_argument("--team")
        sp.add_argument("--no-tail", action="store_true")
        sp.add_argument("--new-tab", action="store_true")
        sp.add_argument("--update", action="store_true")
        sp.add_argument("--no-update", action="store_true")
    sp = sub.add_parser("install", add_help=False)
    sp.add_argument("--dry-run", action="store_true")
    sp = sub.add_parser("uninstall", add_help=False)
    sp.add_argument("--dry-run", action="store_true")
    sub.add_parser("help", add_help=False)

    argv = list(sys.argv[1:] if argv is None else argv)
    tool_args: list[str] = []
    if argv[:1] in (["launch"], ["spawn"]) and "--" in argv:  # everything after "--" goes to the tool itself
        cut = argv.index("--")
        argv, tool_args = argv[:cut], argv[cut + 1:]
    if any(a in ("-h", "--help") for a in argv):
        print(command_help(argv[0] if argv and not argv[0].startswith("-") else ""))
        return 0
    args = p.parse_args(argv)
    if args.cmd in ("launch", "spawn"):
        args.extra = tool_args
    if args.cmd in (None, "help"):
        print(HELP)
        return 0
    if args.cmd == "hook":
        return hook_main(args.tool, args.event)
    handlers = {
        "ask": lambda a: cmd_ask(a, "ask"), "code": lambda a: cmd_ask(a, "code"),
        "review": lambda a: cmd_ask(a, "review"), "send": cmd_send, "reply": cmd_reply, "wait": cmd_wait,
        "inbox": cmd_inbox, "read": cmd_read, "thread": cmd_thread, "decide": cmd_decide, "say": cmd_say,
        "close": cmd_close, "recall": cmd_recall, "track": cmd_track,
        "status": cmd_status, "tail": cmd_tail, "glossary": lambda a: cmd_glossary(argparse.Namespace(
            term=a.term if a.op == "add" else None, meaning=a.meaning)),
        "launch": cmd_launch, "up": cmd_up, "restart": lambda a: cmd_up(a, restart=True),
        "watch": cmd_watch, "spawn": cmd_spawn, "stop": cmd_stop,
        "install": lambda a: cmd_install(a, remove=False), "uninstall": lambda a: cmd_install(a, remove=True),
    }
    handlers[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
