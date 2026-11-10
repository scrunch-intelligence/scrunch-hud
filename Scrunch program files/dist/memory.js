// Scrunch memory page: his words and records, from the one memory store. Forget, add, export, restore.
let token = '';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function post(path, body) {
  const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Scrunch-Token': token},
                               body: JSON.stringify(body || {})});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || 'Failed');
  return j;
}

async function load() {
  const m = await (await fetch('/api/memory')).json();
  if (m.error) { $('#status').textContent = m.error; return; }
  const d = m.decisions || {};
  $('#status').textContent = m.events.length + ' records';
  $('#summary').textContent = `${m.moves} verified changes kept in move history · decided: ${d.decided || 0}` +
    (d.decided ? ` (keep ${d.keep}, reject ${d.reject}, neither ${d.neither})` : '');
  $('#words tbody').innerHTML = m.words.map(w => `<tr><td><b>${esc(w.term)}</b></td><td>${esc(w.detail)}` +
    `${w.context && w.context.plugin ? ' <span class="dim">(' + esc(w.context.plugin) + ')</span>' : ''}</td>` +
    `<td class="dim">${w.status === 'user_confirmed' ? 'yours' : 'learned, unconfirmed'}</td>` +
    `<td><button data-forget="${esc(w.id)}" data-kind="derived">Forget</button></td></tr>`).join('') ||
    '<tr><td colspan="4" class="dim">No words yet.</td></tr>';
  const u = await (await fetch('/api/usage')).json();
  $('#usagenote').textContent = u.note || '';
  $('#usage tbody').innerHTML = (u.days || []).slice().reverse().map(d => `<tr><td>${esc(d.day)}</td><td>${d.requests}</td>` +
    `<td>${d.modelCalls}</td><td>${d.tokensIn.toLocaleString()}</td><td>${d.tokensOut.toLocaleString()}</td><td>${d.failed}</td></tr>`).join('');
  $('#events tbody').innerHTML = m.events.map(e => `<tr><td class="dim">${e.at ? new Date(e.at * 1000).toLocaleString() : ''}</td>` +
    `<td>${esc(e.words)}</td><td class="dim">${esc(e.plugin)}</td>` +
    `<td><button data-forget="${esc(e.id)}" data-kind="event">Forget</button></td></tr>`).join('');
}

document.addEventListener('click', async ev => {
  const b = ev.target.closest('button[data-forget]');
  if (!b) return;
  const what = b.dataset.kind === 'event' ? 'this record and everything learned from it' : 'this word record';
  if (!confirm('Forget ' + what + '? It will not come back from older backups.')) return;
  try { await post('/api/memory/forget', {id: b.dataset.forget, kind: b.dataset.kind}); await load(); }
  catch (e) { alert(e.message); }
});
$('#add').addEventListener('submit', async ev => {
  ev.preventDefault();
  try { await post('/api/memory/word', {term: $('#term').value, heard: $('#heard').value}); $('#term').value = $('#heard').value = ''; await load(); }
  catch (e) { alert(e.message); }
});
$('#export').addEventListener('click', async () => {
  try { const r = await post('/api/memory/export'); $('#status').textContent = 'Backup saved: ' + r.path; }
  catch (e) { alert(e.message); }
});
$('#restore').addEventListener('click', async () => {
  const path = prompt('Path of the Scrunch memory backup (.zip) to restore:');
  if (!path) return;
  try { await post('/api/memory/restore', {path}); await load(); $('#status').textContent = 'Restored from ' + path; }
  catch (e) { alert(e.message); }
});

(async () => { token = (await (await fetch('/api/state')).json()).token; await load(); })();
