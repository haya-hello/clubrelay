// 仅提示未保存编辑，不自动提交。 / Warn about unsaved edits without submitting automatically.
(() => {
  const editor = document.getElementById('handoff-editor');
  if (!editor) return;
  let dirty = false;
  editor.addEventListener('input', () => { dirty = true; });
  editor.addEventListener('submit', () => { dirty = false; });
  window.addEventListener('beforeunload', event => {
    if (dirty) { event.preventDefault(); event.returnValue = ''; }
  });
  document.querySelectorAll('form').forEach(form => {
    if (form !== editor) form.addEventListener('submit', event => {
      if (dirty) { event.preventDefault(); alert('请先保存当前条目的修改，再进行确认或其他操作。'); }
    });
  });
})();
