"""Actual browser: native screenshot diagnosis, draft, confirmation and resolution."""
import argparse
import json
import os
from pathlib import Path
import time

from playwright.sync_api import sync_playwright, expect


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--url',required=True)
    args=parser.parse_args();root=args.output.resolve()
    if not root.is_relative_to('/data/zjr/enterprise_kb_agent'):
        parser.error('Artifacts must be under /data/zjr/enterprise_kb_agent')
    output=root/'browser';output.mkdir(exist_ok=True)
    os.environ['PLAYWRIGHT_BROWSERS_PATH']='/data/zjr/enterprise_kb_agent/cache/browsers'
    fonts=Path('/data/zjr/enterprise_kb_agent/cache/fonts/fonts.conf')
    if fonts.exists():os.environ['FONTCONFIG_FILE']=str(fonts)
    password=json.loads((root/'credentials.json').read_text())['password']
    runs=[];transitions=[];errors=[];checks={}
    with sync_playwright()as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
        context=browser.new_context(viewport={'width':1440,'height':1000},
            extra_http_headers={'X-KB-Client':'web'},record_video_dir=str(output/'video'))
        context.tracing.start(screenshots=True,snapshots=True)
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        def login(user):
            context.clear_cookies();page.goto(args.url)
            page.locator('#login-form input[name=username]').fill(user)
            page.locator('#login-form input[name=password]').fill(password)
            page.locator('#login-form button').click()
            expect(page.locator('#username')).to_have_text(user)
            expect(page.locator('#mode-badge')).to_have_text('多 Agent 企业服务')
            me=context.request.get(args.url+'/api/me').json()
            assert me['agent_policy']=='business_native_v1'
        def ask(text):
            start=time.monotonic();page.locator('#question').fill(text)
            with page.expect_response(lambda r:r.url.endswith('/api/chat')and r.request.method=='POST')as pending:
                page.locator('#send').click()
            assert pending.value.status==202,pending.value.text()
            ident=pending.value.json()['run_id']
            expect(page.locator('#send')).to_be_enabled(timeout=240000)
            run=context.request.get(args.url+'/api/runs/'+ident).json()
            runs.append({**run,'browser_seconds':time.monotonic()-start})
            (output/'runs.json').write_text(json.dumps(runs,ensure_ascii=False,indent=2))
            assert run['status']in('answered','clarify'),run
            return run
        def tickets():
            r=context.request.get(args.url+'/api/tickets');assert r.ok
            return r.json()
        def action(card,name,status):
            with page.expect_response(lambda r:'/action'in r.url and r.request.method=='POST')as pending:
                card.locator('[data-ticket-action='+name+']').click()
            assert pending.value.ok,pending.value.text()
            result=pending.value.json();assert result['status']==status,result
            expect(card).to_contain_text({'submitted':'已提交','processing':'处理中','resolved':'已解决','cancelled':'已取消'}[status])
            transitions.append({'action':name,'ticket_id':result['id'],'status':status,'version':result['version']})
        try:
            login('member')
            page.locator('#incident-open').click()
            page.locator('#incident-file').set_input_files(root/'inputs/vpn.png')
            expect(page.locator('#incident-fields')).to_contain_text('401',timeout=60000)
            page.screenshot(path=str(output/'01_screenshot.png'),full_page=True)
            page.locator('#incident-reviewed').check();page.locator('#incident-question').click()
            consultation=ask('请先读取刚上传的故障截图，再查询VPN认证失败的处理建议，标注资料依据。先不要生成工单。')
            assert consultation['citations']and not tickets()
            assert any(e['kind']=='tool_effect'and e['payload']['name']=='read_incident'for e in consultation['events'])
            checks['screenshot_retrieval_without_unrequested_write']=True
            page.screenshot(path=str(output/'02_cited_diagnosis.png'),full_page=True)
            draft_run=ask('请根据刚才的VPN故障截图生成工单草稿，标题为VPN登录故障，保留系统、错误码和认证失败信息，等待我确认提交。')
            ticket,=tickets()
            assert ticket['source_run']==draft_run['id']and ticket['status']=='draft'
            assert all(s in ticket['description']for s in('Windows','401'))
            assert ('Authentication failed'in ticket['description']or'认证失败'in ticket['description'])
            checks['followup_preserves_attachment_and_creates_one_draft']=True
            page.locator('[data-inspect]').last.click()
            expect(page.locator('#trace')).to_contain_text('服务办理')
            page.screenshot(path=str(output/'03_native_draft.png'),full_page=True)
            page.locator('#ticket-nav').click()
            card=page.locator('[data-ticket-id="'+ticket['id']+'"]')
            action(card,'submit','submitted');checks['explicit_ui_confirmation']=True
            duplicate=context.request.post(args.url+'/api/tickets/'+ticket['id']+'/action',
                data={'action':'submit','version':ticket['version']})
            assert duplicate.ok and duplicate.json()['status']=='submitted'
            checks['duplicate_confirmation_idempotent']=True
            login('second');page.locator('#ticket-nav').click()
            assert not tickets()
            denied=context.request.post(args.url+'/api/tickets/'+ticket['id']+'/action',data={'action':'cancel','version':1})
            assert denied.status==404;checks['cross_account_ticket_isolation']=True
            login('admin');page.locator('#ticket-nav').click()
            card=page.locator('[data-ticket-id="'+ticket['id']+'"]')
            action(card,'accept','processing')
            card.locator('[data-ticket-note]').fill('已核对账户状态并更新VPN认证，连接恢复。')
            action(card,'resolve','resolved');checks['admin_accept_and_resolve']=True
            page.screenshot(path=str(output/'04_admin_resolution.png'),full_page=True)
            login('member')
            progress=ask('请查询我自己的服务工单处理进度，告诉我工单编号和处理结果。')
            assert ticket['id']in progress['answer']and ('已解决'in progress['answer']or'resolved'in progress['answer'].lower())
            checks['member_queries_actual_resolved_status']=True
            page.screenshot(path=str(output/'05_member_followup.png'),full_page=True)
            page.locator('#new-chat').click()
            cancelled=ask('请准备一个标题为取消演示的VPN故障工单草稿，描述为VPN无法连接，等待我确认。')
            draft=next(t for t in tickets()if t['source_run']==cancelled['id'])
            assert draft['status']=='draft'
            page.locator('#ticket-nav').click();card=page.locator('[data-ticket-id="'+draft['id']+'"]')
            action(card,'cancel','cancelled');checks['user_cancels_draft_without_submission']=True
            assert not errors,errors
            checks['no_browser_javascript_errors']=True
            report={'status':'passed','policy':'business_native_v1','checks':checks,
                'runs':len(runs),'transitions':transitions,'page_errors':errors,
                'scope':'four fixed development requests through a real browser and live local model; actual OCR, HTTP, DB and role changes'}
            (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(json.dumps(report,ensure_ascii=False),flush=True)
        finally:
            (output/'checks.json').write_text(json.dumps({'checks':checks,'errors':errors},ensure_ascii=False,indent=2))
            context.tracing.stop(path=str(output/'trace.zip'))
            context.close();browser.close()


if __name__=='__main__':main()
