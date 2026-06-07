-- Grupos de regras para marcar e-mails como "muito importante" no módulo E-mails
-- Cada grupo tem um nome e uma lista de critérios (domínio do remetente ou e-mail específico)
CREATE TABLE IF NOT EXISTS regras_email_importante (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    familia_id  uuid NOT NULL REFERENCES familias(id) ON DELETE CASCADE,
    nome        text NOT NULL,
    criterios   jsonb NOT NULL DEFAULT '[]',  -- [{"tipo":"dominio","valor":"mundotelecom.com.br"}, {"tipo":"email","valor":"fulano@empresa.com"}]
    ativo       boolean NOT NULL DEFAULT true,
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
);

-- RLS
ALTER TABLE regras_email_importante ENABLE ROW LEVEL SECURITY;

CREATE POLICY "regras_email_importante_acesso"
    ON regras_email_importante FOR ALL
    USING  (familia_id = get_familia_id())
    WITH CHECK (familia_id = get_familia_id());

-- Índice
CREATE INDEX IF NOT EXISTS idx_regras_email_importante_fid
    ON regras_email_importante (familia_id);
