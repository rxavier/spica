// The artist list under a station is read-only, but its "exclude" buttons write
// into the form's never-play-these-artists dropdown, so the form is the one
// source of truth and saving applies exactly what the list shows.
(function () {
  function multiselectFor(field) {
    return [...document.querySelectorAll('[data-multiselect]')].find(
      (box) => box.querySelector('input[name="' + field + '"]')
    );
  }

  const excludeBox = multiselectFor('exclude_artists');
  const includeBox = multiselectFor('include_artists');

  function checkboxFor(box, name) {
    if (!box) return null;
    return [...box.querySelectorAll('input[type="checkbox"]')].find(
      (input) => input.value === name
    );
  }

  function ensureCheckbox(box, name) {
    const found = checkboxFor(box, name);
    if (found || !box) return found;
    const panel = box.querySelector('.ms-panel');
    if (!panel) return null;
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.name = 'exclude_artists';
    input.value = name;
    label.append(input, document.createTextNode(' ' + name));
    const clear = panel.querySelector('.ms-clear');
    panel.insertBefore(label, clear || null);
    return input;
  }

  function excludedNames() {
    if (!excludeBox) return new Set();
    return new Set(
      [...excludeBox.querySelectorAll('input[name="exclude_artists"]:checked')]
        .map((input) => input.value)
    );
  }

  function mark() {
    const excluded = excludedNames();
    document.querySelectorAll('#station-artist-results [data-exclude-artist]').forEach(
      (button) => {
        const on = excluded.has(button.dataset.excludeArtist);
        button.disabled = on;
        button.textContent = on ? 'excluded' : 'exclude';
      }
    );
  }

  function exclude(name) {
    const box = ensureCheckbox(excludeBox, name);
    if (!box) return;
    if (!box.checked) {
      box.checked = true;
      box.dispatchEvent(new Event('change', { bubbles: true }));
    }
    // Never-play wins over play, so a play checkbox for the same name would only
    // make the form say two things at once.
    const include = checkboxFor(includeBox, name);
    if (include && include.checked) {
      include.checked = false;
      include.dispatchEvent(new Event('change', { bubbles: true }));
    }
    mark();
  }

  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-exclude-artist]');
    if (button) exclude(button.dataset.excludeArtist);
  });

  document.body.addEventListener('htmx:afterSwap', mark);
  if (excludeBox) excludeBox.addEventListener('change', mark);
  mark();
})();
