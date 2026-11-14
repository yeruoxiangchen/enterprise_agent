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
    errors, transitions, style_errors = [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1000},
            extra_http_headers={'X-KB-Client': 'web'}, record_video_dir=str(output/'video'))
        context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: style_errors.append(message.text)
                if message.type == 'error' and 'Refused to apply inline style' in message.text else None)
        def login(user):
            context.clear_cookies(); page.goto(args.url)
            expect(page.locator('#login-view')).to_be_visible()
            page.locator('#login-form input[name=username]').fill(user)
            page.locator('#login-form input[name=password]').fill(password)
            page.locator('#login-form button').click()
            expect(page.locator('#username')).to_have_text(user)
        login('member')
        page.locator('#incident-open').click()
        page.locator('#incident-file').set_input_files(root/'inputs/ssh.png')
        expect(page.locator('#incident-result')).to_be_visible(timeout=60000)
        expect(page.locator('#incident-fields')).to_contain_text('Permission denied')
        page.locator('#incident-image').evaluate('(image) => image.decode()')
        image_box = page.locator('#incident-image').bounding_box()
        regions_box = page.locator('#incident-regions').bounding_box()
        assert all(abs(image_box[key]-regions_box[key]) < 1 for key in ['x', 'y', 'width', 'height'])
        page.screenshot(path=str(output/'01-screenshot-evidence.png'))
        page.locator('#incident-reviewed').check()
        page.locator('#incident-question').click()
        expect(page.locator('#question')).to_have_value('请根据知识库帮助我排查以下故障：Ubuntu 24.04；SSH；Permission denied')
        with page.expect_response(lambda r: r.url.endswith('/api/chat') and r.request.method == 'POST') as pending:
            page.locator('#send').click()
        run = pending.value.json()
        expect(page.locator('#send')).to_be_enabled(timeout=60000)
        lookup = context.request.get(args.url+'/api/runs/'+run['run_id']).json()
        assert lookup['status'] == 'answered' and lookup['citations'], lookup
        page.screenshot(path=str(output/'02-knowledge-lookup.png'))
        page.locator('#incident-open').click()
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
