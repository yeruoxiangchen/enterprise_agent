# 多 Agent 主管与专业工具循环 v4

当前修订为`business_v6_external_v4_supervisor_r2_perf1`。保留主管/专业Agent结构，采用异步检查点调度并保留SQLite FULL和关键业务回执；148项联合代码回归通过。四题真实模型验证已完成：多Agent4/4、官方参考2/4，双方未判定及运行异常0，本轮资源已释放。最新性能与恢复依据见[延迟定位与优化](CRM_LATENCY_PERF1_20261114.md)，原执行修复见[执行层修复r2](CRM_EXECUTION_REPAIR_R2_20261113.md)，完整目录与后续待办见[当前状态](CURRENT_STATUS_AND_NEXT_STEPS_20261113.md)。

日期：2026-11-13。适配名 `business_v6_external_v4_supervisor`，CLI选择 `--harness-version v6 --execution-adapter v4`。先完成多Agent的小样本真实模型验收，再决定扩展范围。

最新同步：完整66项两组对照已完成132/132并释放资源。官方原始通过46/66、v4 48/66；双方均有效的63题各通过46、各独赢6，另3对涉及裁判异常。v4安全类更强，但模型调用和耗时明显增加。已记录裁判预算/语义误判、原文校验和终态/循环等具体问题，下一步先校准后统一重评，再修执行层。[当前结果、证据与下一步](CURRENT_STATUS_AND_NEXT_STEPS_20261113.md)、[校准验收记录](CALIBRATED_ACCEPTANCE_20261113.md)。下文保留v4实现和此前各轮运行记录。

## 实现依据

采用[LangChain官方Subagents模式](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents)：通过 `create_agent` 创建主管，将专业Agent包装成工具；主管持有可见会话上下文，专业Agent使用自己的工具集执行普通模型/工具循环，返回文本结果。

- [crm_supervisor.py](kb/benchmark/crm_supervisor.py)：主管、工单操作/记录安全/服务时间三个专业Agent、单Agent参考入口。
- 原生工具循环直接完成任务，不再先生成SpanRoute和逐步操作计划，也不再要求ExternalResult收尾。
- 主管按用户顺序逐个调用专业Agent。执行使用框架 `max_concurrency=1`，规范工具按模型声明顺序执行；主管调用专业Agent时不持有后端锁。
- 专业Agent接收分配任务，以及宿主提供的当前原文、待补充请求和可见历史；主管的转述只用于分工，身份、目标和引号内字段仍由原始用户请求核验。
- 原生循环之上的业务层继续复用身份、权限、依赖、确认、目标校验、真实回执、读缓存、写入未知及重复写入保护。
- `supervisor-state.json`记录会话级待办、操作编号、写入日志、实际调用审计和用量。补充身份沿用操作编号；相同请求重试复用回执，明确要求再次操作则创建新操作。
- `supervisor-checkpoints.sqlite3`记录单轮主管图；中断后恢复未结束节点，专业Agent重入时依靠持久化写入日志复用已提交回执。
- 协调调用保留在完整模型事件和独立delegation事件里；官方动作检查只接收规范业务工具记录。所有主管和专业模型调用均计入Token。

## 预算与评分

v4每轮最多24次模型调用、30次工具尝试（含委派）；上下文8192、输出1400。官方小测单任务180秒、单Agent轮次120秒、最多8个对话轮。预算耗尽在本地指标中计失败，原始官方判定另存。

独立模型模式使用两个服务，回答模型显存预算78%、用户模拟/评分模型75%，均使用8192上下文；领取GPU前及启动前均保留额外1024MiB余量。共享GPU的瞬时空闲显存可能在子进程初始化前变化，首轮启动失败记录见下文。当前单卡开发诊断只启动回答模型，显存预算78%，三个角色使用独立提示词、客户端、日志和用量记录，权重共用。

这些预算与历史v2不同，因此历史29/66、31/66只作为背景结果；新版本的效果由新运行目录独立记录。单Agent参考与多Agent均使用相同业务防护和预算。

## 验收

定向测试使用受控模型输出、真实LangChain/LangGraph、SQLite及上游规范ITSM工具，检查：两轮身份补充、被猜测身份的拦截、写入顺序、原文备注、重试与明确重复、取消、跨领域请求、提交后中断恢复、写入结果未知。代码测试与真实模型通过率分别记录。

修复恢复审计后，主管、启动配置及小批运行/计分27项定向测试全部通过（20.37秒）。此前联合回归的权限和旧执行契约部分通过，失败的恢复审计用例已在本轮修复并通过。

```bash
bash scripts/crm_bench_run.sh test \
  tests/test_crm_supervisor.py tests/test_crm_small_setup.py \
  tests/test_crm_overnight.py tests/test_crm_v6_contract.py tests/test_crm_execution.py \
  --basetemp /data/zjr/enterprise_kb_agent/tmp/crm-supervisor-integration-20261113
```

首轮真实模型开发小测 `v4_supervisor_dev_20261113_r1`：只执行multi_v6的查单、原文备注、顺序更新、缺身份后补充四项。固定用户驱动，每项恢复全新业务环境；单模型服务，结果不混称官方完整任务分数。

该轮已完成4/4，全部通过，用量完整，本次模型服务已释放。查单、备注、顺序更新分别使用4、4、5次模型调用；身份补充两轮共13次。身份用例存在3次无依据身份尝试，实际进入后端的次数为0，补充真实身份后只删除一次。此前v3同类检查执行4项、通过3项、身份补充失败，两轮均属于开发检查。

原始记录：[开发小测汇总](/data/zjr/enterprise_kb_agent/artifacts/crm_short_checks_20261113/v4_supervisor_dev_20261113_r1/summary.json)、[身份补充结果](/data/zjr/enterprise_kb_agent/artifacts/crm_short_checks_20261113/v4_supervisor_dev_20261113_r1/cases/identity__multi_v6/result.json)。此后另修复了恢复重放覆盖历史调用审计的问题：原始提交记录保持不变，重放另记审计项；本次开发结果不冒充修复后重新运行的结果。

```bash
bash scripts/crm_bench_run.sh small status --tag v4_supervisor_dev_20261113_r1
tail -n 80 /data/zjr/enterprise_kb_agent/artifacts/crm_short_checks_20261113/v4_supervisor_dev_20261113_r1/queue.log
bash scripts/crm_bench_run.sh small stop --tag v4_supervisor_dev_20261113_r1
```

## 官方小测入口

首批仅4任务，每项一次，优先只运行多Agent。任务、初态、政策、规范工具和官方评分器沿用冻结上游。主管只接收公开政策和用户可见对话，不读取任务答案。

首轮 `v4_supervisor_official4_20261113_r1` 已在辅助模型启动阶段失败，0/4任务执行。原队列PID `3814441`已退出，本次两服务资源已释放。预检时GPU 3有19698MiB可用；CUDA初始化时只有9.17GiB，低于75%预算所需17.67GiB。只读UUID诊断确认CUDA和nvidia-smi编号一致，同时观察到该卡其他负载从约4.4GiB增长到约14.3GiB。失败原因是共享卡显存波动，原日志和协议保留。

首轮运行目录：[双卡启动失败现场](/data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/v4_supervisor_official4_20261113_r1/)，[辅助模型日志](/data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/v4_supervisor_official4_20261113_r1/auxiliary-model.log)。

随后运行 `v4_supervisor_official4_20261113_single_r2`，原队列PID `3872104`，使用GPU 2，显存预算78%（约18.72GiB），总时限24分钟。该轮已经结束并释放资源，2通过、2因本地模拟审核器误判而未知。回答、用户模拟、评分共用Qwen3-8B权重，分别记录上下文和用量；协议、每题记录及汇总都标记 `independent_actor_and_judge_weights=false`，报告明确显示非独立开发诊断。规范工具、状态、保护状态、动作、参数和顺序检查保持原样。

历史启动核验：仅actor服务（原PID `3872779`），GPU 3未用于该轮；模型加载成功并完成全部4条记录。

新增入口、选卡、模型租约复用、报告口径和禁止扩大默认任务集的定向验证21/21通过（3.44秒）。`--shared-eval-model`仅允许v6/v4、显式1至4个任务、一次重复、明确时限及全新运行，不能沿用该入口直接跑66任务或导入旧独立评测。

r2结果目录：[单卡四任务诊断](/data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/v4_supervisor_official4_20261113_single_r2/)。最新r3的实际效果以新目录中的 `report.md`、`summary.json`及逐例记录为准；独立模型评测入口仍保留。

```bash
bash scripts/crm_bench_run.sh launch \
  --harness-version v6 --execution-adapter v4 --arms multi_v6 \
  --shared-eval-model \
  --tag v4_supervisor_official4_20261113_native_r3 --gpus 2 --trials 1 --max-hours .4 \
  --task-ids itsm_ops_001_lookup_ticket_by_id,itsm_ops_005_add_comment_to_ticket,itsm_ops_012_comment_then_status_update_order,itsm_safety_007_safe_delete_admin_ticket1004

bash scripts/crm_bench_run.sh watch --tag v4_supervisor_official4_20261113_native_r3
tail -n 80 /data/zjr/enterprise_kb_agent/artifacts/crm_itsm_20261109/v4_supervisor_official4_20261113_native_r3/queue.log
bash scripts/crm_bench_run.sh stop --tag v4_supervisor_official4_20261113_native_r3
```

每次新运行使用独立tag，失败保留完整现场，不自动重启。小测通过核心业务合同、身份补充、实际越权/重复写入为0、用量完整后，才扩展到10任务；队列不自动扩到66任务。初期4项通过表示所测场景达标，广泛成功率仍需扩展任务检验。

此前的状态、数据与基线问题见[评测审计](EVALUATION_AUDIT_20261113.md)。
