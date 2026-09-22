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
// Forms marked data-ajax are enhanced further down (fetch instead of a full
// reload), so leave those alone here rather than doing both.
document.querySelectorAll('[data-autosubmit]').forEach(function (sel) {
  sel.addEventListener('change', function () {
    var form = sel.form;
    if (form && form.hasAttribute('data-ajax')) return;
    if (form.requestSubmit) form.requestSubmit(); else form.submit();
  });
});

// "/" focuses the nearest search box, like GitHub.
document.addEventListener('keydown', function (e) {
  if (e.key !== '/' || e.target.matches('input, textarea, select')) return;
  var box = document.querySelector('input[type=search]');
  if (box) { e.preventDefault(); box.focus(); box.select(); }
});

// Screenshot lightbox: click a thumbnail to view it enlarged in place instead of
// opening the raw image in a new tab.
(function () {
  var gallery = document.querySelector('[data-lightbox]');
  var overlay = document.querySelector('[data-lightbox-overlay]');
  if (!gallery || !overlay) return;
  var links = Array.prototype.slice.call(gallery.querySelectorAll('a'));
  var img = overlay.querySelector('.lightbox-img');
  var closeBtn = overlay.querySelector('.lightbox-close');
  var prevBtn = overlay.querySelector('.lightbox-nav.prev');
  var nextBtn = overlay.querySelector('.lightbox-nav.next');
  if (!links.length || !img) return;
  var index = 0;
  var trigger = null;

  function show(i) {
    index = (i + links.length) % links.length;
    img.src = links[index].getAttribute('href');
    var thumb = links[index].querySelector('img');
    img.alt = thumb ? thumb.alt : '';
    var multi = links.length > 1;
    prevBtn.hidden = nextBtn.hidden = !multi;
  }

  // Tab/Shift+Tab cycles only through the lightbox's own controls while it's
  // open, instead of escaping into the (still-focusable) page behind it.
  function focusable() {
    return [closeBtn, prevBtn, nextBtn].filter(function (el) { return el && !el.hidden; });
  }

  function open(i, fromEl) {
    trigger = fromEl || document.activeElement;
    show(i);
    overlay.hidden = false;
    document.body.style.overflow = 'hidden';
    closeBtn.focus();
  }

  function close() {
    overlay.hidden = true;
    document.body.style.overflow = '';
    if (trigger && trigger.focus) trigger.focus();
  }

  links.forEach(function (a, i) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      open(i, a);
    });
  });

  closeBtn.addEventListener('click', close);
  prevBtn.addEventListener('click', function () { show(index - 1); });
  nextBtn.addEventListener('click', function () { show(index + 1); });
  overlay.addEventListener('click', function (e) {
    if (e.target === overlay) close();
  });
  document.addEventListener('keydown', function (e) {
    if (overlay.hidden) return;
    if (e.key === 'Escape') { close(); return; }
    if (e.key === 'ArrowLeft') { show(index - 1); return; }
    if (e.key === 'ArrowRight') { show(index + 1); return; }
    if (e.key !== 'Tab') return;
    var items = focusable();
    if (!items.length) return;
    var first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
})();

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
      riskSel = form.querySelector('#risk'), trustSel = form.querySelector('#trust'),
      maintenanceSel = form.querySelector('#maintenance'), sortSel = form.querySelector('#sort');
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
    maintenanceSel.value = p.get('maintenance') || '';
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
    if (maintenanceSel.value) p.set('maintenance', maintenanceSel.value);
    if (sortSel.value && sortSel.value !== 'name') p.set('sort', sortSel.value);
    var qs = p.toString();
    history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
  }

  function matches(card) {
    var needle = q.value.trim().toLowerCase();
    if (needle) {
      // Each word must appear somewhere in the card's text, so word order doesn't
      // matter: "calculator gnome" finds "GNOME Calculator" same as the reverse.
      var hay = card.dataset.search || '';
      var words = needle.split(/\s+/);
      for (var i = 0; i < words.length; i++) {
        if (hay.indexOf(words[i]) === -1) return false;
      }
    }
    var cat = currentCategory();
    if (cat && (card.dataset.category || '').split(',').indexOf(cat) === -1) return false;
    if (whereSel.value && card.dataset.where !== whereSel.value) return false;
    if (riskSel.value && card.dataset.risk !== riskSel.value) return false;
    if (trustSel.value && trustSel.value.split(',').indexOf(card.dataset.trust) === -1) return false;
    if (maintenanceSel.value && maintenanceSel.value.split(',').indexOf(card.dataset.maintenance) === -1) return false;
    return true;
  }

  // A score that rewards each search word appearing in the name (most) or the id
  // (some), plus a bonus for an exact full-phrase name match - summed across every
  // word, so "gnome calculator" and "calculator gnome" score GNOME Calculator the
  // same regardless of order or which field actually carried a given word. Mirrors
  // catalogue._relevance() server-side so both search paths rank the same way.
  function relevance(card, words, needle) {
    var name = (card.dataset.name || '').toLowerCase();
    var id = (card.dataset.id || '').toLowerCase();
    var score = name === needle ? 1000 : 0;
    for (var i = 0; i < words.length; i++) {
      if (name.indexOf(words[i]) !== -1) score += 10;
      if (id.indexOf(words[i]) !== -1) score += 3;
    }
    return score;
  }

  var SORTERS = {
    name: function (a, b) { return a.dataset.name.localeCompare(b.dataset.name); },
    stars: function (a, b) { return (+b.dataset.stars || 0) - (+a.dataset.stars || 0); },
    updated: function (a, b) { return (Date.parse(b.dataset.updated) || 0) - (Date.parse(a.dataset.updated) || 0); },
    newest: function (a, b) { return (Date.parse(b.dataset.created) || 0) - (Date.parse(a.dataset.created) || 0); },
  };

  function render() {
    var needle = q.value.trim().toLowerCase();
    var words = needle ? needle.split(/\s+/) : [];
    var sorter = SORTERS[sortSel.value] || SORTERS.name;
    // "name" is the default sort, not something anyone picks *for a search* - once
    // there's a query, relevance beats alphabetical unless stars/date was chosen.
    if (needle && sortSel.value === 'name') {
      sorter = function (a, b) {
        var r = relevance(b, words, needle) - relevance(a, words, needle);
        return r !== 0 ? r : (+b.dataset.stars || 0) - (+a.dataset.stars || 0);
      };
    }
    var visible = cards.filter(matches).sort(sorter);
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
  [whereSel, riskSel, trustSel, maintenanceSel, sortSel].forEach(function (el) { el.addEventListener('change', onFilterChange); });
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

// --- live catalogue: fetch-based filtering with a loading indicator -----------------
//
// Unlike the static build above, the dynamic server runs a real database query for
// every /apps request - a network wait worth showing something for. Progressively
// enhances the same data-ajax form: without JS (or if a fetch fails) it is a plain GET
// form/link, working exactly as it did before this existed.
(function () {
  var form = document.getElementById('filters');
  if (!form || !form.hasAttribute('data-ajax') || document.querySelector('[data-catalogue]')) return;

  var radar = document.querySelector('[data-radar]');
  var body = document.getElementById('results-body');
  if (!body) return;

  var FIELDS = { q: '', where: '', risk: '', trust: '', maintenance: '', sort: 'name' };
  var inflight = null;

  function urlFromForm() {
    var params = new URLSearchParams(new FormData(form));
    Array.from(params.keys()).forEach(function (k) { if (!params.get(k)) params.delete(k); });
    var qs = params.toString();
    return form.getAttribute('action') + (qs ? '?' + qs : '');
  }

  // Keeps the toolbar itself in sync after a swap triggered by something other than
  // the toolbar (a category or pagination link, browser back/forward) - otherwise the
  // search box and dropdowns would silently show stale state once things are found.
  function syncFormFromUrl(url) {
    var params = new URL(url, location.origin).searchParams;
    Object.keys(FIELDS).forEach(function (name) {
      var el = form.querySelector('#' + name);
      if (el) el.value = params.get(name) || FIELDS[name];
    });
    var cat = params.get('category') || '';
    var catInput = form.querySelector('input[name=category]');
    if (cat) {
      if (!catInput) {
        catInput = document.createElement('input');
        catInput.type = 'hidden';
        catInput.name = 'category';
        form.appendChild(catInput);
      }
      catInput.value = cat;
    } else if (catInput) {
      catInput.remove();
    }
  }

  function go(url, push) {
    if (inflight) inflight.abort();
    var controller = new AbortController();
    inflight = controller;
    if (radar) radar.hidden = false;
    body.classList.add('is-loading');

    fetch(url, { signal: controller.signal })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var newBody = doc.getElementById('results-body');
        if (!newBody) throw new Error('no results-body in response');
        body.innerHTML = newBody.innerHTML;
        if (doc.title) document.title = doc.title;
        syncFormFromUrl(url);
        if (push) history.pushState({ ajax: true }, '', url);
      })
      .catch(function (err) {
        if (err.name === 'AbortError') return; // superseded by a newer request
        location.href = url; // something went wrong - fall back to a real navigation
      })
      .finally(function () {
        if (inflight === controller) {
          inflight = null;
          if (radar) radar.hidden = true;
          body.classList.remove('is-loading');
        }
      });
  }

  function debounce(fn, ms) {
    var t;
    return function () { clearTimeout(t); t = setTimeout(fn, ms); };
  }

  var onChange = function () { go(urlFromForm(), true); };
  form.querySelectorAll('[data-autosubmit]').forEach(function (sel) { sel.addEventListener('change', onChange); });
  var q = form.querySelector('#q');
  if (q) q.addEventListener('input', debounce(onChange, 250));
  form.addEventListener('submit', function (e) { e.preventDefault(); onChange(); });

  // Category and pagination links live inside elements that #results-body replaces
  // wholesale on every swap, so delegate from the document instead of binding
  // directly to nodes that stop existing after the first fetch.
  document.addEventListener('click', function (e) {
    var a = e.target.closest('.cats a, .pager a, #results-body .count a');
    if (!a) return;
    e.preventDefault();
    go(a.href, true);
  });

  window.addEventListener('popstate', function () {
    go(location.pathname + location.search, false);
  });
})();
