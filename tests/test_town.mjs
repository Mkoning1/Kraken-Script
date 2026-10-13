import {chromium} from 'playwright';
import assert from 'node:assert/strict';

const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
const errors=[];
try{
  const page=await browser.newPage({viewport:{width:1570,height:900},reducedMotion:'reduce'});
  page.on('pageerror',e=>errors.push(String(e)));
  page.on('console',m=>{if(m.type()==='error'&&/TypeError|ReferenceError|SyntaxError|Uncaught/i.test(m.text()))errors.push(m.text())});
  const res=await page.goto('http://127.0.0.1:8765/index.html',{waitUntil:'domcontentloaded'});
  assert.equal(res.status(),200,'homepage served');
  await page.waitForFunction(()=>document.getElementById('h-equity')?.textContent?.includes('€'),{timeout:30000});
  const equity=await page.locator('#h-equity').textContent();
  assert.match(equity,/€/,'real Kraken data displays a euro value');
  assert.equal(await page.locator('.place-label').count(),7,'seven working town buildings');
  // Clicking a building must open real information from the existing static feed.
  await page.locator('#label-treasury').click({force:true});
  assert.match(await page.locator('#detail-title').textContent(),/Schatkamer/);
  assert.match(await page.locator('#detail-content').textContent(),/Beschikbaar/);
  await page.locator('#label-guild').click({force:true});
  assert.match(await page.locator('#detail-title').textContent(),/Gildehuis/);
  await page.locator('#label-tower').click({force:true});
  assert.match(await page.locator('#detail-content').textContent(),/Max. positiegrootte/);
  await page.locator('#label-gate').click({force:true});
  assert.match(await page.locator('#detail-content').textContent(),/Markten gezien/);
  await page.locator('#home').click();
  assert.match(await page.locator('#detail-title').textContent(),/Raadshuis/);
  await page.screenshot({path:'/tmp/koopmansstad-desktop.png',fullPage:true});
  // A real canvas is required: the town is a game world, not a screenshot.
  assert.equal(await page.locator('#world canvas').count(),1,'interactive WebGL town canvas present');
  // No unhandled errors in the page.
  assert.deepEqual(errors,[],'browser has no runtime errors');

  const mobile=await browser.newPage({viewport:{width:390,height:844},reducedMotion:'reduce'});
  mobile.on('pageerror',e=>errors.push('mobile: '+String(e)));
  await mobile.goto('http://127.0.0.1:8765/index.html',{waitUntil:'domcontentloaded'});
  await mobile.waitForFunction(()=>document.getElementById('h-equity')?.textContent?.includes('€'),{timeout:30000});
  assert.equal(await mobile.locator('#label-market').count(),1);
  await mobile.locator('#label-market').click({force:true});
  assert.match(await mobile.locator('#detail-title').textContent(),/Marktplaats/);
  await mobile.screenshot({path:'/tmp/koopmansstad-mobile.png',fullPage:true});
  assert.deepEqual(errors,[],'mobile browser has no runtime errors');

  console.log('Koopmansstad: browser smoke tests passed, live data & 3D renderer OK');
}finally{
 await browser.close();
}
