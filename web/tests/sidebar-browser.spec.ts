import { expect, test, type Page } from "@playwright/test";

// WebKit automation can tap but cannot reproduce iOS's native momentum drag.
// Inject the browser's cancellation/movement signals to test our event lifecycle.
async function touch(page: Page, selector: string, type: string, y = 250) {
  await page.locator(selector).evaluate((element, { type, y }) => {
    const point = { identifier: 1, clientX: 100, clientY: y };
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperties(event, {
      touches: { value: type === "touchend" || type === "touchcancel" ? [] : [point] },
      changedTouches: { value: [point] },
    });
    element.dispatchEvent(event);
  }, { type, y });
}

test.beforeEach(async ({ page }) => {
  await page.goto("/tests/sidebar-browser.html");
  await expect(page.locator(".scard")).toHaveCount(40);
});

test("mobile activity opens at the bottom with newest reachable and older above", async ({ page }) => {
  const scroll = page.locator(".s-scroll");
  await expect(page.locator(".scard").last()).toHaveAttribute("data-session-id", "session-0");
  await expect(page.locator('[data-session-id="session-0"]')).toBeInViewport();
  await expect.poll(() => scroll.evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop)).toBeLessThan(2);
  await scroll.evaluate(el => { el.scrollTop -= 150; });
  const before = await page.locator('[data-session-id="session-1"]').boundingBox();
  await page.evaluate(() => (window as unknown as { __sidebar: { refresh(id: string): void } }).__sidebar.refresh("session-8"));
  await expect(page.locator('[data-session-id="session-8"]')).toContainText("运行");
  const after = await page.locator('[data-session-id="session-1"]').boundingBox();
  expect(Math.abs(after!.y - before!.y)).toBeLessThan(1);
});

test("cancelled sidebar touch cannot open a ghost long-press menu", async ({ page }) => {
  await touch(page, '[data-session-id="session-1"]', "touchstart");
  await touch(page, '[data-session-id="session-1"]', "touchcancel");
  await page.waitForTimeout(550);
  await expect(page.locator(".scard.lifting")).toHaveCount(0);
  await expect(page.locator(".card-menu")).toHaveCount(0);
  await page.locator('[data-session-id="session-1"]').dispatchEvent("click", { detail: 1 });
  await expect(page.getByTestId("selected")).toBeEmpty();
  await page.locator('[data-session-id="session-1"]').tap();
  await expect(page.getByTestId("selected")).toHaveText("session-1");
});

test("activity refresh preserves the row under the finger until sidebar reopens", async ({ page }) => {
  const card = page.locator('[data-session-id="session-1"]');
  const before = await card.boundingBox();
  expect(before).not.toBeNull();
  await touch(page, '[data-session-id="session-1"]', "touchstart");
  await page.evaluate(() => (window as unknown as {
    __sidebar: { refresh(id: string): void };
  }).__sidebar.refresh("session-8"));
  // Wait for the fresh running state, not an arbitrary list-update timeout.
  await expect(page.locator(".scard").filter({ hasText: "会话 8" })).toContainText("运行");
  const after = await card.boundingBox();
  expect(Math.abs(after!.y - before!.y)).toBeLessThan(1);
  await touch(page, '[data-session-id="session-1"]', "touchend");
  await page.touchscreen.tap(before!.x + 40, before!.y + before!.height / 2);
  await expect(page.getByTestId("selected")).toHaveText("session-1");
  await page.evaluate(() => (window as unknown as {
    __sidebar: { setOpen(open: boolean): void };
  }).__sidebar.setOpen(false));
  await expect(page.locator(".sessions")).not.toHaveClass(/show/);
  await page.evaluate(() => (window as unknown as {
    __sidebar: { setOpen(open: boolean): void };
  }).__sidebar.setOpen(true));
  await expect(page.locator(".scard").last()).toContainText("会话 8");
});

test("vertical drag does not turn its trailing click into a session selection", async ({ page }) => {
  await touch(page, '[data-session-id="session-1"]', "touchstart", 250);
  await touch(page, '[data-session-id="session-1"]', "touchmove", 170);
  await touch(page, '[data-session-id="session-1"]', "touchend", 170);
  await page.locator('[data-session-id="session-1"]').dispatchEvent("click", { detail: 1 });
  await expect(page.getByTestId("selected")).toBeEmpty();
  await page.waitForTimeout(650);
  await expect(page.locator(".scard.lifting")).toHaveCount(0);
});

test("rows loaded into an already open sidebar retain their positions on refresh", async ({ page }) => {
  await page.goto("/tests/sidebar-browser.html?empty");
  await expect(page.locator(".scard")).toHaveCount(0);
  await page.evaluate(() => (window as unknown as {
    __sidebar: { populate(): void };
  }).__sidebar.populate());
  await expect(page.locator(".scard")).toHaveCount(40);
  const before = await page.locator(".scard").last().boundingBox();
  await page.evaluate(() => (window as unknown as {
    __sidebar: { refresh(id: string): void; add(): void };
  }).__sidebar.refresh("session-8"));
  await expect(page.locator('[data-session-id="session-8"]')).toContainText("运行");
  await expect(page.locator(".scard").last()).toHaveAttribute("data-session-id", "session-0");
  await page.evaluate(() => (window as unknown as {
    __sidebar: { add(): void };
  }).__sidebar.add());
  await expect(page.locator(".scard")).toHaveCount(41);
  const after = await page.locator(".scard").last().boundingBox();
  // Allow native scroll/layout subpixel rounding, never a row-sized shift.
  expect(Math.abs(after!.y - before!.y)).toBeLessThan(2);
});

test("normal taps, long press actions and explicit pinning still work", async ({ page }) => {
  await touch(page, '[data-session-id="session-1"]', "touchstart");
  await page.waitForTimeout(650);
  await expect(page.locator(".scard.lifting")).toHaveCount(1);
  await touch(page, '[data-session-id="session-1"]', "touchend");
  await page.locator('[data-session-id="session-1"]').dispatchEvent("click", { detail: 1 });
  await expect(page.getByTestId("selected")).toBeEmpty();
  await page.getByRole("button", { name: "置顶", exact: true }).tap();
  await expect(page.locator(".scard").last()).toHaveAttribute("data-session-id", "session-1");
  await expect(page.locator(".scard.lifting")).toHaveCount(0);
  await page.locator('[data-session-id="session-2"] .scard-title').tap();
  await expect(page.getByTestId("selected")).toHaveText("session-2");
});

test("closing during a hold cannot resurrect its menu on the next opening", async ({ page }) => {
  await touch(page, '[data-session-id="session-1"]', "touchstart");
  await page.evaluate(() => (window as unknown as {
    __sidebar: { setOpen(open: boolean): void };
  }).__sidebar.setOpen(false));
  await page.waitForTimeout(650);
  await page.evaluate(() => (window as unknown as {
    __sidebar: { setOpen(open: boolean): void };
  }).__sidebar.setOpen(true));
  await expect(page.locator(".scard.lifting")).toHaveCount(0);
  await expect(page.locator(".card-menu")).toHaveCount(0);
});

test("native scroll without touchmove cancels a pending long press", async ({ page }) => {
  await touch(page, '[data-session-id="session-1"]', "touchstart");
  await page.locator(".s-scroll").evaluate((element) => { element.scrollTop = 120; });
  await page.waitForTimeout(650);
  await expect(page.locator(".scard.lifting")).toHaveCount(0);
  await expect(page.locator(".card-menu")).toHaveCount(0);
});

test("scrolling with a mouse does not suppress the next deliberate click", async ({ page }) => {
  await page.locator(".s-scroll").evaluate((element) => { element.scrollTop = 120; });
  await page.locator('[data-session-id="session-3"] .scard-title').click();
  await expect(page.getByTestId("selected")).toHaveText("session-3");
});

test("sidebar owns scrolling and hit targets match visible rows after viewport changes", async ({ page }) => {
  await expect.poll(() => page.locator(".sessions").evaluate((element) =>
    getComputedStyle(element).transform)).toBe("none");
  await expect(page.locator(".s-scroll")).toHaveAttribute("data-lock-horizontal-swipe", "true");
  const styles = await page.locator(".s-scroll").evaluate((element) => ({
    overscroll: getComputedStyle(element).overscrollBehaviorY,
    touchAction: getComputedStyle(element).touchAction,
    bodyPosition: getComputedStyle(document.body).position,
  }));
  expect(styles.overscroll).toBe("contain");
  expect(styles.touchAction).toContain("pan-y");
  expect(styles.bodyPosition).toBe("fixed");
  for (const height of [510, 740, 510, 844]) {
    await page.setViewportSize({ width: 390, height });
    const scroll = page.locator(".s-scroll");
    await scroll.evaluate((element) => { element.scrollTop = 600; });
    const down = await scroll.evaluate((element) => element.scrollTop);
    await scroll.evaluate((element) => { element.scrollTop -= 100; });
    expect(await scroll.evaluate((element) => element.scrollTop)).toBeLessThan(down);
    const target = await scroll.evaluate((element) => {
      const bounds = element.getBoundingClientRect();
      const card = [...element.querySelectorAll<HTMLElement>(".scard")].find((row) => {
        const r = row.getBoundingClientRect();
        return r.top > bounds.top + 5 && r.bottom < bounds.bottom - 5;
      })!;
      const r = card.getBoundingClientRect();
      const x = r.left + 40, y = r.top + r.height / 2;
      return {
        x, y, id: card.dataset.sessionId,
        hitId: document.elementFromPoint(x, y)?.closest<HTMLElement>(".scard")?.dataset.sessionId,
        rootScroll: window.scrollY,
      };
    });
    expect(target.rootScroll).toBe(0);
    expect(target.hitId).toBe(target.id);
    await page.touchscreen.tap(target.x, target.y);
    await expect(page.getByTestId("selected")).toHaveText(target.id!);
  }
});

test("chat and composer fit through keyboard-sized viewports before returning to sidebar", async ({ page }) => {
  await page.goto("/tests/sidebar-browser.html?chat");
  await expect(page.locator(".thread")).toBeVisible();
  const editor = page.locator(".composer textarea");
  await editor.fill("第一行\n第二行\n第三行\n第四行");
  for (const height of [510, 740, 844]) {
    await page.setViewportSize({ width: 390, height });
    await expect.poll(() => page.locator(".composer").evaluate((element) =>
      Math.round(element.getBoundingClientRect().bottom))).toBeLessThanOrEqual(height);
    expect(await page.evaluate(() => window.scrollY)).toBe(0);
    const thread = await page.locator(".thread").evaluate((element) => ({
      height: element.clientHeight, content: element.scrollHeight,
    }));
    expect(thread.height).toBeGreaterThan(100);
    expect(thread.content).toBeGreaterThan(thread.height);
  }
  await editor.blur();
  await page.evaluate(() => (window as unknown as {
    __sidebar: { setOpen(open: boolean): void };
  }).__sidebar.setOpen(true));
  await page.locator('[data-session-id="session-1"] .scard-title').tap();
  await expect(page.getByTestId("selected")).toHaveText("session-1");
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
});


test("mobile directory view opens at bottom with newest groups and rows below older ones", async ({page}) => {
  await page.getByRole("button", {name:"项目目录", exact:true}).tap();
  await expect(page.locator(".scard").last()).toHaveAttribute("data-session-id", "session-0");
  await expect(page.locator('[data-session-id="session-0"]')).toBeInViewport();
  await expect.poll(() => page.locator(".s-scroll").evaluate(el => el.scrollHeight-el.clientHeight-el.scrollTop)).toBeLessThan(2);
  const newestGroup = page.locator(".sgroup-wrap").last();
  await expect(newestGroup.locator(".scard").first()).toHaveAttribute("data-session-id", "session-3");
  await expect(newestGroup.locator(".scard").last()).toHaveAttribute("data-session-id", "session-0");
});


test("directory preview retains the five newest sessions before reversing them", async ({page}) => {
  await page.goto("/tests/sidebar-browser.html?dense");
  await page.getByRole("button",{name:"项目目录",exact:true}).tap();
  const group=page.locator(".sgroup-wrap").last();
  await expect(group.locator(".scard")).toHaveCount(5);
  await expect(group.locator(".scard").first()).toHaveAttribute("data-session-id","session-4");
  await expect(group.locator(".scard").last()).toHaveAttribute("data-session-id","session-0");
  await group.getByRole("button",{name:"展开其余 3 个会话",exact:true}).tap();
  await expect(group.locator(".scard")).toHaveCount(8);
  await expect(group.locator(".scard").first()).toHaveAttribute("data-session-id","session-7");
  await expect(group.locator(".scard").last()).toHaveAttribute("data-session-id","session-0");
});

test("large activity and directory lists mount a recent batch with correct hit targets", async ({page}) => {
  await page.goto("/tests/sidebar-browser.html?large");
  await expect(page.locator(".sidebar-window")).toHaveAttribute("data-windowed-count", "500");
  await expect(page.locator(".sidebar-window")).not.toHaveAttribute("data-measuring", "true");
  await expect(page.locator('[data-session-id="session-0"]')).toBeInViewport();
  await expect(page.locator(".scard")).toHaveCount(40);
  await expect(page.locator(".sidebar-window")).toHaveAttribute("data-mounted-count", "40");
  await expect.poll(() => page.locator(".s-scroll").evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop)).toBeLessThan(2);
  for (const directory of [false, true]) {
    if (directory) await page.getByRole("button", {name:"项目目录",exact:true}).tap();
    const scroll = page.locator(".s-scroll");
    await scroll.evaluate(el => { el.scrollTop = el.scrollHeight / 2; });
    const height = await scroll.evaluate(el => el.scrollHeight);
    const target = await scroll.evaluate(async el => {
      await new Promise(requestAnimationFrame);
      const bounds = el.getBoundingClientRect();
      const card = [...el.querySelectorAll<HTMLElement>(".scard")].find(row => {
        const box = row.getBoundingClientRect();
        return box.top > bounds.top + 4 && box.bottom < bounds.bottom - 4;
      })!;
      const box = card.getBoundingClientRect(), x = box.left + 30, y = box.top + 20;
      return { x, y, id: card.dataset.sessionId,
        hit: document.elementFromPoint(x,y)?.closest<HTMLElement>(".scard")?.dataset.sessionId };
    });
    expect(target.hit).toBe(target.id);
    expect(await scroll.evaluate(el => el.scrollHeight)).toBe(height);
    await page.touchscreen.tap(target.x,target.y);
    await expect(page.getByTestId("selected")).toHaveText(target.id!);
  }
  await page.getByRole("textbox", {name:"搜索会话",exact:true}).fill("会话 289");
  await expect(page.locator(".scard")).toHaveCount(1);
  await expect(page.locator('[data-session-id="session-289"]')).toBeInViewport();
});

test("background token rerenders never write the sidebar scroll position", async ({page}) => {
  const scroll = page.locator(".s-scroll");
  await scroll.evaluate(el => { el.scrollTop -= 150; });
  await page.evaluate(() => {
    const element = document.querySelector(".s-scroll")!;
    const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop")!;
    Object.assign(window, { __scrollWrites: 0 });
    Object.defineProperty(element, "scrollTop", {
      get() { return descriptor.get!.call(this); },
      set(value) {
        (window as unknown as { __scrollWrites: number }).__scrollWrites++;
        descriptor.set!.call(this, value);
      },
    });
  });
  for (let index = 0; index < 20; index++) {
    await page.evaluate(() => (window as unknown as {__sidebar:{tick():void}}).__sidebar.tick());
  }
  await expect(page.getByTestId("tick")).toHaveText("20");
  expect(await page.evaluate(() => (window as unknown as {__scrollWrites:number}).__scrollWrites)).toBe(0);
});

test("windowed sidebar preserves a reading row when an incoming session is prepended", async ({page}) => {
  await page.goto("/tests/sidebar-browser.html?large");
  await page.evaluate(() => Object.defineProperty(navigator, "clipboard", {configurable:true,
    value:{writeText:async(text:string)=>{(window as unknown as {copiedSessionId:string}).copiedSessionId=text;}}}));
  await expect(page.locator('[data-session-id="session-0"]')).toBeInViewport();
  const scroll = page.locator(".s-scroll");
  await expect.poll(() => scroll.evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop)).toBeLessThan(2);
  await scroll.evaluate(el => { el.scrollTop = el.scrollHeight / 2; });
  const anchor = await scroll.evaluate(async el => {
    // ResizeObserver measures the newly visible multi-line titles before a
    // reading anchor is meaningful. Wait for painted geometry to settle.
    let previous = "", stable = 0;
    for (let frame = 0; frame < 20; frame++) {
      await new Promise(requestAnimationFrame);
      const stamp = `${el.scrollTop}:${el.scrollHeight}`;
      stable = stamp === previous ? stable + 1 : 0;
      previous = stamp;
      if (stable >= 3) break;
    }
    const top = el.getBoundingClientRect().top;
    const row = [...el.querySelectorAll<HTMLElement>(".scard")].find(row => row.getBoundingClientRect().top > top)!;
    return {id: row.dataset.sessionId!, y: row.getBoundingClientRect().top};
  });
  await page.evaluate(() => (window as unknown as {__sidebar:{add():void}}).__sidebar.add());
  await expect(page.locator(".sidebar-window")).toHaveAttribute("data-windowed-count", "501");
  await expect.poll(async () => Math.abs((await page.locator(`[data-session-id="${anchor.id}"]`).boundingBox())!.y-anchor.y)).toBeLessThan(2);
  const card = page.locator(`[data-session-id="${anchor.id}"]`);
  await touch(page, `[data-session-id="${anchor.id}"]`, "touchstart");
  await expect(card).toHaveClass(/lifting/);
  await touch(page, `[data-session-id="${anchor.id}"]`, "touchend");
  await card.getByRole("button", {name:"复制 session ID", exact:true}).tap();
  await expect(card.getByRole("button", {name:"已复制",exact:true})).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as {copiedSessionId:string}).copiedSessionId)).toBe(anchor.id);
});

test("large activity loads older batches upward without moving the reading row", async ({page}) => {
  await page.goto("/tests/sidebar-browser.html?large");
  await expect(page.locator(".scard")).toHaveCount(40);
  const scroll=page.locator(".s-scroll");
  const reading=page.locator('[data-session-id="session-35"]');
  await scroll.evaluate(el=>{el.scrollTop=500;});
  const before=await reading.boundingBox();
  await expect(page.locator(".scard")).toHaveCount(80);
  const after=await reading.boundingBox();
  expect(Math.abs(after!.y-before!.y)).toBeLessThan(2);
  await expect(page.locator('[data-session-id="session-0"]')).toHaveCount(1);
  await page.getByRole("textbox",{name:"搜索会话",exact:true}).fill("会话 489");
  await expect(page.locator('[data-session-id="session-489"]')).toBeVisible();
});

test("sidebar execution status precedes current marker and activity time", async ({page}) => {
  await page.goto("/tests/sidebar-browser.html?current");
  await page.evaluate(() => (window as unknown as {__sidebar:{refresh(id:string):void}}).__sidebar.refresh("session-0"));
  const row=page.locator('[data-session-id="session-0"] .scard-status');
  await expect(row).toContainText('运行');
  const text=await row.innerText();
  expect(text.indexOf('运行')).toBeLessThan(text.indexOf('当前'));
});

for(const view of ['最近活跃','项目目录'])test(`large activity immediately reveals a newly created current chat in ${view}`,async({page})=>{
  await page.goto('/tests/sidebar-browser.html?large');
  await page.getByRole('button',{name:view,exact:true}).click();
  await page.locator('.s-scroll').evaluate(el=>{el.scrollTop=180;});
  await page.evaluate(()=>(window as unknown as {__sidebar:{create():void}}).__sidebar.create());
  await expect(page.locator('[data-session-id="created-active"]')).toBeInViewport();
  await expect(page.locator('[data-session-id="created-active"]')).toHaveClass(/active/);
  expect(await page.locator('.scard').count()).toBeLessThanOrEqual(40);
  await expect.poll(()=>page.locator('.s-scroll').evaluate(el=>el.scrollHeight-el.clientHeight-el.scrollTop)).toBeLessThan(2);
});
