import { chromium } from 'playwright';
import os from 'node:os';
import path from 'node:path';
import { browserOwnershipMatches, conversationIdFromUrl, grabSnapshot } from './chatgpt_dom.js';
import { fillPlannerComposer, normalizePlannerText, plannerComposerFormSelector, plannerComposerSelector, plannerTextMatches } from './chatgpt_composer.mjs';

const [operation, url, text] = process.argv.slice(2);
const out = (value, code = 0) => { console.log(JSON.stringify(value)); process.exitCode = code; };
let browser;
let submissionAttempted = false;

try {
  if (!['verify', 'verify-refresh', 'readiness', 'submit'].includes(operation) || !url) {
    throw new Error('Expected verify|verify-refresh|readiness|submit and a Planner conversation URL');
  }
  if (operation === 'submit' && (!text || !text.length)) throw new Error('Planner payload must be non-empty');
  const expectedId = conversationIdFromUrl(url);
  if (!expectedId) throw new Error('Planner URL must identify a conversation');
  const profile = path.resolve(process.env.BOT_CHROME_PROFILE || path.join(os.homedir(), '.agent-relay/chrome-profile'));
  browser = await chromium.connectOverCDP('http://127.0.0.1:9222');
  const cdp = await browser.newBrowserCDPSession();
  const commandLine = await cdp.send('Browser.getBrowserCommandLine');
  if (!browserOwnershipMatches(commandLine.arguments, profile, 9222)) throw new Error('CDP browser ownership could not be verified');
  const context = browser.contexts()[0];
  let page = context.pages().find((item) => conversationIdFromUrl(item.url()) === expectedId);
  if (!page) {
    page = await context.newPage();
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
  }
  if (conversationIdFromUrl(page.url()) !== expectedId) throw new Error('Planner conversation identity mismatch');

  if (operation === 'verify' || operation === 'verify-refresh') {
    if (operation === 'verify-refresh') {
      await page.reload({ waitUntil: 'domcontentloaded', timeout: 30000 });
      if (conversationIdFromUrl(page.url()) !== expectedId) throw new Error('Planner conversation identity changed during refresh');
    }
    const messages = await readUserMessages(page);
    out({ ok: true, conversation_id: expectedId, messages });
  } else if (operation === 'readiness') {
    out({ ok: true, conversation_id: expectedId,
      ...await plannerReadiness(page, expectedId, { allowDraft: Boolean(text), expectedText: text }) });
  } else {
    const readiness = await plannerReadiness(page, expectedId, { allowDraft: true, expectedText: text });
    if (!readiness.ready) {
      out({ ok: false, not_ready: true, submitted: false, submission_attempted: false,
        reason: readiness.reason, ...readiness }, 1);
    } else {
      const receipt = await fillPlannerComposer({ page, conversationId: expectedId, text, transport: async (composer) => {
      if (conversationIdFromUrl(page.url()) !== expectedId) throw new Error('Planner conversation changed before submit');
      const send = page.locator(plannerComposerFormSelector(expectedId))
        .locator('button[data-testid="send-button"], button[aria-label*="Send" i]').last();
      try {
        // Large reports can take longer to hydrate the exact form's send
        // control. Keep waiting before any click; do not fall back to Enter.
        await send.waitFor({ state: 'visible', timeout: 60000 });
      } catch {
        await clearComposerIfUnchanged(composer, text);
        return { ok: false, not_ready: true, submitted: false, submission_attempted: false,
          reason: 'Planner send control is not ready; no send was attempted' };
      }
      const afterFill = await plannerReadiness(page, expectedId, { allowDraft: true, expectedText: text });
      if (!afterFill.ready || !await send.isEnabled()) {
        await clearComposerIfUnchanged(composer, text);
        return { ok: false, not_ready: true, submitted: false, submission_attempted: false,
          reason: afterFill.reason || 'Planner became busy before send; no send was attempted' };
      }
      // From this point on a click may have reached the page even if Playwright
      // reports an error, so the caller must treat the outcome as uncertain.
      submissionAttempted = true;
      await send.click({ timeout: 15000 });
      return { ok: true, submitted: true, submission_attempted: true, conversation_id: expectedId };
      }});
      if (receipt.not_ready) out(receipt, 1);
      else out(receipt);
    }
  }
} catch (error) {
  out({ ok: false, submitted: submissionAttempted, submission_attempted: submissionAttempted,
    reason: String(error?.message || error) }, 1);
} finally {
  await browser?.close().catch(() => {});
}

async function plannerReadiness(page, conversationId, { allowDraft = false, expectedText = '' } = {}) {
  if (conversationIdFromUrl(page.url()) !== conversationId) {
    return { ready: false, busy: false, reason: 'Planner conversation identity mismatch' };
  }
  const snapshot = await page.evaluate(grabSnapshot);
  const composer = page.locator(plannerComposerSelector(conversationId));
  const count = await composer.count();
  let visible = false, editable = false, composerText = '';
  if (count === 1) {
    visible = await composer.isVisible();
    editable = visible && await composer.isEditable();
    if (visible) composerText = await composer.evaluate((el) => el.innerText || el.textContent || '');
  }
  const hasDraft = Boolean(normalizePlannerText(composerText));
  const composerMatchesExpected = allowDraft && plannerTextMatches(composerText, expectedText);
  const draftMatches = !hasDraft || composerMatchesExpected;
  const busy = Boolean(snapshot.streaming || !snapshot.latest_message_is_assistant);
  const ready = !busy && count === 1 && visible && editable && draftMatches;
  let reason = '';
  if (snapshot.streaming) reason = 'Planner is generating a response';
  else if (!snapshot.latest_message_is_assistant) reason = 'Planner has a user query awaiting a response';
  else if (count !== 1 || !visible || !editable) reason = 'Planner composer is not visible and editable';
  else if (!draftMatches) reason = 'Planner composer contains a different unsent draft';
  return { ready, busy, streaming: Boolean(snapshot.streaming), latest_message_is_assistant: Boolean(snapshot.latest_message_is_assistant),
    composer_count: count, composer_visible: visible, composer_editable: editable,
    composer_has_draft: hasDraft, composer_matches_expected: composerMatchesExpected, reason };
}

async function clearComposerIfUnchanged(composer, text) {
  const current = await composer.evaluate((el) => el.innerText || el.textContent || '').catch(() => null);
  if (current !== null && plannerTextMatches(current, text)) {
    await composer.click().catch(() => {});
    await composer.page().keyboard.press('ControlOrMeta+a').catch(() => {});
    await composer.page().keyboard.press('Backspace').catch(() => {});
  }
}

async function readUserMessages(page) {
  return page.locator('[data-user-message-bubble], [data-message-author-role="user"]').evaluateAll((nodes) => nodes.map((node) => {
    const textNode = node.cloneNode(true);
    textNode.querySelectorAll('button,[role="toolbar"],svg,[hidden],[aria-hidden="true"]').forEach((item) => item.remove());
    return {
      id: node.closest('[data-chatgpt-search-message-ids]')?.getAttribute('data-chatgpt-search-message-ids')?.split(/\s+/)[0]
        || node.getAttribute('data-dil-message-id') || node.getAttribute('data-message-id') || node.getAttribute('data-user-message-id')
        || node.querySelector('[data-dil-message-id],[data-message-id],[data-user-message-id]')?.getAttribute('data-dil-message-id')
        || node.querySelector('[data-dil-message-id],[data-message-id],[data-user-message-id]')?.getAttribute('data-message-id')
        || node.querySelector('[data-dil-message-id],[data-message-id],[data-user-message-id]')?.getAttribute('data-user-message-id') || null,
      text: (textNode.innerText || textNode.textContent || '').replace(/\u00a0/g, ' ').trim(),
    };
  }));
}
