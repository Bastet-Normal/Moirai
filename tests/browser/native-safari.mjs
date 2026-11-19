import { Builder, By, Key } from 'selenium-webdriver'
import safari from 'selenium-webdriver/safari.js'
import { readFile, writeFile, readdir, stat, unlink, mkdtemp, rm } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { createRequire } from 'node:module'
import { setTimeout as delay } from 'node:timers/promises'

const require = createRequire(import.meta.url)
const library = await readFile(require.resolve('@testing-library/dom/dist/@testing-library/dom.umd.min.js'), 'utf8')

export async function launchSafari() {
  const service = new safari.ServiceBuilder('/usr/bin/safaridriver').build()
  const scratch = await mkdtemp(join(tmpdir(),'moirai-safari-'))
  let driver
  try {
    driver = await new Builder().disableEnvironmentOverrides().usingServer(await service.start()).forBrowser('safari').build()
  } catch (err) { await service.kill(); throw err }

  async function newPage({viewport}) {
    let layoutWidth = viewport.width
    const [dx, dy] = await driver.executeScript('return [outerWidth-innerWidth,outerHeight-innerHeight]')
    await driver.manage().window().setRect({x:0,y:0,width:viewport.width+dx,height:viewport.height+dy})
    function selector(steps) {
      async function matches() {
        return driver.executeScript(function (steps) {
          let roots = [document.body]
          for (const {type, value, regexp, exact, level} of steps) {
            const name = regexp ? new RegExp(value) : value
            if (type === 'nth') { roots = roots.slice(value,value+1); continue }
            if (type === 'filter') { roots = roots.filter(el=>el.textContent.includes(value)); continue }
            if (type === 'first') { roots = roots.slice(0,1); continue }
            roots = roots.flatMap(root => {
              if (type === 'css') return [...(root === document.body ? document : root).querySelectorAll(value)]
              if (type === 'label') return TestingLibraryDom.queryAllByLabelText(root,name,{exact})
              if (type === 'text') return TestingLibraryDom.queryAllByText(root,name,{exact})
              return TestingLibraryDom.queryAllByRole(root,type,{name,level})
            })
          }
          return roots
        }, steps)
      }
      const first = async () => driver.wait(async () => (await matches())[0], 5000, JSON.stringify(steps))
      return {
        ...queries(steps),
        nth: value => selector([...steps,{type:'nth',value}]),
        filter: ({hasText}) => selector([...steps,{type:'filter',value:hasText}]),
        enabled: async () => (await first()).isEnabled(),
        focused: async () => driver.executeScript('return arguments[0] === document.activeElement',await first()),
        focus: async () => driver.executeScript('arguments[0].focus()',await first()),
        press: async key => driver.actions().sendKeys(Key[key === 'ArrowRight' ? 'ARROW_RIGHT' : key.toUpperCase()]).perform(),
        innerText: async () => (await first()).getProperty('innerText'),
        async selectOption(value) {
          await driver.executeScript("arguments[0].value=arguments[1]; arguments[0].dispatchEvent(new Event('change',{bubbles:true}))",await first(),value)
        }, first: () => selector([...steps,{type:'first'}]),
        async click() {
          const el = await first()
          await driver.wait(() => driver.executeScript(function(el) {
            el.scrollIntoView({block:'center',behavior:'instant'})
            const r=el.getBoundingClientRect()
            return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))
          },el),5000)
          await driver.executeScript("window.safariClickSeen = false; arguments[0].addEventListener('click',()=>window.safariClickSeen=true,{once:true,capture:true})",el)
          await el.click()
          if (!await driver.executeScript("return window.safariClickSeen")) {
            const current = (await matches())[0]
            if (current) await current.click()
          }
        },
        async fill(value) {
          const el = await first()
          await driver.executeScript("arguments[0].focus(); Object.getOwnPropertyDescriptor(Object.getPrototypeOf(arguments[0]),'value').set.call(arguments[0],arguments[1]); arguments[0].dispatchEvent(new Event('input',{bubbles:true})); arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",el,value)
        },
        async setInputFiles({name, mimeType, buffer}) {
          await driver.executeScript((input, name, type, encoded) => {
            const bytes = Uint8Array.from(atob(encoded), byte => byte.charCodeAt(0))
            const transfer = new DataTransfer()
            transfer.items.add(new File([bytes], name, {type}))
            input.files = transfer.files
            input.dispatchEvent(new Event('change', {bubbles: true}))
          }, await first(), name, mimeType, buffer.toString('base64'))
        },
        count: async () => (await matches()).length,
        textContent: async () => (await first()).getProperty('textContent'),
        allTextContents: async () => Promise.all((await matches()).map(el => el.getProperty('textContent'))),
        getAttribute: async name => (await first()).getAttribute(name),
        evaluate: async fn => driver.executeScript(fn, await first()),
        isVisible: async () => { const el=(await matches())[0]; return Boolean(el && await el.isDisplayed()) },
        async waitFor({state='visible',timeout=5000}={}) {
          await driver.wait(async () => {
            try {
              const el = (await matches())[0]
              return Boolean(el && await el.isDisplayed()) === (state !== 'hidden')
            } catch (error) {
              if (error.name !== 'StaleElementReferenceError') throw error
              return false
            }
          }, timeout, JSON.stringify(steps))
        }
      }
    }
    function queries(steps) {
      function append(type, name, exact=true, level) {
        return selector([...steps,{type,value:name instanceof RegExp ? name.source : name,regexp:name instanceof RegExp,exact,level}])
      }
      return { locator: css => append('css',css), getByRole: (role,{name,exact,level}={}) => append(role,name,exact,level), getByText: (text,opts) => append('text',text,opts?.exact), getByLabel: label => append('label',label) }
    }
    return {
      ...queries([]), evaluate: (fn,...args) => driver.executeScript(fn,...args),
      async goto(url) {
        const target = new URL(url,process.env.SAFARI_BASE_URL)
        await driver.switchTo().defaultContent()
        if (layoutWidth === 320) {
          await driver.get(new URL('/__layout_320',target).href)
          await driver.switchTo().frame(await driver.findElement(By.css('iframe')))
        } else {
          await driver.get(target.href)
        }
        await driver.executeScript(library)
        await driver.wait(() => driver.executeScript("return document.fonts.status === 'loaded'"), 10000)
        return fetch(target)
      },
      async setViewportSize({width,height}) {
        layoutWidth = width
        await driver.manage().window().setRect({x:0,y:0,width:width+dx,height:height+dy})
      },
      title: () => driver.getTitle(),
      request: {
        get: path => fetch(new URL(path,process.env.SAFARI_BASE_URL)),
        post: (path,{data}) => fetch(new URL(path,process.env.SAFARI_BASE_URL),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
      },
      screenshot: async ({path}) => writeFile(path, Buffer.from(await driver.takeScreenshot(),'base64')),
      waitForTimeout: delay,
      keyboard: { press: async key => key === 'Control+K' ? driver.actions().keyDown(Key.CONTROL).keyDown('k').keyUp('k').keyUp(Key.CONTROL).perform() : driver.actions().sendKeys(key==='Escape'?Key.ESCAPE:key).perform() },
      mouse: {click: (x,y) => driver.actions().move({x:Math.round(x),y:Math.round(y)}).click().perform()}
    }
  }
  return {newPage, async close() { try { await driver.quit() } finally { await service.kill(); await rm(scratch,{recursive:true,force:true}) } }}
}

// Safari saves downloads outside the WebDriver session. Only accept files created by this action.
export async function watchDownload(page, extension) {
  const prefix = 'moirai-test-' + randomUUID() + '-'
  await page.evaluate(token => {
    const proto = HTMLAnchorElement.prototype
    const descriptor = window.originalDownloadDescriptor ??= Object.getOwnPropertyDescriptor(proto,'download')
    const originalClick = window.originalAnchorClick ??= proto.click
    proto.click = function() {
      if (this.dataset.testDownloadPrefix && this.href.startsWith(location.origin + '/api/novel/')) {
        const url = new URL(this.href)
        url.searchParams.set('safariFixture',this.dataset.testDownloadPrefix)
        this.href = url.href
      }
      return originalClick.call(this)
    }
    Object.defineProperty(proto, 'download', {
      ...descriptor,
      set(filename) { this.dataset.testDownloadPrefix = token; descriptor.set.call(this, token + filename) }
    })
  }, prefix)
  const dir = join(homedir(), 'Downloads')
  return async () => {
    for (let n=0; n<200; n++) {
      const filename = (await readdir(dir)).find(name => name.startsWith(prefix) && name.endsWith(extension))
      if (filename && (await stat(join(dir,filename))).size > 0) {
        const file = join(dir,filename)
        return {name:filename.slice(prefix.length), async read() {try {return await readFile(file)} finally {await unlink(file)}}}
      }
      await delay(100)
    }
    throw new Error('Safari download did not finish: ' + extension)
  }
}
