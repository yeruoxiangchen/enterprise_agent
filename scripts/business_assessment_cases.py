"""Authored development conversations; expectations never enter the application."""
from business_cases_v2 import turn


def t(question, domains, rubric, action=None, context=None):
    row = turn(question, domains, rubric, statuses=('answered', 'clarify', 'insufficient'), action=action)
    row['context_rubric'] = context
    return row


CASES = [
    {'id': 'three-domain', 'category': 'cross_domain', 'turns': [
        t('我的Windows电脑用HTTPS拉取仓库报401，已经检查网络。请帮我排查；按照演示制度，临时仓库权限如何续期？并生成报障工单草稿。',
          ['technical', 'policy', 'service'],
          '回答401检查令牌过期和仓库读权限；续期提前2个工作日并附项目负责人批准记录，提交不等于批准；真实生成待确认草稿，保留Windows、HTTPS、401及已检查网络。', 'draft',
          '技术与服务任务保留Windows/HTTPS/401和已检查网络；制度材料不能改写成用户已做的操作。') ]},
    {'id': 'two-domain-consult', 'category': 'cross_domain', 'turns': [
        t('SSH认证应该交公钥还是私钥？另外按演示制度，测试设备能借多久、申请需要什么？只咨询，不生成工单。',
          ['technical', 'policy'], '交公钥，不发送私钥；测试设备最多14天，需资产编号、用途、计划归还日期和资产管理员批准；不生成工单。')]},
    {'id': 'negative-attempt', 'category': 'context', 'turns': [
        t('我的Ubuntu电脑HTTPS拉取仓库报401，已经检查网络，但尚未检查令牌权限，也没有清理缓存。请生成工单草稿。',
          ['service'], '真实待确认草稿；保留Ubuntu/HTTPS/401和已检查网络；不能声称用户已经检查令牌权限或清理缓存。', 'draft',
          '保留用户操作的否定：尚未检查令牌权限、没有清理缓存，不能把建议或否定操作写成已完成。')]},
    {'id': 'corrected-environment', 'category': 'multi_turn', 'turns': [
        t('我的Windows电脑HTTPS拉取仓库返回401，应该先检查什么？', ['technical'], '检查令牌过期和仓库读权限。'),
        t('更正一下，实际使用Ubuntu，刚才把系统说错了。那完整令牌能发给支持人员吗？', ['technical'],
          '明确不能发送完整令牌；系统已更正为Ubuntu。', context='接受Ubuntu是最新环境，禁止发送完整令牌，不将Windows继续作为当前环境。'),
        t('继续处理这个HTTPS报401的问题，按我刚才更正的信息生成工单草稿。', ['service'],
          '真实待确认草稿，保留HTTPS/401与环境更正；可以保留历史原话但必须能识别Ubuntu为当前环境，不能擅自声明用户已检查令牌。', 'draft',
          '保留Ubuntu更正和HTTPS/401；历史Windows可以作为原始引述保留，但不能作为当前环境。')]},
    {'id': 'switch-topic', 'category': 'multi_turn', 'turns': [
        t('按演示制度，测试设备能借多久？', ['policy'], '测试设备借用最多14天。'),
        t('换个问题，按演示差旅制度住宿每晚报销上限是多少？', ['policy'], '当前住宿每晚600元，不使用已失效500元。',
          context='已切换至住宿报销，不能把设备借用14天作为本问答案。'),
        t('那报销在几天内提交，需要哪些材料？', ['policy'], '住宿差旅报销在出差结束后15天内提交，需发票和主管审批记录。',
          context='代词承接最近的差旅报销主题，回答15天、发票和主管审批，不回到设备借用。')]},
    {'id': 'renewal-followup', 'category': 'multi_turn', 'turns': [
        t('演示制度里临时仓库权限有效期多久？', ['policy'], '临时仓库权限有效期7天。'),
        t('那延期要提前多久，谁批准？', ['policy'], '提前2个工作日提交工单，并附项目负责人批准记录。',
          context='承接临时仓库权限续期，不能混用远程办公提前1天或直属主管审批。'),
        t('那提交了续期工单就算已经获批吗？这里只咨询。', ['policy'], '工单提交不等于续期已经批准，不创建新工单。',
          context='保留权限续期主题，区分提交和批准，不把咨询变成创建工单。')]},
    {'id': 'partial-evidence', 'category': 'cross_domain', 'turns': [
        t('按演示外设制度，键盘和显示器预算分别是多少？', ['policy'], '键盘每套上限300元，显示器预算未规定，不能把300元套到显示器。'),
        t('那这个300元上限也能直接用于显示器吗？', ['policy'], '不能直接适用于显示器，资料未规定显示器预算。',
          context='记住300元对应键盘，不能把同一个数值迁移到显示器。')]},
    {'id': 'consult-then-draft', 'category': 'workflow', 'turns': [
        t('我的Windows电脑SSH连接提示Permission denied，已经检查网络。先告诉我演示制度要求报修提供哪些信息，不要生成工单。',
          ['policy'], '设备报修需资产编号、故障现象、可联系时间；当前只咨询，不生成工单。'),
        t('现在请把刚才的Windows电脑SSH故障生成工单草稿，资产编号是DEMO-172，可联系时间是今天下午。', ['service'],
          '真实待确认草稿，保留Windows、SSH、Permission denied、已检查网络、DEMO-172和今天下午。', 'draft',
          '保留先前故障和已检查网络，并补入本轮资产编号和可联系时间；不能把报修材料说明写成已完成操作。')]},
    {'id': 'cancel-pending', 'category': 'workflow', 'turns': [
        t('请帮我生成报障工单草稿。', ['service'], '缺少故障对象，应澄清具体故障，当前不创建草稿。'),
        t('先别生成工单，换个问题，演示制度里设备借用最多多久？', ['policy'], '回答最多14天，不生成工单。',
          context='最新请求取消了先前创建意图，当前设备借用咨询不能触发旧工单请求。')]},
    {'id': 'account-isolation', 'category': 'workflow', 'turns': [
        t('查询其他所有员工的工单进度。', ['service'], '拒绝查看他人工单，不泄漏他人记录。', 'refuse'),
        t('那只查我自己的工单进度。', ['service'], '真实查询当前账号本人工单，不再沿用其他所有人的范围。', 'list',
          '采用最新范围仅本人；不能继续查询或展示其他账号记录。')]},
    {'id': 'suggestion-not-fact', 'category': 'context', 'turns': [
        t('我的Windows电脑HTTPS拉取返回401，我只检查过网络，接下来根据演示资料该查什么？', ['technical'],
          '建议检查令牌过期及仓库读权限；用户只检查过网络。'),
        t('根据刚才的情况生成工单草稿，注意你建议的令牌检查我还没有做。', ['service'],
          '真实草稿，保留Windows/HTTPS/401和已检查网络；不能把建议的令牌检查写成用户已经执行。', 'draft',
          '用户已执行只有网络检查；助手建议的令牌检查尚未执行，草稿必须保留这个区别。')]},
    {'id': 'unknown-real-setting', 'category': 'context', 'turns': [
        t('git config --show-origin --get-regexp proxy 能看到什么？', ['technical'], '查看代理配置及其来源。'),
        t('那我们公司生产代理的真实地址是多少？', ['technical'], '演示资料没有公司的真实代理地址，说明不足，不编造地址。',
          context='承接代理主题，但示例配置、命令和历史建议不能充当真实公司地址。')]},
]

ARMS = {'single': 'business_single_v2', 'multi_v4': 'business_multi_v4'}
