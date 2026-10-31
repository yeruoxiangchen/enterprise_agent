# v4 保存回答的分项复评

原执行结果、旧评分校准和数据库不改写。不重跑问答，不重复创建工单。
分开评内容、引用、事实忠实性；咨询任务完成=内容正确，办理任务额外要求原数据库回执检查通过。
三项仍是同一个模型的评审，不是独立人工准确率；无须知识引用的答复按引用支持通过计算。
旧校准失败不用于任何正确率结论；本次保留原5题标签并增加3个对照。

评分状态：{
  "pid": 664843,
  "start_ticks": "1312659771",
  "updated_at": 1793362266.3612478,
  "phase": null,
  "status": "calibration_failed_pending_manual_review",
  "completed": 8,
  "total": 8,
  "failed": 1
}

| 阶段 | 正常返回 | 内容正确/错误/未知 | 引用支持/错误/未知 | 任务完成/错误/未知 | 忠实/错误/未知 |
|---|---|---|---|---|---|
|multi_v3|20/23|0/0/23|0/0/23|0/0/23|0/0/23|
|multi_v4|23/23|0/0/23|0/0/23|0/0/23|0/0/23|
|new_acceptance|13/13|0/0/13|0/0/13|0/0/13|0/0/13|
|regression|17/17|0/0/17|0/0/17|0/0/17|0/0/17|
|single|23/23|0/0/23|0/0/23|0/0/23|0/0/23|

原始回答：/data/zjr/enterprise_kb_agent/artifacts/business_multi_v4_20261030/dev_v1/results.jsonl
原校准：/data/zjr/enterprise_kb_agent/artifacts/business_multi_v4_20261030/dev_v1/calibration.json
本次逐题评分：/data/zjr/enterprise_kb_agent/artifacts/business_multi_v4_20261030/dev_v1/review_split_v1/grades.jsonl
本次校准：/data/zjr/enterprise_kb_agent/artifacts/business_multi_v4_20261030/dev_v1/review_split_v1/calibration.json
