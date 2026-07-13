"""verify.normalize / verify_quotes 경계 확인.

테스트 프레임워크를 쓰지 않는다(컨벤션: 5일 스코프). 함수 하나다.

    uv run python scripts/check_verify.py

여기 있는 4개 케이스가 normalize의 경계 그 자체다.
README는 "느슨함과 엄격함 사이의 가장 좁은 지점을 잡았다"고 주장하는데,
그 주장을 뒷받침하는 것이 이 파일뿐이다. 케이스가 깨지면 주장도 깨진다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.schemas import Evidence, GapAnalysis  # noqa: E402
from src.verify import verify_quotes  # noqa: E402

RESUME = """\
- FastAPI로 4개 에이전트 API를 구현하고 사내 도구에 연동했습니다.
- Pydantic으로 LLM 구조화 출력을 정의하고, 파싱 실패 시 재시도하는 로직을 작성했습니다.
"""

# 강등은 오직 "원문에 없다"(= 지어냈다)에만 쓴다.
# 원문에 존재하는 인용은 살리되, 부분 인용이면 경고한다.
# 진짜 근거를 강등해서 버리는 것은 갭을 숨기는 것만큼 나쁘다.
#
# (이름, quote, 강등되어야 하는가, 경고가 나와야 하는가)
CASES = [
    (
        "원문 그대로",
        "FastAPI로 4개 에이전트 API를 구현하고 사내 도구에 연동했습니다.",
        False,
        False,
    ),
    (
        "개행/공백만 다름",
        "FastAPI로 4개 에이전트 API를\n  구현하고   사내 도구에 연동했습니다.",
        False,
        False,
    ),
    (
        "LLM이 요약한 문장 (원문에 없음)",
        "FastAPI로 여러 API를 만들고 운영했습니다.",
        True,
        True,
    ),
    (
        "원문의 절반만 (부분 인용)",
        "FastAPI로 4개 에이전트 API를 구현하고",
        False,  # 원문에 실재하므로 지어낸 것이 아니다 → 강등하지 않는다
        True,  # 다만 문장 중간을 자르면 의미가 뒤집힐 수 있다 → 경고한다
    ),
]


def main() -> int:
    failed = 0

    for name, quote, should_demote, should_warn in CASES:
        analysis = GapAnalysis(
            evidences=[Evidence(requirement_id="r1", status="충분", quote=quote, reason="-")]
        )
        _verified, demoted, warnings = verify_quotes(analysis, RESUME)
        was_demoted = demoted == 1
        was_warned = len(warnings) > 0

        ok = was_demoted == should_demote and was_warned == should_warn
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}")
        print(
            f"       강등: 기대={_yn(should_demote)} 실제={_yn(was_demoted)} | "
            f"경고: 기대={_yn(should_warn)} 실제={_yn(was_warned)}"
        )
        for w in warnings:
            print(f"       → {w}")
        if not ok:
            failed += 1

    print()
    if failed:
        print(f"{failed}개 케이스 실패")
        return 1
    print("4개 케이스 전부 통과")
    return 0


def _yn(value: bool) -> str:
    return "O" if value else "X"


if __name__ == "__main__":
    sys.exit(main())
