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

  // Auto-inyección de CSS: embebido acá porque el discovery del manifest
  // se cachea por proceso — un campo css agregado después del arranque
  // del dashboard no se inyecta hasta el próximo restart. Embebido =
  // un solo archivo que cargar, cero dependencia del cache del host.
  const CSS = [
    ".arnes-viewer{display:flex;gap:12px;height:100%;min-height:0;padding:12px;font-size:13px}",
    ".av-list{display:flex;flex-direction:column;width:340px;flex-shrink:0;border:1px solid #333;border-radius:6px;overflow:hidden}",
    ".av-list-header{display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-bottom:1px solid #333;font-size:12px;font-weight:600}",
    ".av-list-body{min-height:0;flex:1;overflow:auto}",
    ".av-auto-label{display:flex;align-items:center;gap:6px;font-size:11px;font-weight:400;opacity:.7;cursor:pointer}",
    ".av-turn{display:block;width:100%;padding:8px 12px;border:0;border-bottom:1px solid rgba(128,128,128,.25);background:transparent;color:inherit;text-align:left;cursor:pointer;font:inherit}",
    ".av-turn:hover{background:rgba(128,128,128,.12)}",
    ".av-turn.av-selected{background:rgba(128,128,128,.18)}",
    ".av-turn-meta{display:flex;align-items:center;gap:8px;font-size:11px;font-family:ui-monospace,monospace;opacity:.75}",
    ".av-turn-msg{margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;opacity:.85}",
    ".av-badge{display:inline-flex;align-items:center;border:1px solid;border-radius:4px;padding:1px 6px;font-size:11px;font-family:ui-monospace,monospace}",
    ".av-badge-dressed{color:#34d399;border-color:rgba(52,211,153,.4);background:rgba(52,211,153,.08)}",
    ".av-badge-raw{color:#a1a1aa;border-color:rgba(161,161,170,.4);background:rgba(161,161,170,.08)}",
    ".av-badge-fail{color:#f87171;border-color:rgba(248,113,113,.4);background:rgba(248,113,113,.08)}",
    ".av-detail{display:flex;flex-direction:column;gap:12px;flex:1;min-width:0;overflow:auto}",
    ".av-userbox{border:1px solid #333;border-radius:6px;padding:12px}",
    ".av-userbox-label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;opacity:.55;margin-bottom:4px}",
    ".av-userbox-meta{display:flex;align-items:center;gap:10px;margin-top:8px;font-size:11px;font-family:ui-monospace,monospace;opacity:.7}",
    ".av-panels{display:flex;gap:12px;min-width:0;align-items:stretch;margin-top:12px}",
    ".av-panel{display:flex;flex-direction:column;gap:4px;flex:1;min-width:0}",
    ".av-panel-label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;opacity:.55}",
    ".av-panel pre{margin:0;max-height:460px;overflow:auto;white-space:pre-wrap;word-break:break-word;border:1px solid #333;border-radius:6px;padding:12px;font-size:12.5px;line-height:1.55;background:rgba(9,9,11,.6)}",
    ".av-empty{padding:24px;text-align:center;opacity:.6}",
    ".av-error{padding:12px;color:#f87171;font-size:12px}",
  ].join("\n");
  if (!document.getElementById("arnes-viewer-style")) {
    const styleEl = document.createElement("style");
    styleEl.id = "arnes-viewer-style";
    styleEl.textContent = CSS;
    document.head.appendChild(styleEl);
  }

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
