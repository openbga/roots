() => {
  // Panel spacing (docs/ui-standards.md section 12): visible text and controls inside a panel sit at
  // least --vr-panel-inset from the panel's edge, stacked blocks in a panel body are at least
  // --vr-stack apart, and key facts start on their grid's track lines. Returns the offenders; an
  // empty list passes. Reused by qa on any page: page.evaluate(open("e2e/panel_spacing.js").read()).
  const root = getComputedStyle(document.documentElement);
  const rem = parseFloat(root.fontSize);
  const token = name => parseFloat(root.getPropertyValue(name)) * rem;
  const inset = token('--vr-panel-inset');
  const stack = token('--vr-stack');
  const tolerance = 1;
  const visible = el => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' &&
      !el.closest('.visually-hidden, .visually-hidden-focusable, [hidden], details:not([open]) > :not(summary)');
  };
  // The part of the page an element is actually shown in: clipped by scrolling ancestors.
  const shownIn = el => {
    let r = el.getBoundingClientRect();
    let box = {left: r.left, right: r.right, top: r.top, bottom: r.bottom};
    for (let a = el.parentElement; a; a = a.parentElement) {
      const s = getComputedStyle(a);
      if (/(auto|scroll|hidden|clip)/.test(s.overflowX + s.overflowY)) {
        const c = a.getBoundingClientRect();
        box = {left: Math.max(box.left, c.left), right: Math.min(box.right, c.right),
               top: Math.max(box.top, c.top), bottom: Math.min(box.bottom, c.bottom)};
      }
    }
    return box;
  };
  const label = el => {
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
    return `${el.tagName.toLowerCase()}${el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).join('.') : ''} "${text.slice(0, 40)}"`;
  };
  const problems = [];
  for (const card of document.querySelectorAll('.card')) {
    if (!visible(card) || card.closest('.modal:not(.show), .dropdown-menu:not(.show)')) continue;
    const c = card.getBoundingClientRect();
    // Text runs (measured as rendered glyph boxes) and controls.
    const boxes = [];
    const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT, {
      acceptNode: n => n.textContent.trim() && n.parentElement && visible(n.parentElement) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT,
    });
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const range = document.createRange();
      range.selectNodeContents(n);
      for (const r of range.getClientRects()) {
        if (r.width && r.height) boxes.push([n.parentElement, r]);
      }
    }
    for (const control of card.querySelectorAll('input:not([type=hidden]), select, textarea, button, .btn, .form-check-input, .ts-control')) {
      if (visible(control)) boxes.push([control, control.getBoundingClientRect()]);
    }
    for (const [el, r] of boxes) {
      const shown = shownIn(el);
      // Only what is actually visible: a box cut by a scrolling table is an overflow, not spacing.
      if (r.left < shown.left - 0.5 || r.right > shown.right + 0.5 || r.top < shown.top - 0.5 || r.bottom > shown.bottom + 0.5) continue;
      // Inside a table that scrolls sideways (an entry grid) any column may sit at the scroll edge.
      const scroller = el.closest('.vr-table-wrap');
      if (scroller && scroller.scrollWidth > scroller.clientWidth + 1) continue;
      const gap = Math.min(r.left - c.left, c.right - r.right);
      if (gap < inset - tolerance) {
        problems.push(`inset ${gap.toFixed(1)}px < ${inset}px: ${label(el)} in ${label(card).slice(0, 60)}`);
      }
    }
    // Stack: consecutive visible blocks in a panel body.
    for (const body of card.querySelectorAll('.vr-panel-body')) {
      if (!visible(body)) continue;
      const kids = [...body.children].filter(k => visible(k) && getComputedStyle(k).position !== 'absolute');
      for (let i = 1; i < kids.length; i++) {
        const gap = kids[i].getBoundingClientRect().top - kids[i - 1].getBoundingClientRect().bottom;
        // Blocks side by side on one line (a button and its note) are not a stack.
        if (gap < 0) continue;
        if (gap < stack - tolerance) {
          problems.push(`stack ${gap.toFixed(1)}px < ${stack}px: ${label(kids[i - 1]).slice(0, 50)} → ${label(kids[i]).slice(0, 50)}`);
        }
      }
    }
  }
  // Key facts (section 12): every fact starts on one of its grid's track lines, so labels and values
  // line up from row to row.
  for (const dl of document.querySelectorAll('dl.vr-facts')) {
    if (!visible(dl) || dl.closest('.alert')) continue;
    const style = getComputedStyle(dl);
    const tracks = style.gridTemplateColumns.split(' ').map(parseFloat);
    const gap = parseFloat(style.columnGap) || 0;
    const origin = dl.getBoundingClientRect().left + parseFloat(style.paddingLeft);
    const lines = tracks.map((_, i) => origin + tracks.slice(0, i).reduce((sum, w) => sum + w + gap, 0));
    for (const fact of dl.children) {
      if (!visible(fact)) continue;
      const left = fact.getBoundingClientRect().left;
      if (!lines.some(line => Math.abs(line - left) <= tolerance)) {
        problems.push(`fact off the grid at ${left.toFixed(1)}px: ${label(fact).slice(0, 60)}`);
      }
    }
  }
  return {inset, stack, problems: [...new Set(problems)]};
}
