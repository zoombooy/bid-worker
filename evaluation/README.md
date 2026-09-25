# 人工黄金样本与评测

每份黄金样本对应一个确定版本的原始招标文件。使用 `gold_sample.template.json` 复制创建标注文件，不要把未完成的模板用于计算指标。

## 标注步骤

1. 对原始文件计算 SHA-256；值必须与预测结果 `analysis.document.sha256` 完全一致，否则评测器拒绝比较。
2. 完整阅读原件并填写 `coverage_reviewed`：`project_fields`、`technical_criteria`、`commercial_criteria`、`price_criteria`。漏项既可能被误判为模型漏检，也可能造成虚高的召回率。
3. 在 `project_fields` 只标注业务要求覆盖的字段。值填原文值；确认全文未出现时填 `null`。复杂金额和日期不自行换算，原始表达作为正确答案。
4. `criteria` 中逐项登记所有评分项。保留完整评分原文，填写类别、原件页码、明示的最高分；原文没有明示最高分时填 `null`。有 bbox 时使用 PDF 页面点坐标 `[x0,y0,x1,y1]`。
5. 由另一位复核者检查字段、全部技术/商务/价格评分项、分值和证据定位，再设置 `annotation_complete: true`。

字段示例：

```json
"project_fields": {
  "project_title": {"value": "示例项目名称", "page_no": 1, "source_quote": "项目名称：示例项目名称"},
  "project_number": {"value": null, "page_no": null, "source_quote": null}
}
```

评分项示例：

```json
"criteria": [
  {
    "category": "technical",
    "subcategory": "similar_projects",
    "source_quote": "近五年完成类似工程业绩，每项得2分，最高6分。",
    "page_no": 18,
    "max_score": 6,
    "bbox": [72.0, 320.0, 520.0, 348.0]
  }
]
```

上述内容仅说明标注格式，不是系统识别效果或真实样本数据。评分原文应来自原件并经人工复核。

## 执行评测

从分析 API 保存的响应 JSON 直接评估：

```powershell
python scripts/evaluate_gold.py --gold evaluation/gold_doc_001.json --prediction evaluation/prediction_doc_001.json --output evaluation/report_doc_001.json
```

报告包含字段精确准确率、评分项 precision/recall/F1、匹配项类别与最高分准确率、证据页码/原文准确率，以及提供 bbox 标注时 IoU≥0.5 的定位准确率。评分项匹配按页码约束（若提供页码）和字符 bigram Dice 阈值进行一对一匹配；该策略用于建立第一版基线，需人工检查 `criterion_matches` 和未匹配列表，不能把自动匹配结果视为黄金标注真值。

准备多个项目的评测集时，复制 `manifest.template.json`，每个样本分别指向完整人工黄金标注和对应 API 预测 JSON：

```powershell
python scripts/evaluate_corpus.py --manifest evaluation/manifest.json --output evaluation/corpus_report.json
```

汇总器会检查同一 `project_id` 不得同时出现在 development 与 blind_test，并输出总体 micro 指标、按划分统计和按文件类型统计。建议先用 development 样本调规则；冻结版本后再一次性跑 blind_test。

盲测应按完整项目分组，不能把同一项目的补遗、模板近似件或不同格式副本拆进调试集和测试集。汇总报告应同时列出各格式、扫描/数字 PDF、跨页表格等分层指标与人工复核比例，不应只报总体平均值。
