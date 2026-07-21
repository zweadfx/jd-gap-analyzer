"""이미지 공고 → 텍스트 전사 (B안). 웹 레이어 전용 — 파이프라인(src/)을 건드리지 않는다.

스파이크(2026-07-21/22 게이트 통과)와 동일 로직: 폭≤1000 축소 → 높이 1600·겹침 80 세로 타일
→ gpt-5.5 전사 → 병합. 전사는 입력창을 채우는 것까지만이고, 이후는 기존 텍스트 흐름 그대로다.

**이미지·전사 원문은 메모리에서만 처리한다. 디스크 저장 금지** (기존 원문 미저장 원칙의 연장).
"""

import base64
import io
import time

from openai import OpenAI
from PIL import Image

# 스파이크에서 게이트를 통과한 조합 그대로. 파이프라인 MODEL(src.schemas)과는 별개다.
VISION_MODEL = "gpt-5.5"
# OpenAI 공식 가격표(developers.openai.com/api/docs/pricing, 2026-07-21 확인).
PRICE_INPUT_PER_1M = 5.00
PRICE_OUTPUT_PER_1M = 30.00

MAX_W = 1000
TILE_H = 1600
OVERLAP = 80
# 이미지당 타일 상한. 스파이크 최장(AhnLab)이 11타일·~100s였다 — 12타일 ≈ 최악 ~2분.
# 초과는 조용히 자르지 않고 거절한다(침묵 실패 금지).
MAX_TILES = 12

# 스파이크와 동일 프롬프트 — 전사만. 요구사항 추출 금지.
PROMPT = "이미지에 적힌 텍스트를 그대로 받아 적어 주세요. 요약·해석·설명을 하지 말고, 보이는 글자만 순서대로 적으세요."

# 빈 타일에서 모델이 내는 필러 문구. 병합 시 통째로 버린다.
_FILLER_PREFIXES = ("보이는 텍스트가 없", "텍스트가 없습니다", "이미지에 텍스트가 없")


class ImageTooLongError(Exception):
    """타일 상한 초과. 자르지 않고 거절한다."""


def tile_image(data: bytes) -> list[bytes]:
    """이미지 바이트 → JPEG 타일 목록. 깨진 이미지는 PIL이 예외를 낸다(호출부에서 400)."""
    im = Image.open(io.BytesIO(data))
    im = im.convert("RGB")
    if im.width > MAX_W:
        im = im.resize((MAX_W, int(im.height * MAX_W / im.width)), Image.LANCZOS)
    tiles: list[bytes] = []
    y = 0
    while y < im.height:
        tile = im.crop((0, y, im.width, min(y + TILE_H, im.height)))
        buf = io.BytesIO()
        tile.save(buf, format="JPEG", quality=85)
        tiles.append(buf.getvalue())
        if len(tiles) > MAX_TILES:
            raise ImageTooLongError(f"타일 {len(tiles)}개 초과 (상한 {MAX_TILES})")
        y += TILE_H - OVERLAP
    return tiles


def merge_parts(parts: list[str]) -> str:
    """타일 전사들을 이어붙인다. 단순하게 — 완전 일치만 지운다(유사도 매칭 없음).

    - 필러 문구뿐인 타일은 통째로 버린다.
    - 겹침(OVERLAP) 때문에 타일 경계에서 같은 줄이 반복된다: 이전 누적의 꼬리 줄들과
      다음 타일의 머리 줄이 **완전 일치**하면 다음 타일 쪽에서 제거한다.
    """
    acc_lines: list[str] = []
    for part in parts:
        text = part.strip()
        if not text or any(text.startswith(p) for p in _FILLER_PREFIXES):
            continue
        lines = text.splitlines()
        tail = [ln.strip() for ln in acc_lines[-6:] if ln.strip()]
        while lines and lines[0].strip() and lines[0].strip() in tail:
            lines.pop(0)
        if acc_lines and lines:
            acc_lines.append("")  # 타일 사이 빈 줄 하나
        acc_lines.extend(lines)
    return "\n".join(acc_lines).strip()


def transcribe_tiles(tiles: list[bytes]) -> dict:
    """타일 목록 전사. 반환: text 및 계측(tiles/latency_s/cost_usd) — 이미지 내용은 반환 외 미보관.

    tile_image()와 분리한 이유: 타일 상한 초과·깨진 이미지 거절은 API 호출 전(=과금·카운터 소비 전)에
    일어나야 한다. 호출부가 tile_image → 가드 소비 → transcribe_tiles 순으로 부른다.
    """
    client = OpenAI()
    t0 = time.time()
    parts: list[str] = []
    tok_in = tok_out = 0
    for jpg in tiles:
        b64 = base64.b64encode(jpg).decode()
        resp = client.chat.completions.create(
            model=VISION_MODEL,
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
    return {
        "text": merge_parts(parts),
        "tiles": len(tiles),
        "latency_s": time.time() - t0,
        "cost_usd": tok_in / 1e6 * PRICE_INPUT_PER_1M + tok_out / 1e6 * PRICE_OUTPUT_PER_1M,
    }
