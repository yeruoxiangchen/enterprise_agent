# Agent Pack 逐题归因与独立评分轴复核

原始结果只读；按已通过验证的内容/引用评分分别恢复。这里是助手审计，不是独立人工标注；不把缺失引用评分算正确。
固定RAG hooks__2-2 的自动评分理由自相矛盾，另列复核标记；以下仍保留原模型值，不能当人工最终结论。

|组别|内容对/错/未知|忠实性对/错/未知|
|---|---|---|
|fixed-rerank|24/10/0|27/2/5|
|official-agentpack|21/13/0|26/7/1|

配对结果：{'improved': 2, 'same': 27, 'regressed': 5}。
过滤检索 59 次，其中空结果 58 次。
失败标签：{'answer_without_citation': 6, 'action_marker_missing': 3, 'no_evidence_after_filtered_search': 22, 'tool_call_leaked_as_text': 3, 'wrong_despite_retrieved_text': 3, 'step_limit_backup': 5}。

|题目|配对变化|失败标签|
|---|---|---|
|first-time-setup__1-1|improved|answer_without_citation|
|first-time-setup__1-2|same|answer_without_citation|
|first-time-setup__2-1|regressed|action_marker_missing, no_evidence_after_filtered_search, tool_call_leaked_as_text|
|first-time-setup__2-2|regressed|no_evidence_after_filtered_search|
|first-time-setup__2-3|same|—|
|aliases__1-1|same|—|
|aliases__1-2|same|answer_without_citation, wrong_despite_retrieved_text|
|tagging__2-1|same|—|
|tagging__2-2|same|no_evidence_after_filtered_search|
|bundling__1-2|regressed|action_marker_missing, no_evidence_after_filtered_search, tool_call_leaked_as_text|
|hooks__2-2|same|answer_without_citation, wrong_despite_retrieved_text|
|hooks__2-4|same|no_evidence_after_filtered_search, step_limit_backup|
|multi-dev-ssh-store-1|same|answer_without_citation, wrong_despite_retrieved_text|
|multi-dev-ssh-store-3|same|no_evidence_after_filtered_search|
|unknown-1-1|same|no_evidence_after_filtered_search, step_limit_backup|
|unknown-1-2|same|no_evidence_after_filtered_search, step_limit_backup|
|unknown-1-3|same|no_evidence_after_filtered_search|
|unknown-1-4|same|—|
|unknown-2-1|same|no_evidence_after_filtered_search, step_limit_backup|
|unknown-2-2|same|no_evidence_after_filtered_search|
|unknown-2-3|same|no_evidence_after_filtered_search|
|unknown-2-4|same|no_evidence_after_filtered_search, step_limit_backup|
|clarify-1-1|same|no_evidence_after_filtered_search|
|clarify-1-2|same|no_evidence_after_filtered_search|
|clarify-1-3|same|no_evidence_after_filtered_search|
|clarify-1-4|regressed|no_evidence_after_filtered_search|
|acl-1-1|same|no_evidence_after_filtered_search|
|acl-1-2|same|no_evidence_after_filtered_search|
|acl-1-3|same|no_evidence_after_filtered_search|
|acl-1-4|same|no_evidence_after_filtered_search|
|injection-dev-1|improved|—|
|injection-dev-2|same|answer_without_citation|
|injection-dev-3|same|—|
|injection-dev-4|regressed|action_marker_missing, no_evidence_after_filtered_search, tool_call_leaked_as_text|
