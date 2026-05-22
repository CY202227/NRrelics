#!/usr/bin/env python3
"""
批量 OCR：读取 data/temp 下的截图，在同目录写出同名 .json。

默认对整图启用「检测 + 识别」（use_det=True），逐块读出屏幕上所有文字。
主程序遗物词条用的是单行 ROI（use_det=False），不适合直接套在全屏截图上。

用法（在项目根目录）:
    python test/ocr_temp_images.py --overwrite
    python test/ocr_temp_images.py --mode deepnight --split-lines 6

依赖: pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.ocr_engine import (  # noqa: E402
    OCREngine,
    correct_entries,
    postprocess_text,
    split_entries,
)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def _box_sort_key(box: np.ndarray) -> tuple[float, float]:
    """按阅读顺序：先上后下，同行从左到右。"""
    cy = float(box[:, 1].mean())
    cx = float(box[:, 0].mean())
    return (cy, cx)


def _box_to_list(box: np.ndarray) -> list[list[float]]:
    return [[float(x), float(y)] for x, y in box.tolist()]


def ocr_full_image_detect(ocr: OCREngine, image: np.ndarray, *, correct: bool) -> dict:
    """
    整图 OCR：先检测文字框，再逐块识别（适合全屏/UI 截图）。
    """
    result = ocr.engine(image, use_det=True, use_cls=True)
    if not result or not result.txts:
        return {
            "method": "full_detect",
            "success": False,
            "regions": [],
            "texts": [],
            "raw_joined": "",
            "raw_entries": [],
            "entries": [],
        }

    boxes = result.boxes
    txts = list(result.txts)
    scores = list(result.scores) if result.scores else [0.0] * len(txts)

    if boxes is not None and len(boxes) == len(txts):
        ordered = sorted(
            zip(boxes, txts, scores),
            key=lambda item: _box_sort_key(item[0]),
        )
    else:
        ordered = [(None, t, s) for t, s in zip(txts, scores)]

    regions = []
    texts = []
    for index, (box, text, score) in enumerate(ordered):
        text = str(text).strip()
        if not text:
            continue
        region = {
            "index": index,
            "text": text,
            "score": round(float(score), 4),
        }
        if box is not None:
            region["box"] = _box_to_list(np.asarray(box))
        regions.append(region)
        texts.append(text)

    raw_joined = "\n".join(texts)
    processed = postprocess_text(raw_joined)
    raw_entries = split_entries(processed)

    entries = raw_entries
    correction_time_ms = 0.0
    if correct and raw_entries and ocr.corrector:
        import time

        t0 = time.time()
        entries = correct_entries(raw_entries, ocr.corrector)
        correction_time_ms = round((time.time() - t0) * 1000, 2)

    return {
        "method": "full_detect",
        "success": bool(texts),
        "region_count": len(regions),
        "regions": regions,
        "texts": texts,
        "raw_joined": raw_joined,
        "raw_entries": raw_entries,
        "entries": entries,
        "correction_time_ms": correction_time_ms,
    }


def split_horizontal_lines(
    image: np.ndarray, line_count: int
) -> list[np.ndarray]:
    """将整图按高度均分为若干行 ROI（遗物词条专用，需与游戏 ROI 对齐时才准）。"""
    if line_count < 1:
        return [image]
    height, width = image.shape[:2]
    if height < line_count:
        return [image]

    lines: list[np.ndarray] = []
    step = height / line_count
    for i in range(line_count):
        y0 = int(round(i * step))
        y1 = int(round((i + 1) * step))
        if y1 <= y0:
            continue
        lines.append(image[y0:y1, 0:width].copy())
    return lines or [image]


def ocr_line_blocks(ocr: OCREngine, image: np.ndarray, line_count: int) -> list[dict]:
    """按行切分后单行 OCR（与游戏内逻辑一致，非默认路径）。"""
    rows: list[dict] = []
    for index, line_img in enumerate(split_horizontal_lines(image, line_count)):
        text, score = ocr.recognize_single_line(line_img)
        rows.append({
            "line_index": index,
            "text": text,
            "score": round(float(score), 4),
        })
    return rows


def build_payload(
    image_path: Path,
    ocr: OCREngine,
    *,
    mode: str | None,
    line_count: int,
    correct: bool,
) -> dict:
    image = cv2.imread(str(image_path))
    if image is None:
        return {
            "image": image_path.name,
            "image_path": str(image_path),
            "success": False,
            "error": "无法读取图片",
        }

    payload: dict = {
        "image": image_path.name,
        "image_path": str(image_path),
        "width": int(image.shape[1]),
        "height": int(image.shape[0]),
        "ocr_at": datetime.now(timezone.utc).isoformat(),
        "success": False,
    }

    # 1. 整图检测 OCR（主结果）
    full = ocr_full_image_detect(ocr, image, correct=correct)
    payload["full"] = full
    payload["success"] = full.get("success", False)

    # 2. 可选：按行切分（遗物整图试验用）
    if line_count > 0:
        line_rows = ocr_line_blocks(ocr, image, line_count)
        payload["lines"] = line_rows
        combined = "\n".join(row["text"] for row in line_rows if row["text"])
        payload["lines_combined"] = combined
        if combined:
            payload["lines_entries"] = split_entries(combined)

    # 3. 可选：词条库分类（需 --mode，且遗物行切分更准确）
    if mode in ("normal", "deepnight"):
        if line_count > 0:
            line_images = split_horizontal_lines(image, line_count)
            classified = ocr.recognize_with_classification_from_lines(
                line_images, mode
            )
        else:
            classified = ocr.recognize_with_classification(image, mode)

        payload["classification"] = {
            "mode": mode,
            "success": classified.get("success", False),
            "positive_count": classified.get("positive_count", 0),
            "negative_count": classified.get("negative_count", 0),
            "affixes": classified.get("affixes", []),
            "correction_failed_affixes": classified.get(
                "correction_failed_affixes", []
            ),
        }

    return payload


def iter_images(input_dir: Path) -> list[Path]:
    return [
        p for p in sorted(input_dir.iterdir())
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    ]


def write_json(output_path: Path, data: dict) -> None:
    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def preview_text(payload: dict, limit: int = 100) -> str:
    full = payload.get("full", {})
    texts = full.get("texts") or []
    if texts:
        return " | ".join(texts)[:limit]
    if full.get("raw_joined"):
        return full["raw_joined"][:limit]
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="对 data/temp 内图片做整图检测 OCR，并写出同名 JSON"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "temp",
        help="图片目录（默认: data/temp）",
    )
    parser.add_argument(
        "--mode",
        choices=("normal", "deepnight"),
        default=None,
        help="可选：额外做遗物词条正面/负面分类（建议配合 --split-lines 6）",
    )
    parser.add_argument(
        "--split-lines",
        type=int,
        default=0,
        metavar="N",
        help="额外：按高度切 N 行做单行 OCR（默认不做）",
    )
    parser.add_argument(
        "--no-correction",
        action="store_true",
        help="不做词条库纠错，只保留检测出的原文",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已存在的 JSON",
    )
    args = parser.parse_args()

    input_dir = args.input_dir.resolve()
    if not input_dir.is_dir():
        print(f"[错误] 目录不存在: {input_dir}")
        return 1

    images = iter_images(input_dir)
    if not images:
        print(f"[提示] {input_dir} 下没有图片")
        return 0

    print("加载 OCR 模型（整图模式: 检测+识别）...")
    ocr = OCREngine()
    if args.no_correction:
        ocr.corrector = None
    elif not ocr.corrector:
        ocr.load_vocabulary("deepnight")
    if args.mode:
        ocr.load_vocabulary(args.mode)

    ok_count = 0
    for image_path in images:
        json_path = image_path.with_suffix(".json")
        if json_path.exists() and not args.overwrite:
            print(f"[跳过] {image_path.name}（已有 JSON，加 --overwrite 覆盖）")
            continue

        print(f"[OCR] {image_path.name} ...", flush=True)
        try:
            payload = build_payload(
                image_path,
                ocr,
                mode=args.mode,
                line_count=args.split_lines,
                correct=not args.no_correction,
            )
            write_json(json_path, payload)
            n = payload.get("full", {}).get("region_count", 0)
            status = f"{n} 块文字" if payload.get("success") else "无文字"
            print(f"  -> {json_path.name} [{status}] {preview_text(payload)}")
            if payload.get("success"):
                ok_count += 1
        except Exception as exc:
            print(f"  -> 失败: {exc}")
            write_json(json_path, {
                "image": image_path.name,
                "success": False,
                "error": str(exc),
            })

    print(f"\n完成: {ok_count}/{len(images)} 张有识别结果")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
