# Quant-Arena Table 2 独立窄核对

**结论：旧冷审P09的“无配置胜出”部分应被本记录替代；原成本边界修正保留。** 本次未改stage或旧冷审。所读版本为[arXiv 2610.30113v1](https://arxiv.org/html/2610.30113v1)，读取范围限摘要、相关协议、Table 2及结论。

## 原文事实和内部矛盾

| 定位 | 独立核对结果 |
| --- | --- |
| Table 2，GPT-6 Sol medium行 | ARR 18.4%；等权毛参照17.1%；超额栏报告+1.4个百分点。 |
| §3.2表后首段、§5结论 | 38个迭代配置中仅该配置胜出。 |
| 摘要第三段、§1贡献项 | 概括为“No model beats the equal-weight market in 2024”。 |
| §3.2表后首段 | 明确写“only one beats the market”；与上项绝对概括不一致。 |
| §2.4 | 策略含成本/模拟冲击；参照不扣成本。 |

§3.1把后续诊断限定为每模型总分最高配置；不能自行断言这就是摘要的未注明本意。主文应按表格和正文精确陈述配置范围，并记录原文内部不一致。

按显示数计算18.4−17.1为1.3个百分点。因此+1.4应继续标记为**作者超额栏报告值**；未获得未舍入数值，不能确认差异来自舍入，也不应擅改论文报告值。

## 哪些记录需要改，哪些已经正确

| 文件与定位 | 本次裁定 |
| --- | --- |
| `research_v5/reviews/papers_root_cold_review.json`，R32-P09的findings[0].correction、supported_claim、proposed_decision_reason | 本记录明确supersedes其中的无模型/无配置胜出概括；保留成本不对称发现。 |
| `research_v5/reviews/papers_root_cold_review.md`，Quant-Arena小节原第29行 | “其未跑赢等权的结论可以准确转述”缺少例外，应被本记录替代。历史文件不删除。 |
| `research_v5/evidence/papers_root.json`，R32-P09 | 当前已经写明一例胜出，核心事实正确。 |
| `research_v5/reviews/root_final.json`，RP090 | 当前例外和成本边界均已正确。 |
| stage的`reference_papers_v32.json`与`REFERENCE_PAPERS_V32.md`，R32-P09 | 当前采用结论正确；被审input_claim作为原始材料继续保留。 |
| `research_v5/reviews/root.json`，RP090 | 是原通用审查指针，本身没有写全配置均失败，勿误归为错误数值断言。 |

发布QA宜写明：**独立复核曾仅读摘要和评估协议，遗漏Table 2的配置例外；本次补读表格、正文和结论后更正，旧冷审的对应部分由本记录替代。** 这只描述可观察的读取范围与更正关系，不揣测旧审查员的内部过程。

本次不授予生产有效性：未复现作者代码、原始精度或订单簿；不能从一个封存年度的一项配置推出持续超额收益。完整文件哈希、字段定位和替换建议见`narrow_quant_review.json`。
