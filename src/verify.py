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


def _sentences(text: str) -> list[str]:
    """이력서를 '완결된 문장' 단위로 쪼갠다. 경계 검사용."""
    units: list[str] = []
    for line in text.splitlines():
        for chunk in re.split(r"(?<=[.!?])\s+", line):
            # 불릿 기호("- ", "• ")는 문장의 일부가 아니다. quote에도 안 들어온다.
            cleaned = normalize(re.sub(r"^[\s\-•*·]+", "", chunk))
            if cleaned:
                units.append(cleaned)
    return units


def _is_sentence_aligned(quote_n: str, units: list[str]) -> bool:
    """quote가 이력서의 완결된 문장 1~2개와 정확히 일치하는가.

    프롬프트는 "근거가 되는 문장 1~2개까지만 인용하라"고 지시한다.
    그 지시를 지켰는지 코드가 확인한다. 어긋나면 부분 인용이거나 문단 통째 복사다.
    """
    for i in range(len(units)):
        for j in (1, 2):
            if i + j <= len(units) and " ".join(units[i : i + j]) == quote_n:
                return True
    return False


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
    units = _sentences(resume_text)
    verified = analysis.model_copy(deep=True)

    demoted = 0
    warnings: list[str] = []

    for ev in verified.evidences:
        if not ev.quote:
            continue

        quote_n = normalize(ev.quote)

        # 강등은 오직 이 조건에만. "원문에 없다" = 지어냈다.
        if quote_n not in haystack:
            demoted += 1
            warnings.append(
                f'[{ev.requirement_id}] quote 강등: 이력서 원문에 없는 인용 — "{_clip(ev.quote)}"'
            )
            ev.quote = None
            ev.status = "없음"
            ev.reason = f"(원문 대조 실패로 강등) {ev.reason}"
            continue

        # 아래는 경고만 한다. 원문에 존재하는 이상 지어낸 것이 아니므로,
        # 강등해서 진짜 근거를 버리는 쪽이 더 손해다.
        if not _is_sentence_aligned(quote_n, units):
            warnings.append(
                f"[{ev.requirement_id}] 부분 인용 의심: 완결된 문장 1~2개와 일치하지 않음 — "
                f'"{_clip(ev.quote)}". 문장 중간을 잘라내면 의미가 뒤집힐 수 있다 '
                f'("경험은 없습니다" → "경험").'
            )
        elif len(ev.quote) > MAX_QUOTE_CHARS:
            warnings.append(
                f"[{ev.requirement_id}] quote가 {len(ev.quote)}자로 너무 김 "
                f"({MAX_QUOTE_CHARS}자 초과). 문단을 통째로 인용했을 가능성."
            )

    return verified, demoted, warnings


def _clip(text: str, limit: int = 40) -> str:
    flat = normalize(text)
    return flat if len(flat) <= limit else flat[:limit] + "…"
