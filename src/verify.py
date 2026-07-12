"""quote 원문 대조 검증.

LLM은 그럴듯한 문장을 지어낸다. 프롬프트로 "복사만 하라"고 아무리 말해도
일부는 요약하거나 매끄럽게 다듬어서 돌려준다. 그건 근거가 아니다.
그래서 코드가 이력서 원문과 직접 대조한다. 이 파일이 이 도구의 신뢰성을 담보한다.
"""

import re
import unicodedata

from .schemas import GapAnalysis

# quote가 이보다 길면 "근거 문장"이 아니라 문단 통째 복사로 본다.
# 원문에는 존재하므로 강등하지는 않는다 — 게으른 것이지 지어낸 것이 아니다.
# 다만 근거로서 쓸모가 떨어지므로 시끄럽게 경고한다.
MAX_QUOTE_CHARS = 200


def normalize(text: str) -> str:
    """대조용 정규화.

    공백/개행/전각문자 차이만 흡수한다. 그 이상 느슨하게 하지 않는다.
    - 느슨하면(예: 조사 제거, 부분 일치) 지어낸 문장이 통과한다. 검증이 무의미해진다.
    - 엄격하면 진짜 인용이 줄바꿈 하나 때문에 강등된다. 그래서 공백만 흡수한다.
    """
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def verify_quotes(analysis: GapAnalysis, resume_text: str) -> tuple[GapAnalysis, int, list[str]]:
    """이력서 원문에 존재하지 않는 quote를 강등한다.

    강등 = quote를 버리고 status를 "없음"으로 내린다.
    지어낸 근거를 근거로 인정하면 갭이 숨어버린다. 이 도구의 존재 이유가 사라진다.

    Returns:
        (검증된 분석, 강등 건수, 경고 목록)
    """
    haystack = normalize(resume_text)
    verified = analysis.model_copy(deep=True)

    demoted = 0
    warnings: list[str] = []

    for ev in verified.evidences:
        if not ev.quote:
            continue

        if normalize(ev.quote) not in haystack:
            demoted += 1
            warnings.append(
                f'[{ev.requirement_id}] quote 강등: 이력서 원문에 없는 인용 — "{_clip(ev.quote)}"'
            )
            ev.quote = None
            ev.status = "없음"
            ev.reason = f"(원문 대조 실패로 강등) {ev.reason}"
            continue

        if len(ev.quote) > MAX_QUOTE_CHARS:
            warnings.append(
                f"[{ev.requirement_id}] quote가 {len(ev.quote)}자로 너무 김 "
                f"({MAX_QUOTE_CHARS}자 초과). 문단을 통째로 인용했을 가능성."
            )

    return verified, demoted, warnings


def _clip(text: str, limit: int = 40) -> str:
    flat = normalize(text)
    return flat if len(flat) <= limit else flat[:limit] + "…"
