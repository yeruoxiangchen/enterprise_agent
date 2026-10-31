"""Real browser acceptance on the dedicated demo, never the original 8768 app."""
import json
import os
from pathlib import Path
import time
import sys
from playwright.sync_api import sync_playwright, expect

ROOT=Path('/data/zjr/enterprise_kb_agent')
os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(ROOT/'cache/browsers')
if (ROOT/'cache/fonts/fonts.conf').exists():os.environ['FONTCONFIG_FILE']=str(ROOT/'cache/fonts/fonts.conf')
out=Path(os.environ.get('KB_BROWSER_OUTPUT',str(ROOT/'artifacts/business_multi_v1_20261030/browser')))
out.mkdir(parents=True,exist_ok=True)
credentials=dict(line.split('：',1) for line in Path(os.environ.get('KB_BROWSER_CREDENTIALS',str(ROOT/'preview_business_20261030/credentials.txt'))).read_text().splitlines())
url=os.environ.get('KB_BROWSER_URL','http://127.0.0.1:8772')
errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1440,'height':1000})
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(url)
    page.locator('#login-form input[name=username]').fill(credentials['账号'])
    page.locator('#login-form input[name=password]').fill(credentials['密码'])
    page.locator('#login-form button').click()
    expect(page.locator('#mode-badge')).to_have_text('多 Agent 企业服务')
    if '--capture-only' in sys.argv:
        page.locator('#ticket-nav').click()
        expect(page.locator('#tickets-list article').first).to_be_visible()
        page.screenshot(path=str(out/'tickets.png'),full_page=True)
        browser.close();print('截图已更新');raise SystemExit(0)
    page.locator('#question').fill('请生成报障工单草稿：浏览器联调电脑是Windows系统，开机出现蓝屏，已经重启仍然失败。')
    page.locator('#send').click()
    expect(page.locator('#send')).to_be_enabled(timeout=240000)
    expect(page.locator('.answer-body').last).to_contain_text('草稿')
    page.locator('[data-inspect]').last.click()
    expect(page.locator('#trace')).to_contain_text('服务办理')
    page.locator('#ticket-nav').click()
    card=page.locator('#tickets-list article').filter(has=page.locator('[data-ticket-action=submit]')).first
    expect(card).to_be_visible()
    ticket_id=card.get_attribute('data-ticket-id')
    card.locator('[data-ticket-action=submit]').click()
    card=page.locator(f'[data-ticket-id="{ticket_id}"]')
    expect(card).to_contain_text('已提交')
    card.locator('[data-ticket-action=accept]').click()
    expect(card).to_contain_text('处理中')
    card.locator('[data-ticket-note]').fill('浏览器验收：模拟登记处理结果。')
    card.locator('[data-ticket-action=resolve]').click()
    expect(card).to_contain_text('已解决')
    page.screenshot(path=str(out/'tickets.png'),full_page=True)
    browser.close()
assert not errors,errors
report={'status':'passed','url':url,'ticket_id':ticket_id,
        'checks':['login','business_badge','real_model_draft','trace','confirm','accept','resolve'],'page_errors':errors,'at':time.time()}
(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False))
