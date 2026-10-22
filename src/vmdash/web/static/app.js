// vmdash: search, collapse, templates toggle, theme and auto-refresh. No libraries.
(() => {
  "use strict";
  const doc = document, root = doc.documentElement, body = doc.body;
  const $ = (id) => doc.getElementById(id);
  const q = $("q"), kbd = $("kbd"), tpl = $("tpl"), fold = $("fold"), count = $("count");
  const group = body.dataset.group || "host", snapshot = body.dataset.snapshot || "";
  const GB = 1073741824;

  const get = (s, k) => { try { return s.getItem(k); } catch (e) { return null; } };
  const put = (s, k, v) => { try { v == null ? s.removeItem(k) : s.setItem(k, v); } catch (e) { /* storage blocked */ } };
  const size = (b) => { const g = b / GB; return g >= 1024 ? (g / 1024).toFixed(1).replace(/\.0$/, "") + " TB" : Math.round(g) + " GB"; };

  // Parse every row once: data-n = "vcpu memMB capBytes running hostMoref"
  const groups = [...doc.querySelectorAll("tbody.grp")].map((tb) => ({
    tb, key: tb.dataset.key, btn: tb.querySelector(".gbtn"), tot: tb.querySelector(".htot"),
    rows: [...tb.querySelectorAll("tr.vm")].map((tr) => {
      const n = tr.dataset.n.split(" ");
      return { tr, q: tr.dataset.q, tpl: tr.classList.contains("tpl"),
        cpu: +n[0], ram: +n[1], cap: +n[2], run: n[3] === "1", host: n[4] || "" };
    }),
  }));

  const ckey = "vmdash.collapsed." + group;
  let collapsed;
  try { collapsed = new Set(JSON.parse(get(sessionStorage, ckey) || "[]")); } catch (e) { collapsed = new Set(); }
  const saveCollapsed = () => put(sessionStorage, ckey, JSON.stringify([...collapsed]));

  const canHighlight = !!(window.CSS && CSS.highlights && window.Highlight);

  function highlight(terms, trs) {
    if (!canHighlight) return;
    if (!terms.length) { CSS.highlights.delete("q"); return; }
    const h = new Highlight();
    for (const tr of trs) {
      const walk = doc.createTreeWalker(tr, NodeFilter.SHOW_TEXT);
      for (let node = walk.nextNode(); node; node = walk.nextNode()) {
        const text = node.data.toLowerCase();
        for (const t of terms) {
          for (let i = text.indexOf(t); i !== -1; i = text.indexOf(t, i + t.length)) {
            const r = doc.createRange();
            r.setStart(node, i); r.setEnd(node, i + t.length); h.add(r);
          }
        }
      }
    }
    CSS.highlights.set("q", h);
  }

  function apply() {
    const terms = q.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const searching = terms.length > 0, showT = root.classList.contains("show-tpl");
    const sum = { vms: 0, running: 0, vcpu: 0, ram: 0, cap: 0 }, hosts = new Set(), shown = [];
    let pool = 0, tplHits = 0;
    for (const g of groups) {
      let n = 0, cpu = 0, ram = 0, cap = 0;
      for (const r of g.rows) {
        const match = !searching || terms.every((t) => r.q.includes(t));
        r.tr.classList.toggle("nomatch", !match);
        if (r.tpl && !showT) { if (match && searching) tplHits++; continue; }
        pool++;
        if (!match) continue;
        n++; cpu += r.cpu; ram += r.ram; cap += r.cap;
        sum.vms++; sum.running += r.run; sum.vcpu += r.cpu; sum.ram += r.ram; sum.cap += r.cap;
        if (r.host) hosts.add(r.host);
        shown.push(r.tr);
      }
      const isCollapsed = !searching && collapsed.has(g.key);
      g.tb.classList.toggle("hide", n === 0);
      g.tb.classList.toggle("collapsed", isCollapsed);
      g.btn.setAttribute("aria-expanded", String(!isCollapsed));
      g.tot.textContent = `${n} VM${n === 1 ? "" : "s"} · ${cpu} vCPU · ${Math.round(ram / 1024)} GB RAM · ${size(cap)}`;
    }
    const vals = { hosts: hosts.size, vms: sum.vms, running: sum.running, vcpu: sum.vcpu,
      ram: Math.round(sum.ram / 1024), cap: size(sum.cap) };
    for (const el of doc.querySelectorAll("[data-sum]")) el.textContent = vals[el.dataset.sum];
    count.innerHTML = (searching ? `<b>${sum.vms}</b> of ${pool} VMs` : `<b>${pool}</b> VMs`) +
      (tplHits ? ` · <button type="button" class="lnk" id="showtpl">${tplHits} more in templates</button>` : "");
    $("nores").hidden = !(searching && sum.vms === 0 && groups.length);
    const visible = groups.filter((g) => !g.tb.classList.contains("hide"));
    fold.textContent = visible.length && visible.every((g) => collapsed.has(g.key)) ? "Expand all" : "Collapse all";
    kbd.hidden = doc.activeElement === q || q.value !== "";
    highlight(terms, shown);
  }

  let frame = 0;
  const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(apply); };

  q.addEventListener("input", schedule);
  q.addEventListener("focus", () => { kbd.hidden = true; });
  q.addEventListener("blur", () => { kbd.hidden = q.value !== ""; });

  doc.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(doc.activeElement.tagName);
    if ((e.key === "/" && !typing) || ((e.key || "").toLowerCase() === "k" && (e.ctrlKey || e.metaKey))) {
      e.preventDefault(); q.focus(); q.select();
    } else if (e.key === "Escape" && doc.activeElement === q) {
      q.value = ""; q.blur(); apply();
    }
  });

  $("t").addEventListener("click", (e) => {
    const btn = e.target.closest(".gbtn");
    if (!btn || q.value.trim()) return;
    const key = btn.closest("tbody").dataset.key;
    collapsed.has(key) ? collapsed.delete(key) : collapsed.add(key);
    saveCollapsed(); apply();
  });

  // Click an IP or DNS name to copy it. The Clipboard API needs HTTPS; plain HTTP falls back to execCommand.
  // "Copied!" is a fixed bubble on <body>: table cells clip anything that sticks out of them.
  const tip = doc.createElement("div");
  tip.className = "tip"; tip.hidden = true; tip.setAttribute("role", "status");
  body.append(tip);
  let tipTimer = 0;
  $("t").addEventListener("click", (e) => {
    const el = e.target.closest(".ip,.dns"), sel = getSelection();
    if (!el || String(sel)) return; // leave a manual text selection alone
    const show = (ok) => {
      const r = el.getBoundingClientRect();
      tip.textContent = ok ? "Copied!" : "Copy failed";
      tip.style.left = r.left + r.width / 2 + "px"; tip.style.top = r.top + "px"; tip.hidden = false;
      clearTimeout(tipTimer); tipTimer = setTimeout(() => { tip.hidden = true; }, 1000);
    };
    const legacy = () => {
      const r = doc.createRange(); r.selectNodeContents(el);
      sel.removeAllRanges(); sel.addRange(r); // the click left a collapsed caret; Chrome ignores a second range
      let ok = false;
      try { ok = doc.execCommand("copy"); } catch (err) { /* unsupported */ }
      sel.removeAllRanges(); show(ok);
    };
    if (navigator.clipboard && isSecureContext) navigator.clipboard.writeText(el.textContent).then(() => show(true), legacy);
    else legacy();
  });

  // Host usage popups are position:fixed (the table's scroll box would clip them): put each one under its host
  // name when it is about to show, or above it when there is no room below. CSS shows it after 1 s.
  const placePop = (hn) => {
    const p = hn && hn.querySelector(".pop");
    if (!p) return;
    const r = hn.getBoundingClientRect(), h = p.offsetHeight;
    p.style.left = r.left + "px";
    p.style.top = (r.bottom + 6 + h > innerHeight ? r.top - 6 - h : r.bottom + 6) + "px";
  };
  $("t").addEventListener("mouseover", (e) => placePop(e.target.closest(".pu")));
  $("t").addEventListener("focusin", (e) => placePop(e.target.querySelector && e.target.querySelector(".pu")));

  fold.addEventListener("click", () => {
    const keys = groups.filter((g) => !g.tb.classList.contains("hide")).map((g) => g.key);
    const all = keys.every((k) => collapsed.has(k));
    for (const k of keys) all ? collapsed.delete(k) : collapsed.add(k);
    saveCollapsed(); apply();
  });

  const setTemplates = (on) => {
    root.classList.toggle("show-tpl", on); tpl.checked = on;
    put(localStorage, "vmdash.tpl", on ? "1" : "0"); apply();
  };
  tpl.checked = root.classList.contains("show-tpl");
  tpl.addEventListener("change", () => setTemplates(tpl.checked));
  count.addEventListener("click", (e) => { if (e.target.id === "showtpl") setTemplates(true); });
  // Changing the grouping loads a new page: carry the search over (restored at the end of this file).
  doc.querySelector(".seg").addEventListener("click", (e) => {
    const newTab = e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0;
    if (e.target.closest("a") && !newTab) put(sessionStorage, "vmdash.q", q.value);
  });

  const themeBtn = $("theme");
  const showTheme = () => {
    const label = `Switch to ${root.dataset.theme === "dark" ? "light" : "dark"} mode`;
    themeBtn.title = label; themeBtn.setAttribute("aria-label", label);
  };
  themeBtn.addEventListener("click", () => {
    root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
    put(localStorage, "vmdash.theme", root.dataset.theme); showTheme();
  });
  showTheme();

  // Auto-refresh: reload when a new snapshot exists, otherwise just update the badge.
  const sync = $("sync"), banner = $("banner");
  function setBadge(b) {
    sync.dataset.state = b.state;
    sync.querySelector(".l1").textContent = b.line1;
    sync.querySelector(".l2").textContent = b.line2;
    banner.hidden = !b.banner;
    banner.className = "banner gut " + b.state;
    banner.textContent = b.banner || "";
  }
  async function poll() {
    try {
      const r = await fetch("status.json", { cache: "no-store", credentials: "same-origin" });
      if (!r.ok) return;
      const s = await r.json();
      if (String(s.snapshot_uid ?? "") !== snapshot) {
        put(sessionStorage, "vmdash.q", q.value);
        location.reload();
        return;
      }
      setBadge(s.badge);
    } catch (e) { /* offline or server restarting: try again next time */ }
  }
  setInterval(poll, 30000);
  doc.addEventListener("visibilitychange", () => { if (!doc.hidden) poll(); });

  const saved = get(sessionStorage, "vmdash.q");
  if (saved) { q.value = saved; put(sessionStorage, "vmdash.q", null); }
  apply();
})();
