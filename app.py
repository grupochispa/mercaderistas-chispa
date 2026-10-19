from flask import Flask, render_template, jsonify, request, session, redirect, url_for
import requests
import os
import sys
import time
import threading
from math import radians, sin, cos, sqrt, atan2
import json
from datetime import datetime, timedelta, timezone
from functools import wraps

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════════════════
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'tu-clave-secreta-super-larga-y-segura-2025-xyz123')

SUPABASE_URL = os.getenv("SUPABASE_URL",https://fnmjdevxbimprposrhie.supabase.co)
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZubWpkZXZ4YmltcHJwb3NyaGllIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTI0MTEzMDQsImV4cCI6MjEwNzk4NzMwNH0.QsOUXyiK8nFp3ZNtYBg5vDWwIKhhdctO-V91BjlLdqc')

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "count=exact"
}

# Fotos de visita: se conservan 2 meses y luego se borran del bucket.
# La fila de web_precios (precios, stock, caras, GPS, comentarios) se mantiene.
PHOTO_RETENTION_MONTHS = int(os.getenv("PHOTO_RETENTION_MONTHS", "2"))
# Plan Free de Supabase: 1 GB. Al llegar al 80 % se borran fotos de visita
# aunque tengan menos de 2 meses, de la más antigua a la más nueva.
STORAGE_LIMIT_BYTES = int(os.getenv("STORAGE_LIMIT_BYTES", str(1024 ** 3)))
STORAGE_HEADROOM_RATIO = float(os.getenv("STORAGE_HEADROOM_RATIO", "0.8"))
VISIT_PHOTO_BUCKET = "visits_photos"
VISIT_PHOTO_FOLDERS = ("before", "after")
_PHOTO_PURGE_LOCK = threading.Lock()
_photo_retention_started = False

# ═══════════════════════════════════════════════════════════════════════════════
# CACHÉ EN MEMORIA CON TTL
# ═══════════════════════════════════════════════════════════════════════════════
_cache: dict = {}
CACHE_TTL_SECONDS = 120          # datos dinámicos (records, stats)
CACHE_STATIC_TTL = 3600          # datos semi-estáticos (estados, zonas)

def cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < CACHE_TTL_SECONDS:
        return entry["data"]
    return None

def cache_get_static(key: str):
    """Cache con TTL extendido para datos que cambian poco (estados, zonas)."""
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < CACHE_STATIC_TTL:
        return entry["data"]
    return None

def cache_set(key: str, data):
    _cache[key] = {"data": data, "ts": time.time()}

def cache_invalidate_prefix(prefix: str):
    """Invalida todas las entradas que empiecen con prefix."""
    keys_to_del = [k for k in _cache if k.startswith(prefix)]
    for k in keys_to_del:
        del _cache[k]

# ═══════════════════════════════════════════════════════════════════════════════
# DECORADORES
# ═══════════════════════════════════════════════════════════════════════════════

def require_session(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'empresa_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


# ── Aliases de módulos (compatibilidad vieja ↔ nueva) ─────────────────────────
_MOD_ALIASES = {
    'gps':              ['gps_verificacion'],
    'gps_verificacion': ['gps'],
    'analisis_precios': ['analisis'],
    'analisis':         ['analisis_precios'],
}

def get_empresa_modulos() -> dict:
    """Retorna SIEMPRE un dict {key: True} de modulos_activos.
    Compatible con array text[] (Chispa) y JSONB objeto (legacy)."""
    empresa_id = session.get('empresa_id')
    if not empresa_id:
        return {}
    url = f"{SUPABASE_URL}/rest/v1/empresas?id=eq.{empresa_id}&select=modulos_activos"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=5)
        if resp.ok and resp.json():
            raw = resp.json()[0].get('modulos_activos')
            if not raw:
                return {}
            if isinstance(raw, list):
                return {k: True for k in raw if isinstance(k, str)}
            if isinstance(raw, str):
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return {k: True for k in parsed if isinstance(k, str)}
                return parsed
            return raw
    except Exception:
        pass
    return {}

def modulo_activo(key: str) -> bool:
    """Retorna True si el módulo está activo para la empresa actual."""
    mods = get_empresa_modulos()
    if not mods:
        return True  # sin restricciones → acceso total
    if mods.get(key) is True:
        return True
    for alias in _MOD_ALIASES.get(key, []):
        if mods.get(alias) is True:
            return True
    return False

def require_modulo(key):
    """Decorador que bloquea una ruta si el módulo no está activo."""
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


# ═══════════════════════════════════════════════════════════════════════════════
# UTILIDADES
# ═══════════════════════════════════════════════════════════════════════════════

def supabase_url(table: str) -> str:
    """Construye URL base para la REST API de Supabase."""
    return f"{SUPABASE_URL}/rest/v1/{table}"

def _supabase_error_msg(res) -> str:
    """Traduce errores de Supabase a mensajes legibles para el usuario."""
    try:
        info = res.json()
        msg = info.get("message") or info.get("details") or info.get("hint") or res.text
    except Exception:
        msg = res.text or "Error desconocido"
    if "uix_myproductos_nombre_gramaje_empresa" in msg or "uix_competidor_nombre_gramaje_empresa" in msg:
        return "Ya existe un producto con ese nombre y gramaje en tu empresa."
    if "unique" in msg.lower():
        return "Ya existe un producto con esos datos en tu empresa."
    return msg

def _verificar_propiedad(table: str, record_id, empresa_id):
    """Verifica que un registro pertenece a la empresa. Retorna (ok, error_tuple)."""
    check = requests.get(
        f"{supabase_url(table)}?id=eq.{record_id}&empresa_id=eq.{empresa_id}&select=id",
        headers=HEADERS, timeout=10
    )
    if not check.ok or not check.json():
        return False, (jsonify({"error": "Registro no encontrado o sin permiso"}), 404)
    return True, None

def _crear_producto(table: str, data: dict):
    """Lógica común para crear productos (myproductos / competidor)."""
    empresa_id = data.get("empresa_id")
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

    res = requests.post(supabase_url(table), headers=HEADERS, json=payload, timeout=10)
    if res.status_code in (200, 201):
        return jsonify({"success": True}), 201
    return jsonify({"error": _supabase_error_msg(res)}), res.status_code

def _actualizar_o_eliminar(table: str, record_id, empresa_id, method: str, data: dict = None):
    """Lógica común para PATCH/DELETE de productos (myproductos / competidor)."""
    ok, err = _verificar_propiedad(table, record_id, empresa_id)
    if not ok:
        return err

    op_url = f"{supabase_url(table)}?id=eq.{record_id}&empresa_id=eq.{empresa_id}"
    try:
        if method == 'PATCH':
            payload = {k: v for k, v in data.items() if k != 'empresa_id'}
            if 'gramaje' in payload and payload['gramaje'] is not None:
                payload['gramaje'] = float(payload['gramaje'])
            res = requests.patch(op_url, headers=HEADERS, json=payload, timeout=10)
        else:
            res = requests.delete(op_url, headers=HEADERS, timeout=10)

        if res.status_code in (200, 204):
            return jsonify({"success": True}), (200 if method == 'PATCH' else 204)
        return jsonify({"error": _supabase_error_msg(res)}), res.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def fetch_table(table_name, params=None, empresa_id=None, limit=1000):
    """Fetch paginado contra Supabase REST API."""
    url = supabase_url(table_name)
    all_data = []
    offset = 0
    query_params = list(params or [])

    if empresa_id is not None:
        query_params.append(("empresa_id", f"eq.{empresa_id}"))

    while True:
        h = {**HEADERS, "Range": f"{offset}-{offset + limit - 1}"}
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
    """Fetch de UNA página específica con count total. Retorna (data, total_count)."""
    url = supabase_url(table_name)
    offset = (page - 1) * page_size
    h = {**HEADERS, "Range": f"{offset}-{offset + page_size - 1}", "Prefer": "count=exact"}
    try:
        resp = requests.get(url, headers=h, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
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
    """Distancia en metros entre dos coordenadas (fórmula Haversine)."""
    R = 6371000.0
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))

def get_week_date_range(year: int, week_number: int):
    """Retorna (fecha_inicio, fecha_fin) para una semana ISO."""
    jan4 = datetime(year, 1, 4)
    monday_w1 = jan4 - timedelta(days=jan4.weekday())
    start = monday_w1 + timedelta(weeks=week_number - 1)
    end = start + timedelta(days=6)
    return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')

def safe_json_parse(value):
    """Convierte un valor (str/None/list) a una lista Python de forma segura."""
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
    """Convierte a float de forma segura, sin excepciones."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default

def get_current_empresa():
    """Obtiene datos básicos de la empresa en sesión."""
    empresa_id = session.get('empresa_id')
    if not empresa_id:
        return None
    url = f"{supabase_url('empresas')}?id=eq.{empresa_id}&select=id,nombre,planogram_image"
    resp = requests.get(url, headers=HEADERS, timeout=5)
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    return None

def _compute_verification(visit_lat, visit_lon, cliente_data):
    """Calcula distancia y estado de verificación para una visita.
    Retorna (distance, verified_status, cliente_coords_str)."""
    distance = 0.0
    verified_status = "Sin Coords Cliente"
    cliente_coords_str = "N/A"

    if cliente_data:
        c_lat = safe_float(cliente_data.get("latitude"), 0.0)
        c_lon = safe_float(cliente_data.get("longitude"), 0.0)
        if c_lat != 0.0:
            cliente_coords_str = f"{c_lat:.5f}, {c_lon:.5f}"
        if visit_lat is None or visit_lon is None:
            verified_status = "Sin GPS Visita"
        elif c_lat == 0.0 or c_lon == 0.0:
            verified_status = "Sin Coords Cliente"
        else:
            try:
                distance = calculate_distance(visit_lat, visit_lon, c_lat, c_lon)
                verified_status = "Confirmado" if distance <= 150 else "No Confirmado"
            except Exception:
                verified_status = "Error cálculo distancia"
    elif visit_lat is not None and visit_lon is not None:
        verified_status = "Sin Coords Cliente"

    return distance, verified_status, cliente_coords_str

def _format_record(record, clientes_by_id, clientes_by_trade):
    """Convierte un raw record de Supabase al formato que espera el dashboard."""
    visit_lat = safe_float(record.get("latitude"))
    visit_lon = safe_float(record.get("longitude"))

    # Buscar cliente asociado
    trade_visita = (record.get("trade") or "").strip().upper()
    cliente_id_raw = record.get("cliente_id")
    cliente_data = None
    if cliente_id_raw is not None:
        cliente_data = clientes_by_id.get(str(cliente_id_raw)) or clientes_by_id.get(cliente_id_raw)
    if not cliente_data and trade_visita:
        cliente_data = clientes_by_trade.get(trade_visita)

    distance, verified_status, cliente_coords_str = _compute_verification(visit_lat, visit_lon, cliente_data)

    return {
        "id":              record.get("id"),
        "created_at":      record.get("created_at"),
        "promoter_name":   record.get("promoter_name") or "Sin Nombre",
        "promoter_id":     record.get("promoter_id"),
        "state":           record.get("state", "N/A"),
        "zone":            record.get("zone", "N/A"),
        "trade":           record.get("trade", "N/A"),
        "linea_id":        record.get("linea_id"),
        "linea_nombre":    record.get("linea_nombre"),
        "shelf_meters":    record.get("shelf_meters"),
        "p_mayorista":     record.get("p_mayorista"),
        "cliente_cerrado": record.get("cliente_cerrado"),
        "our_faces_after":          record.get("our_faces_after"),
        "our_faces_before_counted": record.get("our_faces_before_counted"),
        "our_faces_before_manual":  record.get("our_faces_before_manual"),
        "total_faces":              record.get("total_faces"),
        "total_faces_before":       record.get("total_faces_before"),
        "distance":      round(distance, 2) if distance else 0,
        "verified":      verified_status,
        "latitude":      visit_lat,
        "longitude":     visit_lon,
        "client_coords": cliente_coords_str,
        "myitems":          safe_json_parse(record.get("myitems")),
        "competitoritems":  safe_json_parse(record.get("competitoritems")),
        "before_photos":    safe_json_parse(record.get("before_photos")),
        "after_photos":     safe_json_parse(record.get("after_photos")),
        "comments":         record.get("comments"),
        "espacios_adicionales": safe_json_parse(record.get("espacios_adicionales")),
    }

def _build_cliente_indexes(clientes_todos):
    """Construye índices id→cliente y trade_name→cliente."""
    by_id = {}
    by_trade = {}
    for c in clientes_todos:
        cid = c.get("id")
        if cid is not None:
            try:
                by_id[int(cid)] = c
            except Exception:
                pass
            by_id[str(cid)] = c
        trade = (c.get("trade_name") or "").strip().upper()
        if trade:
            by_trade[trade] = c
    return by_id, by_trade

def _build_record_params(empresa_id, date_from=None, date_to=None,
                         promoter_id=None, week=None, year=None,
                         linea_id=None, trade=None):
    """Construye la lista de params para query a web_precios."""
    year = year or datetime.now().year
    params = [
        ("select", "*"),
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
    if linea_id and linea_id != 'all':
        params.append(("linea_id", f"eq.{linea_id}"))
    if trade and trade != 'all':
        params.append(("trade", f"eq.{trade}"))

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
    return render_template('gps.html')
 
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
            response = requests.get(url, headers=HEADERS)
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



# ── IA: Resumen inteligente via OpenRouter ─────────────────────────────────────
@app.route('/api/ia/resumen', methods=['POST'])
@require_session
def ia_resumen():
    """Proxy hacia OpenRouter — la key vive en OPENROUTER_KEY env var."""
    try:
        OR_KEY   = os.getenv('OPENROUTER_KEY', '').strip()  # strip() por si hay espacios
        OR_URL   = 'https://openrouter.ai/api/v1/chat/completions'
        OR_MODEL = 'openrouter/auto'

        if not OR_KEY:
            return jsonify({'error': 'OPENROUTER_KEY no configurada en el servidor. Ve a Render → Environment y agrega la variable.'}), 500
        
        # Log para debug (solo primeros 10 chars para no exponer la key)
        print(f"[IA] OR_KEY cargada: {OR_KEY[:10]}... ({len(OR_KEY)} chars)")

        body = request.get_json()
        prompt = body.get('prompt', '')
        if not prompt:
            return jsonify({'error': 'Prompt vacío'}), 400

        resp = requests.post(OR_URL, json={
            'model':       OR_MODEL,
            'messages':    [{'role': 'user', 'content': prompt}],
            'temperature': 0.7,
            'max_tokens':  1024,
        }, headers={
            'Authorization': f'Bearer {OR_KEY}',
            'HTTP-Referer':  request.host_url,
            'X-Title':       'CHISPA Dashboard',
            'Content-Type':  'application/json',
        }, timeout=30)

        if not resp.ok:
            try:
                err_body = resp.json()
                err = err_body.get('error', {}).get('message', '') or str(err_body)
            except Exception:
                err = f'HTTP {resp.status_code}: {resp.text[:200]}'
            print(f"[IA] OpenRouter error: {err}")
            return jsonify({'error': err}), resp.status_code

        data    = resp.json()
        content_text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
        return jsonify({'content': content_text})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
    linea_id    = request.args.get('linea_id', '').strip()
    trade       = request.args.get('trade', '').strip()

    try:
        year = int(year_str)
    except Exception:
        year = datetime.now().year

    params = _build_record_params(
        empresa_id, date_from=date_from, date_to=date_to,
        promoter_id=promoter_id, week=week, year=year,
        linea_id=linea_id, trade=trade
    )

    # ── Consultas paralelas ───────────────────────────────────────────
    records_raw    = fetch_table("web_precios",    params=params)
    clientes_todos = fetch_table("web_clientes",   empresa_id=empresa_id,
                                 params=[("order", "trade_name.asc")])
    promotores     = fetch_table("web_promotores", empresa_id=empresa_id)
    estados        = fetch_table("web_estados")
    zonas          = fetch_table("web_zonas")
    lineas         = fetch_table("web_lineas",     empresa_id=empresa_id,
                                 params=[("activa", "eq.true"), ("order", "nombre.asc")])

    # Construir índices de clientes
    clientes_by_id, clientes_by_trade = _build_cliente_indexes(clientes_todos)

    # Formatear registros usando el helper unificado
    formatted_records = []
    for record in records_raw:
        try:
            formatted_records.append(_format_record(record, clientes_by_id, clientes_by_trade))
        except Exception as e:
            print(f"Error procesando registro {record.get('id', 'sin-id')}: {e}")
            continue

    # Deduplicar promotores
    seen = set()
    promotores_unicos = []
    for p in promotores:
        pid = p.get("promoter_id")
        if pid and pid not in seen:
            seen.add(pid)
            promotores_unicos.append(p)
 
    return jsonify({
        "records":   formatted_records,
        "promoters": promotores_unicos,
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
    res = requests.delete(url, headers=HEADERS, timeout=10)

    if res.ok:
        # Invalida caché de esta empresa
        cache_invalidate_prefix(f"records:{empresa_id}")
        cache_invalidate_prefix(f"stats:{empresa_id}")
        cache_invalidate_prefix(f"weeks:{empresa_id}")

    return jsonify({"success": res.ok})



# ═══════════════════════════════════════════════════════════════════════════════
# API: PRODUCTOS COMPETENCIA
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/competitorproducts', methods=['GET', 'POST'])
def handle_competitor_products():
    if request.method == 'GET':
        empresa_id = request.args.get('empresa_id')
        if not empresa_id:
            return jsonify({"error": "empresa_id es requerido"}), 400

        res = requests.get(
            f"{supabase_url('web_competidor')}"
            f"?empresa_id=eq.{empresa_id}"
            f"&select=id,presentation,gramaje,unidad,created_at"
            f"&order=presentation.asc",
            headers=HEADERS, timeout=10
        )
        return jsonify({"products": res.json() if res.ok else []})

    # POST — usa helper compartido
    if not request.is_json:
        return jsonify({"error": "Se esperaba JSON"}), 400
    return _crear_producto("web_competidor", request.json)


@app.route('/api/competitorproducts/<product_id>', methods=['PATCH', 'DELETE'])
def update_delete_competitor(product_id):
    empresa_id = request.json.get('empresa_id') if request.method == 'PATCH' and request.is_json else request.args.get('empresa_id')
    if not empresa_id:
        return jsonify({"error": "empresa_id es requerido"}), 400
    return _actualizar_o_eliminar("web_competidor", product_id, empresa_id,
                                  request.method, request.json if request.method == 'PATCH' else None)


# ═══════════════════════════════════════════════════════════════════════════════
# API: MIS PRODUCTOS
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/myproducts', methods=['GET', 'POST'])
def handle_my_products():
    if request.method == 'GET':
        empresa_id = request.args.get('empresa_id')
        if not empresa_id:
            return jsonify({"error": "empresa_id es requerido"}), 400

        res = requests.get(
            f"{supabase_url('web_myproductos')}"
            f"?empresa_id=eq.{empresa_id}"
            f"&select=id,presentation,gramaje,unidad,linea_id,created_at,web_lineas(nombre)"
            f"&order=presentation.asc",
            headers=HEADERS, timeout=10
        )
        return jsonify({"products": res.json() if res.ok else []})

    # POST — usa helper compartido
    if not request.is_json:
        return jsonify({"error": "Se esperaba JSON"}), 400
    return _crear_producto("web_myproductos", request.json)


@app.route('/api/myproducts/<product_id>', methods=['PATCH', 'DELETE'])
def update_delete_myproduct(product_id):
    empresa_id = request.json.get('empresa_id') if request.method == 'PATCH' and request.is_json else request.args.get('empresa_id')
    if not empresa_id:
        return jsonify({"error": "empresa_id es requerido"}), 400
    return _actualizar_o_eliminar("web_myproductos", product_id, empresa_id,
                                  request.method, request.json if request.method == 'PATCH' else None)

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
        raw, content_type = _compress_image_bytes(file.read())
        file_name = f"planogramas/{empresa_id}/planograma_{timestamp}.jpg"
        storage_url = f"{SUPABASE_URL}/storage/v1/object/visits_photos/{file_name}"
        upload_resp = requests.put(
            storage_url,
            headers={"Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": content_type},
            data=raw,
            timeout=30
        )
        if upload_resp.status_code not in (200, 201):
            return jsonify({"success": False, "error": "Error al subir al storage"}), 500

        update_resp = requests.patch(
            f"{SUPABASE_URL}/rest/v1/empresas?id=eq.{empresa_id}",
            headers=HEADERS,
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
    res_e = requests.get(url_e, headers=HEADERS, timeout=5)
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
    res_p = requests.get(url_p, headers=HEADERS, timeout=5)
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
            headers=HEADERS, timeout=10
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
        headers=HEADERS, timeout=10
    )
    if not chk.ok or not chk.json():
        return jsonify({"error": "Producto no encontrado o sin permiso"}), 404

    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/web_producto_competencia",
        headers=HEADERS,
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
        headers=HEADERS, timeout=10
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



# ═══════════════════════════════════════════════════════════════════════════════
# RETENCIÓN DE FOTOS
# Borra before/ y after/ en visits_photos: por antigüedad (2 meses)
# y, si el bucket va a pasar el límite, también las más recientes.
# La fila de web_precios se conserva; solo se vacían las fotos.
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_ts(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _photo_cutoff():
    """Fecha límite: visitas más antiguas que 2 meses pierden sus fotos."""
    now = datetime.now(timezone.utc)
    month = now.month - PHOTO_RETENTION_MONTHS
    year = now.year
    while month <= 0:
        month += 12
        year -= 1
    # El día se ajusta si el mes destino es más corto (31 ene → 28 feb).
    day = min(now.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return now.replace(year=year, month=month, day=day)


def _compress_image_bytes(data: bytes, max_edge: int = 1280, quality: int = 60):
    """Devuelve JPEG reducido. Lado mayor de 1280 px y calidad 60."""
    from io import BytesIO
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(BytesIO(data)))
    if img.mode != "RGB":
        img = img.convert("RGB")
    width, height = img.size
    scale = min(1.0, max_edge / max(width, height))
    if scale < 1:
        img = img.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.LANCZOS,
        )
    out = BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue(), "image/jpeg"


def _storage_paths_for_photos(photos, folder):
    """Rutas dentro del bucket. Ignora base64 y planogramas."""
    paths = []
    for photo in safe_json_parse(photos):
        if not isinstance(photo, str):
            continue
        photo = photo.strip()
        if not photo or photo.startswith("data:"):
            continue
        path = None
        for marker in (
            f"/object/public/{VISIT_PHOTO_BUCKET}/",
            f"/object/{VISIT_PHOTO_BUCKET}/",
        ):
            if marker in photo:
                path = photo.split(marker, 1)[1].split("?", 1)[0].lstrip("/")
                break
        if path is None and not photo.startswith("http"):
            path = photo if "/" in photo else f"{folder}/{photo}"
        if not path or path.startswith("planogramas/") or ".." in path.split("/"):
            continue
        if not path.startswith(f"{folder}/") and "/" not in path:
            path = f"{folder}/{path}"
        paths.append(path)
    return paths


def _has_stored_photos(record):
    before = safe_json_parse(record.get("before_photos"))
    after = safe_json_parse(record.get("after_photos"))
    return bool(before or after)


def _restricted_message(resp):
    body = (resp.text or "")[:300]
    if resp.status_code >= 400 and (
        "restricted" in body.lower() or "exceed_storage" in body.lower()
    ):
        return body
    return None


def _delete_storage_paths(paths):
    """Borra objetos del bucket. Devuelve (cantidad, error)."""
    deleted = 0
    unique = list(dict.fromkeys(paths))
    url = f"{SUPABASE_URL}/storage/v1/object/{VISIT_PHOTO_BUCKET}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    for i in range(0, len(unique), 100):
        batch = unique[i:i + 100]
        resp = requests.delete(url, headers=headers, json={"prefixes": batch}, timeout=60)
        restricted = _restricted_message(resp)
        if restricted:
            return deleted, restricted
        if resp.status_code not in (200, 204):
            return deleted, f"storage {resp.status_code}: {(resp.text or '')[:300]}"
        deleted += len(batch)
    return deleted, None


def _clear_photo_columns(record_ids):
    cleared = 0
    headers = {**HEADERS, "Prefer": "return=minimal"}
    for i in range(0, len(record_ids), 50):
        batch = record_ids[i:i + 50]
        id_list = ",".join(str(rid) for rid in batch)
        resp = requests.patch(
            f"{SUPABASE_URL}/rest/v1/web_precios?id=in.({id_list})",
            headers=headers,
            json={"before_photos": [], "after_photos": []},
            timeout=30,
        )
        restricted = _restricted_message(resp)
        if restricted:
            return cleared, restricted
        if resp.status_code not in (200, 204):
            return cleared, f"web_precios {resp.status_code}: {(resp.text or '')[:300]}"
        cleared += len(batch)
    return cleared, None


def _purge_from_records(cutoff):
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    offset = 0
    page_size = 200
    files = []
    record_ids = []
    pages = 0
    while pages < 250:
        pages += 1
        headers = {**HEADERS, "Range": f"{offset}-{offset + page_size - 1}"}
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/web_precios",
            headers=headers,
            params=[
                ("select", "id,created_at,before_photos,after_photos"),
                ("created_at", f"lt.{cutoff_iso}"),
                ("order", "created_at.asc"),
            ],
            timeout=30,
        )
        restricted = _restricted_message(resp)
        if restricted:
            return None, restricted
        if not resp.ok:
            return None, f"web_precios {resp.status_code}: {(resp.text or '')[:300]}"
        rows = resp.json()
        if not rows:
            break
        for row in rows:
            if not _has_stored_photos(row):
                continue
            record_ids.append(row.get("id"))
            files.extend(_storage_paths_for_photos(row.get("before_photos"), "before"))
            files.extend(_storage_paths_for_photos(row.get("after_photos"), "after"))
        if len(rows) < page_size:
            break
        offset += page_size
    return {"files": files, "record_ids": [rid for rid in record_ids if rid is not None]}, None


def _storage_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def _object_size(obj):
    meta = obj.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError, ValueError):
            meta = {}
    try:
        return int(meta.get("size") or meta.get("contentLength") or 0)
    except (TypeError, ValueError):
        return 0


def _list_bucket_files():
    """Lista before/, after/ y planogramas/ con fecha y peso."""
    found = []
    headers = _storage_headers()
    prefixes = [f"{folder}/" for folder in VISIT_PHOTO_FOLDERS] + ["planogramas/"]
    for prefix in prefixes:
        offset = 0
        protected = prefix.startswith("planogramas/")
        while offset < 200000:
            resp = requests.post(
                f"{SUPABASE_URL}/storage/v1/object/list/{VISIT_PHOTO_BUCKET}",
                headers=headers,
                json={
                    "prefix": prefix,
                    "limit": 100,
                    "offset": offset,
                    "sortBy": {"column": "created_at", "order": "asc"},
                },
                timeout=30,
            )
            restricted = _restricted_message(resp)
            if restricted:
                return None, restricted
            if not resp.ok:
                return None, f"list {resp.status_code}: {(resp.text or '')[:300]}"
            objects = resp.json() or []
            if not objects:
                break
            for obj in objects:
                name = (obj.get("name") or "").strip()
                if not name or name.endswith("/") or obj.get("id") is None:
                    continue
                path = name if name.startswith(prefix) else f"{prefix}{name}"
                if ".." in path.split("/"):
                    continue
                found.append({
                    "path": path,
                    "created": _parse_ts(obj.get("created_at") or obj.get("updated_at")),
                    "size": _object_size(obj),
                    "protected": protected,
                })
            if len(objects) < 100:
                break
            offset += 100
    return found, None


def _select_files_to_delete(objects, cutoff):
    """2 meses siempre. Si el peso llega al 80 % del límite, sigue por las más viejas."""
    threshold = int(STORAGE_LIMIT_BYTES * STORAGE_HEADROOM_RATIO)
    visit = [obj for obj in objects if not obj["protected"]]
    total = sum(obj["size"] for obj in objects)
    aged = [obj for obj in visit if obj["created"] is not None and obj["created"] < cutoff]
    selected = list(aged)
    remaining = total - sum(obj["size"] for obj in selected)
    emergency = 0
    if remaining >= threshold:
        rest = [obj for obj in visit if obj not in selected]
        rest.sort(key=lambda obj: obj["created"] or datetime.min.replace(tzinfo=timezone.utc))
        for obj in rest:
            if remaining < threshold:
                break
            selected.append(obj)
            remaining -= obj["size"]
            emergency += 1
    return selected, {
        "total_bytes": total,
        "threshold_bytes": threshold,
        "emergency_files": emergency,
        "remaining_bytes": max(remaining, 0),
    }


def _records_matching_paths(paths):
    wanted = set(paths)
    if not wanted:
        return [], None
    offset = 0
    page_size = 200
    ids = []
    pages = 0
    while pages < 250:
        pages += 1
        headers = {**HEADERS, "Range": f"{offset}-{offset + page_size - 1}"}
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/web_precios",
            headers=headers,
            params=[
                ("select", "id,before_photos,after_photos"),
                ("order", "created_at.asc"),
            ],
            timeout=30,
        )
        restricted = _restricted_message(resp)
        if restricted:
            return None, restricted
        if not resp.ok:
            return None, f"web_precios {resp.status_code}: {(resp.text or '')[:300]}"
        rows = resp.json()
        if not rows:
            break
        for row in rows:
            files = _storage_paths_for_photos(row.get("before_photos"), "before")
            files.extend(_storage_paths_for_photos(row.get("after_photos"), "after"))
            if any(path in wanted for path in files):
                if row.get("id") is not None:
                    ids.append(row["id"])
        if len(rows) < page_size:
            break
        offset += page_size
    return ids, None


def purge_expired_visit_photos():
    """Borra fotos de más de 2 meses y, si el bucket se acerca al límite, las demás."""
    cutoff = _photo_cutoff()
    result = {
        "cutoff": cutoff.isoformat(),
        "mode": "storage",
        "files_deleted": 0,
        "records_cleared": 0,
        "emergency_files": 0,
        "total_bytes": None,
        "error": None,
    }
    objects, error = _list_bucket_files()
    if error:
        found, fallback_error = _purge_from_records(cutoff)
        result["mode"] = "records"
        if fallback_error:
            result["error"] = fallback_error
            return result
        deleted, delete_error = _delete_storage_paths(found["files"])
        result["files_deleted"] = deleted
        if delete_error:
            result["error"] = delete_error
            return result
        if found["record_ids"]:
            cleared, clear_error = _clear_photo_columns(found["record_ids"])
            result["records_cleared"] = cleared
            if clear_error:
                result["error"] = clear_error
        return result

    chosen, stats = _select_files_to_delete(objects, cutoff)
    result.update(stats)
    paths = [obj["path"] for obj in chosen]
    deleted, delete_error = _delete_storage_paths(paths)
    result["files_deleted"] = deleted
    if delete_error:
        result["error"] = delete_error
        return result

    record_ids, clear_lookup_error = _records_matching_paths(paths)
    if clear_lookup_error:
        result["error"] = clear_lookup_error
        return result
    if record_ids:
        cleared, clear_error = _clear_photo_columns(record_ids)
        result["records_cleared"] = cleared
        if clear_error:
            result["error"] = clear_error
    return result


def _photo_retention_loop():
    time.sleep(20)
    while True:
        if _PHOTO_PURGE_LOCK.acquire(blocking=False):
            try:
                summary = purge_expired_visit_photos()
                print(f"[fotos] purga {summary}")
            except Exception as exc:
                print(f"[fotos] error de purga: {exc}")
            finally:
                _PHOTO_PURGE_LOCK.release()
        time.sleep(24 * 60 * 60)


@app.before_request
def _boot_photo_retention():
    global _photo_retention_started
    if _photo_retention_started or os.getenv("PHOTO_RETENTION", "1") == "0":
        return
    _photo_retention_started = True
    threading.Thread(
        target=_photo_retention_loop, daemon=True, name="photo-retention"
    ).start()


# ─── Entrypoint ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if "--purgar-fotos" in sys.argv:
        print(json.dumps(purge_expired_visit_photos(), ensure_ascii=False))
    else:
        port = int(os.environ.get("PORT", 10000))
        app.run(host='0.0.0.0', port=port, debug=False)
