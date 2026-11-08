---
description: Team Agent 自动子代理分流与防冲突规则
globs: **/*
alwaysApply: false
---

# Team Mode 激活配置 (Preset: custom)

- **Explorer**: tier=`fast`, active=`Gemini 3.8 Flash`, effort=`High`, readonly=`True`
- **Executor**: tier=`standard`, active=`Gemini 3.8 Flash`, effort=`High`, readonly=`False`
- **Reviewer**: tier=`high`, active=`Claude Sonnet 4.6 (Thinking)`, effort=`Thinking`, readonly=`True`
- **ExpertAdvisor**: tier=`flagship`, active=`Claude Opus 4.6 (Thinking)`, effort=`Thinking`, readonly=`True`
- **Researcher**: tier=`fast`, active=`Gemini 3.8 Flash`, effort=`High`, readonly=`True`
- **GeneralWorker**: tier=`standard`, active=`Gemini 3.8 Flash`, effort=`High`, readonly=`False`
- **Designer**: tier=`fast`, active=`Gemini 3.8 Flash`, effort=`Medium`, readonly=`True`
