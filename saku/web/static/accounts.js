(() => {
  const form = document.querySelector('.account-form');
  const box = document.querySelector('[data-provider-combobox]');
  if (!form || !box) return;

  const input = box.querySelector('#provider-search');
  const valueInput = box.querySelector('[data-provider-value]');
  const list = box.querySelector('.provider-options');
  const empty = box.querySelector('[data-provider-empty]');
  const help = form.querySelector('[data-provider-help]');
  const kinds = [...form.querySelectorAll('input[name="kind"]')];
  const options = [...list.querySelectorAll('.provider-option')];

  const activeKind = () => kinds.find((radio) => radio.checked)?.value || 'bank';
  const labels = {
    bank: ['Search bank', 'Only bank providers are shown.'],
    ewallet: ['Search e-wallet', 'Only e-wallet providers are shown.'],
    cash: ['Search cash provider', 'Cash providers only.'],
  };
  const visibleOptions = () => options.filter((option) => !option.hidden);
  const normalise = (value) => value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 30);

  function openList() {
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  }

  function closeList() {
    list.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    options.forEach((option) => option.removeAttribute('aria-selected'));
  }

  function filterOptions() {
    const kind = activeKind();
    const query = input.value.trim().toLowerCase();
    let count = 0;
    options.forEach((option) => {
      const matchesKind = option.dataset.kind === kind;
      const matchesQuery = !query || option.dataset.label.includes(query) || option.dataset.provider.includes(query);
      option.hidden = !(matchesKind && matchesQuery);
      if (!option.hidden) count += 1;
    });
    empty.hidden = count !== 0;
  }

  function selectOption(option) {
    input.value = option.dataset.display;
    input.dataset.selectedProvider = option.dataset.provider;
    valueInput.value = option.dataset.provider;
    input.focus({preventScroll: true});
    closeList();
  }

  function syncKind({clear = false} = {}) {
    const [placeholder, hint] = labels[activeKind()];
    input.placeholder = placeholder;
    help.textContent = hint;
    if (clear) {
      input.value = '';
      valueInput.value = '';
      delete input.dataset.selectedProvider;
    }
    filterOptions();
  }

  input.addEventListener('focus', () => { filterOptions(); openList(); });
  input.addEventListener('input', () => {
    delete input.dataset.selectedProvider;
    valueInput.value = normalise(input.value);
    filterOptions();
    openList();
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { closeList(); return; }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      const first = visibleOptions()[0];
      if (first) { first.setAttribute('aria-selected', 'true'); first.focus(); }
    }
  });

  options.forEach((option) => {
    option.addEventListener('click', () => selectOption(option));
    option.addEventListener('keydown', (event) => {
      const visible = visibleOptions();
      const index = visible.indexOf(option);
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectOption(option); }
      if (event.key === 'Escape') { closeList(); input.focus(); }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        visible[(index + step + visible.length) % visible.length]?.focus();
      }
    });
  });

  kinds.forEach((radio) => radio.addEventListener('change', () => syncKind({clear: true})));
  document.addEventListener('click', (event) => { if (!box.contains(event.target)) closeList(); });
  form.addEventListener('submit', () => {
    valueInput.value = input.dataset.selectedProvider || normalise(input.value);
  });

  syncKind();
})();
