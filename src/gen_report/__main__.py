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
TEAM_REPORT_HINT = "工作報告匯總"


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
        "Summarize achievements, ongoing work. Keep factual, merge duplicates, and preserve important numbers.\n"
    )
    instruction = """依照組員的報告，並使用markdown格式來整理出部門報告。
如果組員的報告有'## Holding'、'以前完成:'、'done:'等字眼，忽視這些段落或子項目。
組員的報告中，會先用* 標示所屬專案，下一層用1.代表工作內容，再下一層用1.1 代表的是工作內容的子項目。
一段超過30個字時，請縮減他並標註這段話經過縮減，例如: (經過縮減)。
如果專案沒有在專案列表中，要在專案名稱前加上（新專案），例如:## （新項目）六軸機器人。
不要把多項工作內容合併在同一行。

以下是專案列表
* 一機一手
* EBONY 平台
* iMotion-3dof
* 螺絲案
  * 得鑫 HMI
  * 得鑫 PLC
* 育成計畫
* 其他
* VRB
* plc_tx7_cpu


以下是組員報告的範例，只是讓你確定格式，請不要照著內容做彙整

# 週報

## Running

* iMotion-3dof
  1. Resymot GUI / codegg
    1. 新增 gRPC web 功能與相關整合。
  2. dart_resymot_client
    1.以前完成:
      1. 創建 lib 資料結構並實現。
    2.進度:
      2. 修正檔案命名錯誤，重構 lib 資料夾結構。
  3. Resymot 客戶端Demo GUI 實現
  4. Auto page 程式碼暫存功能
  5. Auto page reset功能

* TMDC 測試
  1. 電流環測試
  2. 位置環測試

## Holding

* 育成計畫
  1. resymot專案編譯練習
     1. done:
        1. Kdl msvc release static lib編譯
        2. xyz machine full demo
     2. Kdl msvc release shared lib編譯(0%)
  2. 專案修改練習
     1. webots-gRPC新增getMaxVelocity功能(0%)


以下是整理後的部門報告的參考，只是讓你確定格式，請不要照著內容做彙整

# 軟體部-智能控制組 部門週報

## iMotion-3dof

* Resymot GUI / codegg
    * 新增 gRPC web 功能與相關整合。
* dart_resymot_client
    * 修正檔案命名錯誤，重構 lib 資料夾結構。
* Resymot 客戶端Demo GUI 實現
* Auto page 程式碼暫存功能
* Auto page reset功能

## VRB 項目 pre-work

* 在 IPC 上練習，把 Mosquitto 檔案放進 IPC，當作 MQTT Server。(20%)
* 加入 Shell 自動化腳本，使開機時能和其他通訊一起執行。(20%)

## 其他

* pdf_signature
    * 支援 docker 化部署，並整合 github action push 到 docker.io。
* gen-report
    * 建立 feat/util 分支。
    * 用於週報自動整合。

## (新項目)TMDC 測試

* 電流環測試
* 位置環測試

"""

    instructions2 = (
        "把組員的報告整理成部門報告，使用chinese(zh-TW);"
        "格式參考如下"
        "組員報告中，項目列表清單(list)的最上級是專案名稱，依照專案來整理報告，被忽視的子項目或者段落都不用被整理。"
        "若組員報告的清單中出現 '以前完成:' 或 'done:'... 時，忽視他的子項目。"
        "標題會分成'## Running'與'## Holding'，只需要整理'## Running'的段落(paragraphs)，忽視'## Holding'的段落。"
        "不需要標註是已完成或進行中，組員會自行標註。只需要彙整組員的報告內容。"
        "保留組員報告內容中，項目間的縮排（縮排為2個半型空格)關係，最多可以用到3層縮排。"
        "不要用'、'去分隔許多工作內容，用換行或項目清單(list)的方式呈現。"
    )
    input_block = "\n\n".join(
        f"<REPORT index={i}>\n{txt}\n</REPORT>" for i, txt in enumerate(member_markdowns, 1)
    )
    prompt = (
        f"{intro}{instruction}\n"
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
