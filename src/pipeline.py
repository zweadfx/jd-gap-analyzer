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
import json
import re
import time
import unicodedata
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
    """모델에게 실제로 전달되는 모든 것의 해시.

    = prompts.py 원문 + LLM 응답 스키마(JSON schema)

    응답 스키마를 반드시 포함해야 한다. Pydantic 모델의 필드명과 Field(description=...)은
    JSON schema로 변환돼 API로 전달된다 — 그것도 사실상 프롬프트다.
    스키마 설명 한 줄만 고쳐도 모델 행동이 바뀌는데 prompt_hash가 그대로면,
    "같은 프롬프트인데 결과가 달라졌다"고 잘못 결론 내리게 된다. 관측 가능성이 조용히 깨진다.

    RunSummary/RunRecord 같은 로깅 전용 모델은 API로 나가지 않으므로 제외한다.
    """
    h = hashlib.sha256()
    h.update(Path(__file__).with_name("prompts.py").read_bytes())
    for model in (JobRequirements, GapAnalysis, Suggestions):
        schema = json.dumps(model.model_json_schema(), sort_keys=True, ensure_ascii=False)
        h.update(schema.encode())
    return h.hexdigest()[:12]


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


# --- Top 3 진입 차단 필터 (코드가 막는다. 프롬프트를 믿지 않는다) ---
#
# 프롬프트에 "인성 항목 제외", "조건 완화 문구 제외"를 넣었는데도 뚫린다.
# 실측: job04 2회차 Top 3에 "학력무관, 경력무관"이 실제로 올라왔다.
# quote 검증과 같은 원칙 — 프롬프트로 지시하고, 코드로 검사한다.
#
# **조용히 거르지 않는다.** 걸러낸 항목은 warnings에 남기고 "그 외" 목록에는 그대로 둔다.
# Top 3에서만 뺀다. 조용한 필터는 컨벤션 1조 위반이다.

# 조건 완화 문구: 요구사항이 아니라 "요구사항이 없다"는 선언이다.
_RELAX_TOKEN = re.compile(r"(학력|경력|전공|나이|성별|연령)\s*무관|무관\s*$|경력\s*무관")

# 실질 요건을 함께 담고 있으면 지우면 안 된다.
# "이공계(전공 무관) 석사 이상"은 진짜 요구사항이다 — "석사 이상"이 실질이다.
_SUBSTANTIVE = re.compile(
    r"[A-Za-z]{2,}|\d\s*년|석사|박사|학사|경험|구축|개발|운영|설계|구현|능숙|이상"
)

# 인성 항목: 지원 문서에서 문장으로 인용할 수 없는 것들.
# 실제 공고에서 나온 문구만 넣는다. 넓게 잡으면 진짜 요구사항을 죽인다 —
# "전 주기를 주도적으로 수행", "영어 커뮤니케이션 능력"은 검증 가능한 항목이라 건드리지 않는다.
_PERSONALITY = re.compile(
    r"오픈\s*마인드|책임감\s*있게|창의적인\s*의견|피드백을\s*수용"
    r"|열정적|성실한\s*분|함께\s*성장|긍정적인\s*(자세|태도)|유연한\s*사고"
)


def _blocked_from_top3(text: str) -> str | None:
    """Top 3에 올리면 안 되는 항목이면 이유를, 아니면 None.

    보수적으로 잡는다. 애매하면 통과시킨다 — 진짜 요구사항을 Top 3에서 빼는 것이
    무의미한 항목 하나를 남기는 것보다 나쁘다(거짓 갭이 아니라 갭 은폐가 된다).
    """
    if _RELAX_TOKEN.search(text) and not _SUBSTANTIVE.search(text):
        return "조건 완화 문구 (요구사항이 아니라 요구사항이 없다는 선언)"
    if _PERSONALITY.search(text):
        return "검증 불가능한 인성 항목 (지원 문서에서 인용할 수 없다)"
    return None


def _source_position(text: str, job_text: str) -> int:
    """이 요구사항이 공고 원문의 몇 번째 줄에서 왔는가. **결정적인 키다.**

    기존 타이브레이커는 `order[r.id]` — LLM이 뱉은 리스트의 인덱스였다.
    docstring은 "공고에 먼저 나온 순서"라고 주장했지만 거짓이었다.
    실측: 같은 입력 2회 실행에서 리스트 순서가 19건 뒤집혔고, 그 결과
    Top 3의 2/3이 실행마다 바뀌었다(무작위 추출과 구별되지 않는 수준).

    공고 원문에서의 위치는 LLM 출력이 아니라 **입력**이므로 절대 흔들리지 않는다.
    요구사항 텍스트는 LLM이 바꿔 쓰지만, 어느 줄에서 왔는지는 토큰 겹침으로 찾는다.
    """
    lines = job_text.splitlines()
    want = _tokens(text)
    if not want:
        return len(lines)
    best_i, best_score = len(lines), 0.0
    for i, line in enumerate(lines):
        have = _tokens(line)
        if not have:
            continue
        score = len(want & have) / len(want | have)
        if score > best_score:
            best_score, best_i = score, i
    return best_i


def _tokens(text: str) -> set[str]:
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"[^\w가-힣]+", " ", t)
    return {w for w in t.split() if len(w) >= 2}


def rank_gaps(
    requirements: list[Requirement],
    analysis: GapAnalysis,
    job_text: str = "",
) -> tuple[list[Requirement], list[Requirement], list[str]]:
    """근거 없는 항목을 우선순위로 정렬한다. LLM을 쓰지 않는다.

    우선순위: 필수 > 우대, 기술/경험 > 도메인 > 소프트스킬,
    동점이면 **공고 원문의 등장 순서**(LLM 출력이 아니라 입력이므로 결정적이다).

    verify 이후에 호출해야 한다 — 강등된 항목이 후보에 포함되어야 하기 때문.

    Returns:
        (eligible, blocked, warnings)
        blocked는 **버리지 않는다.** Top 3에만 못 올라갈 뿐, "그 외" 목록에는 그대로 나온다.
        조용히 지우면 그것이야말로 갭 은폐다. (컨벤션 1조: 조용한 실패 금지)
    """
    missing_ids = {ev.requirement_id for ev in analysis.evidences if ev.status == "없음"}
    gaps = [r for r in requirements if r.id in missing_ids]

    warnings: list[str] = []
    eligible: list[Requirement] = []
    blocked: list[Requirement] = []
    for r in gaps:
        reason = _blocked_from_top3(r.text)
        if reason:
            blocked.append(r)
            warnings.append(f'[{r.id}] Top 3에서 제외: {reason} — "{r.text[:40]}"')
        else:
            eligible.append(r)

    # 타이브레이커: LLM 리스트 순서가 아니라 공고 원문 위치.
    # job_text가 없으면(구 호출부) 폴백으로 리스트 순서를 쓴다 — 흔들리는 키다.
    fallback = {r.id: i for i, r in enumerate(requirements)}

    def sort_key(r: Requirement) -> tuple:
        pos = _source_position(r.text, job_text) if job_text else fallback[r.id]
        return (
            CATEGORY_RANK.get(r.category, 9),
            KIND_RANK.get(r.kind, 9),
            pos,
            r.text,  # 위치까지 같으면 텍스트로. 완전 결정적으로 만든다.
        )

    eligible.sort(key=sort_key)
    blocked.sort(key=sort_key)
    return eligible, blocked, warnings


def select_top_gaps(
    requirements: list[Requirement],
    analysis: GapAnalysis,
    n: int = 3,
    job_text: str = "",
) -> tuple[list[Requirement], list[str]]:
    """Top n. **차단된 항목은 절대 여기 들어오지 않는다.**

    eligible이 n개보다 적으면 그만큼만 낸다. 무의미한 항목으로 자리를 채우느니
    빈 자리가 낫다 — 유저가 보는 3칸은 이 제품의 전부다.
    """
    eligible, _blocked, warnings = rank_gaps(requirements, analysis, job_text)
    return eligible[:n], warnings


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
    """파일 경로로 실행한다. cli.py 전용 얇은 래퍼.

    길이 검증은 load_inputs가 한다(초과 시 InputTooLongError, 자동으로 자르지 않음).
    """
    job_text, resume_text = load_inputs(job_path, resume_path)
    return analyze(job_text, resume_text, job_path=str(job_path), resume_path=str(resume_path))


def analyze(
    job_text: str,
    resume_text: str,
    *,
    job_path: str = "<web>",
    resume_path: str = "<web>",
) -> RunRecord:
    """Step 1 → Step 2 → verify → Top3 선정 → Step 3.

    텍스트를 직접 받는다. CLI(파일)와 웹(붙여넣기)이 같은 함수를 쓰게 하려는 것이다.
    파이프라인이 두 벌이 되면 웹에서만 나는 버그가 생긴다.

    job_path/resume_path는 기록용 라벨일 뿐이다. 웹에서는 경로가 없으므로 "<web>".
    """
    if len(job_text) > MAX_JOB_CHARS:
        raise InputTooLongError(
            f"공고가 {len(job_text):,}자로 제한({MAX_JOB_CHARS:,}자)을 넘었습니다. "
            f"직접 줄여서 다시 시도하세요. 자동으로 자르지 않습니다."
        )
    if len(resume_text) > MAX_RESUME_CHARS:
        raise InputTooLongError(
            f"이력서가 {len(resume_text):,}자로 제한({MAX_RESUME_CHARS:,}자)을 넘었습니다. "
            f"직접 줄여서 다시 시도하세요. 자동으로 자르지 않습니다."
        )
    if not job_text.strip():
        raise InputTooLongError("공고가 비어 있습니다.")
    if not resume_text.strip():
        raise InputTooLongError("이력서가 비어 있습니다.")

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
    eligible, blocked, top3_warnings = rank_gaps(
        requirements.requirements, analysis, job_text=job_text
    )
    top_gaps = eligible[:3]
    warnings.extend(top3_warnings)

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
        # 지어내기율의 분모. 반드시 '검증 전'에서 센다 - 검증 후에는 가짜 quote가
        # 이미 None으로 지워져 있어서, 지어낸 적이 없는 것처럼 보인다.
        quotes_offered=sum(1 for e in analysis_raw.evidences if e.quote),
        demoted_count=hallucination_count,
        # 검증 후 살아남은 근거. 게으른 모델(전부 "없음")을 잡아내는 짝 지표.
        evidence_found=sum(1 for e in analysis.evidences if e.status in ("충분", "약함")),
        latency_s=sum(u.latency_s for u in usages),
        tokens_in=sum(u.prompt_tokens for u in usages),
        tokens_out=sum(u.completion_tokens for u in usages),
        cost_usd=sum(u.cost_usd for u in usages),
    )

    return RunRecord(
        timestamp=datetime.now().strftime("%Y%m%d_%H%M%S"),
        summary=summary,
        model=MODEL,
        job_path=job_path,
        resume_path=resume_path,
        job_chars=len(job_text),
        resume_chars=len(resume_text),
        prompts={"step1": prompt1, "step2": prompt2, "step3": prompt3},
        raw_responses={"step1": raw1, "step2": raw2, "step3": raw3},
        requirements=requirements,
        analysis=analysis,
        analysis_before_verify=analysis_raw,
        suggestions=suggestions,
        top_gap_ids=[r.id for r in top_gaps],
        ranked_gap_ids=[r.id for r in eligible] + [r.id for r in blocked],
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
