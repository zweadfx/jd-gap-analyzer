"""이벤트 로깅 — page_view → submit → result_shown → error.

append-only JSONL. 외부 서비스 의존 없음.

**PostHog 등 외부 분석 도구를 쓰지 않는다.** 타깃이 개발자라 애드블록에 차단된다.
차단된 이벤트는 조용히 사라지고, 퍼널이 조용히 틀려진다. 조용한 실패 금지(컨벤션 1조).

**절대 규칙: 이벤트에 공고/지원 문서 내용을 넣지 않는다.**
유저가 붙여넣는 것은 본인 이력서 전문이다. 그것이 서버 로그에 쌓이는 순간
이 프로젝트는 개인정보를 수집하는 서비스가 된다. 길이(chars)와 지표만 남긴다.

**anon_id는 프론트가 localStorage에 만들어 보낸다. 쿠키가 아니다.**
프론트(Vercel)와 API(Railway)는 서로 다른 도메인이다. 쿠키는 서드파티가 되어
Safari/Firefox의 기본 차단에 걸린다. 그러면 재방문을 영영 이을 수 없다.
localStorage + 명시적 헤더 전송이 유일하게 확실한 방법이다.

**error 이벤트가 반드시 있어야 한다.** 10초 지연에서 이탈이 "관심 없어서"인지
"터져서"인지 구분 못 하면 퍼널 데이터가 통째로 무의미해진다.

재방문은 이벤트가 아니라 **로그에서 유도한다.** 같은 anon_id의 page_view가
서로 다른 날에 있으면 재방문이다. 이벤트를 늘리는 대신 계산한다.
"""

import json
import os
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Literal

# feedback은 퍼널 4종(page_view→submit→result_shown/error)과 별개의 순수 로그다.
# 파이프라인에 들어가지 않고, 집계에도 안 쓴다. "결과가 이상한가요?" 한 줄의 보관소.
EventName = Literal["page_view", "submit", "result_shown", "error", "feedback"]

# 피드백 자유 입력의 상한. 이력서를 통째로 붙여넣는 사고를 길이로 차단한다.
MAX_FEEDBACK_CHARS = 500

# 배포 시 반드시 영구 볼륨을 마운트하고 여기를 그 경로로 지정할 것.
# 안 하면 재배포마다 퍼널이 초기화되어 "N명이 썼다"를 증명할 수 없게 된다.
EVENTS_PATH = Path(os.getenv("EVENTS_PATH", "out/private/events.jsonl"))


def new_anon_id() -> str:
    """프론트가 id를 안 보냈을 때의 폴백. 정상 경로에서는 프론트가 만든다."""
    return uuid.uuid4().hex[:16]


def log_event(
    name: EventName,
    anon_id: str,
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
    top_gap_count: int | None = None,
    latency_s: float | None = None,
    cost_usd: float | None = None,
    error_kind: str | None = None,
    feedback_text: str | None = None,
) -> None:
    """이벤트 한 줄을 append 한다.

    인자를 명시적으로 나열한다(**kwargs 금지). 그래야 실수로 공고/지원 문서 원문을
    넘기는 코드가 시그니처 단계에서 걸린다. 편의를 위해 **kwargs를 열면 언젠가 샌다.
    """
    row = {
        "ts": int(time.time()),
        "event": name,
        "anon_id": anon_id,
        "job_chars": job_chars,
        "resume_chars": resume_chars,
        "model": model,
        "prompt_hash": prompt_hash,
        "requirements_count": requirements_count,
        "quotes_offered": quotes_offered,
        "demoted_count": demoted_count,
        "evidence_found": evidence_found,
        "partial_quote_warnings": partial_quote_warnings,
        "top_gap_count": top_gap_count,
        "latency_s": round(latency_s, 2) if latency_s is not None else None,
        "cost_usd": round(cost_usd, 5) if cost_usd is not None else None,
        "error_kind": error_kind,
        # 유일하게 허용되는 자유 텍스트. 유저가 자발적으로 쓴 피드백 한 줄이지
        # 공고/지원 문서가 아니다. 그래도 상한으로 자른다 — 전문 붙여넣기 사고 방지.
        "feedback_text": feedback_text[:MAX_FEEDBACK_CHARS] if feedback_text else None,
    }
    row = {k: v for k, v in row.items() if v is not None}

    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 같은 메타 페이로드를 디스코드로도 한 줄 보낸다. row에는 공고/문서 원문·quote가
    # 애초에 없으므로(위 시그니처가 막는다) 유출이 구조적으로 불가능하다.
    _notify_discord(row)


# ---------------------------------------------------------------------------
# 디스코드 웹훅 — 홍보 당일 조기경보. 대시보드·집계 없음. 알림 한 줄 POST가 전부다.
# ---------------------------------------------------------------------------

# page_view는 시끄러워서 제외한다.
_DISCORD_EVENTS = {"submit", "result_shown", "error", "feedback"}
# error 알림 스팸 방지: 같은 error_kind는 이 간격 안에 한 번만 보낸다.
# (전역 한도 초과 시 매 요청이 error를 찍어도 디스코드가 도배되지 않게)
_ERROR_ALERT_THROTTLE_S = 300
_last_error_alert: dict[str, float] = {}


def _format_discord(row: dict) -> str:
    ev = row.get("event")
    if ev == "submit":
        return f"📥 **submit** · 공고 {row.get('job_chars')}자 · 문서 {row.get('resume_chars')}자"
    if ev == "result_shown":
        return (
            f"✅ **result_shown** · 요구사항 {row.get('requirements_count')} · "
            f"인용 {row.get('quotes_offered')} · 강등 {row.get('demoted_count')} · "
            f"발견 {row.get('evidence_found')} · {row.get('latency_s')}s · ${row.get('cost_usd')}"
        )
    if ev == "error":
        return f"🚨 **error** · kind=`{row.get('error_kind')}`"
    if ev == "feedback":
        return f"💬 **feedback** · {row.get('feedback_text')}"
    return str(ev)


def _notify_discord(row: dict) -> None:
    """이벤트 메타를 디스코드로 한 줄 전송. fire-and-forget.

    - DISCORD_WEBHOOK_URL 미설정이면 조용히 비활성.
    - 데몬 스레드로 보내 유저 요청을 절대 막지 않는다. 실패는 로그만 남기고 삼킨다.
    - row는 log_event가 만든 메타 dict 그대로다 — 새 데이터를 만들지 않는다.
    """
    url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not url:
        return
    ev = row.get("event")
    if ev not in _DISCORD_EVENTS:
        return
    if ev == "error":
        kind = str(row.get("error_kind", ""))
        now = time.time()
        if now - _last_error_alert.get(kind, 0.0) < _ERROR_ALERT_THROTTLE_S:
            return
        _last_error_alert[kind] = now
    content = _format_discord(row)

    def _post() -> None:
        try:
            data = json.dumps({"content": content}).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as exc:  # noqa: BLE001 - 웹훅 실패는 유저 요청에 영향 없다
            print(f"[events] discord webhook 실패: {type(exc).__name__}", file=sys.stderr)

    threading.Thread(target=_post, daemon=True).start()
