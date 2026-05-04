from flask import Flask, render_template,flash, jsonify, request, session, redirect, url_for
import requests
import os
import time
from math import radians, sin, cos, sqrt, atan2
import json
from datetime import datetime, timedelta
from functools import wraps

app = Flask(__name__)

# ─── Configuración de seguridad ───────────────────────────────────────────────
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'tu-clave-secreta-super-larga-y-segura-2025-xyz123')

SUPABASE_URL = "https://djjylikkocemrlsjxscr.supabase.co"
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImRqanlsaWtrb2NlbXJsc2p4c2NyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjMxNjUyNDEsImV4cCI6MjA3ODc0MTI0MX0.fnv1BKn_o-PYEAPljG0V3dt3b2Uifwn8EEzkP8Aab3M')

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "count=exact"
}
def _supabase_error_msg(res):
    try:
        info = res.json()
        msg  = info.get("message") or info.get("details") or info.get("hint") or res.text
    except Exception:
        msg = res.text or "Error desconocido"
    # Traducir mensajes técnicos a algo legible
    if "uix_myproductos_nombre_gramaje_empresa" in msg or "uix_competidor_nombre_gramaje_empresa" in msg:
        return "Ya existe un producto con ese nombre y gramaje en tu empresa."
    if "unique" in msg.lower():
        return "Ya existe un producto con esos datos en tu empresa."
    return msg

# ─── Caché en memoria con TTL ─────────────────────────────────────────────────
# { cache_key: { "data": ..., "ts": timestamp } }
_cache: dict = {}
CACHE_TTL_SECONDS = 120  # 2 minutos — configurable


def cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < CACHE_TTL_SECONDS:
        return entry["data"]
    return None


def cache_set(key: str, data):
    _cache[key] = {"data": data, "ts": time.time()}



def cache_invalidate_prefix(prefix: str):
    """Invalida todas las entradas que empiecen con prefix."""
    keys_to_del = [k for k in _cache if k.startswith(prefix)]
    for k in keys_to_del:
        del _cache[k]


# ─── Decorador de sesión ──────────────────────────────────────────────────────
def require_session(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'empresa_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


# ─── Utilidades ───────────────────────────────────────────────────────────────
def fetch_table(table_name, params=None, empresa_id=None, limit=1000):
    """Fetch paginado contra Supabase REST API."""
    url = f"{SUPABASE_URL}/rest/v1/{table_name}"
    all_data = []
    offset = 0
    query_params = list(params or [])

    if empresa_id is not None:
        query_params.append(("empresa_id", f"eq.{empresa_id}"))

    while True:
        h = {**headers, "Range": f"{offset}-{offset + limit - 1}"}
        try:
            resp = requests.get(url, headers=h, params=query_params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            all_data.extend(data)
            if len(data) < limit:
                break
            offset += limit
        except requests.RequestException as e:
            print(f"[fetch_table] Error tabla {table_name}: {e}")
            break

    return all_data


def fetch_table_page(table_name, params, page: int, page_size: int):
    """
    Fetch de UNA página específica con count total.
    Retorna (data, total_count).
    """
    url = f"{SUPABASE_URL}/rest/v1/{table_name}"
    offset = (page - 1) * page_size
    h = {
        **headers,
        "Range": f"{offset}-{offset + page_size - 1}",
        "Prefer": "count=exact"
    }
    try:
        resp = requests.get(url, headers=h, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # Supabase devuelve Content-Range: 0-24/1000
        content_range = resp.headers.get("Content-Range", "")
        total = 0
        if "/" in content_range:
            try:
                total = int(content_range.split("/")[1])
            except ValueError:
                total = len(data)
        return data, total
    except requests.RequestException as e:
        print(f"[fetch_table_page] Error: {e}")
        return [], 0


def calculate_distance(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def get_week_date_range(year: int, week_number: int):
    jan4 = datetime(year, 1, 4)
    monday_w1 = jan4 - timedelta(days=jan4.weekday())
    start = monday_w1 + timedelta(weeks=week_number - 1)
    end = start + timedelta(days=6)
    return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')


def safe_json_parse(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    return []


def safe_float(value, default=None):
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def get_current_empresa():
    empresa_id = session.get('empresa_id')
    if not empresa_id:
        return None
    url = f"{SUPABASE_URL}/rest/v1/empresas?id=eq.{empresa_id}&select=id,nombre,planogram_image"
    resp = requests.get(url, headers=headers, timeout=5)
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    return None
def get_empresa_modulos():
    """Retorna el dict de modulos_activos de la empresa en sesión."""
    empresa_id = session.get('empresa_id')
    if not empresa_id:
        return None
    url = f"{SUPABASE_URL}/rest/v1/empresas?id=eq.{empresa_id}&select=modulos_activos"
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.ok and resp.json():
            raw = resp.json()[0].get('modulos_activos') or {}
            if isinstance(raw, str):
                import json as _json
                raw = _json.loads(raw)
            return raw
    except Exception:
        pass
    return {}
 
# ─── ALIASES DE MÓDULOS (Compatibilidad vieja ↔ nueva) ─────────────────────
_MOD_ALIASES = {
    # GPS
    'gps':              ['gps_verificacion'],
    'gps_verificacion': ['gps'],
    
    # Análisis
    'analisis_precios': ['analisis'],
    'analisis':         ['analisis_precios'],
}

def modulo_activo(key: str) -> bool:
    """
    Retorna True si el módulo está activo para la empresa actual.
    Soporta tanto las claves antiguas como las nuevas.
    """
    mods = get_empresa_modulos()
    
    # Si no hay restricciones de módulos (empresas antiguas), dar acceso total
    if not mods or len(mods) == 0:
        return True
    
    # 1. Verificar clave exacta
    if mods.get(key) is True:
        return True
    
    # 2. Verificar aliases (compatibilidad)
    for alias in _MOD_ALIASES.get(key, []):
        if mods.get(alias) is True:
            return True
    
    return False
 
def require_modulo(key):
    """Decorador que bloquea una ruta si el módulo no está activo."""
    from functools import wraps
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if 'empresa_id' not in session:
                return redirect(url_for('login'))
            if not modulo_activo(key):
                return render_template('modulo_bloqueado.html', modulo=key), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator


def process_record(record: dict, clientes_by_trade: dict) -> dict:
    """Convierte un raw record de Supabase al formato que espera el dashboard."""
    promoter_info = record.get('web_promotores') or {}
    visit_lat = safe_float(record.get("latitude"))
    visit_lon = safe_float(record.get("longitude"))

    trade_name = (record.get("trade") or "").strip().upper()
    cliente_data = clientes_by_trade.get(trade_name)

    distance = 0.0
    verified_status = "Cliente Desconocido"
    cliente_coords_str = "N/A"
    visit_coords_str = "N/A"

    if visit_lat is not None and visit_lon is not None:
        visit_coords_str = f"{visit_lat:.5f}, {visit_lon:.5f}"
        verified_status = "Sin Coordenadas Cliente"

    if cliente_data:
        c_lat = safe_float(cliente_data.get("latitude"), 0.0)
        c_lon = safe_float(cliente_data.get("longitude"), 0.0)
        if c_lat != 0.0:
            cliente_coords_str = f"{c_lat:.5f}, {c_lon:.5f}"
        if visit_lat is not None and visit_lon is not None and c_lat != 0.0:
            try:
                distance = calculate_distance(visit_lat, visit_lon, c_lat, c_lon)
                verified_status = "Confirmado" if distance <= 150 else "No Confirmado"
            except Exception:
                verified_status = "Error distancia"
        elif visit_lat is None or visit_lon is None:
            verified_status = "Sin GPS Visita"

    return {
        "id": record.get("id"),
        "created_at": record.get("created_at"),
        "promoter_name": promoter_info.get('promoter_name', "Sin Nombre"),
        "state": record.get("state", "N/A"),
        "zone": record.get("zone", "N/A"),
        "trade": record.get("trade", "N/A"),
        "distance": round(distance, 2),
        "verified": verified_status,
        "visit_coords": visit_coords_str,
        "client_coords": cliente_coords_str,
        "latitude": visit_lat,
        "longitude": visit_lon,
        "comments": record.get("comments", ""),
        "p_mayorista": record.get("p_mayorista", "No"),
        "cliente_cerrado": record.get("cliente_cerrado", "No"),
        "total_faces_before": record.get("total_faces_before"),
        "total_faces": record.get("total_faces"),
        "our_faces_before_manual": record.get("our_faces_before_manual"),
        "our_faces_after": record.get("our_faces_after"),
        "myitems": safe_json_parse(record.get("myitems")),
        "competitoritems": safe_json_parse(record.get("competitoritems")),
        "before_photos": safe_json_parse(record.get("before_photos")),
        "after_photos": safe_json_parse(record.get("after_photos")),
    }


def build_records_params(empresa_id, date_from=None, date_to=None,
                         promoter_id=None, week=None, year=None):
    """Construye la lista de params para query a web_precios."""
    year = year or datetime.now().year
    params = [
        ("select", "*,web_promotores!inner(promoter_name,promoter_id)"),
        ("order", "created_at.desc"),
        ("empresa_id", f"eq.{empresa_id}"),
    ]

    if week:
        try:
            d_from, d_to = get_week_date_range(int(year), int(week))
            params.append(("created_at", f"gte.{d_from}T00:00:00+00:00"))
            params.append(("created_at", f"lte.{d_to}T23:59:59+00:00"))
        except Exception as e:
            print(f"[build_params] Semana inválida {week}/{year}: {e}")
    else:
        if date_from:
            params.append(("created_at", f"gte.{date_from}T00:00:00+00:00"))
        if date_to:
            params.append(("created_at", f"lte.{date_to}T23:59:59+00:00"))

    if promoter_id and promoter_id != 'all':
        params.append(("promoter_id", f"eq.{promoter_id}"))

    return params


# ─── Rutas de vistas ──────────────────────────────────────────────────────────

@app.route('/logout')
def logout():
    empresa_id = session.get('empresa_id')
    # Limpiar caché de esta empresa antes de cerrar sesión
    if empresa_id:
        cache_invalidate_prefix(f"records:{empresa_id}")
        cache_invalidate_prefix(f"stats:{empresa_id}")
        cache_invalidate_prefix(f"weeks:{empresa_id}")
    session.clear()
    response = redirect(url_for('login'))
    # Forzar que el navegador no cachee esta respuesta
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response
 

@app.route('/analisis')
@require_modulo('analisis_precios')
def analisis():
    return render_template('analisis.html')
 
@app.route('/caras')
@require_modulo('caras')
def caras():
    return render_template('caras.html')
 
@app.route('/metros_espacios')
@require_modulo('metros_espacios')
def metros_espacios():
    return render_template('metros_espacios.html')
 
@app.route('/stock')
@require_modulo('stock')
def stock():
    return render_template('stock.html')
 
@app.route('/gps')
@require_modulo('gps_verificacion')
def gps():
    return render_template('GPS.html')
 
@app.route('/productos')
@require_modulo('productos')
def productos():
    return render_template('productos.html')
 
@app.route('/competencia')
@require_modulo('competencia')
def productos_competencia():
    return render_template('competencia.html')
 
@app.route('/planograma')
@require_modulo('planograma')
def planograma():
    empresa = get_current_empresa()
    if not empresa:
        return redirect(url_for('login'))
    return render_template('planograma.html',
                           empresa_nombre=empresa['nombre'],
                           empresa_id=empresa['id'])
 
@app.route('/lineas')
@require_modulo('lineas')
def lineas():
    return render_template('lineas.html')
 
@app.route('/clientes')
@require_modulo('clientes')
def clientes():
    return render_template('clientes.html')
 
@app.route('/promotores')
@require_modulo('promotores')
def promotores():
    return render_template('promotores.html')

@app.route('/espacios_adicionales')
@require_modulo('espacios_adicionales')
def espacios_adicionales():
     return render_template('espacios_adicionales.html')

@app.route('/reportes')
@require_modulo('reportes')
def reportes():
    return render_template('reportes.html')
 
# Dashboard y admin no tienen restricción de módulo
@app.route('/dashboard')
@require_session
def dashboard():
    return render_template('dashboard.html')
 
@app.route('/admin')
def admin():
    return render_template('admin.html')
 


# ─── Login / Logout ───────────────────────────────────────────────────────────


@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'GET' and session.get('empresa_id'):
        return redirect(url_for('dashboard'))
 
    if request.method == 'POST':
        # Leer del form HTML (no JSON)
        nombre = request.form.get('empresa', '').strip().upper()
        clave  = request.form.get('clave',   '').strip()
 
        if not nombre or not clave:
            return render_template('login.html', error='Completa ambos campos')
 
        # Buscar empresa por nombre exacto (ilike = case-insensitive en Supabase)
        url = (f"{SUPABASE_URL}/rest/v1/empresas"
               f"?nombre=ilike.{nombre}"
               f"&select=id,nombre,clave_acceso,estatus,fecha_vencimiento"
               f"&limit=10")
 
        try:
            response = requests.get(url, headers=headers)
            if response.status_code != 200:
                return render_template('login.html', error='Error de conexión con la base de datos')
 
            empresas = response.json()
 
            # Buscar coincidencia EXACTA de nombre + clave
            empresa = None
            for e in empresas:
                if e.get('nombre', '').upper() == nombre and e.get('clave_acceso') == clave:
                    empresa = e
                    break
 
            if not empresa:
                nombres = [e.get('nombre', '').upper() for e in empresas]
                if nombre in nombres:
                    return render_template('login.html', error='Clave incorrecta')
                return render_template('login.html', error='Empresa no encontrada')
 
            estatus = empresa.get('estatus', 'activa')
            if estatus == 'bloqueada':
                return render_template('login.html', error='bloqueada')
            elif estatus == 'suspendida':
                return render_template('login.html', error='suspendida')
            elif estatus == 'inactiva':
                return render_template('login.html', error='inactiva')
 
            # Limpiar sesión anterior y guardar la nueva
            session.clear()
            session['empresa_id']     = empresa['id']
            session['empresa_nombre'] = empresa['nombre']
 
            return redirect(url_for('dashboard'))
 
        except Exception as e:
            print(f"[LOGIN ERROR] {str(e)}")
            return render_template('login.html', error='Error interno del servidor')
 
    # GET — mostrar formulario
    return render_template('login.html')


# ─── API: Configuración pública del dashboard (sin exponer SUPABASE_KEY) ──────
@app.route('/api/dashboard/config')
@require_session
def dashboard_config():
    """
    Retorna solo lo necesario para que el frontend inicialice.
    NUNCA expone SUPABASE_KEY.
    """
    return jsonify({
        "empresa_id": session['empresa_id'],
        "empresa_nombre": session.get('empresa_nombre', ''),
    
    })


@app.route('/api/config')
def get_config():
    """
    Devuelve las credenciales públicas de Supabase SOLO si el usuario
    tiene sesión activa en Flask. El frontend las recibe en memoria,
    nunca se hardcodean en el HTML ni en archivos estáticos.
    """
    if 'empresa_id' not in session:
        return jsonify({'error': 'No autorizado'}), 401
 
    return jsonify({
        'supabase_url': SUPABASE_URL,
        'supabase_key': SUPABASE_KEY
    })

@app.route('/api/pwa-config')
def pwa_config():
    """
    Endpoint seguro que sirve la configuración de Supabase a la PWA.
    La key nunca viaja en el HTML estático — la PWA la fetchea en runtime.
    Solo devuelve la anon key (nunca la service_role key).
    """
    return jsonify({
        "supabase_url": SUPABASE_URL,
        "supabase_key": SUPABASE_KEY
    })

# ─── API: Records con paginación server-side ──────────────────────────────────
@app.route('/api/records', methods=['GET'])
def get_records():
    empresa_id = request.args.get('empresa_id')
    if not empresa_id:
        return jsonify({"error": "empresa_id es requerido"}), 400

    date_from   = request.args.get('date_from')
    date_to     = request.args.get('date_to')
    promoter_id = request.args.get('promoter_id')
    week        = request.args.get('week')
    year_str    = request.args.get('year', str(datetime.now().year))

    # ── Nuevos filtros ────────────────────────────────────────────────
    linea_id_filter = request.args.get('linea_id', '').strip()
    trade_filter    = request.args.get('trade', '').strip()

    params = [
        ("select", "*,web_promotores!inner(promoter_name, promoter_id)"),
        ("order",      "created_at.desc"),
        ("empresa_id", f"eq.{empresa_id}")
    ]

    try:
        year = int(year_str)
    except Exception:
        year = datetime.now().year

    # ── Rango de fechas ───────────────────────────────────────────────
    if week:
        try:
            week_num = int(week)
            date_from, date_to = get_week_date_range(year, week_num)
            params.append(("created_at", f"gte.{date_from}T00:00:00+00:00"))
            params.append(("created_at", f"lte.{date_to}T23:59:59+00:00"))
        except Exception as e:
            print(f"Error calculando semana {week}/{year}: {e}")
            return jsonify({"error": "Rango de semana inválido"}), 400
    elif date_from:
        params.append(("created_at", f"gte.{date_from}T00:00:00+00:00"))

    if date_to and not week:
        params.append(("created_at", f"lte.{date_to}T23:59:59+00:00"))

    # ── Filtros individuales ──────────────────────────────────────────
    if promoter_id and promoter_id != 'all':
        params.append(("promoter_id", f"eq.{promoter_id}"))

    if linea_id_filter and linea_id_filter != 'all':
        params.append(("linea_id", f"eq.{linea_id_filter}"))

    if trade_filter and trade_filter != 'all':
        params.append(("trade", f"eq.{trade_filter}"))

    # ── Consultas paralelas ───────────────────────────────────────────
    records_raw    = fetch_table("web_precios",    params=params)
    clientes_todos = fetch_table("web_clientes",   empresa_id=empresa_id,
                                 params=[("order", "trade_name.asc")])
    promotores     = fetch_table("web_promotores", empresa_id=empresa_id)
    estados        = fetch_table("web_estados")
    zonas          = fetch_table("web_zonas")
    lineas         = fetch_table("web_lineas",     empresa_id=empresa_id,
                                 params=[("activa", "eq.true"), ("order", "nombre.asc")])

    # Mapa id → cliente para cálculo de distancia y verificación
    clientes_by_id = {}
    for c in clientes_todos:
        cid = c.get("id")
        if cid is not None:
            try:
                clientes_by_id[int(cid)] = c
            except Exception:
                continue

    # ── Formatear registros ───────────────────────────────────────────
    formatted_records = []

    for record in records_raw:
        try:
            promoter_info = record.get('web_promotores') or {}

            visit_lat = safe_float(record.get("latitude"))
            visit_lon = safe_float(record.get("longitude"))

            cliente_id_raw = record.get("cliente_id")
            cliente_id = None
            if cliente_id_raw is not None:
                try:
                    cliente_id = int(cliente_id_raw)
                except Exception:
                    pass

            cliente_data      = clientes_by_id.get(cliente_id)
            distance          = 0.0
            verified_status   = "Cliente Desconocido"
            cliente_coords_str = "N/A"

            if cliente_data:
                c_lat = safe_float(cliente_data.get("latitude"), 0.0)
                c_lon = safe_float(cliente_data.get("longitude"), 0.0)

                if c_lat != 0.0:
                    cliente_coords_str = f"{c_lat:.5f}, {c_lon:.5f}"

                if visit_lat is not None and visit_lon is not None and c_lat != 0.0:
                    try:
                        distance = calculate_distance(visit_lat, visit_lon, c_lat, c_lon)
                        verified_status = "Confirmado" if distance <= 150 else "No Confirmado"
                    except Exception as dist_err:
                        print(f"Error distancia registro {record.get('id')}: {dist_err}")
                        verified_status = "Error cálculo distancia"
                elif visit_lat is None or visit_lon is None:
                    verified_status = "Sin GPS Visita"

            formatted_records.append({
                "id":              record.get("id"),
                "created_at":      record.get("created_at"),
                "promoter_name":   promoter_info.get('promoter_name', "Sin Nombre"),
                "promoter_id":     record.get("promoter_id"),
                "state":           record.get("state", "N/A"),
                "zone":            record.get("zone", "N/A"),
                "trade":           record.get("trade", "N/A"),
                # ── campos sincronizados con PWA ──────────────────────
                "linea_id":        record.get("linea_id"),
                "linea_nombre":    record.get("linea_nombre"),
                "shelf_meters":    record.get("shelf_meters"),
                "p_mayorista":     record.get("p_mayorista"),
                "cliente_cerrado": record.get("cliente_cerrado"),
                # ── caras ─────────────────────────────────────────────
                "our_faces_after":          record.get("our_faces_after"),
                "our_faces_before_counted": record.get("our_faces_before_counted"),
                "our_faces_before_manual":  record.get("our_faces_before_manual"),
                "total_faces":              record.get("total_faces"),
                "total_faces_before":       record.get("total_faces_before"),
                # ── geo ───────────────────────────────────────────────
                "distance":      round(distance, 2) if distance else 0,
                "verified":      verified_status,
                "latitude":      visit_lat,
                "longitude":     visit_lon,
                "client_coords": cliente_coords_str,
                # ── items ─────────────────────────────────────────────
                "myitems":          safe_json_parse(record.get("myitems")),
                "competitoritems":  safe_json_parse(record.get("competitoritems")),
                "before_photos":    safe_json_parse(record.get("before_photos")),
                "after_photos":     safe_json_parse(record.get("after_photos")),
                "comments":         record.get("comments"),
                "espacios_adicionales": safe_json_parse(record.get("espacios_adicionales")),
            })

        except Exception as e:
            print(f"Error procesando registro {record.get('id', 'sin-id')}: {e}")
            continue

    seen = set()
    promotores_unicos = []
    for p in promotores:
        pid = p.get("promoter_id")
        if pid and pid not in seen:
            seen.add(pid)
            promotores_unicos.append(p)
 
    return jsonify({
        "records":   formatted_records,
        "promoters": promotores_unicos,   # ← deduplicado
        "estados":   estados,
        "zonas":     zonas,
        "lineas":    lineas,
        "clientes":  clientes_todos,
    })


# ─── API: Stats rápidos (KPIs del top del dashboard) ─────────────────────────
@app.route('/api/records/stats')
def get_records_stats():
    """
    Devuelve conteos agregados SIN traer todos los registros.
    Usado por los KPI cards y el realtime polling.
    """
    empresa_id = request.args.get('empresa_id')
    date_from  = request.args.get('date_from')
    date_to    = request.args.get('date_to')
    if not empresa_id:
        return jsonify({"error": "empresa_id requerido"}), 400

    cache_key = f"stats:{empresa_id}:{date_from}:{date_to}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify({**cached, "from_cache": True})

    params = [
        ("select", "id,verified,promoter_id,created_at"),
        ("empresa_id", f"eq.{empresa_id}"),
    ]
    if date_from:
        params.append(("created_at", f"gte.{date_from}T00:00:00+00:00"))
    if date_to:
        params.append(("created_at", f"lte.{date_to}T23:59:59+00:00"))

    # Solo traemos los campos ligeros para contar
    records = fetch_table("web_precios", params=params)

    total = len(records)
    # Confirmados = distancia calculada en tiempo real pero para stats usamos proxy
    # (si el frontend ya tiene los records, que cuente él; este endpoint es para el header)
    result = {
        "total_visits": total,
        "last_updated": datetime.utcnow().isoformat(),
    }
    cache_set(cache_key, result)
    return jsonify(result)


# ─── API: Weeks with visits ───────────────────────────────────────────────────
@app.route('/api/weeks_with_visits', methods=['GET'])
def get_weeks_with_visits():
    empresa_id = request.args.get('empresa_id')
    if not empresa_id:
        return jsonify({"error": "empresa_id requerido"}), 400

    year = request.args.get('year', str(datetime.now().year))
    cache_key = f"weeks:{empresa_id}:{year}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    params = [
        ("select", "created_at"),
        ("empresa_id", f"eq.{empresa_id}"),
        ("created_at", f"gte.{year}-01-01T00:00:00+00:00"),
    ]
    records = fetch_table("web_precios", params=params)
    weeks = set()
    for r in records:
        try:
            dt = datetime.fromisoformat((r.get('created_at') or '').replace('Z', '+00:00'))
            weeks.add(dt.isocalendar()[1])
        except Exception:
            continue

    result = {"weeks": sorted(list(weeks), reverse=True)}
    cache_set(cache_key, result)
    return jsonify(result)


# ─── API: Delete records (invalida caché automáticamente) ────────────────────
@app.route('/delete_records', methods=['POST'])
def delete_records():
    data = request.json or {}
    ids = data.get("ids", [])
    empresa_id = data.get("empresa_id")
    if not empresa_id or not ids:
        return jsonify({"success": False, "error": "Parámetros inválidos"}), 400

    id_list = ",".join(map(str, ids))
    url = f"{SUPABASE_URL}/rest/v1/web_precios?id=in.({id_list})&empresa_id=eq.{empresa_id}"
    res = requests.delete(url, headers=headers, timeout=10)

    if res.ok:
        # Invalida caché de esta empresa
        cache_invalidate_prefix(f"records:{empresa_id}")
        cache_invalidate_prefix(f"stats:{empresa_id}")
        cache_invalidate_prefix(f"weeks:{empresa_id}")

    return jsonify({"success": res.ok})



# ──────────────────────────────────────────────────────────────────────
# PRODUCTOS COMPETENCIA
# ──────────────────────────────────────────────────────────────────────
@app.route('/api/competitorproducts', methods=['GET', 'POST'])
def handle_competitor_products():

    if request.method == 'GET':
        empresa_id = request.args.get('empresa_id')
        if not empresa_id:
            return jsonify({"error": "empresa_id es requerido"}), 400

        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/web_competidor"
            f"?empresa_id=eq.{empresa_id}"
            f"&select=id,presentation,gramaje,unidad,created_at"
            f"&order=presentation.asc",
            headers=headers, timeout=10
        )
        return jsonify({"products": res.json() if res.ok else []})

    if not request.is_json:
        return jsonify({"error": "Se esperaba JSON"}), 400

    data         = request.json
    empresa_id   = data.get("empresa_id")
    presentation = data.get("presentation", "").strip().upper()

    if not presentation:
        return jsonify({"error": "El nombre del producto es requerido"}), 400
    if not empresa_id:
        return jsonify({"error": "empresa_id es requerido"}), 400

    payload = {"presentation": presentation, "empresa_id": empresa_id}
    if data.get("gramaje") is not None:
        payload["gramaje"] = float(data["gramaje"])
    if data.get("unidad"):
        payload["unidad"] = data["unidad"]
    if data.get("linea_id"):
        payload["linea_id"] = data["linea_id"]

    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/web_competidor",
        headers=headers, json=payload, timeout=10
    )
    if res.status_code in (200, 201):
        return jsonify({"success": True}), 201
    return jsonify({"error": _supabase_error_msg(res)}), res.status_code


@app.route('/api/competitorproducts/<product_id>', methods=['PATCH', 'DELETE'])
def update_delete_competitor(product_id):

    if request.method == 'PATCH':
        if not request.is_json:
            return jsonify({"error": "Se esperaba JSON"}), 400
        empresa_id = request.json.get('empresa_id')
    else:
        empresa_id = request.args.get('empresa_id')

    if not empresa_id:
        return jsonify({"error": "empresa_id es requerido"}), 400

    check = requests.get(
        f"{SUPABASE_URL}/rest/v1/web_competidor"
        f"?id=eq.{product_id}&empresa_id=eq.{empresa_id}&select=id",
        headers=headers, timeout=10
    )
    if not check.ok or not check.json():
        return jsonify({"error": "Registro no encontrado o sin permiso"}), 404

    op_url = f"{SUPABASE_URL}/rest/v1/web_competidor?id=eq.{product_id}&empresa_id=eq.{empresa_id}"

    try:
        if request.method == 'PATCH':
            payload = {k: v for k, v in request.json.items() if k != 'empresa_id'}
            if 'gramaje' in payload and payload['gramaje'] is not None:
                payload['gramaje'] = float(payload['gramaje'])
            res = requests.patch(op_url, headers=headers, json=payload, timeout=10)
        else:
            res = requests.delete(op_url, headers=headers, timeout=10)

        if res.status_code in (200, 204):
            return jsonify({"success": True}), (200 if request.method == 'PATCH' else 204)
        return jsonify({"error": _supabase_error_msg(res)}), res.status_code

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── API: My products ────────────────────────────────────────────────────────
@app.route('/api/myproducts', methods=['GET', 'POST'])
def handle_my_products():

    # GET — listar productos de la empresa (con linea join)
    if request.method == 'GET':
        empresa_id = request.args.get('empresa_id')
        if not empresa_id:
            return jsonify({"error": "empresa_id es requerido"}), 400

        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/web_myproductos"
            f"?empresa_id=eq.{empresa_id}"
            f"&select=id,presentation,gramaje,unidad,linea_id,created_at,web_lineas(nombre)"
            f"&order=presentation.asc",
            headers=headers, timeout=10
        )
        return jsonify({"products": res.json() if res.ok else []})

    # POST — crear producto
    if not request.is_json:
        return jsonify({"error": "Se esperaba JSON"}), 400

    data         = request.json
    empresa_id   = data.get("empresa_id")
    presentation = data.get("presentation", "").strip().upper()

    if not presentation:
        return jsonify({"error": "El nombre del producto es requerido"}), 400
    if not empresa_id:
        return jsonify({"error": "empresa_id es requerido"}), 400

    payload = {"presentation": presentation, "empresa_id": empresa_id}
    if data.get("gramaje") is not None:
        payload["gramaje"] = float(data["gramaje"])
    if data.get("unidad"):
        payload["unidad"] = data["unidad"]
    if data.get("linea_id"):
        payload["linea_id"] = data["linea_id"]

    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/web_myproductos",
        headers=headers, json=payload, timeout=10
    )
    if res.status_code in (200, 201):
        return jsonify({"success": True}), 201
    return jsonify({"error": _supabase_error_msg(res)}), res.status_code


@app.route('/api/myproducts/<product_id>', methods=['PATCH', 'DELETE'])
def update_delete_myproduct(product_id):

    if request.method == 'PATCH':
        if not request.is_json:
            return jsonify({"error": "Se esperaba JSON"}), 400
        empresa_id = request.json.get('empresa_id')
    else:
        empresa_id = request.args.get('empresa_id')

    if not empresa_id:
        return jsonify({"error": "empresa_id es requerido"}), 400

    # Verificar propiedad
    check = requests.get(
        f"{SUPABASE_URL}/rest/v1/web_myproductos"
        f"?id=eq.{product_id}&empresa_id=eq.{empresa_id}&select=id",
        headers=headers, timeout=10
    )
    if not check.ok or not check.json():
        return jsonify({"error": "Registro no encontrado o sin permiso"}), 404

    op_url = f"{SUPABASE_URL}/rest/v1/web_myproductos?id=eq.{product_id}&empresa_id=eq.{empresa_id}"

    try:
        if request.method == 'PATCH':
            payload = {k: v for k, v in request.json.items() if k != 'empresa_id'}
            # Asegurar float en gramaje
            if 'gramaje' in payload and payload['gramaje'] is not None:
                payload['gramaje'] = float(payload['gramaje'])
            res = requests.patch(op_url, headers=headers, json=payload, timeout=10)
        else:
            res = requests.delete(op_url, headers=headers, timeout=10)

        if res.status_code in (200, 204):
            return jsonify({"success": True}), (200 if request.method == 'PATCH' else 204)
        return jsonify({"error": _supabase_error_msg(res)}), res.status_code

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── API: Planograma upload ───────────────────────────────────────────────────
@app.route('/api/upload_planogram', methods=['POST'])
def upload_planogram():
    empresa_id = session.get('empresa_id')
    if not empresa_id:
        return jsonify({"success": False, "error": "Sin sesión"}), 401

    file = request.files.get('file')
    if not file:
        return jsonify({"success": False, "error": "No se recibió archivo"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png'):
        return jsonify({"success": False, "error": "Usa JPG o PNG"}), 400

    try:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        file_name = f"planogramas/{empresa_id}/planograma_{timestamp}{ext}"
        storage_url = f"{SUPABASE_URL}/storage/v1/object/visits_photos/{file_name}"
        upload_resp = requests.put(
            storage_url,
            headers={"Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": file.content_type or "image/jpeg"},
            data=file.read(),
            timeout=30
        )
        if upload_resp.status_code not in (200, 201):
            return jsonify({"success": False, "error": "Error al subir al storage"}), 500

        update_resp = requests.patch(
            f"{SUPABASE_URL}/rest/v1/empresas?id=eq.{empresa_id}",
            headers=headers,
            json={"planogram_image": file_name},
            timeout=10
        )
        if update_resp.status_code not in (200, 204):
            return jsonify({"success": False, "error": "Error al actualizar BD"}), 500

        public_url = f"{SUPABASE_URL}/storage/v1/object/public/visits_photos/{file_name}"
        return jsonify({"success": True, "url": public_url})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    

#ESTADO DE CUENTAS

@app.route('/api/empresa/status')
@require_session
def empresa_status():
    """
    Retorna el estado de cuenta de la empresa:
    estatus, fecha_vencimiento, dias_gracia, y pagos vencidos/próximos.
    """
    empresa_id = session['empresa_id']
    hoy = datetime.now().date()

    # Datos de la empresa
    url_e = f"{SUPABASE_URL}/rest/v1/empresas?id=eq.{empresa_id}&select=estatus,fecha_vencimiento,dias_gracia,nombre"
    res_e = requests.get(url_e, headers=headers, timeout=5)
    if not res_e.ok or not res_e.json():
        return jsonify({'error': 'Empresa no encontrada'}), 404
    empresa = res_e.json()[0]

    estatus        = empresa.get('estatus', 'activa')
    fecha_venc_raw = empresa.get('fecha_vencimiento')
    dias_gracia    = empresa.get('dias_gracia') or 15

    # Calcular días para vencimiento del plan
    dias_para_vencer = None
    if fecha_venc_raw:
        fecha_venc = datetime.strptime(fecha_venc_raw, '%Y-%m-%d').date()
        dias_para_vencer = (fecha_venc - hoy).days

    # Pagos vencidos o próximos a vencer (capta_pagos)
    url_p = (f"{SUPABASE_URL}/rest/v1/capta_pagos"
             f"?empresa_id=eq.{empresa_id}"
             f"&select=tipo,concepto,vencimiento,estado,monto")
    res_p = requests.get(url_p, headers=headers, timeout=5)
    pagos = res_p.json() if res_p.ok else []

    alertas_pagos = []
    for p in pagos:
        if not p.get('vencimiento'):
            continue
        venc_pago = datetime.strptime(p['vencimiento'][:10], '%Y-%m-%d').date()
        dias_diff = (venc_pago - hoy).days
        if dias_diff < 0:
            alertas_pagos.append({
                'tipo': p['tipo'],
                'concepto': p.get('concepto', p['tipo']),
                'dias_vencido': abs(dias_diff),
                'nivel': 'vencido'
            })
        elif dias_diff <= 7:
            alertas_pagos.append({
                'tipo': p['tipo'],
                'concepto': p.get('concepto', p['tipo']),
                'dias_para_vencer': dias_diff,
                'nivel': 'proximo'
            })

    return jsonify({
        'estatus': estatus,
        'dias_para_vencer': dias_para_vencer,
        'dias_gracia': dias_gracia,
        'alertas_pagos': alertas_pagos,
        'nombre': empresa.get('nombre', '')
    })
    
    # ──────────────────────────────────────────────────────────────────────
# COMPETENCIA DIRECTA POR PRODUCTO
# GET  /api/producto_competencia?producto_id=X&empresa_id=Y  → lista
# POST /api/producto_competencia  {producto_id, competidor_id, empresa_id}
# DELETE /api/producto_competencia/<id>?empresa_id=Y
# ──────────────────────────────────────────────────────────────────────
@app.route('/api/producto_competencia', methods=['GET', 'POST'])
def handle_producto_competencia():

    if request.method == 'GET':
        producto_id = request.args.get('producto_id')
        empresa_id  = request.args.get('empresa_id')
        if not producto_id or not empresa_id:
            return jsonify({"error": "producto_id y empresa_id son requeridos"}), 400

        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/web_producto_competencia"
            f"?producto_id=eq.{producto_id}&empresa_id=eq.{empresa_id}"
            f"&select=id,competidor_id,web_competidor(id,presentation,gramaje,unidad)"
            f"&order=created_at.asc",
            headers=headers, timeout=10
        )
        return jsonify({"relaciones": res.json() if res.ok else []})

    if not request.is_json:
        return jsonify({"error": "Se esperaba JSON"}), 400

    data          = request.json
    empresa_id    = data.get("empresa_id")
    producto_id   = data.get("producto_id")
    competidor_id = data.get("competidor_id")

    if not all([empresa_id, producto_id, competidor_id]):
        return jsonify({"error": "empresa_id, producto_id y competidor_id son requeridos"}), 400

    # Verificar que producto pertenece a la empresa
    chk = requests.get(
        f"{SUPABASE_URL}/rest/v1/web_myproductos?id=eq.{producto_id}&empresa_id=eq.{empresa_id}&select=id",
        headers=headers, timeout=10
    )
    if not chk.ok or not chk.json():
        return jsonify({"error": "Producto no encontrado o sin permiso"}), 404

    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/web_producto_competencia",
        headers=headers,
        json={"empresa_id": empresa_id, "producto_id": producto_id, "competidor_id": competidor_id},
        timeout=10
    )
    if res.status_code in (200, 201):
        return jsonify({"success": True}), 201
    try:
        info = res.json()
        msg  = info.get("message", "")
    except Exception:
        msg = res.text
    if "unique" in msg.lower() or "web_producto_competencia_unique" in msg:
        return jsonify({"error": "Ese competidor ya está vinculado a este producto"}), 409
    return jsonify({"error": msg or "Error al vincular"}), res.status_code


@app.route('/api/producto_competencia/<relacion_id>', methods=['DELETE'])
def delete_producto_competencia(relacion_id):
    empresa_id = request.args.get('empresa_id')
    if not empresa_id:
        return jsonify({"error": "empresa_id es requerido"}), 400

    res = requests.delete(
        f"{SUPABASE_URL}/rest/v1/web_producto_competencia"
        f"?id=eq.{relacion_id}&empresa_id=eq.{empresa_id}",
        headers=headers, timeout=10
    )
    if res.status_code in (200, 204):
        return jsonify({"success": True}), 204
    return jsonify({"error": "No se pudo eliminar"}), res.status_code

@app.route('/api/lineas', methods=['GET'])
def api_lineas():
    empresa_id = request.args.get('empresa_id') or session.get('empresa_id')
    if not empresa_id:
        return jsonify({"error": "empresa_id requerido"}), 400
 
    lineas = fetch_table(
        "web_lineas",
        empresa_id=empresa_id,
        params=[("order", "nombre.asc")]
    )
    return jsonify({"lineas": lineas})



# ─── Entrypoint ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
