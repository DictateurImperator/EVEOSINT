    document.addEventListener("DOMContentLoaded", () => {
      const root = document.querySelector("[data-entity-search]");
      if (!root) return;

      const input = root.querySelector("[data-entity-search-input]");
      const resultsBox = root.querySelector("[data-entity-search-results]");
      if (!input || !resultsBox) return;

      let debounceTimer = null;
      let lastResults = [];
      let selectedIndex = -1;
      let lastQueryId = 0;

      const escapeHtml = (value) => String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");

      const showBox = () => resultsBox.classList.add("show");
      const hideBox = () => resultsBox.classList.remove("show");

      const clearResults = () => {
        lastResults = [];
        selectedIndex = -1;
        resultsBox.innerHTML = "";
        hideBox();
      };

      const normalizeResults = (payload) => {
        const raw = payload && Array.isArray(payload.results) ? payload.results : [];
        return raw
          .map((item) => ({
            label: item.label || item.name || "Unknown",
            subtitle: item.subtitle || item.entity_type || "Entity",
            url: item.url || "",
            image_url: item.image_url || "",
            entity_type: item.entity_type || "entity",
            section: item.section || String(item.entity_type || "ENTITY").toUpperCase(),
          }))
          .filter((item) => item.url);
      };

      const renderResults = (items) => {
        lastResults = items || [];
        selectedIndex = -1;

        if (!lastResults.length) {
          resultsBox.innerHTML = '<div class="px-3 py-2 muted-line small">No result</div>';
          showBox();
          return;
        }

        window.EVEOSINTEntitySearch.render(resultsBox, lastResults);

        resultsBox.querySelectorAll(".topbar-entity-search-result").forEach((node) => {
          node.addEventListener("mousedown", (event) => {
            event.preventDefault();
            window.location.href = node.getAttribute("href");
          });
        });

        showBox();
      };

      const setActive = () => {
        resultsBox.querySelectorAll(".topbar-entity-search-result").forEach((node, index) => {
          node.classList.toggle("active", index === selectedIndex);
        });
      };

      const searchEntities = async (limit) => {
        const q = input.value.trim();
        if (!q) {
          clearResults();
          return [];
        }

        const queryId = ++lastQueryId;
        const path = `/api/entity-search?q=${encodeURIComponent(q)}&limit=${encodeURIComponent(limit)}&scope=global`;
        const url = {{ public_search|default(false)|tojson }} ? '/public/read?'+new URLSearchParams({source:{{ public_search_source|default('/killboard')|tojson }},path}) : path;

        try {
          const response = await fetch(url, {
            method: "GET",
            headers: {"Accept": "application/json"},
            credentials: "same-origin",
            cache: {{ ('default' if public_search else 'no-store')|tojson }},
          });

          if (queryId !== lastQueryId) return [];

          const text = await response.text();
          if (!response.ok) {
            resultsBox.innerHTML = '<div class="px-3 py-2 text-danger small">Erreur 500</div>';
            showBox();
            return [];
          }

          let payload = {};
          try {
            payload = JSON.parse(text || "{}");
          } catch (error) {
            resultsBox.innerHTML = '<div class="px-3 py-2 text-danger small">Erreur 500</div>';
            showBox();
            return [];
          }

          const items = normalizeResults(payload);
          return items;
        } catch (error) {
          if (queryId !== lastQueryId) return [];
          resultsBox.innerHTML = '<div class="px-3 py-2 text-danger small">Erreur 500</div>';
          showBox();
          return [];
        }
      };

      const refreshSuggestions = async () => {
        const items = await searchEntities(6);
        renderResults(items);
      };

      const scheduleSuggestions = () => {
        window.clearTimeout(debounceTimer);
        if (!input.value.trim()) {
          clearResults();
          return;
        }
        debounceTimer = window.setTimeout(refreshSuggestions, 250);
      };

      const goToSelectedOrFirst = async () => {
        window.clearTimeout(debounceTimer);

        if (lastResults.length) {
          const index = selectedIndex >= 0 ? selectedIndex : 0;
          window.location.href = lastResults[index].url;
          return;
        }

        const items = await searchEntities(1);
        if (items.length) {
          window.location.href = items[0].url;
          return;
        }
        renderResults([]);
      };

      input.addEventListener("input", scheduleSuggestions);
      input.addEventListener("focus", () => {
        if (lastResults.length) showBox();
        else if (input.value.trim()) scheduleSuggestions();
      });

      input.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          clearResults();
          return;
        }
        if (event.key === "ArrowDown") {
          if (!lastResults.length) return;
          event.preventDefault();
          selectedIndex = Math.min(selectedIndex + 1, lastResults.length - 1);
          setActive();
          return;
        }
        if (event.key === "ArrowUp") {
          if (!lastResults.length) return;
          event.preventDefault();
          selectedIndex = Math.max(selectedIndex - 1, 0);
          setActive();
          return;
        }
        if (event.key === "Enter") {
          event.preventDefault();
          goToSelectedOrFirst();
        }
      });

      document.addEventListener("click", (event) => {
        if (!root.contains(event.target)) hideBox();
      });
    });
