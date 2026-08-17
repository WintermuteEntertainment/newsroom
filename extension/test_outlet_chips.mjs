// The per-outlet chips, driven by the SHIPPED storyNode against a REAL digest payload.
//
// Every value here comes from a live /api/digest response, not a hand-written fixture: the
// thing most likely to break this feature is a payload shape assumption (outlet_links is a
// JSON *string*, not an object, because it round-trips through the run's CSV), and a fixture
// I write myself would encode my assumption rather than test it.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

// --- Minimal DOM. Only what storyNode touches. ---
function makeEl(tag) {
  return {
    tagName: tag.toUpperCase(), className: '', textContent: '', title: '',
    href: undefined, target: undefined, rel: undefined,
    children: [], style: {},
    appendChild(c) { this.children.push(c); return c; },
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, '');
      const out = [];
      const walk = (n) => n.children.forEach((c) => {
        if (String(c.className).split(/\s+/).includes(cls)) out.push(c);
        walk(c);
      });
      walk(this);
      return out;
    },
  };
}
globalThis.document = { createElement: makeEl, querySelector: () => null };

// Load the shipped renderer and pull out storyNode + parseOutletLinks.
const src = readFileSync('digest.js', 'utf8');
const storyNode = new Function(`${src}; return storyNode;`)();
const parseOutletLinks = new Function(`${src}; return parseOutletLinks;`)();

const data = JSON.parse(readFileSync('live_fixture.json', 'utf8'));
const rows = data.rows;
assert.ok(rows.length >= 10, 'need a real multi-story payload');

let checked = 0, leaderSeen = 0, moreSeen = 0;
for (const [i, row] of rows.entries()) {
  const node = storyNode(row, i + 1);
  const chips = node.querySelectorAll('chip');
  const links = parseOutletLinks(row.outlet_links);
  const closest = (row.covered_closest_by || '').split(';').map(s => s.trim()).filter(Boolean);
  const carried = (row.also_carried_by || '').split(';').map(s => s.trim()).filter(Boolean);
  const total = closest.length + carried.length;

  const named = chips.filter(c => !String(c.className).includes('chip-more'));
  assert.ok(named.length <= 3, `row ${i}: ${named.length} chips, cap is 3`);
  assert.equal(named.length, Math.min(3, total), `row ${i}: wrong chip count`);

  // Leaders first, and marked.
  named.forEach((c) => {
    const isLeader = String(c.className).includes('chip-leader');
    assert.equal(isLeader, closest.includes(c.textContent),
      `row ${i}: "${c.textContent}" leader flag wrong`);
    if (isLeader) leaderSeen++;
  });

  // Every chip with a real link must be an anchor pointing at THAT outlet's article,
  // opened safely. A chip without a link must not be an anchor.
  named.forEach((c) => {
    const url = links[c.textContent];
    if (url) {
      assert.equal(c.tagName, 'A', `row ${i}: "${c.textContent}" has a url but is not a link`);
      assert.equal(c.href, url, `row ${i}: "${c.textContent}" wrong href`);
      assert.equal(c.target, '_blank');
      assert.ok(String(c.rel).includes('noopener'), 'target=_blank needs rel=noopener');
    } else {
      assert.equal(c.tagName, 'SPAN', `row ${i}: unlinked "${c.textContent}" must not be an anchor`);
    }
  });

  // Leaders must come FIRST, not merely be marked. With only three of up to nineteen outlets
  // shown, showing also-carried outlets ahead of the ones the snippet was drawn from would
  // silently misrepresent the sourcing -- and marking alone does not catch that.
  const leaderFlags = named.map(c => String(c.className).includes('chip-leader'));
  const firstNonLeader = leaderFlags.indexOf(false);
  if (firstNonLeader !== -1) {
    assert.ok(!leaderFlags.slice(firstNonLeader).includes(true),
      `row ${i}: leader chips appear after non-leaders -- ordering lost (${named.map(c => c.textContent).join(', ')})`);
  }
  // And the shown set must be the first three of leaders-then-carried, in that order.
  assert.deepEqual(named.map(c => c.textContent), [...closest, ...carried].slice(0, 3),
    `row ${i}: chip order does not follow closest-then-carried`);

  // The regression that matters: chips all pointing at the featured link instead of each
  // outlet's own article. (The leading outlet's own article legitimately IS row.link, so
  // this is a per-row check, not a per-chip one.)
  const linkedHrefs = named.filter(c => c.tagName === 'A').map(c => c.href);
  if (linkedHrefs.length > 1) {
    assert.ok(new Set(linkedHrefs).size > 1,
      `row ${i}: all ${linkedHrefs.length} chips share one href -- not per-outlet links`);
  }

  const more = chips.filter(c => String(c.className).includes('chip-more'));
  if (total > 3) {
    assert.equal(more.length, 1, `row ${i}: expected an overflow chip`);
    assert.equal(more[0].textContent, `+${total - 3}`, `row ${i}: wrong overflow count`);
    assert.equal(more[0].tagName, 'SPAN', 'overflow chip must not be a link');
    assert.ok(more[0].title.length > 0, 'overflow chip should name the hidden outlets');
    moreSeen++;
  } else {
    assert.equal(more.length, 0, `row ${i}: unexpected overflow chip`);
  }
  checked++;
}
console.log(`  PASS  ${checked} real stories: <=3 chips, leaders marked and ordered first, every href is that outlet's own article`);
assert.ok(leaderSeen > 0 && moreSeen > 0, 'payload should exercise both leader and overflow paths');
console.log(`  PASS  exercised both paths on real data (${leaderSeen} leader chips, ${moreSeen} rows with overflow)`);

// --- Degradation: outlet_links is the one field that can arrive malformed. ---
const base = rows[0];
for (const [label, bad] of [['null', null], ['empty string', ''], ['broken JSON', '{not json'],
                            ['a JSON array', '[1,2]'], ['a JSON scalar', '42']]) {
  const node = storyNode({ ...base, outlet_links: bad }, 1);
  const chips = node.querySelectorAll('chip').filter(c => !String(c.className).includes('chip-more'));
  assert.ok(chips.length > 0, `${label}: outlet names should still render`);
  chips.forEach(c => assert.equal(c.tagName, 'SPAN', `${label}: must not produce a dead anchor`));
}
console.log('  PASS  malformed outlet_links degrades to unlinked names, never a dead link');

// A story no outlet is credited with must not render an empty chip strip.
const bare = storyNode({ ...base, covered_closest_by: '', also_carried_by: '' }, 1);
assert.equal(bare.querySelectorAll('chip').length, 0, 'no outlets -> no chips');
console.log('  PASS  a story with no credited outlets renders no chip strip');

// The object form must work too, in case the payload ever stops stringifying it.
assert.deepEqual(parseOutletLinks({ Reuters: 'https://x' }), { Reuters: 'https://x' });
console.log('  PASS  parseOutletLinks accepts the object form as well as the string form');
