/* ================================================================
   CAPTA — Utilidades compartidas v1.0
   Cargado por navbar.html antes que cualquier template.
   ================================================================ */

window.captaWaitDb = function waitDb(fn) {
  if (window.db) { fn(); return; }
  const t = setInterval(() => {
    if (window.db) { clearInterval(t); fn(); }
  }, 50);
};

window.captaParseJ = function parseJ(v) {
  if (Array.isArray(v)) return v;
  if (!v) return [];
  try { return JSON.parse(v) || []; } catch { return []; }
};

window.captaFmtProdTxt = function fmtProdTxt(item) {
  if (!item) return '—';
  // Fallbacks: app móvil moderna usa "presentation", PWA legacy "producto" o "name"
  const name = item.presentation || item.producto || item.name || item.descripcion || '';
  if (!name && !item.gramaje) return '—';
  if (!item.gramaje) return name || '—';
  const g = item.gramaje % 1 === 0 ? parseInt(item.gramaje) : item.gramaje;
  const suffix = `${g}${item.unidad || ''}`;
  if (name && name.includes(suffix)) return name;
  return name ? `${name} — ${suffix}` : suffix;
};

window.captaFmtDate = function fmtDate(s) {
  if (!s) return '—';
  return new Date(s).toLocaleDateString('es-ES', {
    day: '2-digit', month: 'short', year: 'numeric'
  });
};

/**
 * Enriquece los campos myitems y competitoritems de un array de
 * registros de web_precios con gramaje/unidad desde los catálogos
 * web_myproductos y web_competidor.
 *
 * Solo completa los campos que lleguen null — no sobreescribe
 * registros que ya traigan gramaje (app móvil moderna).
 */
window.captaEnrichItems = async function enrichItems(records, empresaId, db) {
  if (!records || !records.length) return records;

  const [{ data: productos }, { data: competidores }] = await Promise.all([
    db.from('web_myproductos')
      .select('presentation, gramaje, unidad')
      .eq('empresa_id', empresaId),
    db.from('web_competidor')
      .select('presentation, gramaje, unidad')
      .eq('empresa_id', empresaId),
  ]);

  const prodIdx = {};
  (productos || []).forEach(p => {
    if (p.presentation)
      prodIdx[p.presentation.trim().toUpperCase()] = { gramaje: p.gramaje, unidad: p.unidad };
  });

  const compIdx = {};
  (competidores || []).forEach(c => {
    if (c.presentation)
      compIdx[c.presentation.trim().toUpperCase()] = { gramaje: c.gramaje, unidad: c.unidad };
  });

  const enrichArray = (items, idx) => {
    if (!Array.isArray(items)) {
      try { items = JSON.parse(items || '[]'); } catch { return []; }
    }
    return items.map(item => {
      if (!item || !item.presentation || item.gramaje != null) return item;
      const meta = idx[item.presentation.trim().toUpperCase()];
      if (!meta) return item;
      return { ...item, gramaje: meta.gramaje, unidad: meta.unidad };
    });
  };

  return records.map(record => ({
    ...record,
    myitems:         enrichArray(record.myitems,         prodIdx),
    competitoritems: enrichArray(record.competitoritems, compIdx),
  }));
};