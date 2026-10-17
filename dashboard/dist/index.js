/**
 * Claude Accounts — dashboard tab for the claude-subscription-directsdk-experimental plugin.
 * Plain IIFE on window.__HERMES_PLUGIN_SDK__ (no build step). Shows each pooled Claude login:
 * plan, rotation order, benching (with reset countdown), request count and the account's own
 * session/weekly limits from Anthropic's usage API. One action: clear a bench early.
 */
(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button } = SDK.components;
  const cn = SDK.utils.cn;
  const BASE = "/api/plugins/claude-subscription-directsdk-experimental";

  function api(path, init) { return SDK.fetchJSON(BASE + path, init); }

  function fmtIn(ts) {
    if (!ts) return "";
    const s = Math.round(ts - Date.now() / 1000);
    if (s <= 0) return "now";
    const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), mm = Math.floor((s % 3600) / 60);
    if (d) return d + "d " + hh + "h";
    if (hh) return hh + "h " + mm + "m";
    return mm + "m";
  }
  function fmtAt(ts) { return ts ? new Date(ts * 1000).toLocaleString() : ""; }

  function Bar(props) {
    const pct = props.pct == null ? null : Math.max(0, Math.min(100, props.pct));
    // Inline colours: the host's Tailwind build only ships classes it uses itself, so bg-emerald-500 & co. would be purged.
    const tone = pct == null ? "transparent" : pct >= 90 ? "#ef4444" : pct >= 70 ? "#f59e0b" : "#10b981";
    return h("div", { className: "space-y-1" },
      h("div", { className: "flex justify-between text-xs" },
        h("span", null, props.label),
        h("span", { className: "text-muted-foreground" },
          pct == null ? "—" : pct.toFixed(0) + "%",
          props.resetAt ? " · resets in " + fmtIn(props.resetAt) : "")),
      h("div", { className: "h-2 w-full rounded bg-muted overflow-hidden", style: { border: "1px solid hsl(var(--border, 0 0% 50%) / 0.4)" } },
        h("div", { className: "h-full rounded", style: { width: (pct == null ? 0 : pct) + "%", backgroundColor: tone, transition: "width .3s" } })));
  }

  function StatusBadge(a) {
    if (!a.logged_in) return h(Badge, { variant: "destructive" }, "logged out");
    if (a.benched) return h(Badge, { variant: "destructive" }, "benched" + (a.error_reset_at ? " · " + fmtIn(a.error_reset_at) : ""));
    if (a.status && a.status !== "ok") return h(Badge, { variant: "secondary" }, a.status);
    return h(Badge, { variant: "default" }, "ok");
  }

  function AccountCard(props) {
    const a = props.account;
    const u = a.usage || {};
    return h(Card, { className: cn(a.current && "border-primary") },
      h(CardHeader, { className: "pb-2" },
        h(CardTitle, { className: "flex items-center gap-2 text-base" },
          h("span", { className: "text-muted-foreground" }, "#" + (a.priority + 1)),
          a.label,
          a.plan ? h(Badge, { variant: "outline" }, a.plan) : null,
          a.current ? h(Badge, { variant: "outline" }, "active") : null,
          h("span", { className: "flex-1" }),
          StatusBadge(a))),
      h(CardContent, { className: "space-y-3" },
        u.unavailable
          ? h("p", { className: "text-sm text-muted-foreground" }, "Limits unavailable: " + u.unavailable)
          : h("div", { className: "space-y-2" }, (u.windows || []).map(function (w, i) {
              return h(Bar, { key: i, label: w.label, pct: w.used_percent, resetAt: w.reset_at });
            })),
        (u.details || []).length ? h("p", { className: "text-xs text-muted-foreground" }, u.details.join(" · ")) : null,
        a.benched || (a.status && a.status !== "ok")
          ? h("div", { className: "rounded border border-destructive/40 bg-destructive/5 p-2 text-xs space-y-1" },
              h("div", null, h("b", null, a.error_reason || a.status),
                a.error_reset_at ? " — reopens " + fmtAt(a.error_reset_at) : ""),
              a.error_message ? h("div", { className: "text-muted-foreground break-words" }, a.error_message) : null,
              h(Button, { size: "sm", variant: "outline", onClick: function () { props.onClear(a.id); } }, "Clear bench now"))
          : null,
        h("div", { className: "flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" },
          h("span", null, a.request_count + " requests"),
          a.status_at ? h("span", null, "status " + SDK.utils.timeAgo(a.status_at * 1000)) : null,
          h("span", { className: "font-mono truncate", title: a.config_dir }, a.config_dir),
          a.login_error ? h("span", { className: "text-destructive" }, a.login_error) : null)));
  }

  function Page() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [busy, setBusy] = useState(false);

    const load = useCallback(function () {
      api("/accounts").then(function (d) { setData(d); setError(null); })
        .catch(function (e) { setError(String(e)); });
    }, []);
    useEffect(function () { load(); const id = setInterval(load, 60000); return function () { clearInterval(id); }; }, [load]);

    function refresh() {
      setBusy(true);
      api("/accounts/refresh", { method: "POST" }).then(load).finally(function () { setBusy(false); });
    }
    function clear(id) {
      api("/accounts/" + encodeURIComponent(id) + "/clear-bench", { method: "POST" }).then(load)
        .catch(function (e) { setError(String(e)); });
    }

    const accounts = (data && data.accounts) || [];
    const dflt = data && data.default_login;
    return h("div", { className: "space-y-4 p-1" },
      h("div", { className: "flex items-center gap-3" },
        h("h2", { className: "text-lg font-semibold" }, "Claude Accounts"),
        h("span", { className: "text-sm text-muted-foreground" },
          accounts.length ? accounts.length + " pooled login" + (accounts.length === 1 ? "" : "s") + " · rotation order = priority" : "pool empty"),
        h("span", { className: "flex-1" }),
        h(Button, { size: "sm", variant: "outline", disabled: busy, onClick: refresh }, busy ? "Refreshing…" : "Refresh limits")),
      error ? h("p", { className: "text-sm text-destructive" }, error) : null,
      !data ? h("p", { className: "text-sm text-muted-foreground" }, "Loading…") : null,
      accounts.map(function (a) { return h(AccountCard, { key: a.id, account: a, onClear: clear }); }),
      dflt ? h(Card, null,
        h(CardHeader, { className: "pb-2" }, h(CardTitle, { className: "flex items-center gap-2 text-base" },
          "Default Claude Code login", dflt.plan ? h(Badge, { variant: "outline" }, dflt.plan) : null,
          h("span", { className: "flex-1" }),
          h(Badge, { variant: dflt.logged_in ? "default" : "destructive" }, dflt.logged_in ? "logged in" : "logged out"))),
        h(CardContent, { className: "space-y-2" },
          dflt.usage && dflt.usage.unavailable
            ? h("p", { className: "text-sm text-muted-foreground" }, "Limits unavailable: " + dflt.usage.unavailable)
            : (dflt.usage.windows || []).map(function (w, i) { return h(Bar, { key: i, label: w.label, pct: w.used_percent, resetAt: w.reset_at }); }),
          h("p", { className: "text-xs text-muted-foreground font-mono" }, dflt.config_dir))) : null,
      data ? h("p", { className: "text-xs text-muted-foreground" },
        "Add a subscription from a terminal: ", h("code", null, data.add_hint),
        " · limits refresh every 60 s; a bench clears itself at the reset time.") : null);
  }

  window.__HERMES_PLUGINS__.register("claude-subscription-directsdk-experimental", Page);
})();
