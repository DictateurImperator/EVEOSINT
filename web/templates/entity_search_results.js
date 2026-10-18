// Shared renderer for top-bar navigation and in-page entity selection.
window.EVEOSINTEntitySearch = Object.freeze({
  render(container, items, onSelect = null) {
    container.replaceChildren();
    if (!items.length) {const empty=document.createElement('div');empty.className='px-3 py-2 muted-line small';empty.textContent='No result';container.append(empty);return;}
    const sections = new Map();
    items.forEach((item, index) => {
      const section = item.section || String(item.entity_type || 'ENTITY').toUpperCase();
      if (!sections.has(section)) sections.set(section, []);
      sections.get(section).push({item, index});
    });
    for (const [title, rows] of sections) {
      const section = document.createElement('section');
      section.className = 'topbar-entity-search-section';
      const heading = document.createElement('div');
      heading.className = 'topbar-entity-search-section-title';
      heading.textContent = title;
      const grid = document.createElement('div');
      grid.className = 'topbar-entity-search-grid';
      for (const {item, index} of rows) {
        const result = document.createElement(onSelect ? 'button' : 'a');
        result.className = 'topbar-entity-search-result';
        result.dataset.index = index;
        if (onSelect) {
          result.type = 'button';
          result.addEventListener('click', () => onSelect(item));
        } else result.href = item.url;
        if (item.image_url) {
          const image = document.createElement('img');
          image.src = item.image_url; image.alt = ''; image.loading = 'lazy';
          image.onerror = () => { image.style.display = 'none'; };
          result.append(image);
        } else {
          const icon = document.createElement('span');
          icon.className = 'topbar-entity-search-fallback-icon ' + (item.entity_type || 'entity');
          icon.setAttribute('aria-hidden', 'true');
          result.append(icon);
        }
        const text = document.createElement('span');
        text.className = 'topbar-entity-search-text';
        const name = document.createElement('span');
        name.className = 'topbar-entity-search-name';
        name.textContent = item.label || item.name || 'Unknown';
        const subtitle = document.createElement('span');
        subtitle.className = 'topbar-entity-search-type';
        subtitle.textContent = item.subtitle || item.entity_type || 'Entity';
        text.append(name, subtitle); result.append(text); grid.append(result);
      }
      section.append(heading, grid); container.append(section);
    }
  }
});
