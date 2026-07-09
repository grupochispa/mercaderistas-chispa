-- ═══════════════════════════════════════════════════════════════════
-- SQL para crear la tabla web_tasa en Supabase
-- ═══════════════════════════════════════════════════════════════════

-- 1. Función para actualizar updated_at automáticamente
CREATE OR REPLACE FUNCTION update_web_tasa_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW() AT TIME ZONE 'America/Caracas';
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 2. Crear la tabla
CREATE TABLE public.web_tasa (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  fecha date NOT NULL,
  tasa_bs_usd numeric(10, 4) NOT NULL,
  tasa_cop_usd numeric(10, 4) NOT NULL,
  created_at timestamp without time zone NOT NULL DEFAULT (NOW() AT TIME ZONE 'America/Caracas'),
  updated_at timestamp without time zone NOT NULL DEFAULT (NOW() AT TIME ZONE 'America/Caracas'),
  CONSTRAINT web_tasa_pkey PRIMARY KEY (id),
  CONSTRAINT web_tasa_fecha_key UNIQUE (fecha)
) TABLESPACE pg_default;

-- 3. Índice para búsqueda rápida por fecha
CREATE INDEX IF NOT EXISTS idx_web_tasa_fecha 
  ON public.web_tasa USING btree (fecha) 
  TABLESPACE pg_default;

-- 4. Trigger para actualizar updated_at
CREATE TRIGGER update_web_tasa_updated_at_trigger
  BEFORE UPDATE ON web_tasa
  FOR EACH ROW
  EXECUTE FUNCTION update_web_tasa_updated_at();

-- 5. Permisos (ajustar según sea necesario)
ALTER TABLE public.web_tasa ENABLE ROW LEVEL SECURITY;
GRANT ALL ON TABLE public.web_tasa TO anon;
GRANT ALL ON TABLE public.web_tasa TO authenticated;
GRANT ALL ON TABLE public.web_tasa TO service_role;