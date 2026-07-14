"""D2 베이스라인 — 실제 공고 N건 × 2회 × 생존 모델 2종.

    uv run python scripts/baseline.py                    # data/private/ 전체
    uv run python scripts/baseline.py --reps 2

**표만 낸다. 판정하지 않는다.** 모델 결정은 사람이 이 표를 보고 한다.
자동 판정 로직을 넣지 않는 이유: 낙관적 판정은 나중에 자기기만의 근거가 된다.
(measure_noise.py에서 같은 이유로 판정 로직을 제거했다.)

내는 것:
  1. 공고별 원시 지표 (지어내기율 / 발견율 / 지연 / 비용)
  2. 부호 검정 — 공고별로 어느 모델의 발견율이 높았나. 집계 델타만 보면 한 공고에서 튄
     값을 개선으로 착각한다. 8/10이 같은 방향이면 신호, 5/5로 갈리면 노이즈다.
  3. 새 노이즈 밴드 — 같은 공고 2회의 산포. 9%p(구 프롬프트, job1 단건)는 폐기됐다.
  4. 부분 인용 경고 건수
  5. 관측 2종 (컨벤션):
     - 검증 불가 요구사항("오픈 마인드")이 Top 3에 오른 빈도
     - 조건 완화 문구("경력 무관")가 요구사항으로 뽑힌 건수
     → 키워드 휴리스틱이다. **거르지 않고 표시만 한다.** 조용한 필터는 금지다.

결과는 out/private/(gitignore)에 저장된다 — 실제 공고/이력서 원문이 들어간다.
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
from openai import OpenAI  # noqa: E402

from src import prompts  # noqa: E402
from src.pipeline import prompt_hash, select_top_gaps  # noqa: E402
from src.schemas import GapAnalysis, JobRequirements  # noqa: E402
from src.verify import verify_quotes  # noqa: E402

# 생존 모델 2종. Step1 count/category가 안정적이라 실격을 면한 것들.
MODELS = ["gpt-4o-mini", "gpt-5.4-nano"]

PRICE = {"gpt-4o-mini": (0.15, 0.60), "gpt-5.4-nano": (0.20, 1.25)}

# --- 관측용 키워드 (거르지 않는다. 세기만 한다) ---
UNVERIFIABLE = [
    "오픈 마인드",
    "오픈마인드",
    "책임감",
    "태도",
    "열정",
    "성실",
    "흥미를 느끼",
    "창의적",
    "적극적",
    "커뮤니케이션 능력",
    "함께 성장",
    "주도적",
    "긍정적",
    "유연한 사고",
]
RELAXATION = ["무관", "관계없", "상관없", "우대하지 않"]


def is_unverifiable(text: str) -> bool:
    return any(k in text for k in UNVERIFIABLE)


def is_relaxation(text: str) -> bool:
    return any(k in text for k in RELAXATION)


def run_once(client: OpenAI, model: str, job_text: str, resume_text: str) -> dict:
    t0 = time.perf_counter()
    r1 = client.chat.completions.parse(
        model=model,
        temperature=0.0,
        messages=[
            {"role": "system", "content": prompts.STEP1_SYSTEM},
            {"role": "user", "content": prompts.build_step1_user(job_text)},
        ],
        response_format=JobRequirements,
    )
    reqs = r1.choices[0].message.parsed

    r2 = client.chat.completions.parse(
        model=model,
        temperature=0.0,
        messages=[
            {"role": "system", "content": prompts.STEP2_SYSTEM},
            {"role": "user", "content": prompts.build_step2_user(reqs.requirements, resume_text)},
        ],
        response_format=GapAnalysis,
    )
    analysis_raw = r2.choices[0].message.parsed
    latency = time.perf_counter() - t0

    analysis, demoted, warnings = verify_quotes(analysis_raw, resume_text)
    top, top3_warnings = select_top_gaps(reqs.requirements, analysis, job_text=job_text)

    offered = sum(1 for e in analysis_raw.evidences if e.quote)
    found = sum(1 for e in analysis.evidences if e.status in ("충분", "약함"))
    n = len(reqs.requirements)

    pin, pout = PRICE[model]
    cost = sum(
        (r.usage.prompt_tokens / 1e6 * pin + r.usage.completion_tokens / 1e6 * pout)
        for r in (r1, r2)
        if r.usage
    )

    status_by_id = {e.requirement_id: e.status for e in analysis.evidences}

    return {
        "requirements": n,
        "quotes_offered": offered,
        "demoted": demoted,
        "evidence_found": found,
        "hallu": demoted / offered if offered else 0.0,
        "found_rate": found / n if n else 0.0,
        "latency": latency,
        "cost": cost,
        "partial_warnings": sum(1 for w in warnings if "부분 인용" in w),
        "top3_blocked": top3_warnings,
        # 관측 2종
        "unverifiable_all": [r.text for r in reqs.requirements if is_unverifiable(r.text)],
        "unverifiable_in_top3": [t.text for t in top if is_unverifiable(t.text)],
        "relaxation": [r.text for r in reqs.requirements if is_relaxation(r.text)],
        # 항목 6~10 검토용 원자료. 요구사항 원문이 없으면 OR 조건/역방향/영어/지문을 볼 수 없다.
        # 공고 텍스트지 이력서가 아니므로 out/private/에 저장해도 개인정보 문제는 없다.
        "fingerprint": [(r.text, r.category, r.kind) for r in reqs.requirements],
        "items": [
            {
                "id": r.id,
                "text": r.text,
                "category": r.category,
                "kind": r.kind,
                "status": status_by_id.get(r.id, "?"),
                "in_top3": any(t.id == r.id for t in top),
            }
            for r in reqs.requirements
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs-dir", type=Path, default=Path("data/private"))
    ap.add_argument("--resume", type=Path, default=None)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--models", nargs="+", default=MODELS)
    ap.add_argument("--out", type=Path, default=Path("out/private/baseline_raw.json"))
    args = ap.parse_args()

    models = args.models

    load_dotenv(".env")

    jobs = sorted(p for p in args.jobs_dir.glob("*.txt") if p.name.startswith("job"))
    resume_path = args.resume or next(
        (p for p in args.jobs_dir.glob("*.txt") if "resume" in p.name.lower()), None
    )

    if not jobs:
        print(f"{args.jobs_dir}/ 에 job*.txt 가 없습니다.")
        return 1
    if not resume_path or not resume_path.exists():
        print(f"이력서를 찾을 수 없습니다. {args.jobs_dir}/resume.txt 를 두거나 --resume 지정.")
        return 1

    resume_text = resume_path.read_text(encoding="utf-8").strip()
    client = OpenAI()

    print(
        f"prompt_hash {prompt_hash()} | 공고 {len(jobs)}건 × {args.reps}회 × 모델 {len(models)}종"
    )
    print(f"이력서 {resume_path.name} ({len(resume_text):,}자)\n")
    if len(jobs) < 10:
        print(f"⚠️ 공고가 {len(jobs)}건입니다. D2 종료 기준은 10건 — 이 분포로 판단하지 마세요.\n")

    # model -> job -> [rep 결과]
    data: dict[str, dict[str, list[dict]]] = {m: {} for m in models}

    for model in models:
        print(f"── {model} " + "─" * 52)
        print(f"  {'공고':<10} {'요구':>4} {'지어내기':>12} {'발견율':>12} {'지연':>7} {'비용':>8}")
        for job in jobs:
            job_text = job.read_text(encoding="utf-8").strip()
            reps = [run_once(client, model, job_text, resume_text) for _ in range(args.reps)]
            data[model][job.stem] = reps
            for i, r in enumerate(reps):
                tag = job.stem if i == 0 else ""
                print(
                    f"  {tag:<10} {r['requirements']:>4} "
                    f"{r['demoted']:>3}/{r['quotes_offered']:<3}({r['hallu']:>4.0%}) "
                    f"{r['evidence_found']:>3}/{r['requirements']:<3}({r['found_rate']:>4.0%}) "
                    f"{r['latency']:>6.1f}s ${r['cost']:.4f}"
                )
        print()

    # ---------- 1. 모델별 요약 ----------
    print("=" * 66)
    print("모델별 중앙값")
    print("=" * 66)
    print(
        f"  {'모델':<14} {'지어내기율':>10} {'발견율':>8} {'지연':>8} {'비용':>9} {'부분인용':>8}"
    )
    for model in models:
        flat = [r for reps in data[model].values() for r in reps]
        print(
            f"  {model:<14} {statistics.median(r['hallu'] for r in flat):>9.0%} "
            f"{statistics.median(r['found_rate'] for r in flat):>7.0%} "
            f"{statistics.median(r['latency'] for r in flat):>7.1f}s "
            f"${statistics.mean(r['cost'] for r in flat):>7.4f} "
            f"{sum(r['partial_warnings'] for r in flat):>7}건"
        )

    # ---------- 2. 부호 검정 ----------
    print("\n" + "=" * 66)
    print("부호 검정 — 공고별로 어느 모델의 발견율이 높았나")
    print("=" * 66)
    if len(models) < 2:
        print("  (모델 1종이므로 생략)")
        a = b = None
    else:
        a, b = models[0], models[1]
    wins_a = wins_b = ties = 0
    if a and b:
        for job in jobs:
            ra = statistics.median(r["found_rate"] for r in data[a][job.stem])
            rb = statistics.median(r["found_rate"] for r in data[b][job.stem])
            if abs(ra - rb) < 1e-9:
                mark, ties = "=", ties + 1
            elif ra > rb:
                mark, wins_a = f"{a} 우세", wins_a + 1
            else:
                mark, wins_b = f"{b} 우세", wins_b + 1
            print(f"  {job.stem:<10} {a} {ra:>5.0%}  vs  {b} {rb:>5.0%}   → {mark}")
    print(f"\n  {a}: {wins_a}승 | {b}: {wins_b}승 | 무승부: {ties}")
    print("  (같은 방향이 8/10이면 신호. 5/5로 갈리면 집계 델타가 커도 노이즈다.)")

    # ---------- 3. 새 노이즈 밴드 ----------
    print("\n" + "=" * 66)
    print(f"노이즈 밴드 — 같은 공고 {args.reps}회의 발견율 산포 (구 9%p는 폐기)")
    print("=" * 66)
    for model in models:
        bands = [
            max(r["found_rate"] for r in reps) - min(r["found_rate"] for r in reps)
            for reps in data[model].values()
        ]
        print(
            f"  {model:<14} 중앙값 {statistics.median(bands):>5.0%}p | "
            f"최대 {max(bands):>4.0%}p | 밴드 0인 공고 {sum(1 for x in bands if x == 0)}/{len(bands)}건"
        )
    print("  ※ 이 밴드보다 작은 모델 간 차이는 검출 불가다.")

    # ---------- 4. 관측 2종 ----------
    print("\n" + "=" * 66)
    print("관측 — 프롬프트 수정 라운드의 입력 (거르지 않고 표시만)")
    print("=" * 66)
    for model in models:
        flat = [r for reps in data[model].values() for r in reps]
        top3_hits = [t for r in flat for t in r["unverifiable_in_top3"]]
        all_hits = {t for r in flat for t in r["unverifiable_all"]}
        relax = {t for r in flat for t in r["relaxation"]}
        runs = len(flat)
        print(f"\n  {model}")
        print(
            f"    검증 불가 항목이 Top3에 오름: {len(top3_hits)}건 / {runs}회 실행 "
            f"({len(top3_hits) / runs:.0%})"
        )
        for t in sorted(set(top3_hits)):
            print(f"        ★ {t}")
        print(f"    검증 불가 항목이 요구사항에 포함: {len(all_hits)}종")
        for t in sorted(all_hits):
            print(f"        - {t}")
        print(f"    조건 완화 문구가 요구사항으로: {len(relax)}종")
        for t in sorted(relax):
            print(f"        ★ {t}")

    # ---------- 5. 지문 안정성 (항목 10) ----------
    print("\n" + "=" * 66)
    print(f"Step1 텍스트 지문 안정성 — 같은 공고 {args.reps}회가 동일한가")
    print("=" * 66)
    print("  (count/category 흔들림 = 실격 사유. kind/text = 기록만. 사전 커밋 e9b9c2e)")
    for model in models:
        print(f"\n  {model}")
        for job in jobs:
            reps = data[model][job.stem]
            counts = [r["requirements"] for r in reps]
            fps = {tuple(r["fingerprint"]) for r in reps}
            cats = {tuple(sorted((t, c) for t, c, _ in r["fingerprint"])) for r in reps}
            count_ok = len(set(counts)) == 1
            flags = []
            if not count_ok:
                flags.append("★count")
            if len(cats) > 1:
                flags.append("★category")
            if len(fps) > 1 and count_ok and len(cats) == 1:
                flags.append("kind/text")
            print(
                f"    {job.stem:<8} 개수 {counts} "
                f"{'동일' if len(fps) == 1 else ' / '.join(flags) + ' 흔들림'}"
            )

    # ---------- 원자료 저장 (항목 6~9 검토용) ----------
    args.out.parent.mkdir(parents=True, exist_ok=True)
    dest = args.out
    dest.write_text(
        json.dumps(
            {
                m: {j: [{k: v for k, v in r.items()} for r in reps] for j, reps in d.items()}
                for m, d in data.items()
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n원자료 저장: {dest} (요구사항 원문 — OR조건/역방향/영어/지문 검토용)")

    print("\n" + "=" * 66)
    print("판정하지 않는다. 이 표를 보고 사람이 결정한다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
