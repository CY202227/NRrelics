# 测试脚本

## `ocr_temp_images.py`

对 `data/temp/` 下的截图批量 OCR，每张图生成同名 `.json`（例如 `001.png` → `001.json`）。

### 准备

```bash
cd NRrelics-master
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

把截图放进 `data/temp/`。

### 运行

```bash
# 默认：整图「检测+识别」，读出屏幕上所有文字块
python test/ocr_temp_images.py --overwrite

# 仅原文，不做词条库纠错
python test/ocr_temp_images.py --overwrite --no-correction

# 遗物截图额外做 6 行切分 + 深夜分类（可选）
python test/ocr_temp_images.py --overwrite --split-lines 6 --mode deepnight
```

**说明**：主程序遗物识别用的是已裁好的单行 ROI（`use_det=False`）。全屏截图必须用检测模式，否则会整屏当一行，结果只有乱码或空。

### JSON 字段说明

| 字段 | 说明 |
|------|------|
| `full.regions` | 每个检测框的 `text`、`score`、`box` |
| `full.texts` | 按阅读顺序排列的全部文字 |
| `full.raw_joined` | 多行拼接原文 |
| `full.entries` | 词条分割 + 纠错后的列表 |
| `lines` | `--split-lines` 时额外单行 OCR |
| `classification` | `--mode` 时正面/负面结构 |

后续可在这些 JSON 基础上做：恢复存档 → 抽卡/购买 → 备份合格存档 → 写 log。
