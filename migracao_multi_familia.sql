-- =============================================================
-- MIGRAÇÃO: Suporte Multi-Família
-- Execute este script no SQL Editor do Supabase.
-- Após executar, copie o FAMILIA_ID abaixo para o app_sentinela.py:
--   FAMILIA_ID = '11111111-1111-1111-1111-111111111111'
-- =============================================================

-- ---------------------------------------------------------------
-- 1. TABELA: familias
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS familias (
    id   uuid PRIMARY KEY DEFAULT '11111111-1111-1111-1111-111111111111',
    nome text NOT NULL,
    created_at timestamptz DEFAULT now()
);

INSERT INTO familias (id, nome)
VALUES ('11111111-1111-1111-1111-111111111111', 'Família Gonçalves')
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------
-- 2. TABELA: membros
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS membros (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    familia_id uuid NOT NULL REFERENCES familias(id) ON DELETE CASCADE,
    nome       text NOT NULL,
    created_at timestamptz DEFAULT now()
);

INSERT INTO membros (familia_id, nome) VALUES
    ('11111111-1111-1111-1111-111111111111', 'Fabrício'),
    ('11111111-1111-1111-1111-111111111111', 'Fabiana'),
    ('11111111-1111-1111-1111-111111111111', 'Gabriel'),
    ('11111111-1111-1111-1111-111111111111', 'Fernanda'),
    ('11111111-1111-1111-1111-111111111111', 'Ana Maria')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------
-- 3. TABELA: instituicoes_familia
--    Engloba cartões de crédito E bancos (extratos)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instituicoes_familia (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    familia_id              uuid NOT NULL REFERENCES familias(id) ON DELETE CASCADE,
    nome                    text NOT NULL,
    chave_checklist         text NOT NULL,
    tipo                    text NOT NULL CHECK (tipo IN ('fatura', 'extrato')),
    padroes_arquivo         text[] NOT NULL DEFAULT '{}',
    comprador_fixo          text,
    comprador_padrao        text,
    tem_multiplos_portadores boolean NOT NULL DEFAULT false,
    created_at              timestamptz DEFAULT now()
);

INSERT INTO instituicoes_familia
    (familia_id, nome, chave_checklist, tipo, padroes_arquivo, comprador_fixo, comprador_padrao, tem_multiplos_portadores)
VALUES
    ('11111111-1111-1111-1111-111111111111', 'Itaú',                 'Itaú',           'extrato', ARRAY['ITAU','ITAÚ'],                       NULL,       'Fabrício', false),
    ('11111111-1111-1111-1111-111111111111', 'CEF',                  'CEF',            'extrato', ARRAY['CEF','CAIXA'],                        NULL,       'Fabrício', false),
    ('11111111-1111-1111-1111-111111111111', 'BB OuroCard',          'BB',             'fatura',  ARRAY['BB'],                                'Fabiana',  NULL,        false),
    ('11111111-1111-1111-1111-111111111111', 'C6 Black',             'C6',             'fatura',  ARRAY['C6'],                                NULL,       NULL,        true),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black','Master Múltiplo','fatura',  ARRAY['MASTER','MULTIPLO','MÚLTIPLO'],       NULL,       NULL,        true),
    ('11111111-1111-1111-1111-111111111111', 'Visa Azul Infinity',   'Visa Azul',      'fatura',  ARRAY['VISA INFINITY','VISA AZUL'],          'Fabrício', NULL,        false)
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------
-- 4. TABELA: mapeamento_compradores
--    Nome como aparece na fatura → membro da família
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mapeamento_compradores (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    familia_id     uuid NOT NULL REFERENCES familias(id) ON DELETE CASCADE,
    cartao         text NOT NULL,
    nome_na_fatura text NOT NULL,
    nome_membro    text NOT NULL,
    created_at     timestamptz DEFAULT now()
);

INSERT INTO mapeamento_compradores (familia_id, cartao, nome_na_fatura, nome_membro) VALUES
    -- C6 Black
    ('11111111-1111-1111-1111-111111111111', 'C6 Black', 'FABRICIO GONCALVES',  'Fabrício'),
    ('11111111-1111-1111-1111-111111111111', 'C6 Black', 'FABRICIO GUIMARAES',  'Fabrício'),
    ('11111111-1111-1111-1111-111111111111', 'C6 Black', 'GABRIEL LOTT',        'Gabriel'),
    ('11111111-1111-1111-1111-111111111111', 'C6 Black', 'GABRIELLOTT',         'Gabriel'),
    ('11111111-1111-1111-1111-111111111111', 'C6 Black', 'FERNANDALOTT',        'Fernanda'),
    ('11111111-1111-1111-1111-111111111111', 'C6 Black', 'FABIANALOTT',         'Fabiana'),
    -- Master Múltiplo Black
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'FABRICIO GUIMARAES', 'Fabrício'),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'FABIANA MORAES',     'Fabiana'),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'GABRIEL LOTT',       'Gabriel'),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'FERNANDA LOTT',      'Fernanda'),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'GABRIELLOTT',        'Gabriel'),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'FERNANDALOTT',       'Fernanda'),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'FABIANALOTT',        'Fabiana')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------
-- 5. TABELA: estabelecimentos_ignorados
--    Lançamentos que devem ser pulados no processamento
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS estabelecimentos_ignorados (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    familia_id uuid NOT NULL REFERENCES familias(id) ON DELETE CASCADE,
    texto      text NOT NULL,
    tipo       text NOT NULL CHECK (tipo IN ('fatura', 'extrato')),
    created_at timestamptz DEFAULT now()
);

INSERT INTO estabelecimentos_ignorados (familia_id, texto, tipo) VALUES
    -- Ignorados em faturas de cartão (substituídos por lançamentos fixos)
    ('11111111-1111-1111-1111-111111111111', 'PAG BOLETO SUL AMERICA',   'fatura'),
    ('11111111-1111-1111-1111-111111111111', 'PIX QRS SUL AMERICA',      'fatura'),
    ('11111111-1111-1111-1111-111111111111', 'OUTRAS DESPESAS ANA MARIA','fatura'),
    ('11111111-1111-1111-1111-111111111111', 'CREDIARIO AUTOM',          'fatura'),
    ('11111111-1111-1111-1111-111111111111', 'PIX TRANSF FABIANA',       'fatura'),
    -- Ignorados em extratos bancários
    ('11111111-1111-1111-1111-111111111111', 'PAG BOLETO BANCO C6 S.A',  'extrato'),
    ('11111111-1111-1111-1111-111111111111', 'LIQUIDA OPERACAO',         'extrato')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------
-- 6. TABELA: lancamentos_fixos
--    Gerados automaticamente a cada processamento
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lancamentos_fixos (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    familia_id     uuid NOT NULL REFERENCES familias(id) ON DELETE CASCADE,
    estabelecimento text NOT NULL,
    descricao      text,
    valor_parcela  numeric(12,2) NOT NULL,
    categoria      text,
    subcategoria   text,
    comprado_por   text,
    comprado_para  text,
    tipo_pagamento text,
    periodicidade  text,
    importancia    text,
    cartao         text,
    gatilho        text NOT NULL DEFAULT 'extrato_itau' CHECK (gatilho IN ('extrato_itau', 'sempre')),
    created_at     timestamptz DEFAULT now()
);

INSERT INTO lancamentos_fixos
    (familia_id, estabelecimento, descricao, valor_parcela, categoria, subcategoria,
     comprado_por, comprado_para, tipo_pagamento, periodicidade, importancia, cartao, gatilho)
VALUES
    -- Planos de Saúde (gatilho: extrato Itaú)
    ('11111111-1111-1111-1111-111111111111',
     'PAG BOLETO SUL AMERICA COMPANHIA DE SEG', 'Plano de Saúde - Fabrício',
     2272.44, 'Saúde', 'Plano de Saúde', 'Fabrício', 'Família', '06', 'Fixa Mensal', 'Essencial', 'Itaú', 'extrato_itau'),

    ('11111111-1111-1111-1111-111111111111',
     'PAG BOLETO SUL AMERICA COMPANHIA DE SEG', 'Plano de Saúde - Fabiana',
     2272.44, 'Saúde', 'Plano de Saúde', 'Fabiana', 'Família', '06', 'Fixa Mensal', 'Essencial', 'Itaú', 'extrato_itau'),

    ('11111111-1111-1111-1111-111111111111',
     'PAG BOLETO SUL AMERICA COMPANHIA DE SEG', 'Plano de Saúde - Fernanda',
     637.98, 'Saúde', 'Plano de Saúde', 'Fernanda', 'Família', '06', 'Fixa Mensal', 'Essencial', 'Itaú', 'extrato_itau'),

    ('11111111-1111-1111-1111-111111111111',
     'PAG BOLETO SUL AMERICA COMPANHIA DE SEG', 'Plano de Saúde - Ana Maria',
     3827.93, 'Ana Maria', 'Plano de Saúde', 'Ana Maria', 'Família', '06', 'Fixa Mensal', 'Essencial', 'Itaú', 'extrato_itau'),

    -- Faxineira Ana Maria (gatilho: extrato Itaú)
    ('11111111-1111-1111-1111-111111111111',
     'OUTRAS DESPESAS ANA MARIA', 'Faxineira Ana Maria',
     1600.00, 'Ana Maria', 'Outras Despesas', 'Ana Maria', 'Família', '02', 'Fixa Mensal', 'Essencial', 'Itaú', 'extrato_itau')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------
-- 7. ADICIONAR familia_id NAS TABELAS EXISTENTES
-- ---------------------------------------------------------------
ALTER TABLE lancamentos
    ADD COLUMN IF NOT EXISTS familia_id uuid REFERENCES familias(id);

ALTER TABLE regras_classificacao
    ADD COLUMN IF NOT EXISTS familia_id uuid REFERENCES familias(id);

ALTER TABLE categorias
    ADD COLUMN IF NOT EXISTS familia_id uuid REFERENCES familias(id);

-- ---------------------------------------------------------------
-- 8. MIGRAR DADOS EXISTENTES PARA A FAMÍLIA GONÇALVES
-- ---------------------------------------------------------------
UPDATE lancamentos
    SET familia_id = '11111111-1111-1111-1111-111111111111'
    WHERE familia_id IS NULL;

UPDATE regras_classificacao
    SET familia_id = '11111111-1111-1111-1111-111111111111'
    WHERE familia_id IS NULL;

UPDATE categorias
    SET familia_id = '11111111-1111-1111-1111-111111111111'
    WHERE familia_id IS NULL;

-- ---------------------------------------------------------------
-- FIM DA MIGRAÇÃO
-- Próximo passo: adicionar em app_sentinela.py, linha após GEMINI_KEY:
--   FAMILIA_ID = '11111111-1111-1111-1111-111111111111'
-- ---------------------------------------------------------------
