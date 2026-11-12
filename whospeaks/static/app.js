"use strict";

// --- Jeton : ?token=... dans l'URL (carte iframe HA), sinon mémorisé localement ---
const params = new URLSearchParams(location.search);
let token = params.get("token") || "";
try {
  if (token) localStorage.setItem("whospeaks_token", token);
  else token = localStorage.getItem("whospeaks_token") || "";
} catch (e) { /* stockage indisponible : on garde le jeton en mémoire */ }

const state = { personnes: [], groupes: [], reglages: {}, etat: {}, tab: "journal", filtre: "", satellite: "" };

function url(path, extra = {}) {
  const q = new URLSearchParams(extra);
  if (token) q.set("token", token);
  const s = q.toString();
  return path + (s ? (path.includes("?") ? "&" : "?") + s : "");
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url(path), opts);
  if (resp.status === 401) {
    await askToken();
    return api(method, path, body);
  }
  const data = resp.headers.get("content-type")?.includes("json") ? await resp.json() : null;
  if (!resp.ok) throw new Error(data?.detail || `Erreur ${resp.status}`);
  return data;
}

function askToken() {
  return new Promise((resolve) => {
    const dlg = document.getElementById("token-dialog");
    dlg.onclose = () => {
      token = document.getElementById("token-input").value.trim();
      try { localStorage.setItem("whospeaks_token", token); } catch (e) { /* ignoré */ }
      resolve();
    };
    if (!dlg.open) dlg.showModal();
  });
}

// --- Petits utilitaires DOM (jamais d'innerHTML avec des données) ---
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function toast(text, erreur = false) {
  const el = document.getElementById("message");
  el.textContent = text;
  el.className = "message" + (erreur ? " erreur" : "");
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.hidden = true), erreur ? 6000 : 2500);
}

async function act(fn, ok) {
  try {
    const r = await fn();
    if (ok) toast(ok);
    await refresh();
    return r;
  } catch (e) {
    toast(e.message, true);
  }
}

const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "medium" }) : "—";
const fmtScore = (s) => (s === null || s === undefined) ? "" : s.toFixed(2);

let currentAudio = null;
function playButton(src) {
  const btn = h("button", { class: "play", title: "Écouter", type: "button" }, "▶");
  btn.addEventListener("click", () => {
    if (currentAudio && currentAudio.btn === btn && !currentAudio.paused) {
      currentAudio.pause();
      return;
    }
    if (currentAudio) currentAudio.pause();
    const a = new Audio(url(src));
    a.btn = btn;
    a.onplay = () => (btn.textContent = "❚❚");
    a.onpause = a.onended = () => (btn.textContent = "▶");
    a.onerror = () => toast("Audio indisponible", true);
    currentAudio = a;
    a.play();
  });
  return btn;
}

function personSelect(onPick, placeholder = "Attribuer à…") {
  const sel = h("select", {},
    h("option", { value: "" }, placeholder),
    state.personnes.map((p) => h("option", { value: p.id }, p.nom)),
    h("option", { value: "__new" }, "➕ Nouvelle personne…"));
  sel.addEventListener("change", () => {
    const v = sel.value;
    sel.value = "";
    if (!v) return;
    if (v === "__new") {
      const nom = prompt("Nom de la nouvelle personne :");
      if (nom && nom.trim()) onPick({ nouveau_nom: nom.trim() });
    } else onPick({ personne_id: Number(v) });
  });
  return sel;
}

function resultBadge(p) {
  const s = state.reglages;
  if (p.resultat === "ignoree_silence") return h("span", { class: "badge none", title: "aucune parole détectée : texte vide renvoyé à HA" }, "ignorée (silence)");
  if (p.resultat === "ignoree_hallucination") return h("span", { class: "badge none", title: "texte inventé par Whisper : texte vide renvoyé à HA" }, "ignorée (hallucination)");
  if (p.resultat === "reprise" && p.personne_id) {
    // Phrase trop courte : interlocuteur repris de la dernière identification.
    const corrigee = p.etat === "attribuee" ? ", corrigée" : "";
    return h("span", { class: "badge warn", title: `reprise de l'identification précédente (score ${fmtScore(p.score)})` },
      `${p.nom || "?"} (reprise${corrigee})`);
  }
  if (p.resultat === "identifie" || p.etat === "attribuee") {
    const sure = p.score !== null && p.score >= (s.seuil_auto ?? 0.75);
    const label = p.nom || "?";
    const cls = p.resultat === "identifie" ? (sure ? "ok" : "warn") : "ok";
    const suffix = p.resultat === "identifie" ? ` ${fmtScore(p.score)}` : (p.resultat === "enrolement" ? " (enrôlement)" : " (attribué)");
    return h("span", { class: "badge " + cls, title: `score ${fmtScore(p.score)} · marge ${fmtScore(p.marge)}` }, label + suffix);
  }
  if (p.resultat === "trop_court") return h("span", { class: "badge none" }, "trop court");
  if (p.resultat === "erreur") return h("span", { class: "badge bad" }, "erreur");
  if (p.etat === "ignoree") return h("span", { class: "badge none" }, "ignorée");
  return h("span", { class: "badge bad", title: `meilleur score ${fmtScore(p.score)}` },
    "inconnu" + (p.cluster_nom ? ` · ${p.cluster_nom}` : "") + (p.score !== null ? ` ${fmtScore(p.score)}` : ""));
}

// --- Journal ---
async function renderJournal() {
  const root = document.getElementById("journal");
  const [phrases, satellites, temps] = await Promise.all([
    api("GET", "/api/phrases?" + new URLSearchParams({ limit: 150, ...journalParams() })),
    api("GET", "/api/satellites"),
    api("GET", "/api/temps"),
  ]);
  const filtre = h("select", { onchange: (e) => { state.filtre = e.target.value; refresh(); } },
    JOURNAL_FILTERS.map(([v, l]) => h("option", { value: v, selected: state.filtre === v }, l)));
  // Satellite : pièce ou nom ; « ? » pour les phrases dont le satellite est inconnu
  const satFilter = h("select", { title: "Satellite", onchange: (e) => { state.satellite = e.target.value; refresh(); } },
    h("option", { value: "" }, "Tous les satellites"),
    satellites.map((x) => h("option", { value: x.satellite ?? "?", selected: state.satellite === (x.satellite ?? "?") }, x.satellite ?? "? (inconnu)")));
  // Filtres sans apprentissage : leurs phrases peuvent partir d'un coup.
  const bulk = BULK_DELETE.includes(state.filtre) && phrases.length
    ? h("button", { class: "danger", onclick: () => deleteFiltered(state.filtre) }, "🗑 Tout supprimer") : null;
  const list = phrases.length ? phrases.map((p) => h("div", { class: "card phrase" },
    h("div", {}, h("div", {}, fmtDate(p.horodatage)),
      h("div", { class: "muted" }, [p.duree_parole ? `${p.duree_parole.toFixed(1)} s` : null,
        p.temps_ajoute_ms !== null ? `+${Math.round(p.temps_ajoute_ms)} ms` : null].filter(Boolean).join(" · "))),
    h("div", { class: "grow" }, h("div", { class: "row" }, satelliteBadge(p), resultBadge(p)), h("div", { class: "transcript" }, p.transcript || "—"),
      p.transcript_corrige ? h("div", { class: "muted" }, "vraiment dit : ", h("span", { class: "transcript" }, p.transcript_corrige)) : null),
    timingCell(p),
    h("div", { class: "actions" },
      h("button", { class: openDetails.has(p.id) ? "on" : null, title: "Détail : temps de chaque étape, correction du texte", onclick: () => { openDetails.has(p.id) ? openDetails.delete(p.id) : openDetails.add(p.id); refresh(); } }, "ⓘ"),
      p.audio ? playButton(`/api/phrases/${p.id}/audio`) : null,
      p.resultat === "reprise" || ((p.a_empreinte || p.audio) && !["trop_court", "ignoree_silence"].includes(p.resultat))
        ? personSelect((b) => act(() => api("POST", `/api/phrases/${p.id}/attribuer`, b), "Phrase attribuée"),
          p.personne_id ? "Corriger…" : "Attribuer à…") : null,
      p.personne_id ? h("button", { title: "Ce n'était pas cette personne", onclick: () => act(() => api("POST", `/api/phrases/${p.id}/desattribuer`), "Remise en inconnue") }, "✗ pas lui/elle") : null,
      !p.personne_id && p.etat !== "ignoree" && p.a_empreinte ? h("button", { onclick: () => act(() => api("POST", `/api/phrases/${p.id}/ignorer`), "Ignorée") }, "Ignorer") : null,
      h("button", { class: "danger", title: "Supprimer", onclick: () => confirm("Supprimer cette phrase du journal ?") && act(() => api("DELETE", `/api/phrases/${p.id}`)) }, "🗑")),
    openDetails.has(p.id) ? phraseDetails(p) : null)
  ) : [h("div", { class: "empty" }, "Aucune phrase pour l'instant.")];
  root.replaceChildren(h("div", { class: "toolbar" }, filtre, satFilter, bulk,
    h("label", { class: "muted" }, h("input", { type: "checkbox", id: "auto", checked: autoRefresh, onchange: (e) => (autoRefresh = e.target.checked) }), " actualisation auto")),
    satelliteStats(satellites), timingStats(temps), ...list);
}

// --- Temps de chaque étape ---
const openDetails = new Set();
const TIMINGS = [
  ["duree_audio_s", "Audio reçu de HA"],
  ["duree_parole", "Parole (VAD)"],
  ["attente_fin_s", "Attente fin de parole → fin du flux (détection de HA)"],
  ["whisper_s", "Whisper (fin du flux → texte)"],
  ["identification_s", "Identification de la voix"],
  ["attente_identification_s", "Attente de l'identification après Whisper"],
  ["total_s", "Total (premier paquet audio → texte renvoyé à HA)"],
];
const sec = (v) => (v === null || v === undefined) ? "—" : `${v.toFixed(v < 10 ? 2 : 1)} s`;

function timingCell(p) {
  if (p.total_s === null || p.total_s === undefined) return h("div", { class: "temps muted" }, "");
  return h("div", { class: "temps", title: "Temps total, dont Whisper" },
    h("div", {}, `⏱ ${sec(p.total_s)}`),
    p.whisper_s !== null ? h("div", { class: "muted" }, `Whisper ${sec(p.whisper_s)}`) : null);
}

function phraseDetails(p) {
  const corriger = () => {
    const texte = prompt("Ce qui a vraiment été dit :", p.transcript_corrige || p.transcript || "");
    if (texte === null || !texte.trim()) return;
    act(async () => {
      const r = await api("POST", `/api/phrases/${p.id}/texte`, { texte });
      toast(r.mots.length ? `Mots retenus : ${r.mots.map((m) => `${m.faux} → ${m.vrai}`).join(", ")}` : "Texte enregistré (aucun mot différent)");
    });
  };
  return h("div", { class: "details" },
    h("table", {}, TIMINGS.map(([k, l]) => h("tr", {}, h("td", { class: "muted" }, l), h("td", {}, sec(p[k]))))),
    h("div", { class: "row" },
      p.transcript ? h("button", { onclick: corriger }, "✎ Corriger le texte") : null,
      h("span", { class: "muted" }, "Le texte envoyé à HA reste celui de Whisper ; les mots corrigés passent en tête du vocabulaire.")));
}

// Médiane et 90e percentile sur 24 h, par satellite
function timingStats(stats) {
  const rows = stats.filter((x) => x.total_s || x.whisper_s);
  if (!rows.length) return null;
  const pair = (v) => v ? `${v.mediane.toFixed(1)} / ${v.p90.toFixed(1)} s` : "—";
  return h("div", { class: "stats muted" }, "Temps sur 24 h (médiane / 90 %) : ",
    rows.map((x) => h("span", { class: "badge none", title: `${x.phrases} phrase(s)` },
      `${x.satellite ?? "?"} : total ${pair(x.total_s)} · Whisper ${pair(x.whisper_s)} · fin de parole ${pair(x.attente_fin_s)}`)));
}

// --- Vocabulaire ---
async function renderVocabulaire() {
  const root = document.getElementById("vocabulaire");
  const v = await api("GET", "/api/vocabulaire");
  const manual = h("textarea", { rows: 3, spellcheck: "false", placeholder: "Jarvis, OctoPi, Ambilight, frotte-frotte" });
  manual.value = (state.reglages.mots_en_plus || []).join(", ");
  const maxTokens = h("input", { type: "number", min: 20, max: 224, step: 10, value: v.max_tokens });
  const save = () => act(() => api("PUT", "/api/reglages", {
    mots_en_plus: manual.value.split(/[,\n]/).map((m) => m.trim()).filter(Boolean),
    vocab_max_tokens: Number(maxTokens.value),
  }), "Vocabulaire enregistré");
  const copy = async () => {
    try { await navigator.clipboard.writeText(v.prompt); toast("Prompt copié"); } catch (e) { toast("Copie impossible : sélectionner le texte à la main", true); }
  };
  const ha = v.ha.maj ? `relevé dans HA le ${fmtDate(v.ha.maj)} : ${v.ha.entites} entité(s) partagée(s) avec Assist, ${v.ha.pieces} pièce(s), ${v.ha.etages} étage(s)` : "jamais relevé dans HA";
  const byCat = {};
  for (const t of v.termes) (byCat[t.libelle] ??= []).push(t);
  root.replaceChildren(
    h("div", { class: "card" }, h("h2", {}, "Prompt donné à Whisper"),
      h("pre", { class: "prompt" }, v.prompt || "(vide)"),
      h("div", { class: "row" },
        h("span", { class: "badge " + (v.tokens > v.max_tokens ? "bad" : "none") }, `≈ ${v.tokens} / ${v.max_tokens} tokens`),
        h("button", { onclick: copy }, "Copier"),
        h("a", { href: url("/api/vocabulaire/prompt"), target: "_blank" }, "texte brut"),
        h("button", { onclick: () => act(() => api("POST", "/api/vocabulaire/rafraichir"), "Vocabulaire relevé dans HA") }, "Relire HA maintenant")),
      h("p", { class: "muted" }, `Vocabulaire ${ha}. ` + (v.ha.erreur ? `Dernier relevé en échec : ${v.ha.erreur}. ` : "") +
        "Relu au démarrage puis toutes les 15 min. wyoming-whisper-gpu ne lit pas encore de prompt par phrase : " +
        "copier ce texte dans sa variable STT_PROMPT et le redémarrer (voir le README).")),
    h("div", { class: "card" }, h("h2", {}, "Mots en plus"),
      h("div", { class: "form-grid" },
        h("label", { class: "wide" }, "Mots à faire connaître à Whisper (séparés par des virgules)", manual),
        h("label", {}, "Taille maximale du prompt (tokens)", maxTokens)),
      h("div", { class: "toolbar", style: "margin-top:12px" }, h("button", { class: "primary", onclick: save }, "Enregistrer"))),
    h("div", { class: "card" }, h("h2", {}, "Mots souvent mal compris"),
      v.corrections.length ? h("table", {},
        h("tr", {}, h("th", {}, "Whisper a écrit"), h("th", {}, "Vraiment dit"), h("th", {}, "Fois"), h("th", {}, "")),
        v.corrections.map((c) => h("tr", {}, h("td", {}, c.faux), h("td", {}, h("strong", {}, c.vrai)), h("td", {}, c.nombre),
          h("td", {}, h("button", { class: "danger", onclick: () => act(() => api("DELETE", "/api/vocabulaire/corrections?" + new URLSearchParams({ faux: c.faux, vrai: c.vrai })), "Mot retiré") }, "🗑")))))
        : h("p", { class: "muted" }, "Aucun pour l'instant. Dans le Journal, ⓘ puis « Corriger le texte » sur une phrase mal transcrite.")),
    h("details", { class: "card" }, h("summary", {}, `Tous les termes (${v.termes.length}), par priorité`),
      Object.entries(byCat).map(([cat, terms]) => h("div", { class: "termes" }, h("strong", {}, cat + " : "),
        terms.map((t) => h("span", { class: "badge " + (t.dans_prompt ? "none" : "warn"), title: t.dans_prompt ? "dans le prompt" : "hors prompt (trop long)" }, t.terme))))));
}

function journalParams() {
  return { ...(state.filtre ? { filtre: state.filtre } : {}), ...(state.satellite ? { satellite: state.satellite } : {}) };
}

function satelliteBadge(p) {
  return h("span", { class: "badge sat", title: p.satellite_entity || "satellite inconnu" }, p.satellite || "?");
}

// Phrases ignorées par satellite sur 24 h : un micro qui se réveille tout seul saute aux yeux
function satelliteStats(satellites) {
  const recent = satellites.filter((x) => x.total_24h > 0);
  if (!recent.length) return null;
  return h("div", { class: "stats muted" }, "Dernières 24 h, phrases ignorées (silence, hallucination, trop courtes) : ",
    recent.map((x) => {
      const suspect = x.ignorees_24h >= 5 && x.ignorees_24h >= x.total_24h / 2;
      return h("span", { class: "badge " + (suspect ? "warn" : "none"), title: `${x.ignorees_24h} ignorée(s) sur ${x.total_24h} phrase(s)` },
        `${x.satellite ?? "?"} : ${x.ignorees_24h} / ${x.total_24h}`);
    }));
}

const JOURNAL_FILTERS = [["", "Toutes les phrases"], ["identifie", "Reconnues"], ["inconnu", "Inconnues à traiter"],
  ["attribuee", "Attribuées"], ["reprise", "Reprises"], ["trop_court", "Trop courtes"], ["enrolement", "Enrôlement"],
  ["ignoree_silence", "Ignorées (silence)"], ["ignoree_hallucination", "Ignorées (hallucination)"], ["erreur", "Erreurs"]];
const BULK_DELETE = ["trop_court", "ignoree_silence", "ignoree_hallucination", "erreur"];

async function deleteFiltered(filtre) {
  const label = JOURNAL_FILTERS.find(([v]) => v === filtre)[1];
  try {
    // Le nombre total, pas seulement les phrases affichées (limitées à 150).
    // Le filtre satellite s'applique aussi : on ne supprime que ce qui est affiché comme filtré
    const query = new URLSearchParams({ filtre, ...(state.satellite ? { satellite: state.satellite } : {}) });
    const where = state.satellite ? ` du satellite « ${state.satellite} »` : "";
    const { nombre } = await api("GET", "/api/phrases/compte?" + query);
    if (!nombre || !confirm(`Supprimer définitivement les ${nombre} phrase(s) « ${label} »${where} et leur audio ?`)) return;
    const r = await api("DELETE", "/api/phrases?" + query);
    toast(`${r.supprimees} phrase(s) supprimée(s)`);
  } catch (e) {
    toast(e.message, true);
  }
  refresh();
}

// --- Personnes ---
const openSamples = new Set();

async function renderPersonnes() {
  const root = document.getElementById("personnes");
  const nom = h("input", { placeholder: "Nom", required: true });
  const ent = h("input", { placeholder: "person.xxx (facultatif)" });
  const form = h("form", { class: "card row", onsubmit: (e) => {
    e.preventDefault();
    act(() => api("POST", "/api/personnes", { nom: nom.value, person_entity: ent.value || null }), "Personne créée");
  } }, h("strong", {}, "Nouvelle personne"), nom, ent, h("button", { class: "primary" }, "Créer"));

  const cards = [];
  for (const p of state.personnes) {
    const src = Object.entries(p.par_source).map(([k, v]) => `${k} ${v}`).join(", ") || "aucun";
    const n = h("input", { type: "number", min: 1, max: 100, value: 12, title: "Nombre de phrases" });
    const fileInput = h("input", { type: "file", accept: "audio/*", multiple: true, hidden: true, onchange: (e) => upload(p.id, e.target.files, "fichier") });
    const entity = h("input", { value: p.person_entity || "", placeholder: "person.xxx" });
    const card = h("div", { class: "card" },
      h("div", { class: "row" },
        h("h2", { class: "grow" }, p.nom, " ",
          h("button", { class: "icon", title: "Renommer", "aria-label": `Renommer ${p.nom}`, onclick: () => renamePerson(p) }, "✎"), " ",
          h("span", { class: "muted" }, `${p.echantillons} échantillons (${src})`)),
        h("button", { onclick: () => { openSamples.has(p.id) ? openSamples.delete(p.id) : openSamples.add(p.id); refresh(); } }, openSamples.has(p.id) ? "Masquer" : "Échantillons"),
        h("button", { class: "danger", onclick: () => confirm(`Supprimer ${p.nom} et ses échantillons ?`) && act(() => api("DELETE", `/api/personnes/${p.id}`), "Supprimée") }, "Supprimer")),
      h("div", { class: "row" },
        h("span", { class: "muted" }, "Entité HA :"), entity,
        h("button", { onclick: () => act(() => api("PATCH", `/api/personnes/${p.id}`, { person_entity: entity.value }), "Enregistré") }, "OK"),
        state.etat.ha_url && p.person_entity ? h("a", { href: `${state.etat.ha_url}/config/person`, target: "_blank" }, "ouvrir dans HA") : null),
      h("div", { class: "row", style: "margin-top:8px" },
        h("button", { class: "primary", onclick: () => act(() => api("POST", "/api/enrolement", { personne_id: p.id, n: Number(n.value) || 12 }), "Enrôlement lancé : parlez aux satellites") }, "Enrôler par les satellites"), n,
        h("button", { onclick: () => fileInput.click() }, "Envoyer des fichiers"), fileInput,
        recordButton(p.id)));
    if (openSamples.has(p.id)) card.append(await samplesList(p.id));
    cards.push(card);
  }
  root.replaceChildren(form, ...(cards.length ? cards : [h("div", { class: "empty" }, "Aucune personne. Créez-en une puis enrôlez-la.")]));
}

function renamePerson(p) {
  const nom = prompt(`Nouveau nom pour ${p.nom} :`, p.nom);
  if (nom === null || !nom.trim() || nom.trim() === p.nom) return;
  // Les échantillons, phrases et la reprise suivent : ils sont liés à la personne, pas au nom.
  act(() => api("PATCH", `/api/personnes/${p.id}`, { nom: nom.trim() }), "Personne renommée");
}

async function samplesList(pid) {
  const samples = await api("GET", `/api/personnes/${pid}/echantillons`);
  const modele = state.etat.modele_charge;
  return h("div", { class: "samples" }, samples.length ? samples.map((s) => h("div", { class: "sample" },
    s.audio ? playButton(`/api/echantillons/${s.id}/audio`) : h("span", { class: "play muted" }, "—"),
    h("span", { class: "badge none" }, s.source),
    s.modele !== modele ? h("span", { class: "badge warn", title: "Empreinte d'un autre modèle" }, s.modele) : null,
    h("span", { class: "muted" }, fmtDate(s.cree_le), s.duree ? ` · ${s.duree.toFixed(1)} s` : ""),
    h("span", { class: "grow transcript" }, s.transcript || ""),
    h("button", { class: "danger", onclick: () => act(() => api("DELETE", `/api/echantillons/${s.id}`)) }, "🗑"))) : h("div", { class: "muted" }, "Aucun échantillon."));
}

async function upload(pid, files, source) {
  if (!files || !files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("fichiers", f, f.name || "enregistrement.wav");
  toast("Envoi…");
  try {
    const r = await api("POST", `/api/personnes/${pid}/fichiers?source=${source}`, fd);
    toast(`${r.ajoutes} échantillon(s) ajouté(s)` + (r.erreurs.length ? ` — ${r.erreurs.join(" ; ")}` : ""), r.erreurs.length > 0 && !r.ajoutes);
  } catch (e) {
    toast(e.message, true);
  }
  refresh();
}

// Enregistrement dans le navigateur : MediaRecorder, puis décodage et ré-échantillonnage à 16 kHz.
let recorder = null;
function recordButton(pid) {
  const btn = h("button", { type: "button" }, recorder && recorder.pid === pid ? "■ Arrêter" : "🎙 Enregistrer");
  btn.addEventListener("click", async () => {
    if (recorder) { recorder.stop(); return; }
    if (!navigator.mediaDevices?.getUserMedia) {
      toast("Micro indisponible (le navigateur exige HTTPS ou localhost)", true);
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: true } });
      const chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.pid = pid;
      recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
      recorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        recorder = null;
        try {
          const wav = await toWav16k(new Blob(chunks));
          await upload(pid, [new File([wav], "navigateur.wav", { type: "audio/wav" })], "navigateur");
        } catch (e) {
          toast("Enregistrement illisible : " + e.message, true);
          refresh();
        }
      };
      recorder.start();
      toast("Enregistrement… parlez quelques phrases puis cliquez sur Arrêter");
      refresh();
    } catch (e) {
      recorder = null;
      toast("Micro refusé : " + e.message, true);
    }
  });
  return btn;
}

async function toWav16k(blob) {
  const buf = await blob.arrayBuffer();
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const decoded = await ctx.decodeAudioData(buf);
  ctx.close();
  const length = Math.ceil(decoded.duration * 16000);
  const off = new OfflineAudioContext(1, length, 16000);
  const src = off.createBufferSource();
  src.buffer = decoded;
  src.connect(off.destination);
  src.start();
  const rendered = await off.startRendering();
  const data = rendered.getChannelData(0);
  const out = new DataView(new ArrayBuffer(44 + data.length * 2));
  const w = (o, s) => [...s].forEach((c, i) => out.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF"); out.setUint32(4, 36 + data.length * 2, true); w(8, "WAVE"); w(12, "fmt ");
  out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, 1, true);
  out.setUint32(24, 16000, true); out.setUint32(28, 32000, true); out.setUint16(32, 2, true);
  out.setUint16(34, 16, true); w(36, "data"); out.setUint32(40, data.length * 2, true);
  for (let i = 0; i < data.length; i++) out.setInt16(44 + i * 2, Math.max(-1, Math.min(1, data[i])) * 0x7fff, true);
  return out.buffer;
}

// --- Voix inconnues ---
async function renderInconnus() {
  const root = document.getElementById("inconnus");
  const [groupes, ignores] = await Promise.all([api("GET", "/api/groupes"), api("GET", "/api/ignores")]);
  state.groupes = groupes;
  const cards = groupes.map((g) => {
    const others = groupes.filter((o) => o.id !== g.id);
    const merge = h("select", { onchange: (e) => {
      const v = Number(e.target.value);
      e.target.value = "";
      if (v) act(() => api("POST", "/api/groupes/fusionner", { source: g.id, cible: v }), "Groupes fusionnés");
    } }, h("option", { value: "" }, "Fusionner dans…"), others.map((o) => h("option", { value: o.id }, `${o.nom} (${o.taille})`)));
    return h("div", { class: "card" },
      h("div", { class: "row" },
        h("h2", { class: "grow" }, g.nom, " ", h("span", { class: "muted" }, `${g.taille} phrase(s) · vu ${fmtDate(g.derniere_apparition)}`)),
        h("button", { title: "Renommer", onclick: () => { const n = prompt("Nom du groupe :", g.nom); if (n !== null) act(() => api("PATCH", `/api/groupes/${g.id}`, { nom: n })); } }, "✎")),
      h("div", { class: "extraits" }, g.extraits.map((e) => h("div", { class: "extrait" },
        e.audio ? playButton(`/api/phrases/${e.id}/audio`) : h("span", { class: "play muted" }, "—"),
        h("span", { class: "transcript" }, e.transcript || "—")))),
      h("div", { class: "row" },
        personSelect((b) => act(() => api("POST", `/api/groupes/${g.id}/attribuer`, b), "Groupe attribué")),
        others.length ? merge : null,
        h("button", { title: "Télé, invité ponctuel… : les phrases proches seront ignorées", onclick: () => act(() => api("POST", `/api/groupes/${g.id}/ignorer`), "Groupe ignoré") }, "Ignorer"),
        h("button", { class: "danger", onclick: () => confirm("Supprimer ce groupe et ses phrases ?") && act(() => api("DELETE", `/api/groupes/${g.id}`), "Groupe supprimé") }, "Supprimer")));
  });
  const ign = ignores.length ? h("details", { class: "card" }, h("summary", {}, `Liste à ignorer (${ignores.length})`),
    h("table", {}, ignores.map((i) => h("tr", {}, h("td", {}, i.raison || ""), h("td", { class: "muted" }, fmtDate(i.cree_le)),
      h("td", {}, h("button", { onclick: () => act(() => api("DELETE", `/api/ignores/${i.id}`)) }, "Retirer")))))) : null;
  root.replaceChildren(
    h("div", { class: "toolbar" },
      h("button", { onclick: () => act(() => api("POST", "/api/groupes/recalculer"), "Recalcul lancé en tâche de fond") }, "Recalculer les groupes"),
      h("span", { class: "muted" }, "Groupes triés par taille ; trois extraits par groupe.")),
    ...(cards.length ? cards : [h("div", { class: "empty" }, "Aucune voix inconnue en attente.")]), ign);
}

// --- Réglages ---
const FIELDS = [
  ["seuil_identification", "Seuil d'identification", "number", 0.01],
  ["marge_min", "Marge minimale avec la 2ᵉ personne", "number", 0.01],
  ["agregation", "Agrégation des échantillons", "select", [["max", "Maximum"], ["top3", "Moyenne des 3 meilleurs"]]],
  ["duree_min_parole", "Parole minimale (s)", "number", 0.1],
  ["duree_min_enrolement", "Parole minimale en enrôlement (s)", "number", 0.1],
  ["reprise_active", "Reprendre l'interlocuteur sur les phrases trop courtes", "checkbox"],
  ["reprise_duree_s", "Durée de reprise (s, 0 à 600)", "number", 1, [0, 600]],
  ["enrolement_silencieux", "Jarvis ne répond pas pendant un enrôlement", "checkbox"],
  ["filtre_silence_actif", "Ne rien transcrire quand personne ne parle", "checkbox"],
  ["silence_max_s", "Parole en dessous de laquelle c'est du silence (s, 0 à 2)", "number", 0.05, [0, 2]],
  ["hallucinations", "Textes inventés par Whisper à ignorer (un par ligne ; « ~ » en tête : expression régulière)", "lines"],
  ["seuil_cluster", "Seuil de regroupement", "number", 0.01],
  ["seuil_ignore", "Seuil de la liste à ignorer", "number", 0.01],
  ["enrichissement_auto", "Enrichissement automatique", "checkbox"],
  ["seuil_auto", "Seuil d'enrichissement auto", "number", 0.01],
  ["marge_auto", "Marge d'enrichissement auto", "number", 0.01],
  ["max_echantillons", "Échantillons max par personne", "number", 1],
  ["retention_jours", "Conservation de l'audio (jours)", "number", 1],
  ["garder_audio", "Garder l'audio des phrases", "checkbox"],
  ["garder_audio_echantillons", "Garder l'audio des échantillons au-delà de la rétention", "checkbox"],
];

async function renderReglages() {
  const root = document.getElementById("reglages");
  const [cfg, modeles] = await Promise.all([api("GET", "/api/reglages"), api("GET", "/api/modeles")]);
  const v = cfg.valeurs;
  const inputs = {};
  const fields = FIELDS.map(([key, label, type, opt, bounds]) => {
    let input;
    if (type === "lines") {
      input = h("textarea", { rows: 5, spellcheck: "false" });
      input.value = (v[key] || []).join("\n");
      inputs[key] = () => input.value.split("\n").map((l) => l.trim()).filter(Boolean);
      return h("label", { class: "wide" }, label, input);
    }
    if (type === "checkbox") {
      input = h("input", { type: "checkbox", checked: !!v[key] });
      inputs[key] = () => input.checked;
      return h("label", { class: "check" }, input, label);
    }
    if (type === "select") {
      input = h("select", {}, opt.map(([val, l]) => h("option", { value: val, selected: v[key] === val }, l)));
      inputs[key] = () => input.value;
    } else {
      input = h("input", { type: "number", step: opt, value: v[key], min: bounds?.[0], max: bounds?.[1] });
      inputs[key] = () => Number(input.value);
    }
    return h("label", {}, label, input);
  });
  const modele = h("select", {}, modeles.map((m) => h("option", { value: m.cle, selected: (v.modele || cfg.modele_env) === m.cle }, `${m.cle} — ${m.description}`)));
  const save = () => {
    const body = Object.fromEntries(Object.entries(inputs).map(([k, f]) => [k, f()]));
    const newModel = modele.value;
    if (newModel !== (v.modele || cfg.modele_env)) {
      if (!confirm("Changer de modèle recalcule toutes les empreintes depuis l'audio conservé. Continuer ?")) return;
      body.modele = newModel;
    }
    act(() => api("PUT", "/api/reglages", body), "Réglages enregistrés");
  };
  const calib = h("div", {});
  const importInput = h("input", { type: "file", accept: ".zip", hidden: true, onchange: (e) => {
    const f = e.target.files[0];
    if (!f || !confirm("Remplacer la base actuelle par cette sauvegarde ? (la base actuelle est sauvegardée dans /data)")) return;
    const fd = new FormData();
    fd.append("archive", f);
    act(() => api("POST", "/api/import", fd), "Sauvegarde importée");
  } });
  root.replaceChildren(
    h("div", { class: "card" }, h("h2", {}, "Identification et regroupement"), h("div", { class: "form-grid" }, ...fields,
      h("label", {}, "Modèle d'empreinte", modele)),
      h("div", { class: "toolbar", style: "margin-top:12px" }, h("button", { class: "primary", onclick: save }, "Enregistrer"))),
    h("div", { class: "card" }, h("h2", {}, "Calibrage"),
      h("p", { class: "muted" }, "Compare les phrases d'une même personne et de personnes différentes, puis propose seuil et marge (validation croisée sur les échantillons)."),
      h("button", { onclick: () => runCalibration(calib) }, "Lancer le calibrage"), calib),
    h("div", { class: "card" }, h("h2", {}, "Maintenance"),
      h("div", { class: "toolbar" },
        h("a", { href: url("/api/export", { audio: "true" }) }, h("button", { type: "button" }, "Exporter (avec audio)")),
        h("a", { href: url("/api/export", { audio: "false" }) }, h("button", { type: "button" }, "Exporter (empreintes seules)")),
        h("button", { onclick: () => importInput.click() }, "Importer une sauvegarde"), importInput,
        h("button", { onclick: () => act(() => api("POST", "/api/empreintes/recalculer"), "Recalcul lancé") }, "Recalculer les empreintes"),
        h("button", { onclick: () => act(async () => { const r = await api("POST", "/api/retention"); toast(`${r.supprimes} extrait(s) supprimé(s)`); }) }, "Appliquer la rétention")),
      taskInfo()));
}

function taskInfo() {
  const t = state.etat.tache || {};
  if (!t.nom) return h("div", { class: "muted" }, "Aucune tâche récente.");
  const pct = Math.round((t.progression || 0) * 100);
  return h("div", { class: "muted" }, `Tâche « ${t.nom} » : ${t.etat}${t.etat === "en_cours" ? ` (${pct} %)` : ""}${t.message ? " — " + t.message : ""}${t.resultat ? " — " + JSON.stringify(t.resultat) : ""}`);
}

async function runCalibration(target) {
  target.replaceChildren(h("p", { class: "muted" }, "Calcul…"));
  try {
    const r = await api("POST", "/api/calibrage");
    const p = r.proposition;
    const pct = (x) => (x * 100).toFixed(1) + " %";
    const hist = (a, b) => {
      const max = Math.max(1, ...a.counts, ...b.counts);
      return h("div", { class: "hist", title: "vert : même personne · rouge : personnes différentes (de -0,2 à 1)" },
        a.counts.map((c, i) => h("div", {},
          h("div", { class: "meme", style: `height:${(c / max) * 100}%` }),
          h("div", { class: "diff", style: `height:${(b.counts[i] / max) * 100}%` }))));
    };
    target.replaceChildren(
      h("p", {}, `${r.personnes} personnes, ${r.echantillons} échantillons — EER ${r.eer !== null ? pct(r.eer) : "—"} (seuil ${r.seuil_eer ?? "—"})`),
      hist(r.hist_meme, r.hist_diff),
      h("div", { class: "legende" }, h("span", {}, "même personne"), h("span", { class: "diff" }, "personnes différentes"), h("span", { class: "muted" }, "similarité de −0,2 à 1")),
      h("p", {}, `Réglage actuel (${r.actuel.seuil_identification} / ${r.actuel.marge_min}) : ${pct(r.actuel.precision)} de bonnes identifications, ${pct(r.actuel.taux_erreur)} d'erreurs.`),
      p ? h("div", {},
        h("p", {}, h("strong", {}, `Proposition : seuil ${p.seuil_identification}, marge ${p.marge_min}`),
          ` → ${pct(p.precision)} bonnes, ${pct(p.taux_erreur)} erreurs, ${pct(p.taux_inconnu)} inconnues (erreurs max ${pct(p.max_erreur)}).`),
        h("button", { class: "primary", onclick: () => act(() => api("PUT", "/api/reglages", { seuil_identification: p.seuil_identification, marge_min: p.marge_min }), "Seuils appliqués") }, "Appliquer"),
        r.confusion ? h("table", {}, h("tr", {}, h("th", {}, "Vrai"), h("th", {}, "Prédit")),
          Object.entries(r.confusion).map(([t, row]) => h("tr", {}, h("td", {}, t), h("td", {}, Object.entries(row).map(([k, n]) => `${k} : ${n}`).join(", "))))) : null)
        : h("p", {}, "Pas assez d'échantillons pour proposer des seuils."));
  } catch (e) {
    target.replaceChildren(h("p", { class: "badge bad" }, e.message));
  }
}

// --- Bandeau, onglets, actualisation ---
function renderBandeau() {
  const el = document.getElementById("enrolement");
  const e = state.etat.enrolement;
  el.hidden = !e;
  const muet = state.reglages.enrolement_silencieux ? " Jarvis ne répond pas pendant l'enrôlement." : "";
  if (e) el.replaceChildren(h("span", {}, `🎙 Enrôlement de ${e.personne} : ${e.n}/${e.total} phrases — parlez à un satellite (« Jarvis… » puis une phrase d'au moins 2 s).${muet}`),
    h("button", { onclick: () => act(() => api("DELETE", "/api/enrolement"), "Enrôlement arrêté") }, "Arrêter"));
  const s = state.etat;
  document.getElementById("statut").textContent = s.modele_charge
    ? `v${s.version}${s.commit ? ` (${s.commit})` : ""} · ${s.modele_charge} · ${s.personnes} pers. · ${s.phrases} phrases`
    : `⚠ modèle non chargé${s.modele_erreur ? " : " + s.modele_erreur : ""}`;
}

let busy = false;
async function refresh() {
  if (busy) return;
  busy = true;
  try {
    const [etat, personnes, reglages] = await Promise.all([api("GET", "/api/etat"), api("GET", "/api/personnes"), api("GET", "/api/reglages")]);
    state.etat = etat;
    state.personnes = personnes;
    state.reglages = reglages.valeurs;
    renderBandeau();
    if (state.tab === "journal") await renderJournal();
    else if (state.tab === "personnes") await renderPersonnes();
    else if (state.tab === "inconnus") await renderInconnus();
    else if (state.tab === "vocabulaire") await renderVocabulaire();
    else if (state.tab === "reglages") await renderReglages();
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy = false;
  }
}

document.getElementById("tabs").addEventListener("click", (e) => {
  const tab = e.target.dataset?.tab;
  if (!tab) return;
  state.tab = tab;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === tab));
  refresh();
});

let autoRefresh = true;
setInterval(() => {
  const playing = currentAudio && !currentAudio.paused;
  const editing = document.activeElement && ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);
  if (document.hidden || playing || editing || recorder) return;
  if (state.tab === "journal" && autoRefresh) refresh();
  else if (state.etat.enrolement || state.etat.tache?.etat === "en_cours") refresh();
}, 5000);

refresh();
