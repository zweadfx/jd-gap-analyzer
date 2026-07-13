"""FastAPI — 웹에서 파이프라인을 호출한다.

프론트(Vercel)와 분리된 서비스다. 배포는 이쪽을 먼저 띄워 URL을 확보한다.

**파이프라인을 두 벌로 만들지 않는다.** CLI와 똑같이 src.pipeline.analyze()를 부른다.
웹용 사본을 만들면 웹에서만 나는 버그가 생기고, 프롬프트를 고칠 때 한쪽을 빠뜨린다.

**응답과 로그에 이력서 원문을 남기지 않는다.**
- RunRecord는 프롬프트(=이력서 전문)와 원본 응답을 들고 있다. 그대로 반환하면 유출이다.
- 그래서 to_response()가 유저에게 보여줄 것만 골라 새 dict를 만든다.
- save_run()도 부르지 않는다. 서버에 유저 이력서를 파일로 쌓지 않는다.
"""

import os
import time
from collections import defaultdict, deque

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from openai import APIError, AuthenticationError
from pydantic import BaseModel, Field

from src import events
from src.pipeline import InputTooLongError, LLMParseError, analyze, select_top_gaps
from src.schemas import MAX_JOB_CHARS, MAX_RESUME_CHARS, RunRecord

app = FastAPI(title="jd-gap-analyzer")

# Vercel 프론트에서 부른다. 배포 후 실제 도메인으로 좁힐 것.
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# --- 레이트리밋: IP당 하루 5회 (컨벤션 7장) ---
#
# 인메모리다. 프로세스가 재시작되면 초기화되고, 인스턴스가 여러 개면 각자 센다.
# 5일 프로젝트에 Redis를 붙이지 않는다. 다만 이 한계를 알고 있어야 한다 —
# 비용 하드캡은 이것이 아니라 OpenAI 선불 크레딧 + 자동충전 OFF다.
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_DAY", "5"))
RATE_WINDOW_S = 24 * 3600
_hits: dict[str, deque[float]] = defaultdict(deque)

SID_COOKIE = "sid"


class AnalyzeRequest(BaseModel):
    job: str = Field(description="채용 공고 원문")
    resume: str = Field(description="이력서 원문")


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


def get_sid(request: Request, response: Response) -> str:
    sid = request.cookies.get(SID_COOKIE)
    if not sid:
        sid = events.new_session_id()
        response.set_cookie(
            SID_COOKIE, sid, max_age=90 * 24 * 3600, httponly=True, samesite="lax", secure=True
        )
    return sid


def to_response(record: RunRecord) -> dict:
    """유저에게 보낼 것만 고른다.

    RunRecord에는 prompts(=이력서 전문)와 raw_responses가 들어 있다.
    통째로 반환하면 유출이다. 화이트리스트로 새로 만든다.
    """
    reqs = {r.id: r for r in record.requirements.requirements}
    evs = {e.requirement_id: e for e in record.analysis.evidences}
    bullets = {s.requirement_id: s.bullets for s in record.suggestions.suggestions}

    # "그 외 근거 없는 항목"도 내려준다. Top3만 주면 필수 갭이 조용히 사라진다.
    all_gaps = select_top_gaps(
        record.requirements.requirements, record.analysis, n=len(record.requirements.requirements)
    )
    rest = [g.id for g in all_gaps if g.id not in record.top_gap_ids]

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
            "hallucination_rate": s.hallucination_rate,
            "evidence_found": s.evidence_found,
            "evidence_rate": s.evidence_rate,
            "latency_s": s.latency_s,
            "model": s.model,
        },
        "warnings": record.warnings,
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True, "limits": {"job": MAX_JOB_CHARS, "resume": MAX_RESUME_CHARS}}


@app.post("/events/view")
def track_view(request: Request, response: Response) -> dict:
    """랜딩 도착. 같은 sid가 24h 이전에 온 적이 있으면 재방문으로도 기록한다."""
    existing = request.cookies.get(SID_COOKIE)
    sid = get_sid(request, response)
    if existing and events.is_revisit(sid):
        events.log_event("revisit", sid)
    events.log_event("view", sid)
    return {"sid": sid}


@app.post("/analyze")
def analyze_endpoint(body: AnalyzeRequest, request: Request, response: Response) -> dict:
    sid = get_sid(request, response)
    ip = client_ip(request)

    events.log_event("submit", sid, job_chars=len(body.job), resume_chars=len(body.resume))

    if rate_limited(ip):
        events.log_event("error", sid, error_kind="rate_limited")
        response.status_code = 429
        return {"error": f"하루 {RATE_LIMIT}회까지 사용할 수 있습니다. 내일 다시 시도해주세요."}

    try:
        record = analyze(body.job, body.resume)
    except InputTooLongError as exc:
        # 자동으로 자르지 않는다. 시끄럽게 실패한다. (컨벤션 1조)
        events.log_event("error", sid, error_kind="input_too_long")
        response.status_code = 400
        return {"error": str(exc)}
    except LLMParseError as exc:
        events.log_event("error", sid, error_kind="llm_parse_failed")
        response.status_code = 502
        return {"error": f"분석에 실패했습니다. 다시 시도해주세요. ({exc})"}
    except AuthenticationError:
        events.log_event("error", sid, error_kind="auth")
        response.status_code = 500
        return {"error": "서버 설정 오류입니다."}
    except APIError:
        events.log_event("error", sid, error_kind="openai_api")
        response.status_code = 502
        return {"error": "일시적인 오류입니다. 잠시 후 다시 시도해주세요."}

    s = record.summary
    events.log_event(
        "result",
        sid,
        job_chars=record.job_chars,
        resume_chars=record.resume_chars,
        model=s.model,
        prompt_hash=s.prompt_hash,
        requirements_count=s.requirements_count,
        quotes_offered=s.quotes_offered,
        demoted_count=s.demoted_count,
        evidence_found=s.evidence_found,
        partial_quote_warnings=sum(1 for w in record.warnings if "부분 인용" in w),
        latency_s=s.latency_s,
        cost_usd=s.cost_usd,
    )

    # save_run()을 부르지 않는다. 유저 이력서를 서버에 파일로 쌓지 않는다.
    return to_response(record)
