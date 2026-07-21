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
import statistics
import sys
import threading
import time
import urllib.request
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

# feedback은 퍼널 4종(page_view→submit→result_shown/error)과 별개의 순수 로그다.
# 파이프라인에 들어가지 않고, 집계에도 안 쓴다. "결과가 이상한가요?" 한 줄의 보관소.
# transcribe_*는 B안(이미지 전사)의 계측 — 메타(타일 수·지연·비용)만, 이미지 내용은 기록 안 함.
EventName = Literal[
    "page_view",
    "submit",
    "result_shown",
    "error",
    "feedback",
    "transcribe_requested",
    "transcribe_succeeded",
    "transcribe_failed",
]

# 피드백 자유 입력의 상한. 이력서를 통째로 붙여넣는 사고를 길이로 차단한다.
MAX_FEEDBACK_CHARS = 500

# 배포 시 반드시 영구 볼륨을 마운트하고 여기를 그 경로로 지정할 것.
# 안 하면 재배포마다 퍼널이 초기화되어 "N명이 썼다"를 증명할 수 없게 된다.
EVENTS_PATH = Path(os.getenv("EVENTS_PATH", "out/private/events.jsonl"))

# 테스트 이벤트 제외 — 퍼널 집계("N명이 썼다") 시 반드시 이 prefix로 시작하는 anon_id를 뺀다.
# 검증하며 실서버에 쏜 이벤트가 events.jsonl에 그대로 남고, 삭제 엔드포인트가 없어(SSH 차단)
# 사후 제거가 불가능하다. 걸러내지 않으면 D5 집계의 "N명"에 가짜가 섞인다 — 방어선은 이 필터뿐이다.
#
# ⚠️ 앞으로 테스트 이벤트의 anon_id는 무조건 "verify_" 하나만 쓴다. 새 prefix를 만들지 마라.
#    "embed"는 과거에 이미 새 나간 흔적이라 하위호환으로 남겨둘 뿐이다. prefix가 세 종류,
#    네 종류로 늘면 언젠가 하나를 빠뜨린다. 종류 증식을 여기서 끊는다.
TEST_ANON_PREFIXES = ("verify_", "embed")


def is_test_anon(anon_id: str) -> bool:
    """퍼널 집계에서 제외할 테스트 이벤트인가. 집계 코드는 반드시 이 함수로 거른다."""
    return anon_id.startswith(TEST_ANON_PREFIXES)


def owner_anon_ids() -> set[str]:
    """소유자 본인 트래픽 제외용 anon_id 집합. 소유자가 자기 브라우저(폰·PC 각각)의
    localStorage 'jd_anon_id'를 확인해 OWNER_ANON_IDS 환경변수(콤마 구분)에 넣는다.

    is_test_anon과 **별개 축**이다 — 소유자는 테스트가 아니라 소유자다. 그래서 집계에서
    똑같이 제외하되 카운트 라벨은 따로 센다. 값이 런타임 env라 매 호출에 다시 읽는다.
    """
    return {a.strip() for a in os.getenv("OWNER_ANON_IDS", "").split(",") if a.strip()}


# 자정 경계는 KST로 자른다. 서버 ts는 UTC epoch이지만 이 지표를 보는 사람은 한국에 있고,
# "7/15에 몇 명"은 KST 기준이어야 직관과 맞는다.
KST = timezone(timedelta(hours=9))

# ── 재방문 정의 (데이터 확인 전 사전 정의, 2026-07-15 확정) ────────────────────────────
# 재방문자 = 같은 anon_id가 "서로 다른 KST 날짜 2일 이상"에 이벤트를 남긴 사람.
# 하루에 몇 번을 오든 그날은 1일로 센다(날짜 집합의 크기 ≥ 2). 이벤트 종류는 가리지 않는다.
# **결과를 보고 이 정의를 바꾸지 않는다.** summarize_events.py와 일일 다이제스트가 이 상수를 공유한다.
REVISIT_MIN_DAYS = 2


def kst_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=KST).strftime("%Y-%m-%d")


def aggregate_events(events: list[dict]) -> dict:
    """이벤트 리스트 → 퍼널 지표 dict. 테스트 이벤트(is_test_anon)는 항상 제외한다.

    CLI 표(summarize_events.py)와 일일 디스코드 다이제스트가 이 함수 하나를 공유한다.
    스코프(--since, 당일 등)는 호출자가 events를 걸러서 넘긴다. 테스트 제외만 여기서 강제한다 —
    어느 소비처든 검증용 이벤트를 실수로 셈에 넣지 못하게.
    """
    events = [e for e in events if "ts" in e and "event" in e]
    owner_ids = owner_anon_ids()
    test_excluded = sum(1 for e in events if is_test_anon(e.get("anon_id", "")))
    # 소유자 제외는 테스트와 별개 축이다. 라벨을 나눠 세되(테스트로도 잡힌 건 이중계상 방지),
    # 집계 대상에서는 둘 다 뺀다.
    owner_excluded = sum(
        1
        for e in events
        if e.get("anon_id", "") in owner_ids and not is_test_anon(e.get("anon_id", ""))
    )
    events = [
        e
        for e in events
        if not is_test_anon(e.get("anon_id", "")) and e.get("anon_id", "") not in owner_ids
    ]

    by_event: dict[str, list[dict]] = {}
    for e in events:
        by_event.setdefault(e["event"], []).append(e)

    def anons(name: str) -> set[str]:
        return {e["anon_id"] for e in by_event.get(name, [])}

    pv, sub, res = anons("page_view"), anons("submit"), anons("result_shown")

    dates_by_anon: dict[str, set[str]] = {}
    for e in events:
        dates_by_anon.setdefault(e["anon_id"], set()).add(kst_date(e["ts"]))
    revisit = sorted(
        ((a, sorted(ds)) for a, ds in dates_by_anon.items() if len(ds) >= REVISIT_MIN_DAYS),
        key=lambda kv: -len(kv[1]),
    )

    errs = by_event.get("error", [])

    daily: dict[str, dict[str, set[str]]] = {}
    for e in events:
        d = daily.setdefault(kst_date(e["ts"]), {})
        d.setdefault(e["event"], set()).add(e["anon_id"])
    daily_rows = [
        (
            day,
            len(d.get("page_view", set())),
            len(d.get("submit", set())),
            len(d.get("result_shown", set())),
        )
        for day, d in sorted(daily.items())
    ]

    costs = [e["cost_usd"] for e in events if "cost_usd" in e]
    # 지연 중앙값은 분석(result_shown) 기준 — transcribe 지연과 섞으면 두 지표 다 무의미해진다.
    lats = [e["latency_s"] for e in by_event.get("result_shown", []) if "latency_s" in e]
    vision = by_event.get("transcribe_succeeded", [])

    return {
        "n_events": len(events),
        "test_excluded": test_excluded,
        "owner_excluded": owner_excluded,
        "visitors": len(pv),
        "users": len(res),
        "funnel": {"page_view": len(pv), "submit": len(sub), "result_shown": len(res)},
        "total_anon": len(dates_by_anon),
        "revisitors": len(revisit),
        "revisit_detail": revisit,
        "errors": len(errs),
        "error_kinds": Counter(e.get("error_kind", "?") for e in errs).most_common(),
        "daily": daily_rows,
        "total_cost": sum(costs),
        "median_latency": statistics.median(lats) if lats else None,
        "latency_min": min(lats) if lats else None,
        "latency_max": max(lats) if lats else None,
        # B안 비전 사용 계측(성공 건 기준). 비용은 total_cost에도 포함된다(총 API 비용).
        "vision_count": len(vision),
        "vision_cost": sum(e.get("cost_usd", 0) for e in vision),
    }


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
    placement: str | None = None,
    tiles: int | None = None,
    input_mode: str | None = None,
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
        # 제보를 보낸 화면·진입점 메타(landing/floating_result 등). 위치 라벨일 뿐 원문이 아니다.
        # 클라이언트 조작 대비 짧게 자른다 — enum 라벨이라 24자면 충분하다.
        "placement": placement[:24] if placement else None,
        # B안 계측: 전사 타일 수. 이미지 내용은 어디에도 기록하지 않는다.
        "tiles": tiles,
        # 분석 입력 방식(text|image) — B안 성공 지표(전환율 비교)의 분모 라벨.
        "input_mode": input_mode[:8] if input_mode else None,
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


# 이벤트별 embed 색(10진). 채널에서 색만 보고 종류를 가른다. error는 빨강 — 홍보 당일
# 눈에 확 띄어야 하는 유일한 메시지다.
_EMBED_COLOR = {
    "submit": 0x3498DB,  # 파랑
    "result_shown": 0x2ECC71,  # 초록
    "error": 0xE74C3C,  # 빨강
    "feedback": 0x9B59B6,  # 보라
}


def _field(name: str, value: object, inline: bool = True) -> dict:
    return {"name": name, "value": str(value), "inline": inline}


def _build_embed(row: dict) -> dict:
    """메타 row → 디스코드 embed. 형식만 바꾼다 — row 필드 외 새 데이터는 만들지 않는다.

    - footer의 anon_id 뒤 6자리는 미관이 아니라 기능이다: 동시 유저가 여럿일 때
      어느 submit이 어느 result_shown과 같은 사람인지 채널에서 눈으로 짝지을 수 있어야 한다.
    - timestamp는 embed 규격 필드로 넣어 디스코드가 뷰어 로컬 시간으로 자동 렌더하게 한다.
    """
    ev = row.get("event")
    embed = {
        "color": _EMBED_COLOR.get(str(ev), 0x95A5A6),
        "timestamp": datetime.fromtimestamp(row["ts"], tz=UTC).isoformat(),
        "footer": {"text": f"id …{str(row.get('anon_id', ''))[-6:]}"},
    }
    if ev == "submit":
        embed["title"] = "📥 새 분석 요청"
        embed["fields"] = [
            _field("공고", f"{row.get('job_chars')}자"),
            _field("문서", f"{row.get('resume_chars')}자"),
        ]
    elif ev == "result_shown":
        embed["title"] = "✅ 결과 전달"
        embed["fields"] = [
            _field("요구사항", row.get("requirements_count")),
            _field("인용", row.get("quotes_offered")),
            _field("강등", row.get("demoted_count")),
            _field("발견", row.get("evidence_found")),
            _field("지연", f"{row.get('latency_s')}s"),
            _field("비용", f"${row.get('cost_usd')}"),
        ]
    elif ev == "error":
        embed["title"] = f"🔴 에러: {row.get('error_kind')}"
    elif ev == "feedback":
        embed["title"] = "💬 제보"
        # feedback_text는 설계상 허용된 유일한 자유 텍스트 필드다(log_event가 상한으로 자른다).
        embed["description"] = row.get("feedback_text") or ""
        if row.get("placement"):
            embed["fields"] = [_field("위치", row["placement"])]
    else:
        embed["title"] = str(ev)
    return embed


def _webhook_url_for(kind: str) -> str:
    """이벤트 종류별 목적지 웹훅 URL. 채널 분리는 목적지만 바꾼다 — 페이로드 규칙은 전부 동일.

    - feedback → FEEDBACK_WEBHOOK_URL (피드백 채널)
    - digest   → DIGEST_WEBHOOK_URL (일일요약 채널)
    - 그 외(submit/result_shown/error) → DISCORD_WEBHOOK_URL (알림 채널, 기존)

    ⚠️ 새 env 미설정 시 DISCORD_WEBHOOK_URL로 폴백한다. 채널 분리 실패로 메시지가 조용히
       사라지는 일은 없어야 한다(조용한 실패 금지). 전역 한도 초과 error도 알림 채널 유지된다.
    """
    base = os.getenv("DISCORD_WEBHOOK_URL", "")
    if kind == "feedback":
        return os.getenv("FEEDBACK_WEBHOOK_URL", "") or base
    if kind == "digest":
        return os.getenv("DIGEST_WEBHOOK_URL", "") or base
    return base


def _post_discord_payload(payload: dict, url: str) -> None:
    """디스코드 웹훅 url로 payload를 데몬 스레드에서 POST. fire-and-forget.

    - url이 비면 조용히 비활성.
    - 실패는 로그만 남기고 삼킨다. 유저 요청도 스케줄러도 절대 막지 않는다.
    - User-Agent 필수: 디스코드 앞단 Cloudflare가 urllib 기본 UA를 봇으로 보고 403(error code
      1010)으로 막는다(curl은 통과해서 URL 검증만으론 안 잡힌다). 실측: UA 없음→403 / 있음→204.

    이벤트 알림(_notify_discord)과 일일 다이제스트(send_digest)가 이 전송부를 공유한다.
    """
    if not url:
        return

    def _post() -> None:
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "jd-gap-analyzer/1.0 (+webhook)",
                },
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as exc:  # noqa: BLE001 - 웹훅 실패는 서비스에 영향 없다
            print(f"[events] discord webhook 실패: {type(exc).__name__}", file=sys.stderr)

    threading.Thread(target=_post, daemon=True).start()


def _notify_discord(row: dict) -> None:
    """이벤트 메타를 디스코드 embed로 전송. row 외 새 데이터는 만들지 않는다.

    feedback은 피드백 채널로, 나머지(submit/result_shown/error)는 알림 채널로 라우팅한다.
    """
    ev = row.get("event")
    if ev not in _DISCORD_EVENTS:
        return
    url = _webhook_url_for(str(ev))
    if not url:
        return
    if ev == "error":
        kind = str(row.get("error_kind", ""))
        now = time.time()
        if now - _last_error_alert.get(kind, 0.0) < _ERROR_ALERT_THROTTLE_S:
            return
        _last_error_alert[kind] = now
    _post_discord_payload({"embeds": [_build_embed(row)]}, url)


# ---------------------------------------------------------------------------
# 일일 요약 다이제스트 — 당일 지표 + 재방문 누적을 embed 1개로. 매일 22:00 KST 자동 발송 +
# /admin/digest 온디맨드. aggregate_events()를 CLI 집계와 공유한다(같은 함수 재사용).
# ---------------------------------------------------------------------------

# 이벤트 embed와 겹치지 않는 디스코드 블러플. "지표 요약"이라는 다른 종류임을 색으로 구분한다.
_DIGEST_COLOR = 0x5865F2


def _read_events_file() -> list[dict]:
    """EVENTS_PATH(볼륨)의 JSONL을 읽어 이벤트 리스트로. 파일 없으면 빈 리스트."""
    if not EVENTS_PATH.exists():
        return []
    out: list[dict] = []
    for ln in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _digest_footer(today: dict) -> str:
    excl = f"테스트 {today['test_excluded']}건"
    if today.get("owner_excluded"):
        excl += f" · 소유자 {today['owner_excluded']}건"
    return f"집계 {today['n_events']}건 · {excl} 제외"


def build_digest_embed(day: str, today: dict, cumulative: dict) -> dict:
    """당일 지표(today) + 재방문 누적(cumulative)을 embed 1개로. aggregate_events 결과를 읽는다."""
    f = today["funnel"]
    conv = f"{f['result_shown'] / f['page_view']:.0%}" if f["page_view"] else "—"
    med = today["median_latency"]
    fields = [
        _field("방문", today["visitors"]),
        _field("사용자", today["users"]),
        _field("전환(방문→결과)", conv),
        _field("재방문(누적)", cumulative["revisitors"]),
        _field("에러", today["errors"]),
        _field("비용", f"${today['total_cost']:.4f}"),
        _field("지연중앙", f"{med:.1f}s" if med is not None else "—"),
        _field("비전(이미지)", f"{today['vision_count']}회 · ${today['vision_cost']:.4f}"),
    ]
    embed = {
        "title": f"📊 일일 요약 · {day} (KST)",
        "color": _DIGEST_COLOR,
        "fields": fields,
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "footer": {"text": _digest_footer(today)},
    }
    if today["error_kinds"]:
        embed["description"] = "에러 kind: " + ", ".join(
            f"{k} {n}" for k, n in today["error_kinds"]
        )
    return embed


def send_digest(day: str | None = None) -> dict:
    """당일(KST) 지표 + 재방문 누적을 디스코드로 embed 1개 전송. fire-and-forget.

    스케줄러(매일 22:00 KST)와 /admin/digest가 공유한다. 반환값은 엔드포인트 응답용 요약이다
    (전송은 데몬 스레드라 반환 시점에 도착 보장은 없다 — 채널에서 눈으로 확인한다).
    """
    all_events = _read_events_file()
    target_day = day or kst_date(int(time.time()))
    today_events = [e for e in all_events if "ts" in e and kst_date(e["ts"]) == target_day]
    today = aggregate_events(today_events)
    cumulative = aggregate_events(all_events)
    _post_discord_payload(
        {"embeds": [build_digest_embed(target_day, today, cumulative)]},
        _webhook_url_for("digest"),
    )
    return {
        "day": target_day,
        "visitors": today["visitors"],
        "users": today["users"],
        "errors": today["errors"],
        "revisitors_cumulative": cumulative["revisitors"],
        "cost_usd": round(today["total_cost"], 4),
    }


def _seconds_until_kst(hour: int, minute: int = 0) -> float:
    now = datetime.now(tz=KST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def start_daily_digest_scheduler(hour: int = 22, minute: int = 0) -> None:
    """매일 지정 KST 시각에 send_digest()를 부르는 데몬 스레드를 띄운다.

    cron 인프라를 새로 만들지 않는다 — 프로세스 안의 데몬 스레드다(인메모리 레이트리밋과 같은 수준).
    프로세스가 죽으면 사라진다. 인스턴스 1대 전제(레이트리밋과 동일 전제)라 중복 발송이 없다.
    DISCORD_WEBHOOK_URL이 없으면 send_digest가 내부에서 no-op이므로 루프만 돌 뿐 무해하다.
    """

    def _loop() -> None:
        while True:
            time.sleep(_seconds_until_kst(hour, minute))
            try:
                send_digest()
            except Exception as exc:  # noqa: BLE001 - 다이제스트 실패가 서비스를 죽이면 안 된다
                print(f"[events] 일일 다이제스트 실패: {type(exc).__name__}", file=sys.stderr)
            time.sleep(60)  # 같은 분에 두 번 깨어 두 번 발송하는 것을 방지

    threading.Thread(target=_loop, daemon=True).start()
