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
    skipped: list[str] = []
    # (model, prompt_hash)로 묶는다. prompt_hash만으로 묶으면 모델이 다른 실행이
    # 한 분포에 섞여 조용히 오염된다. 같은 프롬프트라도 모델이 바뀌면 다른 실험이다.
    by_group: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        summary = record["summary"]

        # 구 스키마(quotes_offered 없음)는 지어내기율을 계산할 수 없다.
        # 0으로 채워 넣으면 옛 실행이 '지어내기 0%인 정직한 모델'로 둔갑한다. 건너뛴다.
        if "quotes_offered" not in summary:
            skipped.append(path.name)
            continue

        summary["_job"] = Path(record["job_path"]).name
        summary["_job_anon"] = anonymize(record["job_path"])
        by_group[(summary["model"], summary["prompt_hash"])].append(summary)
        all_runs.append(summary)

    if skipped:
        print(
            f"⚠️ 구 스키마 기록 {len(skipped)}건을 건너뜁니다 "
            f"(quotes_offered 없음 → 지어내기율 계산 불가): {', '.join(skipped)}"
        )
    if not all_runs:
        print("집계할 수 있는 실행이 없습니다.")
        return 1

    if len({m for m, _ in by_group}) > 1:
        print("⚠️ 서로 다른 모델의 실행이 섞여 있습니다. 아래 분포를 가로질러 비교하지 마세요.")

    for (model, prompt_hash), runs in by_group.items():
        # 두 지표를 짝으로 본다. 지어내기율만 보면 게으른 모델(quote를 아예 안 주는)이
        # 1등을 한다. 발견율이 그것을 잡아낸다.
        hallu = [r["demoted_count"] / max(r["quotes_offered"], 1) for r in runs]
        found = [r["evidence_found"] / max(r["requirements_count"], 1) for r in runs]
        distinct_jobs = {r["_job"] for r in runs}

        print(
            f"\n{model} / prompt_hash {prompt_hash}  "
            f"({len(runs)}건 실행, 서로 다른 공고 {len(distinct_jobs)}건)"
        )
        print("─" * 72)
        print(f"  {'공고':<16} {'지어내기':>16} {'발견율':>16} {'지연':>7} {'비용':>9}")

        for run, h, f in zip(runs, hallu, found, strict=True):
            print(
                f"  {run['_job']:<16} "
                f"{run['demoted_count']:>3}/{run['quotes_offered']:<3}({h:>5.0%}) "
                f"{run['evidence_found']:>3}/{run['requirements_count']:<3}({f:>5.0%}) "
                f"{run['latency_s']:>6.1f}s ${run['cost_usd']:.4f}"
            )

        print("─" * 72)
        print(
            f"  지어내기율 중앙값 {statistics.median(hallu):.0%} | "
            f"발견율 중앙값 {statistics.median(found):.0%} | "
            f"평균 비용 ${statistics.mean(r['cost_usd'] for r in runs):.4f}"
        )

        # 분포를 판단하기에 표본이 부족하면 시끄럽게 알린다. (컨벤션: 조용한 실패 금지)
        if len(distinct_jobs) < 10:
            print(
                f"  ⚠️ 서로 다른 공고가 {len(distinct_jobs)}건뿐입니다. "
                f"D2 종료 기준은 10건입니다 — 이 분포로 판단하지 마세요."
            )
        if all(r["demoted_count"] == 0 for r in runs) and statistics.median(found) < 0.5:
            print(
                f"  ⚠️ 지어내기 0건이지만 발견율 중앙값이 {statistics.median(found):.0%}입니다. "
                f"정직한 것인지 게으른 것인지 구분되지 않습니다."
            )

    if do_export:
        export_metrics(all_runs, Path("out/metrics.jsonl"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
