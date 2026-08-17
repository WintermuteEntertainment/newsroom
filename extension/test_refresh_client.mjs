// Drives the extension's OWN startRun/watchRun code against a real HTTP server.
//
// The server-side guard is covered by test_refresh_concurrency.py. This covers the other half:
// that the client reacts correctly when the server refuses. The interesting case is that a 409
// must NOT be presented as an error -- it means a run is already going (possibly one the user
// started on the site), so the right behaviour is to report it and attach to that run.
//
//   node test_refresh_client.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';

// Pull the two functions out of digest.js rather than reimplementing them -- a copy here would
// pass while the shipped code broke.
const src = fs.readFileSync(new URL('./digest.js', import.meta.url), 'utf8');
function extract(name) {
  const start = src.indexOf(`async function ${name}(`);
  assert.notEqual(start, -1, `${name} not found in digest.js -- did it get renamed?`);
  let depth = 0, i = src.indexOf('{', start);
  const from = i;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}' && --depth === 0) break;
  }
  return src.slice(start, i + 1);
}

const { startRun } = new Function(`${extract('startRun')}\nreturn { startRun };`)();

// A server that mimics the real endpoint's contract: one run at a time, 409 with a message.
let running = false;
let starts = 0;
const server = http.createServer((req, res) => {
  const reply = (code, body) => {
    res.writeHead(code, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(body));
  };
  if (req.method === 'GET') return reply(200, { running, error: null, profile: running ? 'local' : null });
  if (req.method === 'OPTIONS') return reply(501, {});   // as the real server does
  let body = '';
  req.on('data', (c) => (body += c));
  req.on('end', () => {
    const parsed = JSON.parse(body || '{}');
    if (running) return reply(409, { error: 'A refresh is already running.' });
    if (parsed.profile === '__metered__') return reply(401, { error: 'Password required.' });
    if (parsed.profile === '__bogus__') return reply(400, { error: 'Unknown profile.' });
    running = true; starts++;
    reply(202, { running: true, profile: parsed.profile });
  });
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;

// 1. A first run is accepted.
await startRun(base, 'local');
assert.equal(starts, 1, 'the first run did not start');

// 2. A second is refused -- and flagged as "already running", not as a fault.
let caught;
try {
  await startRun(base, 'local');
  assert.fail('a second run was accepted while one was in progress');
} catch (err) {
  caught = err;
}
assert.equal(caught.alreadyRunning, true,
  'a 409 was not flagged as alreadyRunning, so the UI would show it as a red error');
assert.match(caught.message, /already running/i, 'the refusal carried no usable message');
assert.equal(starts, 1, 'a second run started anyway');

// 3. Eight simultaneous clicks start exactly one run.
running = false; starts = 0;
const outcomes = await Promise.allSettled(
  Array.from({ length: 8 }, () => startRun(base, 'local')));
const accepted = outcomes.filter((o) => o.status === 'fulfilled').length;
const busy = outcomes.filter((o) => o.status === 'rejected' && o.reason.alreadyRunning).length;
assert.equal(starts, 1, `${starts} runs started from 8 concurrent clicks`);
assert.equal(accepted, 1, `expected 1 acceptance, got ${accepted}`);
assert.equal(busy, 7, `expected 7 already-running refusals, got ${busy}`);

// 4. A real error is still a real error: 401 and 400 must NOT be flagged as already-running,
//    or the UI would quietly report a run that never started.
running = false;
for (const [profile, pattern] of [['__metered__', /password/i], ['__bogus__', /unknown profile/i]]) {
  try {
    await startRun(base, profile);
    assert.fail(`${profile} was accepted`);
  } catch (err) {
    assert.notEqual(err.alreadyRunning, true, `${profile} was wrongly flagged as already running`);
    assert.match(err.message, pattern, `${profile} produced an unhelpful message: ${err.message}`);
  }
}

// 5. The request must avoid a CORS preflight: the real server answers OPTIONS with 501, so a
//    JSON content-type would fail before reaching the handler.
assert.match(src, /'Content-Type':\s*'text\/plain'/,
  'startRun does not send text/plain; a preflight would fail against the real server');

server.close();
console.log('  PASS  first run accepted');
console.log('  PASS  second run refused as alreadyRunning, with a message');
console.log('  PASS  8 concurrent clicks start exactly 1 run (1x202, 7x409)');
console.log('  PASS  401 and 400 stay real errors, not silent "already running"');
console.log('  PASS  request avoids the CORS preflight (text/plain)');
