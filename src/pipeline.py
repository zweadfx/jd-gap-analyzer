"""Step 1/2/3 오케스트레이션.

설계 요지
- 공개 진입점은 run_pipeline() 하나. cli.py는 이것만 부르고 출력만 담당한다.
- LLM 호출은 call_structured() 하나로 통일한다. 재시도/계측/원본응답 보관이 세 Step에
  똑같이 필요한데, Step마다 복붙하면 프롬프트 튜닝할 때 어긋난다.
- select_top_gaps()는 LLM을 쓰지 않는다. 순수 함수. 우선순위는 결정론적이어야 한다.
- 검증(verify)은 Step 2와 Step 3 '사이'에 들어간다. 강등된 항목이 Top 3 후보에
  포함되어야 하기 때문이다. (지어낸 근거를 근거로 인정하면 갭이 숨는다)
"""

import hashlib
import time
from datetime import datetime
from pathlib import Path

from openai import LengthFinishReasonError, OpenAI
from pydantic import BaseModel, ValidationError

from . import prompts
from .schemas import (
    BULLETS_PER_GAP,
    MAX_JOB_CHARS,
    MAX_REQUIREMENTS,
    MAX_RESUME_CHARS,
    MIN_REQUIREMENTS,
    MODEL,
    TEMPERATURE,
    GapAnalysis,
    JobRequirements,
    Requirement,
    RunRecord,
    RunSummary,
    StepUsage,
    Suggestions,
)
from .verify import verify_quotes

# 샘플 입력만 out/에 커밋한다. 이 밖의 입력(실제 이력서)은 out/private/로 간다.
SAMPLES_DIR = Path("data/samples")


def prompt_hash() -> str:
    """src/prompts.py 내용의 해시.

    실행 결과와 함께 저장해서, 나중에 "강등률이 낮았던 실행들은 어떤 프롬프트
    버전이었나"를 커밋 로그를 뒤지지 않고 코드로 뽑을 수 있게 한다.
    """
    source = Path(__file__).with_name("prompts.py").read_bytes()
    return hashlib.sha256(source).hexdigest()[:12]


def resolve_out_dir(resume_path: Path, base: Path = Path("out")) -> Path:
    """실제 이력서로 돌린 결과가 out/에 남지 않게 코드로 막는다.

    out/run_*.json에는 이력서 전문이 들어간다(step2 프롬프트에 통째로 박힌다).
    out/은 커밋되는 디렉터리이므로, 샘플이 아닌 입력은 gitignore된 out/private/로 보낸다.
    규율에 맡기면 언젠가 한 번은 실수한다. 그 한 번이 public 레포에 영구히 남는다.
    """
    try:
        resume_path.resolve().relative_to(SAMPLES_DIR.resolve())
    except ValueError:
        return base / "private"
    return base


class InputTooLongError(ValueError):
    """공고/이력서가 길이 제한을 넘음. 자동으로 자르지 않고 실패시킨다."""


class LLMParseError(RuntimeError):
    """재시도 1회 후에도 구조화 파싱 실패."""


# ---------------------------------------------------------------------------
# 입력
# ---------------------------------------------------------------------------


def load_inputs(job_path: Path, resume_path: Path) -> tuple[str, str]:
    """공고/이력서를 읽고 길이를 검증한다.

    절대 자동으로 자르지 않는다 — 조용한 품질 저하가 최악이므로 시끄럽게 실패한다.
    """
    job_text = job_path.read_text(encoding="utf-8").strip()
    resume_text = resume_path.read_text(encoding="utf-8").strip()

    if len(job_text) > MAX_JOB_CHARS:
        raise InputTooLongError(
            f"공고가 {len(job_text):,}자로 제한({MAX_JOB_CHARS:,}자)을 넘었습니다. "
            f"직접 줄여서 다시 실행하세요. 자동으로 자르지 않습니다."
        )
    if len(resume_text) > MAX_RESUME_CHARS:
        raise InputTooLongError(
            f"이력서가 {len(resume_text):,}자로 제한({MAX_RESUME_CHARS:,}자)을 넘었습니다. "
            f"직접 줄여서 다시 실행하세요. 자동으로 자르지 않습니다."
        )
    if not job_text:
        raise InputTooLongError(f"공고가 비어 있습니다: {job_path}")
    if not resume_text:
        raise InputTooLongError(f"이력서가 비어 있습니다: {resume_path}")

    return job_text, resume_text


# ---------------------------------------------------------------------------
# 공용 LLM 호출 (세 Step이 공유)
# ---------------------------------------------------------------------------


def call_structured[T: BaseModel](
    client: OpenAI,
    *,
    step: str,
    system: str,
    user: str,
    schema: type[T],
    max_retries: int = 1,
) -> tuple[T, StepUsage, str]:
    """구조화 출력 호출 + 파싱 실패 시 1회 재시도.

    재시도는 파싱/거부/길이초과에만 한다. 인증·네트워크 오류는 재시도해도 소용없으므로
    그대로 올려보내 cli가 처리한다. (컨벤션: 무한 재시도 금지 — 비용)

    Returns:
        (파싱된 모델, 계측값, 원본 응답 문자열)
    """
    started = time.perf_counter()
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            completion = client.chat.completions.parse(
                model=MODEL,
                temperature=TEMPERATURE,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=schema,
            )
            message = completion.choices[0].message

            if message.refusal:
                raise LLMParseError(f"{step}: 모델이 응답을 거부했습니다 — {message.refusal}")
            if message.parsed is None:
                raise LLMParseError(f"{step}: 구조화 파싱 결과가 비었습니다")

            usage = StepUsage(
                step=step,
                model=MODEL,
                latency_s=time.perf_counter() - started,
                prompt_tokens=completion.usage.prompt_tokens if completion.usage else 0,
                completion_tokens=completion.usage.completion_tokens if completion.usage else 0,
                retried=attempt > 0,
            )
            # 원본 응답 문자열을 그대로 보관한다. 프롬프트를 고친 뒤 diff를 뜨려면
            # 파싱된 객체가 아니라 모델이 실제로 뱉은 바이트가 필요하다.
            raw = message.content or ""
            return message.parsed, usage, raw

        except (ValidationError, LengthFinishReasonError, LLMParseError) as exc:
            last_error = exc
            if attempt >= max_retries:
                break

    raise LLMParseError(
        f"{step}: 재시도 {max_retries}회 후에도 구조화 응답 파싱에 실패했습니다.\n"
        f"마지막 오류: {last_error}"
    ) from last_error


# ---------------------------------------------------------------------------
# Step 1 — 공고 → 요구사항 (이력서 미투입)
# ---------------------------------------------------------------------------


def extract_requirements(
    client: OpenAI, job_text: str
) -> tuple[JobRequirements, StepUsage, str, str]:
    """Returns: (요구사항, 계측값, 원본응답, 전송한 user 프롬프트)

    resume_text를 인자로 받지 않는다 — 시그니처 자체로 편향을 차단한다.
    """
    user = prompts.build_step1_user(job_text)
    parsed, usage, raw = call_structured(
        client,
        step="step1_requirements",
        system=prompts.STEP1_SYSTEM,
        user=user,
        schema=JobRequirements,
    )
    return parsed, usage, raw, user


# ---------------------------------------------------------------------------
# Step 2 — 요구사항 + 이력서 → 근거 매칭
# ---------------------------------------------------------------------------


def match_evidence(
    client: OpenAI, requirements: list[Requirement], resume_text: str
) -> tuple[GapAnalysis, StepUsage, str, str]:
    """Returns: (근거 매칭 결과, 계측값, 원본응답, 전송한 user 프롬프트)

    여기서 나온 quote는 아직 신뢰할 수 없다. verify.verify_quotes()를 통과해야 한다.
    """
    user = prompts.build_step2_user(requirements, resume_text)
    parsed, usage, raw = call_structured(
        client, step="step2_evidence", system=prompts.STEP2_SYSTEM, user=user, schema=GapAnalysis
    )
    return parsed, usage, raw, user


# ---------------------------------------------------------------------------
# Top 3 선정 — 순수 함수, LLM 없음
# ---------------------------------------------------------------------------

CATEGORY_RANK = {"필수": 0, "우대": 1}
KIND_RANK = {"기술": 0, "경험": 0, "도메인": 1, "소프트스킬": 2}


def select_top_gaps(
    requirements: list[Requirement], analysis: GapAnalysis, n: int = 3
) -> list[Requirement]:
    """status="없음"인 항목을 우선순위로 정렬해 상위 n개.

    우선순위: 필수 > 우대, 그리고 기술/경험 > 도메인 > 소프트스킬.
    동점이면 공고에 먼저 나온 순서로 안정 정렬한다.

    verify 이후에 호출해야 한다 — 강등된 항목이 후보에 포함되어야 하기 때문.
    """
    missing_ids = {ev.requirement_id for ev in analysis.evidences if ev.status == "없음"}
    gaps = [r for r in requirements if r.id in missing_ids]

    order = {r.id: i for i, r in enumerate(requirements)}
    gaps.sort(
        key=lambda r: (
            CATEGORY_RANK.get(r.category, 9),
            KIND_RANK.get(r.kind, 9),
            order[r.id],
        )
    )
    return gaps[:n]


# ---------------------------------------------------------------------------
# Step 3 — 보완 bullet
# ---------------------------------------------------------------------------


def generate_suggestions(
    client: OpenAI, role_summary: str, gaps: list[Requirement], resume_text: str
) -> tuple[Suggestions, StepUsage | None, str, str]:
    """Returns: (보완 제안, 계측값, 원본응답, 전송한 user 프롬프트)

    gaps가 비어 있으면(= 갭 없음) LLM을 호출하지 않는다. 부를 이유가 없다.
    """
    if not gaps:
        return Suggestions(suggestions=[]), None, "", ""

    user = prompts.build_step3_user(role_summary, gaps, resume_text)
    parsed, usage, raw = call_structured(
        client, step="step3_suggestions", system=prompts.STEP3_SYSTEM, user=user, schema=Suggestions
    )
    return parsed, usage, raw, user


# ---------------------------------------------------------------------------
# 오케스트레이션
# ---------------------------------------------------------------------------


def run_pipeline(job_path: Path, resume_path: Path) -> RunRecord:
    """Step 1 → Step 2 → verify → Top3 선정 → Step 3.

    cli.py가 부르는 유일한 함수. 출력 포매팅은 하지 않는다.
    """
    job_text, resume_text = load_inputs(job_path, resume_path)
    client = OpenAI()

    warnings: list[str] = []
    usages: list[StepUsage] = []

    # --- Step 1: 공고만 본다 ---
    requirements, usage1, raw1, prompt1 = extract_requirements(client, job_text)
    usages.append(usage1)

    count = len(requirements.requirements)
    if not MIN_REQUIREMENTS <= count <= MAX_REQUIREMENTS:
        # 조용히 자르지 않는다. 결과는 그대로 쓰되 시끄럽게 알린다.
        warnings.append(
            f"요구사항이 {count}개입니다 (기대: {MIN_REQUIREMENTS}~{MAX_REQUIREMENTS}개). "
            f"공고가 너무 짧거나 Step 1 프롬프트를 손봐야 할 수 있습니다."
        )

    # --- Step 2: 요구사항 + 이력서 ---
    analysis_raw, usage2, raw2, prompt2 = match_evidence(
        client, requirements.requirements, resume_text
    )
    usages.append(usage2)

    # --- 검증: 지어낸 quote를 강등한다 (Step 3보다 반드시 먼저) ---
    analysis, hallucination_count, verify_warnings = verify_quotes(analysis_raw, resume_text)
    warnings.extend(verify_warnings)

    # --- Top 3 선정: 코드가 한다 ---
    top_gaps = select_top_gaps(requirements.requirements, analysis)

    # --- Step 3: 보완 bullet ---
    suggestions, usage3, raw3, prompt3 = generate_suggestions(
        client, requirements.role_summary, top_gaps, resume_text
    )
    if usage3 is not None:
        usages.append(usage3)

    for s in suggestions.suggestions:
        if len(s.bullets) != BULLETS_PER_GAP:
            warnings.append(
                f"[{s.requirement_id}] 보완 bullet이 {len(s.bullets)}개입니다 "
                f"(기대: {BULLETS_PER_GAP}개). 잘라내지 않고 그대로 출력합니다."
            )

    summary = RunSummary(
        model=MODEL,
        temperature=TEMPERATURE,
        prompt_hash=prompt_hash(),
        requirements_count=len(requirements.requirements),
        demoted_count=hallucination_count,
        latency_s=sum(u.latency_s for u in usages),
        tokens_in=sum(u.prompt_tokens for u in usages),
        tokens_out=sum(u.completion_tokens for u in usages),
        cost_usd=sum(u.cost_usd for u in usages),
    )

    return RunRecord(
        timestamp=datetime.now().strftime("%Y%m%d_%H%M%S"),
        summary=summary,
        model=MODEL,
        job_path=str(job_path),
        resume_path=str(resume_path),
        job_chars=len(job_text),
        resume_chars=len(resume_text),
        prompts={"step1": prompt1, "step2": prompt2, "step3": prompt3},
        raw_responses={"step1": raw1, "step2": raw2, "step3": raw3},
        requirements=requirements,
        analysis=analysis,
        analysis_before_verify=analysis_raw,
        suggestions=suggestions,
        top_gap_ids=[r.id for r in top_gaps],
        hallucination_count=hallucination_count,
        usages=usages,
        warnings=warnings,
    )


def save_run(record: RunRecord, base: Path = Path("out")) -> Path:
    """run_<timestamp>.json 으로 저장하고 경로를 돌려준다.

    샘플 입력이면 out/(커밋됨), 아니면 out/private/(커밋 안 됨)에 쓴다.
    """
    out_dir = resolve_out_dir(Path(record.resume_path), base)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"run_{record.timestamp}.json"
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return path
