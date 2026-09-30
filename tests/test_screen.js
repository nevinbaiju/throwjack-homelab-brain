const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
// Pull the two pure functions out and exercise them with no DOM at all.
const grab = (name, args) => {
  const re = new RegExp('function ' + name + '\\(' + args + '\\)\\{[\\s\\S]*?\\n\\}');
  const hit = src.match(re);
  if (!hit) { console.log('  FAIL could not extract ' + name); process.exitCode = 1; return ''; }
  return hit[0];
};
// esc() lives behind the DOM in the real page; the renderers only need it to
// return a string, and these tests are about WHICH string, not escaping.
const esc = (x) => String(x == null ? '' : x);
eval(grab('chooseScreen', 'd'));
eval(grab('staleLine', 'n'));
eval(grab('mutLine', 'm'));
eval(grab('updLine', 'm'));
eval(grab('choreNote', 'm'));
eval(grab('stateLine', 'c'));
eval(grab('cadenceText', 'c'));
eval(grab('shortDate', 'iso'));

const T = (label, d, want) => {
  const got = chooseScreen(d);
  console.log(`  ${got === want ? 'ok  ' : 'FAIL'} ${label.padEnd(52)} -> ${got}${got===want?'':'  (wanted '+want+')'}`);
  if (got !== want) process.exitCode = 1;
};
const pend = (today) => ({from_today: today, kind:'draft', proposal:{}});

T('fetch failed',                                   null,                                      'error');
T('nothing applied, no draft',                      {is_current:false, pending:null},          'fresh');
T('applied today, no draft',                        {is_current:true,  pending:null},          'current');
T('draft from today, nothing applied',              {is_current:false, pending:pend(true)},    'review');
T('draft from today, plan already applied today',   {is_current:true,  pending:pend(true)},    'review');
T('STALE draft from a previous day, nothing applied',{is_current:false, pending:pend(false)},  'fresh');
T('STALE draft from a previous day, applied today', {is_current:true,  pending:pend(false)},   'current');
T('pending key absent entirely',                    {is_current:false},                        'fresh');
T('a request is in flight',                         {is_current:false, working:{id:'j'}},      'working');
T('in flight outranks a pending draft',             {is_current:false, working:{id:'j'}, pending:pend(true)}, 'working');
T('in flight outranks an applied plan',             {is_current:true,  working:{id:'j'}},      'working');

console.log();
for (const [in_, want] of [[0,''],[1,'1 item was dropped — finished or removed since the plan was made.'],
                           [3,'3 items were dropped — finished or removed since the plan was made.'],
                           [['a','b'],'2 items were dropped — finished or removed since the plan was made.']]) {
  const got = staleLine(in_);
  console.log(`  ${got===want?'ok  ':'FAIL'} staleLine(${JSON.stringify(in_)}) -> ${JSON.stringify(got)}`);
  if (got !== want) process.exitCode = 1;
}

// --- the renderers must never produce a blank row -------------------------
// renderDraft wraps whatever these return in a PRE-TICKED checkbox, so an op
// they do not recognise used to render as an invisible, already-accepted change.
console.log();
const ops = [
  {op:'retitle', was:'a', title:'b'},
  {op:'set_blocked', blocked_on:'Sam', title:'t'},
  {op:'clear_blocked', title:'t'},
  {op:'set_lane', lane:'backlog', title:'t'},
  {op:'note_append', text:'n', title:'t'},
  {op:'list_add', label:'Costco', items:['paneer']},
  {op:'complete', title:'t'},
  {op:'schedule_chore', slug:'laundry', title:'Laundry', at:'19:00', state:'due'},
  {op:'to_today', title:'t'},
  {op:'from_today', title:'t'},
  {op:'add', lane:'today', title:'t'},
  {op:'something_invented_later', title:'t'},
];
for (const m of ops) {
  for (const [fn, name] of [[mutLine,'mutLine'], [updLine,'updLine']]) {
    const got = fn(m);
    const ok = typeof got === 'string' && got.trim().length > 0;
    console.log(`  ${ok?'ok  ':'FAIL'} ${name}(${m.op}) renders something`);
    if (!ok) process.exitCode = 1;
  }
}
const sc = mutLine({op:'schedule_chore', slug:'laundry', title:'Laundry', at:'19:00', state:'due'});
for (const want of ['Laundry', '19:00']) {
  const ok = sc.includes(want);
  console.log(`  ${ok?'ok  ':'FAIL'} a scheduled chore shows ${want}`);
  if (!ok) process.exitCode = 1;
}
// A chore the user asked for out of its window is accepted, so the row has to
// SAY it is not due rather than silently pretending it was.
const warn = mutLine({op:'schedule_chore', slug:'sheets', title:'Sheets', at:'11:00',
                      state:'waiting', next_due:'2026-10-03'});
const ok = warn.includes('not due') && warn.includes('2026-10-03');
console.log(`  ${ok?'ok  ':'FAIL'} scheduling a chore out of its window says so`);
if (!ok) process.exitCode = 1;

// --- stateLine ------------------------------------------------------------
console.log();
const S = (label, c, ...wants) => {
  const got = stateLine(c);
  const good = wants.every(w => got.includes(w)) && got.trim().length > 0;
  console.log(`  ${good?'ok  ':'FAIL'} ${label.padEnd(50)} -> ${got}`);
  if (!good) process.exitCode = 1;
};
S('due today, never done',      {state:'due', overdue_days:0, missed_windows:0}, 'due today', 'never done');
S('due and overdue',            {state:'due', overdue_days:3, missed_windows:0, last_done:'2026-09-20'}, '3d over', 'last done');
S('due, deferred twice',        {state:'due', overdue_days:14, missed_windows:2}, 'deferred 2x');
S('waiting for its window',     {state:'waiting', overdue_days:2, next_due:'2026-10-03'}, 'waiting for its window');
S('not due yet',                {state:'not_due', next_due:'2026-10-06'}, 'next');
S('snoozed',                    {state:'snoozed', snooze_until:'2026-10-02'}, 'snoozed until');
S('broken chore',               {state:'broken', error:'cannot read every'}, 'BROKEN');
S('already on today',           {state:'due', scheduled_today:{at:'2026-09-29T19:00:00', status:'needs_action'}}, 'on today at 19:00');
S('done today',                 {state:'not_due', scheduled_today:{at:'2026-09-29T19:00:00', status:'completed'}}, 'done today');

console.log();
const C = (label, c, want) => {
  const got = cadenceText(c);
  console.log(`  ${got===want?'ok  ':'FAIL'} ${label.padEnd(50)} -> ${JSON.stringify(got)}`);
  if (got !== want) process.exitCode = 1;
};
C('plain cadence',        {every:'7d'}, 'every 7d');
C('weekend only',         {every:'14d', window:'weekend'}, 'every 14d · weekends only');
C('explicit days',        {every:'7d', days:['mon','thu']}, 'every 7d · mon/thu');
C('the lot',              {every:'1m', window:'weekend', time_of_day:'evening', est_min:20, essential:true},
                          'every 1m · weekends only · evening · ~20m · essential');
