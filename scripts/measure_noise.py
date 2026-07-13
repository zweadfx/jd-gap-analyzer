"""노이즈 플로어 측정 — D2 프롬프트 튜닝을 할 가치가 있는지 결정하는 게이트.

    uv run python scripts/measure_noise.py [N]        # 기본 5회

temperature=0이 재현성을 보장하지 않는다는 것이 실측으로 확인됐다(같은 입력·같은
prompt_hash로 quote 제시가 3→4로 흔들림). 그래서 지표의 산포를 먼저 잰다.

**이 산포가 최소 검출 가능 효과다.**
프롬프트를 고쳐서 지어내기율이 12%→8%로 움직여도, 노이즈 밴드가 ±10%p면 그건
아무것도 증명하지 못한다. 노이즈보다 작은 개선을 쫓는 것은 자기기만이다.

  노이즈가 작다 → D2 튜닝 진행. "개선했다"를 주장할 근거가 생긴다.
  노이즈가 크다 → D2 튜닝을 접고 D3 웹으로 간다. 그것도 정당한 결론이다.

두 구간을 분리해서 잰다. 대응이 완전히 다르기 때문이다.
  A. 전체(Step1+Step2): 실사용 조건의 총 노이즈
  B. Step1 동결 + Step2만 반복: Step2 자체의 노이즈
  → A만 크고 B가 작으면, 노이즈원은 Step1의 문구 흔들림이 Step2로 캐스케이드한 것이다.
    그렇다면 D2 튜닝 중에는 Step1 출력을 동결해야 한다(변수 하나 제거).
"""

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
from openai import OpenAI  # noqa: E402

from src.pipeline import extract_requirements, match_evidence, prompt_hash  # noqa: E402
from src.schemas import MODEL, TEMPERATURE  # noqa: E402
from src.verify import verify_quotes  # noqa: E402

JOB = Path("data/samples/job1.txt")
RESUME = Path("data/samples/resume.txt")


def measure(analysis, requirements_count: int, resume_text: str) -> dict:
    verified, demoted, _ = verify_quotes(analysis, resume_text)
    offered = sum(1 for e in analysis.evidences if e.quote)
    found = sum(1 for e in verified.evidences if e.status in ("충분", "약함"))
    return {
        "quotes_offered": offered,
        "demoted": demoted,
        "evidence_found": found,
        "requirements": requirements_count,
        "hallucination_rate": demoted / offered if offered else 0.0,
        "evidence_rate": found / requirements_count if requirements_count else 0.0,
    }


def spread(label: str, values: list[float], pct: bool = True) -> str:
    lo, hi = min(values), max(values)
    med = statistics.median(values)
    fmt = (lambda v: f"{v:.0%}") if pct else (lambda v: f"{v:.1f}")
    band = hi - lo
    band_s = f"{band:.0%}p" if pct else f"{band:.1f}"
    return f"  {label:<14} 중앙값 {fmt(med):>6} | 범위 {fmt(lo)}~{fmt(hi)} | 밴드 {band_s}"


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    load_dotenv(".env")
    client = OpenAI()

    job_text = JOB.read_text(encoding="utf-8").strip()
    resume_text = RESUME.read_text(encoding="utf-8").strip()

    print(f"모델 {MODEL} | temperature {TEMPERATURE} | prompt_hash {prompt_hash()}")
    print(f"입력 {JOB.name} × {RESUME.name} | 반복 {n}회\n")

    # --- A. 전체 파이프라인 (Step1 + Step2) ---
    print("A. 전체 (Step1 + Step2) — 실사용 조건의 총 노이즈")
    print("─" * 62)
    a_runs = []
    for i in range(n):
        reqs, _, _, _ = extract_requirements(client, job_text)
        analysis, _, _, _ = match_evidence(client, reqs.requirements, resume_text)
        m = measure(analysis, len(reqs.requirements), resume_text)
        a_runs.append(m)
        print(
            f"  {i + 1}회: 요구사항 {m['requirements']:>2} | quote {m['quotes_offered']:>2} "
            f"| 강등 {m['demoted']} | 발견 {m['evidence_found']:>2} "
            f"({m['evidence_rate']:.0%})"
        )
    print()
    print(spread("요구사항 수", [r["requirements"] for r in a_runs], pct=False))
    print(spread("지어내기율", [r["hallucination_rate"] for r in a_runs]))
    print(spread("발견율", [r["evidence_rate"] for r in a_runs]))

    # --- B. Step1 동결 + Step2만 반복 ---
    print("\nB. Step1 동결 + Step2만 반복 — Step2 자체의 노이즈")
    print("─" * 62)
    frozen, _, _, _ = extract_requirements(client, job_text)
    print(f"  (동결된 요구사항 {len(frozen.requirements)}개를 매 회 동일하게 입력)")
    b_runs = []
    for i in range(n):
        analysis, _, _, _ = match_evidence(client, frozen.requirements, resume_text)
        m = measure(analysis, len(frozen.requirements), resume_text)
        b_runs.append(m)
        print(
            f"  {i + 1}회: quote {m['quotes_offered']:>2} | 강등 {m['demoted']} "
            f"| 발견 {m['evidence_found']:>2} ({m['evidence_rate']:.0%})"
        )
    print()
    print(spread("지어내기율", [r["hallucination_rate"] for r in b_runs]))
    print(spread("발견율", [r["evidence_rate"] for r in b_runs]))

    # --- 판정 ---
    #
    # 밴드를 '작다/크다'로 자동 판정하지 않는다. n=5에서 A가 5회 연속 같은 값을 뽑으면
    # 밴드가 0%p로 나오는데, 그건 노이즈가 없다는 뜻이 아니라 운이 좋았다는 뜻이다.
    # 낙관적인 자동 판정은 나중에 자기기만의 근거가 된다. 숫자만 내놓고 판단은 사람이 한다.
    a_band = max(r["evidence_rate"] for r in a_runs) - min(r["evidence_rate"] for r in a_runs)
    b_band = max(r["evidence_rate"] for r in b_runs) - min(r["evidence_rate"] for r in b_runs)
    req_band = max(r["requirements"] for r in a_runs) - min(r["requirements"] for r in a_runs)
    n_reqs = statistics.median(r["requirements"] for r in a_runs)
    quantum = 1 / n_reqs if n_reqs else 0  # 지표의 최소 눈금

    print("\n" + "=" * 62)
    print(f"관측 밴드 (발견율): 전체 {a_band:.0%}p | Step2만 {b_band:.0%}p")
    print(f"양자화 한계: 요구사항 {n_reqs:.0f}개 → 지표는 {quantum:.0%}p 단위로만 움직인다")
    print(f"최소 검출 가능 효과 = max(노이즈, 양자화) ≈ {max(a_band, b_band, quantum):.0%}p")
    print("이보다 작은 개선은 '개선했다'고 주장할 수 없다.\n")

    if req_band == 0:
        print("Step1: 요구사항 수가 전 회차 동일 → Step1은 노이즈원이 아니다.")
        print("       (튜닝 중 Step1 출력을 동결할 필요 없음)")
    else:
        print(f"Step1: 요구사항 수가 {req_band}개 흔들림 → Step2로 캐스케이드한다.")
        print("       튜닝 중에는 Step1 출력을 파일로 동결하고 Step2만 비교할 것.")

    print("\n⚠️ 공고 1건 × 소수 반복으로는 프롬프트 개선을 검출할 수 없다.")
    print("   요구사항 개수가 적어 눈금이 굵다. 공고를 늘려야 눈금이 잘게 쪼개지고")
    print("   노이즈도 평균에서 희석된다. 판단은 공고 10건 집계로만 한다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
