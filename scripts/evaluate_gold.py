from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidreader.evaluation import evaluate


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate one BidReader analysis against human gold labels.")
    parser.add_argument("--gold", required=True, type=Path, help="Human annotation JSON for one source file")
    parser.add_argument("--prediction", required=True, type=Path, help="Analysis JSON or API response JSON")
    parser.add_argument("--output", type=Path, help="Optional path to save the metrics report")
    parser.add_argument("--match-threshold", type=float, default=0.75)
    args = parser.parse_args()

    gold = json.loads(args.gold.read_text(encoding="utf-8-sig"))
    prediction = json.loads(args.prediction.read_text(encoding="utf-8-sig"))
    report = evaluate(gold, prediction, match_threshold=args.match_threshold)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
