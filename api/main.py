"""FastAPI — 웹에서 파이프라인을 호출한다.

프론트(Vercel)와 분리된 서비스다(Railway). 배포는 이쪽을 먼저 띄워 URL을 확보한다.

**파이프라인을 두 벌로 만들지 않는다.** CLI와 똑같이 src.pipeline.analyze()를 부른다.
웹용 사본을 만들면 웹에서만 나는 버그가 생기고, 프롬프트를 고칠 때 한쪽을 빠뜨린다.

**응답과 로그에 지원 문서 원문을 남기지 않는다.**
- RunRecord는 프롬프트(=문서 전문)와 원본 응답을 들고 있다. 그대로 반환하면 유출이다.
- to_response()가 유저에게 보여줄 것만 골라 새 dict를 만든다.
- save_run()도 부르지 않는다. 서버에 유저 문서를 파일로 쌓지 않는다.

**anon_id는 프론트가 localStorage에서 만들어 헤더로 보낸다.** 쿠키가 아니다.
Vercel과 Railway는 다른 도메인이라 쿠키가 서드파티가 되어 브라우저에 차단된다.
"""

import os
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from openai import APIError, AuthenticationError
from pydantic import BaseModel, Field

from src import events
from src.pipeline import InputTooLongError, LLMParseError, analyze
from src.schemas import MAX_JOB_CHARS, MAX_RESUME_CHARS, RunRecord

# 로컬 개발용. 배포에서는 환경변수로 주입되며 .env는 없다.
load_dotenv()

# 설정 누락은 기동 시점에 죽는다. 요청마다 500을 뱉게 두면 배포는 "성공"인데
# 첫 유저가 빈 에러를 받는다. 헬스체크가 통과하면 안 되는 상태다.
if not os.getenv("OPENAI_API_KEY"):
    raise RuntimeError("OPENAI_API_KEY가 없습니다. 로컬은 .env, 배포는 환경변수에 설정하세요.")

app = FastAPI(title="jd-gap-analyzer")

ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Anon-Id"],
)

# --- 레이트리밋: IP당 하루 5회 (컨벤션 7장) ---
#
# 인메모리다. 프로세스가 재시작되면 초기화되고, 인스턴스가 여러 개면 각자 센다.
# 5일 프로젝트에 Redis를 붙이지 않는다. 다만 이 한계를 알고 있어야 한다 —
# 비용 하드캡은 이것이 아니라 OpenAI 선불 크레딧 + 자동충전 OFF다.
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_DAY", "5"))
RATE_WINDOW_S = 24 * 3600
_hits: dict[str, deque[float]] = defaultdict(deque)


class AnalyzeRequest(BaseModel):
    job: str = Field(description="채용 공고 원문")
    resume: str = Field(description="지원 문서(이력서 또는 포트폴리오) 원문")


class FeedbackRequest(BaseModel):
    text: str = Field(description="결과 화면의 피드백 한 줄. 파이프라인에 들어가지 않는 순수 로그")


# 샘플 체험용. data/samples/는 커밋된 가상 데이터라 배포 이미지에 항상 있다.
# 프론트에 사본을 두지 않는다 — 원본이 둘이 되면 언젠가 어긋난다.
_SAMPLES_DIR = Path(__file__).resolve().parent.parent / "data" / "samples"


def client_ip(request: Request) -> str:
    # 프록시 뒤에 있으면 X-Forwarded-For의 첫 IP가 진짜다.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limited(ip: str) -> bool:
    now = time.time()
    hits = _hits[ip]
    while hits and now - hits[0] > RATE_WINDOW_S:
        hits.popleft()
    if len(hits) >= RATE_LIMIT:
        return True
    hits.append(now)
    return False


def to_response(record: RunRecord) -> dict:
    """유저에게 보낼 것만 고른다.

    RunRecord에는 prompts(=지원 문서 전문)와 raw_responses가 들어 있다.
    통째로 반환하면 유출이다. 화이트리스트로 새로 만든다.
    """
    reqs = {r.id: r for r in record.requirements.requirements}
    evs = {e.requirement_id: e for e in record.analysis.evidences}
    bullets = {s.requirement_id: s.bullets for s in record.suggestions.suggestions}

    # "그 외 근거 없는 항목"도 내려준다. Top3만 주면 필수 갭이 조용히 사라진다.
    # 순위는 run_pipeline이 이미 계산했다(공고 원문 위치 기반). 여기서 다시 정렬하지 않는다.
    rest = [rid for rid in record.ranked_gap_ids if rid not in record.top_gap_ids]

    def item(rid: str, with_bullets: bool = False) -> dict:
        r, e = reqs[rid], evs.get(rid)
        d = {
            "id": rid,
            "text": r.text,
            "category": r.category,
            "kind": r.kind,
            "reason": e.reason if e else "",
        }
        if with_bullets:
            d["bullets"] = bullets.get(rid, [])
        return d

    s = record.summary
    return {
        "role_summary": record.requirements.role_summary,
        "top_gaps": [item(rid, with_bullets=True) for rid in record.top_gap_ids],
        "other_gaps": [item(rid) for rid in rest],
        "evidence": [
            {
                "id": e.requirement_id,
                "text": reqs[e.requirement_id].text if e.requirement_id in reqs else "",
                "status": e.status,
                "quote": e.quote,
                "reason": e.reason,
            }
            for e in record.analysis.evidences
            if e.status in ("충분", "약함")
        ],
        "metrics": {
            "requirements_count": s.requirements_count,
            "quotes_offered": s.quotes_offered,
            "demoted_count": s.demoted_count,
            "evidence_found": s.evidence_found,
            "latency_s": s.latency_s,
            "model": s.model,
        },
        "warnings": record.warnings,
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True, "limits": {"job": MAX_JOB_CHARS, "resume": MAX_RESUME_CHARS}}


@app.post("/events/page_view")
def track_page_view(x_anon_id: str = Header(default="")) -> dict:
    """랜딩 도착. 재방문은 로그에서 유도한다(같은 anon_id의 page_view가 다른 날에 있으면)."""
    events.log_event("page_view", x_anon_id or events.new_anon_id())
    return {"ok": True}


@app.post("/events/feedback")
def track_feedback(body: FeedbackRequest, x_anon_id: str = Header(default="")) -> dict:
    """결과 화면의 '결과가 이상한가요?' 한 줄. 저장만 하고 아무 데도 쓰지 않는다."""
    text = body.text.strip()
    if text:
        events.log_event("feedback", x_anon_id or events.new_anon_id(), feedback_text=text)
    return {"ok": True}


@app.get("/sample")
def sample() -> dict:
    """샘플 체험 버튼용. 낯선 사이트에 자기 이력서를 바로 붙여넣는 사람은 없다."""
    return {
        "job": (_SAMPLES_DIR / "job1.txt").read_text(encoding="utf-8"),
        "resume": (_SAMPLES_DIR / "resume.txt").read_text(encoding="utf-8"),
    }


@app.post("/analyze")
def analyze_endpoint(
    body: AnalyzeRequest,
    request: Request,
    response: Response,
    x_anon_id: str = Header(default=""),
) -> dict:
    anon_id = x_anon_id or events.new_anon_id()
    ip = client_ip(request)

    events.log_event("submit", anon_id, job_chars=len(body.job), resume_chars=len(body.resume))

    if rate_limited(ip):
        events.log_event("error", anon_id, error_kind="rate_limited")
        response.status_code = 429
        return {"error": f"하루 {RATE_LIMIT}회까지 사용할 수 있습니다. 내일 다시 시도해주세요."}

    try:
        record = analyze(body.job, body.resume)
    except InputTooLongError as exc:
        # 자동으로 자르지 않는다. 시끄럽게 실패한다. (컨벤션 1조)
        events.log_event("error", anon_id, error_kind="input_too_long")
        response.status_code = 400
        return {"error": str(exc)}
    except LLMParseError:
        events.log_event("error", anon_id, error_kind="llm_parse_failed")
        response.status_code = 502
        return {"error": "분석에 실패했습니다. 잠시 후 다시 시도해주세요."}
    except AuthenticationError:
        events.log_event("error", anon_id, error_kind="auth")
        response.status_code = 500
        return {"error": "서버 설정 오류입니다."}
    except APIError:
        events.log_event("error", anon_id, error_kind="openai_api")
        response.status_code = 502
        return {"error": "일시적인 오류입니다. 잠시 후 다시 시도해주세요."}
    except Exception as exc:  # noqa: BLE001 - 예상 못 한 예외도 반드시 이벤트로 남긴다
        # 안 잡으면 빈 500이 나간다. 유저는 설명을 못 받고, 로그에도 안 남아
        # 무슨 일이 있었는지 영원히 모른다. 조용한 실패 금지.
        events.log_event("error", anon_id, error_kind=f"unexpected:{type(exc).__name__}")
        response.status_code = 500
        return {"error": "알 수 없는 오류가 발생했습니다."}

    s = record.summary
    events.log_event(
        "result_shown",
        anon_id,
        job_chars=record.job_chars,
        resume_chars=record.resume_chars,
        model=s.model,
        prompt_hash=s.prompt_hash,
        requirements_count=s.requirements_count,
        quotes_offered=s.quotes_offered,
        demoted_count=s.demoted_count,
        evidence_found=s.evidence_found,
        partial_quote_warnings=sum(1 for w in record.warnings if "부분 인용" in w),
        top_gap_count=len(record.top_gap_ids),
        latency_s=s.latency_s,
        cost_usd=s.cost_usd,
    )

    # save_run()을 부르지 않는다. 유저 문서를 서버에 파일로 쌓지 않는다.
    return to_response(record)
