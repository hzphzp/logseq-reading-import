const byId = id => document.getElementById(id);
const storage = () => logseq.FileStorage;
let preview, pending, graph, reading = false, starting = false;
async function read(path) {
  const raw = await storage().getItem(path);
  return raw ? JSON.parse(raw) : null;
}
async function helper() {
  if (!await storage().hasItem('helper.json')) return null;
  const status = await read('helper.json');
  return status && Date.now() - status.at < 8000 ? status.session : null;
}
function buttons() {
  byId('preview').disabled = (starting || Boolean(pending)) || !graph;
  byId('apply').disabled = (starting || Boolean(pending)) || !preview || preview.repo !== graph?.url;
}
function notice(text) { byId('message').textContent = text; }
function clearPreview() {
  preview = null;
  byId('counts').hidden = true;
}
async function open() {
  graph = await logseq.App.getCurrentGraph();
  if (preview?.repo !== graph?.url) clearPreview();
  byId('graph').textContent = graph ? '图谱 · ' + (graph.name || graph.url).replace(/^logseq_db_/, '') : '请先打开数据库图谱';
  if (!pending && !preview) notice('先预览新增的书籍、划线和想法，再决定是否导入。');
  buttons();
  logseq.showMainUI();
}
async function start(action) {
  if (starting || pending) return;
  starting = true;
  buttons();
  try { await request(action); }
  finally { starting = false; buttons(); }
}
async function request(action) {
  const active = await logseq.App.getCurrentGraph();
  if (!active?.url || graph?.url !== active.url) { await open(); return; }
  const session = await helper();
  if (!session) { notice('请先启动本机助手，再点预览。'); byId('setup').open = true; return; }
  const previous = preview;
  if (action === 'apply' && (!previous || previous.session !== session || previous.repo !== active.url)) {
    clearPreview(); buttons(); notice('助手或图谱已切换，请重新预览。'); return;
  }
  const task = { id: crypto.randomUUID(), action, repo: active.url, session, at: Date.now() };
  pending = task;
  clearPreview(); buttons();
  try {
    await storage().setItem('requests/' + task.id + '.json', JSON.stringify({...task, preview: previous?.id}));
  } catch (error) { pending = null; buttons(); throw error; }
  notice(action === 'apply' ? '正在备份图谱并导入所选预览…' : '正在读取微信读书并核对图谱…');
}
async function poll() {
  if (!pending || reading) return;
  reading = true;
  try {
    const task = pending, path = 'results/' + task.id + '.json';
    if (!await storage().hasItem(path)) {
      if (Date.now() - task.at > 10000) {
        const status = await read('helper.json').catch(() => null);
        if (!status || status.session !== task.session || Date.now() - status.at > 15000) {
          pending = null; notice('助手连接中断。请查看终端，重新预览以核对实际写入结果。'); buttons();
        }
      }
      return;
    }
    const result = await read(path);
    const active = await logseq.App.getCurrentGraph();
    pending = null;
    if (active?.url !== task.repo) { await open(); return; }
    if (!result.ok) { notice(result.message); return; }
    const values = ['new_books','new_highlights','new_reviews'].map(k => result.counts[k]);
    byId('counts').querySelectorAll('dd').forEach((el,i) => { el.textContent = values[i]; });
    byId('counts').hidden = false;
    if (task.action === 'preview') {
      preview = task;
      notice('预览完成。确认后将备份图谱并导入。');
    } else { notice('导入完成，已核对图谱记录。'); }
    await storage().removeItem(path);
  } finally { reading = false; buttons(); }
}
const report = fn => () => fn().catch(error => notice(String(error?.message || error)));
logseq.ready(() => {
  logseq.App.registerCommandPalette({key:'weread-import',label:'微信读书 · WeRead Notes Import'}, report(open));
  logseq.Editor.registerSlashCommand('微信读书 · WeRead Notes Import', report(open));
  byId('preview').onclick = report(() => start('preview'));
  byId('apply').onclick = report(() => start('apply'));
  byId('close').onclick = () => logseq.hideMainUI();
  document.addEventListener('keydown', e => { if (e.key === 'Escape') logseq.hideMainUI(); });
  logseq.App.onCurrentGraphChanged(() => { clearPreview(); graph = null; buttons(); logseq.hideMainUI(); });
  const timer = setInterval(report(poll),1000);
  logseq.beforeunload(() => clearInterval(timer));
}).catch(console.error);
