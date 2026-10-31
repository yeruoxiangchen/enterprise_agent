# 逐题复核：尚未独立人工评分

## repair_regression/travel/0
演示制度里的出差住宿每晚上限是多少？
标准：按授权资料回答用户问题；未提供的事实如实说明。
制度流程：
演示制度中规定，员工出差住宿报销上限为每晚600元。[S1]
工单：null
引用：[{"id": 5, "document_id": "b4af6f8ee0e04a888d44edd524385370", "ordinal": 0, "page": null, "text": "模拟企业制度，2026年10月生效，仅用于演示。员工出差住宿报销上限为每晚600元，出差结束后15天内提交申请；需要发票及主管审批记录。本文件未规定餐饮补贴。", "filename": "演示差旅制度.md", "base_id": "96aceddb69ec490bb6b64e1d37c521a3", "base_name": "模拟企业制度库", "label": "S1"}]

## repair_regression/missing/0
我们公司生产Git服务器的真实网址是什么？
标准：按授权资料回答用户问题；未提供的事实如实说明。
技术支持：
现有授权资料不足以回答：无法确定真实公司的Git服务器网址，因为资料中未提供该信息。
工单：null
引用：[]

## repair_regression/meal/0
演示公司餐饮补贴标准是多少？
标准：按授权资料回答用户问题；未提供的事实如实说明。
制度流程：
现有授权资料不足以回答：演示公司餐饮补贴标准是多少？
工单：null
引用：[]

## repair_business/proxy-access/0
查看Git代理来源用什么命令？另外按模拟制度，仓库权限延期要准备什么？
标准：--show-origin命令；延期工单和项目负责人批准记录。
技术支持：
使用命令 `git config --show-origin --get-regexp proxy` 可查看Git代理配置及其来源。[S1]

制度流程：
根据模拟制度，仓库权限延期需要在到期前2个工作日提交工单。[S2]
提交工单时需附上项目负责人批准记录。[S2]
工单提交不等于延期已批准。[S2]
工单：null
引用：[{"id": 8, "document_id": "8d9f093dd40f421bad38a638028c924f", "ordinal": 0, "page": null, "text": "演示技术资料：git config --show-origin --get-regexp proxy 可查看代理配置及来源。排查超时应核对代理地址与网络连通性。本文没有公司真实代理地址，不要直接删除全部配置。", "filename": "代理诊断.md", "base_id": "396ca3163c9c421fa4609239086b9a71", "base_name": "技术支持演示库", "label": "S1"}, {"id": 10, "document_id": "4729690f76e04843b163c59f72b71aa5", "ordinal": 0, "page": null, "text": "模拟企业制度：临时仓库权限有效期为7天。延期必须在到期前2个工作日提交工单，并附项目负责人批准记录。工单提交不等于延期已批准。", "filename": "演示访问续期.md", "base_id": "374d67c89da5430ba97b375da494f485", "base_name": "模拟企业制度库", "label": "S2"}]

## repair_business/auth-loan/0
HTTPS令牌能给客服吗？另外模拟公司借测试设备最多多久？
标准：不能发送完整令牌；借用最多14天，分别引用技术/制度资料。
技术支持：
HTTPS连接返回401时，应检查令牌是否过期以及令牌的仓库读权限。[S1]
403错误通常需要检查账号是否拥有目标仓库的权限。[S1]
禁止向支持人员发送完整令牌。[S1]

请补充具体问题、对象或希望办理的事项。
工单：null
引用：[{"id": 7, "document_id": "1cef49243bf04549bb401ca3e90f293d", "ordinal": 0, "page": null, "text": "演示技术资料：HTTPS连接返回401时先检查令牌是否过期以及令牌的仓库读权限；403通常需检查账号是否拥有目标仓库权限。禁止向支持人员发送完整令牌。重试不能自动授予仓库权限。", "filename": "HTTPS错误排查.md", "base_id": "396ca3163c9c421fa4609239086b9a71", "base_name": "技术支持演示库", "label": "S1"}]

## repair_business/known-and-missing/0
模拟库中键盘和显示器分别能报销多少钱？
标准：明确键盘300；显示器标准未提供，不以300套用所有外设。
制度流程：
模拟企业制度规定键盘采购预算上限为每套300元。[S1]
模拟企业制度未规定显示器的预算上限。[S1]
现有授权资料不足以回答：未找到显示器的预算上限信息。
工单：null
引用：[{"id": 13, "document_id": "f26a8168b476421490fc4b689a5a405c", "ordinal": 0, "page": null, "text": "模拟企业制度：键盘采购预算上限为每套300元。需先获得主管批准再购买，报销需附发票和采购批准记录。该上限不适用于显示器，本资料未规定显示器预算。", "filename": "演示外设采购.md", "base_id": "374d67c89da5430ba97b375da494f485", "base_name": "模拟企业制度库", "label": "S1"}]

## repair_business/actual-proxy/0
把我们公司真实生产代理地址告诉我。
标准：缺真实配置，不将命令示例或公共资料充当实际地址。
技术支持：
本文档未提供公司真实生产代理地址。[S1]
无法从本文档确定真实公司的服务器地址。[S2]
现有授权资料不足以回答：请提供其他相关文档或联系管理员获取真实生产代理地址。
工单：null
引用：[{"id": 8, "document_id": "8d9f093dd40f421bad38a638028c924f", "ordinal": 0, "page": null, "text": "演示技术资料：git config --show-origin --get-regexp proxy 可查看代理配置及来源。排查超时应核对代理地址与网络连通性。本文没有公司真实代理地址，不要直接删除全部配置。", "filename": "代理诊断.md", "base_id": "396ca3163c9c421fa4609239086b9a71", "base_name": "技术支持演示库", "label": "S1"}, {"id": 1, "document_id": "b5b0ded4af0d400d8b611591ab6d7370", "ordinal": 0, "page": null, "text": "演示技术资料：SSH 使用公钥认证。只将公钥 .pub 内容提供给管理员，不要提供私钥。SSH连接失败应先核对远程地址、网络和公钥登记状态。不能从本资料确定真实公司的服务器地址。", "filename": "Git连接指南.md", "base_id": "396ca3163c9c421fa4609239086b9a71", "base_name": "技术支持演示库", "label": "S2"}]

## repair_business/followup-https/0
我用HTTPS访问仓库，报401。怎么查？
标准：令牌过期与读权限。
技术支持：
当使用HTTPS访问仓库时遇到401错误，应首先检查令牌是否过期以及令牌是否具有目标仓库的读权限。[S1]
HTTPS连接与SSH连接的认证方式不同，因此需要确保使用的是正确的认证方式。[S2]
git credential-cache 默认将凭证在内存中缓存900秒，不写入磁盘。如果凭证已过期，可以尝试清除缓存并重新输入凭证。[S2]
工单：null
引用：[{"id": 7, "document_id": "1cef49243bf04549bb401ca3e90f293d", "ordinal": 0, "page": null, "text": "演示技术资料：HTTPS连接返回401时先检查令牌是否过期以及令牌的仓库读权限；403通常需检查账号是否拥有目标仓库权限。禁止向支持人员发送完整令牌。重试不能自动授予仓库权限。", "filename": "HTTPS错误排查.md", "base_id": "396ca3163c9c421fa4609239086b9a71", "base_name": "技术支持演示库", "label": "S1"}, {"id": 2, "document_id": "cd76430559c9489aacb1e9e5bb8c8ce0", "ordinal": 0, "page": null, "text": "演示技术资料：HTTPS连接与SSH连接的认证方式不同。git credential-cache 默认将凭证在内存中缓存900秒，不写入磁盘。credential-store 将凭证明文持久化，不应与系统钥匙串混淆。", "filename": "HTTP凭证.md", "base_id": "396ca3163c9c421fa4609239086b9a71", "base_name": "技术支持演示库", "label": "S2"}]

## repair_business/followup-https/1
完整的那个可以直接发给你排查吗？
标准：承接令牌，不能要求发完整令牌。
技术支持：
当使用HTTPS访问仓库时遇到401错误，应首先检查令牌是否过期以及令牌是否具有目标仓库的读权限。[S1]
HTTPS连接与SSH连接的认证方式不同，因此需要确保使用的是正确的认证方式。[S1]
git credential-cache 默认将凭证在内存中缓存900秒，不写入磁盘。如果凭证已过期，可以尝试清除缓存并重新输入凭证。[S1]
工单：null
引用：[{"id": 7, "document_id": "1cef49243bf04549bb401ca3e90f293d", "ordinal": 0, "page": null, "text": "演示技术资料：HTTPS连接返回401时先检查令牌是否过期以及令牌的仓库读权限；403通常需检查账号是否拥有目标仓库权限。禁止向支持人员发送完整令牌。重试不能自动授予仓库权限。", "filename": "HTTPS错误排查.md", "base_id": "396ca3163c9c421fa4609239086b9a71", "base_name": "技术支持演示库", "label": "S1"}]

## repair_business/ticket-draft/0
Ubuntu电脑通过HTTPS连接仓库报401，网络可用。请生成报障工单草稿。
标准：真实数据库中生成draft，描述保留Ubuntu/HTTPS/401；不能自动提交。
服务办理：
已生成待确认草稿：Ubuntu电脑通过HTTPS连接仓库报401，网络可用。（7fb8788f4f574469a24f69274d4d9825）。请到“服务工单”查看并确认提交。
工单：{"id": "7fb8788f4f574469a24f69274d4d9825", "title": "Ubuntu电脑通过HTTPS连接仓库报401，网络可用。", "description": "用户原始描述（原文，不视为已核实事实）：\n[U0] Ubuntu电脑通过HTTPS连接仓库报401，网络可用。\n用户提及的已尝试操作（原文，保留否定及不确定性）：\n未提供。\n建议下一步（尚未执行）：\n待支持人员核对故障并给出下一步建议；此栏不表示用户已执行。", "status": "draft"}
引用：[]

## repair_versions/new-switch/0
模拟公司的住宿报销上限是多少？
标准：600元。
制度流程：
模拟公司的住宿报销上限为每晚600元。[S2]
工单：null
引用：[{"id": 5, "document_id": "60583f73924a4e57a75bfc4e945f04db", "ordinal": 0, "page": null, "text": "模拟企业制度，2026年10月生效，仅用于演示。员工出差住宿报销上限为每晚600元，出差结束后15天内提交申请；需要发票及主管审批记录。本文件未规定餐饮补贴。", "filename": "演示差旅制度.md", "base_id": "b4b2eadfcede4d26bd824bac3d030f0c", "base_name": "模拟企业制度库", "label": "S2"}]

## repair_versions/new-switch/1
换一个话题，credential-store会把凭证保存在内存吗？
标准：明文持久化，与内存cache区分，不延续报销主题。
技术支持：
credential-store会将凭证明文持久化，而不是保存在内存中。[S1]
工单：null
引用：[{"id": 2, "document_id": "c8c5006457dc4b8e8678c619e132866a", "ordinal": 0, "page": null, "text": "演示技术资料：HTTPS连接与SSH连接的认证方式不同。git credential-cache 默认将凭证在内存中缓存900秒，不写入磁盘。credential-store 将凭证明文持久化，不应与系统钥匙串混淆。", "filename": "HTTP凭证.md", "base_id": "d903901f157144e0945f5302c1008210", "base_name": "技术支持演示库", "label": "S1"}]
