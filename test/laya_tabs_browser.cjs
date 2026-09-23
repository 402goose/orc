// Real local HTTP state and browser history; disposable workspace, no model calls.
const {chromium, expect} = require('@playwright/test');
const {spawn} = require('node:child_process');
const fs = require('node:fs'), path = require('node:path');
(async () => {
  const fixture = spawn('python3', ['-u', path.join(__dirname, 'ui_browser_fixture.py')], {env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
  let browser, page, stderr='';
  fixture.stderr.on('data', b=>stderr+=b);
  try {
    const info = await new Promise((resolve,reject)=>{
      let out=''; const timer=setTimeout(()=>reject(Error('Fixture timeout '+stderr)),20000);
      fixture.stdout.on('data',b=>{out+=b;const line=out.split('\n').find(l=>l.startsWith('{"url"'));if(line){clearTimeout(timer);resolve(JSON.parse(line));}});
      fixture.on('exit',c=>{clearTimeout(timer);reject(Error('Fixture exit '+c+' '+stderr));});
    });
    browser=await chromium.launch({headless:true});
    const context=await browser.newContext({viewport:{width:1440,height:1050}});
    page=await context.newPage();
    const errors=[]; context.on('page',p=>p.on('pageerror',e=>errors.push(e.message)));
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(info.url);
    await page.locator('nav [data-view=decisions]').click();
    const tab=name=>page.getByRole('tab',{name:new RegExp('^'+name)}).click();
    const selected=name=>expect(page.getByRole('tab',{name:new RegExp('^'+name)})).toHaveAttribute('aria-selected','true');
    await selected('Overview');
    await expect(page.getByRole('tab')).toHaveCount(6);
    await expect(page.locator('.garden-panel,.quality-panel,.impact-panel,.decision-list,.training-dashboard')).toHaveCount(0);
    await page.evaluate(()=>window.shallowSentinel='same document');
    let documents=0; page.on('request',r=>{if(r.isNavigationRequest())documents++;});
    for (const name of ['Review','Garden','Training','Quality','Results','Overview']) {
      await tab(name);
      await selected(name);
      expect(new URL(page.url()).hash).toMatch(new RegExp('^#decisions/'+name.toLowerCase()+'\\?w='));
      await expect(page.getByRole('tabpanel')).toHaveAttribute('data-lab-section',name.toLowerCase());
      expect(await page.evaluate(()=>window.shallowSentinel)).toBe('same document');
    }
    expect(documents).toBe(0);
    await tab('Review');
    await page.locator('.decision-row[data-id=fixture-decision]').click();
    await page.locator('#label-evidence').fill('Unsaved notes across lab tabs.');
    const decisionURL=page.url();
    await tab('Garden');
    await page.goBack();
    await selected('Review');
    await expect(page.locator('#label-evidence')).toHaveValue('Unsaved notes across lab tabs.');
    expect(page.url()).toBe(decisionURL);
    await page.goForward();
    await selected('Garden');
    await tab('Review');
    await expect(page.locator('#label-evidence')).toHaveValue('Unsaved notes across lab tabs.');
    await page.locator('[data-filter=needs_evidence]').click();
    await expect(page.locator('.decision-row')).toHaveCount(1);
    expect(page.url()).toContain('decision=abstained-review');
    expect(page.url()).toContain('filter=needs_evidence');
    const filteredURL=page.url();
    await page.reload();
    await selected('Review');
    await expect(page.locator('[data-filter=needs_evidence]')).toHaveAttribute('aria-pressed','true');
    await expect(page.locator('.decision-row.selected')).toHaveAttribute('data-id','abstained-review');
    expect(page.url()).toBe(filteredURL);
    // All sections reload in place, including an empty garden and training state.
    for (const name of ['Garden','Training','Quality','Results','Overview']) {
      await tab(name); const url=page.url(); await page.reload(); await selected(name); expect(page.url()).toBe(url);
    }
    // Roving keyboard focus remains valid after a data refresh.
    await page.getByRole('tab',{name:'Overview',exact:true}).focus();
    await page.keyboard.press('ArrowRight');
    await selected('Review');
    await expect(page.getByRole('tab',{name:/^Review/})).toBeFocused();
    await page.evaluate(()=>refresh(true));
    await expect(page.getByRole('tab',{name:/^Review/})).toBeFocused();
    await page.keyboard.press('End'); await selected('Results');
    await page.keyboard.press('Home'); await selected('Overview');
    // A copied link chooses its registered workspace over another remembered one.
    const second=path.join(info.root,'second-workspace'); fs.mkdirSync(second);
    await page.evaluate(async p=>{const r=await api('workspaces',{path:p});localStorage.setItem('fusion-workspace',r.id);},second);
    const bookmark=await context.newPage();
    await bookmark.goto(filteredURL);
    await expect(bookmark.locator('.decision-row.selected')).toHaveAttribute('data-id','abstained-review');
    expect(bookmark.url()).toBe(filteredURL);
    await bookmark.close();
    // Unsupported tabs and malformed segments fall back without crashing.
    const base=new URL(info.url).origin;
    await page.goto(base+'/#decisions/%E0%A4%A'); await selected('Overview');
    await page.goto(base+'/#decisions/unrecognized'); await selected('Overview');
    await page.goto(base+'/#decisions/review?decision=missing&filter=garbage');
    await expect(page.getByRole('heading',{name:'Decision unavailable.'})).toBeVisible();
    await page.locator('.decision-row[data-id=fixture-decision]').click();
    await expect(page.locator('#label-evidence')).toBeVisible();
    await page.selectOption('#label-approval','council');
    await expect(page.locator('#label-mode')).toHaveValue('council');
    await expect(page.locator('.council-members')).toBeVisible();
    await expect(page.getByRole('button',{name:'Run council & approve',exact:true})).toBeEnabled();
    // Mobile tab overflow stays inside the tab strip; every palette keeps its accent.
    for (const name of ['Garden','Training','Quality','Results','Overview','Review']) {
      await tab(name);
      await page.setViewportSize({width:390,height:844});
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    }
    await tab('Garden');
    await page.screenshot({path:'/tmp/orc-laya-tabs-mobile.png',fullPage:true});
    await page.setViewportSize({width:1440,height:1050});
    await page.evaluate(()=>ORCAppearance.set({theme:'ocean',mode:'light'}));
    await tab('Overview');
    await page.screenshot({path:'/tmp/orc-laya-tabs-light.png',fullPage:true});
    await page.evaluate(()=>ORCAppearance.set({theme:'sand',mode:'dark'}));
    await tab('Garden');
    await page.screenshot({path:'/tmp/orc-laya-tabs-dark.png',fullPage:true});
    for (const theme of await page.evaluate(()=>ORCAppearance.themes.map(t=>t.id))) {
      for (const mode of ['light','dark']) {
        await page.evaluate(p=>ORCAppearance.set(p),{theme,mode});
        expect(await page.locator('[role=tab][aria-selected=true]').evaluate(el=>getComputedStyle(el).borderBottomColor)).not.toBe('rgba(0, 0, 0, 0)');
      }
    }
    expect(errors).toEqual([]);
    console.log('Laya tabs passed: six focused views, shallow links, all reloads, Back/Forward, decision/filter/workspace bookmarks, unsaved edits, keyboard/focus, malformed routes, mobile and all 20 theme/mode combinations.');
  } catch(e) { if(page)await page.screenshot({path:'/tmp/orc-laya-tabs-failure.png',fullPage:true}).catch(()=>{}); throw e; }
  finally {if(browser)await browser.close();fixture.kill('SIGINT');await new Promise(resolve=>{if(fixture.exitCode!==null)return resolve();fixture.once('exit',resolve);setTimeout(resolve,10000).unref();});if(stderr)process.stderr.write(stderr);}
})().catch(e=>{console.error(e);process.exitCode=1;});
