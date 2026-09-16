// Progressive enhancement only: the site works without this file.
document.documentElement.classList.add('js');

// Copy buttons on command blocks.
document.addEventListener('click', function (e) {
  var btn = e.target.closest('.copy');
  if (!btn) return;
  var code = btn.parentElement.querySelector('code');
  if (!code || !navigator.clipboard) return;
  navigator.clipboard.writeText(code.innerText.trim()).then(function () {
    btn.textContent = 'Copied';
    btn.classList.add('done');
    setTimeout(function () { btn.textContent = 'Copy'; btn.classList.remove('done'); }, 1500);
  });
});

// Filter selects apply themselves; the Apply button is only for no-JS.
document.querySelectorAll('[data-autosubmit]').forEach(function (sel) {
  sel.addEventListener('change', function () {
    var form = sel.form;
    if (form.requestSubmit) form.requestSubmit(); else form.submit();
  });
});

// "/" focuses the nearest search box, like GitHub.
document.addEventListener('keydown', function (e) {
  if (e.key !== '/' || e.target.matches('input, textarea, select')) return;
  var box = document.querySelector('input[type=search]');
  if (box) { e.preventDefault(); box.focus(); box.select(); }
});

// --- static-catalogue: client-side search/filter/sort/pagination -------------------------
//
// GitHub Pages has no server to run the dynamic /apps query against, so the static build
// (web/build.py) renders every open-source app into the page once, tagged with data-*
// attributes, and this filters/sorts/paginates over that same DOM instead of re-fetching
// anything. The dynamic server (uvicorn) never renders this markup, so this is a no-op there.
(function () {
  var root = document.querySelector('[data-catalogue]');
  if (!root) return;

  var form = root.querySelector('[data-catalogue-form]');
  var q = form.querySelector('#q'), whereSel = form.querySelector('#where'),
      riskSel = form.querySelector('#risk'), trustSel = form.querySelector('#trust'), sortSel = form.querySelector('#sort');
  var grid = root.querySelector('.app-grid');
  var cards = grid ? Array.prototype.slice.call(grid.children) : [];
  var countEl = root.querySelector('[data-count]');
  var emptyEl = root.querySelector('[data-empty]');
  var loadmoreWrap = root.querySelector('[data-loadmore]');
  var loadmoreBtn = root.querySelector('[data-loadmore-btn]');
  var pageSize = parseInt(root.getAttribute('data-page-size'), 10) || 36;
  var shown = pageSize;
  var catLinks = document.querySelectorAll('[data-cat]');

  if (!grid || !cards.length) return;

  function currentCategory() {
    return new URLSearchParams(location.search).get('category') || '';
  }

  function readStateFromUrl() {
    var p = new URLSearchParams(location.search);
    q.value = p.get('q') || '';
    whereSel.value = p.get('where') || '';
    riskSel.value = p.get('risk') || '';
    trustSel.value = p.get('trust') || '';
    sortSel.value = p.get('sort') || 'name';
  }

  function writeStateToUrl(categoryOverride) {
    var p = new URLSearchParams();
    if (q.value.trim()) p.set('q', q.value.trim());
    var cat = categoryOverride !== undefined ? categoryOverride : currentCategory();
    if (cat) p.set('category', cat);
    if (whereSel.value) p.set('where', whereSel.value);
    if (riskSel.value) p.set('risk', riskSel.value);
    if (trustSel.value) p.set('trust', trustSel.value);
    if (sortSel.value && sortSel.value !== 'name') p.set('sort', sortSel.value);
    var qs = p.toString();
    history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
  }

  function matches(card) {
    var needle = q.value.trim().toLowerCase();
    if (needle && (card.dataset.search || '').indexOf(needle) === -1) return false;
    var cat = currentCategory();
    if (cat && (card.dataset.category || '').split(',').indexOf(cat) === -1) return false;
    if (whereSel.value && card.dataset.where !== whereSel.value) return false;
    if (riskSel.value && card.dataset.risk !== riskSel.value) return false;
    if (trustSel.value && trustSel.value.split(',').indexOf(card.dataset.trust) === -1) return false;
    return true;
  }

  var SORTERS = {
    name: function (a, b) { return a.dataset.name.localeCompare(b.dataset.name); },
    stars: function (a, b) { return (+b.dataset.stars || 0) - (+a.dataset.stars || 0); },
    updated: function (a, b) { return (Date.parse(b.dataset.updated) || 0) - (Date.parse(a.dataset.updated) || 0); },
    newest: function (a, b) { return (Date.parse(b.dataset.created) || 0) - (Date.parse(a.dataset.created) || 0); },
  };

  function render() {
    var visible = cards.filter(matches).sort(SORTERS[sortSel.value] || SORTERS.name);
    var toShow = visible.slice(0, shown);
    var wanted = {};
    toShow.forEach(function (c) { wanted[c.dataset.id] = true; grid.appendChild(c); });
    cards.forEach(function (c) { c.hidden = !wanted[c.dataset.id]; });

    var cat = currentCategory();
    countEl.textContent = visible.length.toLocaleString() + ' app' + (visible.length === 1 ? '' : 's') +
      (q.value.trim() ? ' matching “' + q.value.trim() + '”' : '') + (cat ? ' in ' + cat : '');
    emptyEl.hidden = visible.length !== 0;

    var remaining = visible.length - toShow.length;
    loadmoreWrap.hidden = remaining <= 0;
    if (remaining > 0) loadmoreBtn.textContent = 'Load ' + Math.min(pageSize, remaining) + ' more (' + remaining + ' left)';

    catLinks.forEach(function (a) {
      if ((a.getAttribute('data-cat') || '') === cat) a.setAttribute('aria-current', 'true');
      else a.removeAttribute('aria-current');
    });
  }

  function onFilterChange() {
    shown = pageSize;
    writeStateToUrl();
    render();
  }

  function debounce(fn, ms) {
    var t;
    return function () {
      var args = arguments;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(null, args); }, ms);
    };
  }

  q.addEventListener('input', debounce(onFilterChange, 200));
  [whereSel, riskSel, trustSel, sortSel].forEach(function (el) { el.addEventListener('change', onFilterChange); });
  form.addEventListener('submit', function (e) { e.preventDefault(); onFilterChange(); });

  catLinks.forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      shown = pageSize;
      writeStateToUrl(a.getAttribute('data-cat') || '');
      render();
      root.scrollIntoView({ block: 'start', behavior: 'smooth' });
    });
  });

  loadmoreBtn.addEventListener('click', function () {
    shown += pageSize;
    render();
  });

  readStateFromUrl();
  render();
})();
