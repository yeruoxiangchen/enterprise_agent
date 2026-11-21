"""Browser acceptance: screenshot -> knowledge lookup -> member/admin ticket flow."""
import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--url', required=True)
    args = parser.parse_args(); root = args.output.resolve()
    if not root.is_relative_to('/data/zjr/enterprise_kb_agent'):
        parser.error('Browser artifacts must be on /data')
    output = root/'browser'; output.mkdir(exist_ok=True)
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/data/zjr/enterprise_kb_agent/cache/browsers'
    fonts = Path('/data/zjr/enterprise_kb_agent/cache/fonts/fonts.conf')
    if fonts.exists(): os.environ['FONTCONFIG_FILE'] = str(fonts)
    password = json.loads((root/'credentials.json').read_text())['password']
    errors, transitions, style_errors, streams = [], [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1000},
            extra_http_headers={'X-KB-Client': 'web'}, record_video_dir=str(output/'video'))
        context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: style_errors.append(message.text)
                if message.type == 'error' and 'Refused to apply inline style' in message.text else None)
        page.on('request', lambda r: streams.append(r.url) if '/events?' in r.url else None)
        def login(user):
            context.clear_cookies(); page.goto(args.url)
            expect(page.locator('#login-view')).to_be_visible()
            page.locator('#login-form input[name=username]').fill(user)
            page.locator('#login-form input[name=password]').fill(password)
            page.locator('#login-form button').click()
            expect(page.locator('#username')).to_have_text(user)
        login('member')
        existing_tickets = context.request.get(args.url+'/api/tickets').json()
        question = 'SSH 登录这台机器时应该先检查哪一项？'
        page.locator('#question').fill(question)
        page.locator('#incident-open').click()
        page.locator('#incident-file').set_input_files(root/'inputs/ssh.png')
        expect(page.locator('#incident-result')).to_be_visible(timeout=60000)
        expect(page.locator('#incident-progress')).to_contain_text('已上传')
        page.locator('#incident-image').evaluate('(image) => image.decode()')
        image_box = page.locator('#incident-image').bounding_box()
        regions_box = page.locator('#incident-regions').bounding_box()
        assert all(abs(image_box[key]-regions_box[key]) < 1 for key in ['x', 'y', 'width', 'height'])
        page.screenshot(path=str(output/'01-screenshot-evidence.png'))
        expect(page.locator('#incident-query')).to_have_value(question)
        expect(page.locator('#incident-reviewed')).not_to_be_checked()
        with page.expect_response(lambda r: r.url.endswith('/api/chat') and r.request.method == 'POST') as pending:
            page.locator('#incident-question').click()
        assert pending.value.status == 202, pending.value.text()
        run = pending.value.json()
        expect(page.locator('.incident-readout')).to_contain_text('Permission denied',timeout=60000)
        expect(page.locator('.message.user')).to_have_text(question)
        expect(page.locator('#send')).to_be_enabled(timeout=60000)
        lookup = context.request.get(args.url+'/api/runs/'+run['run_id']).json()
        assert lookup['status'] == 'answered' and lookup['citations'], lookup
        page.screenshot(path=str(output/'02-knowledge-lookup.png'))
        page.locator('#incident-open').click()
        page.locator('#incident-ticket-options > summary').click()
        expect(page.locator('#incident-fields')).to_contain_text('Permission denied',timeout=60000)
        page.locator('#incident-draft').click()
        expect(page.locator('#toast')).to_contain_text('请先核对')
        page.locator('#incident-reviewed').check()
        with page.expect_response(lambda r: '/api/incident-evidence/' in r.url and r.url.endswith('/draft')) as draft:
            page.locator('#incident-draft').click()
        assert draft.value.status == 201
        ticket = draft.value.json(); card = page.locator('[data-ticket-id="'+ticket['id']+'"]')
        expect(card).to_contain_text('待确认'); card.scroll_into_view_if_needed()
        page.screenshot(path=str(output/'03-reviewed-draft.png'))
        card.locator('[data-ticket-action=submit]').click()
        expect(card).to_contain_text('已提交'); transitions.append('submitted')
        login('admin'); page.locator('#ticket-nav').click()
        card = page.locator('[data-ticket-id="'+ticket['id']+'"]')
        card.locator('[data-ticket-action=accept]').click()
        expect(card).to_contain_text('处理中'); transitions.append('processing')
        card.locator('[data-ticket-note]').fill('已核对SSH公钥和账户权限，连接恢复。')
        card.locator('[data-ticket-action=resolve]').click()
        expect(card).to_contain_text('已解决'); transitions.append('resolved')
        card.scroll_into_view_if_needed(); page.screenshot(path=str(output/'04-admin-resolution.png'))
        login('member'); page.locator('#ticket-nav').click()
        card = page.locator('[data-ticket-id="'+ticket['id']+'"]')
        expect(card).to_contain_text('连接恢复'); card.scroll_into_view_if_needed()
        page.screenshot(path=str(output/'05-member-followup.png'))
        # The cropped screen shows the VPN client but omits its status code.
        fixture = context.new_page()
        fixture.set_content('<div style="padding:24px;font:44px sans-serif">VPN</div>')
        cropped = fixture.locator('div').screenshot()
        fixture.close()
        page.locator('#new-chat').click()
        page.locator('#incident-open').click()
        page.locator('#incident-file').set_input_files({'name':'cropped.png','mimeType':'image/png','buffer':cropped})
        expect(page.locator('#incident-result')).to_be_visible(timeout=60000)
        page.locator('#incident-query').fill('这个错误码应该怎样处理？')
        with page.expect_response(lambda r: r.url.endswith('/api/chat') and r.request.method == 'POST') as missing:
            page.locator('#incident-question').click()
        intake = missing.value.json()
        assert missing.value.status == 202 and intake['status'] == 'queued'
        expect(page.locator('#incident-followup-label')).to_contain_text('错误码',timeout=60000)
        page.reload()
        expect(page.locator('#incident-answer')).to_be_visible()
        expect(page.locator('#incident-followup-label')).to_contain_text('错误码')
        page.locator('#incident-answer').fill('HTTP 403')
        with page.expect_response(lambda r: r.url.endswith('/clarification') and r.request.method == 'POST') as corrected:
            page.locator('#send').click()
        assert corrected.value.status == 202 and corrected.value.json()['run_id'] == intake['run_id']
        expect(page.locator('.incident-readout')).to_contain_text('员工补充 · 错误码：HTTP 403',timeout=60000)
        expect(page.locator('#send')).to_be_enabled(timeout=60000)
        ident = corrected.value.json()['conversation_id']
        page.reload()
        expect(page.locator('[data-conversation="'+ident+'"]')).to_be_visible()
        expect(page.locator('.incident-readout')).to_contain_text('HTTP 403')
        assert len(context.request.get(args.url+'/api/tickets').json()) == len(existing_tickets) + 1
        assert any(run['run_id']+'/events?' in url for url in streams)
        assert not errors, errors
        assert not style_errors, style_errors
        context.tracing.stop(path=str(output/'trace.zip'))
        context.close(); browser.close()
    result = {'passed': True, 'page_errors': errors, 'lookup_run_id': run['run_id'],
              'ticket_id': ticket['id'], 'transitions': transitions,
              'scope': 'real browser and authenticated backend; CPU OCR and extractive knowledge lookup',
              'artifacts': str(output)}
    (output/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__': main()
