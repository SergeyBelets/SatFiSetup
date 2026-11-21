"""Offline planning and economic tools; no remote proving or network writes."""
import argparse
import json
from pathlib import Path
from .economics import tco
from .replay import idle_replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser("replay", help="synthetic healthy-idle calendar, not proof evidence")
    replay.add_argument("--hours", type=int, choices=(1, 6, 24, 84), default=84)
    replay.add_argument("--days", type=int, default=30)
    replay.add_argument("--output", type=Path)
    economics = commands.add_parser("tco", help="exact Decimal economic inputs; missing values stay unknown")
    economics.add_argument("--inputs", type=Path, required=True)
    economics.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = idle_replay(hours=args.hours, days=args.days) if args.command == "replay" else tco(json.loads(args.inputs.read_text()))
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
        print(str(args.output))
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
