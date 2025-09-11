from __future__ import annotations

import argparse
import asyncio
import os
import traceback
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Any, cast, Union, Dict
from dotenv import load_dotenv

from litellm import acompletion

import mammoth  # type: ignore


MEMBER_REPORT_REGEX = re.compile(r"^(?P<name>.+?)工作報告-(?P<date>\d{8})\.(?P<ext>docx|doc|md)$")
TEAM_REPORT_HINT = "之工作報告"


@dataclass
class ExampleWeek:
    date: str
    member_reports: List[str]
    team_report: str


def convert_docx_to_markdown(path: Path) -> str:
    """Convert a .docx file to markdown text.

    If mammoth is unavailable or conversion fails, returns an empty string.
    """
    if mammoth is None:
        return ""
    try:
        with path.open("rb") as f:
            result = mammoth.convert_to_markdown(f)
        return result.value.strip()
    except Exception:
        return ""


def load_markdown_from_file(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".md":
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
    if ext in {".docx", ".doc"}:
        return convert_docx_to_markdown(path)
    return ""


def iter_member_reports(folder: Path) -> Iterable[tuple[str, str, Path]]:
    for p in folder.glob("*"):
        if not p.is_file():
            continue
        m = MEMBER_REPORT_REGEX.match(p.name)
        if not m:
            continue
        name = m.group("name")
        date = m.group("date")
        yield name, date, p


def collect_examples(examples_dir: Optional[Path]) -> List[ExampleWeek]:
    if not examples_dir or not examples_dir.exists():
        return []
    # Group by date extracted from member reports; then look for a team report file containing hint & date range.
    by_date: dict[str, List[str]] = {}
    for name, date, path in iter_member_reports(examples_dir):
        text = load_markdown_from_file(path)
        if not text:
            continue
        by_date.setdefault(date, []).append(f"# {name}\n\n{text}")

    weeks: List[ExampleWeek] = []
    # Find potential team reports
    team_candidates = [
        p for p in examples_dir.glob("*") if p.is_file() and TEAM_REPORT_HINT in p.name
    ]
    for date, member_chunks in by_date.items():
        team_text = ""
        for cand in team_candidates:
            # Heuristic: if date substring appears in file name
            if date in cand.name:
                team_text = load_markdown_from_file(cand)
                break
        if not team_text and team_candidates:
            # fallback first candidate
            team_text = load_markdown_from_file(team_candidates[0])
        if team_text:
            weeks.append(
                ExampleWeek(date=date, member_reports=member_chunks, team_report=team_text)
            )
    return weeks


def collect_examples_from_dirs(example_dirs: List[Path]) -> List[ExampleWeek]:
    """Collect and merge example weeks from multiple directories.

    If the same date appears across directories, member reports are concatenated
    and the first non-empty team report encountered is used.
    """
    if not example_dirs:
        return []
    merged: Dict[str, ExampleWeek] = {}
    for d in example_dirs:
        for w in collect_examples(d):
            if w.date not in merged:
                merged[w.date] = ExampleWeek(
                    date=w.date,
                    member_reports=list(w.member_reports),
                    team_report=w.team_report,
                )
            else:
                existing = merged[w.date]
                # Append member reports, avoid exact duplicates
                seen = set(existing.member_reports)
                for mr in w.member_reports:
                    if mr not in seen:
                        existing.member_reports.append(mr)
                        seen.add(mr)
                if not existing.team_report and w.team_report:
                    existing.team_report = w.team_report
    # Return as list
    return list(merged.values())


def build_few_shot_examples(weeks: List[ExampleWeek]) -> str:
    if not weeks:
        return ""
    # Use most recent dates (sorted descending)
    weeks_sorted = sorted(weeks, key=lambda w: w.date, reverse=True)
    blocks = []
    for w in weeks_sorted:
        blocks.append(
            f"<EXAMPLE_WEEK date={w.date}>\n<INPUT>\n{chr(10).join(w.member_reports)}\n</INPUT>\n<OUTPUT>\n{w.team_report}\n</OUTPUT>\n</EXAMPLE_WEEK>"
        )
    return "\n\n".join(blocks)


def build_prompt(member_markdowns: List[str], examples_block: str) -> str:
    intro = (
        "You are an assistant that aggregates individual weekly engineering reports into a concise, well-structured department report. "
        "Summarize achievements, ongoing work, issues/risks, metrics, and next week plan. Keep factual, merge duplicates, and preserve important numbers.\n"
    )
    instructions = (
        "use chinese(zh-TW);"
        "不需要保留組員名稱"
        "組員報告中，項目列表清單(list)的最上級是專案名稱，依照專案來整理報告。"
        "若組員報告的清單中出現 '以前完成:' 或 'done:'... 時，代表是很久以前完成的，忽視他的子項目。"
        " '## Holding'標題底下的段落(paragraphs)代表被暫緩的工作項目，忽視整個段落。"
        "不需要標註是已完成或進行中，組員會自行標註。只需要彙整組員的報告內容。"
        "保留組員報告內容中，項目間的縮排（縮排為2個半型空格)關係，最多可以用到3層縮排。"
        "不要用'、'去分隔許多工作內容，用換行或項目清單(list)的方式呈現。"
    )
    input_block = "\n\n".join(
        f"<REPORT index={i}>\n{txt}\n</REPORT>" for i, txt in enumerate(member_markdowns, 1)
    )
    prompt = (
        f"{intro}{instructions}\n"
        + (f"\nFEW-SHOT EXAMPLES:\n{examples_block}\n" if examples_block else "")
        + f"\nTARGET INPUT:\n{input_block}\n\nGenerate the consolidated department weekly report in markdown now."
    )
    return prompt


async def call_llm(model: str, prompt: str, max_tokens: int = 2000) -> Union[str, None]:
    try:
        # Read temperature from environment variable TEMPERATURE (0.0 - 1.0).
        # Fall back to 0.5 if unset or invalid.
        def _parse_temperature() -> float:
            val = os.getenv("TEMPERATURE")
            if val is None:
                return 0.5
            try:
                t = float(val)
            except Exception:
                return 0.5
            # Clamp to valid range
            if t != t:  # NaN guard
                return 0.5
            return max(0.0, min(1.0, t))

        temperature = _parse_temperature()

        resp: Any = await acompletion(
            temperature=temperature,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
        # Support both object-style and dict-style responses
        choices: Any = getattr(resp, "choices", None)
        if choices is None and isinstance(resp, dict):
            choices = resp.get("choices")
        if not choices:
            return None
        first = choices[0]
        message: Any = getattr(first, "message", None)
        if message is None and isinstance(first, dict):
            message = first.get("message")
        if not message:
            return None
        content: Optional[str] = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = cast(Optional[str], message.get("content"))
        return content
    except Exception:
        print(f"error occurred: {traceback.format_exc()}")
        return None


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate consolidated weekly department report from individual reports"
    )
    p.add_argument(
        "--source",
        "-s",
        required=True,
        help="Folder containing this week's member reports (docx/md)",
    )
    p.add_argument(
        "--examples",
        "-e",
        action="append",
        default=[],
        help="Folder containing historical example weeks (optional). May be provided multiple times.",
    )
    p.add_argument(
        "--model",
        "-m",
        default="gemini/gemini-1.5-flash",
        help="LLM model name for litellm (default: gemini/gemini-1.5-flash)",
    )
    p.add_argument("--out", "-o", default="department_report.md", help="Output markdown file path")
    p.add_argument("--max-tokens", type=int, default=2000, help="Max tokens for generation")
    p.add_argument(
        "--dry-run", action="store_true", help="Only build and print prompt (no LLM call)"
    )
    return p.parse_args(argv)


def gather_member_markdowns(source_dir: Path) -> List[str]:
    chunks: List[str] = []
    for name, date, path in iter_member_reports(source_dir):
        text = load_markdown_from_file(path)
        if not text:
            continue
        chunks.append(f"# {name}\n\n{text}")
    return chunks


def ensure_api_key_present() -> None:
    # litellm supports many providers; we only check a few common env vars.
    if any(
        os.getenv(k)
        for k in [
            "GOOGLE_API_KEY",  # Gemini
            "GEMINI_API_KEY",  # alternate naming if user sets
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "AZURE_OPENAI_API_KEY",
            "GROQ_API_KEY",
        ]
    ):
        return
    raise RuntimeError(
        "No provider API key env var found (e.g., GOOGLE_API_KEY for Gemini). Set one before running."
    )


if __name__ == "__main__":
    load_dotenv()
    args = parse_args()
    source_dir = Path(args.source)
    if not source_dir.exists():
        raise SystemExit(f"Source folder not found: {source_dir}")
    examples_dirs = [Path(p) for p in (args.examples or [])]

    member_markdowns = gather_member_markdowns(source_dir)
    if not member_markdowns:
        raise SystemExit("No valid member reports found in source folder.")
    examples = collect_examples_from_dirs(examples_dirs)
    examples_block = build_few_shot_examples(examples)
    prompt = build_prompt(member_markdowns, examples_block)

    if args.dry_run:
        print(prompt)

    async def _run():
        report_md = await call_llm(args.model, prompt, max_tokens=args.max_tokens)
        out_path = Path(args.out)
        if report_md:
            out_path.write_text(report_md, encoding="utf-8")
        print(f"Report written to {out_path}")

    asyncio.run(_run())
