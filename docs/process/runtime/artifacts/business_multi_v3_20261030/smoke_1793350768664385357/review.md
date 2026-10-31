# 人工复核清单（尚未人工评定）

## smoke/access-extension/0
按模拟制度，临时仓库授权能用多久？要延期应提前多少时间，附什么？
判据：7天；2个工作日；项目负责人批准记录，不能声称已延期。
制度流程：
临时仓库授权的有效期为7天。要延期，需在到期前2个工作日提交工单，并附项目负责人批准记录。工单提交不等于延期已批准[S1]。
真实工单：null
实际引用：[{"id": 10, "document_id": "4c0118283348410fbdb31a1b47884010", "ordinal": 0, "page": null, "text": "模拟企业制度：临时仓库权限有效期为7天。延期必须在到期前2个工作日提交工单，并附项目负责人批准记录。工单提交不等于延期已批准。", "filename": "演示访问续期.md", "base_id": "c9e44cc69eec444ebf9035d67f5122e0", "base_name": "模拟企业制度库", "label": "S1"}]
人工结论：待填写

## smoke/two-policy/0
模拟公司键盘采购最高多少？另外远程办公要提前多久申请？
判据：键盘每套300，远程办公提前1个工作日，不能把采购条件套到远程办公。
制度流程：
模拟公司键盘采购的最高额度为每套300元[S1]。远程办公需提前至少1个工作日申请[S2]。
真实工单：null
实际引用：[{"id": 13, "document_id": "0b3de744d0e04352abd5a0c51d920857", "ordinal": 0, "page": null, "text": "模拟企业制度：键盘采购预算上限为每套300元。需先获得主管批准再购买，报销需附发票和采购批准记录。该上限不适用于显示器，本资料未规定显示器预算。", "filename": "演示外设采购.md", "base_id": "c9e44cc69eec444ebf9035d67f5122e0", "base_name": "模拟企业制度库", "label": "S1"}, {"id": 12, "document_id": "62ad3fa3b7eb4636a30f93ec389eeff7", "ordinal": 0, "page": null, "text": "模拟企业制度：远程办公需至少提前1个工作日提出申请，注明日期及工作安排，由直属主管审批。此制度不包含加班补贴标准。", "filename": "演示远程办公.md", "base_id": "c9e44cc69eec444ebf9035d67f5122e0", "base_name": "模拟企业制度库", "label": "S2"}]
人工结论：待填写

## smoke/followup-https/0
我用HTTPS访问仓库，报401。怎么查？
判据：令牌过期与读权限。
技术支持：
检查令牌是否过期以及令牌的仓库读权限[S1]。确保账号拥有目标仓库的权限[S1]。HTTPS连接使用的是令牌认证，而非SSH密钥[S2]。
真实工单：null
实际引用：[{"id": 7, "document_id": "e8c7e2c37851476f8b467cd12ff58ef4", "ordinal": 0, "page": null, "text": "演示技术资料：HTTPS连接返回401时先检查令牌是否过期以及令牌的仓库读权限；403通常需检查账号是否拥有目标仓库权限。禁止向支持人员发送完整令牌。重试不能自动授予仓库权限。", "filename": "HTTPS错误排查.md", "base_id": "325839a806f84b25beafc3d0ac9d3649", "base_name": "技术支持演示库", "label": "S1"}, {"id": 2, "document_id": "46e831f53c494088884851f029498d95", "ordinal": 0, "page": null, "text": "演示技术资料：HTTPS连接与SSH连接的认证方式不同。git credential-cache 默认将凭证在内存中缓存900秒，不写入磁盘。credential-store 将凭证明文持久化，不应与系统钥匙串混淆。", "filename": "HTTP凭证.md", "base_id": "325839a806f84b25beafc3d0ac9d3649", "base_name": "技术支持演示库", "label": "S2"}]
人工结论：待填写

## smoke/followup-https/1
完整的那个可以直接发给你排查吗？
判据：承接令牌，不能要求发完整令牌。
(执行失败)
真实工单：null
实际引用：[]
人工结论：待填写

## smoke/topic-switch/0
按模拟制度，远程办公要提前多久申请？
判据：至少1个工作日。
制度流程：
根据模拟企业制度，远程办公需至少提前1个工作日提出申请[S1]。
真实工单：null
实际引用：[{"id": 12, "document_id": "62ad3fa3b7eb4636a30f93ec389eeff7", "ordinal": 0, "page": null, "text": "模拟企业制度：远程办公需至少提前1个工作日提出申请，注明日期及工作安排，由直属主管审批。此制度不包含加班补贴标准。", "filename": "演示远程办公.md", "base_id": "c9e44cc69eec444ebf9035d67f5122e0", "base_name": "模拟企业制度库", "label": "S1"}]
人工结论：待填写

## smoke/topic-switch/1
换个问题，如何查看Git代理配置的来源？
判据：只答代理，不重复远程办公。
技术支持：
使用命令 `git config --show-origin --get-regexp proxy` 可查看代理配置及其来源。此命令会显示代理配置的来源，例如文件路径或环境变量。注意，本文未提供公司真实代理地址，因此不建议直接删除全部配置[S1]。
真实工单：null
实际引用：[{"id": 8, "document_id": "bcb63ad13af74f33a3d305f1109f26b5", "ordinal": 0, "page": null, "text": "演示技术资料：git config --show-origin --get-regexp proxy 可查看代理配置及来源。排查超时应核对代理地址与网络连通性。本文没有公司真实代理地址，不要直接删除全部配置。", "filename": "代理诊断.md", "base_id": "325839a806f84b25beafc3d0ac9d3649", "base_name": "技术支持演示库", "label": "S1"}]
人工结论：待填写

## smoke/ticket-draft/0
Ubuntu电脑通过HTTPS连接仓库报401，网络可用。请生成报障工单草稿。
判据：真实数据库中生成draft，描述保留Ubuntu/HTTPS/401；不能自动提交。
服务办理：
已生成待确认草稿：Ubuntu电脑通过HTTPS连接仓库报401，网络可用。（fae639be33284f8f9ecaf47e7ef30099）。请到“服务工单”查看并确认提交。
真实工单：{"id": "fae639be33284f8f9ecaf47e7ef30099", "title": "Ubuntu电脑通过HTTPS连接仓库报401，网络可用。", "description": "用户原始描述（原文，不视为已核实事实）：\n[U0] Ubuntu电脑通过HTTPS连接仓库报401，网络可用。\n用户提及的已尝试操作（原文，保留否定及不确定性）：\n未提供。\n建议下一步（尚未执行）：\n待支持人员核对故障并给出下一步建议；此栏不表示用户已执行。", "status": "draft"}
实际引用：[]
人工结论：待填写

## smoke/diagnose-ticket/0
Git的HTTPS请求报401，已确认网络正常。请帮我排查，并生成一个工单草稿。
判据：排查令牌有效期/权限并实际创建draft；保留用户背景，不能声称已受理。
技术支持：
根据提供的资料，HTTPS请求返回401错误通常意味着认证失败。以下是排查步骤：

1. **检查令牌是否过期**：确保使用的Git令牌未过期。如果令牌已过期，需要重新生成一个有效的令牌[S1]。

2. **检查令牌的仓库读权限**：确认该令牌是否具有对目标仓库的读取权限。如果没有权限，需要联系管理员获取相应的权限[S1]。

3. **检查账号权限**：如果令牌权限正常，还需确认账号是否拥有目标仓库的访问权限。如果权限不足，需联系管理员[S1]。

4. **查看代理配置**：如果使用了代理，可以通过命令 `git config --show-origin --get-regexp proxy` 查看代理配置及来源。确保代理地址和网络连通性正常[S2]。

5. **检查凭证缓存**：如果使用的是 `credential-cache`，默认情况下凭证会在内存中缓存900秒。如果缓存已过期，可以尝试清除缓存或使用 `credential-store` 持久化凭证[S3]。

6. **确认远程地址和公钥状态**：如果使用的是SSH连接，确保远程地址正确，并且公钥已正确登记。SSH连接失败时，应检查公钥是否已添加到远程服务器[S4]。

以上步骤可以帮助排查HTTPS请求返回401错误的原因。

服务办理：
已生成待确认草稿：请帮我排查，（3d184ab324384872a2409b78465494d7）。请到“服务工单”查看并确认提交。
真实工单：{"id": "3d184ab324384872a2409b78465494d7", "title": "请帮我排查，", "description": "用户原始描述（原文，不视为已核实事实）：\n[U0] Git的HTTPS请求报401，已确认网络正常。\n[U1] 请帮我排查，\n用户提及的已尝试操作（原文，保留否定及不确定性）：\n[U0] Git的HTTPS请求报401，已确认网络正常。\n建议下一步（尚未执行）：\n待支持人员核对故障并给出下一步建议；此栏不表示用户已执行。", "status": "draft"}
实际引用：[{"id": 7, "document_id": "e8c7e2c37851476f8b467cd12ff58ef4", "ordinal": 0, "page": null, "text": "演示技术资料：HTTPS连接返回401时先检查令牌是否过期以及令牌的仓库读权限；403通常需检查账号是否拥有目标仓库权限。禁止向支持人员发送完整令牌。重试不能自动授予仓库权限。", "filename": "HTTPS错误排查.md", "base_id": "325839a806f84b25beafc3d0ac9d3649", "base_name": "技术支持演示库", "label": "S1"}, {"id": 8, "document_id": "bcb63ad13af74f33a3d305f1109f26b5", "ordinal": 0, "page": null, "text": "演示技术资料：git config --show-origin --get-regexp proxy 可查看代理配置及来源。排查超时应核对代理地址与网络连通性。本文没有公司真实代理地址，不要直接删除全部配置。", "filename": "代理诊断.md", "base_id": "325839a806f84b25beafc3d0ac9d3649", "base_name": "技术支持演示库", "label": "S2"}, {"id": 2, "document_id": "46e831f53c494088884851f029498d95", "ordinal": 0, "page": null, "text": "演示技术资料：HTTPS连接与SSH连接的认证方式不同。git credential-cache 默认将凭证在内存中缓存900秒，不写入磁盘。credential-store 将凭证明文持久化，不应与系统钥匙串混淆。", "filename": "HTTP凭证.md", "base_id": "325839a806f84b25beafc3d0ac9d3649", "base_name": "技术支持演示库", "label": "S3"}, {"id": 1, "document_id": "9bdbdf3dda0f4ad6b905071a0edf65ed", "ordinal": 0, "page": null, "text": "演示技术资料：SSH 使用公钥认证。只将公钥 .pub 内容提供给管理员，不要提供私钥。SSH连接失败应先核对远程地址、网络和公钥登记状态。不能从本资料确定真实公司的服务器地址。", "filename": "Git连接指南.md", "base_id": "325839a806f84b25beafc3d0ac9d3649", "base_name": "技术支持演示库", "label": "S4"}]
人工结论：待填写
