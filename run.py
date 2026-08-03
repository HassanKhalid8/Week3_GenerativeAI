#!/usr/bin/env python
"""Command-line front end - the automated download pipeline.

    python run.py "a brass orrery on a walnut desk" --ratio 16:9 --style photoreal -n 3
    python run.py --engines
    python run.py --history

Runs exactly the same six-stage pipeline as the web UI, printing each stage as it
executes and saving high-resolution assets locally.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from imagestudio import GenerationRequest, ratio_table, style_table  # noqa: E402
from imagestudio import providers as provider_registry  # noqa: E402
from imagestudio.engine import Studio  # noqa: E402
from imagestudio.storage import assets_root, library_stats, read_manifest  # noqa: E402

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, GREEN, YELLOW, RED = "\033[36m", "\033[32m", "\033[33m", "\033[31m"

_STATE_MARK = {"running": f"{CYAN}>{RESET}", "done": f"{GREEN}+{RESET}",
               "warn": f"{YELLOW}!{RESET}", "failed": f"{RED}x{RESET}"}


def printer(verbose: bool):
    def emit(kind: str, data: dict) -> None:
        if kind == "stage":
            mark = _STATE_MARK.get(data.get("state", ""), " ")
            detail = data.get("detail", "")
            if data.get("state") == "running" and not verbose:
                return
            index = data.get("index")
            tag = f"[{index + 1}] " if index is not None else ""
            print(f"  {mark} {tag}{data['stage']:<10} {DIM}{detail}{RESET}")
        elif kind == "retry":
            print(f"  {YELLOW}~{RESET} retry {data['attempt']}: {data['detail']} "
                  f"{DIM}(backing off {data['sleep_ms'] / 1000:.1f}s){RESET}")
        elif kind == "note":
            print(f"  {DIM}note: {data['text']}{RESET}")
        elif kind == "asset_start":
            print(f"\n{BOLD}asset {data['index'] + 1}/{data['total']}{RESET} {DIM}seed {data['seed']}{RESET}")
    return emit


def cmd_engines() -> int:
    print(f"\n{BOLD}Engine matrix{RESET}")
    for engine in provider_registry.catalogue():
        mark = f"{GREEN}ready{RESET}" if engine["available"] else f"{DIM}needs {engine['env_key']}{RESET}"
        cost = "free" if engine["free"] else "paid"
        print(f"  {engine['name']:<14} {mark:<28} {DIM}{cost:<5} {engine['max_prompt_chars']:>6} chars{RESET}")
        print(f"  {DIM}{'':<14} {engine['notes']}{RESET}")
    print(f"\n  auto selects: {BOLD}{provider_registry.resolve('auto').label}{RESET}\n")
    return 0


def cmd_presets() -> int:
    print(f"\n{BOLD}Aspect ratios{RESET}")
    for row in ratio_table():
        print(f"  {row['ratio']:<7} {row['width']:>5} x {row['height']:<5} "
              f"{DIM}{row['pixels']:>10,} px  {row['target']}{RESET}")
    print(f"\n{BOLD}Style presets{RESET}")
    for row in style_table():
        print(f"  {row['key']:<14} {DIM}{(row['positive'] or 'no modifiers')[:76]}{RESET}")
    print()
    return 0


def cmd_history(limit: int) -> int:
    entries = read_manifest(limit=limit)
    stats = library_stats()
    print(f"\n{BOLD}Asset library{RESET} {DIM}{stats['root']}{RESET}")
    print(f"  {stats['assets']} asset(s), {stats['megabytes']} MB\n")
    if not entries:
        print(f"  {DIM}manifest is empty{RESET}\n")
        return 0
    for entry in entries:
        score = entry.get("qa_aesthetic")
        score_text = f"{score:.1f}/10" if isinstance(score, (int, float)) else (entry.get("error_code") or "-")
        colour = GREEN if entry.get("status") == "accepted" else YELLOW if entry.get("status") == "flagged" else RED
        print(f"  {colour}{entry.get('status', '?'):<9}{RESET} {entry.get('timestamp', '')[:19]} "
              f"{DIM}{entry.get('provider', ''):<13}{RESET} {score_text:<8} {(entry.get('prompt') or '')[:52]}")
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Lumen Forge - Multimodal Image Generation Studio - CLI pipeline",
    )
    parser.add_argument("prompt", nargs="?", help="the text description to render")
    parser.add_argument("-N", "--negative", default="", help="negative prompt")
    parser.add_argument("-r", "--ratio", default="1:1", help="aspect ratio key, e.g. 16:9")
    parser.add_argument("-s", "--style", default="none", help="style preset key")
    parser.add_argument("-n", "--count", type=int, default=1, help="generation count (1-4)")
    parser.add_argument("--seed", type=int, default=None, help="base seed for reproducibility")
    parser.add_argument("-e", "--engine", default="auto", help="engine name, or auto")
    parser.add_argument("--connect-timeout", type=float, default=3.05)
    parser.add_argument("--read-timeout", type=float, default=60.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--qa-threshold", type=float, default=7.0)
    parser.add_argument("--discard-below-qa", action="store_true",
                        help="delete assets that fail the QA gate")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--engines", action="store_true", help="list the engine matrix and exit")
    parser.add_argument("--presets", action="store_true", help="list ratios and styles and exit")
    parser.add_argument("--history", action="store_true", help="show the asset manifest and exit")
    args = parser.parse_args()

    if args.engines:
        return cmd_engines()
    if args.presets:
        return cmd_presets()
    if args.history:
        return cmd_history(30)
    if not args.prompt:
        parser.print_help()
        return 1

    request = GenerationRequest(
        prompt=args.prompt,
        negative_prompt=args.negative,
        aspect_ratio=args.ratio,
        style=args.style,
        count=args.count,
        seed=args.seed,
        provider=args.engine,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        max_retries=args.retries,
        qa_threshold=args.qa_threshold,
        qa_discard=args.discard_below_qa,
    )

    result = Studio().generate(request, on_event=printer(args.verbose))

    print()
    if result.error:
        print(f"{RED}batch halted{RESET} [{result.error_code}] {result.error}\n")
        return 2

    counts = result.to_dict()["counts"]
    print(f"{BOLD}{result.provider_label}{RESET} {DIM}in {result.elapsed_ms / 1000:.1f}s{RESET}")
    print(f"  accepted {GREEN}{counts['accepted']}{RESET}  "
          f"flagged {YELLOW}{counts['flagged']}{RESET}  "
          f"rejected {RED}{counts['rejected']}{RESET}  "
          f"failed {RED}{counts['failed']}{RESET}")
    for asset in result.assets:
        if asset.filename:
            score = asset.qa.get("aesthetic")
            score_text = f"{score:.1f}/10" if isinstance(score, (int, float)) else "-"
            print(f"  {DIM}->{RESET} {assets_root() / asset.filename}  {DIM}{score_text}{RESET}")
        else:
            print(f"  {RED}->{RESET} [{asset.error_code}] {asset.error}")
    print()
    return 0 if counts["accepted"] or counts["flagged"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
