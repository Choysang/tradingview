# Pulse A股平台 v3.2 完整重写实施包

这是可开发的产品、数据库、接口、研究和验收规格，附有限可运行参考。主线为消息、情绪、技术、资金 → 全市场与板块排序 → 个股条件计划 → 统一评价 → 策略池反馈。Agent用于并行开发；模型能力升级通过版本化适配器和评价协议接入。

本次以十一卷参考规格为主要输入，逐文件处置1524个实质单元，形成590条决定：69采用、41已有覆盖、459修改后采用或限定、19不采纳、2项事实未解决。复合条目进一步展开159个因素命名条目、56个来源条目、70个事件条目、21项产品子功能。每项包含理由、证据边界、实施动作与验收条件。覆盖数字不代表逐行证明全部旧代码，也不代表收益复现。

现有合同基线保持80表883列、31项API操作、142个公共定义和唯一经济账本。16类产品资源另有完整扩展卡，列出41个拟议应用操作、40个拟议类型及持久化要求；这些尚未进入正式OpenAPI和DDL，不计入上述已检查基线。61策略和105因素仍为候选登记。

## 建议阅读顺序

| 目的 | 文件 |
| --- | --- |
| 先看本轮关键修订和理由 | spec/REFERENCE_RECONCILIATION_V32.md |
| 逐条核对十一卷原文与取舍 | review_v32/REVIEW_FULL.md；decisions.json；unit_decision_map.json |
| 一次读完整方案 | Pulse_Ashare_Rewrite_Full.md，或配套Word；Markdown另含完整审查附录 |
| 看全部55条文献引用如何处置 | research/REFERENCE_PAPERS_V32.md；reference_papers_v32.json |
| 看21项子功能和16类资源的接口缺口 | review_v32/product_subfeature_mapping.json；product_api_mapping.json；product_extension_cards.json |
| 看数据如何产出并到达页面 | review_v32/dataset_job_page_mapping.json |
| 确定唯一权威与实际验证状态 | authority_manifest.json；verification/verification_report.md |
| 看产品及全链语义 | spec/FINAL_SYSTEM_SPEC.md |
| 看数据库字段、容量、映射和恢复 | database/；integration/ |
| 看来源、社区、龙虎榜、板块和分类 | sources_sectors/；catalogs/；review_v32/appendix_item_mapping.json |
| 看唯一成交经济账本与benchmark | execution/ |
| 看共享计算与时点对齐参考 | reference_features/；reference_system/；reference_alignment/ |
| 看试用预算与基线连续性参考 | reference_transition/ |
| 看策略联赛与核心46篇研究 | spec/STRATEGY_LEAGUE_SPEC.md；research/papers_registry.json |
| 交给开发Agent | AGENT_START_PROMPT.md；traceability/work_packages.json；work_packages_v32_extension.json；milestones_v32.json |
| 看既有补充方案的50项决议 | spec/ADDENDUM_V31.md；spec/PROPOSAL_DECISIONS_V31.md |
| 看旧仓库问题及局部补丁 | spec/CODE_AUDIT*.md；patches/；review_v32/audit_ledger.md |

## 重新验证

使用Python 3.12或兼容环境，安装requirements-validation.txt后，在解压根目录执行：

```bash
python run_verification.py
python validate_package.py
```

运行会重建验证日志。作者参考单元测试167项，独立ASOF单元用例16项，合计183项；另外分别列示手算案例、合成fixture、静态合同及审查完整性检查。三个Repository关联缺口仍明确未实现，不能计为生产通过。发布SHA256SUMS.json描述交付原始字节；重新运行后日志时间戳变化不表示代码变更。

所有正式公共接口在contracts/。v3.2对旧文字的明确修订优先；扩展卡须由W01进入同一合同与迁移，再交业务实现。review_v32/reference_input为上传原文，baseline_v31为少量被引用且后来已改的历史规格，两者均不具备当前执行权威。旧代码提交和证据定位规则见review_v32/LOCATOR_GUIDE.md。

交付没有连接真实源、执行正式数据库迁移、重跑论文训练收益或部署网站。源码审计基线为6f5c57f；上传作者所述3fc555d未取得。剩余事实不确定性按影响范围列入任务，不阻止已授权的合同、内核与产品开发。
