"""Pydantic 모델 정의.

LLM 구조화 출력용 모델(Step 1/2/3)과, 실행 기록용 모델(토큰/지연/원본 응답)을 함께 둔다.
5일짜리 프로젝트이므로 베이스 클래스나 상속 계층은 만들지 않는다. 전부 평평한 모델.
"""

from typing import Literal

from pydantic import BaseModel, Field

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

MODEL = "gpt-4o-mini"

# 재현성. 프롬프트를 고친 효과를 보려면 샘플링 노이즈를 없애야 한다.
TEMPERATURE = 0.0

# gpt-4o-mini 단가 (USD / 1M tokens). 비용 추정에만 쓴다.
PRICE_INPUT_PER_1M = 0.15
PRICE_OUTPUT_PER_1M = 0.60


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

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_usd(self) -> float:
        return (
            self.prompt_tokens / 1_000_000 * PRICE_INPUT_PER_1M
            + self.completion_tokens / 1_000_000 * PRICE_OUTPUT_PER_1M
        )


class RunRecord(BaseModel):
    """out/run_<timestamp>.json 에 그대로 직렬화되는 실행 스냅샷.

    프롬프트를 고친 뒤 이전 실행과 비교하는 것이 목적이므로,
    파싱된 결과뿐 아니라 각 Step의 '원본 LLM 응답 문자열'도 그대로 보관한다.
    """

    timestamp: str
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

    @property
    def total_latency_s(self) -> float:
        return sum(u.latency_s for u in self.usages)

    @property
    def total_tokens(self) -> int:
        return sum(u.total_tokens for u in self.usages)

    @property
    def total_cost_usd(self) -> float:
        return sum(u.cost_usd for u in self.usages)
