// Does the Upkeep tab actually render the controls its handlers query for?
//
// Every endpoint test passed while the tab was unusable: the add form POSTed,
// the server said 400 with a good reason, and #status -- which lives inside
// .bar, hidden on this tab -- swallowed it. So this checks the markup, not the
// server.
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const html = fs.readFileSync(process.argv[3], 'utf8');

const grab = (name, args) => {
  const re = new RegExp('function ' + name + '\\(' + args + '\\)\\{[\\s\\S]*?\\n\\}');
  const hit = src.match(re);
  if (!hit) { console.log('  FAIL could not extract ' + name); process.exitCode = 1; return ''; }
  return hit[0];
};
// eval() of a `const` does NOT leak into the enclosing scope, unlike a function
// declaration -- so these are rebound onto globalThis to be visible to
// renderLists.
const pick = (name) => {
  const hit = src.match(new RegExp('const ' + name + ' = [^;]*;'));
  if (!hit) { console.log('  FAIL could not extract ' + name); process.exitCode = 1; return ''; }
  return hit[0].replace('const ' + name + ' =', 'globalThis.' + name + ' =');
};

const esc = (x) => String(x == null ? '' : x).replace(/&/g,'&amp;').replace(/</g,'&lt;');
let captured = '';
const fakeNode = {
  set innerHTML(v){ captured = v; },
  get innerHTML(){ return captured; },
  querySelectorAll: () => [],
  querySelector: () => null,
  classList: {toggle(){}, add(){}, remove(){}},
};
const $ = () => fakeNode;
const CACHE = {lists:null};
const itemRow = () => fakeNode, wireTick = () => {}, shopOf = () => null,
      bumpCount = () => {}, say = () => {}, loadLists = () => {},
      choreFields = () => ({}), chorePost = () => {};
eval(pick('DAYS')); eval(pick('TIMES')); eval(pick('SLOT_FOR'));
eval(grab('shortDate','iso')); eval(grab('cadenceText','c')); eval(grab('stateLine','c'));
eval(grab('renderLists','which, data'));

const check = (label, ok, detail) => {
  console.log(`  ${ok?'ok  ':'FAIL'} ${label}${ok?'':'  '+(detail||'')}`);
  if (!ok) process.exitCode = 1;
};

// --- the static page: #status must not be inside .bar --------------------
const bar = html.match(/<div class="bar">[\s\S]*?<\/div>/);
check('#status is NOT inside .bar (setMode hides .bar on this tab)',
      bar && !bar[0].includes('id="status"'), bar && bar[0]);
check('#status still exists somewhere', html.includes('id="status"'));
check('#status is a fixed toast, so it shows on every tab',
      /#status\{[^}]*position:fixed/.test(html));

// The list-delete x must be scoped to a list header: a chore row has one too,
// and that loop runs after the chore wiring and would overwrite its handler.
check('the list-delete kill selector is scoped to a lane header',
      src.includes("querySelectorAll('.lane > h2 .kill')"),
      (src.match(/querySelectorAll\('[^']*kill[^']*'\)/g)||[]).join(' '));

// --- an empty tab still offers the add form ------------------------------
renderLists('upkeep', {upkeep: [], upkeep_errors: []});
check('an empty Upkeep tab renders the add form', captured.includes('id="newchore"'));
for (const cls of ['title','every','win','tod','est','ess']){
  check(`the add form has .${cls}, which choreFields/onsubmit query for`,
        new RegExp('class="' + cls + '"').test(captured));
}
check('the add form submits', captured.includes('type="submit"'));
check('an empty tab says so', captured.includes('no chores yet'));

// --- a populated tab ----------------------------------------------------
const chore = {slug:'change-the-sheets', title:'Change the sheets', every:'14d',
               window:'weekend', days:null, time_of_day:'evening', est_min:15,
               essential:false, state:'due', next_due:'2026-10-03',
               overdue_days:3, missed_windows:1, last_done:'2026-09-15',
               snooze_until:null, scheduled_today:null};
renderLists('upkeep', {upkeep:[chore], upkeep_errors:[]});
check('the row carries data-slug, which the wiring keys on',
      captured.includes('data-slug="change-the-sheets"'));
for (const cls of ['sched','did','snz','save','kill','at']){
  check(`the row has .${cls}`, new RegExp('class="' + cls + '"').test(captured));
}
check('the row has the action box the errors render into',
      captured.includes('class="cadedit acts"'));
check('the row shows its state', captured.includes('3d over'));
check('the row shows its cadence', captured.includes('every 14d'));
check('the row shows the deferral count', captured.includes('deferred 1x'));
check('the weekend window is pre-selected in the editor',
      /<option value="weekend" selected>/.test(captured));
check('the time-of-day is pre-selected', /<option value="evening" selected>/.test(captured));
check('est_min is pre-filled', captured.includes('value="15"'));
check('the slot defaults from time_of_day', captured.includes('value="19:00"'));

// A chore already on today must not offer "Add to today" again.
renderLists('upkeep', {upkeep:[{...chore, scheduled_today:{at:'2026-09-29T19:00:00',
                                                           status:'needs_action'}}],
                       upkeep_errors:[]});
check('a chore already on today does not offer Add to today',
      !captured.includes('class="sched"'));
check('...but can still be marked done', captured.includes('class="did"'));
check('...and says where it is', captured.includes('on today at 19:00'));

// A broken chore file must be visible on the tab, not swallowed.
renderLists('upkeep', {upkeep:[], upkeep_errors:['Laundry: cannot read every']});
check('a broken chore file is shown on the tab',
      captured.includes('Problems in upkeep.yaml') && captured.includes('cannot read every'));

// explicit days instead of a window
renderLists('upkeep', {upkeep:[{...chore, window:null, days:['mon','thu']}],
                       upkeep_errors:[]});
check('explicit days are checked in the editor',
      (captured.match(/class="daybox" type="checkbox" value="(mon|thu)" checked/g)||[]).length === 2,
      captured.match(/daybox[^>]*/g));
check('...and the window select falls back to any day',
      /<option value="" selected>/.test(captured));
