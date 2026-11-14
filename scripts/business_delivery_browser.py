"""Browser-only member/admin/member acceptance against an isolated v5 app."""
import json
import os
from pathlib import Path
import sys
import time

from playwright.sync_api import sync_playwright, expect


def main():
    target = Path(sys.argv[1])
    url = sys.argv[2]
    root = Path('/data/zjr/enterprise_kb_agent')
    out = target/'browser'
    out.mkdir(parents=True, exist_ok=True)
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(root/'cache/browsers')
    if (root/'cache/fonts/fonts.conf').exists():
        os.environ['FONTCONFIG_FILE'] = str(root/'cache/fonts/fonts.conf')
    credentials = dict(line.split('：', 1) for line in (target/'app/credentials.txt').read_text().splitlines())
    errors, runs, transitions = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                                      record_video_dir=str(out/'video'), record_video_size={'width': 1280, 'height': 900})
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        context.tracing.start(screenshots=True, snapshots=True)

        def login(username):
            page.goto(url)
            page.locator('#login-form input[name=username]').fill(username)
            page.locator('#login-form input[name=password]').fill(credentials['密码'])
            page.locator('#login-form button').click()
            expect(page.locator('#mode-badge')).to_have_text('多 Agent 企业服务')

        def ask(question):
            page.locator('#question').fill(question)
            with page.expect_response(lambda r: r.url.endswith('/api/chat') and r.request.method == 'POST') as response:
                page.locator('#send').click()
            assert response.value.status == 202
            saved = response.value.json()
            expect(page.locator('#send')).to_be_enabled(timeout=300000)
            # DOM completion and API state must refer to this exact invocation.
            run_response = context.request.get(url+'/api/runs/'+saved['run_id'])
            assert run_response.ok
            row = run_response.json()
            assert row['status'] in ('answered', 'insufficient', 'clarify'), row.get('error')
            assert row['answer']
            runs.append(row)
            return row

        def tickets():
            response = context.request.get(url+'/api/tickets')
            assert response.ok
            return response.json()

        def action(card, action_name, expected_status):
            with page.expect_response(lambda r: '/action' in r.url and r.request.method == 'POST') as response:
                card.locator(f'[data-ticket-action={action_name}]').click()
            assert response.value.ok, response.value.text()
            saved = response.value.json()
            assert saved['status'] == expected_status, saved
            transitions.append({'action': action_name, 'status': saved['status'], 'version': saved['version']})
            return saved

        try:
            login('demo-member')
            first = ask('我的Windows电脑SSH连接提示Permission denied，已经检查网络。先告诉我演示制度要求报修提供哪些信息，不要生成工单。')
            assert not tickets(), 'Consultation created a ticket'
            assert '资产' in first['answer'] and '时间' in first['answer']
            page.screenshot(path=str(out/'01_consultation.png'), full_page=True)
            second = ask('更正一下，实际使用Ubuntu，刚才把系统说错了。现在请把刚才的SSH连接故障生成工单草稿，资产编号是DEMO-V5-042，可联系时间是今天14:00。')
            draft, = tickets()
            assert draft['source_run'] == second['id']
            assert draft['status'] == 'draft'
            assert 'Ubuntu' in draft['title'] and 'Windows' not in draft['title'], draft['title']
            for field in ('Ubuntu', 'SSH', 'Permission denied', 'DEMO-V5-042', '今天14:00'):
                assert field in draft['description'], field
            page.locator('[data-inspect]').last.click()
            expect(page.locator('#trace')).to_contain_text('服务办理')
            page.screenshot(path=str(out/'02_draft_trace.png'), full_page=True)
            ticket_id = draft['id']
            page.locator('#ticket-nav').click()
            card = page.locator(f'#tickets-list [data-ticket-id="{ticket_id}"]')
            expect(card).to_be_visible()
            action(card, 'submit', 'submitted')
            expect(card).to_contain_text('已提交')
            page.screenshot(path=str(out/'03_member_confirmed.png'), full_page=True)
            page.locator('#logout').click()
            expect(page.locator('#login-form')).to_be_visible()
            login('demo-admin')
            page.locator('#ticket-nav').click()
            card = page.locator(f'#tickets-list [data-ticket-id="{ticket_id}"]')
            action(card, 'accept', 'processing')
            expect(card).to_contain_text('处理中')
            card.locator('[data-ticket-note]').fill('演示处理记录：已模拟核对SSH公钥登记与权限，等待员工确认恢复。')
            action(card, 'resolve', 'resolved')
            expect(card).to_contain_text('已解决')
            page.screenshot(path=str(out/'04_admin_resolved.png'), full_page=True)
            page.locator('#logout').click()
            expect(page.locator('#login-form')).to_be_visible()
            login('demo-member')
            final = ask('查询我自己的工单进度。')
            assert ticket_id in final['answer']
            assert 'resolved' in final['answer'] or '已解决' in final['answer']
            resolved, = tickets()
            assert resolved['status'] == 'resolved'
            page.screenshot(path=str(out/'05_member_status.png'), full_page=True)
            assert not errors, errors
            report = {'status': 'passed', 'url': url, 'policy': 'business_multi_v5', 'ticket_id': ticket_id,
                      'run_ids': [r['id'] for r in runs], 'transitions': transitions,
                      'checks': ['member_consult_without_write', 'corrected_environment_in_actual_draft',
                                 'inline_asset_contact', 'trace', 'member_confirm', 'admin_accept',
                                 'admin_resolve', 'member_query_resolved_ticket'],
                      'page_errors': errors, 'at': time.time()}
            (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            (out/'runs.json').write_text(json.dumps(runs, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False), flush=True)
        finally:
            context.tracing.stop(path=str(out/'trace.zip'))
            context.close()
            browser.close()


if __name__ == '__main__':
    main()
