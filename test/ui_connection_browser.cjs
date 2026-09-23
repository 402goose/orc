// Connection recovery against a disposable local server; no real workers or credentials.
const {chromium, expect} = require('@playwright/test');
const {spawn} = require('node:child_process');
const path = require('node:path');
(async()=>{
 const fixture=spawn('python3',['-u',path.join(__dirname,'ui_browser_fixture.py')],{env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
 let stderr='',browser;
 fixture.stderr.on('data',b=>stderr+=b);
 try {
  const info=await new Promise((resolve,reject)=>{
   let output='';const timer=setTimeout(()=>reject(Error('Fixture timed out '+stderr)),20000);
   fixture.stdout.on('data',b=>{output+=b;const line=output.split('\n').find(l=>l.startsWith('{"url"'));if(line){clearTimeout(timer);resolve(JSON.parse(line));}});
   fixture.on('exit',c=>{clearTimeout(timer);reject(Error('Fixture exit '+c+' '+stderr));});
  });
  const origin=new URL(info.url).origin;
  browser=await chromium.launch({headless:true});
  const context=await browser.newContext();
  const errors=[];
  context.on('page',p=>p.on('pageerror',e=>errors.push(e.message)));
  const old=await context.newPage();
  await old.addInitScript(()=>sessionStorage.setItem('fusion-token','expired'));
  await old.goto(origin+'/#decisions');
  await expect(old.locator('#content')).toContainText('Connect to your control room.');
  await expect(old.locator('#connection')).toHaveText('reconnect');

  // A fresh authenticated tab recovers the stale tab, preserving its requested view.
  const fresh=await context.newPage();
  await fresh.goto(info.url);
  await expect(fresh.locator('#connection')).toHaveText('live');
  await expect(old.locator('.learning-dashboard')).toBeVisible();
  await expect(old.locator('#error-banner')).toBeHidden();
  expect(new URL(old.url()).hash).toBe('#decisions');
  const bookmark=await context.newPage();
  await bookmark.goto(origin+'/#workflows');
  await expect(bookmark.locator('#connection')).toHaveText('live');
  await expect(bookmark.locator('.run-row')).toHaveCount(1);

  // Reconnection must not discard label edits, change view, or replay a POST.
  await old.bringToFront();
  await old.locator('#label-evidence').fill('Keep my unsaved evidence');
  await old.evaluate(()=>{token='expired';state.authRequired=true;});
  await fresh.evaluate(()=>localStorage.removeItem('fusion-token'));
  await fresh.evaluate(()=>api('bootstrap'));
  await expect(old.locator('#connection')).toHaveText('live');
  await expect(old.locator('#label-evidence')).toHaveValue('Keep my unsaved evidence');
  expect(new URL(old.url()).hash).toBe('#decisions');
  let posts=0;
  await old.route('**/api/garden?*',route=>{posts++;return route.fulfill({status:401,json:{error:'Expired connection'}});});
  const result=await old.evaluate(async()=>{token='expired';try{await api('garden',{enabled:false});return 'unexpected success';}catch(e){return e.message;}});
  expect(result).toBe('Expired connection');expect(posts).toBe(1);
  await old.unroute('**/api/garden?*');
  await old.evaluate(()=>api('overview'));
  await expect(old.locator('#label-evidence')).toHaveValue('Keep my unsaved evidence');

  // Manual reconnect validates a local URL before storing it or contacting anything else.
  const manualContext=await browser.newContext();
  const manual=await manualContext.newPage();
  let external=0;
  await manual.route('**/*',route=>route.request().url().startsWith(origin)?route.continue():(external++,route.abort()));
  await manual.goto(origin+'/#decisions');
  await manual.locator('#content [data-action=reconnect]').click();
  await manual.locator('#reconnect-url').fill('https://example.invalid/#token=do-not-send');
  await manual.getByRole('button',{name:'Connect',exact:true}).click();
  await expect(manual.locator('#dialog-error')).toContainText('this local control room');
  expect(external).toBe(0);
  await manual.locator('#reconnect-url').fill(origin+'/#token=expired');
  await manual.getByRole('button',{name:'Connect',exact:true}).click();
  await expect(manual.locator('#dialog-error')).toContainText('has expired');
  expect(await manual.evaluate(()=>localStorage.getItem('fusion-token'))).toBeNull();
  await manual.locator('#reconnect-url').fill(info.url);
  await manual.getByRole('button',{name:'Connect',exact:true}).click();
  await expect(manual.getByRole('dialog')).not.toBeVisible();
  await expect(manual.locator('.learning-dashboard')).toBeVisible();

  // Pasting a new token into the same tab can be a hash-only navigation.
  const hashContext=await browser.newContext();
  const hashPage=await hashContext.newPage();
  await hashPage.goto(origin+'/#overview');
  await expect(hashPage.locator('#content')).toContainText('Connect to your control room.');
  await hashPage.goto(info.url);
  await expect(hashPage.locator('#connection')).toHaveText('live');
  await expect(hashPage.locator('#error-banner')).toBeHidden();
  expect(new URL(hashPage.url()).hash).toBe('#overview');
  expect(errors).toEqual([]);
  console.log('Connection recovery passed: stale/new tabs, bookmarks, unsaved edits, manual URL validation, hash-only reconnect, no mutation replay.');
 } finally {
  if(browser)await browser.close();
  fixture.kill('SIGINT');
  await new Promise(resolve=>{if(fixture.exitCode!==null)return resolve();fixture.once('exit',resolve);setTimeout(resolve,10000).unref();});
  if(stderr)process.stderr.write(stderr);
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
