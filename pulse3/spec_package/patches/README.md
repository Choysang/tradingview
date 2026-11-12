# 旧系统有限修复补丁

`legacy_fixes.patch` 仅用于已审计旧系统的三项修复；全新业务实现按 `spec/FINAL_SYSTEM_SPEC.md` 与 `spec/IMPLEMENTATION_GUIDE_V3.md` 开发。补丁不应复制进入新架构作为业务模块。

## 基线与内容

- 仓库：https://github.com/Choysang/tradingview
- 基线commit：`6f5c57f8df73c58f0340dab6977b95c05d21740c`
- 补丁SHA-256：`b4bb58bcd2bd82b77c50d741e5bb8fe8e65467939b289251cc8ff8579484a600`
- 仅包含六个文件，124行增加、18行删除；没有依赖、配置或生产数据变更。

推荐过滤统一使用证券主数据的 `board == "10cm"`，未知板块不进入推荐；新闻意外判定异常后回退raw/parquet输入游标、仅重试失败消息；当日日线最终完成界限从15:05移至15:30。对应回归测试随补丁提供。完整证据及原始行号见 `spec/CODE_AUDIT.md` 的C03、C09、C16。

六个文件：

1. `backend/app/playbooks/picks.py`
2. `backend/app/warehouse/news_alerts.py`
3. `backend/app/warehouse/sync.py`
4. `backend/tests/test_picks.py`
5. `backend/tests/test_news_alerts.py`
6. `backend/tests/test_warehouse.py`

## 应用与核对

在上述基线或经过差异核对的工作区根目录执行，路径替换为补丁实际位置：

```bash
git apply --check /path/to/legacy_fixes.patch
git apply /path/to/legacy_fixes.patch
git diff --check
python -m pytest backend/tests/test_picks.py backend/tests/test_news_alerts.py -q
python -m pytest backend/tests/test_warehouse.py -q
```

如果工作区已包含这三项修复，不要重复应用。补丁创建时已用从基线导出的六文件副本执行 `git apply --check`，结果通过；这项检查不修改原仓库文件。

## 已执行验证

推荐和新闻测试文件为 **201 passed, 1 skipped**；仓库同步测试文件为 **47 passed**。两组共 **248 passed, 1 skipped**。跳过项是缺少本地申万行业数据时的 `test_macro_industries_exist_in_the_sw_table`。新增推荐/新闻回归先在未修复实现上观察到5个失败，再在修复后通过。

独立复核3个入口展开为6个场景，结果 **6 passed**；属于重复窄验证，不再加到248。独立diff检查通过。未将局部通过表述为全仓库全部测试通过。

## 明确保留的限制

`REPORT_FORMAT` 增至4只在 `generate_report()` 检查；`latest_report()` 旧缓存读取入口及其部分通知/调度调用仍可能先取得旧报告。不能承诺所有入口立即完成缓存迁移，也不能为了统一当前推荐而改写历史归档。

新闻补丁恢复的是 `judge()` 抛出的异常；LLM HTTP失败、超时等已经被转换为正常返回状态的路径仍可能标记完成。规则状态和模型分析状态分开持久化是新实现的要求，不在这次窄补丁内。补丁也未实现跨文件事务和长期失败消息队列。

15:30修复对应当前日线完成边界，没有新增全历史、全市场交易日历与源数据水位模型；新架构仍需按有效日期和会话确定最终状态。

此次交付没有commit、push、部署、生产通知或真实交易。旧原始数据、冻结主书、实验失败档案均未修改。
