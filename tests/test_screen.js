const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
// Pull the two pure functions out and exercise them with no DOM at all.
const m = src.match(/function chooseScreen\(d\)\{[\s\S]*?\n\}/);
const n = src.match(/function staleLine\(n\)\{[\s\S]*?\n\}/);
if (!m || !n) { console.log("  could not extract pure functions"); process.exit(1); }
eval(m[0]); eval(n[0]);

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
