"""Check the shared KM template: left healing card, attacker UI, small screens."""
from playwright.sync_api import sync_playwright, expect
from scripts.test_combat_healing import km, event, healing, f


def main():
    mail=km()
    events=[event(),event(2,kind='armor_in',amount=75,source=dict(name='Second Logi',character_id=202))]
    healers=healing.enrich(mail,healing.collect(mail,events))
    renderer=f.load('public_page_renderer')
    with sync_playwright() as pw:
        browser=pw.chromium.launch(args=['--no-sandbox'])
        page=browser.new_page(viewport={'width':1920,'height':1080})
        page.route('https://images.evetech.net/**',lambda route:route.abort())
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.set_content(renderer.content('killmail_detail.html',dict(killmail=mail,error=None,healers=healers)))
        expect(page.locator('.healer-row')).to_have_count(2)
        expect(page.locator('.healers-window')).to_contain_text('+ Healing')
        expect(page.locator('.healers-window')).to_contain_text('100 HP')
        healer=page.locator('.healers-window').bounding_box()
        victim=page.locator('.killmail-main-grid > section').nth(1).bounding_box()
        assert healer['x']+healer['width']<=victim['x'],(healer,victim)
        expect(page.locator('#attackers-list .attacker-row')).to_have_count(2)
        page.get_by_role('button',name='Corporation',exact=True).click()
        expect(page.locator('.healer-row')).to_have_count(2)
        page.get_by_role('button',name='Individual',exact=True).click()
        expect(page.locator('#attackers-list .attacker-row')).to_have_count(2)
        page.screenshot(path='/tmp/combat-healing-killmail-preview.png',full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        healer=page.locator('.healers-window').bounding_box()
        assert healer['x']>=0 and healer['x']+healer['width']<=390,healer
        page.set_content(renderer.content('killmail_detail.html',dict(killmail=mail,error=None,healers=[])))
        expect(page.locator('.healers-window')).to_have_count(0)
        expect(page.locator('#attackers-list .attacker-row')).to_have_count(2)
        assert not errors,errors
        browser.close()
    print('Browser: left healing card, English badge, HP, unchanged attacker grouping, mobile, absent without data: OK')


if __name__=='__main__':main()
