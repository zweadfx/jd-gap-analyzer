"""Pydantic 모델 정의.

LLM 구조화 출력용 모델(Step 1/2/3)과, 실행 기록용 모델(토큰/지연/원본 응답)을 함께 둔다.
5일짜리 프로젝트이므로 베이스 클래스나 상속 계층은 만들지 않는다. 전부 평평한 모델.
"""

from typing import Literal

from pydantic import BaseModel, Field, computed_field

# ---------------------------------------------------------------------------
# 입력 제한 / 모델 / 단가
# ---------------------------------------------------------------------------

MAX_JOB_CHARS = 8_000
MAX_RESUME_CHARS = 12_000

# 개수 제약. OpenAI structured outputs는 minItems/maxItems를 지원하지 않아
# 스키마로 강제할 수 없다. 프롬프트로 지시하고, 벗어나면 코드가 경고를 찍는다.
MIN_REQUIREMENTS = 8
MAX_REQUIREMENTS = 15
BULLETS_PER_GAP = 2

# 실제 공고 7건 × 2회 베이스라인을 보고 사람이 판정했다. (D2)
#   부호 검정      7/7 완봉 (우연일 확률 0.5^7 ≈ 0.8%)
#   발견율        50% vs 35% (4o-mini)
#   quote 0개 실행  0/14 vs 4/14 — 4o-mini는 job03·job06에서 제품이 아예 작동하지 않았다
#   부분 인용 비율   12.1% vs 17.6% — 최종 출력에 남는 위험은 nano가 더 낮다
#   지연          9.5s vs 20.9s — 20.9초는 웹에서 사망 선고다
#
# 지어내기율은 nano가 높지만(11% vs 4%) 그것으로 탈락시키지 않았다. 지어낸 quote는
# 검증기가 강등해 출력에서 제거되므로 유저 피해가 0이다. 반면 게으름(발견율 0%)은
# 거짓 갭을 Top 3에 올리고 방어 장치가 없다. 상세: CONVENTIONS.md "폐기된 규칙 #1".
MODEL = "gpt-5.4-nano"

# temperature=0은 재현성을 보장하지 않는다(D2 실측). 그래도 0으로 두는 이유는
# 노이즈를 줄이기는 하기 때문이다. 다만 "고정했으니 재현된다"고 믿지 않는다.
TEMPERATURE = 0.0

# gpt-5.4-nano 단가 (USD / 1M tokens). 비용 추정에만 쓴다.
# MODEL을 바꾸면 이 두 줄도 반드시 같이 바꾼다. 안 바꾸면 cost_usd가 조용히 틀린다.
PRICE_INPUT_PER_1M = 0.20
PRICE_OUTPUT_PER_1M = 1.25


# ---------------------------------------------------------------------------
# Step 1 — 요구사항 추출 (이력서를 절대 입력하지 않는다)
# ---------------------------------------------------------------------------

Category = Literal["필수", "우대"]
Kind = Literal["기술", "경험", "도메인", "소프트스킬"]


class Requirement(BaseModel):
    id: str = Field(description='"r1", "r2" 형식')
    text: str
    category: Category
    kind: Kind


class JobRequirements(BaseModel):
    role_summary: str
    requirements: list[Requirement]


# ---------------------------------------------------------------------------
# Step 2 — 근거 매칭
# ---------------------------------------------------------------------------

EvidenceStatus = Literal["충분", "약함", "없음"]


class Evidence(BaseModel):
    requirement_id: str
    status: EvidenceStatus
    quote: str | None = Field(description="이력서 원문 그대로. 없으면 null")
    reason: str


class GapAnalysis(BaseModel):
    evidences: list[Evidence]


# ---------------------------------------------------------------------------
# Step 3 — 보완 bullet
# ---------------------------------------------------------------------------


class Suggestion(BaseModel):
    requirement_id: str
    bullets: list[str] = Field(description="보완 bullet 2개")


class Suggestions(BaseModel):
    suggestions: list[Suggestion]


# ---------------------------------------------------------------------------
# 실행 기록 (out/run_<timestamp>.json 저장용)
# ---------------------------------------------------------------------------


class StepUsage(BaseModel):
    """Step 하나의 토큰/지연 계측값."""

    step: str
    model: str
    latency_s: float
    prompt_tokens: int
    completion_tokens: int
    retried: bool = False

    # @property가 아니라 @computed_field여야 한다.
    # 순수 @property는 model_dump_json()에 실리지 않아서, 터미널에는 찍히는데
    # out/run_*.json에는 빠지는 사고가 난다. 기록되지 않는 계측은 없는 것과 같다.
    @computed_field
    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @computed_field
    @property
    def cost_usd(self) -> float:
        return (
            self.prompt_tokens / 1_000_000 * PRICE_INPUT_PER_1M
            + self.completion_tokens / 1_000_000 * PRICE_OUTPUT_PER_1M
        )


class RunSummary(BaseModel):
    """실행 하나를 한 줄로 요약하는 고정 스키마.

    프롬프트를 10번 고친 뒤 "강등률이 낮았던 실행은 어떤 프롬프트 버전이었나"를
    커밋 로그를 손으로 뒤지지 않고 코드로 뽑기 위한 필드들이다.
    필드를 빼거나 이름을 바꾸면 과거 실행과 비교가 끊긴다. 추가만 한다.

    prompt_hash가 핵심이다. prompts.py 파일 전체의 해시라서, 이 값이 같은 실행끼리는
    프롬프트가 완전히 동일하다고 단정할 수 있다.
    """

    model: str
    temperature: float
    prompt_hash: str  # src/prompts.py 내용의 sha256 앞 12자

    requirements_count: int
    quotes_offered: int  # 모델이 quote를 제시한 건수 (검증 전). 지어내기율의 분모.
    demoted_count: int  # quote 원문 대조 실패로 강등된 건수
    evidence_found: int  # 검증 후 살아남은 근거 (충분 + 약함)

    latency_s: float
    tokens_in: int
    tokens_out: int
    cost_usd: float

    # 아래 두 지표는 반드시 같이 본다. 하나만 보면 반드시 속는다.
    #
    #   정직성만 보면 → 게으른 모델이 이긴다 (quote를 아예 안 주면 지어내기율 0%)
    #   성실성만 보면 → 뻥쟁이 모델이 이긴다 (막 지어내면 발견율이 높다)

    @computed_field
    @property
    def hallucination_rate(self) -> float:
        """지어내기율 = 강등 / **quote 제시 수**. 낮을수록 정직하다.

        분모가 requirements_count이면 안 된다. 모델이 "없음"이라고 답한 요구사항에는
        애초에 지어낼 기회가 없었다. 11개 중 3개만 quote를 냈고 1개가 가짜라면
        실제 지어내기율은 33%(1/3)이지, 9%(1/11)가 아니다.
        분모를 키우면 실제보다 안전해 보인다 - 조용한 품질 저하다.
        """
        if self.quotes_offered == 0:
            return 0.0
        return self.demoted_count / self.quotes_offered

    @computed_field
    @property
    def evidence_rate(self) -> float:
        """근거 발견율 = 살아남은 근거 / 요구사항. 높을수록 성실하다.

        지어내기율과 짝으로 본다. 이 값이 낮으면서 지어내기율이 0%인 모델은
        정직한 것이 아니라 게으른 것이다 - 아무것도 안 찾고 전부 "없음"이라 답한 것.
        """
        if self.requirements_count == 0:
            return 0.0
        return self.evidence_found / self.requirements_count


class RunRecord(BaseModel):
    """out/run_<timestamp>.json 에 그대로 직렬화되는 실행 스냅샷.

    프롬프트를 고친 뒤 이전 실행과 비교하는 것이 목적이므로,
    파싱된 결과뿐 아니라 각 Step의 '원본 LLM 응답 문자열'도 그대로 보관한다.
    """

    timestamp: str
    summary: RunSummary  # 실행 간 비교용 고정 스키마. 맨 앞에 둔다.

    model: str
    job_path: str
    resume_path: str
    job_chars: int
    resume_chars: int

    prompts: dict[str, str]  # step -> 실제로 전송한 user 프롬프트
    raw_responses: dict[str, str]  # step -> LLM 원본 응답 문자열(JSON)

    requirements: JobRequirements
    analysis: GapAnalysis  # 검증 강등이 반영된 최종본
    analysis_before_verify: GapAnalysis  # 강등 전 원본 (비교용)
    suggestions: Suggestions

    top_gap_ids: list[str]
    hallucination_count: int
    usages: list[StepUsage]

    # 개수 제약 위반, 과도하게 긴 quote 등. 터미널에도 찍고 여기에도 남긴다.
    warnings: list[str] = Field(default_factory=list)

    # 합계는 summary가 들고 있다. 여기에 @property로 또 두면
    # 직렬화되는 값(summary)과 안 되는 값(property)이 갈려서 어긋난다.
