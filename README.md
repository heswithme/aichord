# aichord

*A reference implementation for macOS only, tested with Claude Code 2.1.325, Codex 0.169.0, Antigravity CLI 1.5.9 and tmux 3.7 on macOS 27 (arm64). It is not packaged and not supported: read the code before you run it.*

aichord lets several AI coding agents work on one project, each in its own tool and its own tmux tab (a tmux window). The default team is three:

| Agent | Tool | Role |
|---|---|---|
| claude-0 | Claude Code | writes all code and tests |
| agy-0 | Antigravity CLI | big-picture architecture, specs and cross-checks |
| codex-0 | Codex CLI | scientific and formal review: math, invariants, edge cases |

Any other mix works too, such as three agents in Claude Code, two in Codex and two in Antigravity, or one Codex alone (see "Start"). Each agent is named after its tool and numbered from 0. You type to whichever agent you want. That agent leads the turn and consults the others through the `aichord` command, and the others see your messages too. An agent's name says nothing about its model: you choose the model inside each tool. Everything said and decided is stored in `.aichord/` in the project.

The whole tool is one Python file, `scripts/aichord.py`, using only the standard library; its tests are in `tests/test_core.py` (`uv run --no-project python tests/test_core.py`). `SKILL.md` holds the agents' instructions; this file is for you.

## Why

- Different tools and models catch different mistakes. A plan or a change checked by two other agents is often worth more than a longer think by one.
- Without aichord you copy text between tabs by hand. With it the agents ask each other, wait for the answers, review each other's changes against a frozen snapshot, and record what was decided.
- It uses only what the tools already offer: hooks (commands each tool runs at fixed points, such as after a tool call or at the end of a turn) and each tool's own way to start a turn in an idle session. aichord never types into a pane, and nothing of it runs in the background.

## Requirements

- macOS with tmux.
- Python 3.11 or newer. macOS's own `python3` is 3.9, so use uv or install a newer Python.
- Claude Code (2.1.224 or later, for its message socket), Codex CLI and Antigravity CLI, each signed in and on your PATH as `claude`, `codex` and `agy`.
- `~/.local/bin` on your PATH.
- git in the project, for review snapshots. A project without git works, but `aichord review` can't take a snapshot there.

## Install

```
git clone https://github.com/heswithme/aichord.git ~/aichord
uv run --script ~/aichord/scripts/aichord.py install
```

Any Python 3.11 or newer can run the installer instead of uv. `--dry-run` shows the changes first, and `aichord uninstall` undoes them. Install changes four things:
- **`~/.local/bin/aichord`:** the command. It runs the script with a fixed Python interpreter (a uv-managed one when uv is installed) instead of `uv run`, because `uv run` can't write its cache inside Codex's sandbox.
- **Skill links** to the clone, for all three tools: `~/.claude/skills/aichord`, `~/.codex/skills/aichord` and `~/.gemini/config/skills/aichord`. Anything already at one of these paths is left alone.
- **An `aichord` entry in `~/.gemini/config/hooks.json`** (an existing file is first copied to `hooks.json.bak`). Antigravity has no way to set hooks for one session; these hooks do nothing unless the session was started by `aichord launch`.
- **Nothing global for Claude Code and Codex.** They get their hooks per session, from `aichord launch`.

The command and the links point at the clone, so leave it where it is. To move it, run `aichord uninstall` first and install again from the new place.

## Start

Inside tmux, in your project:

```
aichord up
```

The tab you run it from becomes claude-0's tab, followed by tabs for agy-0, codex-0 and the live message log (`--no-tail` skips the log). If your tab is split into several panes, or with `--new-tab`, it is left alone and all the tabs are added after it. Each tab is named after its agent. Switch tabs with your tmux prefix key (Ctrl-b by default) and the tab's number. When Claude Code exits, its tab goes back to your shell. The tabs aichord opens run only their agent: when its tool exits, the tab stays, closed, showing its last output, until you close it, `aichord stop` closes it, or the next `aichord restart` does. `aichord up` refuses while the team is already running; use `aichord restart` then.

A tmux session still called by tmux's own number is renamed after the project's last two folders (for example `work/myapp`), so `tmux ls` tells several teams apart. A name you gave stays.

Each agent starts with a short first prompt from aichord. It begins with "[aichord]", so it is never recorded as yours, and answering it gives each tool its wake-up path from the start. To start one agent in the current pane instead, run `aichord launch claude-0` (or `agy-0`, or `codex-0`); tool flags go after `--`, for example `aichord launch codex-0 -- -m <model>`.

### Other teams, and changing the team while it runs

- **`aichord up --team ...`** starts any mix: tools, repeated for more agents in one, or NAME:TOOL. `--team claude,claude,claude,codex,codex,agy,agy` starts claude-0, claude-1, claude-2, codex-0, codex-1, agy-0 and agy-1; `--team codex` starts one Codex alone (codex-0); `--team lead:claude,math:codex` picks the names. The whole list is checked before anything starts. The `team` setting (see "Settings") makes another mix your default. Agents registered earlier and not in the new team leave it: their messages stay. A team started before agents were numbered keeps names such as claude until then.
- **Who writes code.** The agents in Claude Code write the code; on a team without Claude Code, those in Codex. When several write, each coding task names one of them and its own files.
- **Who is asked.** The agent you type to asks all the others, unless it names fewer; a tool's name, such as `--to codex`, reaches every agent in it. A lone agent answers you itself.
- **Ask any agent to change the team.** "Run another codex" makes it run `aichord spawn codex`, which opens a tab next to the team's and starts one more agent in Codex there: a codex agent that stopped comes back in its own conversation; if every one runs, the next one starts (codex-1, codex-2, ...). `aichord spawn NAME:TOOL` names it; `--fresh` starts a new agent rather than bringing one back (with a NAME, a new conversation); tool flags go after `--`. The others are told it joined, and questions to everyone reach it at once. `aichord stop NAME` closes the tab aichord opened for an agent, found by a mark aichord puts on it, and keeps the agent on the team (`aichord restart` or `aichord spawn NAME` brings it back); `--drop` takes it off the team. An agent in the tab you ran `aichord up` from isn't closed: quit its tool there. You can run all of these from a shell too. Codex's sandbox can't reach tmux, so an agent in Codex asks another agent, or you, to run them.

The first time in each project, each tool asks whether to trust the folder: answer yes. Codex then shows "Hooks need review": choose "Trust all and continue", which it remembers. Codex may also print a startup warning about running without its shared background server; that is expected, because aichord's hooks run inside codex's own session.

## Restart

`aichord restart` brings the whole team back in the same conversations. Run it from a shell: either a new tmux tab in the project, or the tab you ran `aichord up` from, after quitting its agent's tool there (that agent then comes back in this tab). In order, it:
1. Takes the team as recorded: each agent in its tool, on its own conversation. An agent with no recorded conversation starts in a new one only if you say yes.
2. Updates the tools, if you asked for that (see "Settings").
3. Asks first if an agent is in the middle of work. That work stops, and the resumed conversation doesn't finish it on its own.
4. Closes only this project's agent and log tabs, never the tab it runs in. It waits until every process in them has exited and kills whatever still runs after 10 seconds, so two copies of one conversation never run at once.
5. Starts each tool on its recorded conversation (`claude --resume`, `codex resume`, `agy --conversation`), with a first prompt that tells the agent it was restarted.

Messages, open questions and decisions are kept; only the processes are new. If a conversation can't be resumed, that agent stays "starting" in `aichord status`, and the next restart tries again.

## Daily use

- **Type to any agent.** It leads that turn and asks the others before answering anything that isn't trivial (a status check, looking up one fact, an edit that changes no behaviour). Write "solo" to skip that.
- **Everyone sees your messages.** Each message you type is recorded and shown to the other agents; tool commands such as `/compact` are not. The lead's answer is filed after your latest message to it, also when it comes in a later turn, once the replies it waited for have arrived. When you send several messages before it answers, it is told the numbers of the earlier ones too, and `aichord thread N` shows, under each of them, a later answer that came after it. What it tells you while its own question to the others is still open is not shown to the agents it waits for, so its first view doesn't steer them.
- **Post from any shell:** `aichord say "..."` to the whole team, or `aichord say --to codex "..."` to one agent.
- **See what's going on:** `aichord status` (who is busy or idle, how each idle agent can be woken, what is waiting, and what the tab of an agent that looks stuck shows), `aichord tail` (the live log), `aichord inbox` (replies addressed to you) and `aichord recall <pattern>` (search everything ever said or decided).
- **Reviews.** When the agent that writes code finishes a change, it asks the others to review it with `aichord review`. That freezes the working tree, new files included, as a commit kept under `refs/aichord/`, so later edits can't change what is under review. Your files, staging area and branches are not touched. A reviewer's reply must give one of three verdicts (PASS, PASS WITH NITS, BLOCK), and a BLOCK must name a finding that has to be fixed; anything else is refused. At round 3 of a review the author tells you which finding is still open, and you choose: fix it, accept the risk, or stop.
- **Help.** Every command answers `--help`, and `aichord help` lists them all.

## Keeping the big picture through compaction

Tools compact a long conversation into a summary, and a summary loses detail. aichord keeps what matters outside the conversation and gives it back:
- **Research tracks.** One short card (at most 600 characters) per thread of work: your intent in your own words, each claim with the number of the message it rests on, marked in focus, parked or done. The lead keeps them with `aichord track`, and `.aichord/tracks.md` shows them to you. A card written from an older revision is refused, so a stale draft never replaces a newer one.
- **A review mark.** The lead marks your messages as folded into the cards (`aichord track --reviewed N`). Every message of yours after that mark is listed for every agent, so a correction can't slip through.
- **A start note that fits.** After a launch or a compaction each agent gets a start note: who it is and the rules, its open work, the tracks, your messages since the review mark, the decisions in force, recent threads, and then the team's recent discussion in whatever room is left. Claude Code and Codex pass only about 10,000 characters of a hook's output in full, so for them the note is kept to about 9,000 bytes, most important first. Whatever doesn't fit is named, with where to read it.
- **Claude Code's summary is told what to keep.** Its pre-compaction hook asks the summary to keep the tracks and your words word for word. Codex and Antigravity have no such hook; they rely on the start note after the compaction.

## How a message reaches an agent

| | Busy | Idle |
|---|---|---|
| Claude Code | Shown between its steps (hook) | The sender posts the mail to the session's message socket, which starts a turn |
| Codex | Shown between its steps (hook) | The sender runs `codex queue`, whose prompt carries the mail |
| Antigravity | Added before its next model call (hook) | Its own `aichord watch`, a background command, ends with "New mail" and wakes it; it then reads `aichord inbox` |

The command that stores a question, a reply, a review request, a coding task or a message of yours wakes each idle recipient once, by that tool's one path. A note (`aichord send`) waits for the recipient's next step unless it is sent with `--wake`, because Claude Code puts a long notice under every message that arrives on its socket.
- **One wake-up at a time.** A wake-up takes the mail and a reservation in one step, and nothing wakes that agent again until the wake-up's own prompt starts its turn. Claude Code can accept a wake-up and never deliver it, so there the reservation also ends when the agent's next turn does, and the end of that turn shows the mail. A failed wake-up puts the mail back and is tried again 30 seconds later, by whichever aichord command runs next. One whose turn hasn't started after 15 minutes is reported as stalled in `aichord status` and `.aichord/runtime/wake.log`, not repeated, because Codex may first spend minutes compacting a long conversation. Typing anything in that agent's tab starts its turn.
- **Claude Code** delivers a socket message at once because it carries the session's own token. Where a `crossSessionInbound` setting would hold or refuse it, aichord sends nothing, and `aichord status` says so.
- **Inside Codex's sandbox,** codex's commands can reach neither another session's socket nor Codex's server, so their wake-ups go out from codex's next hook, which Codex runs outside the sandbox. An `aichord ask` from codex therefore returns at once and says to run `aichord wait N`.
- **agy's watch** must be started again after each wake-up. If it isn't running when agy stops, agy is reminded once.
- **A forgotten reply.** An agent can end its turn without answering a question it has read, so it is sent back once to reply or to close the question. Not while its own helpers or background commands still run.
- **codex's helpers.** Codex hands a helper's result to its agent without starting a turn, so aichord follows codex's helpers through Codex's helper hooks and wakes codex once to collect their results after the last one finishes. A collection that changes nothing ends with "nothing new", which isn't recorded as an answer.
- **A blocked agent.** An agent at a prompt only you can answer ("trust this folder?"), with a revoked login or at a usage limit sends nothing, so it used to be invisible. aichord now shows the last lines of its tmux tab to whoever asks: in `aichord status` and message receipts for an agent still starting after two minutes or busy without a step for ten (in status also one idle that long while it owes a reply), and, when a wait for a reply runs out, for each agent that still owes one and has been quiet for 45 seconds. It only reads the tab.
- **A closed tab** is noticed by the next aichord command: that agent's messages wait for its next start, and `aichord ask` stops waiting for it.

A question stays open until it is answered with `aichord reply` or its thread is closed with `aichord close`. An agent may type one line by hand into a peer's tab that seems stuck, as `SKILL.md` describes; aichord itself never does.

## Helpers

Each agent hands bounded jobs (searches, reading many files, collecting data, computations) to its own helpers (subagents), and checks what matters before passing it on. Helpers never run `aichord` commands, and only claude's helpers edit project files. They run on each tool's own default model, except Claude Code's, which default to Sonnet: `aichord launch` sets `CLAUDE_CODE_SUBAGENT_MODEL=sonnet` unless it is already set. `helper_models` under "Settings" changes this.

## Files in `.aichord/`

- `log.md`: every message, readable.
- `decisions.md`: numbered decisions (D1, D2, ...), with replaced and retired ones marked.
- `tracks.md`: the research tracks and the review mark.
- `glossary.md`: project terms, added with `aichord glossary add TERM "meaning"`. A message with abbreviations not in it gets a warning.
- `notes/`: long specs and reviews, linked from messages by path.
- `aichord.db`: the SQLite database behind all of it.
- `runtime/`: hook and wake-up logs, locks, and Claude Code's per-session settings.

`.aichord/` is created readable only by you, because it holds every message and a session token for Claude Code's message socket. New projects get a `.aichord/.gitignore` that ignores everything in it, so `git add .` never sweeps the conversation into your repository.

## Settings

All settings are optional, in `~/.aichord/config.json`:

```json
{
  "agy_flags": ["--dangerously-skip-permissions"],
  "helper_models": {"claude": "sonnet", "codex": "<a model your Codex account offers>"},
  "update_tools": true,
  "before_launch": {"agy": ["/path/to/a/script", "arg"]},
  "team": "claude,codex,agy"
}
```

- **`agy_flags`:** extra flags every agy launch gets. By default agy keeps Antigravity's permission prompts, so each `aichord` command agy runs may wait for your approval. `--dangerously-skip-permissions` avoids that, at the cost that agy then runs any command and edits any file without asking. It is your choice.
- **`helper_models`:** the default model of each agent's helpers. For Claude Code, any model name or alias it accepts (default "sonnet"). For Codex, the value is passed as `agents.default_subagent_model` (default: Codex's own). Antigravity has no such setting; agy's instructions in `SKILL.md` name the tiers to ask for.
- **`update_tools`:** before `aichord up` and `aichord restart`, run each tool's own updater (`claude update`, `codex update`, `agy update`), all at once, for at most two minutes. A failed or stuck update doesn't stop the start; a tool that no longer runs after its update does, before anything is started or closed. When a version changed, the first agent checks that every agent can still be woken. `--update` updates once; `--no-update` skips it once.
- **`team`:** the team `aichord up` starts when `--team` isn't given, in the same form (default `claude,agy,codex`).
- **`before_launch`:** a command run before each launch of that tool (`claude`, `codex` or `agy`), for example a local script you keep for your own setup. Its output is shown, and a failure doesn't stop the launch.

`~/.aichord/rules.md` holds your own standing rules, for example how commits are signed or attributed. It is included in every agent's start note, so keep it short.

## Uninstall

`aichord uninstall` (add `--dry-run` to see the changes first) removes the command, the skill links and the `aichord` entry in `~/.gemini/config/hooks.json`. It leaves each project's `.aichord/` and your `~/.aichord/` alone; delete those yourself if you want them gone.

## Known limits

- **agy can't be woken from outside.** Antigravity's own message command works only from inside Antigravity, so agy depends on its `aichord watch`. If agy doesn't start the watch again, its mail waits until its next turn.
- **A failed wake-up waits for the next aichord command.** Nothing runs in the background, so a failed wake-up is tried again only when an agent stops or someone runs `aichord wait` or `aichord status`. If every agent is idle and nobody runs anything, it waits.
- **A wake-up the tool loses waits for the agent's next turn, or its next launch.** Claude Code can accept a wake-up on its socket and never deliver it (seen once, while the session had background helpers running), and a `crossSessionInbound` setting changed while it runs can hold or drop one already sent. The mail is then shown when the agent's next turn ends; until a turn starts, nothing else wakes it. A `codex queue` that gives no answer within 30 seconds counts as queued; if its prompt never runs, `aichord restart` brings the mail back. Either shows as stalled in `aichord status` after 15 minutes.
- **A blocked agent is noticed only when someone runs an aichord command.** Nothing watches in the background: if every agent has stopped, a tab stuck at a prompt stays unseen until you or an agent asks, waits or runs `aichord status`.
- **Answers are filed by time, not by request.** An answer to an older request, given after you wrote again, is filed after your newer message.
- **codex's helper results count as yours once codex paused on your work.** Codex doesn't say which assignment a reused helper is on, so a result collected after codex stopped on your work goes on with your work, even one a peer's task asked for. The worst case is an extra answer in your thread, never a missing one.
- **codex can't take review snapshots,** because Codex's sandbox blocks writes to `.git`. Ask claude or agy to run `aichord review`.
- **Codex's internal helper threads are ignored.** Codex runs memory consolidation and automatic review as separate threads with codex's environment, so aichord ignores hooks from any session other than codex's main one.
- **Each hook starts a short Python process** (about 20 ms), on every tool call.
- **Antigravity compacts long conversations early,** at about 256,000 tokens whatever the model's window (see [Antigravity CLI issue 1245](https://github.com/google-antigravity/antigravity-cli/issues/1245)). It has no compaction hook, so aichord notices the compaction in its conversation record. After any compaction aichord re-sends the start note and the recent discussion, and tells the agent to re-read files before citing them.
- **Roles are kept by instructions only.** Only the instructions in `SKILL.md` keep each agent to its role (for example, that only the agents in Claude Code edit code); nothing in the tools enforces them.
- **Several agents writing code share one working tree.** Nothing stops two of them editing the same file; the instructions give each task its own files. A review snapshot holds the whole tree, including another writer's unfinished edits.

## License

MIT; see `LICENSE`.
