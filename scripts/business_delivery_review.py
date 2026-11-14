"""Read-only response replay with two explicitly documented wording corrections."""
import argparse
import hashlib
import json

import business_assessment as assessment
import business_ops as old
from business_delivery_contracts import fair_contracts, summarize
from business_delivery_v5 import BASE, REPAIR, folder, career_materials


def review(target, export=True):
    out = target/'wording_review_v2'
    out.mkdir(exist_ok=True)
    protocol = assessment.read(target/'protocol.json')
    rows = assessment.all_records(BASE, 'rows')+assessment.all_records(target, 'rows')
    paths = sorted((BASE/'rows').glob('*.json'))+sorted((target/'rows').glob('*.json'))
    declaration = {
        'scope': 'post-run development acceptance review; all responses reused, no model inference',
        'original_report': str(target/'comparison.json'), 'original_protocol': str(target/'protocol.json'),
        'original_protocol_sha256': hashlib.sha256((target/'protocol.json').read_bytes()).hexdigest(),
        'response_files': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        'changes': [
            '未清理缓存与没有清理缓存视为等价，均需出现在实际草稿描述。',
            '中性故障标题可接受；描述必须明确当前/实际Ubuntu，标题不能声称Windows。'],
        'contracts': fair_contracts(),
    }
    if (out/'protocol.json').exists() and assessment.read(out/'protocol.json') != declaration:
        raise RuntimeError('Wording review already frozen with different inputs')
    old.write(out/'protocol.json', declaration)
    result = summarize(protocol['cases'], ('single', 'multi_v4', 'multi_v5'), rows, contracts=declaration['contracts'])
    result['comparison_path'] = str(out/'comparison.json')
    result['wording_review'] = declaration['changes']
    result['reused_fault_checks'] = {'path': str(REPAIR/'faults/summary.json'),
                                    'summary': assessment.read(REPAIR/'faults/summary.json')}
    result['browser_demo'] = assessment.read(target/'demo/browser/report.json')
    result['generated_new_responses'] = 0
    old.write(out/'comparison.json', result)
    lines = ['# 知页 v5 交付验收复核', '',
             '12个原始开发场景、24轮/组；所有回答复用，三组统一重新评分。原协议、回答和报告完整保留。',
             '复核修正两项文案要求：未清理缓存与没有清理缓存等价；中性故障标题可接受，但描述需明确当前Ubuntu，标题不能声称Windows。',
             '场景通过要求其全部请求通过。这里统计业务契约验收，依据实际工单字段、状态、权限和规定知识点。', '',
             '|版本|场景契约|请求契约|正常返回|模型调用|输入/输出token|', '|---|---|---|---|---|---|']
    for arm, value in result['arms'].items():
        c, t, e = (value[k] for k in ('conversation_contracts', 'turn_contracts', 'execution'))
        lines.append(f'|{arm}|{c["passed"]}/{c["expected"]}|{t["passed"]}/{t["expected"]}|{e["passed"]}/{e["expected"]}|'
                     f'{value["model_calls"]}|{value["prompt_tokens"]}/{value["completion_tokens"]}|')
    lines += ['', '## 完整场景配对', json.dumps(result['paired_conversations'], ensure_ascii=False, indent=2),
              '', '## 工程验收', '既有同版本故障检查8/8：2次真实模型进程强退与恢复，2项脚本化模型故障，4项真实数据库保护检查。',
              '网页验收通过：普通员工咨询→更正环境并生成草稿→本人确认→管理员接单及解决→员工查询本人状态。',
              '网页run_id：'+', '.join(result['browser_demo']['run_ids']), '', '## 逐题未通过要求']
    lookup = {r['key']: r for r in rows}
    review_text = ['# v5对照逐题复核', '', '同义规则和来源散列见protocol.json。', '']
    for arm, details in result['details'].items():
        for checked in details:
            row = lookup.get(checked['key'])
            if checked['passed'] != 1:
                lines.append('- '+checked['key']+': '+json.dumps({k:v for k,v in checked['checks'].items() if v is not True}, ensure_ascii=False))
            if row:
                review_text += ['## '+row['key'], row['question'], 'rubric: '+row['rubric'], row['answer'] or '',
                                '实际工单：'+json.dumps(row['actual_tickets'], ensure_ascii=False),
                                '检查：'+json.dumps(checked['checks'], ensure_ascii=False), '']
    lines += ['', '## 使用口径', '本轮为开发场景验收和事后措辞复核，方法对照使用同一模型、文档、工具和最大预算。',
              '模型调用实际为28/32/31；延时受共享GPU环境影响，简历只写业务验收结果。',
              '报告数字用于说明已覆盖场景；新增业务和全面语义质量仍需相应数据。',
              '', '证据路径：', '- '+str(out/'protocol.json'), '- '+str(target/'demo/browser'), '- '+str(REPAIR/'faults/summary.json')]
    (out/'report.md').write_text('\n'.join(lines)+'\n')
    (out/'review.md').write_text('\n'.join(review_text)+'\n')
    if export:
        career_materials(target, result)
    print(json.dumps({'report': str(out/'report.md'), 'arms': result['arms'],
                      'resume_exported': export, 'generated_new_responses': 0}, ensure_ascii=False, indent=2), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', default='final_v1')
    parser.add_argument('--no-export', action='store_true')
    args = parser.parse_args()
    review(folder(args.tag), export=not args.no_export)
