-- Tabela de integrações externas por família (Z-API, Google, etc.)
CREATE TABLE IF NOT EXISTS integracoes_familia (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    familia_id uuid NOT NULL REFERENCES familias(id) ON DELETE CASCADE,
    tipo       text NOT NULL,                    -- 'zapi', 'google_calendar', 'gmail'
    config     jsonb NOT NULL DEFAULT '{}',
    ativo      boolean NOT NULL DEFAULT true,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    UNIQUE (familia_id, tipo)
);

-- RLS
ALTER TABLE integracoes_familia ENABLE ROW LEVEL SECURITY;

CREATE POLICY "integracoes_familia_acesso"
    ON integracoes_familia FOR ALL
    USING (familia_id = get_familia_id());

-- Índice
CREATE INDEX IF NOT EXISTS idx_integracoes_familia_fid
    ON integracoes_familia (familia_id);
