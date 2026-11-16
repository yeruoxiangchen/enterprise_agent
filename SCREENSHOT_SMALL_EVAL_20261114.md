# 截图小测与完整ITSM复测：2026-11-14

按用户要求，截图仅做少量企业业务验收及公开样例检查，随后启动完整ITSM对照。数据、图片、数据库、模型缓存及结果均在`/data/zjr/enterprise_kb_agent/`。

## 截图结果

| 范围 | 结果 | 含义 |
| --- | --- | --- |
| 8个自建企业故障截图 | 26/26字段正确，知识文档匹配8/8、工单闭环8/8 | 实际CPU OCR、知识检索、确认草稿、管理员处理和员工回查 |
| 两例缺失信息 | 缺失字段保持空缺，人工补充后建单 | 人工补充由脚本模拟，未评估主管自然语言追问 |
| 8个公开网页截图 | API接受8/8，完整标注文字恢复4/8 | OCRBench v2公开截图的本地OCR文字覆盖检查 |

企业验收全流程p50 3.24秒、p95 4.09秒（含测试登录切换）；公开截图API p50 1.71秒、p95 3.22秒。两类结果分别报告。截图评测使用extractive知识检索，大模型调用0。

公开来源：[OCRBench v2](https://github.com/qywh2023/OCRBench/tree/main/OCRBench_v2)、[作者Hugging Face数据](https://huggingface.co/datasets/ling99/OCRBench_v2)。从`website_screenshot`选择8个直接可见文字问题，在推理前固定ID：3011、3043、3064、3074、3144、3186、3197、3214。沿用公开答案，仅测其文字能否在OCR中恢复，未测视觉问答或官方OCRBench总分。

公开样例暴露复杂整页中的浅色小字、分散品牌文字和金额符号漏读。现有功能适合裁剪后的报错截图；用户核对字段后再生成草稿。r1评分归一化遗漏金额符号意义，r2保留金额符号并清理布局下划线，沿用相同图片与答案，两轮均保留。

详细结果：[report.md](/data/zjr/enterprise_kb_agent/artifacts/screenshot_small_20261114/report.md)、[summary.json](/data/zjr/enterprise_kb_agent/artifacts/screenshot_small_20261114/summary.json)。

## 重跑入口

```bash
bash scripts/crm_bench_run.sh incident-acceptance \
  --cases /data/zjr/enterprise_kb_agent/datasets/incident_enterprise8_v1/cases.json \
  --screenshots-only --output /data/zjr/enterprise_kb_agent/artifacts/screenshot_small_20261114/enterprise8_new

bash scripts/crm_bench_run.sh public-screenshot --prepare \
  --output /data/zjr/enterprise_kb_agent/artifacts/screenshot_small_20261114/public8_new
```

企业manifest与公开图片均已落盘。换机器时公开入口可下载这8张图；企业manifest见本次报告归档副本。输出目录要求为全新目录，保留旧结果。

## 完整ITSM对照已启动

标签`strategy1_official_itsm66_20261114_r1`，队列PID`3650126`。官方GraphAgent与当前`experience`多Agent，完整66题各一次，共132次。沿用相同模型、种子、客户模拟和裁判，两组交替执行，每项恢复全新后台。源码和协议已冻结。

单卡Qwen3-8B，候选GPU1/7，实际选GPU1；显存预算78%（约18.4GiB），8192上下文。每题30轮/600秒、Agent单轮120秒；队列总时限6小时，异常释放本轮资源并保留已完成结果。

已进入实际评测，模型接口正常返回。完整目录的冻结任务／工具gold校验通过；完整目录协议与当前主管策略的18项定向检查通过。上次同规模运行1小时51分钟，本轮保留2–3小时的时间预期。截图结果与完整任务成绩分开记录。

```bash
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/crm_bench_run.sh watch --tag strategy1_official_itsm66_20261114_r1
bash /home/zjr/Tracker/internship_project_lab/enterprise_kb_agent/scripts/crm_bench_run.sh stop --tag strategy1_official_itsm66_20261114_r1
```

输出及主日志：`/data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/strategy1_official_itsm66_20261114_r1/`、`queue.log`。模型日志`actor-model.log`。

启动命令：

```bash
bash scripts/crm_bench_run.sh launch --tag strategy1_official_itsm66_20261114_r1 \
  --harness-version v6 --execution-adapter v4 --supervisor-strategy experience \
  --arms official_graph multi_v6 --full-itsm --trials 1 --shared-eval-model \
  --user-strategy contextual-react --max-hours 6 --gpus 1,7
```
