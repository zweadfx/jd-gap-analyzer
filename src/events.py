"""이벤트 로깅 — 방문 → 입력 → 결과 → 재방문.

append-only JSONL. 외부 서비스 의존 없음.

**절대 규칙: 이벤트에 공고/이력서 내용을 넣지 않는다.**
유저가 붙여넣는 것은 본인 이력서 전문이다. 그것이 서버 로그에 쌓이는 순간
이 프로젝트는 개인정보를 수집하는 서비스가 된다. out/private/ 라우팅과 같은 원칙이다.
길이(chars)와 지표만 남긴다. 원문이 필요하면 그건 로그가 아니라 유출이다.

퍼널 정의 (이 순서로만 진행한다):
    view    랜딩 도착
    submit  분석 요청 (입력 완료)
    result  결과 표시 성공
    error   실패 (입력 오류 / LLM 실패 / 레이트리밋)
    revisit 재방문 (같은 sid로 24h 이후 view)

'재방문'이 이 도구의 진짜 성패다. 한 번 써보고 안 돌아오면 결과가 쓸모없었다는 뜻이다.
"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Literal

EventName = Literal["view", "submit", "result", "error", "revisit"]

# 로그 위치. 배포 환경에서 볼륨이 없으면 컨테이너와 함께 날아가므로,
# 배포 시 반드시 영구 볼륨을 마운트할 것. 안 하면 조용히 사라진다.
EVENTS_PATH = Path(os.getenv("EVENTS_PATH", "out/private/events.jsonl"))

REVISIT_AFTER_S = 24 * 3600


def new_session_id() -> str:
    """익명 세션 id. 쿠키로만 유지하고 개인 식별 정보와 절대 연결하지 않는다."""
    return uuid.uuid4().hex[:16]


def log_event(
    name: EventName,
    sid: str,
    *,
    job_chars: int | None = None,
    resume_chars: int | None = None,
    model: str | None = None,
    prompt_hash: str | None = None,
    requirements_count: int | None = None,
    quotes_offered: int | None = None,
    demoted_count: int | None = None,
    evidence_found: int | None = None,
    partial_quote_warnings: int | None = None,
    latency_s: float | None = None,
    cost_usd: float | None = None,
    error_kind: str | None = None,
) -> None:
    """이벤트 한 줄을 append 한다.

    인자를 명시적으로 나열한다(**kwargs 금지). 그래야 실수로 공고/이력서 원문을
    넘기는 코드가 타입 단계에서 걸린다. 편의를 위해 **kwargs를 열면 언젠가 샌다.
    """
    row = {
        "ts": int(time.time()),
        "event": name,
        "sid": sid,
        "job_chars": job_chars,
        "resume_chars": resume_chars,
        "model": model,
        "prompt_hash": prompt_hash,
        "requirements_count": requirements_count,
        "quotes_offered": quotes_offered,
        "demoted_count": demoted_count,
        "evidence_found": evidence_found,
        "partial_quote_warnings": partial_quote_warnings,
        "latency_s": round(latency_s, 2) if latency_s is not None else None,
        "cost_usd": round(cost_usd, 5) if cost_usd is not None else None,
        "error_kind": error_kind,
    }
    row = {k: v for k, v in row.items() if v is not None}

    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def is_revisit(sid: str, now: float | None = None) -> bool:
    """이 sid가 24시간 이전에 view 한 적이 있는가."""
    if not EVENTS_PATH.exists():
        return False
    now = now if now is not None else time.time()
    for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            row.get("sid") == sid
            and row.get("event") == "view"
            and now - row.get("ts", 0) >= REVISIT_AFTER_S
        ):
            return True
    return False
