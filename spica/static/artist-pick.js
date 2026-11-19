(function () {
  function selectedInputs(pick) {
    return [...pick.querySelectorAll('input[type="hidden"]')];
  }

  function selectedSet(pick) {
    return new Set(selectedInputs(pick).map((input) => input.value));
  }

  function mark(pick) {
    if (!pick) return;
    const chosen = selectedSet(pick);
    pick.querySelectorAll('[data-artist-add]').forEach((button) => {
      const on = chosen.has(button.dataset.name);
      button.disabled = on;
      button.textContent = on ? 'added' : 'add';
    });
    const empty = pick.querySelector('[data-artist-empty]');
    if (empty) empty.hidden = chosen.size > 0;
  }

  function remove(pick, name) {
    if (!pick || !name) return;
    [...pick.querySelectorAll('.artist-chip')]
      .filter((chip) => chip.dataset.name === name)
      .forEach((chip) => chip.remove());
    selectedInputs(pick)
      .filter((input) => input.value === name)
      .forEach((input) => input.remove());
    mark(pick);
  }

  function otherField(field) {
    return field === 'include_artists' ? 'exclude_artists' : 'include_artists';
  }

  function add(pick, name) {
    if (!pick || !name || selectedSet(pick).has(name)) return;
    const box = pick.querySelector('[data-artist-selected]');
    const chip = document.createElement('span');
    chip.className = 'chip artist-chip';
    chip.dataset.name = name;
    chip.append(document.createTextNode(name + ' '));
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'artist-remove';
    button.dataset.artistRemove = '';
    button.setAttribute('aria-label', 'remove ' + name);
    button.textContent = '×';
    chip.append(button);

    const hidden = document.createElement('input');
    hidden.type = 'hidden';
    hidden.name = pick.dataset.field;
    hidden.value = name;
    box.append(chip, hidden);

    // An exclusion wins over an inclusion, so keeping the two lists disjoint
    // makes the form say what the station will do.
    const other = document.querySelector(
      '[data-artist-pick][data-field="' + otherField(pick.dataset.field) + '"]'
    );
    if (other) remove(other, name);
    mark(pick);
  }

  document.addEventListener('click', (event) => {
    const addButton = event.target.closest('[data-artist-add]');
    if (addButton) {
      add(addButton.closest('[data-artist-pick]'), addButton.dataset.name);
      return;
    }
    const removeButton = event.target.closest('[data-artist-remove]');
    if (removeButton) {
      const chip = removeButton.closest('.artist-chip');
      remove(removeButton.closest('[data-artist-pick]'), chip ? chip.dataset.name : '');
    }
  });

  // Search and pagination replace the result list; mark whatever is now on
  // screen against the hidden selection.
  document.body.addEventListener('htmx:afterSwap', (event) => {
    const pick = event.target.closest('[data-artist-pick]');
    if (pick) mark(pick);
  });

  document.querySelectorAll('[data-artist-pick]').forEach(mark);
})();
