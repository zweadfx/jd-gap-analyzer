"""진입점 + 출력 포맷팅.

여기엔 분석 로직이 없다. run_pipeline()을 부르고 결과를 사람이 읽게 찍는 것뿐이다.
경고는 stdout이 아니라 stderr로 보낸다 — 결과를 파이프로 넘겨도 경고가 묻히지 않는다.

  uv run python -m src.cli --job data/samples/job1.txt --resume data/samples/resume.txt
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import APIError, AuthenticationError

from .pipeline import InputTooLongError, LLMParseError, run_pipeline, save_run
from .schemas import Evidence, Requirement, RunRecord

RULE = "─" * 25


def main() -> int:
    parser = argparse.ArgumentParser(description="채용 공고 대비 이력서 갭 분석기")
    parser.add_argument("--job", required=True, type=Path, help="채용 공고 텍스트 파일")
    parser.add_argument("--resume", required=True, type=Path, help="이력서 텍스트 파일")
    parser.add_argument("--out", type=Path, default=Path("out"), help="원본 JSON 저장 위치")
    args = parser.parse_args()

    # 로컬에서 바로 알 수 있는 오류를 먼저 본다. 파일 오타를 "키가 없다"고 알리면 헷갈린다.
    for path in (args.job, args.resume):
        if not path.is_file():
            print(f"파일을 찾을 수 없습니다: {path}", file=sys.stderr)
            return 1

    load_dotenv()
    if not os.getenv("OPENAI_API_KEY"):
        print(
            "OPENAI_API_KEY가 없습니다. .env.example을 .env로 복사하고 키를 넣으세요.",
            file=sys.stderr,
        )
        return 1

    try:
        record = run_pipeline(args.job, args.resume)
    except InputTooLongError as exc:
        print(f"입력 오류: {exc}", file=sys.stderr)
        return 1
    except LLMParseError as exc:
        print(f"LLM 응답 처리 실패: {exc}", file=sys.stderr)
        return 1
    except AuthenticationError:
        print("OpenAI 인증 실패. .env의 OPENAI_API_KEY를 확인하세요.", file=sys.stderr)
        return 1
    except APIError as exc:
        print(f"OpenAI API 오류: {exc}", file=sys.stderr)
        return 1

    saved = save_run(record, args.out)
    render(record)

    for warning in record.warnings:
        print(f"경고: {warning}", file=sys.stderr)
    print(f"원본 응답 저장: {saved}", file=sys.stderr)

    return 0


def render(record: RunRecord) -> None:
    reqs = {r.id: r for r in record.requirements.requirements}
    evs = {e.requirement_id: e for e in record.analysis.evidences}
    bullets = {s.requirement_id: s.bullets for s in record.suggestions.suggestions}

    print(f"\n[역할 요약] {record.requirements.role_summary}\n")

    # --- 근거 없는 항목 TOP 3 ---
    print("⚠️ 근거 없는 항목 TOP 3")
    print(RULE)
    if not record.top_gap_ids:
        print("없음 — 모든 요구사항에 이력서 근거가 있습니다.")
    for i, rid in enumerate(record.top_gap_ids, start=1):
        req = reqs[rid]
        print(f"{i}. {req.text} ({req.category})")
        if rid in evs:
            print(f"   이유: {evs[rid].reason}")
        if rid in bullets:
            print("   보완:")
            for bullet in bullets[rid]:
                print(f"     • {bullet}")
    print()

    # --- 그 외 근거 없는 항목 ---
    # Top 3만 출력하면 '필수인데 근거 없는' 항목이 4개 이상일 때 나머지가 조용히 사라진다.
    # 갭을 드러내려고 만든 도구가 갭을 숨기게 된다. (컨벤션 1조: 조용한 실패 금지)
    # 보완 bullet은 Top 3에만 만든다 - 여기서는 존재만 알린다.
    rest = [rid for rid in _missing_ids(record) if rid not in record.top_gap_ids]
    if rest:
        print(f"그 외 근거 없는 항목 ({len(rest)}개)")
        print(RULE)
        for rid in rest:
            req = reqs[rid]
            print(f"- {req.text} ({req.category})")
        print()

    # --- 근거 있는 항목 ---
    strong = [e for e in record.analysis.evidences if e.status == "충분"]
    print(f"✅ 근거 있는 항목 ({len(strong)}개)")
    print(RULE)
    for ev in strong:
        _print_evidence(ev, reqs)
    print()

    # --- 약한 항목 ---
    weak = [e for e in record.analysis.evidences if e.status == "약함"]
    print(f"⚠️ 약한 항목 ({len(weak)}개)")
    print(RULE)
    for ev in weak:
        _print_evidence(ev, reqs)
    print()

    # --- 계측 ---
    # 두 지표를 반드시 나란히 찍는다. 하나만 보면 속는다.
    s = record.summary
    print(RULE)
    print(
        f"quote 강등: {s.demoted_count}건 / 모델이 제시한 quote {s.quotes_offered}개 "
        f"→ 지어내기율 {s.hallucination_rate:.0%}"
    )
    print(
        f"근거 발견: {s.evidence_found}개 / 요구사항 {s.requirements_count}개 "
        f"→ 발견율 {s.evidence_rate:.0%}"
    )
    print(
        f"지연: {s.latency_s:.1f}s | "
        f"토큰: {s.tokens_in + s.tokens_out:,} | "
        f"예상 비용: ${s.cost_usd:.4f}"
    )
    print(f"모델: {s.model} | 프롬프트 해시: {s.prompt_hash} | temperature: {s.temperature}")

    # 지어내기율 0%는 정직한 것일 수도, 게으른 것일 수도 있다. 발견율과 같이 봐야 구분된다.
    if s.quotes_offered == 0:
        print("(모델이 quote를 하나도 제시하지 않음 — 검증이 돌 기회조차 없었다)")
    elif s.demoted_count == 0 and s.evidence_rate < 0.5:
        print(
            f"(지어내기 0건이지만 발견율이 {s.evidence_rate:.0%}로 낮음 — "
            f"정직한 것인지 게으른 것인지 구분되지 않는다)"
        )


def _missing_ids(record: RunRecord) -> list[str]:
    """근거 없는 항목 id 전부, 우선순위 순.

    순위는 run_pipeline이 이미 계산해 ranked_gap_ids에 담았다(공고 원문 위치가
    필요한데 여기선 job_text가 없다). 정렬 로직을 두 곳에 두면 조용히 어긋난다.
    """
    return record.ranked_gap_ids


def _print_evidence(ev: Evidence, reqs: dict[str, Requirement]) -> None:
    req = reqs.get(ev.requirement_id)
    label = req.text if req else ev.requirement_id
    print(f"- {label}")
    if ev.quote:
        print(f'  근거: "{ev.quote}" (이력서 원문)')
    else:
        print(f"  근거: {ev.reason}")


if __name__ == "__main__":
    sys.exit(main())
