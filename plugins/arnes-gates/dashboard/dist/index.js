/**
 * arnes-viewer — debug del par worker/presenter.
 *
 * Panel dividido por turno: izquierda RAW (worker), derecha VESTIDO
 * (presenter). Badge de outcome (dressed / failopen:razón / raw),
 * user_message como contexto arriba, tool_calls y ts en el header.
 *
 * IIFE sin build step. SDK del dashboard: window.__HERMES_PLUGIN_SDK__.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const h = React.createElement;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const { fetchJSON, cn } = SDK;

  const OUTCOME_STYLES = {
    dressed: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
    raw: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30",
  };
  const outcomeClass = function (outcome) {
    if (OUTCOME_STYLES[outcome]) return OUTCOME_STYLES[outcome];
    return "bg-red-500/15 text-red-400 border-red-500/30"; // failopen:*
  };

  const Badge = function (outcome) {
    return h("span", {
      className: cn(
        "inline-flex items-center rounded border px-1.5 py-0.5 text-[11px] font-mono",
        outcomeClass(outcome),
      ),
    }, outcome);
  };

  const Panel = function (label, text, mono) {
    return h("div", { className: "flex min-w-0 flex-1 flex-col gap-1" },
      h("div", { className: "text-[11px] font-semibold uppercase tracking-wider text-zinc-500" }, label),
      h("pre", {
        className: cn(
          "max-h-[420px] overflow-auto whitespace-pre-wrap break-words rounded border border-zinc-800 bg-zinc-950 p-3 text-[12.5px] leading-relaxed",
          mono ? "font-mono" : "font-sans",
        ),
      }, text || "(vacío)"),
    );
  };

  const TurnRow = function (entry, onSelect, selected) {
    const ts = (entry.ts || "").replace("T", " ").slice(5, 16);
    const isSel = selected && selected.ts === entry.ts;
    return h("button", {
      key: (entry.ts || "") + (entry.user_message || "").slice(0, 20),
      onClick: function () { onSelect(entry); },
      className: cn(
        "w-full border-b border-zinc-800/60 px-3 py-2 text-left hover:bg-zinc-900",
        isSel && "bg-zinc-900",
      ),
    },
      h("div", { className: "flex items-center gap-2" },
        Badge(entry.outcome),
        h("span", { className: "text-[11px] text-zinc-500 font-mono" }, ts),
        h("span", { className: "text-[11px] text-zinc-500" }, "·"),
        h("span", { className: "text-[11px] text-zinc-500" },
          (entry.tool_calls || 0) + " tools"),
      ),
      h("div", { className: "mt-0.5 truncate text-[12px] text-zinc-400" },
        entry.user_message || "(sin user_message)"),
    );
  };

  const ViewerPage = function () {
    const [entries, setEntries] = useState([]);
    const [selected, setSelected] = useState(null);
    const [error, setError] = useState(null);
    const [autoRefresh, setAutoRefresh] = useState(true);

    const load = useCallback(async function () {
      try {
        const data = await fetchJSON("/api/plugins/arnes-viewer/pairs");
        setEntries(data.entries || []);
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

    const sel = selected && entries.some(function (e) { return e.ts === selected.ts; })
      ? selected
      : entries[0];

    return h("div", { className: "flex h-full min-h-0 gap-3 p-3" },
      // Columna izquierda: lista de turnos
      h("div", { className: "flex w-[340px] shrink-0 flex-col overflow-hidden rounded border border-zinc-800" },
        h("div", { className: "flex items-center justify-between border-b border-zinc-800 px-3 py-2" },
          h("span", { className: "text-[12px] font-semibold text-zinc-300" },
            "Turnos (" + entries.length + ")"),
          h("label", { className: "flex cursor-pointer items-center gap-1.5 text-[11px] text-zinc-500" },
            h("input", {
              type: "checkbox",
              checked: autoRefresh,
              onChange: function (e) { setAutoRefresh(e.target.checked); },
            }),
            "auto",
          ),
        ),
        h("div", { className: "min-h-0 flex-1 overflow-auto" },
          error && h("div", { className: "p-3 text-[12px] text-red-400" },
            "API: " + error),
          !error && entries.length === 0 && h("div", { className: "p-3 text-[12px] text-zinc-500" },
            "Sin turnos registrados aún. El JSONL se llena con cada turno vestido (presenter_mode on)."),
          entries.map(function (e) { return TurnRow(e, setSelected, sel); }),
        ),
      ),
      // Columna derecha: par seleccionado
      h("div", { className: "flex min-w-0 flex-1 flex-col gap-3 overflow-auto" },
        !sel && h("div", { className: "p-6 text-center text-[13px] text-zinc-500" },
          "Seleccioná un turno de la lista."),
        sel && h(React.Fragment, null,
          h("div", { className: "rounded border border-zinc-800 p-3" },
            h("div", { className: "mb-1 text-[11px] font-semibold uppercase tracking-wider text-zinc-500" },
              "Mensaje del usuario"),
            h("div", { className: "text-[13px] text-zinc-200" }, sel.user_message || "(vacío)"),
            h("div", { className: "mt-2 flex items-center gap-2" },
              Badge(sel.outcome),
              h("span", { className: "text-[11px] font-mono text-zinc-500" }, sel.ts),
              h("span", { className: "text-[11px] text-zinc-500" },
                (sel.tool_calls || 0) + " tool calls"),
            ),
          ),
          h("div", { className: "flex min-w-0 gap-3" },
            Panel("Worker — crudo", sel.raw, false),
            Panel("Presenter — vestido", sel.dressed, false),
          ),
        ),
      ),
    );
  };

  window.__HERMES_PLUGINS__.register("arnes-viewer", ViewerPage);
})();
