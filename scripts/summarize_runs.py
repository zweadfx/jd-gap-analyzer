"""실행 기록을 prompt_hash별로 묶어 강등률 분포를 뽑는다.

    uv run python scripts/summarize_runs.py                       # out/ (샘플 실행)
    uv run python scripts/summarize_runs.py out/private           # 실제 공고/이력서 실행
    uv run python scripts/summarize_runs.py out/private --export  # + 계측치만 커밋용으로 추출

--export는 텍스트가 없는 계측치만 out/metrics.jsonl로 뽑는다. 실제 공고로 돌린 결과 자체는
커밋할 수 없지만(본문이 들어 있다), 강등률 분포라는 '증거'는 레포에 남아야 하기 때문이다.

D2 종료 기준이 "서로 다른 공고 10건 이상의 강등률 분포"이므로, 이걸 눈으로 세지 않는다.
prompt_hash로 묶여 나오기 때문에 "강등률이 낮았던 실행은 어떤 프롬프트 버전이었나"를
커밋 로그를 뒤지지 않고 바로 볼 수 있다.

주의: 같은 공고를 여러 번 돌린 것은 분포가 아니다(temperature=0). 서로 다른 공고여야 한다.
"""

import hashlib
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def anonymize(job_path: str) -> str:
    """공고 파일명을 익명 id로. 파일명에 회사명이 들어가면 그 자체가 유출이다."""
    return "job_" + hashlib.sha256(Path(job_path).name.encode()).hexdigest()[:6]


def export_metrics(runs: list[dict], dest: Path) -> None:
    """텍스트가 전혀 없는 계측치만 뽑아 커밋 가능한 파일로 쓴다.

    실제 공고로 돌린 결과(out/private/)는 커밋할 수 없다. 공고 본문과 이력서 전문이
    통째로 들어 있기 때문이다. 그런데 컨벤션은 프롬프트 커밋과 결과가 짝을 이룰 것을
    요구한다 — 진짜 측정치가 레포에 안 남으면 튜닝의 근거가 로컬에만 있게 된다.

    summary 블록에는 텍스트가 없다(모델/해시/개수/지연/토큰/비용뿐). 이것만 커밋한다.
    내용은 비공개, 측정 증거는 공개.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as f:
        for run in runs:
            row = {k: v for k, v in run.items() if not k.startswith("_")}
            row["job"] = run["_job_anon"]
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\n계측치 저장(텍스트 없음, 커밋 가능): {dest}  {len(runs)}건")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_export = "--export" in sys.argv

    base = Path(args[0]) if args else Path("out")
    files = sorted(base.glob("run_*.json"))

    if not files:
        print(f"{base}/ 에 실행 기록이 없습니다.")
        return 1

    all_runs: list[dict] = []
    by_prompt: dict[str, list[dict]] = defaultdict(list)
    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        summary = record["summary"]
        summary["_job"] = Path(record["job_path"]).name
        summary["_job_anon"] = anonymize(record["job_path"])
        by_prompt[summary["prompt_hash"]].append(summary)
        all_runs.append(summary)

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

    if do_export:
        export_metrics(all_runs, Path("out/metrics.jsonl"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
