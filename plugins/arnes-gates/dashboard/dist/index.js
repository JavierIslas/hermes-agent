/**
 * arnes-viewer — debug del par worker/presenter.
 *
 * Panel dividido por turno: izquierda RAW (worker), derecha VESTIDO
 * (presenter). Badge de outcome, user_message como contexto, tool_calls.
 *
 * IIFE sin build step. Estilos AUTOCONTENIDOS en dist/style.css (patrón
 * kanban): el dashboard no expone sus utilities Tailwind a plugins.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const h = React.createElement;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const { fetchJSON } = SDK;

  const badgeClass = function (outcome) {
    if (outcome === "dressed") return "av-badge av-badge-dressed";
    if (outcome === "raw") return "av-badge av-badge-raw";
    return "av-badge av-badge-fail"; // failopen:*
  };

  const Badge = function (outcome) {
    return h("span", { className: badgeClass(outcome) }, outcome);
  };

  const Panel = function (label, text) {
    return h("div", { className: "av-panel" },
      h("div", { className: "av-panel-label" }, label),
      h("pre", null, text || "(vacío)"),
    );
  };

  const TurnRow = function (entry, onSelect, selectedTs) {
    const ts = (entry.ts || "").replace("T", " ").slice(5, 16);
    const isSel = selectedTs != null && selectedTs === entry.ts;
    return h("button", {
      key: (entry.ts || "") + "|" + (entry.user_message || "").slice(0, 20),
      onClick: function () { onSelect(entry.ts); },
      className: isSel ? "av-turn av-selected" : "av-turn",
    },
      h("div", { className: "av-turn-meta" },
        Badge(entry.outcome),
        h("span", null, ts),
        h("span", null, "·"),
        h("span", null, (entry.tool_calls || 0) + " tools"),
      ),
      h("div", { className: "av-turn-msg" },
        entry.user_message || "(sin user_message)"),
    );
  };

  const ViewerPage = function () {
    const [entries, setEntries] = useState([]);
    const [selectedTs, setSelectedTs] = useState(null);
    const [error, setError] = useState(null);
    const [autoRefresh, setAutoRefresh] = useState(true);

    const load = useCallback(async function () {
      try {
        const data = await fetchJSON("/api/plugins/arnes-gates/pairs");
        setEntries((data && data.entries) || []);
        setError(null);
      } catch (e) {
        setError(String(e && e.message ? e.message : e));
      }
    }, []);

    useEffect(function () {
      load();
      if (!autoRefresh) return undefined;
      const t = setInterval(load, 4000);
      return function () { clearInterval(t); };
    }, [load, autoRefresh]);

    const sel = entries.find(function (e) { return e.ts === selectedTs; })
      || entries[0]
      || null;

    return h("div", { className: "arnes-viewer" },
      // Columna izquierda: lista de turnos
      h("div", { className: "av-list" },
        h("div", { className: "av-list-header" },
          h("span", null, "Turnos (" + entries.length + ")"),
          h("label", { className: "av-auto-label" },
            h("input", {
              type: "checkbox",
              checked: autoRefresh,
              onChange: function (e) { setAutoRefresh(e.target.checked); },
            }),
            "auto",
          ),
        ),
        h("div", { className: "av-list-body" },
          error && h("div", { className: "av-error" }, "API: " + error),
          !error && entries.length === 0 && h("div", { className: "av-empty" },
            "Sin turnos registrados aún."),
          entries.map(function (e) {
            return TurnRow(e, setSelectedTs, sel && sel.ts);
          }),
        ),
      ),
      // Columna derecha: par seleccionado
      h("div", { className: "av-detail" },
        !sel && h("div", { className: "av-empty" },
          "Seleccioná un turno de la lista."),
        sel && h("div", { key: sel.ts },
          h("div", { className: "av-userbox" },
            h("div", { className: "av-userbox-label" }, "Mensaje del usuario"),
            h("div", null, sel.user_message || "(vacío)"),
            h("div", { className: "av-userbox-meta" },
              Badge(sel.outcome),
              h("span", null, sel.ts),
              h("span", null, (sel.tool_calls || 0) + " tool calls"),
            ),
          ),
          h("div", { className: "av-panels", style: { marginTop: 12 } },
            Panel("Worker — crudo", sel.raw),
            Panel("Presenter — vestido", sel.dressed),
          ),
        ),
      ),
    );
  };

  window.__HERMES_PLUGINS__.register("arnes-gates", ViewerPage);
})();
