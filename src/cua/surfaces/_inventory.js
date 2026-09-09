// Element inventory for one frame.
//
// Approximates what an accessibility tree would report, plus one thing a browser AX
// tree does not give us and legacy apps depend on: the caption text sitting in the
// neighbouring table cell. These pages associate no <label for> with their inputs, so
// that caption is the only human-meaningful handle a field has.
() => {
  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : null;
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'hidden') return null;
      if (['submit', 'button', 'reset', 'image'].includes(t)) return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      return 'textbox';
    }
    return null;
  };

  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const accessibleName = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const parts = by.split(/\s+/).map((id) => {
        const n = document.getElementById(id);
        return n ? n.textContent : '';
      });
      if (clean(parts.join(' '))) return clean(parts.join(' '));
    }
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab && clean(lab.textContent)) return clean(lab.textContent);
    }
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (['submit', 'button', 'reset'].includes(t)) return clean(el.value);
    }
    if (tag === 'a' || tag === 'button') return clean(el.textContent);
    return clean(el.getAttribute('title') || el.getAttribute('placeholder') || '');
  };

  // The legacy caption: text of the preceding cell in the same row, falling back to
  // the row's first cell (layouts where several fields share one caption column).
  const captionFor = (el) => {
    const cell = el.closest('td, th');
    if (!cell) return '';
    let prev = cell.previousElementSibling;
    while (prev) {
      const t = clean(prev.textContent);
      if (t) return t;
      prev = prev.previousElementSibling;
    }
    const row = cell.closest('tr');
    if (row && row.cells.length && row.cells[0] !== cell) {
      return clean(row.cells[0].textContent);
    }
    return '';
  };

  const visible = (el) => {
    if (el.getClientRects().length === 0) return false;
    const st = window.getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  };

  const out = [];
  const counters = {};
  for (const el of document.querySelectorAll('a, button, input, select, textarea, [role]')) {
    const role = roleOf(el);
    if (!role) continue;
    counters[role] = (counters[role] || 0) + 1;
    const tag = el.tagName.toLowerCase();
    let value = null;
    if (tag === 'select') {
      value = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : '';
    } else if (tag === 'input' || tag === 'textarea') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      value = ['checkbox', 'radio'].includes(t) ? String(el.checked) : el.value;
    }
    out.push({
      role,
      name: accessibleName(el),
      value,
      label_text: captionFor(el),
      control_id: el.id || el.getAttribute('name') || '',
      text: clean(el.textContent).slice(0, 120),
      enabled: !el.disabled,
      visible: visible(el),
      ordinal: counters[role] - 1,
    });
  }
  return {
    title: document.title,
    text: clean(document.body ? document.body.innerText : '').slice(0, 4000),
    elements: out,
  };
}
