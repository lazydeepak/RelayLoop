import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class ComposerAdapterTests(unittest.TestCase):
    def run_node(self, script):
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout

    def test_scoped_composer_and_fake_submit_preserve_full_payload_once(self):
        script = r'''import assert from 'node:assert/strict';
import {fillPlannerComposer} from './chatgpt_composer.mjs';
const id='6ae4d33a-bee0-83ec-9308-7d48b781c915';
const text='Response **as-is**\nsecond line 👋';
const calls=[];
const composer={count:async()=>1,waitFor:async o=>calls.push(['wait',o.state]),isEditable:async()=>true,click:async()=>calls.push(['click']),payload:null};
const page={locator:s=>{assert.match(s,/form\[data-chatgpt-composer\]/);assert.match(s,new RegExp(id));assert.match(s,/contenteditable=.*role/);return composer},keyboard:{press:async x=>calls.push(['press',x]),insertText:async x=>{composer.payload=x;calls.push(['insertText',x])}},waitForTimeout:async ms=>calls.push(['waitTimeout',ms])};
let submits=0;
const receipt=await fillPlannerComposer({page,conversationId:id,text,transport:async c=>{submits++;assert.equal(c.payload,text);return {confirmed:true,destination_message_id:'fake-planner-user-1'}}});
assert.equal(receipt.confirmed,true);assert.equal(submits,1);assert.equal(calls.filter(c=>c[0]==='insertText').length,1);
console.log('fake submit passed');'''
        self.assertIn("fake submit passed", self.run_node(script))

    def test_missing_or_ambiguous_composer_never_calls_transport(self):
        script = r'''import assert from 'node:assert/strict';
import {fillPlannerComposer} from './chatgpt_composer.mjs';
for (const count of [0,2]) {
 const composer={count:async()=>count,waitFor:async()=>{throw Error('must not wait')},isEditable:async()=>true,fill:async()=>{throw Error('must not fill')}};
 let submissions=0;
 await assert.rejects(fillPlannerComposer({page:{locator:()=>composer},conversationId:'c-test',text:'no submit',transport:async()=>{submissions++}}),/Expected one composer/);
 assert.equal(submissions,0);
}
console.log('missing/ambiguous guarded');'''
        self.assertIn("missing/ambiguous guarded", self.run_node(script))

    def test_full_payload_comparison_ignores_dom_whitespace_layout_only(self):
        script = r'''import assert from 'node:assert/strict';
import {normalizePlannerText,plannerTextMatches} from './chatgpt_composer.mjs';
const report='Paragraph one.\n\n**Verification:** checks passed.\nLast line.';
const rendered='Paragraph one.\n\n\n**Verification:**   checks passed.\nLast line.\n';
assert.equal(normalizePlannerText(rendered),normalizePlannerText(report));
assert.notEqual(normalizePlannerText(rendered.replace('checks passed','checks failed')),normalizePlannerText(report));
const long='[Run](https://example.test/run) '+('verified result '.repeat(40));
const laidOut='[Run](\nhttps://example.test/run) '+('verified result '.repeat(40));
assert.equal(plannerTextMatches(long,laidOut),true);
assert.equal(plannerTextMatches(long,laidOut.replace('verified result','different result')),false);
assert.equal(plannerTextMatches('hello world','helloworld'),false);
'''
        self.run_node(script)


if __name__ == "__main__": unittest.main()
