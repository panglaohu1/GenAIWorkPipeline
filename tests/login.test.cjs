// Login must wait for server account state; tested without a browser or real account.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/genai_pipeline/console/static/login.js', 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
function harness() {
  const elements = new Map();
  const el = selector => {
    if (!elements.has(selector)) elements.set(selector, {value:'', hidden:false, disabled:false,
      textContent:'', setAttribute(key,value) { this[key]=value; }});
    return elements.get(selector);
  };
  const form=el('#loginForm');
  form.username={value:'test-user'}; form.password=el('#password'); form.password.value='test-password';
  const calls=[], pending=[], redirects=[];
  vm.runInNewContext(source, {document:{querySelector:el},
    fetch:(url,options) => { calls.push({url,options}); return new Promise((resolve,reject)=>pending.push({resolve,reject})); },
    location:{reload:()=>redirects.push('reload'),assign:url=>redirects.push(url)}});
  return {el,form,calls,pending,redirects};
}
test('a fast submit cannot guess login before setup status resolves', async () => {
  const h=harness();
  assert.equal(h.el('#enterButton').disabled,true);
  await h.form.onsubmit({preventDefault(){}});
  assert.equal(h.calls.length,1);
  h.pending.shift().resolve({ok:true,json:async()=>({configured:false})}); await flush();
  assert.equal(h.el('#confirmPassword').required,true);
  h.el('#confirmPassword').value='test-password';
  const submission=h.form.onsubmit({preventDefault(){}});
  assert.equal(h.calls[1].url,'/api/auth/setup');
  await h.form.onsubmit({preventDefault(){}});
  assert.equal(h.calls.length,2); // Enter twice must not duplicate the request.
  h.pending.shift().resolve({ok:true,json:async()=>({})}); await submission;
  assert.deepEqual(h.redirects,['/']);
});
test('an unavailable status can be retried without a page reload', async () => {
  const h=harness(); h.pending.shift().reject(new Error('offline')); await flush();
  assert.equal(h.el('#enterButton').disabled,true);
  assert.equal(h.el('#retryConnection').hidden,false);
  h.el('#retryConnection').onclick();
  h.pending.shift().resolve({ok:true,json:async()=>({configured:true})}); await flush();
  assert.equal(h.el('#enterButton').disabled,false);
  assert.equal(h.el('#retryConnection').hidden,true);
  assert.equal(h.el('#confirmRow').hidden,true);
});
