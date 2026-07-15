"""events.jsonl 퍼널 집계 — 이미 쌓이는 이벤트를 읽어 CLI 표로 뽑는다.

새 지표·새 기능이 아니다. events.jsonl(page_view→submit→result_shown→error, feedback)을
읽어 방문/전환/재방문/에러/비용을 센다. summarize_runs.py와 같은 패턴(순수 stdlib, CLI 표).

    # /admin/events에서 받아서 (응답 형태 {"lines":[...]} 그대로 저장):
    curl -s "$API/admin/events?token=$TOKEN&n=100000" > events.json
    uv run python scripts/summarize_events.py events.json

    # 로컬 events.jsonl 직접:
    uv run python scripts/summarize_events.py out/private/events.jsonl

    # 특정 KST 날짜(포함) 이후만:
    uv run python scripts/summarize_events.py events.json --since 2026-07-16

입력은 /admin/events의 JSON 응답 또는 raw JSONL 둘 다 받는다(자동 판별).
TEST_ANON_PREFIXES(verify_·embed)로 검증용 이벤트를 제외한다 — 이미 커밋된 그 상수를 재사용한다.
집계에 가짜가 섞이면 'N명이 썼다'가 틀린다. 삭제 엔드포인트가 없어(SSH 차단) 필터가 유일한 방어선이다.
"""

import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.events import TEST_ANON_PREFIXES, is_test_anon  # noqa: E402

# 자정 경계는 KST로 자른다. 서버 ts는 UTC epoch이지만, 이 표를 보는 사람은 한국에 있고
# "7/15에 몇 명"은 KST 기준이어야 직관과 맞는다.
KST = timezone(timedelta(hours=9))


def kst_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=KST).strftime("%Y-%m-%d")


# ── 재방문 정의 (데이터 보기 전 사전 확정, 2026-07-15) ─────────────────────────────
# 재방문자 = 같은 anon_id가 "서로 다른 KST 날짜 2일 이상"에 이벤트를 남긴 사람.
# 하루에 몇 번을 오든 그날은 1일로 센다(날짜 집합의 크기 ≥ 2). 이벤트 종류는 가리지 않는다
# — 돌아온 사람은 어차피 page_view를 다시 찍는다. README의 "page_view가 다른 날에 있으면"을
# 이 스크립트에서 이렇게 조작적으로 고정한다. **결과를 보고 이 정의를 바꾸지 않는다.**
REVISIT_MIN_DAYS = 2


def load_events(path: Path) -> list[dict]:
    """/admin/events 응답({"lines":[...]})이든 raw JSONL이든 이벤트 리스트로 만든다."""
    text = path.read_text(encoding="utf-8")
    try:
        obj = json.loads(text)
        # 통째로 파싱되면 단일 JSON — /admin/events 응답이거나 단일 이벤트다.
        if isinstance(obj, dict) and "lines" in obj:
            raw: list = obj["lines"]
        elif isinstance(obj, list):
            raw = obj
        else:
            raw = [obj]
    except json.JSONDecodeError:
        # 여러 줄이면 통째 파싱이 실패한다 = raw JSONL.
        raw = [ln for ln in text.splitlines() if ln.strip()]
    return [item if isinstance(item, dict) else json.loads(item) for item in raw]


def _parse_args(argv: list[str]) -> tuple[list[str], str | None]:
    pos: list[str] = []
    since: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--since":
            since = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
        elif a.startswith("--since="):
            since = a.split("=", 1)[1]
            i += 1
        elif a.startswith("--"):
            i += 1  # 알 수 없는 플래그는 무시
        else:
            pos.append(a)
            i += 1
    return pos, since


def _rate(n: int, d: int) -> str:
    return f"{n / d:>4.0%}" if d else "   —"


def main() -> int:
    pos, since = _parse_args(sys.argv[1:])
    if not pos:
        print(
            "사용법: uv run python scripts/summarize_events.py <events.json|events.jsonl> [--since YYYY-MM-DD]"
        )
        return 1
    path = Path(pos[0])
    if not path.exists():
        print(f"파일이 없습니다: {path}")
        return 1

    events = load_events(path)
    total_raw = len(events)
    # ts/event가 없는 줄은 이벤트가 아니다.
    events = [e for e in events if "ts" in e and "event" in e]

    test_n = sum(1 for e in events if is_test_anon(e.get("anon_id", "")))
    events = [e for e in events if not is_test_anon(e.get("anon_id", ""))]

    since_excluded = 0
    if since:
        before = len(events)
        events = [e for e in events if kst_date(e["ts"]) >= since]
        since_excluded = before - len(events)

    print(f"입력: {path}  (원본 {total_raw}건)")
    excl = f"테스트 {test_n}건 {TEST_ANON_PREFIXES}"
    if since:
        excl += f" · --since {since} 이전 {since_excluded}건"
    print(f"제외: {excl}")
    print(f"집계 대상: {len(events)}건")
    if not events:
        print("집계할 이벤트가 없습니다.")
        return 1

    by_event: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_event[e["event"]].append(e)

    def anons(name: str) -> set[str]:
        return {e["anon_id"] for e in by_event.get(name, [])}

    pv, sub, res = anons("page_view"), anons("submit"), anons("result_shown")

    # a·b. 방문자 / 사용자
    print("\n■ 규모")
    print("─" * 60)
    print(f"  방문자 (page_view distinct anon)   {len(pv):>5}")
    print(f"  사용자 (result_shown distinct anon) {len(res):>5}")

    # c. 퍼널
    print("\n■ 퍼널  page_view → submit → result_shown")
    print("─" * 60)
    print(f"  {'단계':<14}{'anon 수':>8}{'직전 대비':>10}{'전체 대비':>10}")
    print(f"  {'page_view':<14}{len(pv):>8}{'  —':>10}{'  —':>10}")
    print(
        f"  {'submit':<14}{len(sub):>8}{_rate(len(sub), len(pv)):>10}{_rate(len(sub), len(pv)):>10}"
    )
    print(
        f"  {'result_shown':<14}{len(res):>8}{_rate(len(res), len(sub)):>10}{_rate(len(res), len(pv)):>10}"
    )
    if len(sub) > len(pv):
        print(
            "  ⚠️ submit이 page_view보다 많다 — page_view 없이 바로 제출한 anon이 있다(직접 유입/로깅 누락)."
        )

    # d. 재방문
    dates_by_anon: dict[str, set[str]] = defaultdict(set)
    for e in events:
        dates_by_anon[e["anon_id"]].add(kst_date(e["ts"]))
    returning = {a: ds for a, ds in dates_by_anon.items() if len(ds) >= REVISIT_MIN_DAYS}
    print(f"\n■ 재방문  (같은 anon_id가 서로 다른 KST 날짜 {REVISIT_MIN_DAYS}일 이상)")
    print("─" * 60)
    print(
        f"  재방문자 {len(returning)} / 전체 anon {len(dates_by_anon)}  ({_rate(len(returning), len(dates_by_anon)).strip()})"
    )
    for a, ds in sorted(returning.items(), key=lambda kv: -len(kv[1]))[:10]:
        print(f"    …{a[-6:]}  {len(ds)}일  ({', '.join(sorted(ds))})")

    # e. 에러
    errs = by_event.get("error", [])
    print(f"\n■ 에러  (총 {len(errs)}건)")
    print("─" * 60)
    if errs:
        for kind, n in Counter(e.get("error_kind", "?") for e in errs).most_common():
            print(f"  {kind:<24}{n:>5}")
    else:
        print("  (없음)")

    # f. 일별 추이
    daily: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for e in events:
        daily[kst_date(e["ts"])][e["event"]].add(e["anon_id"])
    print("\n■ 일별 추이  (distinct anon)")
    print("─" * 60)
    print(f"  {'날짜(KST)':<12}{'방문':>7}{'제출':>7}{'결과':>7}")
    for d in sorted(daily):
        row = daily[d]
        print(
            f"  {d:<12}{len(row.get('page_view', set())):>7}{len(row.get('submit', set())):>7}{len(row.get('result_shown', set())):>7}"
        )

    # g. 운영 (비용·지연) — result_shown에만 실린다.
    costs = [e["cost_usd"] for e in events if "cost_usd" in e]
    lats = [e["latency_s"] for e in events if "latency_s" in e]
    print("\n■ 운영")
    print("─" * 60)
    print(f"  총 API 비용  ${sum(costs):.4f}  ({len(costs)}회 result_shown)")
    if lats:
        print(
            f"  지연 중앙값  {statistics.median(lats):.1f}s  (min {min(lats):.1f} / max {max(lats):.1f})"
        )
    else:
        print("  지연 중앙값  — (result_shown 없음)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
