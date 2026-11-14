const exportForm = document.querySelector('[data-report-export]');

if (exportForm) {
  const copyButton = exportForm.querySelector('[data-copy-reports]');
  const downloadButton = exportForm.querySelector('[data-download-reports]');
  const selectAll = exportForm.querySelector('[data-select-all]');
  const count = exportForm.querySelector('[data-selected-count]');
  const status = exportForm.querySelector('[data-export-status]');
  const fallback = exportForm.querySelector('[data-copy-fallback]');
  const textArea = fallback.querySelector('textarea');
  const checkboxes = [...exportForm.elements].filter(
    (field) => field.name === 'id' && field.type === 'checkbox',
  );
  let copying = false;

  const updateSelection = () => {
    const selected = checkboxes.filter((field) => field.checked).length;
    if (count) count.textContent = `已選 ${selected} 筆`;
    if (selectAll) {
      selectAll.checked = selected === checkboxes.length;
      selectAll.indeterminate = selected > 0 && selected < checkboxes.length;
    }
    const empty = !new FormData(exportForm).getAll('id').length;
    if (copyButton) copyButton.disabled = copying || empty;
    for (const button of exportForm.querySelectorAll('[data-selection-action]')) {
      button.disabled = empty;
    }
    if (downloadButton) downloadButton.disabled = empty;
    for (const menu of exportForm.querySelectorAll('[data-selection-menu]')) {
      menu.hidden = empty;
      menu.querySelector('summary').setAttribute('aria-disabled', String(empty));
      if (empty) menu.open = false;
    }
  };

  for (const menu of exportForm.querySelectorAll('[data-selection-menu]')) {
    menu.querySelector('summary').addEventListener('click', (event) => {
      if (menu.querySelector('summary').getAttribute('aria-disabled') === 'true') {
        event.preventDefault();
      } else {
        for (const other of exportForm.querySelectorAll('[data-selection-menu]')) {
          if (other !== menu) other.open = false;
        }
      }
    });
    menu.addEventListener('click', (event) => {
      if (event.target.closest('button')) menu.open = false;
    });
  }

  document.addEventListener('click', (event) => {
    for (const menu of exportForm.querySelectorAll('[data-selection-menu]')) {
      if (!menu.contains(event.target)) menu.open = false;
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    for (const menu of exportForm.querySelectorAll('[data-selection-menu][open]')) {
      menu.open = false;
      menu.querySelector('summary').focus();
    }
  });

  const clearFeedback = () => {
    status.textContent = '';
    fallback.hidden = true;
    textArea.value = '';
    updateSelection();
  };

  for (const checkbox of checkboxes) checkbox.addEventListener('change', clearFeedback);
  selectAll?.addEventListener('change', () => {
    for (const checkbox of checkboxes) checkbox.checked = selectAll.checked;
    clearFeedback();
  });
  // Browsers may restore checkbox values on history navigation; selection is page-local.
  window.addEventListener('pageshow', () => {
    for (const checkbox of checkboxes) checkbox.checked = false;
    clearFeedback();
  });

  copyButton?.addEventListener('click', async () => {
    const ids = new FormData(exportForm).getAll('id');
    if (!ids.length || copying) return;
    copying = true;
    fallback.hidden = true;
    status.textContent = '正在準備問題資料…';
    updateSelection();
    try {
      const url = new URL(exportForm.getAttribute('action'), window.location.href);
      for (const id of ids) url.searchParams.append('id', id);
      const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok || !response.headers.get('Content-Type')?.startsWith('text/markdown')) {
        status.textContent = '無法取得問題資料，請確認登入狀態後重試。';
        return;
      }
      const text = await response.text();
      try {
        await navigator.clipboard.writeText(text);
        status.textContent = `已複製 ${ids.length} 筆，可貼到 Codex。`;
      } catch {
        textArea.value = text;
        fallback.hidden = false;
        textArea.focus();
        textArea.select();
        status.textContent = '瀏覽器未允許自動複製，已準備好手動複製文字。';
      }
    } catch {
      status.textContent = '連線失敗，請稍後重試。';
    } finally {
      copying = false;
      updateSelection();
    }
  });
  updateSelection();
}
