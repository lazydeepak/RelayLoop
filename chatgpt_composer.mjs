/** Locate ChatGPT's editable composer inside the exact conversation form. */
export function plannerComposerFormSelector(conversationId) {
  if (!/^[A-Za-z0-9_-]+$/.test(conversationId || '')) {
    throw new Error('Planner conversation identity is invalid');
  }
  return `form[data-chatgpt-composer]:has([data-above-composer-conversation-id="chatgpt:${conversationId}"])`;
}

export function plannerComposerSelector(conversationId) {
  return `${plannerComposerFormSelector(conversationId)} [contenteditable="true"][role="textbox"]`;
}

/** Compare the complete payload while ignoring whitespace-only DOM layout differences. */
export function normalizePlannerText(value) {
  return String(value || '').normalize('NFC').replace(/\u00a0/g, ' ')
    .replace(/\r\n?/g, '\n').replace(/\s+/g, ' ').trim();
}

/** Match a complete payload across harmless long-form DOM whitespace layout. */
export function plannerTextMatches(left, right) {
  const expected = normalizePlannerText(left);
  const observed = normalizePlannerText(right);
  if (expected === observed) return true;
  if (Math.min(expected.length, observed.length) < 512) return false;
  return expected.replace(/\s/g, '') === observed.replace(/\s/g, '');
}

export async function resolvePlannerComposer(page, conversationId) {
  const selector = plannerComposerSelector(conversationId);
  const composer = page.locator(selector);
  const count = await composer.count();
  if (count !== 1) throw new Error(`Expected one composer for Planner conversation; found ${count}`);
  await composer.waitFor({ state: 'visible', timeout: 15000 });
  if (!await composer.isEditable()) throw new Error('Planner composer is visible but not editable');
  // Brief stabilization delay for ProseMirror to settle after becoming visible.
  await page.waitForTimeout(300);
  return composer;
}

/** Fill once and invoke an injected submit transport; useful for fake-path tests. */
export async function fillPlannerComposer({ page, conversationId, text, transport }) {
  if (typeof text !== 'string' || !text.length) throw new Error('Planner payload must be non-empty text');
  if (typeof transport !== 'function') throw new Error('Planner submit transport is required');
  const composer = await resolvePlannerComposer(page, conversationId);
  // ProseMirror contenteditable elements are unreliable with Playwright's fill().
  // Click to focus, clear existing content, then insert text via keyboard.
  await composer.click();
  await page.keyboard.press('ControlOrMeta+a');
  await page.keyboard.press('Backspace');
  await page.keyboard.insertText(text);
  return transport(composer);
}
