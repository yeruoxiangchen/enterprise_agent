# 固定业务评估集：补充评测

原99轮回答复评；6个现有场景各追加2次运行，共14轮。原问题、判据、资料、旧结果不修改。
内容与引用使用已校准的独立分项提示；同家族不同权重模型评审，未经独立人工金标验证。
完整任务通过=内容正确且适用的执行/权限/确认提交/办理回执/草稿原文检查均通过。
争议的事实忠实性不发布分数；未知评分单列。引用支持对纯拒答等无需引用的答复按通过计算。
括号内为待审数；分母为已评分数。共享模型服务耗时不等于独占资源基准；没有记录回答Token用量，不估造Token或金额成本。

|阶段|正常返回|内容正确（待审）|引用支持（待审）|完整任务（待审）|服务检查|模型调用|P50秒|P95秒|
|---|---|---|---|---|---|---|---|---|
|multi_v3|20/23|20/23（0）|17/19（4）|20/23（0）|4/4|36|5.17|9.08|
|multi_v4|23/23|22/22（1）|20/23（0）|22/22（1）|4/4|29|14.64|26.22|
|new_acceptance|13/13|13/13（0）|10/13（0）|13/13（0）|4/4|19|9.87|24.54|
|regression|17/17|17/17（0）|16/17（0）|17/17（0）|1/1|22|14.02|25.81|
|repeat_1|7/7|7/7（0）|7/7（0）|7/7（0）|2/2|9|23.66|34.55|
|repeat_2|7/7|7/7（0）|7/7（0）|7/7（0）|2/2|9|17.29|34.31|
|single|23/23|23/23（0）|23/23（0）|22/23（0）|3/4|26|4.93|8.03|

## 同题配对：新版多Agent的完整任务结果
- 对比 single：{"win": 1, "loss": 0, "both_pass": 21, "both_fail": 0, "unknown": 1}
- 对比 multi_v3：{"win": 3, "loss": 0, "both_pass": 19, "both_fail": 0, "unknown": 1}

## 原样重复测试
固定6个场景，原运行加两次追加；每次隔离应用数据库。以下为场景全部轮次、全部3次运行是否通过。
- {"case_id": "https-401", "expected_turns": 3, "observed_turns": 3, "all_execution_pass": true, "all_task_pass": true}
- {"case_id": "proxy-access", "expected_turns": 3, "observed_turns": 3, "all_execution_pass": true, "all_task_pass": true}
- {"case_id": "known-and-missing", "expected_turns": 3, "observed_turns": 3, "all_execution_pass": true, "all_task_pass": true}
- {"case_id": "followup-https", "expected_turns": 6, "observed_turns": 6, "all_execution_pass": true, "all_task_pass": true}
- {"case_id": "diagnose-ticket", "expected_turns": 3, "observed_turns": 3, "all_execution_pass": true, "all_task_pass": true}
- {"case_id": "other-ticket", "expected_turns": 3, "observed_turns": 3, "all_execution_pass": true, "all_task_pass": true}

## 工程验收与缺失指标
原工程验收直接引用，不重复执行：/data/zjr/enterprise_kb_agent/artifacts/business_multi_v4_20261030/dev_v1/engineering/report.json
72次认证读取API、2个真实并发问答、浏览器草稿到解决流程已验收；不声称生产压测。
本批没有独立相关文档标注，不新增Recall/MRR数字，也不把此前其他数据集的检索成绩混入本表。

## 未通过或待复核记录
- regression/status/0: 内容=1，引用=0，完整任务=1
  correctness: 回答按授权资料如实提供了当前账号工单信息，包括工单ID、标题、状态和处理备注，符合rubric要求；未提供其他账号工单信息，也未虚构内容，如实说明权限范围。；citation_support: answer中包含知识断言'以下仅为当前账号自己的工单；不能查看其他账号工单'，但candidate_citations为空，无任何引用支持该断言。
- single/diagnose-ticket/0: 内容=1，引用=1，完整任务=0
  correctness: 回答正确识别了401错误的可能原因（令牌过期或权限不足），并根据rubric要求保留用户背景，未声称已受理。同时，明确要求用户提供必要信息以生成工单草稿，符合任务要求。未生成实际工单或执行工具动作是合理的，因为任务类型为'draft'，且用户未提供足够信息。；citation_support: 回答中提到的‘令牌过期或没有仓库的读权限’、‘检查令牌的有效性以及账号是否拥有目标仓库的权限’、‘确保没有向支持人员发送完整的令牌’等知识断言，均在引用[S1]中得到支持。引用内容明确对应这些断言，无虚构或无关引用。
- multi_v3/two-tech/0: 内容=1，引用=None，完整任务=1
  correctness: 回答准确覆盖了rubric中的两点要求：1) HTTPS报401时检查令牌过期和读权限，引用了HTTPS错误排查.md；2) Git代理配置及来源通过命令`git config --show-origin --get-regexp proxy`查询，引用了代理诊断.md。两项均有独立证据支持，符合rubric要求。；citation_support: answer_quote须逐字摘录实际答案
- multi_v3/proxy-access/0: 内容=0，引用=None，完整任务=0
  
- multi_v3/auth-loan/0: 内容=0，引用=None，完整任务=0
  
- multi_v3/actual-proxy/0: 内容=0，引用=None，完整任务=0
  
- multi_v3/ticket-draft/0: 内容=1，引用=0，完整任务=1
  correctness: 回答生成了符合要求的工单草稿，描述中保留了'Ubuntu/HTTPS/401'关键信息，且明确说明为草稿状态，未自动提交，符合rubric要求。；citation_support: answer中包含知识断言'Ubuntu电脑通过HTTPS连接仓库报401，网络可用'，但candidate_citations为空，无任何引用支持该断言。
- multi_v3/own-ticket/0: 内容=1，引用=0，完整任务=1
  correctness: 回答明确指出‘以下仅为当前账号自己的工单’，符合rubric中‘只展示当前账号预置工单，明确账号范围’的要求；且准确列出了当前账号下唯一工单的状态和备注，满足‘list’任务类型对必答点的完整呈现。；citation_support: answer中包含知识断言'以下仅为当前账号自己的工单；不能查看其他账号工单'，但candidate_citations为空，无任何引用支持该断言。
- multi_v4/two-tech/0: 内容=None，引用=1，完整任务=None
  correctness: answer_quote须逐字摘录实际答案；citation_support: answer中的每个知识断言均有对应引用，且引用内容支持其断言：
1. 'HTTPS报401时应检查令牌是否过期以及令牌的仓库读权限' 对应[S1]，S1内容明确提到'HTTPS连接返回401时先检查令牌是否过期以及令牌的仓库读权限'。
2. 'HTTPS连接与SSH连接的认证方式不同' 对应[S2]，S2内容提到'HTTPS连接与SSH连接的认证方式不同'。
3. '使用命令 git config --show-origin --get-regexp proxy 可查看代理配置及来源' 对应[S3]，S3内容明确提到'git config --show-origin --get-regexp proxy 可查看代理配置及来源'。
所有断言均有实际引用且引用内容支持断言。
- multi_v4/ticket-draft/0: 内容=1，引用=0，完整任务=1
  correctness: 回答生成了真实数据库中的草稿，描述保留了Ubuntu/HTTPS/401，且明确说明未自动提交，符合rubric要求。；citation_support: answer中包含知识断言'Ubuntu电脑通过HTTPS连接仓库报401，网络可用'，但candidate_citations为空，无任何引用支持该断言。
- multi_v4/own-ticket/0: 内容=1，引用=0，完整任务=1
  correctness: 回答明确指出仅展示当前账号工单，符合rubric要求；列出工单ID、标题、状态及处理备注，完整覆盖必答点。；citation_support: answer中包含知识断言'以下仅为当前账号自己的工单；不能查看其他账号工单'，但candidate_citations为空，无任何引用支持该断言。
- multi_v4/private-doc/0: 内容=1，引用=0，完整任务=1
  correctness: 回答正确拒绝了越权请求，明确指出无管理员权限无法获取内部发布口令，并建议联系管理员，符合rubric中禁止泄漏受限文档内容的要求，且未提供任何敏感信息。；citation_support: 知识断言'由于您没有管理员权限，无法直接获取内部发布口令'未被引用支持，虽然[S1]引用了SSH权限诊断内容，但该内容与'内部发布口令'无直接关联，无法支持该断言。
- new_acceptance/new-ticket-clarification/1: 内容=1，引用=0，完整任务=1
  correctness: 回答正确保存了用户原话'Windows电脑开机蓝屏，已经重启仍失败。'，生成了符合要求的草稿工单（ID: 74f2a424d860436abdd06bd9ffb63f00），并提示用户到‘服务工单’查看确认，完全符合rubric中‘沿用刚才创建草稿的请求，保存用户原话，不自动提交’的要求。；citation_support: 答案中包含知识断言'Windows电脑开机蓝屏，已经重启仍失败'，但未提供任何引用支持，且candidate_citations为空，无法验证该断言的真实性或来源。
- new_acceptance/new-no-invented-attempts/0: 内容=1，引用=0，完整任务=1
  correctness: 草稿标题和描述均保留了用户原始表述'还没检查账号权限'，未添加或暗示已检查权限，符合rubric要求；实际工单草稿内容与用户描述一致，且状态为'draft'，符合任务要求。；citation_support: 答案中包含知识断言'Linux访问仓库报403，还没检查账号权限'，但未提供任何引用支持，且candidate_citations为空，无法验证该断言的真实性或来源。
- new_acceptance/new-ticket-list/0: 内容=1，引用=0，完整任务=1
  correctness: 回答真实返回了本人预置工单，工单ID、标题、状态和处理备注均与actual_ticket中的数据一致，符合rubric要求。；citation_support: answer中包含知识断言'以下仅为当前账号自己的工单；不能查看其他账号工单'，但candidate_citations为空，无任何引用支持该断言。
