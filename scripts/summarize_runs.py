"""실행 기록을 prompt_hash별로 묶어 강등률 분포를 뽑는다.

    uv run python scripts/summarize_runs.py            # out/ (샘플 실행)
    uv run python scripts/summarize_runs.py out/private # 실제 이력서 실행

D2 종료 기준이 "서로 다른 공고 10건 이상의 강등률 분포"이므로, 이걸 눈으로 세지 않는다.
prompt_hash로 묶여 나오기 때문에 "강등률이 낮았던 실행은 어떤 프롬프트 버전이었나"를
커밋 로그를 뒤지지 않고 바로 볼 수 있다.

주의: 같은 공고를 여러 번 돌린 것은 분포가 아니다(temperature=0). 서로 다른 공고여야 한다.
"""

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def main() -> int:
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("out")
    files = sorted(base.glob("run_*.json"))

    if not files:
        print(f"{base}/ 에 실행 기록이 없습니다.")
        return 1

    by_prompt: dict[str, list[dict]] = defaultdict(list)
    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        summary = record["summary"]
        summary["_job"] = Path(record["job_path"]).name
        by_prompt[summary["prompt_hash"]].append(summary)

    for prompt_hash, runs in by_prompt.items():
        rates = [r["demoted_count"] / max(r["requirements_count"], 1) for r in runs]
        distinct_jobs = {r["_job"] for r in runs}

        print(
            f"\nprompt_hash {prompt_hash}  ({len(runs)}건 실행, 서로 다른 공고 {len(distinct_jobs)}건)"
        )
        print("─" * 60)

        for run, rate in zip(runs, rates, strict=True):
            print(
                f"  {run['_job']:<16} 강등 {run['demoted_count']:>2}/{run['requirements_count']:<2} "
                f"({rate:>5.1%})  {run['latency_s']:>5.1f}s  ${run['cost_usd']:.4f}"
            )

        print("─" * 60)
        print(
            f"  강등률 중앙값 {statistics.median(rates):.1%} | 최소 {min(rates):.1%} | 최대 {max(rates):.1%}"
        )
        print(f"  평균 비용 ${statistics.mean(r['cost_usd'] for r in runs):.4f}")

        # 분포를 판단하기에 표본이 부족하면 시끄럽게 알린다. (컨벤션: 조용한 실패 금지)
        if len(distinct_jobs) < 10:
            print(
                f"  ⚠️ 서로 다른 공고가 {len(distinct_jobs)}건뿐입니다. "
                f"D2 종료 기준은 10건입니다 — 이 분포로 판단하지 마세요."
            )
        if all(r["demoted_count"] == 0 for r in runs):
            print("  ⚠️ 전 실행 강등 0건. 검증이 실제로 돌고 있는지 의심하세요.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
