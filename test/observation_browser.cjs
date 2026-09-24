/* Read-only Chrome check against real registered workspaces and cached metadata.
 * ORC_OBSERVATION_HOME=/operator/orc-home ORC_OBSERVATION_WORKSPACE=/workspace
 * NODE_PATH=/path/to/puppeteer/node_modules node test/observation_browser.cjs
 * No worker or model dispatch: the only form submissions are intercepted.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const puppeteer = require('puppeteer');
const root = path.resolve(__dirname, '..');
const output = fs.mkdtempSync(path.join(os.tmpdir(), 'orc-observation-browser-'));
const sourceHome = process.env.ORC_OBSERVATION_HOME;
const workspace = process.env.ORC_OBSERVATION_WORKSPACE;
assert(sourceHome && workspace, 'Choose explicit real observation sources');
const source = `
import json, os, pathlib, shutil, sys
from fusion_ui import ControlRoom, Server
target = pathlib.Path(sys.argv[1]); source = pathlib.Path(sys.argv[2])
for name in ('models.json', 'quality.json'):
    if (source / name).is_file(): shutil.copy2(source / name, target / name)
os.environ['ORC_HOME'] = str(target)
os.environ['FUSION_TELEMETRY'] = '0'
app = ControlRoom(pathlib.Path(sys.argv[3]), source / 'ui-workspaces.json')
server = Server(0, app)
server.service_actions = lambda: None  # Observation-only preview: no ambient garden/training jobs.
print(json.dumps({'origin':server.origin}), flush=True)
server.serve_forever()
`;
const server = spawn('python3', ['-c', source, output, sourceHome, workspace], {cwd:root, stdio:['ignore','pipe','pipe']});
let stderr = ''; server.stderr.on('data', x => {stderr += x});
async function main() {
  const origin = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(Error('Preview server timeout: '+stderr)), 10000);
    createInterface({input:server.stdout}).once('line', line => {clearTimeout(timer); resolve(JSON.parse(line).origin)});
    server.once('exit', code => reject(Error('Preview server exited '+code+': '+stderr)));
  });
  const token = fs.readFileSync(path.join(output,'ui-token'),'utf8').trim();
  const browser = await puppeteer.launch({headless:true, executablePath:process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  try {
    const page = await browser.newPage(), errors = [], external = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('request', r => {if (/^https?:/.test(r.url()) && !r.url().startsWith(origin)) external.push(r.url())});
    await page.setViewport({width:1440,height:1100});
    await page.goto(origin+'/#token='+encodeURIComponent(token),{waitUntil:'networkidle2'});
    await page.waitForSelector('.workspace-activity-card');
    const current = await page.$eval('#workspace', el=>el.value);
    const other = await page.$eval('.workspace-activity-grid', (el,current)=>{const b=[...el.querySelectorAll('button')].find(b=>b.dataset.workspace!==current);return b ? {...b.dataset} : null}, current);
    assert(other, 'Real registered work in another workspace must be visible');
    await page.screenshot({path:path.join(output,'overview-desktop.png'),fullPage:false});
    await page.click(`.workspace-activity-card[data-workspace="${other.workspace}"][data-id="${other.id}"]`);
    await page.waitForFunction((id)=>document.querySelector('#workspace').value===id && location.hash.includes('w='+id), {}, other.workspace);
    await page.reload({waitUntil:'networkidle2'});
    assert.equal(await page.$eval('#workspace',el=>el.value),other.workspace);
    assert(new URLSearchParams((await page.evaluate(()=>location.hash)).split('?')[1]).get('w')===other.workspace);
    await page.evaluate(()=>document.querySelector('[data-view="models"]').click());
    await page.waitForSelector('.model-catalog-card');
    const beforeSearch = await page.$eval('#model-catalog-summary',el=>el.innerText);
    assert(beforeSearch.includes('PROVIDER CATALOG') && beforeSearch.includes('BENCHMARK SNAPSHOT'));
    for (const query of ['gpt-6-sol','claude-opus-5.5','claude-fable-5.1','gpt-6-astra']) {
      await page.$eval('#model-search',(el,query)=>{el.value=query;el.dispatchEvent(new Event('input',{bubbles:true}))},query);
      assert((await page.$eval('#model-catalog-results',el=>el.innerText)).includes(query),'Missing actual catalog result: '+query);
    }
    await page.waitForFunction(()=>document.querySelector('#model-catalog-results').innerText.includes('gpt-6-astra'));
    assert(!(await page.$eval('#model-catalog-results',el=>el.innerText)).includes('OR_ID'));
    await page.screenshot({path:path.join(output,'models-desktop.png'),fullPage:false});
    await page.setViewport({width:390,height:844});
    await page.screenshot({path:path.join(output,'models-mobile.png'),fullPage:true});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth), 'Mobile page must not overflow');
    // Test the read-only/write handoff distinction through the real form handler.
    const captured=[];
    await page.setRequestInterception(true);
    page.on('request', async request => {
      if (request.url().includes('/api/config')) {
        const res = await fetch(request.url(), {headers:{'X-Fusion-Token':token}});
        const config = await res.json(); config.admission={mode:'tenet',ready:true,executor:{model:'fixture-only'},context_paths:[]};
        return request.respond({status:200,contentType:'application/json',body:JSON.stringify(config)});
      }
      if (request.url().includes('/api/launch')) {
        captured.push(JSON.parse(request.postData()));
        return request.respond({status:400,contentType:'application/json',body:JSON.stringify({error:'Fixture intercepted; no dispatch'})});
      }
      return request.continue();
    });
    await page.setViewport({width:1440,height:1100});
    await page.reload({waitUntil:'networkidle2'});
    await page.click('#launch-top');
    await page.waitForSelector('#admitted-launch-form');
    await page.type('#admitted-task','Read-only fixture review');
    await page.click('#admitted-launch-form button[type="submit"]');
    await page.waitForFunction(()=>document.querySelector('#dialog-error')?.innerText.includes('Fixture intercepted'));
    assert.deepEqual(captured[0].spec.nodes[0].acceptance.required_handoff,['summary']);
    await page.click('#admitted-launch-form input[name="allow_write"]');
    await page.click('#admitted-launch-form button[type="submit"]');
    await new Promise(resolve=>setTimeout(resolve,150));
    assert.deepEqual(captured[1].spec.nodes[0].acceptance.required_handoff,['summary','tests']);
    await page.click('[data-action="close"]');
    await page.select('#workspace',current);
    await page.waitForFunction(id=>document.querySelector('#workspace').value===id,{},current);
    await page.evaluate(()=>document.querySelector('[data-view="truffle"]').click());
    await page.waitForSelector('[data-action="local-task"]');
    assert((await page.$eval('.room-readiness',el=>el.innerText)).includes('Local tasks use this workspace and do not need GitHub'));
    const repositoryChoices=await page.$$eval('.room-readiness [data-action="source-workspace"]', els=>els.map(el=>el.innerText));
    assert.equal(repositoryChoices.length,new Set(repositoryChoices).size);
    await page.screenshot({path:path.join(output,'github-local-task.png'),fullPage:false});
    await page.click('[data-action="local-task"]');
    await page.waitForSelector('#admitted-launch-form');
    assert.equal(errors.length,0,errors.join('\n')); assert.deepEqual(external,[]);
    const result={source_workspace:workspace,source_home:sourceHome,cross_workspace:other,workspace_survives_reload:true,model_searches:['gpt-6-astra','gpt-6-sol','claude-opus-5.5','claude-fable-5.1'],catalog_and_benchmark_separate:true,readonly_handoff:['summary'],write_handoff:['summary','tests'],local_task_without_github:true,repository_choices:repositoryChoices,page_errors:errors,external_requests:external};
    fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n');
    console.log(JSON.stringify({output,result},null,2));
  } finally {await browser.close()}
}
main().catch(e=>{console.error(e);process.exitCode=1}).finally(()=>server.kill('SIGTERM'));
