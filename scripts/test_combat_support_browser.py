"""Shared participant cards: all four support groups and summary-only activity."""
from playwright.sync_api import sync_playwright,expect
from scripts.test_combat_healing import km,event,healing,f


def main():
    mail=km()
    row=healing.enrich(mail,healing.collect(mail,[event(source=dict(name='Logi',character_id=201,ship='Basilisk'))]))[0]
    row.update(corporation=f.entities._entity_ref('corporation',300,'Logi Corp','LOGI'),
        alliance=f.entities._entity_ref('alliance',400,'Logi Alliance','ALLY'),
        coalition=dict(coalition_id=500,name='Logi Coalition',url='/coalition/500'))
    mail['recorded_activity']=dict(damage_hp=12345,healing_hp=6789,capacitor_gj=4321)
    context=dict(combat_debug=True,killmail=mail,error=None,healers=[row],batteries=[row],assists=[row],battery_assists=[row])
    renderer=f.load('public_page_renderer')
    with sync_playwright() as pw:
        browser=pw.chromium.launch(args=['--no-sandbox'])
        page=browser.new_page(viewport={'width':1920,'height':1080})
        page.route('https://images.evetech.net/**',lambda route:route.abort())
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.set_content(renderer.content('killmail_detail.html',context))
        expect(page.get_by_label('Victim recorded activity')).to_contain_text('12,345 HP')
        expect(page.get_by_label('Victim recorded activity')).to_contain_text('6,789 HP')
        expect(page.get_by_label('Victim recorded activity')).to_contain_text('4,321 GJ')
        for ident,unit,label in [('healers','HP','Top Healer'),('batteries','GJ','Top Battery'),('assists','HP','Top Assist'),('battery-assists','GJ','Top Battery Assist')]:
            panel=page.locator('[data-participants-panel]').filter(has=page.locator('#'+ident+'-list'))
            expect(panel).to_contain_text('100 '+unit)
            for mode in ['Corporation','Alliance','Coalition','Individual']:
                panel.get_by_role('button',name=mode,exact=True).click()
                expect(panel).to_contain_text('100 '+unit)
                expect(panel).to_contain_text(label)
                expect(page.locator('#attackers-list .attacker-row')).to_have_count(2)
        assert len(page.locator('[id]').evaluate_all('(nodes)=>nodes.map(n=>n.id)'))==len(set(page.locator('[id]').evaluate_all('(nodes)=>nodes.map(n=>n.id)')))
        activity=page.get_by_label('Victim recorded activity').bounding_box()
        fitting=page.locator('.victim-column > section').first.bounding_box()
        attacker=page.locator('.attackers-window').bounding_box()
        assist=page.locator('.assist-stack').bounding_box()
        assert activity['y']>=fitting['y']+fitting['height']
        assert assist['x']>=attacker['x']+attacker['width']
        page.screenshot(path='/tmp/eveosint-support-killmail-preview.png',full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth')<=390
        del mail['recorded_activity']
        page.set_content(renderer.content('killmail_detail.html',dict(combat_debug=True,killmail=mail,error=None,healers=[])))
        expect(page.get_by_label('Victim recorded activity')).to_have_count(0)
        expect(page.locator('.healers-window')).to_have_count(0)
        assert not errors,errors
        browser.close()
    print('Four support cards: existing grouping, HP/GJ, summary-only activity, mobile, absent without data: OK')


if __name__=='__main__':main()
