"""Claude step for the Teams chats PoC: turn recent chats into tasks + per-chat summary.

Reuses Noted's `claude -p` bridge (config.get_claude_bin / clean_env), one call per account.
Read-only: it never writes to Teams.

Usage (from the repo root):
    # read chats live (needs a prior --login of teams_chats_poc.py) and analyse them
    python tools/teams_chats_analyze.py --account accenture --since-minutes 1440

    # or analyse a JSON file produced by teams_chats_poc.py
    python tools/teams_chats_analyze.py --account accenture --input chats.json --out teams_2026-10-19.md
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # import Noted's config
from config import clean_env, get_claude_bin  # noqa: E402

MAX_MSGS_PER_CHAT = 40
MAX_CHARS_PER_MSG = 600

PROMPT = """You analyse Microsoft Teams chats for {me}, a consultant, on the account "{account}".
The text below contains chats with their recent messages (author, time, text). The chat marked
SELF_NOTES is {me}'s own "My notes" chat (private jottings, often written from the phone).

Rules:
- Create a task ONLY when something concrete is asked of {me}, promised by {me}, or awaited from someone else.
- "ask_me": someone asks {me} for something in a 1:1 chat, or mentions/addresses {me} by name in a group.
- "commitment": {me} promised something, or a reminder in SELF_NOTES. Notes are always taken into account.
- "waiting": "ask/wait for X" in SELF_NOTES, or {me} is waiting on someone else's answer or deliverable.
- "question": something is missing to complete a task (for what, for whom, by when) - phrase it as a question to {me}.
- Group chatter that does not affect {me}, social chatter and acknowledgements produce no tasks.
- Data or ideas in SELF_NOTES that are not tasks go in "notes" as short statements.
- Write in the language of the chat. Be concise; do not invent facts or deadlines.

Return ONLY valid JSON, no markdown fences, with this exact shape:
{{"tasks":[{{"type":"ask_me|commitment|waiting|question","text":"...","chat":"<chat name>","who":"<person or empty>","due":"<date or empty>"}}],
"notes":[{{"text":"...","chat":"<chat name>"}}],
"summaries":[{{"chat":"<chat name>","summary":"one or two sentences"}}]}}
Include in "summaries" only chats that have something worth knowing.

CHATS:
{chats}
"""

SECTIONS = [
    ("ask_me", "Me piden"),
    ("commitment", "Me comprometo"),
    ("waiting", "Espero"),
    ("question", "Preguntas"),
]


def format_chats(chats: list[dict]) -> str:
    blocks = []
    for c in chats:
        name = c["chat"]
        tag = " [SELF_NOTES]" if name.rstrip().endswith("(You)") else ""
        lines = [f"## {name}{tag}"]
        for m in c["messages"][-MAX_MSGS_PER_CHAT:]:
            text = m["text"].replace("\n", " ")[:MAX_CHARS_PER_MSG]
            quote = f' (replying to: "{m["reply_to"][:150]}")' if m.get("reply_to") else ""
            lines.append(f'[{m["time"][:16]}] {m["author"] or "?"}: {text}{quote}')
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def call_claude(prompt: str) -> dict:
    claude_bin = get_claude_bin()
    if not claude_bin:
        sys.exit("claude CLI not found (same lookup as Noted: PATH, npm, AnthropicClaude).")
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    result = subprocess.run(
        [claude_bin, "-p"], input=prompt, capture_output=True, text=True,
        encoding="utf-8", timeout=240, env=clean_env(), **kwargs,
    )
    if result.returncode != 0:
        sys.exit(f"claude -p failed ({result.returncode}): {result.stderr.strip()[:500]}")
    out = result.stdout.strip()
    out = re.sub(r"^```(?:json)?\s*|\s*```$", "", out)
    start, end = out.find("{"), out.rfind("}")
    try:
        return json.loads(out[start:end + 1])
    except (ValueError, json.JSONDecodeError):
        sys.exit(f"Could not parse Claude output as JSON:\n{out[:800]}")


def render_markdown(account: str, analysis: dict) -> str:
    tasks = analysis.get("tasks", [])
    lines = [f"# Teams chats - {account} - {date.today().isoformat()}", ""]
    for key, title in SECTIONS:
        group = [t for t in tasks if t.get("type") == key]
        if not group:
            continue
        lines.append(f"## {title}")
        for t in group:
            who = f" ({t['who']})" if t.get("who") else ""
            due = f" - due {t['due']}" if t.get("due") else ""
            lines.append(f"- [ ] {t['text']}{who}{due}")
            lines.append(f"  - 💬 {t.get('chat', '?')} · {account}")
        lines.append("")
    if analysis.get("notes"):
        lines.append("## Notas")
        lines += [f"- {n['text']} (💬 {n.get('chat', '?')})" for n in analysis["notes"]]
        lines.append("")
    lines.append(f"## Chats {account}")
    for s in analysis.get("summaries", []):
        lines.append(f"- **{s['chat']}**: {s['summary']}")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", required=True)
    ap.add_argument("--me", default="Inés Campos", help="your name as people address you")
    ap.add_argument("--input", help="JSON from teams_chats_poc.py; if omitted, reads chats live")
    ap.add_argument("--since-minutes", type=int, default=1440)
    ap.add_argument("--max-chats", type=int, default=20)
    ap.add_argument("--out", help="write the markdown here instead of only printing it")
    args = ap.parse_args()

    if args.input:
        chats = json.loads(Path(args.input).read_text(encoding="utf-8"))
    else:
        from teams_chats_poc import read_chats  # same folder
        chats = read_chats(args.account, args.since_minutes, args.max_chats, debug=False)
    if not chats:
        sys.exit("No chats with new activity in the window; nothing to analyse.")

    prompt = PROMPT.format(me=args.me, account=args.account, chats=format_chats(chats))
    md = render_markdown(args.account, call_claude(prompt))
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"Saved {args.out}", file=sys.stderr)
    print(md)


if __name__ == "__main__":
    main()
