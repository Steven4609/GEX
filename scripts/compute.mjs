// GEX 计算核心（Node 18+）。
// 算法逐行沿用 Claude 定时任务里的 gex.js（2026-11-24 版），只做了三处改动：
//   1. 数据不再在浏览器里 fetch，而是由 run.py 抓好后从标准输入传进来；
//   2. 输出结构化数字和类型代码（不再直接拼中文），文字由网页按中文 / 英文生成；
//   3. 支持任意代码（自选股），杠杆 ETF 只对 SPY / QQQ 计算。
// 用法：python 把 JSON 写到 stdin，本脚本把结果 JSON 写到 stdout。
import {readFileSync} from 'node:fs';

const input = JSON.parse(readFileSync(0, 'utf8'));
const RUN = input.run;

const pdf = x => Math.exp(-x * x / 2) / Math.sqrt(2 * Math.PI);
const ncdf = x => { const t = 1 / (1 + 0.2316419 * Math.abs(x)); const d = 0.3989423 * Math.exp(-x * x / 2); const p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274)))); return x > 0 ? 1 - p : p; };
const E = RUN.now; const nowW = Date.UTC(E.y, E.m - 1, E.d, E.H, E.M);
const ymd = s => s.split('-').map(Number);
const [hy, hm, hd] = ymd(RUN.horizon);
const horYY = String(hy).slice(2) + String(hm).padStart(2, '0') + String(hd).padStart(2, '0');
const today = `${E.y}-${String(E.m).padStart(2, '0')}-${String(E.d).padStart(2, '0')}`;
const [cH, cM] = RUN.close.split(':').map(Number), closeMin = cH * 60 + cM, lastMin = `${String(Math.floor((closeMin - 1) / 60)).padStart(2, '0')}:${String((closeMin - 1) % 60).padStart(2, '0')}`;
const sessDay = RUN.session || today; // 数据对应的交易日（周末 / 假日手动运行时是上一个交易日）
const r = 0.04;
const ORDER = ['brokenPut', 'brokenCall', 'putWall', 'callWall', 'zero', 'maxNeg', 'neg', 'pos'];
const PRIMARY = ['putWall', 'callWall', 'zero', 'brokenPut', 'brokenCall'];
const rnd = (x, n = 2) => x == null || !isFinite(x) ? null : +x.toFixed(n);

function analyze(T) {
  const {t, bull, bear, raw, hist, etf} = T;
  const d = raw.data;
  const S = RUN.mode === 'post' ? d.close : (d.current_price || d.close);
  const re = new RegExp('^' + raw.root + '(\\d{6})([CP])(\\d{8})$');
  const opts = [];
  for (const o of d.options) {
    const m = o.option.match(re); if (!m || !(o.open_interest > 0)) continue;
    const yy = m[1]; const ew = Date.UTC(2000 + +yy.slice(0, 2), +yy.slice(2, 4) - 1, +yy.slice(4, 6), 16, 0);
    const days = (ew - nowW) / 864e5; if (days <= 0) continue;
    opts.push({exp: yy, days, cp: m[2], K: +m[3] / 1000, oi: o.open_interest, iv: o.iv || 0, g: Math.max(o.gamma || 0, 0)});
  }
  if (!opts.length) throw new Error(`${t}: 没有可用的期权合约`);
  const sg = o => o.cp === 'C' ? 1 : -1;
  const gM = o => o.g * o.oi * 100 * S * S * 0.01 / 1e6;
  const cboe = o => gM(o) * sg(o);
  const bsG = (o, x) => { if (o.iv <= 0) return 0; const T = o.days / 365, v = o.iv * Math.sqrt(T); const d1 = (Math.log(x / o.K) + (r + o.iv * o.iv / 2) * T) / v; return pdf(d1) / (x * v); };
  const prof = x => opts.reduce((a, o) => a + bsG(o, x) * o.oi * 100 * x * x * 0.01 * sg(o), 0) / 1e6;
  const sum = f => opts.filter(f).reduce((a, o) => a + cboe(o), 0);
  const exps = [...new Set(opts.map(o => o.exp))].sort();
  const near = exps[0];
  const totC = sum(() => true), totBS = prof(S), wk = sum(o => o.days <= 7), nearN = sum(o => o.exp === near);
  const byExp = exps.map(e => [e, sum(o => o.exp === e)]).sort((a, b) => a[1] - b[1]);
  // grid step: $1 above $300, $0.5 above $50, finer for cheap stocks so the zero-gamma search has resolution
  const step = S > 300 ? 1 : S > 50 ? 0.5 : S > 10 ? 0.1 : 0.02; const grid = [];
  const g0 = S > 50 ? Math.round(S * 0.93) : Math.round(S * 0.93 / step) * step;
  const g1 = S > 50 ? Math.round(S * 1.07) : S * 1.07;
  for (let i = 0, x = g0; x <= g1 + 1e-9; i++, x = +(g0 + i * step).toFixed(4)) grid.push([x, prof(x)]);
  const zeros = []; for (let i = 1; i < grid.length; i++) { const [a, fa] = grid[i - 1], [b, fb] = grid[i]; if ((fa < 0) !== (fb < 0)) zeros.push(a + (0 - fa) * (b - a) / (fb - fa)); }
  const zero = zeros.length ? zeros.sort((a, b) => Math.abs(a - S) - Math.abs(b - S))[0] : null;
  const byK = {}; for (const o of opts) { const b = byK[o.K] = byK[o.K] || {c: 0, p: 0}; if (o.cp === 'C') b.c += gM(o); else b.p += gM(o); }
  const all = Object.entries(byK).map(([k, v]) => ({K: +k, c: v.c, p: v.p, n: v.c - v.p}));
  const ks = all.filter(x => Math.abs(x.K - S) / S < 0.04), ks8 = all.filter(x => Math.abs(x.K - S) / S <= 0.08);
  const callWall = ks8.filter(x => x.K >= S).sort((a, b) => b.c - a.c)[0], putWall = ks8.filter(x => x.K <= S).sort((a, b) => b.p - a.p)[0];
  const wallExp = (K, cp) => { const by = {}; let tot = 0; for (const o of opts) if (o.K === K && o.cp === cp) { const g = gM(o); by[o.exp] = (by[o.exp] || 0) + g; tot += g; } const top = Object.entries(by).sort((a, b) => b[1] - a[1])[0]; return top && tot > 0 ? {exp: top[0], share: top[1] / tot} : null; };
  if (callWall) callWall.ex = wallExp(callWall.K, 'C'); if (putWall) putWall.ex = wallExp(putWall.K, 'P');
  const putTop = ks8.slice().sort((a, b) => b.p - a.p)[0], callTop = ks8.slice().sort((a, b) => b.c - a.c)[0];
  const putAbove = putTop && putTop.K > S && (!putWall || putTop.p > putWall.p) ? putTop : null, callBelow = callTop && callTop.K < S && (!callWall || callTop.c > callWall.c) ? callTop : null;
  const posAbove = ks.filter(x => x.K > S && x.n > 0).sort((a, b) => b.n - a.n);
  const negBelow = ks.filter(x => x.K < S && x.n < 0).sort((a, b) => a.n - b.n).slice(0, 3);
  const negAll = ks.filter(x => x.n < 0).sort((a, b) => a.n - b.n);
  const atmPick = cp => opts.filter(o => (RUN.mode === 'pre' ? o.exp > horYY : o.exp >= horYY) && o.cp === cp && o.iv > 0).sort((a, b) => a.exp.localeCompare(b.exp) || Math.abs(a.K - S) - Math.abs(b.K - S))[0];
  const ac = atmPick('C'), ap = atmPick('P'); const iv = ((ac ? ac.iv : 0) + (ap ? ap.iv : 0)) / ((ac ? 1 : 0) + (ap ? 1 : 0) || 1);
  const remMin = Math.max(0, closeMin - Math.max(E.H * 60 + E.M, 570)); const Th = Math.max(((RUN.horizon === today ? 0 : RUN.hzLen) + remMin) / 390, 0.02) / 252; const sig = iv * Math.sqrt(Th);
  let ma200 = null; if (hist && hist.length) { const cl = hist.map(Number); ma200 = cl.slice(-200).reduce((a, b) => a + b, 0) / Math.min(200, cl.length); }
  const regime = zero == null ? (totBS < 0 ? 'neg' : 'pos') : (S < zero ? 'neg' : 'pos');
  const cand = [];
  const add = (K, dir, tags, type) => { const ex = cand.find(c => c.dir === dir && Math.abs(c.K - K) < 0.5 * (S > 50 ? 1 : S / 50)); if (ex) { for (const g of tags) if (!ex.tags.includes(g)) ex.tags.push(g); if (PRIMARY.includes(type)) ex.type = type; } else cand.push({K, dir, tags: [...tags], type}); };
  if (zero != null) add(zero, zero > S ? 'up' : 'down', ['zero'], 'zero');
  for (const x of negBelow) add(x.K, 'down', [negAll[0] && x.K === negAll[0].K ? 'maxNeg' : 'neg'], 'neg');
  if (posAbove[0] && regime === 'pos') add(posAbove[0].K, 'up', ['pos'], 'pos');
  if (callWall && callWall.K > S) add(callWall.K, 'up', ['callWall'], 'callWall');
  if (putWall && putWall.K < S) add(putWall.K, 'down', ['putWall'], 'putWall');
  const brokenPut = putAbove && Math.abs(Math.log(putAbove.K / S)) <= 2 * sig ? putAbove : null, brokenCall = callBelow && Math.abs(Math.log(callBelow.K / S)) <= 2 * sig ? callBelow : null;
  if (brokenPut) add(brokenPut.K, 'up', ['brokenPut'], 'brokenPut');
  if (brokenCall) add(brokenCall.K, 'down', ['brokenCall'], 'brokenCall');
  for (const c of cand) {
    c.tags.sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b));
    c.dist = Math.abs(Math.log(c.K / S)); c.close = 1 - ncdf(c.dist / sig); c.touch = Math.min(1, 2 * c.close);
    c.hitToday = RUN.mode === 'mid' && (c.dir === 'down' ? d.low <= c.K : d.high >= c.K);
  }
  const lv = [], omitted = [];
  const gapK = S > 50 ? 0.5 : S / 100;
  for (const dir of ['up', 'down']) {
    const side = cand.filter(c => c.dir === dir).sort((a, b) => a.dist - b.dist);
    side.forEach((c, i) => { if (i === 0 || (lv.filter(l => l.dir === dir).length < 2 && (c.touch >= 0.05 || c.dist <= 2 * sig))) lv.push(c); else omitted.push(c); });
    const shown = lv.filter(l => l.dir === dir); const last = shown[shown.length - 1];
    if (last && dir === 'down') { const nxt = side.find(c => c.K < last.K - gapK) || negAll.filter(x => x.K < last.K - gapK).sort((a, b) => b.K - a.K)[0]; if (nxt) last.next = nxt.K; }
    if (last && dir === 'up') { const nxt = side.find(c => c.K > last.K + gapK); if (nxt) last.next = nxt.K; }
  }
  lv.sort((a, b) => (a.dir === b.dir ? (a.dir === 'up' ? a.K - b.K : b.K - a.K) : (a.dir === 'up' ? -1 : 1)));
  const Sref = RUN.mode === 'pre' ? d.close : (d.prev_day_close || d.close);
  const etfPx = (e, lev) => { if (!e) return null; const ltt = String(e.last_trade_time || ''); const traded = ltt.slice(0, 10) === sessDay; if (RUN.mode === 'post' && traded && ltt.slice(11, 16) >= lastMin && e.close > 0) return e.close; const p0 = traded ? (e.prev_day_close || e.close) : e.close; return p0 ? p0 * (1 + lev * (S / Sref - 1)) : null; };
  const pB = bull ? etfPx(etf && etf.bull, 3) : null, pR = bear ? etfPx(etf && etf.bear, -3) : null;
  let crossedDn = [], crossedUp = [];
  if (RUN.mode !== 'pre' && d.high > d.low) {
    const maxN = Math.max(...ks8.map(x => Math.abs(x.n)), 1e-9);
    const tag = x => putTop && x.K === putTop.K ? 'origPut' : callTop && x.K === callTop.K ? 'origCall' : (x.n < 0 ? 'neg' : 'pos');
    const key = x => Math.abs(x.n) >= 0.25 * maxN || (putTop && x.K === putTop.K) || (callTop && x.K === callTop.K);
    crossedDn = ks8.filter(x => x.K > S && x.K <= d.high && key(x)).sort((a, b) => Math.abs(b.n) - Math.abs(a.n)).slice(0, 3).sort((a, b) => a.K - b.K).map(x => ({K: x.K, tag: tag(x)}));
    crossedUp = ks8.filter(x => x.K < S && x.K >= d.low && key(x)).sort((a, b) => Math.abs(b.n) - Math.abs(a.n)).slice(0, 3).sort((a, b) => b.K - a.K).map(x => ({K: x.K, tag: tag(x)}));
  }
  const gross = opts.reduce((a, o) => a + gM(o), 0), horShare = gross > 0 ? opts.filter(o => o.exp === horYY).reduce((a, o) => a + gM(o), 0) / gross : 0;
  for (const l of lv) { const ret = l.K / S - 1; l.bull = pB != null ? pB * (1 + 3 * ret) : null; l.bear = pR != null ? pR * (1 - 3 * ret) : null; }
  const wall = w => w ? {K: w.K, c: rnd(w.c, 1), p: rnd(w.p, 1), exp: w.ex ? w.ex.exp : null, share: w.ex ? rnd(w.ex.share, 3) : null} : null;
  const lvOut = l => ({K: rnd(l.K), dir: l.dir, tags: l.tags, type: l.type, touch: rnd(l.touch, 4), close: rnd(l.close, 4), hitToday: !!l.hitToday, next: l.next ?? null, bull: rnd(l.bull), bear: rnd(l.bear)});
  return {t, bull: bull || null, bear: bear || null, src: raw.src, S: rnd(S), close: d.close, cur: d.current_price ?? null, chg: d.price_change_percent ?? null, hi: d.high, lo: d.low, ts: raw.timestamp,
    pB: rnd(pB), pR: rnd(pR), iv: rnd(iv, 4), sig: rnd(sig, 5), zero: rnd(zero, 2), totC: rnd(totC, 1), totBS: rnd(totBS, 1), wk: rnd(wk, 1), nearN: rnd(nearN, 1), near,
    opex: byExp[0] ? [byExp[0][0], rnd(byExp[0][1], 1)] : null, regime, ma200: rnd(ma200),
    lv: lv.map(lvOut), omitted: omitted.sort((a, b) => a.dist - b.dist).map(lvOut),
    callWall: wall(callWall), putWall: wall(putWall), putAbove: wall(putAbove), callBelow: wall(callBelow), brokenPut: wall(brokenPut), brokenCall: wall(brokenCall),
    crossedDn, crossedUp, horShare: rnd(horShare, 4), negTop: negAll.slice(0, 3).map(x => ({K: x.K, n: rnd(x.n, 1)})),
    profile: grid.filter((x, i) => i % Math.max(1, Math.round(5 / step * (S > 50 ? 1 : S / 100))) === 0).filter(x => Math.abs(x[0] - S) / S < 0.035).map(x => [rnd(x[0]), rnd(x[1], 1)]),
    nOpts: opts.length};
}

const results = [], errors = [];
for (const T of input.tickers) {
  try { results.push(analyze(T)); } catch (e) { errors.push({t: T.t, error: String(e.message || e)}); }
}
// state: compact snapshot for next report's comparison and for the tracking table (same fields as the Claude task's state)
const state = {date: today, mode: RUN.mode, horizon: RUN.horizon, time: `${String(E.H).padStart(2, '0')}:${String(E.M).padStart(2, '0')}`};
for (const a of results) state[a.t] = {S: a.S, net: Math.round(a.totC), zero: a.zero != null ? +a.zero.toFixed(1) : null, regime: a.regime,
  hi: RUN.mode === 'mid' ? a.hi : null, lo: RUN.mode === 'mid' ? a.lo : null, dayHi: a.hi, dayLo: a.lo, dayClose: a.close,
  callWall: a.callWall ? a.callWall.K : null, putWall: a.putWall ? a.putWall.K : null,
  lv: a.lv.map(l => ({K: l.K, dir: l.dir, type: l.type, tags: l.tags, hitToday: l.hitToday, touch: +l.touch.toFixed(3), close: +l.close.toFixed(3)}))};
const hzD = new Date(Date.UTC(hy, hm - 1, hd));
const isOpex = hzD.getUTCDay() === 5 && hd >= 15 && hd <= 21;
process.stdout.write(JSON.stringify({results, errors, state, isOpex}));
