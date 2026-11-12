# 审查证据定位与版本说明

decisions.json是590条决定的当前记录，unit_decision_map.json是1524个实质单元的一对一索引。输入文件由reference_input保存，input_manifest.json记录其原始字节hash。REVIEW_FULL.md给出人类可读全文。

## 证据路径

- review_v32/reference_input/：本轮十一卷原文。原文含未经核实的主张；保留不表示认同。
- review_v32/baseline_v31/：被引用且本轮已有修订的旧规格副本，仅用于准确比较。未改的基线合同直接指向包内唯一文件，不另建账本或合同副本。
- review_v32/evidence/：容量、快照、精度等反例及旧代码/台账清点。反例证明具体逻辑问题，不是完整生产压测。
- review_v32/decisions.json#ID：论证记录本身的定位；kind为reasoning时不冒充外部实证。
- git:6f5c57f8df73c58f0340dab6977b95c05d21740c:路径#符号：固定Git提交的文件或符号。可在https://github.com/Choysang/tradingview/tree/6f5c57f8df73c58f0340dab6977b95c05d21740c查阅，或git show该提交文件。分号分隔多个位置；行号/符号后缀不是文件名。
- original_workbench_locator保留作者当时工作目录定位，当前locator用于解压后的追溯；不要求用户复制原工作目录。
- qa/保留首轮冷审与关闭记录。首轮发现不是当前未修问题；须连同closure读取。Quant-Arena首轮冷审遗漏了表中正超额例外，以narrow_quant_review及R32-P09/RP090修订为准。

## 不能混同的完成状态

原文被逐项处置、源论文被读到指定部分、参考代码测试通过、正式API实现、生产接入以及策略盈利验证，是不同状态。文件中的reading_level、implementation_status、remaining_uncertainty和acceptance共同说明边界。
