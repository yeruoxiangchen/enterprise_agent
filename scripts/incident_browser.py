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
        form = page.locator('#question-form')
        question_input = page.locator('#question')
        picker = page.locator('#incident-file')
        send = page.locator('#send')
        def send_message():
            with page.expect_response(lambda r: r.url.endswith('/api/chat') and r.request.method == 'POST') as pending:
                send.click()
            assert pending.value.status == 202, pending.value.text()
            request = pending.value.json()
            expect(form).to_have_attribute('aria-busy', 'false', timeout=60000)
            return request
        question = 'SSH 登录这台机器时应该先检查哪一项？'
        question_input.fill(question)
        with page.expect_file_chooser() as chooser:
            page.locator('#incident-open').click()
        chooser.value.set_files(root/'inputs/ssh.png')
        expect(question_input).to_have_value(question)
        preview = page.locator('#attachments img')
        preview.evaluate('(image) => image.decode()')
        expect(page.locator('dialog[open]')).to_have_count(0)
        page.screenshot(path=str(output/'01-screenshot-evidence.png'))
        # Failure after upload must keep the draft and reuse the uploaded file on retry.
        uploads = []
        page.on('request', lambda r: uploads.append(r.url) if '/api/incident-evidence?' in r.url else None)
        saved_runs = []
        def drop_receipt(route):
            accepted = route.fetch()
            assert accepted.status == 202, accepted.text()
            saved_runs.append(accepted.json()['run_id'])
            route.fulfill(status=503, content_type='application/json', body='{"detail":"暂时不可用"}')
        page.route('**/api/chat', drop_receipt, times=1)
        send.click()
        expect(page.locator('#toast')).to_contain_text('暂时不可用')
        expect(question_input).to_have_value(question)
        expect(preview).to_be_visible()
        run = send_message()
        assert len(uploads) == 1 and saved_runs == [run['run_id']]
        bubble = page.locator('.message.user').last
        expect(bubble).to_contain_text(question)
        expect(bubble.locator('.attachment-chip')).to_have_text('ssh.png')
        lookup = context.request.get(args.url+'/api/runs/'+run['run_id']).json()
        assert lookup['status'] == 'answered' and lookup['citations'], lookup
        assert len(context.request.get(args.url+'/api/tickets').json()) == len(existing_tickets)
        page.screenshot(path=str(output/'02-knowledge-lookup.png'))
        # This fixture has no LLM. Seed a reviewed draft through the supported API to
        # exercise the member/admin views. The native browser suite also creates it in chat.
        evidence = lookup['incident_inputs'][0]
        draft = context.request.post(args.url+'/api/incident-evidence/'+evidence['incident_id']+'/draft',
            data={'confirmed':True, 'digest':evidence['digest'], 'title':'SSH 连接失败',
                  'description':'Ubuntu SSH Permission denied，请核对账户权限。'})
        assert draft.status == 201, draft.text()
        ticket = draft.json()
        page.locator('#ticket-nav').click()
        card = page.locator('[data-ticket-id="'+ticket['id']+'"]')
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
        page.locator('#new-chat').click()
        expect(send).to_be_disabled()
        picker.set_input_files(root/'inputs/ssh.png')
        page.get_by_role('button', name='移除 ssh.png').click()
        expect(send).to_be_disabled()
        picker.set_input_files(root/'inputs/ssh.png')
        image_only = send_message()
        link = page.locator('.message.user .attachment-chip')
        expect(link).to_have_text('ssh.png')
        page.reload()
        expect(link).to_have_text('ssh.png')
        link.locator('img').evaluate('(image) => image.decode()')
        expect(page.locator('.message.user')).to_have_text('ssh.png')
        question_input.fill('HTTP 403 应该怎么处理？')
        followup = send_message()
        assert followup['conversation_id'] == image_only['conversation_id']
        expect(page.locator('.message.user').last).to_have_text('HTTP 403 应该怎么处理？')
        expect(page.locator('.message.user').last.locator('.attachment-chip')).to_have_count(0)
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
