# CLAUDE.md

상세 규칙: @CONVENTIONS.md (커밋/스타일/스코프)

## 절대 규칙 — 위반하면 되돌릴 수 없다

- **커밋 author는 본인 계정 하나.** Co-Authored-By trailer 금지.
- **`out/`은 커밋한다.** gitignore에 넣지 않는다.
  프롬프트 커밋과 실행 결과가 짝을 이뤄야 튜닝이 성립한다.
  실제 이력서 입력은 `resolve_out_dir()`이 `out/private/`로 라우팅한다.
- **`.env` 커밋 금지.** 레포는 public이다.
- **quote는 원문 대조 검증을 거친다.** 실패 시 삭제가 아니라 `status="없음"` 강등 + 카운트.
- **조용한 실패 금지.** 자르지 말고 에러/경고를 낸다.

## 커밋 전

```bash
uv run ruff format . && uv run ruff check . --fix
```

의존성이나 `requires-python`을 건드렸으면 `uv.lock`도 같이 커밋한다.

## 위임 금지

- `prompts.py` 튜닝은 사람이 손으로 한다.
- Step 1/2/3 코어 파이프라인은 오토파일럿 금지.
