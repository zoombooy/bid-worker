from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bidreader.evaluation import aggregate_corpus, evaluate


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate an isolated tender-document gold corpus.")
    parser.add_argument("--manifest", required=True, type=Path, help="JSON manifest with project-level splits")
    parser.add_argument("--output", type=Path, help="Optional path to save the corpus report")
    parser.add_argument("--match-threshold", type=float, default=0.75)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    if manifest.get("schema_version") != "1.0" or not isinstance(manifest.get("samples"), list):
        raise ValueError("manifest 必须包含 schema_version=1.0 和 samples 数组。")
    root = args.manifest.resolve().parent
    evaluated = []
    for sample in manifest["samples"]:
        gold_path = (root / sample["gold"]).resolve()
        prediction_path = (root / sample["prediction"]).resolve()
        gold = json.loads(gold_path.read_text(encoding="utf-8-sig"))
        prediction = json.loads(prediction_path.read_text(encoding="utf-8-sig"))
        evaluated.append({**sample, "report": evaluate(gold, prediction, match_threshold=args.match_threshold)})
    report = aggregate_corpus(evaluated)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
