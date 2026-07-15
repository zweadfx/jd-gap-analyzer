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
집계 로직은 src.events.aggregate_events 하나를 쓴다 — 일일 디스코드 다이제스트와 같은 함수다.
테스트 이벤트(is_test_anon: verify_·embed)는 그 함수 안에서 항상 제외된다.

재방문 정의(데이터 확인 전 사전 정의, 2026-07-15 확정): 같은 anon_id가 서로 다른 KST 날짜
REVISIT_MIN_DAYS(=2)일 이상에 이벤트를 남긴 사람. 정의 원문은 src/events.py에 박혀 있다.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.events import (  # noqa: E402
    REVISIT_MIN_DAYS,
    TEST_ANON_PREFIXES,
    aggregate_events,
    kst_date,
)


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
    events = [e for e in events if "ts" in e and "event" in e]

    since_excluded = 0
    if since:
        before = len(events)
        events = [e for e in events if kst_date(e["ts"]) >= since]
        since_excluded = before - len(events)

    # 집계는 공유 함수 하나로. 테스트 이벤트 제외는 이 안에서 강제된다.
    agg = aggregate_events(events)

    print(f"입력: {path}  (원본 {total_raw}건)")
    excl = f"테스트 {agg['test_excluded']}건 {TEST_ANON_PREFIXES}"
    if since:
        excl += f" · --since {since} 이전 {since_excluded}건"
    print(f"제외: {excl}")
    print(f"집계 대상: {agg['n_events']}건")
    if agg["n_events"] == 0:
        print("집계할 이벤트가 없습니다.")
        return 1

    f = agg["funnel"]
    pv, sub, res = f["page_view"], f["submit"], f["result_shown"]

    # a·b. 방문자 / 사용자
    print("\n■ 규모")
    print("─" * 60)
    print(f"  방문자 (page_view distinct anon)   {agg['visitors']:>5}")
    print(f"  사용자 (result_shown distinct anon) {agg['users']:>5}")

    # c. 퍼널
    print("\n■ 퍼널  page_view → submit → result_shown")
    print("─" * 60)
    print(f"  {'단계':<14}{'anon 수':>8}{'직전 대비':>10}{'전체 대비':>10}")
    print(f"  {'page_view':<14}{pv:>8}{'  —':>10}{'  —':>10}")
    print(f"  {'submit':<14}{sub:>8}{_rate(sub, pv):>10}{_rate(sub, pv):>10}")
    print(f"  {'result_shown':<14}{res:>8}{_rate(res, sub):>10}{_rate(res, pv):>10}")
    if sub > pv:
        print(
            "  ⚠️ submit이 page_view보다 많다 — page_view 없이 바로 제출한 anon이 있다(직접 유입/로깅 누락)."
        )

    # d. 재방문
    print(f"\n■ 재방문  (같은 anon_id가 서로 다른 KST 날짜 {REVISIT_MIN_DAYS}일 이상)")
    print("─" * 60)
    print(
        f"  재방문자 {agg['revisitors']} / 전체 anon {agg['total_anon']}  "
        f"({_rate(agg['revisitors'], agg['total_anon']).strip()})"
    )
    for a, ds in agg["revisit_detail"][:10]:
        print(f"    …{a[-6:]}  {len(ds)}일  ({', '.join(ds)})")

    # e. 에러
    print(f"\n■ 에러  (총 {agg['errors']}건)")
    print("─" * 60)
    if agg["error_kinds"]:
        for kind, n in agg["error_kinds"]:
            print(f"  {kind:<24}{n:>5}")
    else:
        print("  (없음)")

    # f. 일별 추이
    print("\n■ 일별 추이  (distinct anon)")
    print("─" * 60)
    print(f"  {'날짜(KST)':<12}{'방문':>7}{'제출':>7}{'결과':>7}")
    for day, d_pv, d_sub, d_res in agg["daily"]:
        print(f"  {day:<12}{d_pv:>7}{d_sub:>7}{d_res:>7}")

    # g. 운영 (비용·지연)
    print("\n■ 운영")
    print("─" * 60)
    print(f"  총 API 비용  ${agg['total_cost']:.4f}")
    med = agg["median_latency"]
    if med is not None:
        print(
            f"  지연 중앙값  {med:.1f}s  (min {agg['latency_min']:.1f} / max {agg['latency_max']:.1f})"
        )
    else:
        print("  지연 중앙값  — (result_shown 없음)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
