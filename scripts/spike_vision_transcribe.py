"""B안 스파이크(일회성): 이미지 공고 → 비전 전사 품질 실측.

구현이 아니라 게이트 판정 재료다. 파이프라인(src/pipeline.py)을 건드리지 않는다.
전사만 시킨다 — 요구사항 추출 금지(이미지→요구사항 직행은 측정 안 된 새 파이프라인이 된다).

사용: 타일 사전 생성(시스템 python3 + PIL, 프로젝트 venv엔 Pillow가 없어 일회성이라 의존성
추가 대신 사전 처리) 후:

    uv run python scripts/spike_vision_transcribe.py <tiles_dir>

- tiles_dir: <이름>_NN.jpg 형태의 세로 타일들(폭≤1000, 높이 1600, 겹침 80).
  긴 이미지를 통짜로 보내면 API가 축소해 작은 글자가 뭉개진다 — 그래서 타일이다.
- 출력: out/private/spike_vision/<이름>.txt 전사 전문 (커밋 안 됨) + 표준출력 요약
"""

import base64
import sys
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.schemas import MODEL, PRICE_INPUT_PER_1M, PRICE_OUTPUT_PER_1M  # noqa: E402

load_dotenv()

# 전사 프롬프트 — 단순하게. 표 읽기 순서 힌트를 주지 않는다(그게 관전 포인트라 힌트는 반칙).
PROMPT = "이미지에 적힌 텍스트를 그대로 받아 적어 주세요. 요약·해석·설명을 하지 말고, 보이는 글자만 순서대로 적으세요."


def main() -> int:
    if len(sys.argv) < 2:
        print("사용법: uv run python scripts/spike_vision_transcribe.py <tiles_dir>")
        return 1
    tiles_dir = Path(sys.argv[1])
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in sorted(tiles_dir.glob("*.jpg")):
        name = p.stem.rsplit("_", 1)[0]
        groups[name].append(p)
    if not groups:
        print(f"{tiles_dir}에 타일이 없습니다.")
        return 1

    client = OpenAI()
    out_dir = Path("out/private/spike_vision")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"model={MODEL}\n")
    total_cost = 0.0
    for name, tiles in groups.items():
        t0 = time.time()
        parts: list[str] = []
        tok_in = tok_out = 0
        for p in tiles:
            b64 = base64.b64encode(p.read_bytes()).decode()
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                            },
                        ],
                    }
                ],
            )
            parts.append(resp.choices[0].message.content or "")
            tok_in += resp.usage.prompt_tokens
            tok_out += resp.usage.completion_tokens
        latency = time.time() - t0
        cost = tok_in / 1e6 * PRICE_INPUT_PER_1M + tok_out / 1e6 * PRICE_OUTPUT_PER_1M
        total_cost += cost
        text = "\n\n--- [타일 경계] ---\n\n".join(parts)
        (out_dir / f"{name}.txt").write_text(text, encoding="utf-8")
        print(
            f"{name}: 타일 {len(tiles)}개 · {latency:.1f}s · in {tok_in} / out {tok_out} tok · ${cost:.4f}"
        )
    print(f"\n총 비용: ${total_cost:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
