// Real UI / HTTP / worker processes, with deterministic local fixtures only.
const {chromium,expect}=require('@playwright/test');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),path=require('node:path');
(async()=>{
  const fixture=spawn('python3',['-u',path.join(__dirname,'ui_browser_fixture.py')],{env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',FUSION_FIXTURE_COUNCIL:'1',FUSION_FIXTURE_AVAILABLE_COUNCIL:'1'}});
  let browser,stderr=''; fixture.stderr.on('data',b=>stderr+=b);
  try {
    const info=await new Promise((resolve,reject)=>{let out='';const timer=setTimeout(()=>reject(Error('Fixture timeout '+stderr)),20000);fixture.stdout.on('data',b=>{out+=b;const line=out.split('\n').find(l=>l.startsWith('{"url"'));if(line){clearTimeout(timer);resolve(JSON.parse(line));}});fixture.on('exit',c=>{clearTimeout(timer);reject(Error('Fixture exit '+c+' '+stderr));});});
    browser=await chromium.launch({headless:true});
    const page=await browser.newPage({viewport:{width:1440,height:1050}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(info.url);
    await page.locator('nav [data-view=decisions]').click();
    await page.getByRole('tab',{name:/^Garden/}).click();
    await page.getByRole('button',{name:'Enable auto-drafts',exact:true}).click();
    await page.locator('#garden-form [name=enabled]').check();
    await page.selectOption('#garden-approval','council');
    await page.selectOption('#garden-rule','available');
    await page.locator('#garden-form [name=include_existing]').check();
    await page.getByRole('button',{name:'Save garden settings'}).click();
    const live=page.locator('#garden-live-region');
    await expect(live).toContainText('Council approval complete',{timeout:25000});
    await expect(live).toContainText('3/3 finished · 2 assessments');
    await expect(live.locator('.live-member.unavailable')).toContainText('claude');
    await expect(live).toContainText('2 agreeing council members; 1 unavailable');
    await expect(page.locator('.garden-panel')).toContainText('Auto-approve · available members agree (minimum two)');
    const labels=fs.readFileSync(path.join(info.workspace,'.fusion/decisions/events.jsonl'),'utf8').trim().split('\n').map(JSON.parse).filter(e=>e.event==='label');
    expect(labels).toHaveLength(1);
    expect(labels[0].approval_rule).toBe('available');
    expect(labels[0].unavailable_members[0].failure_class).toBe('quota');
    await page.evaluate(()=>ORCAppearance.set({theme:'ocean',mode:'light'}));
    await live.screenshot({path:'/tmp/orc-available-council-light.png'});
    await page.evaluate(()=>ORCAppearance.set({theme:'forest',mode:'dark'}));
    await live.screenshot({path:'/tmp/orc-available-council-dark.png'});
    await page.setViewportSize({width:390,height:844});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.reload();
    await expect(live).toContainText('Council approval complete');
    await page.getByRole('button',{name:'Garden settings',exact:true}).click();
    await expect(page.locator('#garden-rule')).toHaveValue('available');
    expect(errors).toEqual([]);
    console.log('Available council browser passed: quota excluded, two evidence-backed votes auto-approved, audit trail, persisted policy, themes/mobile, reload.');
  } finally {if(browser)await browser.close();fixture.kill('SIGINT');await new Promise(resolve=>{if(fixture.exitCode!==null)return resolve();fixture.once('exit',resolve);setTimeout(resolve,10000).unref();});if(stderr)process.stderr.write(stderr);}
})().catch(e=>{console.error(e);process.exitCode=1;});
