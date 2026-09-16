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
