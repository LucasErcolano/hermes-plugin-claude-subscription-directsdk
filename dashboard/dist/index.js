/**
 * Claude Accounts — dashboard tab for the claude-subscription-directsdk-experimental plugin.
 * Plain IIFE on window.__HERMES_PLUGIN_SDK__ (no build step). Shows each pooled Claude login:
 * plan, rotation order, request count, the account's own session/weekly limits from Anthropic's
 * usage API, and which limit categories it is benched for. Controls: bench or clear a category
 * by hand, allow or block extra usage (usage credits), sign in again in place, remove.
 */
(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button, Input, ConfirmDialog } = SDK.components;
  const cn = SDK.utils.cn;
  const BASE = "/api/plugins/claude-subscription-directsdk-experimental";
  const JSON_HEADERS = { "Content-Type": "application/json" };

  function api(path, init) { return SDK.fetchJSON(BASE + path, init); }
  function post(path, body) {
    return api(path, { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body || {}) });
  }

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
  function fmtMoney(amount, currency) {
    if (amount == null) return "—";
    try { return new Intl.NumberFormat(undefined, { style: "currency", currency: currency || "USD" }).format(amount); }
    catch (e) { return amount.toFixed(2) + " " + (currency || ""); }
  }

  const SCOPE_NAMES = { all: "All models", fable: "Fable", opus: "Opus", sonnet: "Sonnet", haiku: "Haiku" };
  function scopeName(scope) { return SCOPE_NAMES[scope] || scope; }
  const REASONS = {
    limit: "plan limit reached", manual: "benched by you",
    extra_usage: "limit reached, and extra usage is blocked for this account",
  };
  const DURATIONS = [["1", "1 hour"], ["5", "5 hours"], ["24", "1 day"], ["168", "7 days"], ["", "until I clear it"]];

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

  function benchLabel(b) {
    return scopeName(b.scope) + " benched" + (b.until ? " · " + fmtIn(b.until) : "");
  }

  function StatusBadges(a) {
    if (!a.logged_in) return [h(Badge, { key: "out", variant: "destructive" }, "signed out")];
    const benches = a.benches || [];
    if (benches.length) {
      return benches.map(function (b, i) { return h(Badge, { key: i, variant: "destructive" }, benchLabel(b)); });
    }
    if (a.status && a.status !== "ok") return [h(Badge, { key: "st", variant: "secondary" }, a.status)];
    return [h(Badge, { key: "ok", variant: "default" }, "ok")];
  }

  function Select(props) {
    return h("select", {
      value: props.value, onChange: function (e) { props.onChange(e.target.value); },
      className: "h-8 rounded border bg-background px-2 text-xs",
      style: { borderColor: "hsl(var(--border, 0 0% 50%) / 0.6)" },
    }, props.options.map(function (o) { return h("option", { key: o[0], value: o[0] }, o[1]); }));
  }

  function BenchControl(props) {
    const [scope, setScope] = useState("fable");
    const [hours, setHours] = useState("24");
    const scopes = (props.scopes || ["all", "fable", "opus", "sonnet", "haiku"]).map(function (s) { return [s, scopeName(s)]; });
    return h("div", { className: "flex flex-wrap items-center gap-2 text-xs" },
      h("span", { className: "text-muted-foreground" }, "Bench"),
      h(Select, { value: scope, onChange: setScope, options: scopes }),
      h("span", { className: "text-muted-foreground" }, "for"),
      h(Select, { value: hours, onChange: setHours, options: DURATIONS }),
      h(Button, { size: "sm", variant: "outline", onClick: function () { props.onBench(scope, hours ? Number(hours) : null); } }, "Bench"));
  }

  function ExtraUsage(props) {
    const a = props.account;
    const e = (a.usage || {}).extra_usage;
    const allowed = a.extra_usage_allowed !== false;
    let upstream = "not reported for this account";
    if (e) {
      upstream = e.enabled
        ? "on at Anthropic · " + fmtMoney(e.used, e.currency) + (e.limit != null ? " of " + fmtMoney(e.limit, e.currency) : "") + " spent this month"
        : "off at Anthropic" + (e.disabled_reason ? " (" + e.disabled_reason.replace(/_/g, " ") + ")" : "");
    }
    return h("div", { className: "flex flex-wrap items-center gap-2 text-xs" },
      h("span", null, h("b", null, "Extra usage"), " "),
      h(Badge, { variant: allowed ? "outline" : "destructive" }, allowed ? "allowed" : "blocked"),
      h("span", { className: "text-muted-foreground" }, upstream),
      h("span", { className: "flex-1" }),
      h(Button, { size: "sm", variant: "outline", onClick: function () { props.onExtraUsage(a.id, !allowed); } },
        allowed ? "Block extra usage" : "Allow extra usage"));
  }

  function AccountCard(props) {
    const a = props.account;
    const u = a.usage || {};
    const benches = a.benches || [];
    return h(Card, { className: cn(a.current && "border-primary") },
      h(CardHeader, { className: "pb-2" },
        h(CardTitle, { className: "flex flex-wrap items-center gap-2 text-base" },
          h("span", { className: "text-muted-foreground" }, "#" + (a.priority + 1)),
          a.label,
          a.plan ? h(Badge, { variant: "outline" }, a.plan) : null,
          a.current ? h(Badge, { variant: "outline" }, "active") : null,
          h("span", { className: "flex-1" }),
          StatusBadges(a))),
      h(CardContent, { className: "space-y-3" },
        u.unavailable
          ? h("div", { className: "flex flex-wrap items-center gap-2 text-sm text-muted-foreground" },
              h("span", null, "Limits unavailable: " + u.unavailable),
              u.needs_login ? h(Button, { size: "sm", onClick: function () { props.onReauth(a); } }, "Sign in again") : null)
          : h("div", { className: "space-y-2" }, (u.windows || []).map(function (w, i) {
              return h(Bar, { key: i, label: w.label, pct: w.used_percent, resetAt: w.reset_at });
            })),
        (u.details || []).length ? h("p", { className: "text-xs text-muted-foreground" }, u.details.join(" · ")) : null,
        benches.length
          ? h("div", { className: "rounded border border-destructive/40 bg-destructive/5 p-2 text-xs space-y-2" },
              benches.map(function (b, i) {
                return h("div", { key: i, className: "flex flex-wrap items-center gap-2" },
                  h("span", null, h("b", null, scopeName(b.scope)), " — ",
                    b.until ? "benched until " + fmtAt(b.until) : "benched until you clear it"),
                  h("span", { className: "text-muted-foreground" }, REASONS[b.reason] || b.reason),
                  b.message ? h("span", { className: "text-muted-foreground break-words basis-full" }, b.message) : null,
                  h("span", { className: "flex-1" }),
                  h(Button, { size: "sm", variant: "outline", onClick: function () { props.onClear(a.id, b); } }, "Clear"));
              }))
          : null,
        h(ExtraUsage, { account: a, onExtraUsage: props.onExtraUsage }),
        h(BenchControl, { scopes: props.scopes, onBench: function (scope, hours) { props.onBench(a.id, scope, hours); } }),
        h("div", { className: "flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground" },
          a.status_at ? h("span", null, "status " + SDK.utils.timeAgo(a.status_at * 1000)) : null,
          h("span", { className: "font-mono truncate", title: a.config_dir }, a.config_dir),
          a.login_error ? h("span", { className: "text-destructive" }, a.login_error) : null,
          h("span", { className: "flex-1" }),
          h(Button, { size: "sm", variant: "ghost", onClick: function () { props.onReauth(a); } }, "Sign in again"),
          h(Button, { size: "sm", variant: "ghost", onClick: function () { props.onRemove(a); } }, "Remove"))));
  }

  // Sign-in flow, for a new account or (props.reauth) an existing one in place: the official
  // `claude auth login` runs on the server; the page shows its sign-in link and sends the pasted
  // code straight to that process (never through chat or the model).
  function LoginDialog(props) {
    const reauth = props.reauth || null;
    const [label, setLabel] = useState("");
    const [login, setLogin] = useState(null);
    const [code, setCode] = useState("");
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);

    useEffect(function () {
      if (!login || login.state === "done" || login.state === "failed") return undefined;
      const id = setInterval(function () {
        api("/logins/" + login.id).then(setLogin).catch(function () {});
      }, 3000);
      return function () { clearInterval(id); };
    }, [login && login.id, login && login.state]);

    useEffect(function () { if (login && login.state === "done") props.onAdded(); }, [login && login.state]);

    function start() {
      setBusy(true); setErr(null);
      post("/logins", reauth ? { credential_id: reauth.id } : { label: label })
        .then(setLogin).catch(function (e) { setErr(String(e)); }).finally(function () { setBusy(false); });
    }
    function submit() {
      if (!login || !code.trim()) return;
      setBusy(true); setErr(null);
      post("/logins/" + login.id + "/code", { code: code })
        .then(function (l) { setLogin(l); setCode(""); }).catch(function (e) { setErr(String(e)); }).finally(function () { setBusy(false); });
    }
    function close() {
      if (login && login.state !== "done") api("/logins/" + login.id, { method: "DELETE" }).catch(function () {});
      props.onClose();
    }

    const state = login ? login.state : "idle";
    return h(Card, { className: "border-primary" },
      h(CardHeader, { className: "pb-2" }, h(CardTitle, { className: "text-base" },
        reauth ? "Sign in again: " + reauth.label : "Add a Claude subscription")),
      h(CardContent, { className: "space-y-3" },
        state === "idle" ? h("div", { className: "space-y-2" },
          h("p", { className: "text-sm text-muted-foreground" },
            "Starts the official ", h("code", null, "claude auth login"),
            reauth
              ? " for this account. Its place in the rotation, label and benches stay; the current login is only replaced once the new sign-in succeeds."
              : " for a new login directory on the server."),
          reauth ? null : h(Input, { placeholder: "Label (e.g. work, home)", value: label, onChange: function (e) { setLabel(e.target.value); } }),
          h("div", { className: "flex gap-2" },
            h(Button, { size: "sm", disabled: busy, onClick: start }, busy ? "Starting…" : "Start sign-in"),
            h(Button, { size: "sm", variant: "ghost", onClick: close }, "Cancel"))) : null,
        state === "starting" ? h("p", { className: "text-sm" }, "Starting the Claude CLI…") : null,
        state === "awaiting_code" || state === "verifying" ? h("div", { className: "space-y-2" },
          h("ol", { className: "list-decimal pl-5 text-sm space-y-1" },
            h("li", null, "Open ", login.url
              ? h("a", { href: login.url, target: "_blank", rel: "noopener noreferrer", className: "underline" }, "the Claude sign-in page")
              : "the sign-in page (waiting for the CLI to print it…)",
              reauth
                ? " and sign in as this account (use a private window if you're signed in as another one)."
                : " and sign in with the subscription you want to add (use a private window if you're already signed in as another account)."),
            h("li", null, "Claude shows a code — copy the whole thing and paste it here.")),
          h(Input, { placeholder: "Paste the code", value: code, autoComplete: "off", spellCheck: false,
                     onChange: function (e) { setCode(e.target.value); },
                     onKeyDown: function (e) { if (e.key === "Enter") submit(); } }),
          login.error ? h("p", { className: "text-sm text-destructive" }, login.error) : null,
          h("div", { className: "flex items-center gap-2" },
            h(Button, { size: "sm", disabled: busy || state === "verifying" || !code.trim(), onClick: submit },
              state === "verifying" ? "Verifying…" : "Submit code"),
            h(Button, { size: "sm", variant: "ghost", onClick: close }, "Cancel"),
            h("span", { className: "text-xs text-muted-foreground" }, "expires in " + Math.ceil(login.expires_in / 60) + " min"))) : null,
        state === "done" ? h("div", { className: "space-y-2" },
          h("p", { className: "text-sm" }, reauth ? "Signed in again: " : "Added ", h("b", null, login.label || "the account"),
            reauth ? "." : ". It's now in the rotation."),
          h(Button, { size: "sm", onClick: props.onClose }, "Close")) : null,
        state === "failed" ? h("div", { className: "space-y-2" },
          h("p", { className: "text-sm text-destructive" }, login.error || "Sign-in failed."),
          h(Button, { size: "sm", variant: "outline", onClick: function () { setLogin(null); } }, "Try again"),
          h(Button, { size: "sm", variant: "ghost", onClick: close }, "Close")) : null,
        err ? h("p", { className: "text-sm text-destructive" }, err) : null));
  }

  function Page() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [busy, setBusy] = useState(false);
    const [adding, setAdding] = useState(false);
    const [reauth, setReauth] = useState(null);
    const [pendingRemove, setPendingRemove] = useState(null);

    const load = useCallback(function () {
      return api("/accounts").then(function (d) { setData(d); setError(null); })
        .catch(function (e) { setError(String(e)); });
    }, []);
    useEffect(function () { load(); const id = setInterval(load, 60000); return function () { clearInterval(id); }; }, [load]);

    function act(promise) { return promise.then(load).catch(function (e) { setError(String(e)); }); }
    function refresh() {
      setBusy(true);
      api("/accounts/refresh", { method: "POST" }).then(load).finally(function () { setBusy(false); });
    }
    function clear(id, b) {
      act(post("/accounts/" + encodeURIComponent(id) + "/clear-bench", { scope: b.scope, source: b.source }));
    }
    function bench(id, scope, hours) {
      act(post("/accounts/" + encodeURIComponent(id) + "/bench", { scope: scope, hours: hours }));
    }
    function extraUsage(id, allowed) {
      act(post("/accounts/" + encodeURIComponent(id) + "/extra-usage", { allowed: allowed }));
    }

    function remove() {
      const a = pendingRemove; if (!a) return Promise.resolve();
      return api("/accounts/" + encodeURIComponent(a.id), { method: "DELETE" }).then(load)
        .catch(function (e) { setError(String(e)); }).finally(function () { setPendingRemove(null); });
    }

    const accounts = (data && data.accounts) || [];
    const dflt = data && data.default_login;
    return h("div", { className: "space-y-4 p-1" },
      h("div", { className: "flex items-center gap-3" },
        h("h2", { className: "text-lg font-semibold" }, "Claude Accounts"),
        h("span", { className: "text-sm text-muted-foreground" },
          accounts.length ? accounts.length + " pooled login" + (accounts.length === 1 ? "" : "s") + " · rotation order = priority" : "pool empty"),
        h("span", { className: "flex-1" }),
        h(Button, { size: "sm", variant: "outline", disabled: busy, onClick: refresh }, busy ? "Refreshing…" : "Refresh limits"),
        h(Button, { size: "sm", disabled: adding, onClick: function () { setAdding(true); } }, "Add account")),
      error ? h("p", { className: "text-sm text-destructive" }, error) : null,
      !data ? h("p", { className: "text-sm text-muted-foreground" }, "Loading…") : null,
      adding ? h(LoginDialog, { onClose: function () { setAdding(false); load(); }, onAdded: load }) : null,
      reauth ? h(LoginDialog, { key: "reauth-" + reauth.id, reauth: reauth, onClose: function () { setReauth(null); load(); }, onAdded: load }) : null,
      pendingRemove ? h(ConfirmDialog, {
        open: true, title: "Remove " + pendingRemove.label + "?",
        description: "Signs this login out of Claude Code and drops it from the rotation.",
        confirmLabel: "Remove", onConfirm: remove, onCancel: function () { setPendingRemove(null); },
        onOpenChange: function (o) { if (!o) setPendingRemove(null); } }) : null,
      accounts.map(function (a) {
        return h(AccountCard, { key: a.id, account: a, scopes: data.scopes, onClear: clear, onBench: bench,
                                onExtraUsage: extraUsage, onReauth: setReauth, onRemove: setPendingRemove });
      }),
      dflt ? h(Card, null,
        h(CardHeader, { className: "pb-2" }, h(CardTitle, { className: "flex items-center gap-2 text-base" },
          "Default Claude Code login", dflt.plan ? h(Badge, { variant: "outline" }, dflt.plan) : null,
          h("span", { className: "flex-1" }),
          h(Badge, { variant: dflt.logged_in ? "default" : "destructive" }, dflt.logged_in ? "signed in" : "signed out"))),
        h(CardContent, { className: "space-y-2" },
          dflt.usage && dflt.usage.unavailable
            ? h("p", { className: "text-sm text-muted-foreground" }, "Limits unavailable: " + dflt.usage.unavailable)
            : (dflt.usage.windows || []).map(function (w, i) { return h(Bar, { key: i, label: w.label, pct: w.used_percent, resetAt: w.reset_at }); }),
          h("p", { className: "text-xs text-muted-foreground font-mono" }, dflt.config_dir))) : null,
      data ? h("p", { className: "text-xs text-muted-foreground" },
        "Add a subscription from a terminal: ", h("code", null, data.add_hint),
        " · limits refresh every 60 s; a bench clears itself at its end time. ",
        "Blocking extra usage benches an account at each plan limit it reaches, so its requests go to the next account or fallback model and no usage credits are spent.") : null);
  }

  window.__HERMES_PLUGINS__.register("claude-subscription-directsdk-experimental", Page);
})();
