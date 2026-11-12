# 产品扩展卡交叉窄审

审查者：`/root/v32_audit_ledger`。范围仅PEX05、PEX06、PEX12、PEX14、PEX15、PEX16及关联类型/存储。读卡后对照公共JobReceipt、JobRun、ExperimentRequest、EvaluationContext，不改卡、不实施提案。

## 已确认部分

独立计算为16资源、41拟新增操作、40提案类型、27存储定义、14拟SQL表。OpenAPI实际仍为31操作；卡内记录的公共合同和全部4份SQL哈希均与当前文件相等，未因提案加大既有31/80口径。类型与存储状态明确标为proposal。

- PEX05通过D04 sidecar表达origin/change_reason，未塞入严格TradePlan；异步proposal有明确查询和最终plan引用。
- PEX06没有把replay_spec塞入现行六字段ExperimentRequest；D07明确现行训练窗不可空，no_fit须新合同和迁移，未实施前拒绝不支持形状。
- PEX12金额是Decimal字符串，未知费用不能补0；实际券商成交不再由模拟matcher筛掉；账户授权关系明确拟新增，不假称account_head已有owner列；TWR缺边界估值时明确缺失。
- PEX16限定现有四种通知，新事件须新公开版本，poll与SSE共享日志和过滤；未见当前v3混发新类型的许可。

## 需要修订的提案细节

### PX-C01：D27投影身份缺计算上下文

`pulse.account_readmodels@0.1.identity`只有account_id/account_version/projection_kind/policy_hash；policy_hash被定义为状态机或收益政策版本。PEX12绩效查询还接收window，输出依赖估值快照。相同账户事件序号、相同政策下查询不同窗口，或行情价格更新但未发生账户事件，都可能对应不同绩效，却撞到同一个不可变产物身份。

建议增加明确的projection_context_hash，覆盖窗口、收益/估值政策、行情输入闭包及完整请求语义；订单列表也固定窗口/事实水位。若保留policy_hash，不能暗中赋予它未声明的输入快照含义。验收：同account_version的两窗口、两估值快照可以产生四个各自可复核产物，重复相同上下文复用同产物。

### PX-C02：D24导出缺持久化工作区版本输入

`scheduleWorkspaceExport`接受workspace_objects，而`ops.export_request`仅保存行情快照、字段投影、format和job引用，没有这些具体布局/画线版本。恢复worker不能从这些字段唯一恢复渲染内容；后续当前布局可能已经变更。

建议D24显式保存workspace_objects，或保存含该字段的严格不可变export_spec_artifact与hash；同时对所引用revision建立保留引用。验收：提交后改画线、重启worker，导出仍使用提交时的画线/布局版本。不要把只有请求hash的回执当完整请求存储。

### PX-C03：BrokerFill尚不能无歧义表达纯成交撤销

类型只有correction_of_source_execution_id，没有fill/cancel/correct类型标记，且last_fill_quantity和价格均强制正值。它能表达替换成交的某些数据，却不能区分“纯撤销旧成交”与“以正数量新成交替换旧成交”。验收文案已承诺更正链，因此这是输入合同缺口。

建议明确execution_report_type及分支合同：普通成交、纯撤销、替换更正；纯撤销仅定位被撤销事实，不伪造正向新成交；唯一账本生成反向/更正分录，按source/account身份去重。费用后补更正也需说明是否只更正费用。验收：取消与同数量改价产生不同且正确的最终库存/现金，重复报告不重复记账。

### PX-C04：runRegisteredJob的收据路由需补一句

该操作返回现有JobReceipt，其中resource_id和poll_operation_id均必填；顶层async_receipt_bindings未列runRegisteredJob，输出说明只写“可恢复job”。建议登记resource_id=job_run_id、poll_operation_id=getJobRun，或声明另一确切语义，避免调用方猜测。另“重试复用同job identity和lease”宜明确为保留job身份、重新取得有效lease_epoch；不复活过期worker权限。

## 裁定边界

前三项是提案细节中会影响身份、恢复和记账的实质缺口；第四项为具体收据路由澄清。它们不意味着当前31个公共接口已经出错，也不要求立即实现全部扩展。修订相应设计卡即可，不需要扩大测试或修改当前公共合同。

审查时间：2026-11-11T03:00:08.182244+00:00。

卡文件SHA256：`3a3bb1dd8e0baf6a2b139c4bc9ce68595ee9e7ddcdcebf4fb71ab40bc05fcf3b`。

## 修订后窄回读关闭

PX-C01至PX-C04均已在设计层关闭：完整query_context_hash；不可变导出spec保存workspace对象版本并pin；BrokerFill显式fill/cancel/correct及费用语义；作业收据明确job_run_id/getJobRun，接管lease_epoch递增。原发现保留作审计历史，精确哈希见product_extension_cross_review_closure.json。未宣称提案已实现。
