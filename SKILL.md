---
name: aichord
description: Work with the other agents on the same project (agents in Claude Code, Antigravity and Codex, such as claude-0, agy-0 and codex-0) through the `aichord` command - ask, reply, wait, decide, request code or a review. Use it whenever the session started with a "[aichord] You are ..." note, a line starting with "[aichord]" arrives, or the user mentions aichord or the other agents.
---

# aichord: working with the other agents

Several agents work on the same project, each in its own tool and its own tmux tab. The session-start note lists who is on the team. The user can type into any pane. The `aichord` command carries messages between agents and stores everything in `.aichord/` in the project.

You know who you are from the "[aichord] You are ..." note at session start. It's also in `$AICHORD_AGENT`.

## Who does what

The default team:

| Agent | Tool | Role |
|---|---|---|
| agy-0 | Antigravity | Big-picture architecture, specs, cross-checks; explains things in plain English |
| claude-0 | Claude Code | Writes all code, tests and HTML artifacts |
| codex-0 | Codex | Scientific and formal advisor: math, invariants, edge cases, adversarial review |

- **A team can be any mix:** three agents in Claude Code, two in Codex and two in Antigravity, or one Codex alone. Your start note lists the actual team, with each agent's name, tool and role. Roles come with the tool.
- **Names:** each agent is named after its tool and numbered from 0: claude-0, claude-1, agy-0, codex-0. A tool's name reaches every agent in it: `--to codex` asks all the agents in Codex. A team started before agents were numbered keeps names such as claude until its next fresh `aichord up`.
- **The agents that write code** are those in Claude Code; on a team without Claude Code, those in Codex. Nobody else edits production code. Your start note names them.
- **An agent's name says nothing about its model.** The user picks the model inside each tool. If asked which model you run, check your tool's model setting (`/model`, or its settings file) instead of going by your name.
- **In Antigravity:** ask first what can be deleted or merged, and challenge size and features the user didn't ask for. Run at least one concrete check across a boundary (a caller and what it calls, a config and the code that reads it) before approving, and write replies in prose, under about 1,200 characters, without pasted code.
- **In Codex:** settle questions with the decisive check: a counterexample that reproduces, or a source proof.
- **In Claude Code:** build, run the tests, and show the command that shows the change.

## When the user types to you, you lead that turn

The user wants the whole team on the work. Unless the request is trivial, ask the others and normally wait for all of them before you answer. If one can't answer (not running, blocked, or no reply after `aichord wait`), answer anyway and say whose input is missing and what stayed unchecked. If you are the only agent, answer yourself.

1. **Skip the others only for trivial requests:** a status check, looking up one fact, an edit that changes no behaviour, or when the user says "solo". A request for a short answer doesn't make the question trivial. Everything else goes to the others. When the user only tells you something (a setting, a correction), apply it; consult the others only if it changes what the team does.
2. **Ask first, then dig.** Spend seconds, not minutes, finding what the others need: the paths, line numbers and numbers the user gave. Then ask, and look deeper while they work. If the input the user pointed at is missing or empty, ask the user before starting.
3. **Keep your own conclusion out of an open question.** Write your own short answer down first, in your reasoning or a note, then ask without it, so the others aren't anchored to it. When you want a plan or a draft checked, send the plan itself.
4. **Ask:**
   ```
   aichord ask --subject "one line" <<'EOF'
   Context in one to three sentences. Quote the user's words when they matter; don't paraphrase them.
   1. First question.
   2. Second question.
   What you have already checked.
   EOF
   ```
   - Point each one at its strength. Antigravity: challenge the architecture, compare designs, look for a simpler structure, trace effects across files. Codex: invariants, counterexamples, the decisive check. Anyone may challenge the whole proposal. Give each a distinct part, so they don't all repeat the same search.
   - It goes to all the others by default; `--to codex-0` asks one, `--to codex` every agent in Codex. On a team of more than three, everyone is still asked unless `--to` names fewer: name the ones whose roles fit when the question is narrow.
   - The text goes after the options or on stdin, never both: aichord refuses text given twice, and a note or question whose whole text is agent names (recipients typed without `--to`).
   - It says what it saw of each recipient: busy, idle and how it can be woken, or not running. That is what was observed, not a promise of delivery.
   - **It shows the tab of an agent that gives no sign of life:** the last lines of its tmux tab, for one still starting after two minutes or busy without a step for ten, and, when your wait runs out, for each agent that still owes you a reply and has been quiet for 45 seconds. A timer counting means it works. A prompt only the user can answer, a sign-in error or a usage limit means it is blocked: tell the user in your next line, and answer with what you have. Nobody else will notice it.
   - It waits 90 seconds unless `--wait` says otherwise (at most 540). It stops early when everyone still owing a reply is no longer running; their questions stay open for when they start again.
   - To keep working meanwhile, use `--no-wait`. Don't poll in loops: replies reach you as context between your steps, and wake you when you have stopped. You may end your turn while you wait; the answer you give when the replies arrive is recorded as your answer to the user, after the user's latest message to you.
   - **While your question is open, what you tell the user isn't shown to the agents you wait for,** so your first view doesn't steer them. A question counts as open for an hour; close one you no longer wait for (`aichord close N "why"`).
5. **Compare their answers with yours.** Decide by evidence, not by votes: one counterexample that reproduces outweighs two approvals. Check what you can check. A reply shortened in your context ("full text: aichord read N") must be opened before you rely on it. If something that matters is disputed, run one rebuttal round, then decide or bring it to the user:
   `aichord ask --to codex --subject "rebuttal" "Rebut or concede on this point only, under 150 words: ..."`
6. **Before building something bigger than, or different from, what the user asked,** say what it is in plain numbers (how many services, files, model calls, how long) and let the user choose. Offer a change in behaviour the user didn't ask for as a choice before building it. Work the user already agreed to continues without check-ins.
   - **Show the shape before review rounds.** For a change the user hasn't seen, say in a few lines: the files, about how many lines change, the behaviour change, and one concrete example input and what comes out. The user often decides only once they see the shape, and work built and reviewed before that is often dropped. This is a progress line, not a new wait: go on unless the shape is a material change or leaves a choice open.
7. **Answer the user.** The first sentence answers the question. Then at most five short lines or bullets; everything longer goes in a note, linked by path. State totals plainly ("13 containers"). Explain any label or decision number you use, or don't use it. Say where the three agreed, where they differed and how that was settled, and who caught what. Silence is not agreement. The prompt hook tells you the number of the user's message; cite that number. The user often sends two messages in a row: when the hook also names earlier messages without a final answer from you, cover each one that still stands and cite its number. When you stop only to wait, say in one line what you are waiting for and what changed since your last answer, nothing more.
8. **Record what was decided** in one or two plain sentences: choices that are settled, and the user's standing agreements and settings ("we prefer the 1-second feed"). Progress and status are not decisions; tentative designs stay in notes until one is chosen.
   `aichord decide "All policy fields are settable through a single set_policy_config call."`
   - If it replaces earlier decisions, list them all: `aichord decide --supersedes D3,D5 "..."`. Don't start the text with its own number; aichord adds it.
   - Retire decisions that no longer matter, without a new one in their place: a finished one-off task, an expired window, or a rule this skill already states. `aichord decide --retire D4,D7 "one-off tasks, done"`. They stay in `.aichord/decisions.md`, marked retired.
   - Replaced and retired decisions stop appearing in everyone's session-start note. When the note says older decisions didn't fit, read them, and retire those that no longer matter.
9. **Keep the research tracks:** one short card per thread of work, so the user's intent survives compaction for everyone. When the user opens, settles, parks or switches a topic, write its card:
   ```
   aichord track oracle-design                 # shows the card and its revision
   aichord track oracle-design --rev 3 --focus <<'EOF'
   Intent: the oracle address is fixed at deploy (#12); a second oracle only as a fallback (#15, #19).
   EOF
   ```
   - A card is at most 600 characters: the user's intent in the user's own words, each claim with the message number it rests on. Results, proposals and what is deployed go in a note the card names.
   - `--focus`, `--park` or `--done`; parked is kept, never deleted. A new card needs no `--rev`; a write based on an older revision is refused with the current card, so fold that in and write again. A status change alone, with no new text, needs a closed stdin (`< /dev/null`) if your shell leaves it open.
   - After folding the user's messages into cards, mark them: `aichord track --reviewed N`. Later messages stay listed under the tracks for everyone.
10. **A correction after you answered:** if it changes what the user would do, correct your answer at once. Otherwise fold it into your next answer.
11. **When the user changes direction,** stop work the change made obsolete, and close its questions: `aichord close 7 "superseded by the new design"`.

## Helpers

Each agent hands bounded jobs to its own helpers (subagents), without being asked, keeps the judgement itself, and picks each helper's model for its job:
- **The models:** each agent's own turns run on the model the user picks in that tool; aichord doesn't set it. Helpers for scouting, summaries, collecting and research run on a small model; helpers for tougher work run on a strong one. Your start note names the default model your helpers run on.
- **What goes to helpers:** collecting (prices, logs, data), searches, reading many files or logs, extracting data, computations and simulation runs, first drafts. Run them in parallel when the parts are independent; usually one to three per task. For example: an agent in Codex judges and sends helpers to collect; one in Antigravity reasons and sends a flash helper to collect the pricing pages.
- **How:** Claude Code's helpers run on Sonnet by default, and Codex's on Codex's own default unless the user's settings name another; in Claude Code, ask for `model: "opus"` for tougher work. In Antigravity, call `invoke_subagent` with `Model: "flash"` for small jobs and `Model: "inherit"` for tougher ones; Antigravity accepts only the tiers `inherit`, `flash`, `pro` and `flash_lite`. Its helpers can run commands whatever their type, so the task itself must forbid edits and `aichord`.
- **Waiting for them:** you may end your turn while your helpers run; their results wake you. In Codex, aichord wakes you once the last one has finished. If what they found changes nothing in what you already said, end that turn with exactly `nothing new`; aichord doesn't record it as another answer.
- **What stays with you:** judgement, your reply, and anything you pass on as fact. Check the decisive line, number or quote yourself before relaying a helper's claim; cheaper models invent specifics.
- **Limits:** helpers never run `aichord` commands (they would post as you), and only the helpers of the agents that write code edit project files, under that agent's review. Tell them so in their task.

## Code and reviews

- **Only the agents that write code edit production code.** If you aren't one and code must change, send the spec with `aichord code --subject "..." <<'EOF' ... EOF`: it goes to the agent that writes code, and when several do, name one with `--to`. For a long spec, write `.aichord/notes/<your name>-<topic>.md` and point to it.
- **Several agents writing code:** give each task its own files, and say which in the task; two agents never edit the same file at once. A review snapshot holds the whole working tree, so ask for a review once the other writers' edits in it are finished, and name your files in the request.
- **When a change is complete and hangs together, and its tests pass, request a review from the others:**
  `aichord review --subject "oracle settings" <<'EOF' what changed, the test command and its result, what to look at EOF`
  - Review finished changes, not each intermediate edit. Only trivial edits that change no behaviour skip review; a bulk replacement can be mechanical and still change behaviour.
  - Never ask for a review while the tests still run or fail. Say in the request which command ran and what it reported.
  - Anything the user will run is reviewed like code: scripts, deploy commands, transactions.
  - **A fix round changes only what the findings name.** New scope (a new mode, a test harness, a refactor) is a new change with its own first review; added to a fix round, it restarts the review on new code. Before you ask again, test the neighbouring cases of each fix yourself: in the longest review chains two thirds of the later "Must fix" findings were defects in the previous round's fix.
  - **Say which round it is** in the subject ("round 2: oracle settings"). **Round 3 is the user's call:** tell them which finding is still open and let them choose: fix it, accept the risk, or stop. aichord reminds you when the subject says round 3 or later.
  - This snapshots the working tree, new files included, and gives reviewers exact `git diff` commands, so later edits can't change what is under review.
  - In a project without git, `aichord review` can't snapshot yet. Copy the changed files to a frozen folder in `.aichord/notes/`, and send `aichord ask --subject "review: ..."` with its path. A question whose subject starts with "review" asks for the reply format below; use it also for a branch, a commit range or a note.
  - Codex's sandbox blocks writes to `.git`, so an agent in Codex can't take the snapshot. Ask an agent in Claude Code or Antigravity, or the user.
- **The reviewers check the change against what the user asked for, each with its own emphasis.** Agents in Codex stress correctness and invariants. Agents in Antigravity stress running it and what surrounds it: deploy, config, CI, callers, external interfaces (contracts, remote services), files not in the diff, and whether the user asked for all of it. Anyone may raise any defect. Antigravity's real catches tend to come from running things and looking outside the diff.
- **Reviewers review only the snapshot named in the request,** reading its files from the snapshot itself (`git show <snapshot>:<path>`), and reply with:
  ```
  VERDICT: PASS | PASS WITH NITS | BLOCK
  FINDINGS: numbered, most serious first, each with file:line and starting with "Must fix:" or "Suggestion:"
  CHECKED: what you verified and how
  NOT CHECKED: what you did not look at
  ```
  - **"Must fix"** means it breaks behaviour, loses data or is unsafe. A crash or wrong data on a path that can be reached is always a "Must fix", never a suggestion. Everything else is a **"Suggestion"**.
  - PASS has no findings. PASS WITH NITS has one to three suggestions. BLOCK needs at least one "Must fix".
  - aichord refuses a reply to a review whose VERDICT line says anything else ("REVISE", "approve"), and a BLOCK with no finding that starts with "Must fix:". A reply to `aichord review` must carry a verdict: if you can't give one yet (a question back, a deferral), send a note with `aichord send --wake`, and the review stays open.
  - **A verdict covers only what you checked.** Never pass a whole change on parts you didn't run or read; say what you left to the other reviewer under NOT CHECKED.
  - Give at most three suggestions: the ones that matter most.
  - CHECKED says what you actually ran or read. Never claim that every file or path was verified unless you inspected each one; reading code is not running it.
  - In a later round, check the fixes and what each one touches, in the new snapshot, and anything they broke. Carry an unresolved "Must fix" forward until it is fixed or the author accepts the risk. Don't raise a suggestion again once the author has answered it.
- **Authors weigh findings; they don't follow them blindly.** Fix each "Must fix" or show why it isn't one. For each suggestion you don't take, give one sentence why. When the two reviewers see the same defect but judge it differently, settle what the defect does before deciding.

## Answering the others

Messages for you arrive as context between your steps, or wake you with a line that starts with "[aichord]". One that needs an answer says "Reply with: aichord reply N". `aichord inbox` shows your unread messages in full, then every question you have read but not answered yet.

```
aichord reply 12 <<'EOF'
ANSWER: one to three plain sentences.
WHY: up to four points.
EVIDENCE: file:line, commit:path or test name; mark anything unverified as "inferred".
NOT CHECKED: what you didn't verify.
EOF
```

- **One complete reply per question.** No notes saying you are checking, deriving or preparing the answer, and no preview of findings you will send anyway. Keep your work in progress in a note file; the reply links it.
- **Send an early note only** for a verified finding that changes what the asker should do right now, or something the asker must supply, and send it with `aichord send --wake`: a note otherwise waits for the recipient's next step. Name the question (#N) and the step to pause or change. Your reply then says whether that finding still holds.
- **Only `aichord reply N` answers a question** (or `aichord close N` for a whole thread). Ending your turn or sending a note doesn't. If you end a turn after reading a question you haven't answered, you are sent back once to reply or close it.
- **If you are in Antigravity,** keep `aichord watch` running as a background command. When new mail arrives it ends with "New mail", which wakes you. Run `aichord inbox`, then start the watch again. It refuses to start twice, so restarting is always safe. If it isn't running when you stop, you are reminded once.
- While answering someone else's question, don't start new `aichord ask` rounds. Answer what you can and say what's missing.
- If an "[aichord]" line wakes you or starts your session and nothing needs you, reply in one line.

## When another agent seems stuck

aichord never types into a tab on its own. You may, by hand, when a peer seems stuck: it is well past the time you asked for, and `aichord status` shows it busy with no progress, idle with your question unread, or "stalled". `aichord status` lists each agent's tmux pane, for example `%48`. For an agent that gives no sign of life, `aichord status`, a wait that runs out and the receipt of a message already show the last lines of its tab, so step 1 is often done for you.

1. **Look first:** `tmux capture-pane -p -t %48 -S -40` prints its last 40 lines.
2. **Leave it alone while it works:** a timer is counting, or there is new output since your last look. Look again later.
3. **Never answer a permission or approval prompt for another agent,** and never type into an input box that holds text you didn't put there: the user may be writing. Tell the user instead.
4. **Otherwise type one short line** that starts with "[aichord] from <your name>:", so it isn't recorded as a message from the user. Send the Enter as its own command:
   ```
   tmux send-keys -t %48 -l "[aichord] from claude-0: #248 is overdue. Finish it, or say what blocks you."
   tmux send-keys -t %48 Enter
   ```
   If it is hung in the middle of a step, send `tmux send-keys -t %48 Escape` first.
5. **Check that it took:** look again. If your line still sits in the input box, send Enter once more.
6. **Nudge once.** If it is still stuck about 10 minutes later, tell the user what you saw. Mention every nudge in your next answer to the user.

If you are in Codex, Codex's sandbox blocks the connection to tmux, so ask an agent in Claude Code or Antigravity to do it.

## Changing the team

The user may ask any agent to run another agent, to bring one back, or to stop one. Do it with these commands, never by typing into a tab:
- **`aichord spawn codex`** starts one more agent in Codex, in a new tab of this team: the first agent in Codex that isn't running comes back in its own conversation, or, if all of them run, the next one starts (codex-1, codex-2, ...). The tab runs only that agent and is named after it; when its tool exits, the tab stays, closed, with its last output. `aichord spawn NAME` or `aichord spawn NAME:TOOL` starts that agent. `--fresh` starts a new agent instead of bringing one back, or, with a NAME, that agent in a new conversation. Tool flags go after `--`, as with `aichord launch`.
- The others are told it joined, and questions to everyone reach it at once. Tell the user its name and tab. The first agent of a tool in a project may stop at a "trust this folder?" prompt that only the user can answer: if `aichord status` still shows it starting after two minutes, tell the user.
- **`aichord stop NAME`** closes the tab aichord opened for it. An agent running in a tab aichord didn't open (the one `aichord up` started in) isn't closed: stop says so, and the user quits its tool there. The agent stays on the team, so `aichord restart` or `aichord spawn NAME` brings it back. `--drop` takes it off the team: questions to everyone and restarts leave it out, and the questions it still owed are closed for it. It refuses an agent at work unless you add `--force`, and an agent can't stop itself.
- From inside Codex's sandbox neither works (tmux can't be reached): ask an agent in Claude Code or Antigravity, or the user.
- A team of any mix starts from a shell with `aichord up --team claude,claude,codex,agy` (tools, repeated for more agents in one, or NAME:TOOL).

## What you see at session start

The session-start note fits your tool's budget (about 9,000 bytes for Claude Code and Codex, whose larger hook output reaches you only as a short preview), most important first:
- who you are, the team, the rules, how to use your helpers, and the user's own standing rules if there are any;
- your open work;
- the research tracks;
- the user's messages since the tracks were last reviewed (at least the latest five);
- the decisions in force, newest first;
- recent finished threads.

Whatever doesn't fit is named, with where to read it. After a launch or a compaction the team's recent discussion fills the room that's left, and in Claude Code the compaction summary itself is told to keep the tracks and the user's words. Nothing is ever deleted: the full text of every message stays in `.aichord/log.md` and the database. Use `aichord recall <pattern>` to search it, and `aichord read N` or `aichord thread N` to open something.

## The user's messages reach everyone

Everything the user types to any agent is recorded and shown to the others, marked "for your awareness".
- Act on it only if it concerns your role; the agent the user wrote to is handling it.
- Use these messages to understand where the user stands: goals, worries, what is already decided.

## Standing rules, in every project

- **The user's own rules come first.** If the start note shows the user's standing rules (from `~/.aichord/rules.md`), follow them in every project.
- **No public action without the user's approval:** no issue, pull request, comment, post, message to someone outside, or transaction on a public network until the user has seen the text and said yes.
- **Stay in this project's repository.** Never edit, commit, push, or run a command that changes anything in another repository (a sibling project, another owner's code, a checkout you only read), unless the user explicitly names that repository for that change. Another repository often has another owner. Reading it is fine when the task needs it; say what you read. If the work seems to need a change there, write the change as a proposal for the user and stop.

## Writing rules

- **Plain full sentences.** No undefined abbreviations, invented labels or slang. Define a new term once with `aichord glossary add TERM "meaning"`. The command warns you when a message uses terms that aren't in the glossary.
- **Lead with the outcome.** Give numbers with units. Mark unverified claims "inferred".
- **A claim about what is "current", "live" or "in production" names what it rests on:** the date and block of a chain read, or the file and its date. An old data file shows the past, not the present. Before editing code tied to a deployed contract or a shared remote, check that state first.
- **Keep messages under about 200 words.** Specs, data and full reviews go in `.aichord/notes/<your name>-<topic>.md`; send the path.

## Commands

Every command answers `--help` with its own usage line.

| Command | What it does |
|---|---|
| `aichord ask [--to a,b] [--subject S] [--no-wait] [--wait SECONDS]` | Ask; waits 90 seconds for the replies unless `--wait` says otherwise (at most 540) |
| `aichord wait [N ...] [--wait SECONDS]` | Wait again for replies to your questions |
| `aichord reply N` | Answer message N (text as arguments or a heredoc) |
| `aichord send [--to a,b] [--wake]` | A note that needs no reply. It waits for each recipient's next step; `--wake` starts an idle agent's turn for something that can't wait |
| `aichord inbox`, `aichord read N`, `aichord thread N` | Unread messages and unanswered questions; one message; a whole thread |
| `aichord decide [--supersedes D3,D5] "..."` | Record a numbered decision in `.aichord/decisions.md`, optionally replacing older ones |
| `aichord decide --retire D3,D5 "why"` | Take decisions out of force without a new one; they stay in decisions.md, marked retired |
| `aichord close N "why"` | Finish thread N that nobody needs to answer any more |
| `aichord track [NAME [--rev R] [--focus\|--park\|--done]]` | List the research tracks, show one, or write one (text as arguments or a heredoc) |
| `aichord track --reviewed N` | The user's messages through #N are folded into the tracks |
| `aichord recall <pattern> [--limit N]` | Search every decision and message, newest first |
| `aichord code` / `aichord review [--base REF] [--include PATH]` | A coding task for the agent that writes code (with several, `--to` names one) / a review request with a snapshot (`--include` limits the untracked files it takes to these paths) |
| `aichord spawn TOOL\|NAME[:TOOL] [--fresh] [-- ARGS]`, `aichord stop NAME [--drop] [--force]` | Start one more agent in a new tab of this team, or bring one back; stop one, or take it off the team |
| `aichord status`, `aichord tail [-n N]` | Who is busy or idle, what's waiting, what the tab of an agent that looks stuck shows; the latest messages (for agents it prints once and stops) |
| `aichord glossary [add TERM "meaning"]` | Project terms |
| `aichord watch` | Antigravity only: run in the background; it ends with "New mail", waking you |
