from pathlib import Path
import shutil

fpath = Path('templates/stock.html')
c = fpath.read_text(encoding='utf-8')

old = """function aplicarFiltros() {
  const promotor = document.getElementById('fPromotor').value;
  const comercio = document.getElementById('fComercioVal').value;
  const producto = document.getElementById('fProducto').value;
  const alerta   = document.getElementById('fAlerta').value;
  let f = filas;
  if (promotor) f = f.filter(r => r.promotor === promotor);
  if (comercio) f = f.filter(r => r.comercio === comercio);
  if (producto) f = f.filter(r => r.producto === producto);"""

new = """function aplicarFiltros() {
  const promotor = document.getElementById('fPromotor').value;
  const comercio = document.getElementById('fComercioVal').value;
  const producto = document.getElementById('fProducto').value;
  const alerta   = document.getElementById('fAlerta').value;
  const fecha    = document.getElementById('fFecha')?.value;
  let f = filas;
  if (fecha) {
    const [y, m, d] = fecha.split('-');
    f = f.filter(r => r.fecha?.substring(0, 10) === d + '-' + m + '-' + y);
  }
  if (promotor) f = f.filter(r => r.promotor === promotor);
  if (comercio) f = f.filter(r => r.comercio === comercio);
  if (producto) f = f.filter(r => r.producto === producto);"""

if old in c:
    shutil.copy2(fpath, 'templates/stock_bak2.html')
    c = c.replace(old, new)
    fpath.write_text(c, encoding='utf-8')
    print('✅ stock.html OK')
else:
    print('⚠️ No encontrado')